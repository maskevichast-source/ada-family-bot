"""Обработка фото, PDF и скриншотов чеков и переводов."""

import asyncio

from aiogram.types import Message
from services.vision import parse_receipt
from services.sheets import append_transaction, normalize_necessity
from services.telegram_safe import safe_answer
from services.pending_receipts import set_pending, ack_pending
from services.state import dialogue_key
from config import get_authorized_user_name
from services.categories import (
    TYPE_EXPENSE, TYPE_INCOME, is_income_type,
    EXPENSE_CATEGORIES, INCOME_CATEGORIES,
    FALLBACK_EXPENSE_CATEGORY, FALLBACK_INCOME_CATEGORY,
    normalize_category, normalize_subcategory,
    validate_transaction_category_subcategory,
)
from services.banks import normalize_bank_source


def _format_currency(value):
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except (ValueError, TypeError):
        return str(value)


def _format_receipt_report(transactions: list[dict], ai_comment: str = "") -> str:
    lines = ["📸 **Записано по чеку:**"]
    for tx in transactions:
        amt = _format_currency(tx.get("amount", 0))
        curr = tx.get("currency", "KZT")
        bank = tx.get("bank", "Не указан")
        cat = tx.get("category", "")
        comm = str(tx.get("user_comment") or "").strip()
        comm_str = f" ({comm})" if comm else ""
        sign = "+ " if is_income_type(tx.get("type")) else ""
        lines.append(f"• {sign}{amt} {curr} | {bank} | {cat}{comm_str}")
    if ai_comment:
        lines.append(f"\n💬 {ai_comment}")
    return "\n".join(lines)


async def handle_media(message: Message):
    user_name = get_authorized_user_name(message.from_user.id, message.from_user.first_name) or message.from_user.first_name or "Пользователь"
    chat_id = message.chat.id
    pending_key = dialogue_key(chat_id, message.from_user.id)
    caption = message.caption or ""

    if message.photo:
        photo = message.photo[-1]
        file = await message.bot.get_file(photo.file_id)
        file_bytes = await message.bot.download_file(file.file_path)
        file_bytes = file_bytes.read()
        filename = f"{photo.file_id}.jpg"
    elif message.document:
        doc = message.document
        file = await message.bot.get_file(doc.file_id)
        file_bytes = await message.bot.download_file(file.file_path)
        file_bytes = file_bytes.read()
        filename = doc.file_name or f"{doc.file_id}.pdf"
    else:
        await safe_answer(message, "Не распознала формат файла. Пришли фото или PDF.")
        return

    result = await parse_receipt(file_bytes, filename, caption, user_name)
    transactions = result.get("transactions", [])
    reply = result.get("reply", "")

    if not transactions:
        await safe_answer(message, reply or "Не удалось распознать чек.")
        return

    validated_transactions = []
    has_income = False

    for tx in transactions:
        is_income = is_income_type(tx.get("type"))
        if is_income:
            has_income = True
            tx["type"] = TYPE_INCOME
            valid_categories = INCOME_CATEGORIES
            fallback_cat = FALLBACK_INCOME_CATEGORY
        else:
            tx["type"] = TYPE_EXPENSE
            valid_categories = EXPENSE_CATEGORIES
            fallback_cat = FALLBACK_EXPENSE_CATEGORY

        raw_cat = tx.get("category")
        cat = normalize_category(raw_cat, valid_categories, fallback_cat)
        tx["category"] = cat

        if not is_income:
            raw_sub = tx.get("subcategory")
            _, valid_sub = validate_transaction_category_subcategory(cat, raw_sub, valid_categories, fallback_cat)
            tx["subcategory"] = valid_sub or normalize_subcategory(None, cat, "")
        else:
            tx["subcategory"] = ""

        tx["necessity"] = normalize_necessity(tx.get("necessity"), cat)
        tx["source"] = normalize_bank_source(tx.get("bank"), tx.get("source"))
        if not tx.get("bank"): tx["bank"] = "Не указан"
        if not tx.get("currency"): tx["currency"] = "KZT"
        if not tx.get("funds_type"): tx["funds_type"] = "Собственные"
        if not tx.get("resource"): tx["resource"] = "Карта"
        tx["user"] = user_name
        if not tx.get("merchant"): tx["merchant"] = ""
        if not tx.get("user_comment"): tx["user_comment"] = caption or ("Пополнение/доход" if is_income else "")
        if not tx.get("ai_comment"): tx["ai_comment"] = reply or ""

        validated_transactions.append(tx)

    # Стабильный ID на основе сообщения — чтобы повторный вызов handle_media
    # для ТОГО ЖЕ сообщения (retry после сетевой ошибки) не записывал уже
    # успешно сохранённые позиции чека ещё раз (append_transaction сам
    # пропускает дубли по transaction_id).
    for idx, tx in enumerate(validated_transactions):
        if not tx.get("transaction_id"):
            tx["transaction_id"] = f"MEDIA_{chat_id}_{message.message_id}_{idx}"

    # Доходы записываем сразу и гарантированно в Google Sheets
    if has_income or caption:
        try:
            for tx in validated_transactions:
                if caption:
                    tx["user_comment"] = caption
                tx["user"] = user_name
                await asyncio.to_thread(append_transaction, tx)
        except Exception:
            # Часть позиций могла уже успешно записаться — не теряем то,
            # что ещё не сохранилось: кладём в очередь для ретрая (сам
            # append_transaction идемпотентен по transaction_id, так что
            # повторный вызов handle_media с тем же сообщением дозапишет
            # только недостающее, а не задублирует уже сохранённое).
            set_pending(pending_key, validated_transactions, user_name)
            raise
        # Успешно сохранили — если до этого здесь лежал "хвост" от
        # предыдущей неудачной попытки по этому же чату/пользователю,
        # он больше не актуален.
        ack_pending(pending_key)
        report = _format_receipt_report(validated_transactions, reply)
        await safe_answer(message, report)
        return

    # Проверяем транзакции с низкой уверенностью для обычных чеков без подписи
    low_confidence_txs = [
        tx for tx in validated_transactions
        if float(tx.get("confidence", 1.0)) < 0.8 and tx.get("alternatives")
    ]

    if low_confidence_txs:
        set_pending(pending_key, validated_transactions, user_name)
        await safe_answer(
            message,
            (
                f"Распознала {len(validated_transactions)} позиций, но по одной категории сомневаюсь. "
                "Напиши короткий комментарий к чеку — и я сохраню всё вместе."
            ),
        )
        return

    set_pending(pending_key, validated_transactions, user_name)
    await safe_answer(
        message,
        reply or f"Распознала {len(validated_transactions)} покупок. Напиши комментарий к чеку, и я запишу."
    )

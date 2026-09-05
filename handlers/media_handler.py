"""Обработка фото, PDF и скриншотов чеков."""

import asyncio

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from services.vision import parse_receipt
from services.sheets import append_transaction, normalize_necessity
from services.telegram_safe import safe_answer
from services.pending_receipts import set_pending
from services.pending_clarifications import set_clarification
from config import get_authorized_user_name
from services.categories import (
    TYPE_EXPENSE, TYPE_INCOME,
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
    """Формирует аккуратный список записанных по чеку трат (как на Скриншоте 2)."""
    lines = ["📸 **Записано по чеку:**"]
    for tx in transactions:
        amt = _format_currency(tx.get("amount", 0))
        curr = tx.get("currency", "KZT")
        bank = tx.get("bank", "Не указан")
        cat = tx.get("category", "")
        comm = str(tx.get("user_comment") or "").strip()
        comm_str = f" ({comm})" if comm else ""
        lines.append(f"• {amt} {curr} | {bank} | {cat}{comm_str}")
    if ai_comment:
        lines.append(f"\n💬 {ai_comment}")
    return "\n".join(lines)


async def handle_media(message: Message):
    user_name = get_authorized_user_name(message.from_user.id, message.from_user.first_name) or message.from_user.first_name or "Пользователь"
    chat_id = message.chat.id
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
        await safe_answer(message, "Не распознал формат файла. Пришли фото или PDF.")
        return

    result = await parse_receipt(file_bytes, filename, caption, user_name)
    transactions = result.get("transactions", [])
    reply = result.get("reply", "")

    if not transactions:
        await safe_answer(message, reply or "Не удалось распознать чек.")
        return

    # Валидация и заполнение ВСЕХ полей
    validated_transactions = []
    for tx in transactions:
        tx_type = str(tx.get("type") or TYPE_EXPENSE).strip().upper()
        valid_categories = INCOME_CATEGORIES if tx_type == TYPE_INCOME else EXPENSE_CATEGORIES
        fallback_cat = FALLBACK_INCOME_CATEGORY if tx_type == TYPE_INCOME else FALLBACK_EXPENSE_CATEGORY

        raw_cat = tx.get("category")
        cat = normalize_category(raw_cat, valid_categories, fallback_cat)
        tx["category"] = cat
        raw_sub = tx.get("subcategory")
        _, valid_sub = validate_transaction_category_subcategory(cat, raw_sub, valid_categories, fallback_cat)
        tx["subcategory"] = valid_sub or normalize_subcategory(None, cat, "")
        tx["necessity"] = normalize_necessity(tx.get("necessity"), cat)
        tx["source"] = normalize_bank_source(tx.get("bank"), tx.get("source"))
        if not tx.get("bank"): tx["bank"] = "Не указан"
        if not tx.get("currency"): tx["currency"] = "KZT"
        if not tx.get("funds_type"): tx["funds_type"] = "Собственные"
        if not tx.get("resource"): tx["resource"] = "Карта"
        tx["user"] = user_name
        if not tx.get("merchant"): tx["merchant"] = ""
        if not tx.get("user_comment"): tx["user_comment"] = caption or ""
        if not tx.get("ai_comment"): tx["ai_comment"] = reply or ""

        validated_transactions.append(tx)

    # Проверяем, есть ли транзакции с низкой confidence (если пользователь не дал подпись)
    low_confidence_txs = [
        tx for tx in validated_transactions
        if float(tx.get("confidence", 1.0)) < 0.8 and tx.get("alternatives")
    ]

    if low_confidence_txs and not caption:
        # Не теряем остальные позиции чека: весь чек ждёт короткий комментарий/уточнение.
        # Раньше в pending_clarification уходила только одна позиция, а остальные могли потеряться.
        set_pending(chat_id, validated_transactions, user_name)
        await safe_answer(
            message,
            (
                f"Распознала {len(validated_transactions)} позиций, но по одной категории сомневаюсь. "
                "Напиши короткий комментарий к чеку или уточни категорию — и я сохраню всё вместе."
            ),
        )
        return

    if caption:
        for tx in validated_transactions:
            tx["user_comment"] = caption
            tx["user"] = user_name
            await asyncio.to_thread(append_transaction, tx)
        report = _format_receipt_report(validated_transactions, reply)
        await safe_answer(message, report)
    else:
        set_pending(chat_id, validated_transactions, user_name)
        await safe_answer(
            message,
            reply or f"Распознала {len(validated_transactions)} покупок. Напиши комментарий к чеку, и я запишу."
        )

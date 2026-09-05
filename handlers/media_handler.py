"""Обработка фото, изображений файлом и PDF-чеков."""

import asyncio
import logging
from pathlib import Path

from config import get_authorized_user_name
from services.telegram_safe import safe_answer
from services.vision import parse_receipt
from services.sheets import append_transaction
from services.memory import add_chat_message
from services.categories import (
    TYPE_EXPENSE, TYPE_INCOME,
    EXPENSE_CATEGORIES, INCOME_CATEGORIES,
    FALLBACK_EXPENSE_CATEGORY, FALLBACK_INCOME_CATEGORY,
    normalize_category, normalize_subcategory,
    validate_transaction_category_subcategory,
)
from services.banks import normalize_bank_source
from services.sheets import normalize_necessity
from services.pending_receipts import set_pending


def _format_currency(value):
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except Exception:
        return str(value)


async def handle_media(message):
    uid = getattr(message, "from_user", None)
    raw_uid = uid.id if uid else None
    raw_fn = getattr(uid, "first_name", "") or ""
    owner = get_authorized_user_name(raw_uid, raw_fn) or "Пользователь"
    chat_id = message.chat.id
    caption = str(getattr(message, "caption", None) or "").strip()

    photos = getattr(message, "photo", None)
    doc = getattr(message, "document", None)
    item = photos[-1] if photos else doc

    if item is None:
        await safe_answer(message, "Пришли фото чека или PDF-документ.")
        return

    try:
        await message.bot.send_chat_action(chat_id=chat_id, action="typing")
        file = await message.bot.get_file(item.file_id)
        downloaded = await message.bot.download_file(file.file_path)
        data = downloaded.read()
    except Exception as e:
        logging.exception(f"[Media Download Error]: {e}")
        await safe_answer(message, "Не удалось скачать файл из Telegram. Попробуй ещё раз.")
        return

    filename = Path(getattr(doc, "file_name", "") or "receipt.jpg").name

    # Голосовые файлы отправленные документом
    if filename.lower().endswith((".mp3", ".m4a", ".wav", ".ogg", ".oga")):
        from services.voice import transcribe_voice
        from handlers.text_handler import _process_text_message
        text = await transcribe_voice(data, filename)
        if text:
            await safe_answer(message, f"🎤 «{text}»")
            await _process_text_message(message, text)
        else:
            await safe_answer(message, "Не удалось распознать аудио.")
        return

    # Распознавание чека
    result = await parse_receipt(data, filename, caption, owner)
    transactions = result.get("transactions") or []

    if not transactions:
        await safe_answer(message, result.get("reply") or "Не нашла операций на чеке. Пришли чёткое фото.")
        return

    # Заполнение и валидация полей
    validated = []
    for tx in transactions:
        tx_type = str(tx.get("type") or TYPE_EXPENSE).strip().upper()
        valid_cats = INCOME_CATEGORIES if tx_type == TYPE_INCOME else EXPENSE_CATEGORIES
        fallback = FALLBACK_INCOME_CATEGORY if tx_type == TYPE_INCOME else FALLBACK_EXPENSE_CATEGORY

        cat = normalize_category(tx.get("category"), valid_cats, fallback)
        tx["category"] = cat
        _, sub = validate_transaction_category_subcategory(cat, tx.get("subcategory"))
        tx["subcategory"] = sub or normalize_subcategory(None, cat, "")
        tx["necessity"] = normalize_necessity(tx.get("necessity"), cat)
        tx["source"] = normalize_bank_source(tx.get("bank"), tx.get("source"))

        tx["user"] = owner
        tx["currency"] = "KZT"
        if not tx.get("funds_type"): tx["funds_type"] = "Собственные"
        if not tx.get("resource"): tx["resource"] = "Карта"
        if not tx.get("user_comment"): tx["user_comment"] = caption or ""
        validated.append(tx)

    # Если была подпись — записываем сразу
    if caption:
        for tx in validated:
            append_transaction(tx)
        lines = [f"📸 **Записано по чеку ({len(validated)} поз.):**"]
        for tx in validated:
            lines.append(f"• {_format_currency(tx.get('amount'))} KZT | {tx.get('category')} ({tx.get('user_comment')})")
        await safe_answer(message, "\n".join(lines))
    else:
        # Ждем комментарий
        set_pending(chat_id, validated, owner)
        await safe_answer(message, f"📸 Распознала {len(validated)} покупок в чеке на сумму {_format_currency(sum(t.get('amount', 0) for t in validated))} KZT. Напиши комментарий (или «без комментария»), и я сохраню.")

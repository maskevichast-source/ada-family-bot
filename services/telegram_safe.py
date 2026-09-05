"""Безопасная отправка сообщений в Telegram — без потери текста из-за битой разметки.

В коде ответы оформлены GitHub-style Markdown (`**жирный**`). Telegram legacy
Markdown понимает `*жирный*`, поэтому перед отправкой мы мягко конвертируем
двойные звёздочки в формат Telegram. Если Telegram всё равно ругнётся на
разметку из-за свободного текста от ИИ — отправляем без parse_mode.
"""

import re
from aiogram.exceptions import TelegramBadRequest


def _looks_like_markdown_error(error: Exception) -> bool:
    text = str(error).lower()
    return "parse" in text or "entit" in text


def _telegram_markdown(text: str) -> str:
    # **text** -> *text* для Telegram Markdown. Одинарные * не трогаем.
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", str(text), flags=re.DOTALL)


async def safe_answer(message, text: str, **kwargs):
    """Замена message.answer(text) — с откатом на обычный текст при ошибке разметки."""
    text_to_send = _telegram_markdown(text)
    try:
        return await message.answer(text_to_send, **kwargs)
    except TelegramBadRequest as error:
        if _looks_like_markdown_error(error):
            kwargs.pop("parse_mode", None)
            return await message.answer(text, parse_mode=None, **kwargs)
        raise


async def safe_send_message(bot, chat_id, text: str, **kwargs):
    """Замена bot.send_message(chat_id=..., text=...) — с тем же откатом."""
    text_to_send = _telegram_markdown(text)
    try:
        return await bot.send_message(chat_id=chat_id, text=text_to_send, **kwargs)
    except TelegramBadRequest as error:
        if _looks_like_markdown_error(error):
            kwargs.pop("parse_mode", None)
            return await bot.send_message(chat_id=chat_id, text=text, parse_mode=None, **kwargs)
        raise

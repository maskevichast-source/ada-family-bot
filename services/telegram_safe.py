"""Безопасная отправка сообщений в Telegram — без потери текста из-за битой разметки.

В коде ответы оформлены GitHub-style Markdown (`**жирный**`). Telegram legacy
Markdown понимает `*жирный*`, поэтому перед отправкой мы мягко конвертируем
двойные звёздочки в формат Telegram. Если Telegram всё равно ругнётся на
разметку из-за свободного текста от ИИ — отправляем без parse_mode.
"""

import re
from aiogram.exceptions import TelegramBadRequest

# Запас под жёсткий лимит Telegram в 4096 символов на сообщение.
TELEGRAM_LIMIT = 3500


def chunks(text: str, limit: int = TELEGRAM_LIMIT):
    """Режет длинный текст на части безопасного размера для Telegram.

    Режет по переносу строки, если он есть в пределах лимита — чтобы не
    разрывать строку посередине. Раньше длинные ответы (большие списки
    долгов/напоминаний/транзакций) просто падали с ошибкой Telegram
    "message is too long" и терялись.
    """
    text = str(text or "")
    while text:
        if len(text) <= limit:
            yield text
            return
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        yield text[:cut]
        text = text[cut:].lstrip("\n")


def _looks_like_markdown_error(error: Exception) -> bool:
    text = str(error).lower()
    return "parse" in text or "entit" in text


def _telegram_markdown(text: str) -> str:
    # **text** -> *text* для Telegram Markdown. Одинарные * не трогаем.
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", str(text), flags=re.DOTALL)


async def safe_answer(message, text: str, **kwargs):
    """Замена message.answer(text) — с откатом на обычный текст при ошибке разметки
    и с разбивкой на части, если сообщение длиннее лимита Telegram."""
    parts = list(chunks(text))
    result = None
    for part in parts:
        text_to_send = _telegram_markdown(part)
        try:
            result = await message.answer(text_to_send, **kwargs)
        except TelegramBadRequest as error:
            if _looks_like_markdown_error(error):
                kw = dict(kwargs)
                kw.pop("parse_mode", None)
                result = await message.answer(part, parse_mode=None, **kw)
            else:
                raise
    _remember(getattr(getattr(message, "chat", None), "id", None), text)
    return result


async def safe_send_message(bot, chat_id, text: str, **kwargs):
    """Замена bot.send_message(chat_id=..., text=...) — с тем же откатом и разбивкой."""
    parts = list(chunks(text))
    result = None
    for part in parts:
        text_to_send = _telegram_markdown(part)
        try:
            result = await bot.send_message(chat_id=chat_id, text=text_to_send, **kwargs)
        except TelegramBadRequest as error:
            if _looks_like_markdown_error(error):
                kw = dict(kwargs)
                kw.pop("parse_mode", None)
                result = await bot.send_message(chat_id=chat_id, text=part, parse_mode=None, **kw)
            else:
                raise
    _remember(chat_id, text)
    return result


def _remember(chat_id, text: str):
    """Ответы бота тоже попадают в историю чата — иначе следующий вызов ИИ
    не видит, что бот только что сказал (в частности, при чисто локальных
    ответах без похода к ИИ, где раньше add_chat_message для ответа бота
    никто не вызывал)."""
    if chat_id is None or not text:
        return
    try:
        from services.memory import add_chat_message
        add_chat_message(chat_id, "Ада", text)
    except Exception:
        pass

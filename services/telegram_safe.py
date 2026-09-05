"""Chunk long messages; fall back only on markup errors, not network failures."""
import re
from aiogram.exceptions import TelegramBadRequest

def _looks_like_markdown_error(error):
    text = str(error).lower()
    return "parse" in text or "entit" in text

def _telegram_markdown(text):
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", str(text), flags=re.DOTALL)

def chunks(text, limit=3500):
    text = str(text)
    while text:
        if len(text) <= limit:
            yield text
            break
        split = text.rfind("\n", 0, limit)
        if split < limit // 2:
            split = limit
        yield text[:split]
        text = text[split:].lstrip("\n")

async def _send(send, text, kwargs):
    last = None
    parts = list(chunks(text)) or [" "]
    for part in parts:
        options = dict(kwargs)
        if len(parts) > 1:
            options["parse_mode"] = None
        formatted = part if options.get("parse_mode", "Markdown") in (None, "HTML") else _telegram_markdown(part)
        try:
            last = await send(formatted, **options)
        except TelegramBadRequest as error:
            if not _looks_like_markdown_error(error):
                raise
            options["parse_mode"] = None
            last = await send(part, **options)
    return last

async def safe_answer(message, text, **kwargs):
    result = await _send(message.answer, text, kwargs)
    from services.memory import add_chat_message
    add_chat_message(message.chat.id, "Ада", str(text))
    return result

async def safe_send_message(bot, chat_id, text, **kwargs):
    async def send(part, **options):
        return await bot.send_message(chat_id=chat_id, text=part, **options)
    result = await _send(send, text, kwargs)
    from services.memory import add_chat_message
    add_chat_message(chat_id, "Ада", str(text))
    return result

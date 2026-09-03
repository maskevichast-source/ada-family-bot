"""Безопасная отправка сообщений в Telegram — без потери текста из-за битой разметки.

С тех пор как боту задан parse_mode=Markdown по умолчанию (чтобы **жирный
текст** реально был жирным), ЛЮБОЙ ответ, куда попадает свободный текст от
ИИ (комментарий к покупке, "reply", часть текста от DeepSeek/Vision), может
содержать необработанные *, _, `, [ и т.п. Telegram в этом случае просто
отказывается доставить сообщение (TelegramBadRequest: can't parse entities)
— и человек тихо ничего не видит, будто бот вообще не ответил.

safe_answer / safe_send_message пробуют отправить как есть, а при ошибке
разметки — повторяют тем же текстом, но без форматирования, чтобы
сообщение дошло в любом случае.
"""

from aiogram.exceptions import TelegramBadRequest


def _looks_like_markdown_error(error: Exception) -> bool:
    text = str(error).lower()
    return "parse" in text or "entit" in text


async def safe_answer(message, text: str, **kwargs):
    """Замена message.answer(text) — с откатом на обычный текст при ошибке разметки."""
    try:
        return await message.answer(text, **kwargs)
    except TelegramBadRequest as error:
        if _looks_like_markdown_error(error):
            return await message.answer(text, parse_mode=None, **kwargs)
        raise


async def safe_send_message(bot, chat_id, text: str, **kwargs):
    """Замена bot.send_message(chat_id=..., text=...) — с тем же откатом."""
    try:
        return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except TelegramBadRequest as error:
        if _looks_like_markdown_error(error):
            return await bot.send_message(chat_id=chat_id, text=text, parse_mode=None, **kwargs)
        raise


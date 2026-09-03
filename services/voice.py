"""Расшифровка голосовых сообщений в текст через OpenAI Whisper.

Логика простая: скачанный .oga/.ogg файл голосового сообщения из Telegram
отправляется как есть в Whisper API (формат ogg/oga поддерживается), в
ответ приходит обычный текст — дальше он обрабатывается ТОЧНО так же, как
если бы пользователь напечатал его руками (те же интенты, та же логика).

Используется тот же OPENAI_API_KEY, что и для распознавания чеков
(services/vision.py) — отдельный ключ или библиотека не нужны.
"""

from openai import AsyncOpenAI

from config import OPENAI_API_KEY


async def transcribe_voice(file_bytes: bytes, filename: str = "voice.oga") -> str:
    """Расшифровать голосовое сообщение в текст.

    Возвращает пустую строку при любой ошибке (нет файла, сбой API,
    таймаут) — вызывающий код должен сам решить, что ответить пользователю.
    """
    if not file_bytes:
        return ""
    try:
        client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=45.0, max_retries=2)
        transcript = await client.audio.transcriptions.create(
            model="whisper-1",
            file=(filename, file_bytes),
            language="ru",
        )
        return (transcript.text or "").strip()
    except Exception as error:
        print(f"[Голос] Не удалось распознать: {error}")
        return ""

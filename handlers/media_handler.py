"""Photo, image-document, multi-page PDF and audio-document -> validated author-scoped flow."""
import asyncio
import hashlib
import logging
from pathlib import Path
from config import get_authorized_user_name
from services.telegram_safe import safe_answer
from services.vision import parse_receipt
from services.document_reader import is_audio, MAX_FILE
from services.receipt_flow import offer
from services.memory import add_chat_message

async def handle_media(message):
    uid=getattr(message,"from_user",None)
    owner=get_authorized_user_name(uid.id) if uid else None
    if not owner:
        await safe_answer(message,"Нет доступа к семейным данным.");return
    caption=str(getattr(message,"caption",None) or "")
    add_chat_message(message.chat.id,owner,"[Документ] "+caption)
    photos=getattr(message,"photo",None)
    doc=getattr(message,"document",None)
    item=photos[-1] if photos else doc
    if item is None:
        await safe_answer(message,"Пришли фото, изображение файлом, PDF, голосовое или аудиофайл.");return
    if (getattr(item,"file_size",0) or 0)>MAX_FILE:
        await safe_answer(message,"Файл больше 20 МБ. Раздели его на части.");return
    try:
        file=await message.bot.get_file(item.file_id)
        downloaded=await message.bot.download_file(file.file_path)
        data=downloaded.read()
    except Exception:
        logging.exception("HF_DOWNLOAD_FAILED")
        await safe_answer(message,"Не удалось скачать файл из Telegram. Ничего не записала; попробуй повторить отправку.");return
    name=Path(getattr(doc,"file_name","") or "receipt.jpg").name
    if is_audio(data,name,getattr(doc,"mime_type","")):
        from services.voice import transcribe_voice
        from handlers.text_handler import _process_text_message
        text=await transcribe_voice(data,name)
        if not text:
            await safe_answer(message,"Не удалось распознать аудио целиком. Ничего не записала; пришли короткое голосовое или текст.");return
        await safe_answer(message,"🎤 "+text,parse_mode=None)
        await _process_text_message(message,text)
        return
    try:
        result=await parse_receipt(data,name,caption,owner)
        if result.get("debt"):
            from services.debts import handle_model
            await handle_model(message,{"intent":"debt","debt":result["debt"]},owner);return
        rows=result.get("transactions") or []
        if not rows:
            await safe_answer(message,result.get("reply") or "Не вижу читаемых операций. Пришли чёткий документ или введи сумму текстом.",parse_mode=None)
            return
        await offer(message,rows,owner,caption,hashlib.sha256(data).hexdigest(),
                    result.get("payment_status") in {"failed","pending"} or result.get("needs_review") is True)
    except Exception:
        logging.exception("HF_DOCUMENT_FAILED")
        await safe_answer(message,"Не удалось подготовить документ. Не подтверждаю запись. "
                          "В логах Railway — HF_DOCUMENT_FAILED; /receipts покажет сохранённые черновики.",parse_mode=None)

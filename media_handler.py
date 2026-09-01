"""Обработчики изображений, документов, PDF-файлов и голосовых сообщений."""

import tempfile
import traceback
from pathlib import Path

from aiogram import F, Router, types
from openai import AsyncOpenAI

from config import OPENAI_API_KEY, get_authorized_user_name
from services.sheets import (
    append_transaction,
    get_category_limits,
    get_current_month_spending_by_category,
    find_recent_duplicate_transaction,
)
from services.vision import parse_receipt
from services.timezone import now_astana
from services.money import to_clean_number
from services.categories import (
    EXPENSE_CATEGORIES, INCOME_CATEGORIES, TYPE_EXPENSE, TYPE_INCOME,
    FALLBACK_EXPENSE_CATEGORY, FALLBACK_INCOME_CATEGORY, normalize_category,
)
from services.pending_receipts import set_pending, pop_pending
from services.telegram_safe import safe_answer


router = Router()
transcription_client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=45.0, max_retries=2)


async def _download_telegram_file(message: types.Message, file_id: str, suffix: str) -> tuple[bytes, str]:
    """Скачать файл через Telegram API во временный файл и вернуть его содержимое."""
    file_info = await message.bot.get_file(file_id)
    original_name = Path(file_info.file_path or f"file{suffix}").name
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary_file:
        temporary_path = Path(temporary_file.name)
    try:
        with temporary_path.open("wb") as destination:
            await message.bot.download_file(file_info.file_path, destination=destination)
        return temporary_path.read_bytes(), original_name
    finally:
        try:
            temporary_path.unlink()
        except OSError:
            pass


def build_and_save_transactions(transactions: list[dict], user_name: str, custom_reply: str = "") -> str:
    """Сохранить распознанные транзакции и вернуть готовый текст ответа, включая
    комментарий с характером (custom_reply, если передан, иначе — ai_comment
    конкретной позиции; предупреждение о лимите ДОБАВЛЯЕТСЯ к этому комментарию,
    а не затирает его — иначе живая реплика ИИ терялась при каждом превышении лимита).

    Не зависит от aiogram Message — этим пользуется и обработчик медиа
    (чек/скриншот сразу с подписью), и обработчик текста (когда следующее
    сообщение — это комментарий к ранее распознанному чеку), и фоновый
    планировщик в main.py (когда время ожидания комментария истекло и чек
    нужно сохранить как есть, без комментария, а не потерять).

    Позиции с нераспознанной суммой (0 или меньше) пропускаются, а не
    сохраняются как мусорная "0 KZT" строка — это отмечается в ответе.
    """
    limits = get_category_limits()
    now = now_astana()
    response_lines = ["📸 **Записано по чеку:**"]
    saved_any = False
    ai_comment_to_show = custom_reply

    for transaction in transactions:
        if not isinstance(transaction, dict):
            continue

        amount = to_clean_number(transaction.get("amount", 0))
        if not amount or amount <= 0:
            response_lines.append("• ⚠️ Не удалось распознать сумму одной из позиций — она пропущена.")
            continue
        transaction["amount"] = amount

        tx_type = transaction.get("type") or TYPE_EXPENSE
        if tx_type not in (TYPE_EXPENSE, TYPE_INCOME):
            tx_type = TYPE_EXPENSE
        transaction["type"] = tx_type

        valid_categories = INCOME_CATEGORIES if tx_type == TYPE_INCOME else EXPENSE_CATEGORIES
        fallback_cat = FALLBACK_INCOME_CATEGORY if tx_type == TYPE_INCOME else FALLBACK_EXPENSE_CATEGORY
        category = normalize_category(transaction.get("category"), valid_categories, fallback_cat)
        transaction["category"] = category

        duplicate = find_recent_duplicate_transaction(amount)

        transaction["transaction_id"] = f"TRX_{now.strftime('%Y%m%d_%H%M%S')}_{len(response_lines)}_{amount}"
        transaction["date"] = now.strftime("%Y-%m-%d %H:%M:%S")
        transaction["user"] = user_name
        append_transaction(transaction)
        saved_any = True

        item_ai_comment = transaction.get("ai_comment")
        if item_ai_comment and not ai_comment_to_show:
            ai_comment_to_show = item_ai_comment

        if tx_type == TYPE_EXPENSE and limits and category in limits:
            limit_value = limits[category]
            spent_value = get_current_month_spending_by_category(category)
            percentage = (spent_value / limit_value) * 100 if limit_value > 0 else 0
            if percentage >= 80:
                limit_warning = f"⚠️ По категории «{category}» использовано {int(percentage)}% лимита."
                ai_comment_to_show = f"{limit_warning} {ai_comment_to_show}".strip()

        response_lines.append(
            f"• {amount} {transaction.get('currency', 'KZT')} | "
            f"{transaction.get('bank', 'Не указан')} | {category} "
            f"({transaction.get('user_comment', '')})"
        )
        if duplicate:
            response_lines.append(
                f"  ⚠️ Похоже, эта сумма уже записывалась несколько минут назад — "
                f"если это дубль одной и той же покупки, скажи «удали последнюю запись»."
            )

    if not saved_any:
        return "⚠️ Не удалось распознать чек: в файле не найдена сумма."

    body = "\n".join(response_lines)
    if ai_comment_to_show:
        body += f"\n\n💬 {ai_comment_to_show}"
    return body


async def _handle_receipt_file(message: types.Message, file_id: str, suffix: str, filename: str) -> None:
    user_name = get_authorized_user_name(message.from_user.id)
    if not user_name:
        return

    await safe_answer(message, "🔍 Разбираю файл и считаю траты...")
    try:
        file_bytes, telegram_name = await _download_telegram_file(message, file_id, suffix)
        caption = (message.caption or "").strip()
        result = await parse_receipt(
            file_bytes=file_bytes,
            filename=filename or telegram_name,
            caption=caption,
            user_name=user_name,
        )
        if not result:
            await safe_answer(message, "⚠️ Не удалось распознать чек из-за сбоя API, но файл принят.")
            return

        transactions = result.get("transactions", []) if isinstance(result, dict) else []
        if not isinstance(transactions, list) or not transactions:
            # Пустой список — не обязательно сбой: например, ИИ мог распознать
            # пополнение своего же счёта и намеренно ничего не записать, объяснив
            # это в "reply". Показываем это объяснение, а не общую "не смогла".
            explanation = result.get("reply") if isinstance(result, dict) else None
            await safe_answer(message, explanation or "⚠️ Не удалось распознать чек: в файле не найдена сумма.")
            return

        chat_id = message.chat.id
        reply_comment = (result.get("reply") or "").strip() if isinstance(result, dict) else ""

        if caption:
            # Подпись уже есть в сообщении — сохраняем сразу, комментарий не спрашиваем.
            for t in transactions:
                if isinstance(t, dict) and not t.get("user_comment"):
                    t["user_comment"] = caption
            body = build_and_save_transactions(transactions, user_name, custom_reply=reply_comment or "Записала.")
            await safe_answer(message, body)
        else:
            # Подписи нет — если уже ждали комментарий к предыдущему чеку в этом
            # чате, сохраняем его как есть (без комментария), чтобы не терять,
            # и только потом начинаем ждать комментарий к новому.
            leftover = pop_pending(chat_id)
            if leftover:
                try:
                    leftover_transactions, leftover_user = leftover
                    leftover_body = build_and_save_transactions(
                        leftover_transactions, leftover_user,
                        custom_reply="Не дождалась комментария к предыдущему чеку — записала как есть.",
                    )
                    await safe_answer(message, leftover_body)
                except Exception as leftover_error:
                    # Сбой на сохранении СТАРОГО чека не должен мешать обработке нового —
                    # иначе можно застрять без запроса комментария к текущей покупке.
                    print(f"[Медиа] Не удалось сохранить предыдущий чек: {leftover_error}")

            set_pending(chat_id, transactions, user_name)
            prompt_text = reply_comment or (
                "Напиши комментарий к покупке (что именно купил?) или отправь «-», "
                "чтобы записать без комментария."
            )
            await safe_answer(message, prompt_text)
    except Exception as error:
        print(f"[Медиа] Ошибка обработки файла: {error}")
        traceback.print_exc()
        await safe_answer(message, "⚠️ Не удалось распознать чек из-за сбоя API, но файл принят.")


@router.message(F.photo)
async def handle_photo_message(message: types.Message):
    photo = message.photo[-1]
    await _handle_receipt_file(message, photo.file_id, ".jpg", "receipt.jpg")


@router.message(F.document)
async def handle_document_message(message: types.Message):
    document = message.document
    filename = document.file_name or "receipt"
    suffix = Path(filename).suffix.lower() or ".bin"
    await _handle_receipt_file(message, document.file_id, suffix, filename)


@router.message(F.voice)
async def handle_voice_message(message: types.Message):
    user_name = get_authorized_user_name(message.from_user.id)
    if not user_name:
        return

    await safe_answer(message, "🎙 Расшифровываю голосовое сообщение...")
    try:
        voice_bytes, telegram_name = await _download_telegram_file(
            message,
            message.voice.file_id,
            ".ogg",
        )
        with tempfile.NamedTemporaryFile(suffix=".ogg") as audio_file:
            audio_file.write(voice_bytes)
            audio_file.flush()
            with open(audio_file.name, "rb") as audio_stream:
                transcription = await transcription_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_stream,
                    language="ru",
                )
        recognized_text = (getattr(transcription, "text", "") or "").strip()
        if not recognized_text:
            await safe_answer(message, "⚠️ Не удалось разобрать голосовое сообщение.")
            return

        await safe_answer(message, f"📝 Распознано: {recognized_text}")
        from handlers.text_handler import handle_text_message

        await handle_text_message(message, recognized_text)
    except Exception as error:
        print(f"[Голос] Не удалось расшифровать {message.voice.file_id}: {error}")
        traceback.print_exc()
        await safe_answer(message, "⚠️ Не удалось расшифровать голосовое сообщение.")

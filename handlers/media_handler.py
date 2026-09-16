"""Обработка фото, PDF и скриншотов чеков и переводов."""

import asyncio

from aiogram.types import Message
from services.vision import parse_receipt
from services.sheets import (
    append_transaction, normalize_necessity, get_last_200_transactions,
    add_trip_plan, get_planned_trips, add_or_update_subscription,
)
from services.telegram_safe import safe_answer
from services.pending_receipts import set_pending, ack_pending
from services.state import dialogue_key
from services.memory import get_chat_history, add_chat_message
from services import fx, goals
from services.money import normalize_currency_code
from config import get_authorized_user_name
from services.categories import (
    TYPE_EXPENSE, TYPE_INCOME, is_income_type,
    EXPENSE_CATEGORIES, INCOME_CATEGORIES,
    FALLBACK_EXPENSE_CATEGORY, FALLBACK_INCOME_CATEGORY,
    normalize_category, normalize_subcategory,
    validate_transaction_category_subcategory,
)
from services.banks import normalize_bank_source
import re


def _format_currency(value):
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except (ValueError, TypeError):
        return str(value)


_GOAL_DEPOSIT_CAPTION_RE = re.compile(
    r"(?:в|на)\s+копилк[уи]\s*(.+)?|(?:на\s+цель)\s+(.+)?|копилк[ауи]\s+(.+)?",
    re.IGNORECASE,
)


def _build_recent_context(chat_id: int) -> str:
    """Несколько последних трат и сообщений чата — чтобы комментарий к
    новому чеку мог естественно связать его с тем, что уже происходит
    (например, переезд, начатый вчера/сегодня), а не был "слепым" к
    остальной семейной жизни. Специально не тащим много — иначе комментарий
    начнёт КАЖДЫЙ раз пытаться привязаться к прошлому, что уже перебор."""
    lines = []
    try:
        recent_tx = get_last_200_transactions()[-6:]
        for t in recent_tx:
            comm = str(t.get("user_comment") or "").strip()
            if comm:
                lines.append(f"- трата: {t.get('category', '')} — {comm}")
    except Exception:
        pass
    try:
        recent_chat = get_chat_history(chat_id)[-6:]
        for m in recent_chat:
            txt = str(m.get("text") or "").strip()
            if txt and len(txt) < 200:
                lines.append(f"- сообщение ({m.get('sender', '')}): {txt}")
    except Exception:
        pass
    return "\n".join(lines[-10:])


async def _apply_hints(result: dict, transactions: list[dict]) -> str:
    """Если распознавание чека увидело явный признак билета на поездку между
    городами или регулярной подписки — заводит/дополняет Trips/Subscriptions
    сам, без отдельной команды. Возвращает строку-приписку к ответу (или "")."""
    notes = []

    trip_hint = result.get("trip_hint")
    if trip_hint and trip_hint.get("destination"):
        dest = str(trip_hint["destination"]).strip()
        try:
            existing = await asyncio.to_thread(get_planned_trips)
        except Exception:
            existing = []
        match = next((t for t in existing if dest.lower() in str(t.get("destination", "")).lower()), None)
        if match:
            notes.append(f"🧳 Похоже, это по поездке в {match.get('destination')} — уже есть в «Поездках».")
        else:
            try:
                amount = transactions[0].get("amount", 0) if transactions else 0
                await asyncio.to_thread(
                    add_trip_plan, dest, trip_hint.get("dates", ""), 0,
                    f"Заведено автоматически по билету ({trip_hint.get('note', '')})",
                )
                notes.append(
                    f"🧳 Похоже на поездку в {dest} — завела черновик в «Поездках» "
                    f"(бюджет не указан, поправь командой при необходимости)."
                )
            except Exception as e:
                print(f"[Билет->Поездка] Не удалось завести поездку: {e}")

    sub_hint = result.get("subscription_hint")
    if sub_hint and sub_hint.get("name") and transactions:
        try:
            day = int(sub_hint.get("day_of_month") or 1)
            amount = transactions[0].get("amount", 0)
            bank = transactions[0].get("bank", "Не указан")
            await asyncio.to_thread(add_or_update_subscription, sub_hint["name"], amount, bank, day)
            notes.append(
                f"🔁 Похоже на регулярную подписку «{sub_hint['name']}» — добавила в «Подписки», "
                f"буду напоминать о списании и предупреждать заранее."
            )
        except Exception as e:
            print(f"[Чек->Подписка] Не удалось завести подписку: {e}")

    return "\n".join(notes)


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

    result = await parse_receipt(file_bytes, filename, caption, user_name, _build_recent_context(chat_id))
    transactions = result.get("transactions", [])
    reply = result.get("reply", "")

    if not transactions:
        await safe_answer(message, reply or "Не удалось распознать чек.")
        return

    # Пополнение копилки скриншотом/PDF перевода, а не обычная трата —
    # "закинул в копилку на отпуск" + прикреплённый чек банка.
    goal_match = _GOAL_DEPOSIT_CAPTION_RE.search(caption) if caption else None
    if goal_match and transactions:
        goal_name = next((g for g in goal_match.groups() if g), "").strip(" .,")
        amount = transactions[0].get("amount")
        if goal_name and amount:
            try:
                goal = await asyncio.to_thread(goals.deposit_to_goal, goal_name, amount)
            except ValueError as e:
                await safe_answer(message, str(e))
                return
            if goal:
                target = float(goal.get("target_amount", 0) or 0)
                curr = float(goal.get("current_amount", 0) or 0)
                pct = int((curr / target) * 100) if target > 0 else 0
                done = " 🎉 Цель достигнута!" if curr >= target > 0 else ""
                await safe_answer(
                    message,
                    f"✅ Пополнила «{goal['name']}»: {_format_currency(curr)} из {_format_currency(target)} KZT ({pct}%).{done}",
                )
                return
            await safe_answer(message, f"Не нашла активную цель «{goal_name}». Покажи цели, чтобы свериться с названием.")
            return

    # Валюта отличная от KZT — конвертируем по официальному курсу автоматически,
    # а не просим сначала сконвертировать вручную (это и есть тот самый
    # "не понимает другую валюту", который просили починить).
    for tx in transactions:
        cur = normalize_currency_code(tx.get("currency"))
        if cur and cur != "KZT":
            converted = await fx.convert_to_kzt(tx.get("amount", 0), cur)
            if converted:
                kzt_amount, rate = converted
                original_amount = tx.get("amount")
                tx["amount"] = kzt_amount
                tx["amount_kzt"] = kzt_amount
                tx["currency"] = "KZT"
                tx["_fx_note"] = f"{original_amount} {cur} по курсу {rate:.2f}"
            else:
                await safe_answer(
                    message,
                    f"Чек в {cur}, а курс сейчас не удалось узнать — пришли, пожалуйста, сумму в тенге вручную.",
                )
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
        if tx.get("_fx_note"):
            tx["user_comment"] = f"{tx['user_comment']} ({tx['_fx_note']})".strip(" ()")
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
                    tx["user_comment"] = f"{caption} ({tx['_fx_note']})" if tx.get("_fx_note") else caption
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
        extra_note = await _apply_hints(result, validated_transactions)
        if extra_note:
            report += f"\n\n{extra_note}"
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

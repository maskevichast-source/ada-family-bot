"""Обработка фото, PDF и скриншотов чеков и переводов."""

import asyncio

from aiogram.types import Message
from services.vision import parse_receipt
from services.sheets import (
    append_transaction, normalize_necessity, get_last_200_transactions, find_duplicate_receipt,
    add_trip_plan, get_planned_trips, add_or_update_subscription, get_category_limits,
)
from services.telegram_safe import safe_answer
from services.pending_receipts import set_pending, ack_pending
from services.state import dialogue_key
from services import state
from services.memory import get_chat_history, add_chat_message
from services import fx, goals
from services import ada_voice
from services import undo as undo_service
from services.money import normalize_currency_code
from config import get_authorized_user_name
from services.categories import (
    TYPE_EXPENSE, TYPE_INCOME, is_income_type,
    EXPENSE_CATEGORIES, INCOME_CATEGORIES,
    FALLBACK_EXPENSE_CATEGORY, FALLBACK_INCOME_CATEGORY,
    normalize_category, normalize_subcategory,
    validate_transaction_category_subcategory,
)
from services.analytics import detect_amount_anomaly, habit_remark_due
from services.deepseek_service import generate_budget_reflection
from services.banks import normalize_bank_source
import re
from services.receipt_meta import clean_items_summary, format_sheet_datetime, parse_occurred_at
from services.timezone import now_astana, parse_flexible_datetime


def _format_currency(value):
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except (ValueError, TypeError):
        return str(value)


_GOAL_DEPOSIT_CAPTION_RE = re.compile(
    r"(?:в|на)\s+копилк[уи]\s*(.+)?|(?:на\s+цель)\s+(.+)?|копилк[ауи]\s+(.+)?",
    re.IGNORECASE,
)


def _build_recent_context(chat_id: int, user_name: str = "", caption: str = "") -> str:
    """Несколько последних трат и сообщений чата — чтобы комментарий к
    новому чеку мог естественно связать его с тем, что уже происходит
    (например, переезд, начатый вчера/сегодня), а не был "слепым" к
    остальной семейной жизни. Специально не тащим много — иначе комментарий
    начнёт КАЖДЫЙ раз пытаться привязаться к прошлому, что уже перебор."""
    lines = []
    try:
        recent_tx = get_last_200_transactions()[-6:]
        for t in recent_tx:
            # Поля здесь "cat"/"comm" (см. get_last_200_transactions), а не
            # "category"/"user_comment" — раньше тут было несовпадение имён
            # и эта часть контекста фактически никогда не собиралась.
            cat = str(t.get("cat") or "").strip()
            comm = str(t.get("comm") or "").strip()
            if cat or comm:
                lines.append(f"- трата: {cat} — {comm}" if comm else f"- трата: {cat}")
    except Exception:
        pass
    try:
        # Решает код (см. habit_remark_due): только если в подписи речь про
        # такую покупку, повторов уже достаточно и прошлое замечание было давно.
        harmful_count = habit_remark_due(user_name, caption)
        if harmful_count:
            lines.append(
                f"- ФАКТ (посчитано кодом, не выдумано): за последние 7 дней у "
                f"{user_name} уже было {harmful_count} таких покупок (сигареты/"
                f"энергетики/алкоголь), не считая сегодняшней. Можно отметить это "
                f"ОДНОЙ короткой фразой своими словами (с конкретным числом, без "
                f"советов и без слов «стоит»/«нужно») — правила в разделе КОММЕНТАРИИ."
            )
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


def _card_label(bank, source) -> str:
    """Чем заплачено: карта с чека («Home Credit Ozen», «Kaspi Gold»), иначе банк. Тип «рассрочка»
    в чате не показываем: по названию карты и так понятно, а в таблице он ставится кодом по карте."""
    bank = str(bank or "").strip() or "Не указан"
    source = str(source or "").strip()
    if not source:
        return bank
    if bank == "Не указан":
        return source
    b, s = bank.lower(), source.lower()
    if b in s or s in b:
        return source if len(source) >= len(bank) else bank
    return f"{bank} {source}"


def _format_receipt_report(transactions: list[dict], ai_comment: str = "") -> str:
    lines = ["📸 **Записано по чеку:**"]
    for tx in transactions:
        amt = _format_currency(tx.get("amount", 0))
        curr = tx.get("currency", "KZT")
        bank = _card_label(tx.get("bank"), tx.get("source"))
        cat = tx.get("category", "")
        comm = str(tx.get("user_comment") or "").strip()
        comm_str = f" ({comm})" if comm else ""
        sign = "+ " if is_income_type(tx.get("type")) else ""
        extras = []
        when = parse_flexible_datetime(tx.get("occurred_at")) if tx.get("occurred_at") else None
        if when and when.date() != now_astana().date():
            extras.append(f"чек от {when.strftime('%d.%m.%Y %H:%M')}")      # поздняя загрузка: показываем настоящую дату
        extra_str = f" · {', '.join(extras)}" if extras else ""
        lines.append(f"• {sign}{amt} {curr} | {bank} | {cat}{comm_str}{extra_str}")
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

    result = await parse_receipt(file_bytes, filename, caption, user_name, _build_recent_context(chat_id, user_name, caption))

    # Долг (занял/одолжил/вернул), а не обычная покупка — определяется по
    # подписи к фото (например "занял Ануару, перевёл на халык"). Фото/чек
    # никогда раньше не мог стать долгом (только текстовый ввод это умел) —
    # переведённая сумма другу молча записывалась обычным расходом. Если
    # ИИ уверенно распознал долг — ничего не пишем как трату, только черновик
    # на подтверждение, через тот же механизм, что и текстовый ввод (Да/Нет).
    debt_hint = result.get("debt_hint")
    if debt_hint and debt_hint.get("direction") in {"lent", "borrowed"} and debt_hint.get("counterparty"):
        try:
            debt_amount = float(debt_hint.get("amount") or 0)
        except (TypeError, ValueError):
            debt_amount = 0
        if debt_amount > 0:
            payload = {
                "owner": user_name, "event_type": "open",
                "direction": debt_hint["direction"],
                "counterparty": str(debt_hint["counterparty"]).strip(),
                "amount": debt_amount, "currency": "KZT",
            }
            draft_key = dialogue_key(chat_id, message.from_user.id)
            state.put("debt_draft", draft_key, {
                "payload": payload, "event_id": f"DE_{chat_id}_{message.message_id}",
            })
            action = "Выдача в долг" if payload["direction"] == "lent" else "Получение в долг"
            await safe_answer(
                message,
                f"💰 Похоже, это долг, а не обычная трата.\n\n{action}\n"
                f"Участник семьи: {user_name}\nКонтрагент: {payload['counterparty']}\n"
                f"Сумма: {_format_currency(debt_amount)}\n\n"
                "Записать как долг (не как трату)? «Да» / «Нет».",
            )
            return

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
        # Время операции из самого чека (чеки часто присылают позже); сводка «что куплено» из позиций чека.
        occurred = parse_occurred_at(tx.get("occurred_at"), now_astana())
        tx["occurred_at"] = format_sheet_datetime(occurred) if occurred else ""
        tx["items_summary"] = clean_items_summary(tx.get("items_summary")) if not is_income else ""
        if not caption and tx["items_summary"]:
            tx["user_comment"] = tx["items_summary"]
        if not tx.get("user_comment"): tx["user_comment"] = caption or ("Пополнение/доход" if is_income else "")
        if tx.get("_fx_note"):
            tx["user_comment"] = f"{tx['user_comment']} ({tx['_fx_note']})".strip(" ()")
        if not tx.get("ai_comment"): tx["ai_comment"] = reply or ""

        validated_transactions.append(tx)

    # Один и тот же чек могут прислать дважды (Влад и Диана; чек и скриншот банка). Сравниваем по
    # времени операции из чека, сумме и магазину. «не дубль» в подписи отключает проверку.
    if not (caption and re.search(r"не\s*дубл", caption.lower())):
        fresh, duplicates = [], []
        for tx in validated_transactions:
            same = await asyncio.to_thread(find_duplicate_receipt, tx) if tx.get("occurred_at") else None
            (duplicates if same else fresh).append((tx, same))
        if duplicates:
            names = "; ".join(f"{_format_currency(t.get('amount', 0))} ₸ · {t.get('merchant') or 'без названия'}"
                              for t, _ in duplicates)
            if not fresh:
                await safe_answer(message, f"Похоже, этот чек уже записан ({names}), поэтому ничего не добавила. "
                                           "Если это другая покупка — отправь ещё раз с подписью «не дубль».")
                return
            validated_transactions = [t for t, _ in fresh]
            reply = f"{reply}\n(Уже была в таблице и пропущена: {names}.)".strip()

    # Стабильный ID на основе сообщения — чтобы повторный вызов handle_media
    # для ТОГО ЖЕ сообщения (retry после сетевой ошибки) не записывал уже
    # успешно сохранённые позиции чека ещё раз (append_transaction сам
    # пропускает дубли по transaction_id).
    for idx, tx in enumerate(validated_transactions):
        if not tx.get("transaction_id"):
            tx["transaction_id"] = f"MEDIA_{chat_id}_{message.message_id}_{idx}"

    # Если позиции видны в самом чеке (чек Kaspi, длинный чек магазина), спрашивать комментарий
    # незачем — «что куплено» уже есть. Сомневающиеся категории по-прежнему ждут комментария.
    needs_clarification = any(
        float(tx.get("confidence", 1.0)) < 0.8 and tx.get("alternatives") for tx in validated_transactions
    )
    receipt_is_self_explanatory = (
        bool(validated_transactions) and not needs_clarification
        and all(tx.get("items_summary") for tx in validated_transactions)
    )

    # Доходы записываем сразу и гарантированно в Google Sheets
    if has_income or caption or receipt_is_self_explanatory:
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

        # Опционально: если конкретная покупка заметно выбивается из
        # обычного для этой категории у этого человека - короткая живая
        # реплика сразу, а не только в еженедельном дайджесте. Не шумим
        # на каждой крупной покупке - только когда реально есть с чем
        # сравнить (см. min_history в detect_amount_anomaly) и разница
        # заметная.
        try:
            if ada_voice.enabled():
                # Живая реплика по поводу (повод считает код, см. services/ada_voice.py).
                # История читается уже после записи, поэтому только что записанные строки отбрасываем.
                history = await asyncio.to_thread(get_last_200_transactions)
                history = history[:-len(validated_transactions)] if history else history
                limits = await asyncio.to_thread(get_category_limits)
                voice = await ada_voice.comment_for_transactions(
                    validated_transactions, user_name, history, limits, get_chat_history(chat_id))
                if voice:
                    report += f"\n\n💬 {voice}"
            else:
                for tx in validated_transactions:
                    fact = detect_amount_anomaly(tx.get("user") or user_name, tx.get("category"), tx.get("amount"))
                    if fact:
                        reflection = await generate_budget_reflection("anomaly", fact)
                        if reflection:
                            report += f"\n\n{reflection}"
                        break  # одной реплики на чек достаточно, даже если позиций несколько
        except Exception as anomaly_error:
            print(f"[Голос/аномалия] Пропущено: {anomaly_error}")

        await safe_answer(message, report, reply_markup=undo_service.build_keyboard(validated_transactions))
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

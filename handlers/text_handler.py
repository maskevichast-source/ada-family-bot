import asyncio
import datetime
import json
import traceback
from collections import defaultdict
from aiogram import Router, types, F
from aiogram.types import FSInputFile
from openai import AsyncOpenAI
from services.deepseek_service import parse_and_analyze
from services.sheets import (
    append_transaction, get_last_200_transactions, get_transactions_for_period,
    add_shopping_items, get_shopping_items, mark_shopping_items_done,
    add_reminder, get_pending_reminders, add_or_update_subscription, get_active_subscriptions,
    deactivate_subscription,
    get_category_limits, get_current_month_spending_by_category, save_category_limits,
    add_trip_plan, get_planned_trips, find_and_update_record, delete_record_by_keyword,
    split_last_transaction_by_amount, get_installments, add_installment, close_installment,
    find_recent_duplicate_transaction, debug_transactions_snapshot
)
from services.categories import (
    TYPE_EXPENSE, TYPE_INCOME, EXPENSE_CATEGORIES, INCOME_CATEGORIES,
    FALLBACK_EXPENSE_CATEGORY, FALLBACK_INCOME_CATEGORY, normalize_category,
)
from services.limits_ai import generate_ai_limits
from services.charts import generate_spending_chart, generate_income_chart
from services.memory import add_chat_message, get_chat_history
from services.timezone import now_astana
from services.weather import get_weather_forecast
from services.money import parse_amount, to_clean_number
from services.pending_receipts import has_pending, pop_pending
from services.telegram_safe import safe_answer
from config import get_authorized_user_name, DEEPSEEK_API_KEY

router = Router()
ai_client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")


def _to_float(value) -> float:
    return parse_amount(value)


def _to_number(value):
    """Как _to_float, но возвращает int, если число целое — для красивого вывода сумм."""
    return to_clean_number(value)


def _to_number_or_blank(value):
    """Как _to_number, но пустое/отсутствующее значение остаётся пустой строкой,
    а не превращается в 0 (чтобы в таблице и в выводе не путать "не указано" с нулём)."""
    if value in (None, ""):
        return ""
    return _to_number(value)


def _normalize_reminder_time(raw: str | None, now: datetime.datetime) -> str | None:
    """Привести время напоминания к формату YYYY-MM-DD HH:MM:SS.

    Промпт просит ИИ всегда присылать полную дату, но это подстраховка на
    случай, если модель всё же пришлёт голое "14:00" — раньше такое время
    либо тихо ломало напоминание (падало на sheet как есть и не срабатывало
    в планировщике), либо вовсе не доходило до сохранения.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        return raw
    except ValueError:
        pass
    try:
        parsed = datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M")
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            time_only = datetime.datetime.strptime(raw, fmt).time()
            candidate = now.replace(hour=time_only.hour, minute=time_only.minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += datetime.timedelta(days=1)
            return candidate.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def _format_time_short(full_datetime: str) -> str:
    """"2026-08-27 14:00:00" -> "14:00" для короткого текста в ответе."""
    try:
        return datetime.datetime.strptime(full_datetime, "%Y-%m-%d %H:%M:%S").strftime("%H:%M")
    except ValueError:
        return full_datetime


def _month_bounds(now: datetime.datetime) -> tuple[str, str]:
    """Границы [начало_месяца, начало_следующего_месяца) в формате YYYY-MM-DD."""
    start = now.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


@router.message(F.text)
async def handle_text_message(message: types.Message, text_override: str | None = None):
    user_name = get_authorized_user_name(message.from_user.id)
    if not user_name:
        return
    chat_id = message.chat.id
    text = (text_override if text_override is not None else message.text or "").strip()

    # Если бот ждёт комментарий к недавно распознанному чеку без подписи —
    # это сообщение (или расшифровка голосового) и есть тот комментарий.
    if has_pending(chat_id):
        popped = pop_pending(chat_id)
        if popped:
            pending_transactions, pending_user_name = popped
            comment = "" if text.strip() == "-" else text
            for t in pending_transactions:
                if isinstance(t, dict):
                    t["user_comment"] = comment
            from handlers.media_handler import build_and_save_transactions
            body = build_and_save_transactions(pending_transactions, pending_user_name)
            tail = "Записано с комментарием." if comment else "Записано без комментария."
            resp = f"{body}\n\n💬 {tail}"
            add_chat_message(chat_id, user_name, text)
            add_chat_message(chat_id, "Ада", resp)
            await safe_answer(message, resp)
            return

    normalized_text = text.lower().strip()

    if normalized_text == "/chatid":
        await safe_answer(message, f"ID этого чата: `{chat_id}`", parse_mode="Markdown")
        return

    if normalized_text == "/debug":
        diagnostic = await asyncio.to_thread(debug_transactions_snapshot)
        await safe_answer(message, diagnostic)
        return

    if (
        normalized_text.startswith("/chart")
        or "график" in normalized_text
        or "диаграмм" in normalized_text
    ):
        try:
            transactions = await asyncio.to_thread(get_last_200_transactions)
            if "доход" in normalized_text:
                chart_path = await asyncio.to_thread(generate_income_chart, transactions)
                caption = "📊 Доходы за текущий месяц."
            else:
                chart_path = await asyncio.to_thread(generate_spending_chart, transactions)
                caption = "📊 Расходы за текущий месяц."
            await message.answer_photo(FSInputFile(chart_path), caption=caption)
        except Exception as error:
            print(f"[График] Не удалось построить диаграмму: {error}")
            await safe_answer(message, "⚠️ Не удалось построить график.")
        return

    try:
        history_sheets = get_last_200_transactions()
        active_shopping = get_shopping_items()
        limits = get_category_limits()
        pending_rems = get_pending_reminders()
        planned_trips = get_planned_trips()
        active_subs = get_active_subscriptions()
        active_installments = get_installments()
        current_chat_history = get_chat_history(chat_id)

        data = await parse_and_analyze(
            user_text=text,
            user_name=user_name,
            history=history_sheets,
            chat_history=current_chat_history,
            shopping_list=active_shopping,
            limits=limits,
            reminders=pending_rems,
            trips=planned_trips,
            subscriptions=active_subs,
            installments=active_installments
        )

        add_chat_message(chat_id, user_name, text)
        intent = data.get("intent", "chat")

        if intent == "get_limits":
            if not limits:
                resp = "📊 Лимиты в таблице пока не заданы."
            else:
                resp = "📊 **Текущие лимиты на месяц:**\n\n"
                for c, v in limits.items():
                    resp += f"• **{c}**: {v:,.0f} ₸\n"
            if data.get("reply"):
                resp += f"\n💬 {data['reply']}"
            add_chat_message(chat_id, "Ада", resp)
            await safe_answer(message, resp)

        elif intent == "get_reminders":
            rems = get_pending_reminders()
            if not rems:
                resp = "📌 Активных напоминаний нет."
            else:
                resp = "📌 **Активные напоминания:**\n\n"
                for r in rems:
                    rec_str = " (ежедневно)" if str(r.get("recurrence")) == "daily" else ""
                    resp += f"• **{r.get('target_user')}**: {r.get('text')} — ⏰ {r.get('remind_at')}{rec_str}\n"
            if data.get("reply"):
                resp += f"\n💬 {data['reply']}"
            add_chat_message(chat_id, "Ада", resp)
            await safe_answer(message, resp)

        elif intent == "delete_reminder":
            query = data.get("search_query") or data.get("reminder_text") or ""
            deleted = delete_record_by_keyword("Reminders", query) if query else None
            if deleted:
                what = deleted.get("text") or "напоминание"
                when = deleted.get("remind_at") or ""
                reply_text = data.get("reply") or f"🗑 Удалила напоминание «{what}»{f' ({when})' if when else ''}."
            else:
                reply_text = f"⚠️ Не нашла напоминание по запросу «{query}»." if query else "⚠️ Не поняла, какое напоминание удалить."
            add_chat_message(chat_id, "Ада", reply_text)
            await safe_answer(message, reply_text)

        elif intent == "get_shopping":
            items = get_shopping_items()
            if not items:
                resp = "🛒 Список покупок пуст!"
            else:
                resp = "🛒 **Список покупок:**\n" + "\n".join([f"• {i['item']} (добавил: {i['added_by']})" for i in items])
            if data.get("reply"):
                resp += f"\n\n💬 {data['reply']}"
            add_chat_message(chat_id, "Ада", resp)
            await safe_answer(message, resp)

        elif intent == "get_trips":
            trips = get_planned_trips()
            if not trips:
                resp = "🏕 Запланированных поездок пока нет."
            else:
                resp = "🏕 **Запланированные поездки:**\n" + "\n".join([f"• **{t['destination']}** ({t['dates']}) — Бюджет: {t['budget']} ₸ | {t['notes']}" for t in trips])
            if data.get("reply"):
                resp += f"\n\n💬 {data['reply']}"
            add_chat_message(chat_id, "Ада", resp)
            await safe_answer(message, resp)

        elif intent == "get_weather":
            forecast_message = await get_weather_forecast()
            reply_text = forecast_message or "⚠️ Не удалось получить прогноз погоды, попробуй чуть позже."
            if data.get("reply"):
                reply_text += f"\n\n💬 {data['reply']}"
            add_chat_message(chat_id, "Ада", reply_text)
            await safe_answer(message, reply_text)

        elif intent == "get_subscriptions":
            subs = active_subs
            if not subs:
                resp = "📋 Активных подписок и регулярных платежей нет."
            else:
                resp = "📋 **Активные подписки и регулярные платежи:**\n\n"
                for s in subs:
                    resp += f"• **{s.get('name')}** — {s.get('amount')} ₸, {s.get('day_of_month')} числа месяца ({s.get('bank', 'банк не указан')})\n"
            if data.get("reply"):
                resp += f"\n💬 {data['reply']}"
            add_chat_message(chat_id, "Ада", resp)
            await safe_answer(message, resp)

        elif intent == "cancel_subscription":
            name = data.get("subscription_name") or data.get("merchant") or ""
            cancelled = deactivate_subscription(name) if name else None
            if cancelled:
                real_name = cancelled.get("name", name)
                amount = cancelled.get("amount", "")
                reply_text = data.get("reply") or f"🗑 Отменила подписку «{real_name}»{f' ({amount} ₸)' if amount else ''}."
            else:
                reply_text = f"⚠️ Не нашла активную подписку «{name}»." if name else "⚠️ Не поняла, какую подписку отменить."
            add_chat_message(chat_id, "Ада", reply_text)
            await safe_answer(message, reply_text)

        elif intent == "get_income":
            now = now_astana()
            start_date, end_date = _month_bounds(now)
            period_transactions = await asyncio.to_thread(get_transactions_for_period, start_date, end_date)
            total_income = 0.0
            income_by_cat: dict[str, float] = defaultdict(float)
            for t in period_transactions:
                if str(t.get("type") or TYPE_EXPENSE) != TYPE_INCOME:
                    continue
                amt = _to_float(t.get("amt"))
                total_income += amt
                income_by_cat[t.get("cat") or "Прочее"] += amt

            if total_income == 0:
                resp = f"💰 В {now.strftime('%m.%Y')} доходов пока не записано."
            else:
                resp = f"💰 **Доходы за {now.strftime('%m.%Y')}: {total_income:,.0f} ₸**\n\n"
                for c, v in sorted(income_by_cat.items(), key=lambda item: -item[1]):
                    resp += f"• {c}: {v:,.0f} ₸\n"
            if data.get("reply"):
                resp += f"\n💬 {data['reply']}"
            add_chat_message(chat_id, "Ада", resp)
            await safe_answer(message, resp)

        elif intent == "get_summary":
            now = now_astana()
            start_date, end_date = _month_bounds(now)
            period_transactions = await asyncio.to_thread(get_transactions_for_period, start_date, end_date)
            total_income = 0.0
            total_expense = 0.0
            expense_by_cat: dict[str, float] = defaultdict(float)
            for t in period_transactions:
                amt = _to_float(t.get("amt"))
                if str(t.get("type") or TYPE_EXPENSE) == TYPE_INCOME:
                    total_income += amt
                else:
                    total_expense += amt
                    expense_by_cat[t.get("cat") or "Прочее"] += amt

            balance = total_income - total_expense
            balance_icon = "📈" if balance >= 0 else "📉"
            resp = (
                f"📊 **Сводка за {now.strftime('%m.%Y')}:**\n\n"
                f"💰 Доходы: {total_income:,.0f} ₸\n"
                f"💸 Расходы: {total_expense:,.0f} ₸\n"
                f"{balance_icon} Баланс: {balance:,.0f} ₸\n"
            )
            if expense_by_cat:
                resp += "\n**Топ категорий расходов:**\n"
                for c, v in sorted(expense_by_cat.items(), key=lambda item: -item[1])[:5]:
                    resp += f"• {c}: {v:,.0f} ₸\n"
            if data.get("reply"):
                resp += f"\n💬 {data['reply']}"
            add_chat_message(chat_id, "Ада", resp)
            await safe_answer(message, resp)

        elif intent == "split_transaction":
            t_amt = _to_float(data.get("target_amount", 0))
            p1_amt = _to_float(data.get("part1_amount", 0))
            p1_cat = normalize_category(data.get("part1_category"), EXPENSE_CATEGORIES, "Еда и продукты")
            p1_comm = data.get("part1_comment", "")
            p2_amt = _to_float(data.get("part2_amount", 0))
            p2_cat = normalize_category(data.get("part2_category"), EXPENSE_CATEGORIES, FALLBACK_EXPENSE_CATEGORY)
            p2_comm = data.get("part2_comment", "")

            success = split_last_transaction_by_amount(t_amt, p1_amt, p1_cat, p1_comm, p2_amt, p2_cat, p2_comm)
            reply_text = data.get("reply", "Транзакция разделена.")

            if not success:
                reply_text = "Не смогла найти транзакцию на эту сумму в таблице."

            add_chat_message(chat_id, "Ада", reply_text)
            await safe_answer(message, reply_text)

        elif intent == "delete_transaction":
            query = data.get("search_query") or data.get("merchant") or data.get("user_comment") or ""
            deleted = delete_record_by_keyword("Transactions", query) if query else None
            if deleted:
                amt = deleted.get("amount", "")
                dcat = deleted.get("category", "")
                merchant = deleted.get("merchant") or deleted.get("user_comment") or ""
                details = f"{amt} ₸ | {dcat}" + (f" | {merchant}" if merchant else "")
                reply_text = data.get("reply") or f"🗑 Удалила запись: {details}."
            else:
                reply_text = f"⚠️ Не нашла операцию по запросу «{query}»." if query else "⚠️ Не поняла, какую запись удалить."
            add_chat_message(chat_id, "Ада", reply_text)
            await safe_answer(message, reply_text)

        elif intent == "correct_any_record":
            raw_updates = data.get("updates", [])
            if isinstance(raw_updates, dict):
                raw_updates = [raw_updates]
            elif not isinstance(raw_updates, list):
                raw_updates = []
            updates = [u for u in raw_updates if isinstance(u, dict)]

            if not updates and data.get("worksheet") and data.get("search_query") and data.get("new_value") is not None:
                updates = [{
                    "worksheet": data.get("worksheet"),
                    "search_query": data.get("search_query"),
                    "column_to_update": data.get("column_to_update", "amount"),
                    "new_value": data.get("new_value"),
                    "action": data.get("action", "update"),
                }]

            if not updates:
                reply_text = data.get("reply") or "Не поняла, что и на что исправить — уточни, пожалуйста."
                add_chat_message(chat_id, "Ада", reply_text)
                await safe_answer(message, reply_text)
            else:
                result_lines = []
                limits_touched = False

                for upd in updates:
                    ws_name = upd.get("worksheet")
                    query = upd.get("search_query")
                    col = upd.get("column_to_update", "amount")
                    val = upd.get("new_value")
                    action = upd.get("action", "update")

                    if not ws_name or not query:
                        continue

                    if action == "update" and val is not None and ws_name == "Limits":
                        current_limits = get_category_limits()
                        found_key = next((k for k in current_limits if str(query).lower() in k.lower()), query)
                        current_limits[found_key] = _to_float(val)
                        save_category_limits(current_limits)
                        limits_touched = True
                    elif action == "update" and val is not None:
                        clean_val = str(_to_number(val)) if str(col).lower() in ("amount", "сумма") else val
                        old_record = find_and_update_record(ws_name, query, col, clean_val)
                        if old_record:
                            old_val = old_record.get(col, "—") if isinstance(col, str) else "—"
                            result_lines.append(f"✏️ {ws_name}: «{query}» — {col}: {old_val} → {clean_val}")
                        else:
                            result_lines.append(
                                f"⚠️ Не нашла «{query}» в {ws_name}. Если это была НОВАЯ покупка, "
                                f"а не правка старой записи — напиши её ещё раз явно как покупку, "
                                f"например: «Записать {clean_val} тенге, [на что]»."
                            )
                    elif action == "delete":
                        deleted = delete_record_by_keyword(ws_name, query)
                        if deleted:
                            result_lines.append(f"🗑 Удалила запись из {ws_name}")
                        else:
                            result_lines.append(f"⚠️ Не нашла «{query}» для удаления в {ws_name}")

                if limits_touched:
                    updated_limits = get_category_limits()
                    resp = "📊 **БЮДЖЕТ СКОРРЕКТИРОВАН:**\n\n"
                    for c, v in updated_limits.items():
                        resp += f"• **{c}**: {v:,.0f} ₸\n"
                    if result_lines:
                        resp += "\n" + "\n".join(result_lines)
                elif result_lines:
                    resp = "\n".join(result_lines)
                else:
                    resp = "⚠️ Не удалось выполнить обновление в таблице."

                if data.get("reply"):
                    resp += f"\n\n💬 {data['reply']}"

                add_chat_message(chat_id, "Ада", resp)
                await safe_answer(message, resp)

        elif intent == "generate_limits":
            await safe_answer(message, "⏳ Анализирую прошлые траты и утверждаю лимиты на новый месяц...")
            new_limits = await generate_ai_limits(history_sheets)
            save_category_limits(new_limits)

            resp = "📊 **БЮДЖЕТ УТВЕРЖДЕН:**\n\nТаблица обновлена, лимиты проставлены:\n\n"
            for cat, val in new_limits.items():
                resp += f"• **{cat}**: {val:,.0f} ₸\n"
            if data.get("reply"):
                resp += f"\n💬 {data['reply']}"

            add_chat_message(chat_id, "Ада", resp)
            await safe_answer(message, resp)

        elif intent == "get_installments":
            if not active_installments:
                resp = "💳 Активных рассрочек и Kaspi Red пока нет."
            else:
                lines = ["💳 **Активные рассрочки и Kaspi Red:**", ""]
                for item in active_installments:
                    description = item.get("description") or item.get("name") or "Без описания"
                    lines.append(
                        f"• **{description}** ({item.get('bank', 'Банк не указан')}) — "
                        f"всего {item.get('total_amount', '—')} ₸, "
                        f"платёж {item.get('monthly_payment', '—')} ₸, "
                        f"следующий: {item.get('next_payment', 'дата не указана')}"
                    )
                resp = "\n".join(lines)
            if data.get("reply"):
                resp += f"\n\n💬 {data['reply']}"
            add_chat_message(chat_id, "Ада", resp)
            await safe_answer(message, resp)

        elif intent == "add_installment":
            installment = add_installment(
                {
                    "user": user_name,
                    "bank": data.get("bank", "Kaspi"),
                    "kind": data.get("kind", "Рассрочка"),
                    "description": data.get("description") or data.get("merchant", "Рассрочка"),
                    "total_amount": _to_number_or_blank(data.get("total_amount", data.get("amount", ""))),
                    "monthly_payment": _to_number_or_blank(data.get("monthly_payment", "")),
                    "payments_count": data.get("payments_count", ""),
                    "next_payment": data.get("next_payment", ""),
                }
            )
            if installment:
                reply_text = data.get("reply", "💳 Рассрочку записала в таблицу.")
            else:
                reply_text = "⚠️ Не удалось сохранить рассрочку в таблицу."
            add_chat_message(chat_id, "Ада", reply_text)
            await safe_answer(message, reply_text)

        elif intent == "close_installment":
            query = data.get("search_query") or data.get("description") or data.get("merchant") or ""
            closed = close_installment(query) if query else None
            if closed:
                what = closed.get("description") or query
                reply_text = data.get("reply") or f"✅ Отметила рассрочку «{what}» как закрытую."
            else:
                reply_text = f"⚠️ Не нашла рассрочку по запросу «{query}»." if query else "⚠️ Не поняла, какую рассрочку закрыть."
            add_chat_message(chat_id, "Ада", reply_text)
            await safe_answer(message, reply_text)

        elif intent == "transaction":
            now = now_astana()
            amt = to_clean_number(data.get("amount", 0))

            if not amt or amt <= 0:
                reply_text = data.get("reply") or (
                    "Не поняла сумму. Уточни, пожалуйста, сколько было потрачено или получено?"
                )
                add_chat_message(chat_id, "Ада", reply_text)
                await safe_answer(message, reply_text)
            else:
                data["amount"] = amt
                tx_type = str(data.get("type") or "").strip().upper()
                if tx_type not in (TYPE_EXPENSE, TYPE_INCOME):
                    tx_type = TYPE_EXPENSE
                data["type"] = tx_type
                data["transaction_id"] = f"TRX_{now.strftime('%Y%m%d_%H%M%S')}_{amt}"
                data["date"] = now.strftime("%Y-%m-%d %H:%M:%S")
                data["user"] = user_name

                valid_categories = INCOME_CATEGORIES if tx_type == TYPE_INCOME else EXPENSE_CATEGORIES
                fallback_cat = FALLBACK_INCOME_CATEGORY if tx_type == TYPE_INCOME else FALLBACK_EXPENSE_CATEGORY
                raw_cat = data.get("category")
                cat = normalize_category(raw_cat, valid_categories, fallback_cat)
                data["category"] = cat
                if raw_cat and cat != str(raw_cat).strip():
                    note = f" (категория уточнена: «{raw_cat}» → «{cat}»)"
                    data["ai_comment"] = f"{data.get('ai_comment', '')}{note}".strip()

                duplicate = find_recent_duplicate_transaction(amt)

                append_transaction(data)

                if tx_type == TYPE_EXPENSE and limits and cat in limits:
                    limit_val = limits[cat]
                    spent_val = get_current_month_spending_by_category(cat)
                    pct = (spent_val / limit_val) * 100 if limit_val > 0 else 0
                    if pct >= 80:
                        data["ai_comment"] = f"⚠️ Внимание! По категории «{cat}» потрачено {spent_val:,.0f} из {limit_val:,.0f} ₸ ({int(pct)}% лимита). {data.get('ai_comment', '')}"

                if tx_type == TYPE_EXPENSE and data.get("is_recurring"):
                    sub_name = data.get("subscription_name") or data.get("merchant") or "Подписка"
                    day = data.get("day_of_month") or now.day
                    add_or_update_subscription(sub_name, amt, data.get("bank", "BCC"), day)

                ai_comment = data.get("ai_comment", "Записано!")
                if tx_type == TYPE_INCOME:
                    icon, label = "💰", "Доход записан"
                else:
                    icon, label = "✅", "Записано"
                resp = f"{icon} **{label}:** {amt} {data.get('currency', 'KZT')} | {data.get('bank', 'BCC')} | {cat}\n\n💬 {ai_comment}"
                if duplicate:
                    dup_merchant = duplicate.get("merchant") or duplicate.get("user_comment") or ""
                    resp += (
                        f"\n\n⚠️ Похоже, такую же сумму ({amt} ₸{f', {dup_merchant}' if dup_merchant else ''}) "
                        f"я уже записывала несколько минут назад — если это дубль, скажи «удали последнюю запись»."
                    )
                add_chat_message(chat_id, "Ада", resp)
                await safe_answer(message, resp)

        elif intent == "add_reminder":
            target = data.get("reminder_target", user_name)
            rem_text = data.get("reminder_text", "Напоминание")
            recurrence = data.get("recurrence", "once")

            raw_times = data.get("reminder_times")
            if not raw_times:
                single = data.get("reminder_time")
                raw_times = [single] if single else []

            now = now_astana()
            normalized_times = [t for t in (_normalize_reminder_time(rt, now) for rt in raw_times) if t]

            if normalized_times:
                for rt in normalized_times:
                    add_reminder(target, rt, rem_text, recurrence)
                if data.get("reply"):
                    reply_text = data["reply"]
                elif len(normalized_times) > 1:
                    times_str = ", ".join(_format_time_short(t) for t in normalized_times)
                    reply_text = f"Запомнила! Поставила {target} {len(normalized_times)} напоминания на {times_str}."
                else:
                    reply_text = f"Запомнила! Напомню {target} в {_format_time_short(normalized_times[0])}."
            else:
                reply_text = data.get("reply") or (
                    "Не совсем поняла, на какое время поставить напоминание. "
                    "Уточни, например: «в 14:00» или «завтра в 9 утра»."
                )

            add_chat_message(chat_id, "Ада", reply_text)
            await safe_answer(message, reply_text)

        elif intent == "add_shopping":
            items = data.get("shopping_items", [])
            if items:
                add_shopping_items(items, user_name)
                reply_text = data.get("reply", "Закинула в список!")
            else:
                reply_text = data.get("reply") or "Не поняла, что добавить в список покупок."
            add_chat_message(chat_id, "Ада", reply_text)
            await safe_answer(message, reply_text)

        elif intent == "clear_shopping":
            items = data.get("shopping_items", [])
            if items:
                mark_shopping_items_done(items)
                reply_text = data.get("reply", "Вычеркнула.")
            else:
                reply_text = data.get("reply") or "Не поняла, что вычеркнуть из списка."
            add_chat_message(chat_id, "Ада", reply_text)
            await safe_answer(message, reply_text)

        elif intent == "add_trip":
            add_trip_plan(
                destination=data.get("trip_destination", "Поездка"),
                dates=data.get("trip_dates", "Даты не указаны"),
                budget=_to_number(data.get("trip_budget", 0)),
                notes=data.get("trip_notes", "")
            )
            reply_text = data.get("reply", "План поездки сохранен!")
            add_chat_message(chat_id, "Ада", reply_text)
            await safe_answer(message, reply_text)

        else:
            reply_text = data.get("reply", "Принято.")
            add_chat_message(chat_id, "Ада", reply_text)
            await safe_answer(message, reply_text)

    except Exception as e:
        print(f"[Обработка текста] Ошибка: {e}")
        traceback.print_exc()
        await safe_answer(message, "Ошибка связи, попробуй позже.")

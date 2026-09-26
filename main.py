"""Family Finance Bot — Ада (Main)."""

import asyncio
import datetime
import os

from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import BufferedInputFile
try:
    from aiogram.client.default import DefaultBotProperties
except Exception:
    DefaultBotProperties = None

from config import TELEGRAM_BOT_TOKEN, FAMILY_CHAT_ID, VLAD_TELEGRAM_ID, DIANA_TELEGRAM_ID
from handlers.text_handler import (
    handle_text, handle_voice, handle_category_clarification_callback
)
from handlers.media_handler import handle_media
from services.sheets import (
    append_transaction,
    ensure_power_bi_dimension_table, process_due_subscriptions,
    normalize_existing_family_table_values, get_subscription_warnings,
    mark_subscription_warning_sent, get_transactions_for_period,
    save_category_limits, get_category_limits,
    get_active_price_trackings, record_price_check_success,
    record_price_check_failure, set_price_tracking_status,
)
from services.price_tracker import fetch_product_info
from services.categories import FALLBACK_EXPENSE_CATEGORY, TYPE_INCOME, is_income_type
from services.telegram_safe import safe_answer, safe_send_message
from services.pending_receipts import sweep_expired
from services.pending_clarifications import sweep_expired_clarifications
from services.timezone import ASTANA_TZ, parse_flexible_datetime
from services.weather import get_weather_forecast, get_tomorrow_forecast
from services.charts import generate_expense_chart, generate_trend_chart
from services.reports import generate_pdf_report, generate_excel_export
from services.analytics import analyze_budget_leaks, detect_category_pace_anomalies
from services.deepseek_service import generate_budget_reflection
from services.limits_ai import generate_limits_from_history
from services.timezone import now_astana
from services.reminders import deliver_due
from services import preflight, state, debts

if DefaultBotProperties:
    bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
else:
    bot = Bot(token=TELEGRAM_BOT_TOKEN, parse_mode="Markdown")
dp = Dispatcher()


class FamilyPrivacyMiddleware(BaseMiddleware):
    """Пускает к боту только Влада и Диану — по Telegram ID, и только в
    семейном групповом чате или в личной переписке с ботом.

    Раньше был баг «ИЛИ»: сообщение пропускалось, если чат совпадал с
    FAMILY_CHAT_ID — то есть ЛЮБОЙ человек, оказавшийся в семейной группе
    (добавили по ошибке, бывший арендатор остался в чате и т.п.), мог
    писать боту, и его сообщения обрабатывались бы наравне с семьёй.
    Также ловилась обратная дыра: настоящий Влад/Диана из СОВСЕМ ДРУГОГО
    чата (например, бот случайно добавлен в другую группу) тоже прошли бы.
    Теперь нужны оба условия: и ID отправителя, и разрешённый чат
    (семейная группа или личка с ботом).
    """

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None) or getattr(getattr(event, "message", None), "chat", None)

        allowed_users = {str(x) for x in (VLAD_TELEGRAM_ID, DIANA_TELEGRAM_ID) if x}
        allowed_user = bool(user and str(user.id) in allowed_users)
        allowed_chat = bool(chat and (
            int(chat.id) == int(FAMILY_CHAT_ID) or getattr(chat, "type", "") == "private"
        ))

        if allowed_user and allowed_chat:
            return await handler(event, data)

        if hasattr(event, "answer") and chat:
            try:
                await event.answer("Это приватный семейный бот.")
            except Exception:
                pass
        return None


dp.message.middleware(FamilyPrivacyMiddleware())
dp.callback_query.middleware(FamilyPrivacyMiddleware())


def _format_currency(value):
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except (ValueError, TypeError):
        return str(value)


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await safe_answer(
        message,
        "Привет! Я Ада — твоя финансовая помощница.\n\n"
        "Что я умею:\n"
        "• Учёт трат и доходов (текстом, фото чеков, PDF)\n"
        "• Анализ «утечек бюджета» (/leaks)\n"
        "• Экспорт отчётов в PDF (/report) и Excel (/export)\n"
        "• Лимиты бюджета по категориям\n"
        "• Напоминания (разовые, ежедневные, ежемесячные)\n"
        "• Список покупок\n"
        "• Рассрочки и Kaspi Red\n"
        "• Подписки и регулярные платежи\n"
        "• Планирование поездок\n"
        "• Прогноз погоды (на сегодня, завтра или неделю)\n"
        "• Графики расходов (/chart)\n"
        "• Голосовые сообщения\n\n"
        "Просто напиши мне о покупке, доходе или пришли чек!"
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await safe_answer(
        message,
        "📋 Команды:\n"
        "/start — начать работу\n"
        "/chart — график расходов за месяц\n"
        "/trend — динамика расходов по месяцам\n"
        "/chatid — показать ID текущего чата\n"
        "/leaks — анализ утечек бюджета (микротраты)\n"
        "/report — скачать PDF-буклет за месяц\n"
        "/export — скачать выписку в Excel (.xlsx)\n"
        "/debug — диагностика таблицы\n\n"
        "💡 Примеры сообщений:\n"
        "• 'Купил колу за 500 тг'\n"
        "• 'Зарплата 300000'\n"
        "• 'Куда уходят деньги?'\n"
        "• 'Погода на завтра'\n"
        "• 'Прогноз на неделю'\n"
        "• 'Напомни в 21:00 выпить витамины'\n"
        "• 'Добавь в покупки: молоко, хлеб'\n"
        "• 'Покажи лимиты'"
    )


@dp.message(Command("chatid"))
async def cmd_chatid(message: types.Message):
    await safe_answer(
        message,
        f"chat_id: `{message.chat.id}`\n"
        f"user_id: `{message.from_user.id if message.from_user else ''}`\n"
        "Скопируй chat_id в FAMILY_CHAT_ID."
    )


@dp.message(Command("chart"))
async def cmd_chart(message: types.Message):
    try:
        image_bytes = await asyncio.to_thread(generate_expense_chart)
        if image_bytes:
            photo = BufferedInputFile(image_bytes, filename="chart.png")
            await message.answer_photo(photo=photo, caption="📊 Вот твой финансовый дашборд.")
        else:
            await safe_answer(message, "Нет данных для построения графика.")
    except Exception as error:
        print(f"[График] Ошибка: {error}")
        await safe_answer(message, "Не удалось построить график.")


@dp.message(Command("trend"))
async def cmd_trend(message: types.Message):
    try:
        image_bytes = await asyncio.to_thread(generate_trend_chart)
        if image_bytes:
            photo = BufferedInputFile(image_bytes, filename="trend.png")
            await message.answer_photo(photo=photo, caption="📈 Динамика расходов по месяцам.")
        else:
            await safe_answer(message, "Нет данных для графика динамики.")
    except Exception as error:
        print(f"[Trend] Ошибка: {error}")
        await safe_answer(message, "Не удалось построить график динамики.")


@dp.message(Command("leaks"))
async def cmd_leaks(message: types.Message):
    leak_data = await asyncio.to_thread(analyze_budget_leaks)
    text = leak_data["text"]
    reflection = await generate_budget_reflection("leaks", text)
    if reflection:
        text = f"{reflection}\n\n{text}"
    await safe_answer(message, text)


@dp.message(Command("report"))
async def cmd_report(message: types.Message):
    try:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_document")
        pdf_bytes = await asyncio.to_thread(generate_pdf_report)
        pdf_file = BufferedInputFile(pdf_bytes, filename=f"Finance_Report_{now_astana().strftime('%Y_%m')}.pdf")
        await message.answer_document(document=pdf_file, caption="📑 Официальный семейный финансовый отчёт за месяц.")
    except Exception as e:
        print(f"[PDF] Ошибка: {e}")
        await safe_answer(message, "Не удалось сформировать PDF-отчёт.")


@dp.message(Command("export"))
async def cmd_export(message: types.Message):
    try:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_document")
        excel_bytes = await asyncio.to_thread(generate_excel_export)
        excel_file = BufferedInputFile(excel_bytes, filename=f"Family_Finance_{now_astana().strftime('%Y_%m')}.xlsx")
        await message.answer_document(document=excel_file, caption="📊 Полная выписка в формате Excel (.xlsx).")
    except Exception as e:
        print(f"[Excel] Ошибка: {e}")
        await safe_answer(message, "Не удалось сформировать файл Excel.")


@dp.message(Command("debug"))
async def cmd_debug(message: types.Message):
    from services.sheets import debug_transactions_snapshot
    debug_text = debug_transactions_snapshot()
    await safe_answer(message, f"```\n{debug_text}\n```")


@dp.callback_query(F.data.startswith("clarify_opt:"))
async def process_category_clarification(callback: types.CallbackQuery):
    await handle_category_clarification_callback(callback)


@dp.message()
async def handle_all_messages(message: types.Message):
    if message.photo or message.document:
        await handle_media(message)
    elif message.voice or message.audio:
        await handle_voice(message)
    elif message.text:
        await handle_text(message)
    else:
        await safe_answer(message, "Я понимаю текст, фото, PDF и голосовые.")


# ═══════════════════════════════════════════════════════════════════════════════
# ФОНОВЫЕ ЗАДАЧИ
# ═══════════════════════════════════════════════════════════════════════════════

async def check_reminders():
    """Доставка просроченных напоминаний в семейный чат.

    Логика теперь в services/reminders.py (deliver_due): именное упоминание
    Влада/Дианы через tg://user, HTML-форматирование, и главное — корректный
    row_idx при продлении daily/weekly/monthly (раньше при повторных
    напоминаниях правился жёстко заданный номер строки в этом же файле).
    """
    while True:
        try:
            await deliver_due(bot)
        except Exception as e:
            print(f"[Напоминания] Ошибка цикла: {e}")
        await asyncio.sleep(60)


async def check_debt_reminders():
    """Предупреждение о приближающемся/просроченном сроке долга — по тому
    же принципу, что и предупреждения о подписках выше, только раз в день
    (у долга нет смысла проверять каждый час)."""
    while True:
        try:
            now = datetime.datetime.now(ASTANA_TZ)
            if now.hour == 10 and now.minute < 5:
                today_str = now.strftime("%Y-%m-%d")
                due_list = await asyncio.to_thread(debts.get_debts_due_soon, 3)
                for d in due_list:
                    debt_id = d.get("debt_id", "")
                    marker_key = f"debt_due:{debt_id}:{today_str}"
                    if state.get("scheduler", marker_key):
                        continue
                    state.put("scheduler", marker_key, {"warned_at": now.isoformat()})
                    days_left = d.get("days_left", 0)
                    direction = "нам должны" if d.get("direction") == "lent" else "мы должны"
                    when_txt = "уже просрочен" if days_left < 0 else (
                        "сегодня срок" if days_left == 0 else f"через {days_left} дн."
                    )
                    await safe_send_message(
                        bot, chat_id=FAMILY_CHAT_ID,
                        text=(
                            f"⏳ Долг ({direction}, {d.get('counterparty')}): "
                            f"{_format_currency(d.get('balance'))} тг — срок {when_txt} "
                            f"({d.get('due_date')})."
                        ),
                    )
        except Exception as e:
            print(f"[Долги] Ошибка цикла напоминаний: {e}")
        await asyncio.sleep(60)


async def check_limit_warnings():
    """Раз в день предупреждает, если по категории расходов почти выбран
    месячный лимит — не только постфактум по факту превышения (это уже
    видно в отчётах), а заранее, пока ещё можно притормозить."""
    while True:
        try:
            now = datetime.datetime.now(ASTANA_TZ)
            if now.hour == 19 and now.minute < 5:
                month_key = now.strftime("%Y-%m")
                limits = await asyncio.to_thread(get_category_limits)
                if limits:
                    start = now.replace(day=1).strftime("%Y-%m-%d")
                    # +1 день, т.к. get_transactions_for_period сравнивает как
                    # [start, end) — без этого сегодняшние траты (тот же
                    # календарный день, что и now) никогда бы не попадали
                    # в подсчёт, хотя проверка идёт именно "на сегодня".
                    end = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                    txs = await asyncio.to_thread(get_transactions_for_period, start, end)
                    spent = {}
                    for t in txs:
                        if is_income_type(t.get("type")):
                            continue
                        cat = str(t.get("cat") or "")
                        spent[cat] = spent.get(cat, 0.0) + float(t.get("amt") or 0)

                    for cat, limit in limits.items():
                        limit = float(limit or 0)
                        if limit <= 0:
                            continue
                        amount_spent = spent.get(cat, 0.0)
                        pct = amount_spent / limit
                        if pct < 0.85:
                            continue
                        marker_key = f"limit_warning:{cat}:{month_key}"
                        if state.get("scheduler", marker_key):
                            continue
                        state.put("scheduler", marker_key, {"warned_at": now.isoformat(), "pct": pct})
                        if pct >= 1:
                            status = f"лимит превышен на {_format_currency(amount_spent - limit)} тг"
                        else:
                            status = f"осталось {_format_currency(limit - amount_spent)} тг до конца месяца"
                        fact_text = (
                            f"Категория «{cat}»: потрачено {_format_currency(amount_spent)} из "
                            f"{_format_currency(limit)} тг за месяц ({pct * 100:.0f}% лимита, {status})."
                        )
                        reflection = await generate_budget_reflection("limit_warning", fact_text)
                        text = f"{reflection}\n\n📊 {fact_text}" if reflection else f"📊 {fact_text}"
                        await safe_send_message(bot, chat_id=FAMILY_CHAT_ID, text=text)
        except Exception as e:
            print(f"[Лимиты] Ошибка цикла предупреждений: {e}")
        await asyncio.sleep(60)


async def check_price_tracking():
    """Каждую активную позицию перепроверяем раз в ~3 часа (не глобально по
    времени суток, а по last_checked_at конкретной строки — так после
    перезапуска процесса ничего не "проспится" до следующего дня)."""
    while True:
        try:
            now = datetime.datetime.now(ASTANA_TZ)
            items = await asyncio.to_thread(get_active_price_trackings)
            for item in items:
                last_checked_str = str(item.get("last_checked_at") or "")
                try:
                    last_checked = datetime.datetime.strptime(last_checked_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ASTANA_TZ)
                except ValueError:
                    last_checked = None
                if last_checked and (now - last_checked) < datetime.timedelta(hours=3):
                    continue

                row_idx = item.get("row_idx")
                info = await fetch_product_info(item.get("url"))
                if not info or not info.get("price"):
                    fail_count = await asyncio.to_thread(record_price_check_failure, row_idx)
                    if fail_count >= 3:
                        await asyncio.to_thread(set_price_tracking_status, row_idx, "broken")
                        await safe_send_message(
                            bot, chat_id=FAMILY_CHAT_ID,
                            text=(
                                f"⚠️ Не могу больше проверять цену на «{item.get('product_name')}» — "
                                f"похоже, страница на Kaspi изменилась. Отслеживание остановлено, "
                                f"пришли ссылку заново, если товар всё ещё актуален."
                            ),
                        )
                    continue

                new_price = info.get("price")
                image_url = info.get("image_url") or item.get("image_url") or ""
                first_price = float(item.get("first_price") or 0)
                try:
                    target_price = float(item.get("target_price")) if item.get("target_price") not in ("", None) else None
                except (TypeError, ValueError):
                    target_price = None

                dropped = first_price > 0 and new_price < first_price
                reached_target = target_price is not None and new_price <= target_price

                if dropped or reached_target:
                    caption = (
                        f"📉 Цена упала: **{item.get('product_name')}**\n"
                        f"Было: {_format_currency(first_price)} тг → Сейчас: {_format_currency(new_price)} тг\n"
                        f"{info.get('url')}"
                    )
                    if reached_target:
                        caption += f"\n\n🎯 Достигнута нужная цена ({_format_currency(target_price)} тг)!"
                    try:
                        if image_url:
                            await bot.send_photo(chat_id=FAMILY_CHAT_ID, photo=image_url, caption=caption)
                        else:
                            await safe_send_message(bot, chat_id=FAMILY_CHAT_ID, text=caption)
                    except Exception as send_error:
                        print(f"[Цены] Не удалось отправить фото, отправляю текстом: {send_error}")
                        await safe_send_message(bot, chat_id=FAMILY_CHAT_ID, text=caption)
                    await asyncio.to_thread(record_price_check_success, row_idx, new_price, image_url)
                    if reached_target:
                        await asyncio.to_thread(set_price_tracking_status, row_idx, "reached")
                else:
                    await asyncio.to_thread(record_price_check_success, row_idx, new_price, image_url)
        except Exception as e:
            print(f"[Цены] Ошибка цикла отслеживания: {e}")
        await asyncio.sleep(3600)


async def check_subscriptions():
    while True:
        try:
            now = datetime.datetime.now(ASTANA_TZ)
            # Раньше проверялось только "now.minute == 5" — если процесс не
            # работал ровно в эту минуту (перезапуск, деплой), проверка
            # подписок в этот час пропускалась совсем. Идемпотентность и
            # так обеспечена внутри process_due_subscriptions/get_subscription_warnings
            # (по last_paid/дате предупреждения), так что здесь достаточно
            # widen-окна — двойной записи не будет.
            if now.minute >= 5:
                hour_key = f"subscriptions:{now.strftime('%Y-%m-%dT%H')}"
                state.put("scheduler", hour_key, {"checked_at": now.isoformat()})
                warnings = await asyncio.to_thread(get_subscription_warnings, now, 2)
                for item in warnings:
                    await safe_send_message(
                        bot, chat_id=FAMILY_CHAT_ID,
                        text=(
                            f"⚠️ Через 2 дня списание подписки: **{item.get('name')}** — "
                            f"{_format_currency(item.get('amount'))} тг ({item.get('bank')})."
                        )
                    )
                    await asyncio.to_thread(mark_subscription_warning_sent, item.get("row_idx"), now.strftime("%Y-%m-%d"))

                due = await asyncio.to_thread(process_due_subscriptions, now)
                if due:
                    await safe_send_message(bot, chat_id=FAMILY_CHAT_ID, text=f"💳 Автосписание подписок: {', '.join(due)}.")
        except Exception as e:
            print(f"[Подписки] Ошибка цикла: {e}")
        await asyncio.sleep(60)


async def weather_scheduler():
    """Раньше "отправлено сегодня" хранилось в локальных переменных внутри
    функции — при перезапуске процесса (например, деплой на Railway прямо
    в 8:30 утра) счётчик обнулялся, и прогноз мог уйти в чат повторно.
    Теперь маркер "уже отправлено" переживает перезапуск (state.py)."""
    while True:
        try:
            now = datetime.datetime.now(ASTANA_TZ)
            today_str = now.strftime("%Y-%m-%d")

            # 08:30 — утренний прогноз
            if (now.hour == 8 and now.minute >= 30) or (now.hour == 9 and now.minute == 0):
                marker_key = f"weather_morning:{today_str}"
                if not state.get("scheduler", marker_key):
                    forecast = await get_weather_forecast(target="today")
                    if forecast:
                        await safe_send_message(bot, chat_id=FAMILY_CHAT_ID, text=forecast)
                        state.put("scheduler", marker_key, {"sent_at": now.isoformat()})

            # 22:30 — вечерний прогноз на завтра
            if (now.hour == 22 and now.minute >= 30) or (now.hour == 23 and now.minute == 0):
                marker_key = f"weather_evening:{today_str}"
                if not state.get("scheduler", marker_key):
                    forecast = await get_tomorrow_forecast()
                    if forecast:
                        await safe_send_message(bot, chat_id=FAMILY_CHAT_ID, text=forecast)
                        state.put("scheduler", marker_key, {"sent_at": now.isoformat()})

        except Exception as e:
            print(f"[Погода-Шедулер] Ошибка: {e}")

        await asyncio.sleep(30)


async def sweep_pending_receipts():
    while True:
        try:
            expired = sweep_expired()
            for key, transactions, user_name in expired:
                chat_id = int(str(key).split(":", 1)[0])
                for tx in transactions:
                    tx["user"] = user_name
                    tx["user_comment"] = tx.get("user_comment", "") or "(без комментария — авто-сохранение)"
                    await asyncio.to_thread(append_transaction, tx)
                try:
                    await safe_send_message(
                        bot, chat_id=chat_id,
                        text=f"⏰ Автоматически сохранила {len(transactions)} чек(ов) без комментария."
                    )
                except Exception:
                    pass
        except Exception as e:
            print(f"[Pending] Ошибка: {e}")
        await asyncio.sleep(60)


async def sweep_clarifications():
    while True:
        try:
            expired = sweep_expired_clarifications()
            for key, tx in expired:
                chat_id = int(str(key).split(":", 1)[0])
                if not tx.get("category"):
                    tx["category"] = FALLBACK_EXPENSE_CATEGORY
                comm = tx.get("user_comment", "")
                tx["user_comment"] = f"{comm} (автосохранение: {tx['category']})".strip()

                await asyncio.to_thread(append_transaction, tx)
                try:
                    await safe_send_message(
                        bot, chat_id=chat_id,
                        text=f"⏰ Автосохранение: {_format_currency(tx.get('amount', 0))} тг → {tx.get('category')} (не дождалась ответа)"
                    )
                except Exception:
                    pass
        except Exception as e:
            print(f"[Clarifications] Ошибка: {e}")
        await asyncio.sleep(60)


def _month_range(year: int, month: int) -> tuple[str, str]:
    start = datetime.date(year, month, 1).strftime("%Y-%m-%d")
    if month == 12:
        end = datetime.date(year + 1, 1, 1).strftime("%Y-%m-%d")
    else:
        end = datetime.date(year, month + 1, 1).strftime("%Y-%m-%d")
    return start, end


def _period_summary_text(title: str, start: str, end: str, compare_start: str = None, compare_end: str = None) -> str:
    txs = get_transactions_for_period(start, end)
    income = 0.0
    expense = 0.0
    by_cat = {}
    for t in txs:
        amt = float(t.get("amt") or 0)
        if is_income_type(t.get("type")):
            income += amt
        else:
            expense += amt
            cat = str(t.get("cat") or "Прочее")
            by_cat[cat] = by_cat.get(cat, 0.0) + amt

    lines = [f"📊 **{title}**"]
    lines.append(f"💰 Доходы: {_format_currency(income)} тг")
    lines.append(f"💸 Расходы: {_format_currency(expense)} тг")
    lines.append(f"📈 Баланс: {_format_currency(income - expense)} тг")

    if compare_start and compare_end:
        prev_txs = get_transactions_for_period(compare_start, compare_end)
        prev_expense = sum(float(t.get("amt") or 0) for t in prev_txs if not is_income_type(t.get("type")))
        if prev_expense > 0:
            diff_pct = (expense - prev_expense) / prev_expense * 100
            if abs(diff_pct) >= 1:
                word = "больше" if diff_pct > 0 else "меньше"
                lines.append(f"📐 Расходы на {abs(diff_pct):.0f}% {word}, чем за предыдущий период ({_format_currency(prev_expense)} тг).")
            else:
                lines.append("📐 Расходы примерно на уровне предыдущего периода.")

    if by_cat:
        top = sorted(by_cat.items(), key=lambda x: -x[1])[:5]
        lines.append("\nТоп категорий:")
        for cat, amount in top:
            lines.append(f"- {cat}: {_format_currency(amount)} тг")
    return "\n".join(lines)


async def finance_report_scheduler():
    """Еженедельный и месячный автоотчёт в семейный чат.

    Маркеры "уже отправлено" — в state.py (переживают перезапуск), как и у
    остальных шедулеров; раньше здесь были локальные переменные, которые
    обнулялись при каждом деплое и рисковали задвоить отчёт."""
    while True:
        try:
            now = datetime.datetime.now(ASTANA_TZ)
            today = now.date().isoformat()

            # Еженедельный дайджест — по понедельникам в 09:10 за последние 7 дней,
            # со сравнением с предыдущей неделей.
            if now.weekday() == 0 and now.hour == 9 and now.minute >= 10:
                marker_key = f"weekly_report:{today}"
                if not state.get("scheduler", marker_key):
                    state.put("scheduler", marker_key, {"sent_at": now.isoformat()})
                    start_dt = (now.date() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
                    end_dt = now.date().strftime("%Y-%m-%d")
                    cmp_start = (now.date() - datetime.timedelta(days=14)).strftime("%Y-%m-%d")
                    cmp_end = start_dt
                    text = await asyncio.to_thread(
                        _period_summary_text, "Еженедельный финансовый дайджест",
                        start_dt, end_dt, cmp_start, cmp_end,
                    )
                    reflection = await generate_budget_reflection("weekly", text)
                    if reflection:
                        text = f"{reflection}\n\n{text}"

                    # Категории БЕЗ заданного лимита (их не видит
                    # check_limit_warnings) — раз в неделю мягко сверяем
                    # темп трат month-to-date с обычным для этой же
                    # категории за прошлые месяцы. Молчит, если истории
                    # мало или отклонение несущественное.
                    try:
                        pace_facts = await asyncio.to_thread(detect_category_pace_anomalies)
                        if pace_facts:
                            pace_reflection = await generate_budget_reflection("category_pace", pace_facts)
                            if pace_reflection:
                                text = f"{text}\n\n{pace_reflection}"
                    except Exception as pace_error:
                        print(f"[Темп категорий] Пропущено: {pace_error}")

                    await safe_send_message(bot, chat_id=FAMILY_CHAT_ID, text=text)

            # Месячный дайджест — 1-го числа в 09:15 за прошлый месяц, со
            # сравнением с позапрошлым.
            if now.day == 1 and now.hour == 9 and now.minute >= 15:
                marker_key = f"monthly_report:{today}"
                if not state.get("scheduler", marker_key):
                    state.put("scheduler", marker_key, {"sent_at": now.isoformat()})
                    prev_month_last_day = now.date().replace(day=1) - datetime.timedelta(days=1)
                    start_dt, end_dt = _month_range(prev_month_last_day.year, prev_month_last_day.month)
                    prev2 = prev_month_last_day.replace(day=1) - datetime.timedelta(days=1)
                    cmp_start, cmp_end = _month_range(prev2.year, prev2.month)
                    text = await asyncio.to_thread(
                        _period_summary_text,
                        f"Месячный отчёт за {prev_month_last_day.strftime('%m.%Y')}",
                        start_dt, end_dt, cmp_start, cmp_end,
                    )
                    reflection = await generate_budget_reflection("monthly", text)
                    if reflection:
                        text = f"{reflection}\n\n{text}"
                    await safe_send_message(bot, chat_id=FAMILY_CHAT_ID, text=text)
        except Exception as e:
            print(f"[Автоотчёты] Ошибка: {e}")
        await asyncio.sleep(60)


async def monthly_limits_scheduler():
    """Автоподтверждение/перегенерация лимитов 1-го числа.

    Маркер "уже сделано в этом месяце" — в state.py (переживает
    перезапуск), как и для остальных шедулеров выше.
    """
    while True:
        try:
            now = datetime.datetime.now(ASTANA_TZ)
            month_key = now.strftime("%Y-%m")
            marker_key = f"limits:{month_key}"
            auto_enabled = os.environ.get("AUTO_GENERATE_LIMITS", "").strip().lower() in {"1", "true", "yes"}
            if now.day == 1 and now.hour == 9 and now.minute >= 20 and auto_enabled and not state.get("scheduler", marker_key):
                new_limits = await generate_limits_from_history()
                if new_limits:
                    await safe_send_message(
                        bot, chat_id=FAMILY_CHAT_ID,
                        text=f"✅ Лимиты на {month_key} автоматически обновлены и подтверждены."
                    )
                state.put("scheduler", marker_key, {"generated_at": now.isoformat()})
        except Exception as e:
            print(f"[Автолимиты] Ошибка: {e}")
        await asyncio.sleep(60)


async def main():
    await asyncio.to_thread(ensure_power_bi_dimension_table)
    await asyncio.to_thread(normalize_existing_family_table_values)
    try:
        # Создаёт недостающие листы (в т.ч. Debts) с нужными заголовками.
        # Ничего не удаляет и не трогает уже существующие данные.
        await asyncio.to_thread(preflight.initialize_optional)
    except Exception as e:
        print(f"[Preflight] Не удалось проверить/создать листы: {e}")

    asyncio.create_task(check_reminders())
    asyncio.create_task(check_subscriptions())
    asyncio.create_task(check_price_tracking())
    asyncio.create_task(check_debt_reminders())
    asyncio.create_task(check_limit_warnings())
    asyncio.create_task(weather_scheduler())
    asyncio.create_task(finance_report_scheduler())
    asyncio.create_task(monthly_limits_scheduler())
    asyncio.create_task(sweep_pending_receipts())
    asyncio.create_task(sweep_clarifications())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

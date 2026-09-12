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
    save_category_limits,
)
from services.categories import FALLBACK_EXPENSE_CATEGORY, TYPE_INCOME, is_income_type
from services.telegram_safe import safe_answer, safe_send_message
from services.pending_receipts import sweep_expired
from services.pending_clarifications import sweep_expired_clarifications
from services.timezone import ASTANA_TZ, parse_flexible_datetime
from services.weather import get_weather_forecast, get_tomorrow_forecast
from services.charts import generate_expense_chart, generate_trend_chart
from services.reports import generate_pdf_report, generate_excel_export
from services.analytics import analyze_budget_leaks
from services.limits_ai import generate_limits_from_history
from services.timezone import now_astana
from services.reminders import deliver_due
from services import preflight, state

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
    leak_data = analyze_budget_leaks()
    await safe_answer(message, leak_data["text"])


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


def _period_summary_text(title: str, start: str, end: str) -> str:
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
    if by_cat:
        top = sorted(by_cat.items(), key=lambda x: -x[1])[:5]
        lines.append("\nТоп категорий:")
        for cat, amount in top:
            lines.append(f"- {cat}: {_format_currency(amount)} тг")
    return "\n".join(lines)


async def finance_report_scheduler():
    """Еженедельный и месячный автоотчёт в семейный чат."""
    sent_weekly = None
    sent_monthly = None
    while True:
        try:
            now = datetime.datetime.now(ASTANA_TZ)
            today = now.date().isoformat()

            # Еженедельный дайджест — по понедельникам в 09:10 за последние 7 дней.
            if now.weekday() == 0 and now.hour == 9 and now.minute >= 10 and sent_weekly != today:
                start_dt = (now.date() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
                end_dt = now.date().strftime("%Y-%m-%d")
                text = await asyncio.to_thread(_period_summary_text, "Еженедельный финансовый дайджест", start_dt, end_dt)
                await safe_send_message(bot, chat_id=FAMILY_CHAT_ID, text=text)
                sent_weekly = today

            # Месячный дайджест — 1-го числа в 09:15 за прошлый месяц.
            if now.day == 1 and now.hour == 9 and now.minute >= 15 and sent_monthly != today:
                prev_month_last_day = now.date().replace(day=1) - datetime.timedelta(days=1)
                start_dt, end_dt = _month_range(prev_month_last_day.year, prev_month_last_day.month)
                text = await asyncio.to_thread(
                    _period_summary_text,
                    f"Месячный отчёт за {prev_month_last_day.strftime('%m.%Y')}",
                    start_dt,
                    end_dt,
                )
                await safe_send_message(bot, chat_id=FAMILY_CHAT_ID, text=text)
                sent_monthly = today
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
    asyncio.create_task(weather_scheduler())
    asyncio.create_task(finance_report_scheduler())
    asyncio.create_task(monthly_limits_scheduler())
    asyncio.create_task(sweep_pending_receipts())
    asyncio.create_task(sweep_clarifications())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

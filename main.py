"""Family Finance Bot — Ада (Main)."""

import asyncio
import datetime
import logging
import os
import contextlib

from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import BufferedInputFile
try:
    from aiogram.client.default import DefaultBotProperties
except Exception:
    DefaultBotProperties = None

from config import TELEGRAM_BOT_TOKEN, FAMILY_CHAT_ID, VLAD_TELEGRAM_ID, DIANA_TELEGRAM_ID, validate_settings
from services import reminders as reminder_service, state
from services.pending_receipts import ack_pending
from services.pending_clarifications import ack_clarification
from handlers.text_handler import (
    handle_text, handle_voice, handle_category_clarification_callback
)
from handlers.media_handler import handle_media
from services.sheets import (
    get_pending_reminders, mark_reminder_done, append_transaction,
    ensure_power_bi_dimension_table, process_due_subscriptions,
    normalize_existing_family_table_values, get_subscription_warnings,
    mark_subscription_warning_sent, get_transactions_for_period,
    save_category_limits,
)
from services.categories import FALLBACK_EXPENSE_CATEGORY, TYPE_INCOME
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

validate_settings()

if DefaultBotProperties:
    bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
else:
    bot = Bot(token=TELEGRAM_BOT_TOKEN, parse_mode="Markdown")
dp = Dispatcher()


class FamilyPrivacyMiddleware(BaseMiddleware):
    """Пускает к боту только семейный чат или разрешённые Telegram ID.

    Бот приватный: даже если токен/username узнает посторонний, он не сможет
    читать отчёты, писать траты или смотреть таблицу.
    """

    async def __call__(self, handler, event, data):
        chat = getattr(event, "chat", None) or getattr(getattr(event, "message", None), "chat", None)
        user = getattr(event, "from_user", None)

        allowed_chat = bool(chat and int(chat.id) == int(FAMILY_CHAT_ID))
        allowed_users = {str(x) for x in (VLAD_TELEGRAM_ID, DIANA_TELEGRAM_ID) if x}
        allowed_user = bool(user and str(user.id) in allowed_users)

        if allowed_user and (allowed_chat or (chat and getattr(chat.type, "value", chat.type) == "private")):
            if str(getattr(event, "text", "") or "").startswith("/"):
                from services.memory import add_chat_message
                from config import get_authorized_user_name
                add_chat_message(chat.id, get_authorized_user_name(user.id), event.text)
            return await handler(event, data)

        # Молча игнорируем callback, а в личке коротко объясняем.
        if hasattr(event, "answer") and chat:
            try:
                await event.answer("Это приватный семейный бот.")
            except Exception:
                pass
        return None


class SerialActionMiddleware(BaseMiddleware):
    """One mutation at a time; offline state and Sheets writes share a single worker."""
    def __init__(self):
        self.lock = asyncio.Lock()

    async def __call__(self, handler, event, data):
        async with self.lock:
            try:
                return await handler(event, data)
            except Exception:
                logging.exception("Update failed")
                message = getattr(event, "message", None) or event
                if getattr(message, "chat", None):
                    with contextlib.suppress(Exception):
                        await safe_answer(message, "Не удалось завершить действие. Проверь список записей перед повтором. "
                                          "Проверяй подтверждение записи; ожидающие чеки можно уточнить повторно.")
                return None

serial_actions = SerialActionMiddleware()
dp.message.outer_middleware(FamilyPrivacyMiddleware())
dp.callback_query.outer_middleware(FamilyPrivacyMiddleware())
dp.message.outer_middleware(serial_actions)
dp.callback_query.outer_middleware(serial_actions)


def _format_currency(value):
    from services.money import parse_amount
    amount = parse_amount(value)
    return f"{amount:,.{0 if amount.is_integer() else 2}f}".replace(",", " ")


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
        "• Долги и частичные возвраты (/debts, /debts_help)\n"
        "• Напоминания в группе: Владу, Диане или обоим\n"
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
        "/reminders — список активных напоминаний\n"
        "/debts — остатки долгов семьи\n"
        "/debts_help — как записывать долги и возвраты\n"
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
    debug_text = await asyncio.to_thread(debug_transactions_snapshot)
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
    while True:
        try:
            await reminder_service.deliver_due(bot)
        except Exception:
            logging.exception("Reminder loop failed")
        await asyncio.sleep(60)


async def check_subscriptions():
    while True:
        try:
            now = datetime.datetime.now(ASTANA_TZ)
            hour_key = now.strftime("%Y-%m-%dT%H")
            if now.minute >= 5 and not state.get("scheduler", "subscriptions:" + hour_key):
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
                state.put("scheduler", "subscriptions:" + hour_key, True)
                if due:
                    await safe_send_message(bot, chat_id=FAMILY_CHAT_ID, text=f"💳 Автосписание подписок: {', '.join(due)}.")
        except Exception as e:
            print(f"[Подписки] Ошибка цикла: {e}")
        await asyncio.sleep(60)


async def weather_scheduler():
    sent_morning_today = None
    sent_evening_today = None

    while True:
        try:
            now = datetime.datetime.now(ASTANA_TZ)
            today_str = now.strftime("%Y-%m-%d")

            # 08:30 — утренний прогноз
            if (now.hour == 8 and now.minute >= 30) or (now.hour == 9 and now.minute == 0):
                if not state.get("scheduler", "weather_morning:" + today_str):
                    forecast = await get_weather_forecast(target="today")
                    if forecast:
                        await safe_send_message(bot, chat_id=FAMILY_CHAT_ID, text=forecast)
                        state.put("scheduler", "weather_morning:" + today_str, True)

            # 22:30 — вечерний прогноз на завтра
            if (now.hour == 22 and now.minute >= 30) or (now.hour == 23 and now.minute == 0):
                if not state.get("scheduler", "weather_evening:" + today_str):
                    forecast = await get_tomorrow_forecast()
                    if forecast:
                        await safe_send_message(bot, chat_id=FAMILY_CHAT_ID, text=forecast)
                        state.put("scheduler", "weather_evening:" + today_str, True)

        except Exception as e:
            print(f"[Погода-Шедулер] Ошибка: {e}")

        await asyncio.sleep(30)


async def sweep_pending_receipts():
    while True:
        try:
            async with serial_actions.lock:
                for key, transactions, user_name in sweep_expired():
                    for tx in transactions:
                        tx["user"] = user_name
                        tx["user_comment"] = tx.get("user_comment") or "(без комментария — авто-сохранение)"
                        await asyncio.to_thread(append_transaction, tx)
                    ack_pending(key)
                    await safe_send_message(bot, state.chat_from_key(key),
                                            f"⏰ Сохранила {len(transactions)} позиций чека без дополнительного комментария.")
        except Exception:
            logging.exception("Receipt sweep failed; unsaved drafts retained")
        await asyncio.sleep(60)


async def sweep_clarifications():
    while True:
        try:
            async with serial_actions.lock:
                for key, tx in sweep_expired_clarifications():
                    if not tx.get("category"):
                        tx["category"] = FALLBACK_EXPENSE_CATEGORY
                    await asyncio.to_thread(append_transaction, tx)
                    ack_clarification(key)
                    await safe_send_message(bot, state.chat_from_key(key),
                        f"⏰ Автосохранение: {_format_currency(tx.get('amount', 0))} тг → {tx.get('category')}")
        except Exception:
            logging.exception("Clarification sweep failed; unsaved drafts retained")
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
        if str(t.get("type")) == TYPE_INCOME:
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
            if now.weekday() == 0 and now.hour == 9 and now.minute >= 10 and not state.get("scheduler", "weekly:" + today):
                start_dt = (now.date() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
                end_dt = now.date().strftime("%Y-%m-%d")
                text = await asyncio.to_thread(_period_summary_text, "Еженедельный финансовый дайджест", start_dt, end_dt)
                await safe_send_message(bot, chat_id=FAMILY_CHAT_ID, text=text)
                state.put("scheduler", "weekly:" + today, True)

            # Месячный дайджест — 1-го числа в 09:15 за прошлый месяц.
            if now.day == 1 and now.hour == 9 and now.minute >= 15 and not state.get("scheduler", "monthly:" + today):
                prev_month_last_day = now.date().replace(day=1) - datetime.timedelta(days=1)
                start_dt, end_dt = _month_range(prev_month_last_day.year, prev_month_last_day.month)
                text = await asyncio.to_thread(
                    _period_summary_text,
                    f"Месячный отчёт за {prev_month_last_day.strftime('%m.%Y')}",
                    start_dt,
                    end_dt,
                )
                await safe_send_message(bot, chat_id=FAMILY_CHAT_ID, text=text)
                state.put("scheduler", "monthly:" + today, True)
        except Exception as e:
            print(f"[Автоотчёты] Ошибка: {e}")
        await asyncio.sleep(60)


async def monthly_limits_scheduler():
    """Автоподтверждение/перегенерация лимитов 1-го числа."""
    sent_for = None
    while True:
        try:
            now = datetime.datetime.now(ASTANA_TZ)
            month_key = now.strftime("%Y-%m")
            if (os.getenv("AUTO_GENERATE_LIMITS", "true").lower() == "true"
                    and now.day == 1 and now.hour == 9 and now.minute >= 20
                    and not state.get("scheduler", "limits:" + month_key)):
                new_limits = await generate_limits_from_history()
                if new_limits:
                    await safe_send_message(
                        bot, chat_id=FAMILY_CHAT_ID,
                        text=f"✅ Лимиты на {month_key} автоматически обновлены и подтверждены."
                    )
                if new_limits:
                    state.put("scheduler", "limits:" + month_key, True)
        except Exception as e:
            print(f"[Автолимиты] Ошибка: {e}")
        await asyncio.sleep(60)


async def main():
    from services.preflight import check_schema, initialize_optional
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Read-only check first. Missing optional worksheets are then created idempotently.
    await asyncio.to_thread(check_schema)
    await asyncio.to_thread(initialize_optional)
    from services.debts import worksheet as debt_worksheet
    await asyncio.to_thread(reminder_service.worksheet)
    await asyncio.to_thread(debt_worksheet)
    # Original startup features retained, now additive/idempotent rather than destructive.
    await asyncio.to_thread(ensure_power_bi_dimension_table)
    stats = await asyncio.to_thread(normalize_existing_family_table_values)
    logging.info("Startup normalization: %s", stats)
    tasks = [asyncio.create_task(fn(), name=fn.__name__) for fn in (
        check_reminders, check_subscriptions, weather_scheduler, finance_report_scheduler,
        monthly_limits_scheduler, sweep_pending_receipts, sweep_clarifications)]
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        logging.info("Ada ready: polling, group reminders, 7 background workers")
        await dp.start_polling(bot)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    from services.runtime import worker_lock
    with worker_lock():
        asyncio.run(main())

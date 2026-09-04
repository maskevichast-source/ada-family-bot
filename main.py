"""Family Finance Bot — Ада (Main)."""

import asyncio
import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile

from config import TELEGRAM_BOT_TOKEN, FAMILY_CHAT_ID
from handlers.text_handler import (
    handle_text, handle_voice, handle_category_clarification_callback
)
from handlers.media_handler import handle_media
from services.sheets import (
    get_pending_reminders, mark_reminder_done, append_transaction,
    ensure_power_bi_dimension_table, process_due_subscriptions,
)
from services.categories import FALLBACK_EXPENSE_CATEGORY
from services.telegram_safe import safe_answer, safe_send_message
from services.pending_receipts import sweep_expired
from services.pending_clarifications import sweep_expired_clarifications
from services.timezone import ASTANA_TZ, parse_flexible_datetime
from services.weather import get_weather_forecast, get_tomorrow_forecast
from services.charts import generate_expense_chart
from services.reports import generate_pdf_report, generate_excel_export
from services.analytics import analyze_budget_leaks
from services.timezone import now_astana

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()


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
    while True:
        try:
            now = datetime.datetime.now(ASTANA_TZ)
            reminders = get_pending_reminders()
            for rem in reminders:
                try:
                    rem_time = parse_flexible_datetime(rem.get("remind_at"))
                    if rem_time and now >= rem_time:
                        target_user = str(rem.get("target_user", "")) or "Семья"
                        text = str(rem.get("text", ""))
                        recurrence = str(rem.get("recurrence", "once"))
                        remind_at = str(rem.get("remind_at", ""))
                        row_idx = rem.get("row_idx", 0)
                        try:
                            await safe_send_message(
                                bot, chat_id=FAMILY_CHAT_ID,
                                text=f"⏰ Напоминание для {target_user}: {text}"
                            )
                        except Exception as send_err:
                            print(f"[Напоминания] Не удалось отправить: {send_err}")
                        mark_reminder_done(row_idx, recurrence, remind_at)
                except Exception as e:
                    print(f"[Напоминания] Ошибка обработки: {e}")
        except Exception as e:
            print(f"[Напоминания] Ошибка цикла: {e}")
        await asyncio.sleep(60)


async def check_subscriptions():
    while True:
        try:
            now = datetime.datetime.now(ASTANA_TZ)
            if now.minute == 5:
                due = process_due_subscriptions(now)
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
                if sent_morning_today != today_str:
                    forecast = await get_weather_forecast(target="today")
                    if forecast:
                        await safe_send_message(bot, chat_id=FAMILY_CHAT_ID, text=forecast)
                        sent_morning_today = today_str

            # 22:30 — вечерний прогноз на завтра
            if (now.hour == 22 and now.minute >= 30) or (now.hour == 23 and now.minute == 0):
                if sent_evening_today != today_str:
                    forecast = await get_tomorrow_forecast()
                    if forecast:
                        await safe_send_message(bot, chat_id=FAMILY_CHAT_ID, text=forecast)
                        sent_evening_today = today_str

        except Exception as e:
            print(f"[Погода-Шедулер] Ошибка: {e}")

        await asyncio.sleep(30)


async def sweep_pending_receipts():
    while True:
        try:
            expired = sweep_expired()
            for chat_id, transactions, user_name in expired:
                for tx in transactions:
                    tx["user"] = user_name
                    tx["user_comment"] = tx.get("user_comment", "") or "(без комментария — авто-сохранение)"
                    append_transaction(tx)
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
            for chat_id, tx in expired:
                if not tx.get("category"):
                    tx["category"] = FALLBACK_EXPENSE_CATEGORY
                comm = tx.get("user_comment", "")
                tx["user_comment"] = f"{comm} (автосохранение: {tx['category']})".strip()

                append_transaction(tx)
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


async def main():
    await asyncio.to_thread(ensure_power_bi_dimension_table)

    asyncio.create_task(check_reminders())
    asyncio.create_task(check_subscriptions())
    asyncio.create_task(weather_scheduler())
    asyncio.create_task(sweep_pending_receipts())
    asyncio.create_task(sweep_clarifications())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

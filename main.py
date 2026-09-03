"""Family Finance Bot — Ада."""

import asyncio
import datetime
import os

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command

from config import TELEGRAM_BOT_TOKEN, FAMILY_CHAT_ID
from handlers.text_handler import handle_text, handle_voice, handle_category_clarification_callback
from handlers.media_handler import handle_media
from services.sheets import (
    get_pending_reminders, mark_reminder_done, get_active_subscriptions,
    add_or_update_subscription, get_installments, get_transactions_for_period,
    append_transaction, get_db,
)
from services.telegram_safe import safe_answer, safe_send_message
from services.pending_receipts import sweep_expired
from services.pending_clarifications import sweep_expired_clarifications
from services.timezone import ASTANA_TZ

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
        "• Лимиты бюджета по категориям\n"
        "• Напоминания (разовые и ежедневные)\n"
        "• Список покупок\n"
        "• Рассрочки и Kaspi Red\n"
        "• Подписки и регулярные платежи\n"
        "• Планирование поездок\n"
        "• Прогноз погоды\n"
        "• Графики расходов (/chart)\n"
        "• Голосовые сообщения\n\n"
        "Просто напиши мне о покупке или доходе — я всё запишу!"
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await safe_answer(
        message,
        "📋 Команды:\n"
        "/start — начать работу\n"
        "/chart — график расходов за месяц\n"
        "/debug — диагностика таблицы\n\n"
        "💡 Примеры сообщений:\n"
        "• 'Купил колу за 500 тг'\n"
        "• 'Зарплата 300000'\n"
        "• 'Напомни мне в 21:00 выпить витамины'\n"
        "• 'Добавь в список: молоко, хлеб'\n"
        "• 'Покажи лимиты'\n"
        "• 'Какая погода?'"
    )


@dp.message(Command("chart"))
async def cmd_chart(message: types.Message):
    from services.charts import generate_expense_chart
    import asyncio
    try:
        image_bytes = await asyncio.to_thread(generate_expense_chart)
        if image_bytes:
            await message.answer_photo(photo=image_bytes, caption="📊 Вот твой финансовый дашборд.")
        else:
            await safe_answer(message, "Нет данных для построения графика.")
    except Exception as error:
        print(f"[График] Ошибка: {error}")
        await safe_answer(message, "Не удалось построить график.")


@dp.message(Command("debug"))
async def cmd_debug(message: types.Message):
    from services.sheets import debug_transactions_snapshot
    debug_text = debug_transactions_snapshot()
    await safe_answer(message, f"```\n{debug_text}\n```")


# ═══════════════════════════════════════════════════════════════════════════════
# INLINE KEYBOARD CALLBACK — уточнение категории
# ═══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("clarify_cat:"))
async def process_category_clarification(callback: types.CallbackQuery):
    await handle_category_clarification_callback(callback)


# ═══════════════════════════════════════════════════════════════════════════════
# ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ
# ═══════════════════════════════════════════════════════════════════════════════

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
    """Проверка и отправка напоминаний каждую минуту."""
    while True:
        try:
            now = datetime.datetime.now(ASTANA_TZ)
            reminders = get_pending_reminders()
            for rem in reminders:
                try:
                    rem_time = datetime.datetime.strptime(
                        str(rem.get("remind_at")), "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=ASTANA_TZ)
                    if now >= rem_time:
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
    """Проверка подписок — автоматическое списание в начале месяца."""
    while True:
        try:
            now = datetime.datetime.now(ASTANA_TZ)
            if now.hour == 0 and now.minute == 5:
                subs = get_active_subscriptions()
                for sub in subs:
                    try:
                        day = int(sub.get("day_of_month", 1))
                        if now.day == day:
                            append_transaction({
                                "type": "РАСХОД",
                                "amount": float(sub.get("amount", 0)),
                                "currency": "KZT",
                                "bank": str(sub.get("bank", "Не указан")),
                                "source": "Основная карта",
                                "funds_type": "Собственные",
                                "resource": "Карта",
                                "category": "Связь и подписки",
                                "subcategory": "Цифровые подписки",
                                "merchant": str(sub.get("name", "")),
                                "necessity": "Want",
                                "user_comment": f"Автосписание: {sub.get('name')}",
                                "ai_comment": f"Ежемесячное списание: {sub.get('name')}",
                            })
                            add_or_update_subscription(
                                str(sub.get("name")),
                                float(sub.get("amount", 0)),
                                str(sub.get("bank", "Не указан")),
                                day,
                            )
                    except Exception as e:
                        print(f"[Подписки] Ошибка обработки {sub}: {e}")
        except Exception as e:
            print(f"[Подписки] Ошибка цикла: {e}")
        await asyncio.sleep(60)


async def sweep_pending_receipts():
    """Очистка просроченных чеков каждую минуту."""
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
    """Очистка просроченных уточнений категории."""
    while True:
        try:
            expired = sweep_expired_clarifications()
            for chat_id, tx in expired:
                append_transaction(tx)
                try:
                    await safe_send_message(
                        bot, chat_id=chat_id,
                        text=f"⏰ Автосохранение: {_format_currency(tx.get('amount', 0))} тг → {tx.get('category', '...')} (не дождалась уточнения)"
                    )
                except Exception:
                    pass
        except Exception as e:
            print(f"[Clarifications] Ошибка: {e}")
        await asyncio.sleep(60)


async def main():
    asyncio.create_task(check_reminders())
    asyncio.create_task(check_subscriptions())
    asyncio.create_task(sweep_pending_receipts())
    asyncio.create_task(sweep_clarifications())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

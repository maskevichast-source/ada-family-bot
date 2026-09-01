import asyncio
import datetime
import json
import os
from collections import defaultdict
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
import config
from handlers import text_handler, media_handler
from handlers.media_handler import build_and_save_transactions
from services.sheets import (
    get_pending_reminders, mark_reminder_done, get_active_subscriptions,
    get_transactions_for_period, save_category_limits
)
from services.limits_ai import generate_ai_limits
from services.categories import TYPE_EXPENSE, TYPE_INCOME
from services.weather import get_weather_forecast, get_tomorrow_forecast
from services.timezone import ASTANA_TZ, parse_flexible_datetime
from services.pending_receipts import sweep_expired
from services.telegram_safe import safe_send_message

bot_token = getattr(config, 'BOT_TOKEN', getattr(config, 'TELEGRAM_BOT_TOKEN', getattr(config, 'BOT_API_KEY', None)))
# parse_mode=MARKDOWN — без этого весь **жирный текст** в сообщениях (а его много,
# от напоминаний до сводок) показывался бы в Telegram буквально со звёздочками.
# Именно legacy Markdown, а не MarkdownV2 — во всём коде даты/скобки/дефисы нигде
# не экранированы под более строгие правила V2.
bot = Bot(token=bot_token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()

dp.include_router(media_handler.router)
dp.include_router(text_handler.router)

SKIP_REMINDER_KEYWORDS = ["red", "kaspi red", "кредит", "рассрочка", "ипотека", "займ"]

# --- ВЕБ-СЕРВЕР ДЛЯ ПИНГА ---
async def handle_ping(request):
    return web.Response(text="Ада работает.")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/ping", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()

    raw_ports = [int(os.environ.get("PORT", 8080)), 5000, 8000, 3000]
    ports_to_try = list(dict.fromkeys(raw_ports))
    for port in ports_to_try:
        try:
            site = web.TCPSite(runner, "0.0.0.0", port, reuse_address=True)
            await site.start()
            print(f"🌐 Пинг-сервер запущен на порту {port}")
            break
        except OSError:
            continue

# --- ПОЛУЧАТЕЛИ ---
def get_recipients():
    """Личные ID пользователей — используются ТОЛЬКО как запасной вариант,
    если FAMILY_CHAT_ID не настроен (см. _broadcast_targets ниже)."""
    recipients = {}
    if hasattr(config, 'VLAD_TELEGRAM_ID') and config.VLAD_TELEGRAM_ID:
        recipients[config.VLAD_TELEGRAM_ID] = "Влад"
    if hasattr(config, 'DIANA_TELEGRAM_ID') and config.DIANA_TELEGRAM_ID:
        recipients[config.DIANA_TELEGRAM_ID] = "Диана"
    if not recipients and hasattr(config, 'AUTHORIZED_USERS'):
        recipients = config.AUTHORIZED_USERS
    return recipients

_warned_about_family_chat_id = False

def _broadcast_targets() -> list:
    """Куда слать проактивные сообщения (погода, напоминания, дайджесты, подписки).

    Правильное поведение для семейного бота — ОДНО сообщение в общий
    групповой чат, а не личка каждому по отдельности (это и была причина
    бага, когда напоминания и утренняя погода уходили в личку вместо
    группы: VLAD_TELEGRAM_ID/DIANA_TELEGRAM_ID — это ID личных чатов).

    Добавьте в config.py:  FAMILY_CHAT_ID = -1001234567890
    Узнать ID группы: напишите боту в группе "/chatid" — он ответит ID
    текущего чата (число обычно отрицательное для групп).

    Если FAMILY_CHAT_ID не задан, временно откатываемся на старое
    поведение (личка каждому), чтобы бот не замолчал полностью, и громко
    предупреждаем в консоли при каждом запуске цикла.
    """
    global _warned_about_family_chat_id
    family_chat_id = getattr(config, 'FAMILY_CHAT_ID', None)
    if family_chat_id:
        return [family_chat_id]
    if not _warned_about_family_chat_id:
        print(
            "[Настройка] FAMILY_CHAT_ID не задан в config.py — проактивные сообщения "
            "(погода, напоминания, дайджесты) уходят в личку каждому пользователю "
            "вместо общего чата. Напишите боту в группе \"/chatid\" и добавьте "
            "полученный ID как FAMILY_CHAT_ID в config.py."
        )
        _warned_about_family_chat_id = True
    return list(get_recipients().keys())

def _month_bounds(now: datetime.datetime) -> tuple[str, str]:
    start = now.replace(day=1)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

def _prev_month_bounds(now: datetime.datetime) -> tuple[str, str, datetime.datetime]:
    first_of_this_month = now.replace(day=1)
    last_day_prev_month = first_of_this_month - datetime.timedelta(days=1)
    start = last_day_prev_month.replace(day=1)
    return start.strftime("%Y-%m-%d"), first_of_this_month.strftime("%Y-%m-%d"), last_day_prev_month

def _split_income_expense(transactions: list[dict]) -> tuple[float, float, defaultdict, defaultdict]:
    """Вернуть (доходы, расходы) и разбивку по категориям и пользователям для расходов."""
    total_income = 0.0
    users_spend: defaultdict = defaultdict(float)
    cats_spend: defaultdict = defaultdict(float)
    total_expense = 0.0
    for t in transactions:
        try:
            amt = float(str(t.get("amt", 0)).replace(",", "."))
        except (TypeError, ValueError):
            amt = 0.0
        if str(t.get("type") or TYPE_EXPENSE) == TYPE_INCOME:
            total_income += amt
        else:
            total_expense += amt
            users_spend[t.get("user", "Влад")] += amt
            cats_spend[t.get("cat", "Прочее")] += amt
    return total_income, total_expense, users_spend, cats_spend

async def reminders_scheduler():
    last_sub_date = None
    last_digest_year_week = None
    last_limits_year_month = None
    last_weather_date = None
    last_tomorrow_weather_date = None

    while True:
        try:
            now = datetime.datetime.now(ASTANA_TZ)
            broadcast_targets = _broadcast_targets()

            # 0. Чеки, ожидавшие комментарий дольше PENDING_TTL_MINUTES —
            #    сохраняем как есть, без комментария, вместо того чтобы тихо терять.
            expired_receipts = await asyncio.to_thread(sweep_expired)
            for chat_id, transactions, user_name in expired_receipts:
                try:
                    body = await asyncio.to_thread(build_and_save_transactions, transactions, user_name)
                    await safe_send_message(bot, chat_id, f"{body}\n\n💬 Не дождалась комментария к чеку — записала как есть.")
                except Exception as error:
                    print(f"[Чеки] Не удалось сохранить просроченный чек: {error}")

            # 1. Утренний прогноз в 08:30 по времени Астаны
            current_date = now.date()
            if now.hour == 8 and now.minute >= 30 and last_weather_date != current_date:
                forecast_message = await get_weather_forecast()
                if forecast_message:
                    for target in broadcast_targets:
                        try:
                            await safe_send_message(bot, target, forecast_message)
                        except Exception as error:
                            print(f"[Погода] Не удалось отправить прогноз {target}: {error}")
                    last_weather_date = current_date

            # 1б. Вечерний прогноз НА ЗАВТРА в 22:30 — с разбивкой по всему дню
            if now.hour == 22 and now.minute >= 30 and last_tomorrow_weather_date != current_date:
                tomorrow_forecast = await get_tomorrow_forecast()
                if tomorrow_forecast:
                    for target in broadcast_targets:
                        try:
                            await safe_send_message(bot, target, tomorrow_forecast)
                        except Exception as error:
                            print(f"[Погода] Не удалось отправить прогноз на завтра {target}: {error}")
                    last_tomorrow_weather_date = current_date

            # 2. Проверка напоминаний в отдельном потоке
            pending = await asyncio.to_thread(get_pending_reminders)
            for rem in pending:
                try:
                    rem_time = parse_flexible_datetime(rem.get("remind_at"))
                    if rem_time is None:
                        print(f"[Напоминания] Не удалось разобрать время «{rem.get('remind_at')}» (id={rem.get('id', '?')}) — пропускаю.")
                        continue
                    if now >= rem_time:
                        target_name = str(rem.get('target_user', ''))
                        msg = f"🔔 **НАПОМИНАНИЕ ДЛЯ {target_name.upper()}!**\n\n📌 {rem['text']}"

                        for target in broadcast_targets:
                            try:
                                await safe_send_message(bot, target, msg)
                            except Exception as e:
                                print(f"[Напоминания] Не удалось отправить напоминание {target}: {e}")

                        await asyncio.to_thread(mark_reminder_done, rem["row_idx"], rem.get("recurrence", "once"), rem.get("remind_at", ""))
                except Exception as rem_err:
                    print(f"[Напоминания] Не удалось обработать время: {rem_err}")

            # 3. Утренний чек подписок (в 09:00)
            if now.hour == 9 and last_sub_date != current_date:
                last_sub_date = current_date
                subs = await asyncio.to_thread(get_active_subscriptions)
                for sub in subs:
                    sub_name = str(sub.get("name", "")).lower()
                    if any(kw in sub_name for kw in SKIP_REMINDER_KEYWORDS):
                        continue
                    sub_day = int(sub.get("day_of_month", 0))
                    if (sub_day - now.day) == 2:
                        msg = f"💳 **ПОДПИСКА:**\n\nЧерез 2 дня списание за **{sub['name']}** ({sub['amount']} ₸, {sub['bank']})."
                        for target in broadcast_targets:
                            try:
                                await safe_send_message(bot, target, msg)
                            except Exception:
                                pass

            # 4. Итоги месяца и авто-лимиты (1-го числа в 10:00)
            if now.day == 1 and now.hour == 10 and (now.year, now.month) != last_limits_year_month:
                last_limits_year_month = (now.year, now.month)
                start_date, end_date, prev_month_date = _prev_month_bounds(now)
                transactions = await asyncio.to_thread(get_transactions_for_period, start_date, end_date)

                total_income, total_prev_spent, users_prev_spend, cats_prev_spend = _split_income_expense(transactions)
                balance = total_income - total_prev_spent

                report_msg = (
                    f"📈 **ИТОГИ ПРОШЛОГО МЕСЯЦА ({prev_month_date.strftime('%B %Y').upper()}):**\n\n"
                    f"💰 Доходы: **{total_income:,.0f} ₸**\n"
                    f"💸 Всего сожрано бюджета: **{total_prev_spent:,.0f} ₸**\n"
                    f"{'📈' if balance >= 0 else '📉'} Баланс: **{balance:,.0f} ₸**\n\n"
                    f"👥 **Кто сколько потратил:**\n"
                )
                for u, amt in users_prev_spend.items():
                    report_msg += f"• {u}: {amt:,.0f} ₸\n"

                report_msg += f"\n🏆 **Топ категорий расходов:**\n"
                sorted_cats = sorted(cats_prev_spend.items(), key=lambda x: x[1], reverse=True)[:5]
                for c, amt in sorted_cats:
                    report_msg += f"• {c}: {amt:,.0f} ₸\n"

                # Для генерации лимитов даём ИИ более широкий контекст последних операций
                recent_start = (now - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
                recent_end = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                recent_transactions = await asyncio.to_thread(get_transactions_for_period, recent_start, recent_end)
                new_limits = await generate_ai_limits(recent_transactions)
                await asyncio.to_thread(save_category_limits, new_limits)

                limits_msg = f"\n📊 **БЮДЖЕТ НА НОВЫЙ МЕСЯЦ УТВЕРЖДЕН:**\n\n"
                for cat, val in new_limits.items():
                    limits_msg += f"• **{cat}**: {val:,.0f} ₸\n"

                full_broadcast = report_msg + "\n-------------------\n" + limits_msg + "\nТаблица обновлена. Держите себя в руках."

                for target in broadcast_targets:
                    try:
                        await safe_send_message(bot, target, full_broadcast)
                    except Exception:
                        pass

            # 5. Воскресный дайджест (в 22:00)
            iso_year, iso_week, _ = now.isocalendar()
            if now.weekday() == 6 and now.hour == 22 and (iso_year, iso_week) != last_digest_year_week:
                last_digest_year_week = (iso_year, iso_week)
                week_start = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
                week_end = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                transactions = await asyncio.to_thread(get_transactions_for_period, week_start, week_end)

                total_income, total_spent, users_spend, cats_spend = _split_income_expense(transactions)
                balance = total_income - total_spent

                digest_msg = (
                    f"📊 **ВОСКРЕСНЫЙ ФИНАНСОВЫЙ ДАЙДЖЕСТ**\n\n"
                    f"💰 Доходы за неделю: **{total_income:,.0f} ₸**\n"
                    f"💸 Потрачено за неделю: **{total_spent:,.0f} ₸**\n"
                    f"{'📈' if balance >= 0 else '📉'} Баланс: **{balance:,.0f} ₸**\n\n"
                    f"👥 **Потратили:**\n"
                )
                for u, amt in users_spend.items():
                    digest_msg += f"• {u}: {amt:,.0f} ₸\n"

                digest_msg += f"\n🏆 **Топ категорий:**\n"
                sorted_cats = sorted(cats_spend.items(), key=lambda x: x[1], reverse=True)[:3]
                for c, amt in sorted_cats:
                    digest_msg += f"• {c}: {amt:,.0f} ₸\n"

                digest_msg += f"\n💬 Итоги подбиты. Неделя закрыта."

                for target in broadcast_targets:
                    try:
                        await safe_send_message(bot, target, digest_msg)
                    except Exception:
                        pass

        except Exception as e:
            print(f"[Планировщик] Ошибка: {e}")

        await asyncio.sleep(60)

async def main():
    print("🚀 Ада запущена в автономном режиме 24/7...")
    await start_web_server()
    asyncio.create_task(reminders_scheduler())

    while True:
        try:
            print("📡 Подключение к Telegram...")
            await dp.start_polling(bot, polling_timeout=30, skip_updates=True)
        except (TelegramNetworkError, asyncio.TimeoutError) as net_err:
            print(f"⚠️ Ошибка сети Telegram: {net_err}. Переподключение через 5 сек...")
            await asyncio.sleep(5)
        except Exception as err:
            print(f"⚠️ Ошибка поллинга: {err}. Переподключение через 5 сек...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())

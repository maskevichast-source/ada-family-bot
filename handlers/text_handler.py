"""Обработка текстовых сообщений от пользователя (все интенты)."""

import asyncio
import datetime
import re

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram import types

from config import get_authorized_user_name
from services.categories import (
    TYPE_EXPENSE, TYPE_INCOME, is_income_type,
    EXPENSE_CATEGORIES, INCOME_CATEGORIES,
    FALLBACK_EXPENSE_CATEGORY, FALLBACK_INCOME_CATEGORY,
    normalize_category, normalize_subcategory,
    validate_transaction_category_subcategory,
    get_ambiguous_options,
)
from services.money import parse_amount
from services.banks import normalize_bank_source
from services.deepseek_service import parse_and_analyze
from services.sheets import (
    append_transaction, get_last_200_transactions, get_category_limits,
    add_shopping_items, get_shopping_items, mark_shopping_items_done,
    add_trip_plan, get_planned_trips,
    get_active_subscriptions, deactivate_subscription, add_or_update_subscription,
    add_installment, get_installments, close_installment,
    split_last_transaction_by_amount,
    get_transactions_for_period, find_recent_duplicate_transaction,
    debug_transactions_snapshot, normalize_necessity,
    update_last_transaction_bank_and_source,
)
from services.banks import canonical_bank_name
from services.charts import generate_expense_chart
from services.limits_ai import generate_limits_from_history
from services.weather import get_weather_forecast
from services.telegram_safe import safe_answer
from services.pending_receipts import has_pending, pop_pending, ack_pending
from services.pending_clarifications import (
    set_clarification, get_clarification, pop_clarification,
)
from services.memory import get_chat_history, add_chat_message
from services.voice import transcribe_voice
from services.timezone import now_astana, parse_ru_relative_datetime
from services.analytics import analyze_budget_leaks
from services.reports import generate_pdf_report, generate_excel_export
from services.price_tracker import fetch_product_info, detect_marketplace
from services import reminders, reminder_edit, debts, edits
from services.state import dialogue_key


def _to_number_or_blank(value):
    if value is None:
        return 0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0


def _format_currency(value):
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except (ValueError, TypeError):
        return str(value)


_WEATHER_KEYWORDS = [
    "погод", "прогноз", "зонт", "дожд", "снег", "гроза", "осадк",
    "температур", "градус", "холодн", "холодно", "тепло", "жарко", "жара",
    "мороз", "ветер", "ветрено", "туман", "гололед", "гололёд",
    "что надеть", "во что одеться", "куртк", "шапк", "как одеться",
]


_PURCHASE_HINT_RE = re.compile(
    r"\b(?:купил[аи]?|оплатил[аи]?|потратил[аи]?|заплатил[аи]?|взял[аи]?)\b.*\d|"
    r"\d+\s*(?:тг|тенге|kzt|₸)\b"
)


def _is_weather_question(t_clean: str) -> bool:
    if _PURCHASE_HINT_RE.search(t_clean):
        # "купил зонт за 500" — это покупка, а не вопрос про погоду, даже
        # если слово "зонт" совпадает с погодным ключевым словом.
        return False
    return any(k in t_clean for k in _WEATHER_KEYWORDS)


def _weather_target_from_text(t_clean: str) -> str:
    if "недел" in t_clean or "5 дней" in t_clean or "выходн" in t_clean:
        return "week"
    if "послезавтра" in t_clean:
        return "after_tomorrow"
    if "завтра" in t_clean:
        return "tomorrow"
    return "today"


_NOT_A_RECEIPT_COMMENT_RE = re.compile(
    r"^(?:как|что|когда|почему|зачем|где|кто|сколько)\b|\?\s*$|"
    r"\b(?:привет|здравствуй|спасибо|пока|как дела)\b",
    re.I,
)


def _looks_like_receipt_comment(text: str) -> bool:
    """Отличает подпись к чеку ("обед", "продукты") от случайного вопроса,
    болтовни ("Как дела?") или НОВОЙ покупки ("Такси 1000") — комментарий к
    чеку обычно короткое название без сумм, суммы сообщает сам чек."""
    t = text.strip()
    if not t:
        return False
    if re.search(r"\d", t):
        return False
    return not _NOT_A_RECEIPT_COMMENT_RE.search(t.lower())


def _extract_marketplace_url(text: str) -> str | None:
    urls = re.findall(r"https?://\S+", text or "")
    for url in urls:
        clean = url.rstrip(".,);]\n")
        if detect_marketplace(clean):
            return clean
    return None


def _format_confirmation_report(tx: dict, ai_comment: str = "") -> str:
    amt = _format_currency(tx.get("amount", 0))
    curr = tx.get("currency", "KZT")
    if tx.get("resource") == "Наличные":
        bank_source = "Наличные"
    else:
        bank_source = tx.get("source") or tx.get("bank", "Не указан")

    cat = tx.get("category", "")
    comm = str(tx.get("user_comment", "") or "").strip()
    comm_str = f" ({comm})" if comm else ""

    lines = [
        "✍️ **Записано:**",
        f"• {amt} {curr} | {bank_source} | {cat}{comm_str}",
    ]
    if ai_comment and not any(bad in ai_comment.lower() for bad in ["задумалась", "повтори", "на связи"]):
        lines.append(f"\n💬 {ai_comment}")
    return "\n".join(lines)


def _build_clarification_keyboard(options: list[dict], token: str) -> InlineKeyboardMarkup:
    buttons = []
    for idx, opt in enumerate(options):
        buttons.append([InlineKeyboardButton(text=opt["label"], callback_data=f"clarify_opt:{token}:{idx}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _should_ask_clarification(text: str, parsed: dict, options: list[dict] | None) -> bool:
    """Не плодим кнопки, если категория очевидна; спрашиваем только при сомнении."""
    if not options:
        return False

    low = text.lower().replace("ё", "е")
    no_button_markers = [
        "на работу", "на работе", "в офис", "с собой", "домой", "для дома",
        "продукты домой", "такси", "налог", "штраф", "коммунал", "аренда",
        "интернет", "проезд", "автобус", "аптека", "лекарств",
    ]
    if any(m in low for m in no_button_markers):
        return False

    confidence = parsed.get("confidence")
    try:
        if confidence is not None and float(confidence) >= 0.82:
            return False
    except (TypeError, ValueError):
        pass

    return True


async def _try_handle_reminder_directly(message: Message, text: str, user_name: str, chat_id: int) -> bool:
    """Локальный перехват напоминаний: создание/список/удаление без похода к ИИ.

    Раньше здесь была своя урезанная копия этой логики (не поддерживала ни
    редактирование, ни weekly, ни надёжную доставку в семейный чат по ID).
    Теперь используется единый services/reminders.py + reminder_edit.py —
    та же таблица Reminders, только без дублей и с более умным разбором.
    """
    if await reminder_edit.handle(message, text):
        return True
    return await reminders.handle(message, text, user_name)


async def handle_category_clarification_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    data = callback.data

    if not data.startswith("clarify_opt:"):
        return

    parts = data.split(":")
    if len(parts) != 3:
        await callback.answer("Уточнение устарело. Запиши заново.", show_alert=True)
        return
    _, token, idx_str = parts
    idx = int(idx_str)

    key = dialogue_key(chat_id, callback.from_user.id)
    clarification = get_clarification(key)

    if not clarification or clarification.get("token") != token:
        await callback.answer("Уточнение устарело. Запиши заново.", show_alert=True)
        return
    pop_clarification(key)

    if not clarification:
        await callback.answer("Уточнение устарело. Запиши заново.", show_alert=True)
        return

    options = clarification.get("options", [])
    if idx >= len(options):
        await callback.answer("Ошибка выбора.")
        return

    chosen = options[idx]
    tx = clarification["transaction"]
    tx["category"] = chosen["category"]
    tx["subcategory"] = chosen.get("subcategory", "")
    tx["necessity"] = chosen.get("necessity") or normalize_necessity(tx.get("necessity"), tx["category"])

    await asyncio.to_thread(append_transaction, tx)
    report = _format_confirmation_report(tx, f"Категория выбрана: {chosen['label']}.")
    add_chat_message(chat_id, "Ада", report)

    try:
        await callback.message.edit_text(report)
    except Exception:
        try:
            await callback.message.edit_text(report, parse_mode=None)
        except Exception:
            pass

    await callback.answer("Записано!")


async def handle_text(message: Message):
    await _process_text_message(message, message.text or "")


async def handle_voice(message: Message):
    voice = message.voice or message.audio
    if not voice:
        return
    try:
        file = await message.bot.get_file(voice.file_id)
        downloaded = await message.bot.download_file(file.file_path)
        file_bytes = downloaded.read()
    except Exception as e:
        await safe_answer(message, "Не удалось скачать голосовое сообщение.")
        return

    ext = "oga"
    if file.file_path and "." in file.file_path:
        ext = file.file_path.rsplit(".", 1)[-1]

    text = await transcribe_voice(file_bytes, filename=f"voice.{ext}")
    if not text:
        await safe_answer(message, "Не удалось распознать голосовое сообщение.")
        return

    await safe_answer(message, f"🎤 «{text}»")
    await _process_text_message(message, text)


async def _process_text_message(message: Message, text: str):
    user_name = get_authorized_user_name(message.from_user.id, message.from_user.first_name) or message.from_user.first_name or "Пользователь"
    chat_id = message.chat.id
    add_chat_message(chat_id, user_name, text)
    t_clean = text.strip().lower()

    # Явные команды (напоминания/долги/подтверждение правки) проверяются
    # РАНЬШЕ, чем текст решат считать комментарием к ожидающему чеку —
    # иначе "напомни завтра..." под висящим чеком тихо съедалось бы как
    # подпись к чеку вместо создания напоминания.
    if await edits.handle_confirmation(message, text):
        return
    if await debts.handle(message, text, user_name):
        return
    if await _try_handle_reminder_directly(message, text, user_name, chat_id):
        return

    # 1. Чек ожидает комментария
    pending_key = dialogue_key(chat_id, message.from_user.id)
    if has_pending(pending_key) and _looks_like_receipt_comment(text):
        pending = pop_pending(pending_key)
        if pending:
            transactions, receipt_user = pending
            lines = ["📸 **Записано по чеку:**"]
            try:
                for tx in transactions:
                    original_item = str(tx.get("user_comment", "") or "").strip()
                    if original_item and original_item != text:
                        tx["user_comment"] = f"{original_item} ({text})"
                    else:
                        tx["user_comment"] = text
                    tx["user"] = receipt_user
                    await asyncio.to_thread(append_transaction, tx)
                    lines.append(
                        f"• {_format_currency(tx.get('amount'))} {tx.get('currency')} | "
                        f"{tx.get('bank')} | {tx.get('category')} ({tx['user_comment']})"
                    )
            except Exception as e:
                # Запись не подтверждена — НЕ снимаем pending, чек не потерян,
                # его можно будет дозаписать (ack_pending ниже не вызывается).
                print(f"[Чек] Ошибка записи: {e}")
                res = "Не смогла сохранить часть чека в таблицу. Ничего не потеряно — напиши ещё раз тот же комментарий, я попробую снова."
                add_chat_message(chat_id, "Ада", res)
                await safe_answer(message, res)
                return
            ack_pending(pending_key)
            rep = "\n".join(lines)
            add_chat_message(chat_id, "Ада", rep)
            await safe_answer(message, rep)
            return

    # 2. Перехват ответа на уточнение
    clarification_key = dialogue_key(chat_id, message.from_user.id)
    clarification = get_clarification(clarification_key)
    if clarification:
        matched_opt = None
        for opt in clarification.get("options", []):
            words = re.findall(r'\w+', opt["label"].lower())
            if any(w in t_clean for w in words if len(w) > 3):
                matched_opt = opt
                break
        # Раньше здесь был ещё один, гораздо более широкий, запасной
        # вариант сопоставления по одиночным словам вроде "работ"/"дом" —
        # он слишком часто ловил не то (например, "сегодня работа
        # утомила" ошибочно засчитывалось как ответ на уточнение про
        # категорию покупки). Полагаемся только на совпадение по словам
        # из самих предложенных вариантов.

        if matched_opt:
            pop_clarification(clarification_key)
            tx = clarification["transaction"]
            tx["category"] = matched_opt["category"]
            tx["subcategory"] = matched_opt.get("subcategory", "")
            tx["user_comment"] = f"{tx.get('user_comment', '')} ({text})".strip()
            tx["necessity"] = matched_opt.get("necessity") or normalize_necessity(tx.get("necessity"), tx["category"])
            await asyncio.to_thread(append_transaction, tx)
            report = _format_confirmation_report(tx, f"Поняла, это {matched_opt['label']}!")
            add_chat_message(chat_id, "Ада", report)
            await safe_answer(message, report)
            return

    # ── ПРЯМОЙ ПЕРЕХВАТ 0: МГНОВЕННАЯ СМЕНА БАНКА ПОСЛЕДНЕЙ ТРАТЫ ──
    # Префикс с глаголом правки был необязательным (`(?:...)?`), поэтому
    # обычная НОВАЯ покупка вида "Оплатил такси 1200 с карты Kaspi" тоже
    # попадала сюда и трактовалась как команда "поменяй банк последней
    # записи" — новая трата вообще не записывалась. Теперь глагол правки
    # обязателен, и просто упоминание "с карты X" внутри описания покупки
    # больше не путается с командой редактирования.
    bank_match = re.search(r'(?:измени|поменяй|запиши|исправь)\s*(?:что\s+это\s+)?(?:оплата\s+через|банк\s+на|карту\s+на|с\s+карты|банк)\s+([a-zA-Zа-яА-Я]+)', t_clean)
    if bank_match:
        target_bank = canonical_bank_name(bank_match.group(1))
        if target_bank:
            updated_rec = await asyncio.to_thread(update_last_transaction_bank_and_source, target_bank)
            if updated_rec:
                res = f"✅ Исправила в последней записи ({updated_rec.get('user_comment')}, {_format_currency(updated_rec.get('amount'))} KZT): банк изменён на **{updated_rec.get('bank')}** ({updated_rec.get('source')})."
            else:
                res = "Не нашла последнюю запись в таблице для изменения банка."
            add_chat_message(chat_id, "Ада", res)
            await safe_answer(message, res)
            return

    # ── ПРЯМОЙ ПЕРЕХВАТ 1: ДЕТЕКТОР УТЕЧЕК БЮДЖЕТА ──
    if t_clean in {"/leaks", "утечки"} or any(k in t_clean for k in ["утечки бюджета", "микротраты", "куда уходят деньги", "куда утекают деньги", "мелкие траты", "на что уходит мелочь"]):
        leak_data = await asyncio.to_thread(analyze_budget_leaks)
        add_chat_message(chat_id, "Ада", leak_data["text"])
        await safe_answer(message, leak_data["text"])
        return

    # ── ПРЯМОЙ ПЕРЕХВАТ 2: ГЕНЕРАЦИЯ PDF-ОТЧЁТА ──
    if t_clean in {"/report", "pdf"} or any(k in t_clean for k in ["отчет в pdf", "отчёт в pdf", "выгрузи отчет в pdf", "скачать pdf"]):
        try:
            await message.bot.send_chat_action(chat_id=chat_id, action="upload_document")
            pdf_bytes = await asyncio.to_thread(generate_pdf_report)
            pdf_file = BufferedInputFile(pdf_bytes, filename=f"Finance_Report_{now_astana().strftime('%Y_%m')}.pdf")
            await message.answer_document(document=pdf_file, caption="📑 Ваш официальный семейный финансовый отчёт за месяц.")
            return
        except Exception as e:
            print(f"[PDF Report] Ошибка: {e}")
            await safe_answer(message, "Не удалось сформировать PDF-отчёт.")
            return

    # ── ПРЯМОЙ ПЕРЕХВАТ 3: ЭКСПОРТ В EXCEL (.XLSX) ──
    if t_clean in {"/export", "excel"} or any(k in t_clean for k in ["выгрузи в excel", "экспорт в excel", "скачать выписку", "выгрузи выписку", "скачать excel"]):
        try:
            await message.bot.send_chat_action(chat_id=chat_id, action="upload_document")
            excel_bytes = await asyncio.to_thread(generate_excel_export)
            excel_file = BufferedInputFile(excel_bytes, filename=f"Family_Finance_{now_astana().strftime('%Y_%m')}.xlsx")
            await message.answer_document(document=excel_file, caption="📊 Ваша полная выписка в формате Excel со всеми операциями.")
            return
        except Exception as e:
            print(f"[Excel Export] Ошибка: {e}")
            await safe_answer(message, "Не удалось сформировать файл Excel.")
            return

    # ── ПРЯМОЙ ПЕРЕХВАТ 4: ГРАФИКИ ──
    if any(k in t_clean for k in ["график", "диаграмм", "чарт", "дашборд"]) or t_clean in {"/chart", "chart"}:
        try:
            image_bytes = await asyncio.to_thread(generate_expense_chart)
            if image_bytes:
                photo_file = BufferedInputFile(image_bytes, filename="chart.png")
                await message.answer_photo(photo=photo_file, caption="📊 Финансовый дашборд за текущий месяц.")
            else:
                await safe_answer(message, "Нет данных для построения графика.")
        except Exception as error:
            print(f"[График] Ошибка: {error}")
            await safe_answer(message, "Не удалось построить график.")
        return

    # ── ПРЯМОЙ ПЕРЕХВАТ 5: ПОГОДА ──
    # Раньше ловились только буквальные "погода/прогноз/зонт" — вопросы вроде
    # "холодно завтра?", "дождь будет?", "куртку брать?" пролетали мимо этого
    # перехвата, уходили в ИИ с корректным intent="get_weather", а дальше по
    # коду этот intent вообще не обрабатывался и терялся. Список расширен +
    # добавлена обработка intent="get_weather" ниже, после запроса к ИИ.
    if _is_weather_question(t_clean):
        target = _weather_target_from_text(t_clean)
        forecast = await get_weather_forecast(target=target)
        res = forecast or "Не удалось связаться с погодной станцией Open-Meteo."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # ── ПРЯМОЙ ПЕРЕХВАТ 6: ДИАГНОСТИКА ──
    if t_clean in {"debug", "/debug"}:
        debug_text = await asyncio.to_thread(debug_transactions_snapshot)
        await safe_answer(message, f"```\n{debug_text}\n```")
        return

    # ── ПРЯМОЙ ПЕРЕХВАТ 7: РАЗДЕЛИТЬ ТРАНЗАКЦИЮ ──
    if "раздели" in t_clean and "транзакцию" in t_clean:
        match = re.search(r"раздели\s+транзакцию\s+(\d+)[\s:]*(.+?)[\s]*\|\s*(.+?)", text, re.IGNORECASE)
        if match:
            target_amount = float(match.group(1))
            part1_desc = match.group(2).strip()
            part2_desc = match.group(3).strip()
            p1_match = re.match(r"(.+?)\s*[-—]\s*(\d+)", part1_desc)
            p2_match = re.match(r"(.+?)\s*[-—]\s*(\d+)", part2_desc)
            if p1_match and p2_match:
                part1_cat = normalize_category(p1_match.group(1).strip(), EXPENSE_CATEGORIES, FALLBACK_EXPENSE_CATEGORY)
                part1_amt = float(p1_match.group(2))
                part2_cat = normalize_category(p2_match.group(1).strip(), EXPENSE_CATEGORIES, FALLBACK_EXPENSE_CATEGORY)
                part2_amt = float(p2_match.group(2))
                success = await asyncio.to_thread(
                    split_last_transaction_by_amount,
                    target_amount, part1_amt, part1_cat, part1_desc,
                    part2_amt, part2_cat, part2_desc
                )
                res = f"Разделила: {part1_cat} — {part1_amt} тг, {part2_cat} — {part2_amt} тг." if success else "Не нашла такую транзакцию."
                add_chat_message(chat_id, "Ада", res)
                await safe_answer(message, res)
                return
        await safe_answer(message, "Формат: раздели транзакцию <сумма>: <категория> — <сумма> | <категория> — <сумма>")
        return

    # ── ПРЯМОЙ ПЕРЕХВАТ 8: ЦЕНЫ ПО ССЫЛКЕ ──
    product_url = _extract_marketplace_url(text)
    if product_url:
        try:
            await message.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        info = await fetch_product_info(product_url)
        if info:
            stock = "в наличии" if info.get("in_stock") else "нет в наличии"
            price = _format_currency(info.get("price", 0)) if info.get("price") else "цена не найдена"
            res = (
                f"🛒 **{info.get('marketplace')}**\n"
                f"{info.get('title')}\n"
                f"Цена: {price} тг\n"
                f"Статус: {stock}\n"
                f"{info.get('url')}"
            )
        else:
            res = "Не смогла разобрать ссылку на товар."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    try:
        await message.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:
        pass

    # ── ЗАПРОС К ИИ ──
    (
        history,
        limits,
        reminders_data,
        shopping_list,
        trips,
        subscriptions,
        installments,
    ) = await asyncio.gather(
        asyncio.to_thread(get_last_200_transactions),
        asyncio.to_thread(get_category_limits),
        asyncio.to_thread(reminders.pending),
        asyncio.to_thread(get_shopping_items),
        asyncio.to_thread(get_planned_trips),
        asyncio.to_thread(get_active_subscriptions),
        asyncio.to_thread(get_installments),
    )
    chat_history = get_chat_history(chat_id)

    pending_receipt_kwarg = {}
    pending_now = pop_pending(pending_key)
    if pending_now:
        txs, receipt_user = pending_now
        pending_receipt_kwarg["pending_receipt"] = {"transactions": txs, "user_name": receipt_user}

    parsed = await parse_and_analyze(
        user_text=text, user_name=user_name,
        history=history, chat_history=chat_history,
        shopping_list=shopping_list, limits=limits,
        reminders=reminders_data, trips=trips,
        subscriptions=subscriptions, installments=installments,
        **pending_receipt_kwarg,
    )

    intent = parsed.get("intent", "chat")
    reply = parsed.get("reply", "")

    # Блокировка кнопок при командах удаления
    is_delete_or_edit_command = any(k in t_clean for k in ["удали", "удалить", "поменяй", "измени", "исправь", "замени", "отмени"])
    # Намерения, для которых ниже есть свой собственный обработчик — слово
    # вроде "отмени" в "отмени подписку" не должно уводить их в общий
    # edits.propose() для транзакций (реальный баг: "отмени подписку
    # YouTube" никогда не отменял подписку, потому что перехватывался этой
    # эвристикой раньше, чем доходил до своего блока).
    _INTENTS_WITH_OWN_HANDLER = {
        "add_installment", "close_installment", "get_installments",
        "add_subscription", "cancel_subscription", "get_subscriptions",
        "add_reminder", "delete_reminder", "get_reminders", "update_reminder",
        "debt", "get_debts",
        "add_shopping", "clear_shopping", "get_shopping",
        "add_trip", "get_trips",
        "get_limits", "generate_limits", "get_income", "get_weather",
        "split_transaction",
    }
    if intent in _INTENTS_WITH_OWN_HANDLER:
        is_delete_or_edit_command = False

    ambig_options = parsed.get("clarification_options") or get_ambiguous_options(text)
    if (
        not is_delete_or_edit_command
        and (intent in {"need_clarification", "transaction"} or parse_amount(text) > 0)
        and _should_ask_clarification(text, parsed, ambig_options)
    ):
        tx = parsed.get("transaction") or {}
        if not tx.get("amount"):
            tx["amount"] = parse_amount(text)
        if not tx.get("currency"):
            tx["currency"] = "KZT"
        if not tx.get("type"):
            tx["type"] = TYPE_EXPENSE
        if "нал" in text.lower():
            tx["resource"] = "Наличные"
            tx["bank"] = "Не указан"
            tx["source"] = "Основная карта"
        else:
            tx["source"] = normalize_bank_source(tx.get("bank"), tx.get("source"))
        tx["user"] = user_name
        tx["user_comment"] = text

        amt_str = _format_currency(tx.get("amount", 0))
        curr = tx.get("currency", "KZT")
        res_label = ", наличные" if tx.get("resource") == "Наличные" else ""

        if not reply or any(bad in reply.lower() for bad in ["задумалась", "повтори", "на связи", "ошибка"]):
            prompt_text = f"Куда запишем эту покупку ({amt_str} {curr}{res_label})?"
        else:
            prompt_text = reply

        token = set_clarification(clarification_key, tx, ambig_options)
        kb = _build_clarification_keyboard(ambig_options, token)
        add_chat_message(chat_id, "Ада", prompt_text)
        await message.answer(prompt_text, reply_markup=kb)
        return

    # Если ИИ/локальные правила пометили как need_clarification, но код решил,
    # что ситуация очевидна и кнопки не нужны — записываем как обычную транзакцию.
    if intent == "need_clarification" and (parsed.get("transaction") or {}).get("amount"):
        intent = "transaction"

    # Правка уже существующего напоминания (перенос времени/даты) — проверяем
    # раньше общего блока удаления/правки ниже, чтобы "перенеси напоминание
    # на завтра" не улетело в edits.py как правка обычной записи.
    if await reminder_edit.from_model(message, parsed, user_name, text):
        return

    # Долги через ИИ (если локальный разбор в debts.handle() не справился —
    # например, из-за местоимений вроде "он мне вернул").
    if await debts.handle_model(message, parsed, user_name):
        return

    # ── УДАЛЕНИЕ И ПРАВКА ЗАПИСЕЙ ──
    # Раньше правка/удаление выполнялись сразу же, без подтверждения — одна
    # ошибка распознавания ИИ могла стереть не ту транзакцию. Теперь сначала
    # показываем, что именно нашли и что собираемся сделать, и ждём "да"/"нет"
    # (services/edits.py); подтверждение перехватывается в начале обработчика.
    if intent in {"delete_transaction", "correct_any_record"} or (
        is_delete_or_edit_command
        and intent not in {"add_reminder", "delete_reminder", "get_reminders", "update_reminder", "debt", "get_debts"}
    ):
        try:
            await edits.propose(message, parsed)
        except ValueError as error:
            res = str(error)
            add_chat_message(chat_id, "Ада", res)
            await safe_answer(message, res)
        return

    # ── СВОДКА И БАЛАНС ЗА МЕСЯЦ ──
    if intent == "get_summary":
        now = now_astana()
        start = now.replace(day=1).strftime("%Y-%m-%d")
        if now.month == 12:
            end = now.replace(year=now.year + 1, month=1, day=1).strftime("%Y-%m-%d")
        else:
            end = now.replace(month=now.month + 1, day=1).strftime("%Y-%m-%d")

        transactions = await asyncio.to_thread(get_transactions_for_period, start, end)
        total_income = 0.0
        total_expense = 0.0
        by_category = {}
        for t in transactions:
            amt = _to_number_or_blank(t.get("amt"))
            t_type = str(t.get("type") or TYPE_EXPENSE)
            if t_type == TYPE_INCOME:
                total_income += amt
            else:
                total_expense += amt
                cat = str(t.get("cat") or "Прочее")
                by_category[cat] = by_category.get(cat, 0) + amt

        lines = [f"📊 **Сводка за {now.strftime('%B %Y')}:**"]
        lines.append(f"💰 Доходы: {_format_currency(total_income)} тг")
        lines.append(f"💸 Расходы: {_format_currency(total_expense)} тг")
        lines.append(f"📈 Баланс: {_format_currency(total_income - total_expense)} тг\n")
        lines.append("📉 **По категориям:**")
        if by_category:
            for cat, amt in sorted(by_category.items(), key=lambda x: -x[1]):
                lines.append(f"  - {cat}: {_format_currency(amt)} тг")
        else:
            lines.append("  _(В этом месяце расходов ещё не записано)_")

        res = "\n".join(lines)
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # ТРАНЗАКЦИЯ (ОДНОЗНАЧНАЯ)
    if intent == "transaction":
        tx = parsed.get("transaction", {})
        if not tx:
            await safe_answer(message, reply or "Не поняла, что записывать.")
            return

        tx_type = TYPE_INCOME if is_income_type(tx.get("type")) else TYPE_EXPENSE
        tx["type"] = tx_type
        valid_categories = INCOME_CATEGORIES if tx_type == TYPE_INCOME else EXPENSE_CATEGORIES
        fallback_cat = FALLBACK_INCOME_CATEGORY if tx_type == TYPE_INCOME else FALLBACK_EXPENSE_CATEGORY

        cat = normalize_category(tx.get("category"), valid_categories, fallback_cat)
        tx["category"] = cat
        _, valid_sub = validate_transaction_category_subcategory(cat, tx.get("subcategory"), valid_categories, fallback_cat)
        tx["subcategory"] = valid_sub or normalize_subcategory(None, cat, "")
        tx["necessity"] = normalize_necessity(tx.get("necessity"), cat)
        tx["source"] = normalize_bank_source(tx.get("bank"), tx.get("source"))

        if not tx.get("bank"): tx["bank"] = "Не указан"
        if not tx.get("currency"): tx["currency"] = "KZT"
        if not tx.get("funds_type"): tx["funds_type"] = "Собственные"
        if not tx.get("resource"): tx["resource"] = "Карта"
        tx["user"] = user_name
        if not tx.get("merchant"): tx["merchant"] = ""
        if not tx.get("user_comment"): tx["user_comment"] = text
        if not tx.get("ai_comment"): tx["ai_comment"] = reply or ""

        amount = parse_amount(tx.get("amount", 0))
        duplicate = await asyncio.to_thread(find_recent_duplicate_transaction, amount, text)
        if duplicate:
            dup_warning = f"⚠️ _Записала, но похоже на недавний дубль ({_format_currency(amount)} тг в {str(duplicate.get('date', ''))[-8:]})._"
            reply = f"{reply}\n\n{dup_warning}" if reply else dup_warning

        try:
            await asyncio.to_thread(append_transaction, tx)
        except Exception:
            await safe_answer(message, "Не смогла записать трату в таблицу. Не буду делать вид, что записала — проверь Google Sheets.")
            return
        report = _format_confirmation_report(tx, reply)
        add_chat_message(chat_id, "Ада", report)
        await safe_answer(message, report)
        return

    # РАССРОЧКИ
    if intent == "add_installment":
        data = parsed.get("installment", {})
        if data:
            await asyncio.to_thread(add_installment, data)
        res = reply or "Записала рассрочку."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "close_installment":
        query = parsed.get("search_query", "")
        if query:
            await asyncio.to_thread(close_installment, query)
        res = reply or "Закрыла рассрочку."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "split_transaction":
        split = parsed.get("split") or {}
        parts = split.get("parts") or []
        target_amount = split.get("amount")
        if len(parts) == 2 and target_amount is not None:
            success = await asyncio.to_thread(
                split_last_transaction_by_amount,
                target_amount,
                parts[0].get("amount"), parts[0].get("category", ""), parts[0].get("comment", ""),
                parts[1].get("amount"), parts[1].get("category", ""), parts[1].get("comment", ""),
            )
            res = (
                f"Разделила: {parts[0].get('category')} — {parts[0].get('amount')} тг, "
                f"{parts[1].get('category')} — {parts[1].get('amount')} тг."
            ) if success else "Не нашла такую транзакцию или суммы не сходятся."
        else:
            res = reply or "Уточни, на какие две части разделить и какие суммы."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "get_installments":
        items = await asyncio.to_thread(get_installments)
        if items:
            lines = ["📋 **Активные рассрочки:**"]
            for item in items:
                lines.append(
                    f"- {item.get('description')} ({item.get('bank')}): "
                    f"{_format_currency(item.get('total_amount'))} тг, "
                    f"{_format_currency(item.get('monthly_payment'))} тг/мес"
                )
            res = "\n".join(lines)
        else:
            res = "Нет активных рассрочек."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # ПОДПИСКИ
    if intent == "add_subscription":
        sub = parsed.get("subscription") or {}
        name = sub.get("name") or parsed.get("subscription_name", "")
        amount = parse_amount(sub.get("amount", parsed.get("amount", 0)))
        bank = sub.get("bank") or parsed.get("bank", "Не указан")
        day = int(parse_amount(sub.get("day_of_month", parsed.get("day_of_month", now_astana().day))) or now_astana().day)
        if name and amount > 0:
            try:
                saved = await asyncio.to_thread(add_or_update_subscription, name, amount, bank, max(1, min(day, 31)))
                res = reply or f"Записала подписку {saved.get('name', name)}: {_format_currency(amount)} тг/мес, день списания — {max(1, min(day, 31))}."
            except Exception as e:
                print(f"[Подписки] Ошибка сохранения: {e}")
                res = "Не смогла сохранить подписку в таблицу. Проверь Google Sheets."
        else:
            res = reply or "Не хватает названия или суммы подписки."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "cancel_subscription":
        name = parsed.get("subscription_name", "")
        if name:
            await asyncio.to_thread(deactivate_subscription, name)
        res = reply or "Отменила подписку."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "get_subscriptions":
        items = await asyncio.to_thread(get_active_subscriptions)
        if items:
            lines = ["📋 **Активные подписки:**"]
            for item in items:
                lines.append(f"- {item.get('name')}: {_format_currency(item.get('amount'))} тг/мес ({item.get('bank')})")
            res = "\n".join(lines)
        else:
            res = "Нет активных подписок."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # НАПОМИНАНИЯ — единая логика в services/reminders.py (та же, что и для
    # прямого локального перехвата выше), чтобы не было двух разных путей
    # записи в один и тот же лист с разными наборами багов.
    if intent in {"add_reminder", "delete_reminder", "get_reminders"}:
        if await reminders.handle_model(message, parsed, user_name):
            return
        res = reply or "Не смогла обработать напоминание. Уточни, пожалуйста."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "get_debts":
        await debts.handle_model(message, parsed, user_name)
        return

    # СПИСОК ПОКУПОК
    if intent == "add_shopping":
        items = parsed.get("shopping_items", [])
        if items:
            await asyncio.to_thread(add_shopping_items, items, user_name)
        res = reply or "Добавила в список покупок."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "clear_shopping":
        items = parsed.get("shopping_items", [])
        if items:
            await asyncio.to_thread(mark_shopping_items_done, items)
            res = reply or "Убрала из списка."
        else:
            # Ничего конкретного не назвали — значит и убирать нечего.
            # Не повторяем бодрый ответ ИИ "Всё убрала", если по факту
            # список не тронут — иначе создаётся видимость, что что-то
            # удалилось, а на самом деле нет.
            res = "Не нашла, что именно убрать — назови товар."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "get_shopping":
        items = await asyncio.to_thread(get_shopping_items)
        if items:
            lines = ["🛒 **Список покупок:**"]
            for item in items:
                lines.append(f"- {item.get('item')}")
            res = "\n".join(lines)
        else:
            res = "Список покупок пуст."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # ПОЕЗДКИ
    if intent == "add_trip":
        destination = parsed.get("destination", "")
        dates = parsed.get("dates", "")
        budget = _to_number_or_blank(parsed.get("budget", 0))
        notes = parsed.get("notes", "")
        if destination:
            await asyncio.to_thread(add_trip_plan, destination, dates, budget, notes)
        res = reply or "Записала поездку."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "get_trips":
        items = await asyncio.to_thread(get_planned_trips)
        if items:
            lines = ["✈️ **Запланированные поездки:**"]
            for item in items:
                lines.append(f"- {item.get('destination')} ({item.get('dates')}): {_format_currency(item.get('budget'))} тг")
            res = "\n".join(lines)
        else:
            res = "Нет запланированных поездок."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # ЛИМИТЫ
    if intent == "get_limits":
        current_limits = await asyncio.to_thread(get_category_limits)
        if current_limits:
            lines = ["📊 **Текущие лимиты:**"]
            for cat, limit in current_limits.items():
                lines.append(f"- {cat}: {_format_currency(limit)} тг")
            res = "\n".join(lines)
        else:
            res = "Лимиты не заданы."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    if intent == "generate_limits":
        try:
            new_limits = await generate_limits_from_history()
            if new_limits:
                lines = ["📊 **Сгенерированные лимиты:**"]
                for cat, limit in new_limits.items():
                    lines.append(f"- {cat}: {_format_currency(limit)} тг")
                res = "\n".join(lines)
            else:
                res = "Недостаточно данных для генерации лимитов."
        except Exception as error:
            print(f"[Лимиты] Ошибка генерации: {error}")
            res = "Не удалось сгенерировать лимиты."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # ДОХОДЫ
    if intent == "get_income":
        now = now_astana()
        start = now.replace(day=1).strftime("%Y-%m-%d")
        if now.month == 12:
            end = now.replace(year=now.year + 1, month=1, day=1).strftime("%Y-%m-%d")
        else:
            end = now.replace(month=now.month + 1, day=1).strftime("%Y-%m-%d")

        transactions = await asyncio.to_thread(get_transactions_for_period, start, end)
        incomes = [t for t in transactions if str(t.get("type")) == TYPE_INCOME]
        total = sum(_to_number_or_blank(t.get("amt")) for t in incomes)
        lines = [f"💰 **Доходы за {now.strftime('%B %Y')}:** {_format_currency(total)} тг"]
        for t in incomes:
            lines.append(f"  - {t.get('cat')}: {_format_currency(t.get('amt'))} тг ({t.get('comm')})")
        res = "\n".join(lines)
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # ПОГОДА (через ИИ — для формулировок, не пойманных прямым перехватом выше)
    if intent == "get_weather":
        target = parsed.get("weather_target") or "today"
        if target not in {"today", "tomorrow", "after_tomorrow", "week"}:
            target = _weather_target_from_text(t_clean)
        forecast = await get_weather_forecast(target=target)
        res = forecast or reply or "Не удалось связаться с погодной станцией Open-Meteo."
        add_chat_message(chat_id, "Ада", res)
        await safe_answer(message, res)
        return

    # ОБЫЧНЫЙ ЧАТ
    res = reply or "Чем могу помочь?"
    add_chat_message(chat_id, "Ада", res)
    await safe_answer(message, res)

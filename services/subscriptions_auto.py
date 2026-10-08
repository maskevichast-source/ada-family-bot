"""Автоподписки: оплата подписки, записанная вручную (чек, скрин, текст), закрывает платёж месяца,
а незнакомый регулярный сервис бот предлагает добавить в подписки кнопкой.

Как это встроено: append_transaction кладёт копию записанной траты в очередь (enqueue), фоновая
задача в main.py разбирает очередь и вызывает process_tx. Так обработчики сообщений не меняются,
а сбой разбора никогда не мешает записи траты. Очередь живёт в памяти: если процесс перезапустится,
необработанные траты потеряются — это не страшно (в день списания сработает прежняя проверка дубля)."""
import calendar
import datetime
import re
import threading
import time
import uuid
from collections import deque

from services import state

SUGGEST_NS = "sub_suggest"
ASKED_NS = "sub_asked"
CALLBACK_PREFIX = "subadd:"
ASK_AGAIN_AFTER_DAYS = 30
AMOUNT_TOLERANCE = 0.05          # подписка может чуть дорожать (курс, налог) — 5% считаем той же суммой
EARLY_WINDOW_DAYS = 10           # оплата за следующий месяц не раньше чем за 10 дней до срока

_SUB_CATEGORY = "Связь и подписки"
_SUB_SUBCATEGORIES = {"цифровые подписки и сервисы", "мобильная связь и интернет"}
_KNOWN_SERVICES = ("netflix", "нетфликс", "spotify", "спотифай", "youtube", "ютуб", "icloud", "apple", "яндекс плюс",
                   "yandex plus", "chatgpt", "openai", "claude", "anthropic", "google one", "kinopoisk", "кинопоиск",
                   "okko", "ivi", "megogo", "tele2", "теле2", "beeline", "билайн", "altel", "алтел", "activ",
                   "kcell", "кселл", "казахтелеком", "wink", "playstation", "xbox", "steam")
_MONTHS_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
               "ноябрь", "декабрь"]

PENDING: deque = deque(maxlen=200)
_lock = threading.Lock()


def _norm(text) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(text or "").lower())).strip()


def enqueue(tx: dict) -> None:
    """Вызывается после успешной записи траты. Только копирует нужные поля, ничего не читает из таблицы."""
    try:
        comment = str(tx.get("user_comment") or "")
        if comment.startswith("Автосписание"):
            return                                                  # это наша же автозапись
        if str(tx.get("type") or "").upper() != "РАСХОД":
            return
        PENDING.append({k: tx.get(k) for k in ("amount", "date", "merchant", "category", "subcategory",
                                              "user_comment", "bank", "transaction_id")})
    except Exception as error:
        print(f"[Подписки-авто] Не удалось поставить в очередь: {error}")


def drain() -> list[dict]:
    items = []
    while PENDING:
        try:
            items.append(PENDING.popleft())
        except IndexError:
            break
    return items


def month_name(prefix: str) -> str:
    try:
        return _MONTHS_NOM[int(prefix[5:7]) - 1]
    except (ValueError, IndexError):
        return prefix


def _tx_date(tx: dict) -> datetime.date:
    raw = str(tx.get("date") or "")[:10]
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        return datetime.date.today()


def _effective_day(day: int, year: int, month: int) -> int:
    return min(max(day, 1), calendar.monthrange(year, month)[1])


def _month_prefix(date: datetime.date) -> str:
    return date.strftime("%Y-%m")


def _next_month(prefix: str) -> str:
    year, month = int(prefix[:4]), int(prefix[5:7])
    return f"{year + 1}-01" if month == 12 else f"{year}-{month + 1:02d}"


def find_match(subs: list[dict], tx: dict) -> dict | None:
    """Активная подписка, к которой относится трата: название встречается в тексте траты (или наоборот)
    и сумма в пределах допуска. Среди нескольких берём ту, где сумма ближе."""
    from services.money import parse_amount
    text = _norm(f'{tx.get("merchant", "")} {tx.get("user_comment", "")}')
    merchant = _norm(tx.get("merchant"))
    amount = float(parse_amount(tx.get("amount", 0)) or 0)
    best, best_diff = None, None
    for sub in subs:
        name = _norm(sub.get("name"))
        if len(name) < 3:
            continue
        if name not in text and not (len(merchant) >= 3 and merchant in name):
            continue
        sub_amount = float(parse_amount(sub.get("amount", 0)) or 0)
        diff = abs(sub_amount - amount)
        if diff > max(0.5, sub_amount * AMOUNT_TOLERANCE):
            continue
        if best is None or diff < best_diff:
            best, best_diff = sub, diff
    return best


def covered_month(tx_date: datetime.date, day: int, paid_month: str) -> str | None:
    """За какой месяц засчитать оплату. None — это не оплата подписки (скорее дубль или разовая покупка).
    Месяц оплаты ещё не закрыт — закрываем его (и досрочно, и с опозданием). Уже закрыт, а оплата
    пришла незадолго до следующего срока — это оплата вперёд, закрываем следующий месяц."""
    month = _month_prefix(tx_date)
    if paid_month < month:
        return month
    if paid_month == month:
        nxt = _next_month(month)
        year, mon = int(nxt[:4]), int(nxt[5:7])
        due = datetime.date(year, mon, _effective_day(day, year, mon))
        if 0 <= (due - tx_date).days <= EARLY_WINDOW_DAYS:
            return nxt
    return None


def _read_subs():
    from services.sheets import _get_or_create_subscriptions_sheet, _get_all_records_safe
    ws = _get_or_create_subscriptions_sheet()
    records = _get_all_records_safe(ws)
    return ws, [dict(r, _row=idx) for idx, r in enumerate(records, start=2)
                if str(r.get("status") or "").strip().lower() == "active"]


def _is_candidate(tx: dict) -> bool:
    if not str(tx.get("merchant") or "").strip():
        return False
    category = str(tx.get("category") or "").strip()
    subcategory = str(tx.get("subcategory") or "").strip().lower()
    if category == _SUB_CATEGORY and subcategory in _SUB_SUBCATEGORIES:
        return True
    text = _norm(f'{tx.get("merchant", "")}')
    return category == _SUB_CATEGORY or any(word in text for word in _KNOWN_SERVICES)


def _asked_recently(name_key: str) -> bool:
    record = state.get(ASKED_NS, name_key)
    if not record:
        return False
    return time.time() - float(record.get("at", 0)) < ASK_AGAIN_AFTER_DAYS * 86400 or record.get("never")


def process_tx(tx: dict) -> dict | None:
    """{"kind": "paid", ...} — оплата засчитана; {"kind": "suggest", ...} — предложить добавить подписку;
    None — ничего делать не надо."""
    from services.money import parse_amount
    ws, subs = _read_subs()
    match = find_match(subs, tx)
    date = _tx_date(tx)
    if match:
        day = int(parse_amount(match.get("day_of_month", 1)) or 1)
        paid_month = str(match.get("paid_month") or "").strip()
        last_paid = str(match.get("last_paid") or "")[:7]
        paid_month = max(paid_month, last_paid)
        target = covered_month(date, day, paid_month)
        if not target:
            return None
        from services.sheets import SUBSCRIPTION_HEADERS
        ws.update_cell(match["_row"], SUBSCRIPTION_HEADERS.index("last_paid") + 1, date.strftime("%Y-%m-%d"))
        ws.update_cell(match["_row"], SUBSCRIPTION_HEADERS.index("paid_month") + 1, target)
        _remember_mark(tx.get("transaction_id"), str(match.get("name")), str(match.get("last_paid") or ""),
                       str(match.get("paid_month") or ""))
        return {"kind": "paid", "name": str(match.get("name")), "month": target, "date": date.strftime("%d.%m"),
                "day": day, "amount": float(parse_amount(tx.get("amount", 0)) or 0)}
    if not _is_candidate(tx):
        return None
    name = str(tx.get("merchant")).strip()
    key = _norm(name)
    if not key or _asked_recently(key):
        return None
    token = uuid.uuid4().hex[:10]
    amount = float(parse_amount(tx.get("amount", 0)) or 0)
    state.put(SUGGEST_NS, token, {"name": name, "amount": amount, "bank": str(tx.get("bank") or "Не указан"),
                                  "day": date.day, "date": date.strftime("%Y-%m-%d"), "key": key,
                                  "tx_id": str(tx.get("transaction_id") or "")})
    state.put(ASKED_NS, key, {"at": time.time(), "never": False})
    return {"kind": "suggest", "token": token, "name": name, "amount": amount, "day": date.day}


def apply_answer(token: str, add: bool) -> dict | None:
    """Ответ на кнопку. add=True — подписка создаётся, и эта оплата сразу засчитана за месяц оплаты.
    add=False — больше не спрашиваем про этот сервис. None — кнопка устарела."""
    data = state.get(SUGGEST_NS, token)
    if not data:
        return None
    state.delete(SUGGEST_NS, token)
    if not add:
        state.put(ASKED_NS, data["key"], {"at": time.time(), "never": True})
        return {"added": False, **data}
    from services.sheets import add_or_update_subscription
    add_or_update_subscription(data["name"], data["amount"], data["bank"], data["day"], paid_date=data["date"])
    _remember_mark(data.get("tx_id"), data["name"], "", "")
    return {"added": True, **data}


def keyboard(token: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Добавить в подписки", callback_data=f"{CALLBACK_PREFIX}y:{token}"),
        InlineKeyboardButton(text="🚫 Не подписка", callback_data=f"{CALLBACK_PREFIX}n:{token}"),
    ]])


MARK_NS = "sub_paid_by"


def _remember_mark(tx_id, name: str, prev_last_paid: str, prev_paid_month: str) -> None:
    """Запоминаем, какая трата закрыла месяц подписки: если трату отменят, отметку нужно снять."""
    if tx_id:
        state.put(MARK_NS, str(tx_id), {"name": name, "last_paid": prev_last_paid, "paid_month": prev_paid_month})


def revert_for_ids(ids) -> list[str]:
    """Трата(ы) отменены кнопкой «Отменить»: возвращаем у подписки прежние last_paid и paid_month,
    чтобы автосписание этого месяца не пропало. Возвращает названия подписок."""
    from services.sheets import SUBSCRIPTION_HEADERS, _get_all_records_safe, _get_or_create_subscriptions_sheet
    done = []
    wanted = [str(i) for i in (ids or []) if str(i or "").strip()]
    marks = []
    for key in {w for w in wanted}:
        record = state.get(MARK_NS, key)
        if record:
            marks.append((key, record))
    if not marks:
        return done
    ws = _get_or_create_subscriptions_sheet()
    records = _get_all_records_safe(ws)
    for key, record in marks:
        for idx, row in enumerate(records, start=2):
            if str(row.get("name", "")).strip().lower() == record["name"].strip().lower():
                ws.update_cell(idx, SUBSCRIPTION_HEADERS.index("last_paid") + 1, record["last_paid"])
                ws.update_cell(idx, SUBSCRIPTION_HEADERS.index("paid_month") + 1, record["paid_month"])
                done.append(record["name"])
                break
        state.delete(MARK_NS, key)
    return done

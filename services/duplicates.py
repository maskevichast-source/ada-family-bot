"""Защита от дублей: если только что записанная трата похожа на уже записанную (ты прислал чек, а Диана скрин той же
оплаты), бот спрашивает кнопками, не одна ли это покупка. Сам ничего не удаляет без нажатия.

Как и в автоподписках: append_transaction кладёт копию в очередь, фоновая задача разбирает её."""
import datetime
import re
import threading
import uuid
from collections import deque

from services import state

NS = "dup_suggest"
CALLBACK_PREFIX = "dup:"
MIN_AMOUNT = 1000
WINDOW_HOURS = 24

PENDING: deque = deque(maxlen=200)
_lock = threading.Lock()


def _norm(text) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(text or "").lower())).strip()


def enqueue(tx: dict) -> None:
    try:
        if str(tx.get("type") or "").upper() != "РАСХОД":
            return
        if str(tx.get("user_comment") or "").startswith("Автосписание"):
            return
        PENDING.append({k: tx.get(k) for k in ("transaction_id", "date", "amount", "merchant", "bank", "user", "category")})
    except Exception as error:
        print(f"[Дубли] Не удалось поставить в очередь: {error}")


def drain() -> list[dict]:
    items = []
    while PENDING:
        try:
            items.append(PENDING.popleft())
        except IndexError:
            break
    return items


def _same_receipt(a_id: str, b_id: str) -> bool:
    """Позиции одного чека/одной разбивки имеют общее начало ID (TRX_дата_время_микросекунды)."""
    ka, kb = str(a_id).split("_")[:4], str(b_id).split("_")[:4]
    return len(ka) >= 4 and ka == kb


def _merchant_match(a, b) -> bool:
    a, b = _norm(a), _norm(b)
    return len(a) >= 3 and len(b) >= 3 and (a in b or b in a)


def is_probable_duplicate(new: dict, old: dict, parse_dt) -> bool:
    """Похожие траты: та же сумма, в пределах суток, и либо то же место, либо разные люди с одним банком."""
    try:
        amount_new = float(new.get("amount") or 0)
        amount_old = float(old.get("amt") or 0)
    except (TypeError, ValueError):
        return False
    if amount_new < MIN_AMOUNT or abs(amount_new - amount_old) > max(1.0, amount_new * 0.002):
        return False
    if str(old.get("type")) == "ДОХОД" or str(old.get("transaction_id")) == str(new.get("transaction_id")):
        return False
    if _same_receipt(new.get("transaction_id"), old.get("transaction_id")):
        return False
    a, b = parse_dt(new.get("date")), parse_dt(old.get("date"))
    if not a or not b or abs((a - b).total_seconds()) > WINDOW_HOURS * 3600:
        return False
    if _merchant_match(new.get("merchant"), old.get("merchant")):
        return True
    bank_new, bank_old = _norm(new.get("bank")), _norm(old.get("bank"))
    different_people = _norm(new.get("user")) != _norm(old.get("user"))
    return different_people and bank_new not in ("", "не указан") and bank_new == bank_old


def find_duplicate(new: dict, fetch, parse_dt) -> dict | None:
    """fetch(start_iso, end_iso) — операции за период. Возвращает старую запись, похожую на новую."""
    a = parse_dt(new.get("date"))
    if not a:
        return None
    start = (a - datetime.timedelta(days=1)).date().isoformat()
    end = (a + datetime.timedelta(days=2)).date().isoformat()
    for old in fetch(start, end):
        if is_probable_duplicate(new, old, parse_dt):
            return old
    return None


def _money(value) -> str:
    return f"{float(value):,.0f}".replace(",", " ")


def describe(tx_like: dict, amount_key: str) -> str:
    name = tx_like.get("merchant") or tx_like.get("category") or tx_like.get("cat") or "трата"
    who = tx_like.get("user") or ""
    return f"{_money(tx_like.get(amount_key) or 0)} тг, {name}" + (f", {who}" if who else "")


def suggest(new: dict, old: dict) -> dict:
    token = uuid.uuid4().hex[:10]
    state.put(NS, token, {"new_id": str(new.get("transaction_id")), "summary": describe(new, "amount")})
    return {"token": token,
            "text": (f"🔁 Похоже на дубль. Только что записано: {describe(new, 'amount')} "
                     f"({str(new.get('date'))[:16]}). Уже была запись: {describe(old, 'amt')} ({str(old.get('date'))[:16]}). "
                     "Это одна и та же покупка?")}


def keyboard(token: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, удалить новую", callback_data=f"{CALLBACK_PREFIX}y:{token}"),
        InlineKeyboardButton(text="👍 Нет, это разные", callback_data=f"{CALLBACK_PREFIX}n:{token}"),
    ]])


def apply_answer(token: str, delete_new: bool) -> dict | None:
    """None — кнопка устарела. При «удалить» запись удаляется из таблицы по ID; ошибки таблицы пробрасываются."""
    data = state.get(NS, token)
    if not data:
        return None
    if delete_new:
        from services.sheets import delete_transactions_by_ids
        removed = delete_transactions_by_ids([data["new_id"]])
        state.delete(NS, token)
        return {"deleted": len(removed), **data}
    state.delete(NS, token)
    return {"deleted": 0, **data}

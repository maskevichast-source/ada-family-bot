"""Списки для мини-аппа (только чтение): долги, цели, подписки, цены.

Данные берутся теми же функциями, что использует бот. Каждый раздел
загружается отдельно: если один лист таблицы не читается, остальные всё равно
показываются (в разделе будет {"error": true}). Результат кэшируется на 90 секунд.
"""
import calendar
import datetime
import threading
import time

from services.money import parse_amount
from services.timezone import now_astana, parse_flexible_datetime

CACHE_TTL_SECONDS = 90
MIN_REFRESH_INTERVAL_SECONDS = 15

# Ссылки на товары отдаём странице только с этих доменов (защита от чужих URL в таблице).
_SAFE_LINK_HOSTS = ("https://kaspi.kz/", "https://l.kaspi.kz/")


def _debts(now: datetime.datetime) -> dict:
    from services import debts

    today = now.date()
    items = []
    for b in debts.balances():
        balance = float(b.get("balance") or 0)
        if balance <= 0:
            continue
        due_raw = str(b.get("due_date") or "").strip()
        due_dt = parse_flexible_datetime(due_raw) if due_raw else None
        days_left = (due_dt.date() - today).days if due_dt else None
        items.append({
            "counterparty": str(b.get("counterparty") or "").strip(),
            "direction": b.get("direction"),                    # lent — нам должны, borrowed — мы должны
            "balance": round(balance, 2),
            "amount": round(float(parse_amount(b.get("amount"))), 2),
            "due_date": due_dt.strftime("%d.%m.%Y") if due_dt else "",
            "days_left": days_left,
            "note": str(b.get("note") or "").strip()[:80],
        })
    # Сначала просроченные и ближайшие по сроку, затем без срока; внутри — по сумме
    items.sort(key=lambda d: (d["days_left"] is None, d["days_left"] if d["days_left"] is not None else 0, -d["balance"]))
    return {
        "items": items,
        "lent_total": round(sum(d["balance"] for d in items if d["direction"] == "lent"), 2),
        "borrowed_total": round(sum(d["balance"] for d in items if d["direction"] == "borrowed"), 2),
    }


def _goals(now: datetime.datetime) -> dict:
    from services import goals

    items = []
    for g in goals.get_goals():
        target = float(parse_amount(g.get("target_amount", 0)))
        current = float(parse_amount(g.get("current_amount", 0)))
        deadline_raw = str(g.get("deadline") or "").strip()
        dl = parse_flexible_datetime(deadline_raw) if deadline_raw else None
        items.append({
            "name": str(g.get("name") or "").strip(),
            "current": round(current, 2),
            "target": round(target, 2),
            "pct": int(current / target * 100) if target > 0 else 0,
            "remaining": round(max(target - current, 0), 2),
            "deadline": dl.strftime("%d.%m.%Y") if dl else deadline_raw,
            "days_left": (dl.date() - now.date()).days if dl else None,
        })
    return {"items": items, "saved_total": round(sum(g["current"] for g in items), 2)}


def next_payment_date(day_of_month: int, last_paid: str, today: datetime.date) -> datetime.date:
    """Ближайшая дата списания. День 29–31 в коротком месяце — последний день месяца
    (как в check_subscriptions)."""
    day = max(int(day_of_month or 1), 1)
    this_month = datetime.date(today.year, today.month, min(day, calendar.monthrange(today.year, today.month)[1]))
    paid_this_month = str(last_paid or "").startswith(today.strftime("%Y-%m"))
    if paid_this_month:
        ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        return datetime.date(ny, nm, min(day, calendar.monthrange(ny, nm)[1]))
    return max(this_month, today)      # не списано и срок прошёл — спишется на ближайшей проверке


def _subscriptions(now: datetime.datetime) -> dict:
    from services import sheets

    today = now.date()
    items = []
    for r in sheets.get_active_subscriptions():
        nxt = next_payment_date(int(parse_amount(r.get("day_of_month", 1))), r.get("last_paid", ""), today)
        items.append({
            "name": str(r.get("name") or "").strip(),
            "amount": round(float(parse_amount(r.get("amount", 0))), 2),
            "bank": str(r.get("bank") or "").strip(),
            "next_date": nxt.strftime("%d.%m.%Y"),
            "days_left": (nxt - today).days,
        })
    items.sort(key=lambda s: (s["days_left"], -s["amount"]))
    return {"items": items, "month_total": round(sum(s["amount"] for s in items), 2)}


_STATUS_ORDER = {"active": 0, "reached": 1, "broken": 2}


def _prices(now: datetime.datetime) -> dict:
    from services import sheets

    ws = sheets._get_or_create_price_tracking_sheet()
    items = []
    for r in sheets._get_all_records_safe(ws):
        status = str(r.get("status") or "").strip().lower()
        if not str(r.get("id") or "").strip() or status not in _STATUS_ORDER:
            continue                                            # stopped и пустые строки не показываем
        first = float(parse_amount(r.get("first_price", 0)))
        last = float(parse_amount(r.get("last_price", 0)))
        url = str(r.get("url") or "").strip()
        items.append({
            "name": str(r.get("product_name") or "Товар").strip()[:90],
            "status": status,
            "first_price": round(first, 2),
            "last_price": round(last, 2),
            "target_price": round(float(parse_amount(r.get("target_price", 0))), 2) or None,
            "change_pct": round((last - first) / first * 100, 1) if first > 0 and last > 0 else None,
            "url": url if url.startswith(_SAFE_LINK_HOSTS) else "",
        })
    items.sort(key=lambda p: (_STATUS_ORDER[p["status"]], p["name"].lower()))
    return {"items": items}


_SECTIONS = {"debts": _debts, "goals": _goals, "subscriptions": _subscriptions, "prices": _prices}


def load_lists(now: datetime.datetime | None = None) -> dict:
    """Читает все четыре раздела; сбой одного не роняет остальные."""
    now = now or now_astana()
    out = {"updated_at": now.strftime("%H:%M")}
    for name, fn in _SECTIONS.items():
        try:
            out[name] = fn(now)
        except Exception as e:  # noqa: BLE001 - лист мог быть недоступен или повреждён
            print(f"[Мини-апп] Раздел {name}: {e}")
            out[name] = {"error": True, "items": []}
    return out


_cache: dict = {"at": 0.0, "data": None}
_lock = threading.Lock()


def get_lists(force: bool = False, loader=load_lists, clock=time.monotonic) -> dict:
    with _lock:
        age = clock() - _cache["at"]
        if _cache["data"] is not None:
            if (age < CACHE_TTL_SECONDS and not force) or age < MIN_REFRESH_INTERVAL_SECONDS:
                return _cache["data"]
        data = loader()
        _cache.update(at=clock(), data=data)
        return data


def reset_cache() -> None:
    with _lock:
        _cache.update(at=0.0, data=None)

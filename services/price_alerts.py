"""Когда и как сообщать о падении цены (Kaspi).

Раньше сообщение уходило при КАЖДОЙ проверке (раз в 3 часа), пока цена ниже первоначальной
(«было 14 690 → сейчас 14 390» снова и снова), в общий чат, а двойная запись об одном товаре
удваивала сообщения. Теперь:
  * уведомляем, когда цена упала на >= MIN_DROP_PCT и >= MIN_DROP_ABS ₸ ОТ ПОСЛЕДНЕЙ ЦЕНЫ,
    о которой уже сообщали (базовой); после сообщения базовая цена = новая;
  * не чаще раза в COOLDOWN_HOURS на товар;
  * если цена выросла выше базовой, базовая «подтягивается» вверх (падение от нового уровня
    снова станет поводом);
  * цель достигнута — отдельное сообщение один раз (статус reached);
  * сообщение идёт в личку тому, кто следит, а не в общий чат.
"""
import datetime
from urllib.parse import urlsplit

MIN_DROP_PCT = 5.0
MIN_DROP_ABS = 500
COOLDOWN_HOURS = 24
TITLE_MAX_CHARS = 70


def normalize_url(url) -> str:
    """Один и тот же товар в разных написаниях ссылки (с ?c=…, с / в конце) — один ключ."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    return f"{parts.netloc.lower()}{parts.path.rstrip('/')}"


def _num(value) -> float:
    try:
        return float(str(value).replace(" ", "").replace(",", ".")) if value not in (None, "") else 0.0
    except ValueError:
        return 0.0


def _parse_time(value) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(str(value or "").strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def decide(item: dict, new_price: float, now: datetime.datetime) -> dict:
    """Решение по одной проверке цены.

    Возвращает {"reason": None|"drop"|"target", "baseline", "new_baseline", "drop", "drop_pct"}.
    Базовая цена: notified_price; если её ещё нет (старые записи) — последняя известная цена,
    чтобы сразу после обновления не прилетело повторное сообщение о том, о чём уже говорили.
    """
    first = _num(item.get("first_price"))
    last = _num(item.get("last_price"))
    baseline = _num(item.get("notified_price")) or last or first
    target = _num(item.get("target_price")) or None

    result = {"reason": None, "baseline": baseline, "new_baseline": baseline, "drop": 0.0, "drop_pct": 0.0}
    if new_price <= 0:
        return result
    drop = baseline - new_price
    result["drop"] = max(drop, 0.0)
    result["drop_pct"] = round(drop / baseline * 100, 1) if baseline > 0 and drop > 0 else 0.0

    if target is not None and new_price <= target:
        result.update(reason="target", new_baseline=new_price)
        return result
    if new_price > baseline:
        result["new_baseline"] = new_price            # цена выросла: следующее падение считаем от неё
        return result

    notified_at = _parse_time(item.get("notified_at"))
    now_naive = now.replace(tzinfo=None) if now.tzinfo else now
    cooled_down = notified_at is None or (now_naive - notified_at) >= datetime.timedelta(hours=COOLDOWN_HOURS)
    if drop >= MIN_DROP_ABS and result["drop_pct"] >= MIN_DROP_PCT and cooled_down:
        result.update(reason="drop", new_baseline=new_price)
    return result


def _money(value: float) -> str:
    return f"{int(round(value)):,}".replace(",", " ") + " ₸"


def short_title(name) -> str:
    text = " ".join(str(name or "Товар").split())
    return text if len(text) <= TITLE_MAX_CHARS else text[: TITLE_MAX_CHARS - 1].rstrip(" ,.") + "…"


def build_message(item: dict, decision: dict, new_price: float, url: str) -> str:
    """Обычный текст без разметки (раньше «**» показывались буквально)."""
    title = short_title(item.get("product_name"))
    first = _num(item.get("first_price"))
    if decision["reason"] == "target":
        target = _num(item.get("target_price"))
        lines = [f"🎯 Достигнута цель {_money(target)}: {title}", f"Сейчас {_money(new_price)}"]
        if decision["baseline"] > new_price:
            lines[1] += f" (было {_money(decision['baseline'])})"
    else:
        lines = [f"📉 Цена упала на {decision['drop_pct']:g}%: {title}",
                 f"Было {_money(decision['baseline'])} → сейчас {_money(new_price)} (−{_money(decision['drop'])})"]
        if first and abs(first - decision["baseline"]) >= 1 and first > new_price:
            lines.append(f"С начала слежения: {_money(first)} → {_money(new_price)}")
    lines.append(str(url or ""))
    return "\n".join(l for l in lines if l)


def split_duplicates(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Активные записи об одном товаре у одного человека: оставляем первую, остальные — дубли."""
    seen, unique, duplicates = set(), [], []
    for item in items:
        key = (str(item.get("user") or "").strip(), normalize_url(item.get("url")))
        if key[1] and key in seen:
            duplicates.append(item)
        else:
            seen.add(key)
            unique.append(item)
    return unique, duplicates

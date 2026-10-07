"""Тревоги Владу в личку: ошибка записи в таблицу, сбой фонового цикла, перезапуск бота в рабочее время.

Любой код (в том числе обычные синхронные функции в потоках) вызывает report(kind, text): тревога кладётся в очередь.
Фоновая задача в main.py отправляет очередь в личку. Одна и та же тревога (по kind) не чаще раза в час, чтобы
повторяющаяся ошибка не засыпала личку. Сбой самой тревоги ничего не ломает."""
import datetime
import threading
import time
from collections import deque

from services import state

NS = "alert_sent"
MIN_INTERVAL_SECONDS = 3600
WORK_HOURS = range(8, 23)

PENDING: deque = deque(maxlen=50)
_lock = threading.Lock()


def report(kind: str, text: str) -> bool:
    """True — тревога поставлена в очередь; False — такая уже была в последний час."""
    try:
        with _lock:
            record = state.get(NS, kind)
            if record and time.time() - float(record.get("at", 0)) < MIN_INTERVAL_SECONDS:
                return False
            state.put(NS, kind, {"at": time.time()})
        PENDING.append(f"⚠️ {text}"[:1500])
        return True
    except Exception as error:
        print(f"[Тревоги] Не удалось поставить в очередь: {error}")
        return False


def report_error(kind: str, where: str, error: BaseException) -> bool:
    return report(kind, f"{where}: {type(error).__name__}: {str(error)[:300]}")


def drain() -> list[str]:
    items = []
    while PENDING:
        try:
            items.append(PENDING.popleft())
        except IndexError:
            break
    return items


def restart_text(now: datetime.datetime) -> str | None:
    """Текст про перезапуск, только в рабочие часы (ночные деплои и так никого не касаются)."""
    if now.hour not in WORK_HOURS:
        return None
    return f"Бот перезапустился в {now:%H:%M} (деплой или сбой). Если ты ничего не выкатывал, загляни в логи Railway."

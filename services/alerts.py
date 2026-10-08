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


TRANSIENT_WINDOW_SECONDS = 20 * 60
TRANSIENT_REPEATS = 3
_transient_hits: dict[str, deque] = {}
_TRANSIENT_NAMES = ("connection", "timeout", "remotedisconnected", "urlerror", "clienterror", "serverdisconnected",
                    "ssl", "protocolerror", "readerror", "chunkedencoding")
_TRANSIENT_TEXT = ("connection aborted", "connection reset", "temporarily", "timed out", "502", "503", "504",
                   "429", "quota exceeded", "remote end closed")


def is_transient(error: BaseException) -> bool:
    """Разовые сетевые сбои (оборвалось соединение, Google ответил 5xx): сами проходят, пугать ими не нужно."""
    names = [c.__name__.lower() for c in type(error).__mro__]
    if any(any(t in n for t in _TRANSIENT_NAMES) for n in names):
        return True
    return any(t in str(error).lower() for t in _TRANSIENT_TEXT)


def report_error(kind: str, where: str, error: BaseException) -> bool:
    """Временный сбой сети тревогу вызывает, только если повторился 3 раза за 20 минут; остальные ошибки сразу."""
    if is_transient(error):
        hits = _transient_hits.setdefault(kind, deque(maxlen=TRANSIENT_REPEATS))
        now = time.time()
        hits.append(now)
        if len(hits) < TRANSIENT_REPEATS or now - hits[0] > TRANSIENT_WINDOW_SECONDS:
            return False
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

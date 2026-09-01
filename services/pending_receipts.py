"""Чек/скриншот, ожидающий комментарий пользователя, — хранилище в памяти.

Логика (как и задумывалось изначально): если чек/скриншот прислали БЕЗ
подписи — бот запоминает распознанные транзакции здесь и следующим
текстовым (или голосовым, расшифрованным в текст) сообщением ждёт
комментарий. Если подпись УЖЕ была в самом сообщении с файлом — этот
модуль вообще не используется, чек сохраняется сразу.

Хранится в памяти процесса, не в Google Sheets — осознанно просто.
Чтобы не терять данные, если человек так и не ответил, main.py каждую
минуту вызывает sweep_expired() и сохраняет просроченные чеки без
комментария вместо того, чтобы тихо их выбросить.
"""

import datetime
import threading

_lock = threading.RLock()
_pending: dict[int, dict] = {}

PENDING_TTL_MINUTES = 20


def set_pending(chat_id: int, transactions: list[dict], user_name: str) -> None:
    with _lock:
        _pending[chat_id] = {
            "transactions": transactions,
            "user_name": user_name,
            "created_at": datetime.datetime.utcnow(),
        }


def has_pending(chat_id: int) -> bool:
    with _lock:
        return chat_id in _pending


def pop_pending(chat_id: int):
    """Забрать и удалить ожидающие транзакции. Возвращает (transactions, user_name) или None."""
    with _lock:
        entry = _pending.pop(chat_id, None)
    if not entry:
        return None
    return entry["transactions"], entry["user_name"]


def sweep_expired():
    """Забрать все просроченные (старше PENDING_TTL_MINUTES) ожидающие чеки.

    Возвращает список (chat_id, transactions, user_name) — чтобы вызывающий
    код (main.py) мог сохранить их БЕЗ комментария вместо того, чтобы
    молча терять данные, если человек забыл ответить.
    """
    now = datetime.datetime.utcnow()
    expired = []
    with _lock:
        for chat_id in list(_pending.keys()):
            entry = _pending[chat_id]
            if now - entry["created_at"] > datetime.timedelta(minutes=PENDING_TTL_MINUTES):
                expired.append((chat_id, entry["transactions"], entry["user_name"]))
                del _pending[chat_id]
    return expired

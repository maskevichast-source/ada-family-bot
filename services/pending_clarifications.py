"""Хранилище транзакций, ожидающих уточнения категории (InlineKeyboard или текст)."""

import datetime
import threading

_lock = threading.RLock()
_pending: dict[int, dict] = {}
CLARIFICATION_TTL_MINUTES = 10


def set_clarification(chat_id: int, transaction: dict, options: list[dict], message_id: int = None):
    """
    options: список словарей вида:
    [
      {"label": "🍔 Перекус на работе", "category": "Кафе, рестораны и доставка еды", "subcategory": "Перекус и фастфуд"},
      {"label": "🏠 Продукты домой", "category": "Еда и продукты", "subcategory": "Супермаркет и рынок"}
    ]
    """
    with _lock:
        _pending[chat_id] = {
            "transaction": transaction,
            "options": options,
            "created_at": datetime.datetime.utcnow(),
            "message_id": message_id,
        }


def has_clarification(chat_id: int) -> bool:
    with _lock:
        if chat_id not in _pending:
            return False
        entry = _pending[chat_id]
        if datetime.datetime.utcnow() - entry["created_at"] > datetime.timedelta(minutes=CLARIFICATION_TTL_MINUTES):
            del _pending[chat_id]
            return False
        return True


def get_clarification(chat_id: int) -> dict | None:
    with _lock:
        entry = _pending.get(chat_id)
        if not entry:
            return None
        if datetime.datetime.utcnow() - entry["created_at"] > datetime.timedelta(minutes=CLARIFICATION_TTL_MINUTES):
            del _pending[chat_id]
            return None
        return entry


def pop_clarification(chat_id: int) -> dict | None:
    with _lock:
        entry = _pending.pop(chat_id, None)
        if not entry:
            return None
        if datetime.datetime.utcnow() - entry["created_at"] > datetime.timedelta(minutes=CLARIFICATION_TTL_MINUTES):
            return None
        return entry


def sweep_expired_clarifications():
    now = datetime.datetime.utcnow()
    expired = []
    with _lock:
        for chat_id in list(_pending.keys()):
            entry = _pending[chat_id]
            if now - entry["created_at"] > datetime.timedelta(minutes=CLARIFICATION_TTL_MINUTES):
                expired.append((chat_id, entry["transaction"]))
                del _pending[chat_id]
    return expired


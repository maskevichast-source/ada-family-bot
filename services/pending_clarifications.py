"""Хранилище транзакций, ожидающих уточнения категории (InlineKeyboard).

Когда бот не уверен в категории (confidence < 0.8), транзакция сохраняется здесь,
пользователю отправляются кнопки. После нажатия — транзакция записывается
с правильной категорией.
"""

import datetime
import threading

_lock = threading.RLock()
_pending_clarifications: dict[int, dict] = {}

CLARIFICATION_TTL_MINUTES = 10


def set_clarification(chat_id: int, transaction: dict, alternatives: list[str], message_id: int = None) -> None:
    """Сохранить транзакцию, ожидающую уточнения категории."""
    with _lock:
        _pending_clarifications[chat_id] = {
            "transaction": transaction,
            "alternatives": alternatives,
            "created_at": datetime.datetime.utcnow(),
            "message_id": message_id,
        }


def has_clarification(chat_id: int) -> bool:
    """Есть ли ожидающее уточнение для этого чата?"""
    with _lock:
        if chat_id not in _pending_clarifications:
            return False
        entry = _pending_clarifications[chat_id]
        if datetime.datetime.utcnow() - entry["created_at"] > datetime.timedelta(minutes=CLARIFICATION_TTL_MINUTES):
            del _pending_clarifications[chat_id]
            return False
        return True


def get_clarification(chat_id: int) -> dict | None:
    """Получить данные уточнения (без удаления)."""
    with _lock:
        entry = _pending_clarifications.get(chat_id)
        if not entry:
            return None
        if datetime.datetime.utcnow() - entry["created_at"] > datetime.timedelta(minutes=CLARIFICATION_TTL_MINUTES):
            del _pending_clarifications[chat_id]
            return None
        return entry


def pop_clarification(chat_id: int) -> dict | None:
    """Получить и удалить данные уточнения."""
    with _lock:
        entry = _pending_clarifications.pop(chat_id, None)
        if not entry:
            return None
        if datetime.datetime.utcnow() - entry["created_at"] > datetime.timedelta(minutes=CLARIFICATION_TTL_MINUTES):
            return None
        return entry


def sweep_expired_clarifications():
    """Удалить просроченные уточнения."""
    now = datetime.datetime.utcnow()
    expired = []
    with _lock:
        for chat_id in list(_pending_clarifications.keys()):
            entry = _pending_clarifications[chat_id]
            if now - entry["created_at"] > datetime.timedelta(minutes=CLARIFICATION_TTL_MINUTES):
                expired.append((chat_id, entry["transaction"]))
                del _pending_clarifications[chat_id]
    return expired

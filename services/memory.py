"""In-memory хранилище истории чата (последние 50 сообщений).

Хранит реплики Влада, Дианы и ответы самой Ады для сохранения связного контекста диалога.
"""

from collections import defaultdict
import threading

_lock = threading.RLock()
_chat_history: dict[int, list[dict]] = defaultdict(list)
MAX_HISTORY_SIZE = 50


def add_chat_message(chat_id: int, sender: str, text: str):
    """Добавить сообщение в историю чата (пользователя или бота)."""
    if not text:
        return
    with _lock:
        _chat_history[chat_id].append({"sender": sender, "text": text.strip()})
        if len(_chat_history[chat_id]) > MAX_HISTORY_SIZE:
            _chat_history[chat_id] = _chat_history[chat_id][-MAX_HISTORY_SIZE:]


def get_chat_history(chat_id: int) -> list[dict]:
    """Получить последние 50 сообщений чата."""
    with _lock:
        return list(_chat_history.get(chat_id, []))


def clear_chat_history(chat_id: int):
    """Очистить историю чата."""
    with _lock:
        _chat_history[chat_id] = []

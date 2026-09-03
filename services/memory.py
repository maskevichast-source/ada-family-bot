"""In-memory хранилище истории чата (последние N сообщений).

Используется для контекста ИИ. НЕ персистентное — при перезапуске бота
история теряется. Для долговременной памяти используйте Google Sheets.
"""

from collections import defaultdict

# Храним последние 50 сообщений на чат
_chat_history: dict[int, list[dict]] = defaultdict(list)
MAX_HISTORY_SIZE = 50


def add_chat_message(chat_id: int, sender: str, text: str):
    """Добавить сообщение в историю чата."""
    _chat_history[chat_id].append({"sender": sender, "text": text})
    if len(_chat_history[chat_id]) > MAX_HISTORY_SIZE:
        _chat_history[chat_id] = _chat_history[chat_id][-MAX_HISTORY_SIZE:]


def get_chat_history(chat_id: int) -> list[dict]:
    """Получить историю сообщений чата."""
    return list(_chat_history.get(chat_id, []))


def clear_chat_history(chat_id: int):
    """Очистить историю чата."""
    _chat_history[chat_id] = []

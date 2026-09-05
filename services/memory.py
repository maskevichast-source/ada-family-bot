"""Персистентное хранилище истории чата (последние 50 сообщений).

Раньше история жила только в оперативной памяти и терялась при каждом
рестарте/деплое. Теперь она кэшируется в памяти, но синхронно сохраняется в
JSON-файл. Для облака можно задать CHAT_HISTORY_FILE на путь persistent disk.
"""

from collections import defaultdict
import json
import os
import threading
from pathlib import Path

try:
    from config import CHAT_HISTORY_FILE
except Exception:
    CHAT_HISTORY_FILE = "chat_history.json"

_lock = threading.RLock()
_chat_history: dict[int, list[dict]] = defaultdict(list)
MAX_HISTORY_SIZE = 50
_loaded = False


def _history_path() -> Path:
    return Path(CHAT_HISTORY_FILE or "chat_history.json")


def _ensure_loaded():
    global _loaded, _chat_history
    if _loaded:
        return
    path = _history_path()
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            for chat_id, items in raw.items():
                try:
                    _chat_history[int(chat_id)] = list(items)[-MAX_HISTORY_SIZE:]
                except Exception:
                    continue
    except Exception as e:
        print(f"[Memory] Не удалось загрузить историю: {e}")
    _loaded = True


def _save():
    path = _history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {str(k): v[-MAX_HISTORY_SIZE:] for k, v in _chat_history.items()}
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        print(f"[Memory] Не удалось сохранить историю: {e}")


def add_chat_message(chat_id: int, sender: str, text: str):
    """Добавить сообщение в историю чата (пользователя или бота)."""
    if not text:
        return
    with _lock:
        _ensure_loaded()
        _chat_history[int(chat_id)].append({"sender": sender, "text": text.strip()})
        if len(_chat_history[int(chat_id)]) > MAX_HISTORY_SIZE:
            _chat_history[int(chat_id)] = _chat_history[int(chat_id)][-MAX_HISTORY_SIZE:]
        _save()


def get_chat_history(chat_id: int) -> list[dict]:
    """Получить последние 50 сообщений чата."""
    with _lock:
        _ensure_loaded()
        return list(_chat_history.get(int(chat_id), []))


def clear_chat_history(chat_id: int):
    """Очистить историю чата."""
    with _lock:
        _ensure_loaded()
        _chat_history[int(chat_id)] = []
        _save()

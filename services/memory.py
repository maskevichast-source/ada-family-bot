"""Надёжное хранение истории семейного чата на диске."""

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


MAX_HISTORY = 50
MEMORY_FILE = Path(__file__).resolve().parent.parent / "chat_history.json"
_memory_lock = threading.RLock()


def load_memory() -> dict[str, list[dict[str, str]]]:
    """Загрузить историю из JSON, не ломая запуск при повреждённом файле."""
    with _memory_lock:
        try:
            if not MEMORY_FILE.exists():
                return {}
            with MEMORY_FILE.open("r", encoding="utf-8") as file:
                raw_memory = json.load(file)
            if not isinstance(raw_memory, dict):
                return {}
            return {
                str(chat_id): list(messages)[-MAX_HISTORY:]
                for chat_id, messages in raw_memory.items()
                if isinstance(messages, list)
            }
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            print(f"[Память] Не удалось загрузить историю: {error}")
            return {}


def save_memory(memory: dict[str, list[dict[str, str]]] | None = None) -> None:
    """Сохранить историю атомарно, чтобы сбой не оставил битый JSON."""
    with _memory_lock:
        data = memory if memory is not None else load_memory()
        normalized = {
            str(chat_id): list(messages)[-MAX_HISTORY:]
            for chat_id, messages in data.items()
            if isinstance(messages, list)
        }
        temporary_path: str | None = None
        try:
            MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=MEMORY_FILE.parent,
                prefix=".chat_history.",
                suffix=".tmp",
                delete=False,
            ) as file:
                json.dump(normalized, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
                temporary_path = file.name
            os.replace(temporary_path, MEMORY_FILE)
        except (OSError, TypeError, ValueError) as error:
            print(f"[Память] Не удалось сохранить историю: {error}")
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass


def add_chat_message(chat_id: int | str, sender: str, text: str) -> None:
    """Добавить реплику и сразу сохранить последние MAX_HISTORY сообщений."""
    with _memory_lock:
        memory = load_memory()
        key = str(chat_id)
        history = memory.setdefault(key, [])
        history.append({"sender": str(sender), "text": str(text)})
        memory[key] = history[-MAX_HISTORY:]
        save_memory(memory)


def get_chat_history(chat_id: int | str) -> list[dict[str, str]]:
    """Вернуть историю конкретного чата без ссылки на внутренний список."""
    return list(load_memory().get(str(chat_id), []))[-MAX_HISTORY:]

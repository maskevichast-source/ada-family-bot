"""Single-worker guard and durable-volume checks for Railway."""
import contextlib
import fcntl
import logging
import os
from pathlib import Path

@contextlib.contextmanager
def worker_lock():
    # ВАЖНО: та же директория, что и у services/state.py (ADA_STATE_DIR /
    # RAILWAY_VOLUME_MOUNT_PATH / ./data). Раньше здесь импортировался
    # config.STATE_DIR, которого в config.py вообще не существовало —
    # бот падал при каждом запуске (ImportError) ещё до старта polling.
    # Второй, отдельный от state.py путь тоже был бы неправильным: тест
    # на запись должен проверять ИМЕННО тот volume, где лежит state.sqlite3,
    # а не случайную другую папку.
    path = Path(os.getenv("ADA_STATE_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "./data")
    path.mkdir(parents=True, exist_ok=True)
    # Detect read-only/misconfigured volume immediately instead of silently losing memory.
    probe = path / ".write-test"
    probe.write_text("ok")
    probe.unlink()
    if os.getenv("RAILWAY_ENVIRONMENT_ID") and not os.getenv("RAILWAY_VOLUME_MOUNT_PATH"):
        logging.warning("Railway volume variable absent. Attach volume at /app/data to preserve dialogue on deploy.")
    with (path / "worker.lock").open("w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Другой экземпляр Ады уже использует этот volume.")
        yield

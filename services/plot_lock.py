"""Matplotlib is process-global and not thread-safe: one render at a time."""
import threading
from functools import wraps
_lock = threading.RLock()
def serialized_plot(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _lock:
            return fn(*args, **kwargs)
    return wrapped

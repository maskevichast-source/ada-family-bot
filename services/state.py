"""Small durable dialogue store. Mount ADA_STATE_DIR on a persistent volume."""
import json
import os
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager

@contextmanager
def connection():
    path = Path(os.getenv("ADA_STATE_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "./data")
    path.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path / "state.sqlite3", timeout=20)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS state (namespace TEXT, key TEXT, value TEXT, created REAL, PRIMARY KEY(namespace,key))")
        yield db
        db.commit()
    finally:
        db.close()

def put(namespace, key, value):
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO state VALUES (?,?,?,?)",
                   (namespace, str(key), json.dumps(value, ensure_ascii=False), time.time()))

def get(namespace, key):
    with connection() as db:
        row = db.execute("SELECT value,created FROM state WHERE namespace=? AND key=?", (namespace, str(key))).fetchone()
        if row and namespace in {"reminder_draft", "reminder_delete", "reminder_plan", "reminder_list", "debt_draft", "edit_plan"} and time.time() - row[1] > 1800:
            db.execute("DELETE FROM state WHERE namespace=? AND key=?", (namespace, str(key)))
            row = None
    return json.loads(row[0]) if row else None

def delete(namespace, key):
    with connection() as db:
        db.execute("DELETE FROM state WHERE namespace=? AND key=?", (namespace, str(key)))

def entries(namespace, older_than=0):
    with connection() as db:
        rows = db.execute("SELECT key,value FROM state WHERE namespace=? AND created<=?",
                          (namespace, time.time()-older_than)).fetchall()
    return [(key, json.loads(value)) for key,value in rows]

def dialogue_key(chat_id, user_id):
    return f"{chat_id}:{user_id}"

def chat_from_key(key):
    return int(str(key).split(":")[0])

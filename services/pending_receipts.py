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

"""Чек/скриншот, ожидающий комментарий пользователя.

Раньше хранилось в памяти процесса, ключом был просто chat_id. У этого было
два реальных бага для семейного group-чата:
  1. Если Влад и Диана в пределах 20 минут оба присылают чек без подписи —
     запись второго человека затирала первого (один ключ на весь чат).
  2. При перезапуске процесса (деплой на Railway) все ожидающие чеки без
     комментария бесследно терялись, sweep_expired() их уже не видел.

Теперь ключ — это "chat_id:user_id" (services.state.dialogue_key), и хранится
всё в SQLite через services/state.py, переживая перезапуск.

pop_pending() умышленно НЕ удаляет запись — только читает: если между
"прочитали чек" и "дозаписали в Google Sheets" бот упадёт, данные не
потеряются, их можно будет дообработать после перезапуска. Удаляет запись
только ack_pending() — когда сохранение уже точно подтверждено.
"""

from services import state

NS = "receipts"
PENDING_TTL_MINUTES = 20


def set_pending(key, transactions: list[dict], user_name: str) -> None:
    """Добавляет транзакции к уже ожидающим по этому ключу (не затирает их)."""
    key = str(key)
    existing = state.get(NS, key)
    if existing and existing.get("user_name") == user_name:
        merged = list(existing.get("transactions") or []) + list(transactions)
        state.put(NS, key, {"transactions": merged, "user_name": user_name})
    else:
        state.put(NS, key, {"transactions": list(transactions), "user_name": user_name})


def has_pending(key) -> bool:
    return state.get(NS, str(key)) is not None


def pop_pending(key):
    """Прочитать ожидающие транзакции. Возвращает (transactions, user_name) или None.

    Не удаляет запись — см. docstring модуля. Для очистки после успешной
    записи вызови ack_pending(key).
    """
    entry = state.get(NS, str(key))
    if not entry:
        return None
    return entry["transactions"], entry["user_name"]


def ack_pending(key) -> None:
    """Подтвердить, что ожидающие транзакции обработаны, и удалить запись."""
    state.delete(NS, str(key))


def sweep_expired():
    """Забрать все просроченные (старше PENDING_TTL_MINUTES) ожидающие чеки.

    Возвращает список (key, transactions, user_name) — чтобы вызывающий
    код (main.py) мог сохранить их БЕЗ комментария вместо того, чтобы
    молча терять данные, если человек забыл ответить. Сам удаляет запись
    через ack_pending() — вызывающий код не обязан это делать.
    """
    expired = []
    for key, entry in state.entries(NS, older_than=PENDING_TTL_MINUTES * 60):
        expired.append((key, entry.get("transactions") or [], entry.get("user_name", "")))
    for key, _, _ in expired:
        ack_pending(key)
    return expired



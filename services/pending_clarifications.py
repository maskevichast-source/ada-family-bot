"""Хранилище транзакций, ожидающих уточнения категории (InlineKeyboard или текст)."""

"""Хранилище транзакций, ожидающих уточнения категории (InlineKeyboard или текст).

Как и pending_receipts.py — теперь на SQLite через services/state.py (переживает
перезапуск) и предполагает ключ "chat_id:user_id", а не голый chat_id, чтобы
уточнение для одного члена семьи не перезаписывалось уточнением для другого.
"""

from services import state
import uuid

NS = "clarifications"
CLARIFICATION_TTL_MINUTES = 10


def set_clarification(chat_id, transaction: dict, options: list[dict], message_id: int = None):
    """
    options: список словарей вида:
    [
      {"label": "🍔 Перекус на работе", "category": "Кафе, рестораны и доставка еды", "subcategory": "Перекус и фастфуд"},
      {"label": "🏠 Продукты домой", "category": "Еда и продукты", "subcategory": "Супермаркет и рынок"}
    ]

    Каждому новому уточнению присваивается token — он зашивается в
    callback_data кнопок. Если человек нажмёт кнопку от СТАРОГО, уже
    неактуального уточнения (например, после того как появилось новое),
    токены не совпадут и старый выбор не подтвердит новую покупку.
    """
    token = uuid.uuid4().hex[:8]
    state.put(NS, str(chat_id), {
        "transaction": transaction,
        "options": options,
        "message_id": message_id,
        "token": token,
    })
    return token


def has_clarification(chat_id) -> bool:
    return state.get(NS, str(chat_id)) is not None


def get_clarification(chat_id) -> dict | None:
    return state.get(NS, str(chat_id))


def pop_clarification(chat_id) -> dict | None:
    entry = state.get(NS, str(chat_id))
    if entry:
        state.delete(NS, str(chat_id))
    return entry


def sweep_expired_clarifications():
    expired = []
    for key, entry in state.entries(NS, older_than=CLARIFICATION_TTL_MINUTES * 60):
        expired.append((key, entry.get("transaction")))
    for key, _ in expired:
        state.delete(NS, key)
    return expired


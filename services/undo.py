"""Кнопка «↩️ Отменить» под ответом Ады о записи траты.

Под ответом о записи висит кнопка с коротким токеном (лимит Telegram на callback_data —
64 байта). Какие именно записи отменять, лежит в SQLite-стейте (переживает перезапуск).
Нажатие удаляет эти строки из листа Transactions по transaction_id.

Принципы: ответ «отменено» уходит в чат только после реального удаления; повторное
нажатие ничего не ломает; копия удалённых строк остаётся в стейте (на случай ручного
восстановления).
"""
import threading
import time
import uuid

from services import state

NS = "undo"
UNDO_TTL_SECONDS = 7 * 24 * 3600   # через неделю кнопка «устаревает» и запись чистится
CALLBACK_PREFIX = "undo:"
BUTTON_TEXT = "↩️ Отменить"
EDIT_PREFIX = "edt:"
EDIT_BUTTON_TEXT = "✏️ Изменить"

_lock = threading.Lock()


def _format_amount(value) -> str:
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def summarize(transactions: list[dict]) -> str:
    """Короткое описание записей для сообщения «Отменено: …»."""
    if not transactions:
        return ""
    if len(transactions) == 1:
        tx = transactions[0]
        what = str(tx.get("merchant") or tx.get("category") or "").strip()
        base = f"{_format_amount(tx.get('amount'))} ₸"
        return f"{base} · {what}" if what else base
    try:
        total = sum(float(t.get("amount") or 0) for t in transactions)
    except (TypeError, ValueError):
        total = 0
    return f"{len(transactions)} поз. на {_format_amount(total)} ₸"


def _cleanup() -> None:
    try:
        for key, _ in state.entries(NS, older_than=UNDO_TTL_SECONDS):
            state.delete(NS, key)
    except Exception:
        pass


def register(transactions: list[dict]) -> str | None:
    """Запоминает записанные транзакции под новым токеном. None — если нечего отменять."""
    ids = [str(t.get("transaction_id")) for t in transactions if t.get("transaction_id")]
    if not ids:
        return None
    _cleanup()
    token = uuid.uuid4().hex[:10]
    state.put(NS, token, {
        "ids": ids,
        "summary": summarize(transactions),
        "status": "active",
        "ts": time.time(),
    })
    return token


def keyboard_for_token(token: str):
    """Две кнопки под ответом о записи: «Отменить» и «Изменить» (см. handlers/edit_buttons.py)."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=BUTTON_TEXT, callback_data=f"{CALLBACK_PREFIX}{token}"),
        InlineKeyboardButton(text=EDIT_BUTTON_TEXT, callback_data=f"{EDIT_PREFIX}o:{token}"),
    ]])


def build_keyboard(transactions: list[dict]):
    """InlineKeyboardMarkup с кнопками «Отменить»/«Изменить» или None, если кнопки поставить не удалось.
    Сбой кнопок не должен ронять ответ о записи."""
    try:
        token = register(transactions)
        if not token:
            return None
        return keyboard_for_token(token)
    except Exception as error:
        print(f"[Undo] Не удалось поставить кнопки: {error}")
        return None


def refresh_summary(token: str, summary: str) -> None:
    """После правки записи обновляет текст, который покажет «Отменено: …»."""
    with _lock:
        entry = state.get(NS, token)
        if entry and summary:
            entry["summary"] = summary
            state.put(NS, token, entry)


def get_entry(token: str) -> dict | None:
    entry = state.get(NS, token)
    if not entry:
        return None
    if time.time() - float(entry.get("ts") or 0) > UNDO_TTL_SECONDS:
        state.delete(NS, token)
        return None
    return entry


def perform_undo(token: str) -> dict:
    """Выполняет отмену. Возвращает {"status": ...}:
    expired — токен неизвестен/устарел; already — уже отменено;
    done — удалено (deleted, summary); not_found — в таблице строк уже нет.
    Ошибка Google пробрасывается: статус остаётся active, кнопку можно нажать ещё раз."""
    from services.sheets import delete_transactions_by_ids
    with _lock:
        entry = get_entry(token)
        if not entry:
            return {"status": "expired"}
        if entry.get("status") != "active":
            return {"status": "already", "summary": entry.get("summary", "")}
        deleted = delete_transactions_by_ids(entry.get("ids") or [])
        entry["status"] = "done"
        entry["deleted"] = deleted
        state.put(NS, token, entry)
    if not deleted:
        return {"status": "not_found", "summary": entry.get("summary", "")}
    return {"status": "done", "deleted": len(deleted), "summary": entry.get("summary", "")}

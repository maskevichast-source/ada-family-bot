"""Кнопка «↩️ Отменить»: удаление по ID, токены, обработчик нажатия и подключение к ответам.
Офлайн: Sheets — фейк, Telegram — заглушки (живой Telegram не проверяется)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import sheets, undo
from services.telegram_safe import safe_answer


def _rows(db):
    return [r[0] for r in db.worksheet("Transactions").data[1:]]


def _seed(ids):
    for i in ids:
        sheets.append_transaction({"transaction_id": i, "amount": 1000, "category": "Еда и продукты"})


# ── удаление в таблице ─────────────────────────────────────────────────────

def test_delete_by_ids_removes_only_matching(db):
    _seed(["A", "B", "C", "D"])
    deleted = sheets.delete_transactions_by_ids(["B", "D"])
    assert _rows(db) == ["A", "C"]
    assert sorted(r["transaction_id"] for r in deleted) == ["B", "D"]


def test_delete_by_ids_covers_split_parts(db):
    _seed(["MEDIA_1_5_0_1", "MEDIA_1_5_0_2", "MEDIA_1_5_10", "OTHER"])
    sheets.delete_transactions_by_ids(["MEDIA_1_5_0"])
    # части разбивки удалены, «…_10» (другая позиция чека) и чужая запись целы
    assert _rows(db) == ["MEDIA_1_5_10", "OTHER"]


def test_delete_missing_id_changes_nothing(db):
    _seed(["A"])
    assert sheets.delete_transactions_by_ids(["NOPE"]) == []
    assert sheets.delete_transactions_by_ids([]) == []
    assert _rows(db) == ["A"]


# ── токены и отмена ─────────────────────────────────────────────────────────

def test_perform_undo_done_then_already(db):
    _seed(["A", "B"])
    token = undo.register([{"transaction_id": "A", "amount": 1450, "merchant": "Магнит"}])
    first = undo.perform_undo(token)
    assert first["status"] == "done" and first["deleted"] == 1 and "Магнит" in first["summary"]
    assert _rows(db) == ["B"]
    assert undo.perform_undo(token)["status"] == "already"
    assert _rows(db) == ["B"]


def test_perform_undo_unknown_token(db):
    assert undo.perform_undo("nope")["status"] == "expired"


def test_perform_undo_expired_token(db, monkeypatch):
    _seed(["A"])
    token = undo.register([{"transaction_id": "A", "amount": 1}])
    monkeypatch.setattr(undo, "UNDO_TTL_SECONDS", -1)
    assert undo.perform_undo(token)["status"] == "expired"
    assert _rows(db) == ["A"]


def test_perform_undo_row_already_gone(db):
    token = undo.register([{"transaction_id": "GONE", "amount": 1}])
    assert undo.perform_undo(token)["status"] == "not_found"


def test_sheet_failure_keeps_button_active(db, monkeypatch):
    _seed(["A"])
    token = undo.register([{"transaction_id": "A", "amount": 1}])
    real = sheets.delete_transactions_by_ids
    monkeypatch.setattr(sheets, "delete_transactions_by_ids", lambda ids: (_ for _ in ()).throw(RuntimeError("quota")))
    with pytest.raises(RuntimeError):
        undo.perform_undo(token)
    assert _rows(db) == ["A"]
    monkeypatch.setattr(sheets, "delete_transactions_by_ids", real)
    assert undo.perform_undo(token)["status"] == "done"
    assert _rows(db) == []


def test_register_without_ids_returns_none():
    assert undo.register([{"amount": 5}]) is None
    assert undo.build_keyboard([{"amount": 5}]) is None


def test_keyboard_has_button_within_telegram_limit():
    kb = undo.build_keyboard([{"transaction_id": "A", "amount": 1}])
    button = kb.inline_keyboard[0][0]
    assert "Отменить" in button.text
    assert button.callback_data.startswith("undo:") and len(button.callback_data.encode()) <= 64


def test_summary_single_and_multiple():
    assert undo.summarize([{"amount": 1450, "merchant": "Магнит"}]) == "1 450 ₸ · Магнит"
    assert undo.summarize([{"amount": 1000, "category": "Еда"}]) == "1 000 ₸ · Еда"
    assert undo.summarize([{"amount": 1000}, {"amount": 500}]) == "2 поз. на 1 500 ₸"


# ── обработчик нажатия ──────────────────────────────────────────────────────

def _callback(token):
    msg = SimpleNamespace(edit_text=AsyncMock(), edit_reply_markup=AsyncMock())
    return SimpleNamespace(data=f"undo:{token}", message=msg, from_user=SimpleNamespace(id=11), answer=AsyncMock())


def test_callback_deletes_and_rewrites_message(db):
    from handlers.undo_handler import handle_undo_callback
    _seed(["A"])
    token = undo.register([{"transaction_id": "A", "amount": 2900, "merchant": "Food Solutions"}])
    cb = _callback(token)
    asyncio.run(handle_undo_callback(cb))
    assert _rows(db) == []
    text = cb.message.edit_text.call_args.args[0]
    assert text.startswith("↩️ Отменено") and "2 900" in text
    assert cb.message.edit_text.call_args.kwargs["reply_markup"] is None
    cb.answer.assert_awaited()


def test_callback_second_press_is_harmless(db):
    from handlers.undo_handler import handle_undo_callback
    _seed(["A", "B"])
    token = undo.register([{"transaction_id": "A", "amount": 1}])
    asyncio.run(handle_undo_callback(_callback(token)))
    cb = _callback(token)
    asyncio.run(handle_undo_callback(cb))
    assert _rows(db) == ["B"]
    assert cb.answer.call_args.args[0] == "Уже отменено"


def test_callback_expired_shows_alert(db):
    from handlers.undo_handler import handle_undo_callback
    cb = _callback("zzz")
    asyncio.run(handle_undo_callback(cb))
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    cb.message.edit_text.assert_not_awaited()


def test_callback_sheet_error_does_not_claim_success(db, monkeypatch):
    from handlers.undo_handler import handle_undo_callback
    _seed(["A"])
    token = undo.register([{"transaction_id": "A", "amount": 1}])
    monkeypatch.setattr(sheets, "delete_transactions_by_ids", lambda ids: (_ for _ in ()).throw(RuntimeError("net")))
    cb = _callback(token)
    asyncio.run(handle_undo_callback(cb))
    assert _rows(db) == ["A"]
    cb.message.edit_text.assert_not_awaited()
    assert cb.answer.call_args.kwargs.get("show_alert") is True


# ── подключение к ответам ──────────────────────────────────────────────────

def test_safe_answer_markup_only_on_last_part():
    msg = SimpleNamespace(chat=SimpleNamespace(id=-100), answer=AsyncMock())
    asyncio.run(safe_answer(msg, "строка\n" * 1500, reply_markup="KB"))
    calls = msg.answer.call_args_list
    assert len(calls) > 1
    assert all("reply_markup" not in c.kwargs for c in calls[:-1])
    assert calls[-1].kwargs["reply_markup"] == "KB"


@pytest.fixture
def handler(monkeypatch, db):
    import config
    monkeypatch.setattr(config, "VLAD_TELEGRAM_ID", "11")
    monkeypatch.setattr(config, "DIANA_TELEGRAM_ID", "22")
    from services.preflight import initialize_optional
    initialize_optional()
    from handlers import text_handler as h
    monkeypatch.setattr(h, "add_chat_message", lambda *args: None)
    monkeypatch.setattr(h, "get_chat_history", lambda *args: [])
    return h


def _message(text, mid=50):
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=11, first_name="Влад"),
                           chat=SimpleNamespace(id=-100, type="supergroup"), message_id=mid,
                           answer=AsyncMock(), bot=SimpleNamespace(send_chat_action=AsyncMock()))


def test_text_transaction_reply_has_undo_button_that_works(handler, db, monkeypatch):
    from handlers.undo_handler import handle_undo_callback
    monkeypatch.setattr(handler, "parse_and_analyze", AsyncMock(return_value={
        "intent": "transaction",
        "transaction": {"amount": 1200, "bank": "Kaspi", "category": "Транспорт и авто", "type": "РАСХОД"},
        "reply": ""}))
    msg = _message("такси 1200")
    asyncio.run(handler.handle_text(msg))
    assert len(db.worksheet("Transactions").data) == 2
    kb = msg.answer.call_args.kwargs["reply_markup"]
    callback_data = kb.inline_keyboard[0][0].callback_data
    cb = _callback(callback_data.split(":", 1)[1])
    asyncio.run(handle_undo_callback(cb))
    assert len(db.worksheet("Transactions").data) == 1


def test_failed_write_has_no_undo_button(handler, db, monkeypatch):
    monkeypatch.setattr(handler, "parse_and_analyze", AsyncMock(return_value={
        "intent": "transaction",
        "transaction": {"amount": 1200, "bank": "Kaspi", "category": "Транспорт и авто", "type": "РАСХОД"},
        "reply": ""}))
    monkeypatch.setattr(handler, "append_transaction", lambda tx: (_ for _ in ()).throw(RuntimeError("down")))
    msg = _message("такси 1200")
    asyncio.run(handler.handle_text(msg))
    assert "reply_markup" not in msg.answer.call_args.kwargs

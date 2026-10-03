"""«✏️ Изменить»: правка записи кнопками. Офлайн: Sheets — фейк, Telegram — заглушки
(живой Telegram не проверяется)."""
import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import edit_buttons as eb
from services import sheets, state, tx_edit, undo


def _seed(tid="A", **extra):
    data = {"transaction_id": tid, "amount": 1450, "bank": "Kaspi", "category": "Еда и продукты",
            "subcategory": "Супермаркет и рынок", "merchant": "Magnum", "user": "Влад",
            "user_comment": "продукты", "funds_type": "Собственные"}
    data.update(extra)
    sheets.append_transaction(data)
    return tid


def _rec(tid):
    return sheets.get_transactions_by_ids([tid])[tid]


def _callback(data, uid=11, mid=70):
    msg = SimpleNamespace(chat=SimpleNamespace(id=-100), message_id=mid, edit_text=AsyncMock())
    return SimpleNamespace(data=data, message=msg, from_user=SimpleNamespace(id=uid), answer=AsyncMock())


def _press(data, **kw):
    cb = _callback(data, **kw)
    asyncio.run(eb.handle_edit_callback(cb))
    return cb


def _bank_index(label):
    return [row[0] for row in tx_edit.BANK_CHOICES].index(label)


def _shown(cb):
    return cb.message.edit_text.call_args.args[0]


def _buttons(cb):
    kb = cb.message.edit_text.call_args.kwargs["reply_markup"]
    return [b for row in kb.inline_keyboard for b in row]


def _input_message(text, uid=11):
    return SimpleNamespace(text=text, chat=SimpleNamespace(id=-100), from_user=SimpleNamespace(id=uid, first_name="Влад"),
                           answer=AsyncMock(), bot=SimpleNamespace(edit_message_text=AsyncMock()))


# ── таблица ────────────────────────────────────────────────────────────────

def test_update_fields_returns_old_and_new(db):
    _seed()
    old, new = sheets.update_transaction_fields("A", {"amount": 1750, "merchant": "Small"})
    assert str(old["amount"]) == "1450" and new["merchant"] == "Small"
    assert str(_rec("A")["amount"]) == "1750" and _rec("A")["merchant"] == "Small"
    assert _rec("A")["bank"] == "Kaspi"            # остальное не тронуто


def test_update_fields_unknown_id_and_column(db):
    _seed()
    assert sheets.update_transaction_fields("NOPE", {"amount": 1}) is None
    with pytest.raises(ValueError):
        sheets.update_transaction_fields("A", {"no_such_column": 1})
    assert str(_rec("A")["amount"]) == "1450"


# ── разбор значений ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [("1750", 1750), ("1 750 ₸", 1750), ("2,5 тыс", 2500), ("2к", 2000),
                                           ("1750 тг", 1750), ("99,5", 99.5)])
def test_amount_input_ok(text, expected):
    assert tx_edit.parse_amount_input(text) == (expected, None)


@pytest.mark.parametrize("text", ["кофе 500", "", "abc", "0", "-5", "5000000000"])
def test_amount_input_rejected(text):
    value, error = tx_edit.parse_amount_input(text)
    assert value is None and error


def test_date_input():
    now = datetime.datetime(2026, 10, 3, 16, 0, tzinfo=tx_edit.ASTANA_TZ)
    assert tx_edit.parse_date_input("06.09.2026 15:41", now=now)[0] == "2026-09-06 15:41:00"
    # дата без времени сохраняет время записи
    assert tx_edit.parse_date_input("06.09.2026", current="2026-09-01 12:30:15", now=now)[0] == "2026-09-06 12:30:15"
    assert tx_edit.parse_date_input("вчера", current="2026-10-03 09:10:00", now=now)[0] == "2026-10-02 09:10:00"
    assert tx_edit.parse_date_input("вчера 21:05", now=now)[0] == "2026-10-02 21:05:00"
    assert tx_edit.parse_date_input("завтра", now=now)[1]                      # не понял
    assert tx_edit.parse_date_input("10.10.2026", now=now)[1]                  # будущее
    assert tx_edit.parse_date_input("01.01.2010", now=now)[1]                  # слишком давно


def test_text_input_clear_and_limit():
    assert tx_edit.parse_text_input("  Мой   магазин ") == ("Мой магазин", None)
    assert tx_edit.parse_text_input("-") == ("", None)
    assert tx_edit.parse_text_input("x" * 500)[1]


# ── кнопки ─────────────────────────────────────────────────────────────────

def test_open_shows_field_menu(db):
    _seed()
    token = undo.register([{"transaction_id": "A", "amount": 1450}])
    cb = _press(f"edt:o:{token}")
    assert "Что изменить" in _shown(cb) and "1 450" in _shown(cb)
    labels = [b.text for b in _buttons(cb)]
    for needed in ("Банк", "Категория", "Сумма", "Магазин", "Комментарий", "Дата", "Кто", "Нужность"):
        assert any(needed in label for label in labels)
    assert all(len(b.callback_data.encode()) <= 64 for b in _buttons(cb))


def test_bank_choice_updates_bank_source_and_funds_type(db):
    _seed()
    token = undo.register([{"transaction_id": "A", "amount": 1450}])
    cb = _press(f"edt:f:{token}:0:b")
    assert "банк" in _shown(cb).lower()
    assert len(_buttons(cb)) == len(tx_edit.BANK_CHOICES) + 1                 # все банки + «Назад»
    _press(f"edt:v:{token}:0:b:{_bank_index('Kaspi Red')}")
    rec = _rec("A")
    assert (rec["bank"], rec["source"], rec["funds_type"]) == ("Kaspi", "Kaspi Red", "Рассрочка")
    _press(f"edt:v:{token}:0:b:{_bank_index('Kaspi Gold')}")
    rec = _rec("A")
    assert (rec["source"], rec["funds_type"]) == ("Kaspi Gold", "Собственные")
    _press(f"edt:v:{token}:0:b:{_bank_index('Наличные')}")
    rec = _rec("A")
    assert rec["resource"] == "Наличные" and rec["bank"] == "Не указан"


def test_category_then_subcategory(db):
    _seed()
    token = undo.register([{"transaction_id": "A", "amount": 1450}])
    cb = _press(f"edt:f:{token}:0:c")
    assert len(_buttons(cb)) == 19 + 1
    k = tx_edit.EXPENSE_CATEGORIES.index("Транспорт и авто")
    cb = _press(f"edt:v:{token}:0:c:{k}")
    rec = _rec("A")
    assert rec["category"] == "Транспорт и авто" and rec["subcategory"] == "Общественный транспорт"
    assert "подкатегорию" in _shown(cb)
    _press(f"edt:s:{token}:0:1")
    assert _rec("A")["subcategory"] == "Такси"


def test_who_and_necessity(db):
    _seed()
    token = undo.register([{"transaction_id": "A", "amount": 1450}])
    _press(f"edt:v:{token}:0:u:1")
    _press(f"edt:v:{token}:0:w:0")
    rec = _rec("A")
    assert rec["user"] == "Диана" and rec["necessity"] == "Need"


def test_amount_via_text_input(db):
    _seed()
    token = undo.register([{"transaction_id": "A", "amount": 1450, "merchant": "Magnum"}])
    cb = _press(f"edt:f:{token}:0:a")
    assert "новую сумму" in _shown(cb)
    # «кофе 500» — не сумма: правка не применяется, бот просит написать число
    bad = _input_message("кофе 500")
    assert asyncio.run(eb.handle_edit_input(bad, "кофе 500")) is True
    assert str(_rec("A")["amount"]) == "1450" and bad.answer.await_count == 1
    ok = _input_message("1750")
    assert asyncio.run(eb.handle_edit_input(ok, "1750")) is True
    assert str(_rec("A")["amount"]) == "1750"
    sent = ok.bot.edit_message_text.call_args.kwargs
    assert sent["message_id"] == 70 and "1 750" in sent["text"]
    # после успеха ввод закрыт — следующее сообщение не перехватывается
    assert asyncio.run(eb.handle_edit_input(_input_message("привет"), "привет")) is False
    # и «Отменить» теперь показывает новую сумму
    assert "1 750" in undo.perform_undo(token)["summary"]


def test_text_fields_and_clearing(db):
    _seed()
    token = undo.register([{"transaction_id": "A", "amount": 1450}])
    _press(f"edt:f:{token}:0:m")
    asyncio.run(eb.handle_edit_input(_input_message("Small"), "Small"))
    _press(f"edt:f:{token}:0:n")
    asyncio.run(eb.handle_edit_input(_input_message("-"), "-"))
    _press(f"edt:f:{token}:0:d")
    asyncio.run(eb.handle_edit_input(_input_message("06.09.2026 15:41"), "06.09.2026 15:41"))
    rec = _rec("A")
    assert rec["merchant"] == "Small" and rec["user_comment"] == "" and rec["date"] == "2026-09-06 15:41:00"


def test_cancel_input_stops_intercepting(db):
    _seed()
    token = undo.register([{"transaction_id": "A", "amount": 1450}])
    _press(f"edt:f:{token}:0:a")
    _press(f"edt:x:{token}:0")
    assert asyncio.run(eb.handle_edit_input(_input_message("1750"), "1750")) is False
    assert str(_rec("A")["amount"]) == "1450"


def test_input_expires(db, monkeypatch):
    _seed()
    token = undo.register([{"transaction_id": "A", "amount": 1450}])
    _press(f"edt:f:{token}:0:a")
    monkeypatch.setattr(eb, "INPUT_TTL_SECONDS", -1)
    assert asyncio.run(eb.handle_edit_input(_input_message("1750"), "1750")) is False
    assert str(_rec("A")["amount"]) == "1450"


def test_input_from_other_person_not_intercepted(db):
    _seed()
    token = undo.register([{"transaction_id": "A", "amount": 1450}])
    _press(f"edt:f:{token}:0:a", uid=11)
    assert asyncio.run(eb.handle_edit_input(_input_message("1750", uid=22), "1750")) is False
    assert str(_rec("A")["amount"]) == "1450"


def test_multi_position_receipt_edits_only_chosen(db):
    _seed("M0", amount=1000, merchant="Молоко")
    _seed("M1", amount=500, merchant="Хлеб")
    token = undo.register([{"transaction_id": "M0", "amount": 1000}, {"transaction_id": "M1", "amount": 500}])
    cb = _press(f"edt:o:{token}")
    assert "позицию" in _shown(cb) and len(_buttons(cb)) == 2
    cb = _press(f"edt:p:{token}:1")
    assert "Хлеб" in _shown(cb)
    _press(f"edt:v:{token}:1:u:1")
    assert _rec("M1")["user"] == "Диана" and _rec("M0")["user"] == "Влад"


def test_cancelled_record_cannot_be_edited(db):
    _seed()
    token = undo.register([{"transaction_id": "A", "amount": 1450}])
    undo.perform_undo(token)
    cb = _press(f"edt:o:{token}")
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    cb.message.edit_text.assert_not_awaited()


def test_unknown_token_and_missing_row(db):
    cb = _press("edt:o:zzz")
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    token = undo.register([{"transaction_id": "GONE", "amount": 1}])
    cb = _press(f"edt:o:{token}")
    assert cb.answer.call_args.kwargs.get("show_alert") is True


def test_sheet_error_does_not_claim_success(db, monkeypatch):
    _seed()
    token = undo.register([{"transaction_id": "A", "amount": 1450}])
    with monkeypatch.context() as broken:
        broken.setattr(sheets, "update_transaction_fields",
                       lambda tid, changes: (_ for _ in ()).throw(RuntimeError("quota")))
        cb = _press(f"edt:v:{token}:0:u:1")
    cb.message.edit_text.assert_not_awaited()
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    assert _rec("A")["user"] == "Влад"


def test_done_returns_main_buttons(db):
    _seed()
    token = undo.register([{"transaction_id": "A", "amount": 1450}])
    cb = _press(f"edt:d:{token}:0")
    datas = [b.callback_data for b in _buttons(cb)]
    assert datas == [f"undo:{token}", f"edt:o:{token}"]


def test_reply_keyboard_has_edit_button():
    kb = undo.build_keyboard([{"transaction_id": "A", "amount": 1}])
    texts = [b.text for b in kb.inline_keyboard[0]]
    assert any("Отменить" in t for t in texts) and any("Изменить" in t for t in texts)


# ── перехват текста в обработчике сообщений ────────────────────────────────

def test_text_handler_consumes_pending_input(db, monkeypatch):
    import config
    monkeypatch.setattr(config, "VLAD_TELEGRAM_ID", "11")
    monkeypatch.setattr(config, "DIANA_TELEGRAM_ID", "22")
    from services.preflight import initialize_optional
    initialize_optional()
    from handlers import text_handler as h
    monkeypatch.setattr(h, "add_chat_message", lambda *args: None)
    parse = AsyncMock()
    monkeypatch.setattr(h, "parse_and_analyze", parse)
    _seed()
    token = undo.register([{"transaction_id": "A", "amount": 1450}])
    _press(f"edt:f:{token}:0:a")
    msg = SimpleNamespace(text="2000", from_user=SimpleNamespace(id=11, first_name="Влад"),
                          chat=SimpleNamespace(id=-100, type="supergroup"), message_id=90,
                          answer=AsyncMock(), bot=SimpleNamespace(send_chat_action=AsyncMock(), edit_message_text=AsyncMock()))
    asyncio.run(h.handle_text(msg))
    assert str(_rec("A")["amount"]) == "2000" and not parse.called

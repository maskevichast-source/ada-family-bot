import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import loan_fix, state
from services.loan_rules import LOAN_CATEGORY, LOAN_SUBCATEGORY, looks_like_repayment
from services.preflight import TRANSACTION_HEADERS
from test_handlers import handler  # noqa: F401
from test_media_schedulers import app  # noqa: F401  (фикстура app)


# ---------- правило ----------

@pytest.mark.parametrize("merchant,comment,sub,funds,expected", [
    ("Погашение кредита Forte", "Погашение основного кредита", "Гаджеты и техника", "Собственные", True),
    ("Kaspi Кредит", "Погашение рассрочки за технику", "", "Собственные", True),
    ("По номеру телефона", "Погашение кредитной задолженности по карте ozen", "Банковские комиссии", "Собственные", True),
    ("Kaspi Red", "", LOAN_SUBCATEGORY, "Собственные", True),              # уже правильная подкатегория
    ("7DAN", "Сладкое, оплата картой ozen", "Супермаркет и рынок", "Рассрочка", False),       # покупка в рассрочку
    ("Магазин", "Погашение кредита", "", "Кредитные", False),
    ("", "Погашение долга Саше", "", "Собственные", False),                # личный долг — не кредит
    ("Magnum", "продукты", "Супермаркет и рынок", "Собственные", False),
    ("Kaspi Red", "", "", "Собственные", False),                           # без «погашения» и без подкатегории — не гадаем
])
def test_repayment_rule(merchant, comment, sub, funds, expected):
    assert looks_like_repayment(merchant, comment, sub, funds) is expected


# ---------- правка таблицы ----------

def _row(tid, amount, merchant, cat, sub, comment="", funds="Собственные", typ="РАСХОД", ai=""):
    v = {"transaction_id": tid, "date": "2026-09-08 10:00:00", "user": "Влад", "type": typ, "amount": amount,
         "currency": "KZT", "funds_type": funds, "category": cat, "subcategory": sub, "merchant": merchant,
         "user_comment": comment, "ai_comment": ai}
    return [v.get(h, "") for h in TRANSACTION_HEADERS]


def _seed(db):
    ws = db.sheets["Transactions"]
    for r in (
        _row("A", 151304, "Погашение кредита Forte", "Электроника и техника", "Гаджеты и техника", "Погашение основного кредита", ai="Ок."),
        _row("B", 36750, "Kaspi Кредит", LOAN_CATEGORY, LOAN_SUBCATEGORY, "Погашение кредита"),         # уже верно
        _row("C", 4981, "7DAN", "Еда и продукты", "Супермаркет и рынок", "Погашение кредита картой", funds="Рассрочка"),
        _row("D", 15000, "", "Подарки, праздники и благотворительность", "Подарки", "Погашение долга Саше"),
        _row("E", 500000, "Возврат", "Возврат долга", "", "Погашение кредита", typ="ДОХОД"),
        _row("F", 131347, "По номеру телефона", LOAN_CATEGORY, "Банковские комиссии", "Погашение кредитной задолженности по карте ozen"),
    ):
        ws.append_row(r)
    return ws


def test_finds_only_real_misfiled_repayments(db):
    _seed(db)
    changes = loan_fix.find_changes()
    assert [c["id"] for c in changes] == ["A", "F"]
    assert changes[0]["old_category"] == "Электроника и техника" and changes[1]["old_subcategory"] == "Банковские комиссии"
    text = loan_fix.preview_text(changes)
    assert "Нашла 2 платежа" in text and "282 651" in text and "/fix_loans да" in text and "Пока ничего не изменено" in text
    assert "Все погашения" in loan_fix.preview_text([])


def test_apply_updates_cells_is_idempotent_and_undoable(db):
    ws = _seed(db)
    before = [list(r) for r in ws.get_all_values()]
    assert loan_fix.apply_changes(loan_fix.find_changes()) == 2
    rows = {r[0]: r for r in ws.get_all_values()[1:]}
    assert rows["A"][10] == LOAN_CATEGORY and rows["A"][11] == LOAN_SUBCATEGORY
    assert "перекатегорировано: было Электроника и техника › Гаджеты и техника" in rows["A"][15] and rows["A"][15].startswith("Ок.")
    assert rows["F"][10] == LOAN_CATEGORY and rows["F"][11] == LOAN_SUBCATEGORY
    for untouched in ("B", "C", "D", "E"):                                  # остальные строки не тронуты
        assert rows[untouched] == next(r for r in before if r[0] == untouched)
    assert loan_fix.find_changes() == []                                    # повторный запуск ничего не находит
    assert "Вернула прежние категории у 2" in loan_fix.undo_last()
    assert ws.get_all_values() == before
    assert "Нечего откатывать" in loan_fix.undo_last()


def test_apply_finds_rows_by_id_even_if_rows_moved(db):
    ws = _seed(db)
    changes = loan_fix.find_changes()
    ws.rows.insert(1, _row("NEW", 1, "X", "Еда и продукты", "Супермаркет и рынок")) if hasattr(ws, "rows") else None
    if not hasattr(ws, "rows"):
        ws.data.insert(1, _row("NEW", 1, "X", "Еда и продукты", "Супермаркет и рынок"))     # вставка строки сверху
    loan_fix.apply_changes(changes)
    rows = {r[0]: r for r in ws.get_all_values()[1:]}
    assert rows["A"][10] == LOAN_CATEGORY and rows["NEW"][10] == "Еда и продукты"


# ---------- команда ----------

def _say(app, args, monkeypatch=None):
    msg = SimpleNamespace(chat=SimpleNamespace(id=1, type="private"), answer=AsyncMock(), message_id=1)
    asyncio.run(app.cmd_fix_loans(msg, SimpleNamespace(args=args)))
    return msg.answer.call_args.args[0]


def test_command_preview_then_apply_then_undo(app, db, monkeypatch):
    ws = _seed(db)
    monkeypatch.delenv("BACKUP_FOLDER_ID", raising=False)
    assert "Нашла 2 платежа" in _say(app, "")
    assert ws.get_all_values()[1][10] == "Электроника и техника"               # предпросмотр ничего не пишет
    said = _say(app, "да")
    assert "Исправила 2 записей" in said and "/fix_loans откат" in said and "/limits_plan" in said
    assert ws.get_all_values()[1][10] == LOAN_CATEGORY
    assert "Менять нечего" in _say(app, "да")
    assert "Вернула прежние категории у 2" in _say(app, "откат")
    assert ws.get_all_values()[1][10] == "Электроника и техника"


def test_command_makes_backup_first_and_stops_if_it_fails(app, db, monkeypatch):
    ws = _seed(db)
    monkeypatch.setenv("BACKUP_FOLDER_ID", "F")
    monkeypatch.setattr(app.backup_module, "make_backup", lambda now: {"ok": False, "reason": "нет прав"})
    said = _say(app, "да")
    assert "Не применяю" in said and "нет прав" in said and "Ничего не изменено" in said
    assert ws.get_all_values()[1][10] == "Электроника и техника"               # без копии таблицу не трогаем
    monkeypatch.setattr(app.backup_module, "make_backup", lambda now: {"ok": True, "title": "T"})
    assert "Резервная копия таблицы сделана" in _say(app, "да")
    assert ws.get_all_values()[1][10] == LOAN_CATEGORY


def test_command_error_is_reported_without_changes(app, db, monkeypatch):
    monkeypatch.setattr(app.loan_fix, "find_changes", lambda: 1 / 0)
    assert "Ничего не изменено" in _say(app, "да")

"""Разбивка записи «по-человечески»: «разбей последний транш, стики 1230 отдельно».

Регрессия: Ада отвечала «Разбила…», хотя в таблице ничего не менялось (при неполных
данных от модели в чат уходил её собственный reply).
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from services import sheets
from test_handlers import handler, message  # noqa: F401  (фикстура handler)


def _seed(db):
    sheets.append_transaction({"transaction_id": "MEDIA_1_1", "amount": 3360, "user": "Диана",
                               "category": "Еда и продукты", "subcategory": "Супермаркет и рынок",
                               "merchant": "Аягоз", "bank": "Kaspi", "source": "Kaspi Gold",
                               "user_comment": "колбаса, хлеб и стики"})
    sheets.append_transaction({"transaction_id": "TRX_V", "amount": 1700, "user": "Влад",
                               "category": "Подарки, праздники и благотворительность"})   # новее, но чужая
    db.worksheet("Transactions").id = 123
    requests = []
    db.batch_update = lambda payload: requests.append(payload)
    return requests


def _rows(requests):
    cells = requests[0]["requests"][1]["updateCells"]
    def val(row, i):
        v = row["values"][i]["userEnteredValue"]
        return v.get("numberValue", v.get("stringValue"))
    return cells["start"]["rowIndex"], [
        {"id": val(r, 0), "amount": val(r, 4), "cat": val(r, 10), "sub": val(r, 11), "merchant": val(r, 12),
         "comm": val(r, 14), "bank": val(r, 6), "user": val(r, 2)} for r in cells["rows"]]


def _say(handler, monkeypatch, parsed, text="разбей последний транш, стики 1230 тенге отдельно", uid=22):
    monkeypatch.setattr(handler, "parse_and_analyze", AsyncMock(return_value=parsed))
    m = message(text, uid=uid)
    asyncio.run(handler.handle_text(m))
    return " ".join(str(m.answer.call_args.args[0]).split())


def test_dianas_case_splits_her_last_record_and_reports_real_result(handler, db, monkeypatch):
    requests = _seed(db)
    said = _say(handler, monkeypatch, {
        "intent": "split_transaction",
        "split": {"amount": None, "parts": [
            {"amount": 1230, "category": "Алкоголь, табак и энергетики", "comment": "стики"},
            {"amount": None, "category": "", "comment": "колбаса и хлеб"}]},
        "reply": "Разбила: 1 230 ₸ — стики, остальные 2 130 ₸ — колбаса и хлеб."})
    assert len(requests) == 1
    row_index, rows = _rows(requests)
    assert row_index == 1                                   # запись Дианы, а не более новая запись Влада
    assert [r["amount"] for r in rows] == [1230, 2130]
    assert rows[0]["cat"] == "Алкоголь, табак и энергетики"
    assert rows[1]["cat"] == "Еда и продукты" and rows[1]["sub"] == "Супермаркет и рынок"
    assert rows[1]["comm"] == "колбаса и хлеб" and rows[0]["comm"] == "стики"
    assert all(r["merchant"] == "Аягоз" and r["bank"] == "Kaspi" and r["user"] == "Диана" for r in rows)
    assert [r["id"] for r in rows] == ["MEDIA_1_1_1", "MEDIA_1_1_2"]      # след исходного ID сохранён
    assert said.startswith("Разделила запись на 3 360") and "1 230" in said and "2 130" in said


def test_only_one_part_named_remainder_inherits_original(handler, db, monkeypatch):
    requests = _seed(db)
    said = _say(handler, monkeypatch, {
        "intent": "split_transaction",
        "split": {"parts": [{"amount": 1230, "category": "Алкоголь, табак и энергетики", "comment": "стики"}]}})
    _, rows = _rows(requests)
    assert [r["amount"] for r in rows] == [1230, 2130]
    assert rows[1]["cat"] == "Еда и продукты" and rows[1]["comm"] == "колбаса, хлеб и стики"
    assert "Разделила запись" in said


def test_models_false_success_reply_is_never_sent_when_nothing_was_written(handler, db, monkeypatch):
    requests = _seed(db)
    said = _say(handler, monkeypatch, {
        "intent": "split_transaction", "split": {"amount": None, "parts": []},
        "reply": "Разбила: стики отдельно, остальное колбаса и хлеб. Вместо одной записи теперь две."})
    assert requests == []
    assert said.startswith("Не разделила:") and "Разбила" not in said and "осталась как была" in said
    assert len(sheets._get_all_records_safe(db.worksheet("Transactions"))) == 2      # таблица не тронута


def test_user_without_own_records_gets_honest_refusal(handler, db, monkeypatch):
    requests = _seed(db)
    said = _say(handler, monkeypatch, {"intent": "split_transaction",
                "split": {"parts": [{"amount": 100, "category": "Питомцы"}]}}, uid=11)       # Влад: последняя = 1700
    row_index, rows = _rows(requests)
    assert row_index == 2 and [r["amount"] for r in rows] == [100, 1600]                     # его запись, не Дианы
    assert said.startswith("Разделила запись на 1 700")


def test_named_amount_finds_record_even_of_another_user(db):
    requests = _seed(db)
    out = sheets.split_last_transaction("Влад", 3360, [{"amount": 60}])
    assert out["ok"] and out["total"] == 3360 and [p["amount"] for p in out["parts"]] == [60, 3300]
    assert _rows(requests)[0] == 1


@pytest.mark.parametrize("parts,target,fragment", [
    ([{"amount": 3360}], None, "меньше всей записи"),                       # вся сумма одной части — остаток 0
    ([{"amount": 5000}], None, "меньше всей записи"),                       # больше всей записи
    ([{"amount": 1000}, {"amount": 1000}], None, "не равны записи"),        # суммы не сходятся
    ([{"amount": None}, {"amount": None}], None, "не названа сумма"),
    ([], None, "не поняла"),
    ([{"amount": 1}, {"amount": 2}, {"amount": 3}], None, "не поняла"),
    ([{"amount": 10}], 999999, "не нашла расходную запись"),
])
def test_split_refuses_and_writes_nothing(db, parts, target, fragment):
    requests = _seed(db)
    out = sheets.split_last_transaction("Диана", target, parts)
    assert out["ok"] is False and fragment in out["reason"]
    assert requests == []


def test_income_rows_are_never_split(db):
    sheets.append_transaction({"transaction_id": "INC", "amount": 500000, "user": "Диана", "type": "ДОХОД"})
    out = sheets.split_last_transaction("Диана", None, [{"amount": 1000}])
    assert out["ok"] is False and "расходную" in out["reason"]


def test_sheet_error_keeps_record_and_says_so(db):
    _seed(db)
    def boom(payload): raise RuntimeError("quota")
    db.batch_update = boom
    out = sheets.split_last_transaction("Диана", None, [{"amount": 100}])
    assert out["ok"] is False and "осталась как была" in out["reason"]


# ---------- тот же дефект в соседних ветках: ответ модели идёт в чат только после реальной записи ----------

LIE = "Готово, всё записала!"


def _said(handler, monkeypatch, parsed, text="сообщение"):
    monkeypatch.setattr(handler, "parse_and_analyze", AsyncMock(return_value=dict(parsed, reply=LIE)))
    m = message(text, uid=22)
    asyncio.run(handler.handle_text(m))
    return " ".join(str(m.answer.call_args.args[0]).split())


@pytest.mark.parametrize("parsed,fragment", [
    ({"intent": "add_installment", "installment": {}}, "Не записала рассрочку"),
    ({"intent": "close_installment", "search_query": ""}, "Не нашла такую рассрочку"),
    ({"intent": "close_installment", "search_query": "несуществующая"}, "Не нашла такую рассрочку"),
    ({"intent": "cancel_subscription", "subscription_name": ""}, "Не нашла такую подписку"),
    ({"intent": "cancel_subscription", "subscription_name": "Несуществующая"}, "Не нашла такую подписку"),
    ({"intent": "add_shopping", "shopping_items": []}, "Не поняла, что добавить"),
    ({"intent": "add_trip", "destination": ""}, "Не записала поездку"),
])
def test_write_branches_never_send_models_false_success(handler, db, monkeypatch, parsed, fragment):
    said = _said(handler, monkeypatch, parsed)
    assert LIE not in said and fragment in said


def test_write_branches_send_models_reply_after_real_write(handler, db, monkeypatch):
    assert _said(handler, monkeypatch, {"intent": "add_shopping", "shopping_items": ["молоко"]}) == LIE
    assert _said(handler, monkeypatch, {"intent": "add_trip", "destination": "Алматы", "dates": "10-12 окт",
                                        "budget": 100000, "notes": ""}) == LIE
    sheets.add_or_update_subscription("Netflix", 4500, "Kaspi", 5)
    assert _said(handler, monkeypatch, {"intent": "cancel_subscription", "subscription_name": "Netflix"}) == LIE

import datetime
import pytest

from services import data_fix, sheets, state
from services.preflight import TRANSACTION_HEADERS


def _row(tid, **kw):
    v = {"transaction_id": tid, "date": "2026-09-10 10:00:00", "user": "Влад", "type": "РАСХОД", "amount": 1000,
         "currency": "KZT", "bank": "Kaspi", "source": "Kaspi Gold", "funds_type": "Собственные", "resource": "Карта",
         "category": "Еда и продукты", "subcategory": "Супермаркет и рынок", "merchant": "Magnum",
         "necessity": "Need", "user_comment": "", "ai_comment": ""}
    v.update(kw)
    return [v[h] for h in TRANSACTION_HEADERS]


@pytest.fixture
def seeded(db, monkeypatch):
    ws = db.sheets["Transactions"]
    for r in (
        _row("OZ", bank="Home Credit", source="Ozen"),                                   # рассрочка, а «Собственные»
        _row("OZ_OK", bank="Home Credit", source="Ozen", funds_type="Рассрочка"),
        _row("OZ_LOAN", bank="Home Credit", source="Ozen", subcategory="Кредиты и рассрочки",
             category="Финансовые расходы и переводы"),                                   # платёж по кредиту — не трогаем
        _row("SRC", bank="BCC", source="Основная карта"),
        _row("CASH", bank="Не указан", source="Основная карта", resource="Наличные"),
        _row("INC", type="ДОХОД", category="Зарплата", subcategory="", necessity="Need", merchant="Idea"),
        _row("INC_OK", type="ДОХОД", category="Премия и бонусы", subcategory="Премия и бонусы", necessity=""),
        _row("TXT", date="2026-09-23 19:09:57"),
        _row("DT", date="2026-09-20 10:00:00"),
        _row("T1", amount=1230, merchant="Tele2", user_comment="Оплата связи", date="2026-09-22 12:46:27"),
        _row("T2", amount=1230, merchant="Tele2", user_comment="Автосписание: Tele2", date="2026-09-22 12:46:51"),
    ):
        ws.append_row(r)
    # колонка B: 1 = шапка; строка — текст, число — настоящая дата
    raw = ["date"] + [("2026-09-23 19:09:57" if r[0] == "TXT" else 46000.5) for r in ws.data[1:]]
    monkeypatch.setattr(data_fix, "raw_date_cells", lambda w: raw)
    ws.format = lambda *a, **k: ws.__dict__.setdefault("formats", []).append((a, k))
    return db


def test_preview_finds_everything_and_writes_nothing(seeded):
    before = [list(r) for r in seeded.sheets["Transactions"].data]
    found = data_fix.find_changes()
    ids = {f["id"] for f in found["fields"]}
    assert ids == {"OZ", "SRC", "INC"}                       # OZ_OK, OZ_LOAN, CASH, INC_OK не трогаем
    assert [d["id"] for d in found["dates"]] == ["TXT"]
    assert [d["id"] for d in found["duplicates"]] == ["T2"]
    text = data_fix.preview_text(found)
    assert "/fix_data да" in text and "/fix_data удалить T2" in text
    assert seeded.sheets["Transactions"].data == before


def test_apply_and_rollback(seeded):
    ws = seeded.sheets["Transactions"]
    h = TRANSACTION_HEADERS
    done = data_fix.apply_changes(data_fix.find_changes())
    assert done == {"fields": 3, "dates": 1}
    by_id = {r[0]: dict(zip(h, r)) for r in ws.data[1:]}
    assert by_id["OZ"]["funds_type"] == "Рассрочка"
    assert by_id["OZ_LOAN"]["funds_type"] == "Собственные"
    assert by_id["SRC"]["source"] == "BCC Pay"
    assert by_id["CASH"]["source"] == "Основная карта"
    assert by_id["INC"]["necessity"] == "" and by_id["INC"]["subcategory"] == "Зарплата"
    assert getattr(ws, "formats", None)                       # формат колонки даты выставлен
    assert "Вернула" in data_fix.undo_last()
    by_id = {r[0]: dict(zip(h, r)) for r in ws.data[1:]}
    assert by_id["OZ"]["funds_type"] == "Собственные" and by_id["SRC"]["source"] == "Основная карта"
    assert by_id["INC"]["necessity"] == "Need" and by_id["INC"]["subcategory"] == ""
    assert "Нечего" in data_fix.undo_last()


def test_apply_twice_is_noop(seeded):
    data_fix.apply_changes(data_fix.find_changes())
    again = data_fix.find_changes()
    assert not again["fields"]


def test_delete_one_exact_id_only(seeded):
    ws = seeded.sheets["Transactions"]
    ws.append_row(_row("T2_1", amount=5))                      # похожий ID — не должен пострадать
    msg = data_fix.delete_one("T2")
    assert "Удалила" in msg
    ids = [r[0] for r in ws.data[1:]]
    assert "T2" not in ids and "T2_1" in ids and "T1" in ids
    assert "ничего не удалила" in data_fix.delete_one("NOPE")
    assert "Укажи ID" in data_fix.delete_one("")


def test_clean_table(db):
    assert data_fix.preview_text({"fields": [], "dates": [], "duplicates": []}).startswith("В таблице всё чисто")


# ---------- запись новых строк и подписки ----------

def test_income_written_without_necessity_and_with_subcategory(db):
    sheets.append_transaction({"type": "ДОХОД", "amount": 250000, "category": "Зарплата", "necessity": "Need",
                               "bank": "BCC"})
    row = dict(zip(TRANSACTION_HEADERS, db.sheets["Transactions"].data[-1]))
    assert row["necessity"] == "" and row["subcategory"] == "Зарплата"


def test_date_cell_rewritten_as_datetime(db):
    ws = db.sheets["Transactions"]
    calls = []
    orig_append = ws.append_row
    ws.append_row = lambda row, **kw: (orig_append(row, **kw), {"updates": {"updatedRange": "Transactions!A7:P7"}})[1]
    ws.update = lambda **kw: calls.append(kw)
    sheets.append_transaction({"type": "РАСХОД", "amount": 500, "category": "Еда и продукты", "bank": "Kaspi"})
    assert calls and calls[0]["range_name"] == "B7" and calls[0]["value_input_option"] == "USER_ENTERED"


def test_date_rewrite_failure_does_not_fail_write(db):
    ws = db.sheets["Transactions"]
    ws.append_row = lambda row, **kw: {"updates": {"updatedRange": "Transactions!A7:P7"}}
    def boom(**kw): raise RuntimeError("quota")
    ws.update = boom
    sheets.append_transaction({"type": "РАСХОД", "amount": 500, "category": "Еда и продукты", "bank": "Kaspi"})


def _subs(db, name="Tele2", amount=1230, bank="Kaspi", last_paid=""):
    ws = sheets._get_or_create_subscriptions_sheet()
    ws.append_row(["SUB1", name, amount, bank, 22, last_paid, "active", ""])


def test_subscription_skips_when_already_recorded_manually(db):
    _subs(db)
    sheets.append_transaction({"type": "РАСХОД", "amount": 1230, "bank": "Kaspi", "merchant": "Tele2",
                               "category": "Связь и подписки", "user_comment": "Оплата связи"})
    due = sheets.process_due_subscriptions(datetime.datetime.now(sheets.ASTANA_TZ).replace(day=28))
    rows = db.sheets["Transactions"].data[1:]
    assert due == [] and len(rows) == 1


def test_subscription_charge_uses_real_source_and_subcategory(db):
    _subs(db)
    due = sheets.process_due_subscriptions(datetime.datetime.now(sheets.ASTANA_TZ).replace(day=28))
    row = dict(zip(TRANSACTION_HEADERS, db.sheets["Transactions"].data[-1]))
    assert due == ["Tele2"]
    assert row["source"] == "Kaspi Gold" and row["subcategory"] == "Мобильная связь и интернет"


def test_obvious_category_errors_only(db, monkeypatch):
    ws = db.sheets["Transactions"]
    for r in (
        _row("S1", category="Кафе, рестораны и доставка еды", subcategory="Кафе и рестораны", user_comment="стики", merchant="LOVEKA SHOP"),
        _row("S2", category="Еда и продукты", user_comment="Стики 1210 тенге, остальное молоко"),          # смесь — не трогаем
        _row("S3", category="Алкоголь, табак и энергетики", subcategory="Сигареты и стики", user_comment="стики"),
        _row("P1", category="Развлечения и хобби", subcategory="Игры и хобби", merchant="ZOO OCEANICA ASTANA", user_comment="корм для Кайла"),
        _row("P2", category="Питомцы", subcategory="Корм и лакомства", merchant="ZOO OCEANICA ASTANA", user_comment="корм"),
        _row("P3", category="Дом и быт", merchant="IKEA", user_comment="корм"),                              # не зоомагазин
    ):
        ws.append_row(r)
    monkeypatch.setattr(data_fix, "raw_date_cells", lambda w: ["date"] + [46000.5] * (len(ws.data) - 1))
    found = data_fix.find_changes()
    assert {f["id"] for f in found["fields"]} == {"S1", "P1"}
    data_fix.apply_changes(found)
    by = {r[0]: dict(zip(TRANSACTION_HEADERS, r)) for r in ws.data[1:]}
    assert by["S1"]["category"] == "Алкоголь, табак и энергетики" and by["S1"]["subcategory"] == "Сигареты и стики"
    assert by["P1"]["category"] == "Питомцы" and by["S2"]["category"] == "Еда и продукты"
    data_fix.undo_last()
    by = {r[0]: dict(zip(TRANSACTION_HEADERS, r)) for r in ws.data[1:]}
    assert by["S1"]["category"] == "Кафе, рестораны и доставка еды" and by["P1"]["subcategory"] == "Игры и хобби"

import asyncio
import datetime

import pytest

from services import limits_ai, limits_engine as le, sheets, state
from services.categories import EXPENSE_CATEGORIES
from services.timezone import ASTANA_TZ

NOW = datetime.datetime(2026, 10, 1, 9, 30, tzinfo=ASTANA_TZ)


def tx(date, amt, cat="Еда и продукты", typ="РАСХОД"):
    return {"date": date, "amt": amt, "cat": cat, "type": typ}


# История с 1 августа: август и сентябрь полные
FULL = [
    tx("2026-08-02 10:00:00", 100000), tx("2026-09-02 10:00:00", 200000),
    tx("2026-08-03 10:00:00", 50000, "Транспорт и авто"), tx("2026-09-03 10:00:00", 50000, "Транспорт и авто"),
    tx("2026-08-05 10:00:00", 1000000, "Зарплата", "ДОХОД"), tx("2026-09-05 10:00:00", 1000000, "Зарплата", "ДОХОД"),
]


def test_benchmark_covers_all_categories_and_sums_to_one():
    shares = le.benchmark_shares()
    assert set(shares) == set(EXPENSE_CATEGORIES)
    assert sum(shares.values()) == pytest.approx(1.0)
    food = shares["Еда и продукты"] + shares["Кафе, рестораны и доставка еды"]
    assert food == pytest.approx(le.FOOD_SHARE)


def test_partial_month_is_not_used_when_full_months_exist():
    # данные начались 26 августа: август неполный, полный только сентябрь
    txs = [tx("2026-08-26 10:00:00", 999999), tx("2026-09-02 10:00:00", 200000),
           tx("2026-09-05 10:00:00", 1000000, "Зарплата", "ДОХОД")]
    r = le.compute_limits(txs, NOW)
    assert r["mode"] == "complete" and r["months"] == [(2026, 9)]
    assert r["details"]["Еда и продукты"]["own"] == 200000


def test_no_full_months_extrapolates_current_month():
    txs = [tx("2026-09-02 10:00:00", 145000), tx("2026-09-05 10:00:00", 1000000, "Зарплата", "ДОХОД")]
    now = datetime.datetime(2026, 9, 29, 12, tzinfo=ASTANA_TZ)
    r = le.compute_limits(txs, now)
    assert r["mode"] == "partial"
    assert r["details"]["Еда и продукты"]["own"] == round(145000 * 30 / 29)


def test_weights_favor_recent_months():
    r = le.compute_limits(FULL, NOW)
    assert r["months"] == [(2026, 9), (2026, 8)]
    # (3*200000 + 2*100000) / 5
    assert r["details"]["Еда и продукты"]["own"] == 160000


def test_limit_never_below_actual_and_lift_is_capped():
    r = le.compute_limits(FULL, NOW)
    for cat, d in r["details"].items():
        assert r["limits"][cat] >= d["own"], cat
    d = r["details"]["Транспорт и авто"]     # own 50 000, ориентир много больше
    assert r["limits"]["Транспорт и авто"] <= le._round_up(d["own"] * le.MAX_MULT * le.BUFFER)


def test_no_history_categories_get_benchmark_and_rounding():
    r = le.compute_limits(FULL, NOW)
    d = r["details"]["Одежда и обувь"]
    assert d["own"] == 0
    assert r["limits"]["Одежда и обувь"] == le._round_up(d["bench"] * le.NO_HISTORY_LIFT * le.BUFFER)
    assert all(v % 1000 == 0 for v in r["limits"].values())


def test_min_limit_floor_for_tiny_categories():
    txs = FULL + [tx("2026-09-04 10:00:00", 540, "Обязательные платежи и прочее")]
    r = le.compute_limits(txs, NOW)
    assert r["limits"]["Обязательные платежи и прочее"] == le.MIN_LIMIT
    assert min(r["limits"].values()) >= le.MIN_LIMIT


def test_spendable_uses_income_and_falls_back_to_own_spending():
    r = le.compute_limits(FULL, NOW)
    assert r["income_avg"] == 1000000 and r["spendable"] == 800000
    no_income = le.compute_limits([t for t in FULL if t["type"] == "РАСХОД"], NOW)
    assert no_income["income_avg"] == 0
    assert no_income["spendable"] == round(sum(d["own"] for d in no_income["details"].values()))


def test_pinned_category_keeps_current_limit():
    cur = {"Еда и продукты": 77000, "Транспорт и авто": 1234}
    r = le.compute_limits(FULL, NOW, cur, pinned={"Еда и продукты"})
    assert r["limits"]["Еда и продукты"] == 77000 and r["pinned"] == ["Еда и продукты"]
    assert r["limits"]["Транспорт и авто"] != 1234          # не закреплён — пересчитан
    # закреплённая категория без текущего значения — считаем как обычную
    r2 = le.compute_limits(FULL, NOW, {}, pinned={"Еда и продукты"})
    assert r2["pinned"] == []


def test_empty_history_gives_no_crash():
    r = le.compute_limits([], NOW)
    assert set(r["limits"]) == set(EXPENSE_CATEGORIES)


def test_summary_text_shows_changes_pin_and_totals():
    cur = {"Еда и продукты": 34000, "Транспорт и авто": 999999}
    r = le.compute_limits(FULL, NOW, cur, pinned={"Транспорт и авто"})
    text = le.format_summary(r, cur, NOW)
    assert "октябрь 2026" in text and "Еда и продукты: 34 000 →" in text
    assert "📌" in text and "/limits_undo" in text and "Сумма лимитов" in text
    assert "Так Ада пересчитала бы" in le.format_summary(r, cur, NOW, preview=True)


# ---------- запись в таблицу, закрепление, откат ----------

def _seed_limits(db, rows):
    from conftest import Sheet
    db.sheets["Limits"] = Sheet("Limits", [["category", "limit_amount", "pinned"], *rows])


def _seed_transactions(db, txs):
    from services.preflight import TRANSACTION_HEADERS
    ws = db.sheets["Transactions"]
    for i, t in enumerate(txs):
        values = {"transaction_id": f"T{i}", "date": t["date"], "user": "Влад", "type": t["type"],
                  "amount": t["amt"], "currency": "KZT", "category": t["cat"]}
        ws.append_row([values.get(h, "") for h in TRANSACTION_HEADERS])


def test_save_limits_preserves_pinned_flags(db):
    _seed_limits(db, [["Еда и продукты", 50000, "да"], ["Транспорт и авто", 10000, ""]])
    assert sheets.get_pinned_categories() == {"Еда и продукты"}
    sheets.save_category_limits({"Еда и продукты": 60000, "Транспорт и авто": 20000})   # pinned не передан
    rows = db.sheets["Limits"].get_all_values()
    assert rows[0] == ["category", "limit_amount", "pinned"]
    assert rows[1][2] == "да" and rows[2][2] == ""
    assert sheets.get_category_limits() == {"Еда и продукты": 60000, "Транспорт и авто": 20000}


def test_recalc_writes_snapshot_and_undo_restores(db, monkeypatch):
    _seed_limits(db, [["Еда и продукты", 77000, "да"], ["Транспорт и авто", 1000, ""]])
    _seed_transactions(db, [dict(t, date=t["date"]) for t in FULL])
    monkeypatch.setattr(limits_ai, "now_astana", lambda: NOW)
    out = limits_ai.recalc_and_apply()
    assert out is not None
    new, summary = out
    assert new["Еда и продукты"] == 77000                 # закреплён
    assert sheets.get_category_limits()["Транспорт и авто"] == new["Транспорт и авто"] != 1000
    assert sheets.get_pinned_categories() == {"Еда и продукты"}
    assert "Ада пересчитала лимиты" in summary
    msg = limits_ai.restore_previous_limits()
    assert "Вернула" in msg
    assert sheets.get_category_limits()["Транспорт и авто"] == 1000
    assert "Нечего откатывать" in limits_ai.restore_previous_limits()   # снимок одноразовый


def test_unreadable_transactions_never_touch_limits(db, monkeypatch):
    _seed_limits(db, [["Еда и продукты", 77000, ""]])       # транзакций нет вообще
    monkeypatch.setattr(limits_ai, "now_astana", lambda: NOW)
    assert limits_ai.recalc_and_apply() is None
    assert sheets.get_category_limits() == {"Еда и продукты": 77000}
    assert asyncio.run(limits_ai.generate_limits_from_history()) == {}
    assert "ничего не считаю" in limits_ai.preview_limits_text()


def test_preview_does_not_write(db, monkeypatch):
    _seed_limits(db, [["Транспорт и авто", 1000, ""]])
    _seed_transactions(db, FULL)
    monkeypatch.setattr(limits_ai, "now_astana", lambda: NOW)
    text = limits_ai.preview_limits_text()
    assert "Так Ада пересчитала бы" in text
    assert sheets.get_category_limits() == {"Транспорт и авто": 1000}


# ---------- валюта ----------

def test_foreign_currency_row_is_found_and_fixed_once(db):
    from services.preflight import TRANSACTION_HEADERS
    ws = db.sheets["Transactions"]
    def row(tid, amount, cur, comment=""):
        v = {"transaction_id": tid, "date": "2026-09-05 13:44:16", "user": "Влад", "type": "РАСХОД",
             "amount": amount, "currency": cur, "category": "Связь и подписки", "ai_comment": comment}
        return [v.get(h, "") for h in TRANSACTION_HEADERS]
    ws.append_row(row("A", 5000, "KZT"))
    ws.append_row(row("B", 10.44, "USD", "Оплата API."))
    found = sheets.find_foreign_currency_rows()
    assert found == [{"row": 3, "amount": 10.44, "currency": "USD", "comment": "Оплата API."}]
    sheets.apply_currency_fix(3, 5220.0, 10.44, "USD", 500.0, "Оплата API.")
    fixed = ws.get_all_values()[2]
    assert fixed[4] == 5220.0 and fixed[5] == "KZT"
    assert fixed[15].startswith("Оплата API.") and "10.44 USD по курсу 500.00" in fixed[15]
    assert sheets.find_foreign_currency_rows() == []        # идемпотентно


def test_startup_fix_skips_row_when_rate_unavailable(db, monkeypatch):
    import main as app_main
    from services.preflight import TRANSACTION_HEADERS
    v = {"transaction_id": "B", "date": "2026-09-05 13:44:16", "user": "Влад", "type": "РАСХОД",
         "amount": 10.44, "currency": "USD", "category": "Связь и подписки"}
    db.sheets["Transactions"].append_row([v.get(h, "") for h in TRANSACTION_HEADERS])

    async def no_rate(amount, cur): return None
    monkeypatch.setattr(app_main.fx, "convert_to_kzt", no_rate)
    asyncio.run(app_main.fix_foreign_currency_rows())
    assert db.sheets["Transactions"].get_all_values()[1][5] == "USD"      # осталась до следующего старта

    async def rate(amount, cur): return round(amount * 500, 2), 500.0
    monkeypatch.setattr(app_main.fx, "convert_to_kzt", rate)
    asyncio.run(app_main.fix_foreign_currency_rows())
    row = db.sheets["Transactions"].get_all_values()[1]
    assert row[5] == "KZT" and row[4] == 5220.0

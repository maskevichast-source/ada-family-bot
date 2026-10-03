import datetime

from conftest import *  # noqa: F401,F403
from services import sheets

ASTANA = sheets.ASTANA_TZ


def _tx_rows(db):
    return db.worksheet("Transactions").get_all_values()[1:]


def _fix_now(monkeypatch, y, m, d):
    class FixedDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime(y, m, d, 12, 0, tzinfo=tz or ASTANA)
    monkeypatch.setattr(sheets.datetime, "datetime", FixedDT)


def test_subscription_not_charged_twice_when_marker_fails(db, monkeypatch):
    sheets.add_or_update_subscription("Netflix", 3000, "Kaspi", 1)
    ws = sheets._get_or_create_subscriptions_sheet()
    ws.update_cell(2, 6, "")   # ещё не оплачена в этом месяце
    monkeypatch.setattr(ws, "update_cell", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("quota")), raising=False)
    now = datetime.datetime(2026, 10, 2, 12, 0, tzinfo=ASTANA)
    sheets.process_due_subscriptions(now)
    sheets.process_due_subscriptions(now + datetime.timedelta(minutes=1))
    assert len(_tx_rows(db)) == 1


def test_new_subscription_with_past_day_is_not_charged_retroactively(db, monkeypatch):
    _fix_now(monkeypatch, 2026, 10, 20)
    sheets.add_or_update_subscription("Spotify", 1500, "Kaspi", 5)
    rec = sheets.get_active_subscriptions()[0]
    assert str(rec["last_paid"]).startswith("2026-10-20")
    assert sheets.process_due_subscriptions(datetime.datetime(2026, 10, 20, 13, 0, tzinfo=ASTANA)) == []
    assert _tx_rows(db) == []


def test_new_subscription_with_today_or_future_day_stays_unpaid(db, monkeypatch):
    _fix_now(monkeypatch, 2026, 10, 5)
    sheets.add_or_update_subscription("Today", 1000, "Kaspi", 5)
    sheets.add_or_update_subscription("Later", 1000, "Kaspi", 25)
    recs = {r["name"]: r for r in sheets.get_active_subscriptions()}
    assert not recs["Today"]["last_paid"]
    assert not recs["Later"]["last_paid"]


def test_bank_change_updates_funds_type_and_picks_author_row(db):
    sheets.append_transaction({"transaction_id": "A1", "type": "РАСХОД", "amount": 1000, "bank": "Kaspi",
                               "category": "Еда и продукты", "subcategory": "Продукты", "user": "Влад"})
    sheets.append_transaction({"transaction_id": "A2", "type": "РАСХОД", "amount": 2000, "bank": "Halyk",
                               "category": "Еда и продукты", "subcategory": "Продукты", "user": "Диана"})
    rec = sheets.update_last_transaction_bank_and_source("Kaspi Red", "Влад")
    assert rec and rec["transaction_id"] == "A1"
    headers = db.worksheet("Transactions").get_all_values()[0]
    rows = {r[0]: r for r in _tx_rows(db)}
    assert rows["A1"][headers.index("funds_type")] == "Рассрочка"
    assert rows["A2"][headers.index("bank")] == "Halyk"   # чужая запись не тронута
    assert sheets.update_last_transaction_bank_and_source("Kaspi", "Несуществующий") is None


def test_price_check_success_single_batch(db):
    sheets.add_price_tracking({"user": "Влад", "url": "https://kaspi.kz/shop/p/x-1/", "product_name": "X", "price": 10000})
    item = sheets.get_active_price_trackings()[0]
    sheets.record_price_check_success(item["row_idx"], 9000, "img", 9000, "2026-10-02 10:00:00")
    after = sheets.get_active_price_trackings()[0]
    assert float(after["last_price"]) == 9000
    assert float(after["notified_price"]) == 9000
    assert str(after["fail_count"]) in ("0", "")

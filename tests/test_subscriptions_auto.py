import datetime as dt

from services import sheets, state
from services import subscriptions_auto as sa
from services.timezone import ASTANA_TZ


def tx(merchant="Netflix", amount=4500, date="2026-10-02 12:00:00", category="Связь и подписки",
       subcategory="Цифровые подписки и сервисы", comment=""):
    return {"merchant": merchant, "amount": amount, "date": date, "category": category, "subcategory": subcategory,
            "user_comment": comment, "bank": "Kaspi", "type": "РАСХОД", "transaction_id": "T1"}


def sub_row(db):
    return db.worksheet("Subscriptions").data[1]


def test_early_payment_closes_the_month_and_auto_debit_skips(db):
    sheets.add_or_update_subscription("Netflix", 4500, "Kaspi", 5)
    result = sa.process_tx(tx(date="2026-10-02 12:00:00"))
    assert result["kind"] == "paid" and result["month"] == "2026-10" and result["day"] == 5
    assert sub_row(db)[5] == "2026-10-02" and sub_row(db)[8] == "2026-10"
    done = sheets.process_due_subscriptions(dt.datetime(2026, 10, 5, 10, 0, tzinfo=ASTANA_TZ))
    assert done == [] and len(db.worksheet("Transactions").data) == 1       # второй раз не списали
    # в ноябре — списывается как обычно
    assert sheets.process_due_subscriptions(dt.datetime(2026, 11, 5, 10, 0, tzinfo=ASTANA_TZ)) == ["Netflix"]


def test_warning_not_sent_when_month_already_paid(db):
    sheets.add_or_update_subscription("Netflix", 4500, "Kaspi", 5)
    sa.process_tx(tx(date="2026-10-02 12:00:00"))
    assert sheets.get_subscription_warnings(dt.datetime(2026, 10, 3, 9, 0, tzinfo=ASTANA_TZ), 2) == []


def test_prepayment_for_next_month_when_this_one_is_covered(db):
    sheets.add_or_update_subscription("Netflix", 4500, "Kaspi", 5)
    sa.process_tx(tx(date="2026-10-02 12:00:00"))
    assert sa.process_tx(tx(date="2026-10-20 12:00:00")) is None            # слишком рано — не оплата вперёд
    result = sa.process_tx(tx(date="2026-10-28 12:00:00"))                  # за неделю до 5 ноября
    assert result["kind"] == "paid" and result["month"] == "2026-11"
    assert sheets.process_due_subscriptions(dt.datetime(2026, 11, 5, 10, 0, tzinfo=ASTANA_TZ)) == []


def test_amount_tolerance_and_name_match(db):
    sheets.add_or_update_subscription("Netflix", 4500, "Kaspi", 5)
    assert sa.find_match([{"name": "Netflix", "amount": 4500}], tx(amount=6000)) is None     # цена сильно другая
    assert sa.process_tx(tx(merchant="NETFLIX.COM", amount=4600))["kind"] == "paid"      # +2% — та же подписка


def test_unknown_service_is_suggested_once_then_added_with_month_paid(db):
    result = sa.process_tx(tx(merchant="Spotify", amount=1990, date="2026-10-03 09:00:00"))
    assert result["kind"] == "suggest" and result["day"] == 3
    assert sa.process_tx(tx(merchant="Spotify", amount=1990)) is None       # второй раз не спрашиваем
    answer = sa.apply_answer(result["token"], True)
    assert answer["added"] and sub_row(db)[1] == "Spotify" and sub_row(db)[4] == 3
    assert sub_row(db)[5] == "2026-10-03" and sub_row(db)[8] == "2026-10"
    assert sheets.process_due_subscriptions(dt.datetime(2026, 10, 3, 10, 0, tzinfo=ASTANA_TZ)) == []
    assert sa.apply_answer(result["token"], True) is None                   # кнопка одноразовая


def test_decline_means_never_ask_again(db):
    result = sa.process_tx(tx(merchant="Spotify", amount=1990))
    assert sa.apply_answer(result["token"], False)["added"] is False
    state.put(sa.ASKED_NS, sa._norm("Spotify"), {"at": 0, "never": True})
    assert sa.process_tx(tx(merchant="Spotify", amount=1990)) is None


def test_ordinary_purchase_is_not_a_candidate(db):
    assert sa.process_tx(tx(merchant="Magnum", category="Продукты", subcategory="Супермаркет", amount=9000)) is None
    assert sa.process_tx(tx(merchant="", amount=1000)) is None


def test_append_transaction_enqueues_but_auto_debit_does_not(db):
    sa.PENDING.clear()
    sheets.append_transaction({"type": "РАСХОД", "amount": 4500, "merchant": "Netflix", "category": "Связь и подписки",
                               "subcategory": "Цифровые подписки и сервисы", "bank": "Kaspi"})
    assert len(sa.PENDING) == 1 and sa.PENDING[0]["merchant"] == "Netflix"
    sa.PENDING.clear()
    sheets.add_or_update_subscription("Hulu", 1000, "Kaspi", 1)
    sheets.process_due_subscriptions(dt.datetime(2026, 10, 5, 10, 0, tzinfo=ASTANA_TZ))
    assert len(sa.PENDING) == 0                                              # «Автосписание…» в очередь не идёт
    sheets.append_transaction({"type": "ДОХОД", "amount": 500000, "merchant": "Зарплата", "category": "Доход"})
    assert len(sa.PENDING) == 0


def test_covered_month_rules():
    d = dt.date
    assert sa.covered_month(d(2026, 10, 2), 5, "") == "2026-10"
    assert sa.covered_month(d(2026, 10, 9), 5, "2026-09") == "2026-10"      # опоздала — всё равно октябрь
    assert sa.covered_month(d(2026, 10, 9), 5, "2026-10") is None
    assert sa.covered_month(d(2026, 12, 28), 5, "2026-12") == "2027-01"
    assert sa.month_name("2026-11") == "ноябрь"


def test_old_table_gets_ninth_column(db):
    from tests.conftest import Sheet
    old = ["id", "name", "amount", "bank", "day_of_month", "last_paid", "status", "last_warning"]
    db.sheets["Subscriptions"] = Sheet("Subscriptions", [old], cols=8)
    sheets.add_or_update_subscription("Netflix", 4500, "Kaspi", 5, paid_date="2026-10-02")
    assert db.worksheet("Subscriptions").data[0][8] == "paid_month"
    assert db.worksheet("Subscriptions").data[1][8] == "2026-10"

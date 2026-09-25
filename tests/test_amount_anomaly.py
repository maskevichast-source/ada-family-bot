import datetime as dt
from services.timezone import ASTANA_TZ
from services.analytics import detect_amount_anomaly


def _seed_row(db, transaction_id, date, user, category, amount):
    row = [
        transaction_id, date, user, "РАСХОД", amount, "KZT",
        "Kaspi", "Kaspi Gold", "Собственные", "Карта", category,
        "", "", "Want", "", "",
    ]
    db.worksheet("Transactions").append_row(row)


def _fmt(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def test_detect_amount_anomaly_flags_large_outlier(db):
    now = dt.datetime.now(ASTANA_TZ).replace(tzinfo=None)
    for i, amt in enumerate([3000, 3500, 4000]):
        _seed_row(db, f"T{i}", _fmt(now - dt.timedelta(days=i + 1)), "Влад", "Кафе, рестораны и доставка еды", amt)

    fact = detect_amount_anomaly("Влад", "Кафе, рестораны и доставка еды", 15000)
    assert fact is not None
    assert "4 000" in fact.replace("\u2009", " ") or "4000" in fact
    assert "15 000" in fact.replace("\u2009", " ") or "15000" in fact


def test_detect_amount_anomaly_silent_for_ordinary_amount(db):
    now = dt.datetime.now(ASTANA_TZ).replace(tzinfo=None)
    for i, amt in enumerate([3000, 3500, 4000]):
        _seed_row(db, f"T{i}", _fmt(now - dt.timedelta(days=i + 1)), "Влад", "Кафе, рестораны и доставка еды", amt)

    # 4200 - это лишь немного больше прошлого максимума (4000), не в 1.5+ раза
    assert detect_amount_anomaly("Влад", "Кафе, рестораны и доставка еды", 4200) is None


def test_detect_amount_anomaly_silent_without_enough_history(db):
    now = dt.datetime.now(ASTANA_TZ).replace(tzinfo=None)
    # всего 2 записи - меньше min_history=3, сравнивать не с чем
    _seed_row(db, "T1", _fmt(now - dt.timedelta(days=1)), "Влад", "Кафе, рестораны и доставка еды", 3000)
    _seed_row(db, "T2", _fmt(now - dt.timedelta(days=2)), "Влад", "Кафе, рестораны и доставка еды", 3200)

    assert detect_amount_anomaly("Влад", "Кафе, рестораны и доставка еды", 50000) is None


def test_detect_amount_anomaly_ignores_old_history_outside_window(db):
    now = dt.datetime.now(ASTANA_TZ).replace(tzinfo=None)
    for i, amt in enumerate([3000, 3500, 4000]):
        # за пределами lookback_days=90
        _seed_row(db, f"T{i}", _fmt(now - dt.timedelta(days=200 + i)), "Влад", "Кафе, рестораны и доставка еды", amt)

    assert detect_amount_anomaly("Влад", "Кафе, рестораны и доставка еды", 15000) is None


def test_detect_amount_anomaly_separates_by_user_and_category(db):
    now = dt.datetime.now(ASTANA_TZ).replace(tzinfo=None)
    for i, amt in enumerate([3000, 3500, 4000]):
        _seed_row(db, f"TU{i}", _fmt(now - dt.timedelta(days=i + 1)), "Влад", "Кафе, рестораны и доставка еды", amt)
        _seed_row(db, f"TC{i}", _fmt(now - dt.timedelta(days=i + 1)), "Влад", "Еда и продукты", amt)

    # у Дианы истории вообще нет - не должна сравнивать с историей Влада
    assert detect_amount_anomaly("Диана", "Кафе, рестораны и доставка еды", 15000) is None

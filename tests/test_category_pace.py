import datetime as dt
from services.timezone import ASTANA_TZ, now_astana
from services.analytics import detect_category_pace_anomalies
from services.sheets import save_category_limits


def _seed_row(db, transaction_id, date, category, amount, user="Влад"):
    row = [
        transaction_id, date, user, "РАСХОД", amount, "KZT",
        "Kaspi", "Kaspi Gold", "Собственные", "Карта", category,
        "", "", "Want", "", "",
    ]
    db.worksheet("Transactions").append_row(row)


def _month_back(now: dt.datetime, months: int) -> dt.date:
    y, m = now.year, now.month
    for _ in range(months):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return dt.date(y, m, min(now.day, 28))


def test_pace_anomaly_flags_category_without_limit(db):
    now = now_astana()
    cat = "Развлечения и хобби"
    # текущий месяц: заметно больше обычного
    _seed_row(db, "CUR1", now.strftime("%Y-%m-%d 10:00:00"), cat, 30000)
    # 2 прошлых месяца - "обычный" уровень около 8000-10000
    for i in (1, 2):
        d = _month_back(now, i)
        _seed_row(db, f"PAST{i}", d.strftime("%Y-%m-%d 10:00:00"), cat, 9000)

    fact = detect_category_pace_anomalies(min_amount=5000)
    assert fact is not None
    assert cat in fact
    assert "30 000" in fact.replace("\u2009", " ") or "30000" in fact


def test_pace_anomaly_ignores_category_with_limit(db):
    now = now_astana()
    cat = "Еда и продукты"
    save_category_limits({cat: 100000})  # у категории ЕСТЬ лимит
    _seed_row(db, "CUR1", now.strftime("%Y-%m-%d 10:00:00"), cat, 30000)
    for i in (1, 2):
        d = _month_back(now, i)
        _seed_row(db, f"PAST{i}", d.strftime("%Y-%m-%d 10:00:00"), cat, 9000)

    assert detect_category_pace_anomalies(min_amount=5000) is None


def test_pace_anomaly_silent_without_enough_history(db):
    now = now_astana()
    cat = "Развлечения и хобби"
    _seed_row(db, "CUR1", now.strftime("%Y-%m-%d 10:00:00"), cat, 30000)
    # только 1 прошлый месяц из требуемых min_history_months=2
    d = _month_back(now, 1)
    _seed_row(db, "PAST1", d.strftime("%Y-%m-%d 10:00:00"), cat, 9000)

    assert detect_category_pace_anomalies(min_amount=5000) is None


def test_pace_anomaly_silent_for_ordinary_pace(db):
    now = now_astana()
    cat = "Развлечения и хобби"
    _seed_row(db, "CUR1", now.strftime("%Y-%m-%d 10:00:00"), cat, 9500)  # почти как обычно
    for i in (1, 2):
        d = _month_back(now, i)
        _seed_row(db, f"PAST{i}", d.strftime("%Y-%m-%d 10:00:00"), cat, 9000)

    assert detect_category_pace_anomalies(min_amount=5000) is None


def test_pace_anomaly_ignores_tiny_amounts(db):
    now = now_astana()
    cat = "Развлечения и хобби"
    # в 5 раз больше обычного, но сама сумма мизерная - ниже min_amount
    _seed_row(db, "CUR1", now.strftime("%Y-%m-%d 10:00:00"), cat, 2500)
    for i in (1, 2):
        d = _month_back(now, i)
        _seed_row(db, f"PAST{i}", d.strftime("%Y-%m-%d 10:00:00"), cat, 500)

    assert detect_category_pace_anomalies(min_amount=5000) is None

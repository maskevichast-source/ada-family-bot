"""Регрессии по находкам аудита 05.10.2026: разбор сумм, часовой пояс графиков, месяцы /trend."""
import datetime
import pytest

from services.money import parse_amount


@pytest.mark.parametrize("raw,expected", [
    ("1 230", 1230.0),            # неразрывный пробел (так Google/iOS форматируют тысячи)
    ("1 230,50", 1230.5),         # узкий неразрывный пробел
    ("12 345 ₸", 12345.0),
    ("1,5к", 1500.0), ("300к", 300000.0), ("2k", 2000.0), ("1.5К", 1500.0),
    ("кофе 3к", 3000.0),
    ("5 к вечеру", 5.0),               # с пробелом — не тысячи
    ("5 кофе", 5.0), ("500", 500.0), ("1 230", 1230.0), ("2 тыс", 2000.0), ("10 млн", 10_000_000.0),
])
def test_parse_amount_regressions(raw, expected):
    assert parse_amount(raw) == expected


def test_trend_months_are_consecutive_on_31st(monkeypatch):
    from services import charts
    seen = []
    monkeypatch.setattr(charts, "_now", lambda: datetime.datetime(2026, 3, 31, 12))
    monkeypatch.setattr(charts, "get_transactions_for_period", lambda s, e: seen.append(s) or [])
    charts.generate_trend_chart(months_back=3)
    assert seen == ["2025-12-01", "2026-01-01", "2026-02-01", "2026-03-01"]


def test_chart_now_is_astana_not_server_utc():
    from services import charts
    from services.timezone import now_astana
    delta = abs((charts._now() - now_astana().replace(tzinfo=None)).total_seconds())
    assert delta < 5


def test_monthly_reminder_catches_up_after_long_downtime():
    import datetime as dt
    from services.reminders import next_occurrence
    prev = dt.datetime(2026, 1, 31, 9, 0)
    now = dt.datetime(2026, 5, 10, 12, 0)
    nxt = next_occurrence(prev, "monthly", now, anchor_day=31)
    assert nxt == dt.datetime(2026, 5, 31, 9, 0)
    # без простоя — следующий месяц, с укорочением до последнего дня
    assert next_occurrence(prev, "monthly", prev, anchor_day=31) == dt.datetime(2026, 2, 28, 9, 0)


def test_excel_export_text_starting_with_equals_stays_text(monkeypatch):
    import io, openpyxl
    from services import reports
    tx = [{"transaction_id": "T1", "date": "2026-10-01 10:00:00", "user": "Влад", "type": "Расход",
           "amt": 100, "merchant": "=1+1", "comm": "=SUM(A1)", "cat": "Еда и продукты"}]
    monkeypatch.setattr(reports, "get_transactions_for_period", lambda a, b: tx)
    data = reports.generate_excel_export(2026, 10)
    ws = openpyxl.load_workbook(io.BytesIO(data))["Выписка"]
    assert ws.cell(row=2, column=13).data_type == "s"
    assert ws.cell(row=2, column=15).data_type == "s"


def test_owm_blocks_are_converted_from_utc_to_astana():
    from services.weather import _owm_local_dt
    import datetime
    assert _owm_local_dt("2026-10-05 15:00:00") == datetime.datetime(2026, 10, 5, 20, 0)

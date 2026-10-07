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


def test_vision_retries_once_on_empty_result(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from services import vision

    replies = ['{}', '{"transactions": [{"amount": 1280}]}']
    calls = []

    class FakeCompletions:
        async def create(self, **kw):
            calls.append(1)
            msg = SimpleNamespace(content=replies[len(calls) - 1])
            return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])

    class FakeClient:
        def __init__(self, *a, **k):
            self.chat = SimpleNamespace(completions=FakeCompletions())
        async def close(self):
            pass

    monkeypatch.setattr(vision, "AsyncOpenAI", FakeClient)
    result = asyncio.run(vision.parse_receipt(b"\xff\xd8\xff", "a.jpg", "", "Влад", ""))
    assert len(calls) == 2
    assert result["transactions"][0]["amount"] == 1280


def _tx(amount, cat, sub="", summary=""):
    return {"amount": amount, "category": cat, "subcategory": sub, "items_summary": summary, "confidence": 1.0}


def test_receipt_split_kept_when_sum_matches_total():
    from services.vision import normalize_receipt_split
    r = {"receipt_total": 44340, "transactions": [
        _tx(40592, "Еда и продукты"), _tx(1969, "Алкоголь, табак и энергетики"), _tx(1779, "Одежда и обувь")]}
    assert normalize_receipt_split(r)["transactions"] == r["transactions"]


def test_receipt_split_collapses_when_sum_differs():
    from services.vision import normalize_receipt_split
    r = {"receipt_total": 3919, "transactions": [
        _tx(3221, "Дом и быт", summary="контейнер"), _tx(500, "Еда и продукты", summary="пряники")]}
    out = normalize_receipt_split(r)["transactions"]
    assert len(out) == 1 and out[0]["amount"] == 3919 and out[0]["category"] == "Дом и быт"
    assert out[0]["confidence"] <= 0.8


def test_duplicate_copy_of_same_receipt_collapses():
    from services.vision import normalize_receipt_split
    r = {"receipt_total": 12990, "transactions": [_tx(12990, "Красота и уход"), _tx(12990, "Красота и уход")]}
    out = normalize_receipt_split(r)["transactions"]
    assert len(out) == 1 and out[0]["amount"] == 12990


def test_commission_and_no_total_are_left_alone():
    from services.vision import normalize_receipt_split
    r = {"receipt_total": 1000, "transactions": [_tx(1000, "Финансовые расходы и переводы"),
                                                  _tx(10, "Финансовые расходы и переводы", "Банковские комиссии")]}
    assert normalize_receipt_split(r) == r
    r2 = {"transactions": [_tx(10, "Еда и продукты"), _tx(20, "Дом и быт")]}
    assert normalize_receipt_split(r2) == r2


from test_media_schedulers import app  # noqa: E402,F401  (фикстура)
from test_handlers import handler  # noqa: E402,F401


def test_every_registered_command_is_listed_in_help(app):
    import re
    src = open("main.py", encoding="utf-8").read()
    registered = set(re.findall(r'Command\("([a-z_]+)"\)', src))
    listed = {name for name, _ in app.COMMANDS} | {"start"}
    assert registered <= listed, registered - listed
    assert all(len(name) <= 32 and len(desc) <= 256 for name, desc in app.COMMANDS)


# ---------- погода картинкой ----------

def _weather_data(days=2):
    import datetime, math
    n = 24 * days
    base = datetime.datetime(2026, 10, 6, 0, 0)
    times = [(base + datetime.timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    temp = [round(3 + 4 * math.sin((i - 9) / 24 * 2 * math.pi), 1) for i in range(n)]
    return {"hourly": {"time": times, "temperature_2m": temp, "apparent_temperature": [t - 5 for t in temp],
                       "precipitation_probability": [30] * n, "weather_code": [3] * n,
                       "wind_speed_10m": [18.0] * n}}


def test_weather_windows_are_two_halves_of_12_hours():
    import datetime
    from services import weather_chart as wc
    start = datetime.datetime(2026, 10, 6, 9, 0)
    pts = wc.build_points(_weather_data(), start)
    wins = wc.split_windows(pts)
    assert len(pts) == 25 and len(wins) == 2
    assert wc.window_label(wins[0]) == "09:00 → 21:00 · вт" and wc.window_label(wins[1]) == "21:00 → 09:00 · вт → ср"
    assert wins[0][-1]["dt"] == wins[1][0]["dt"]


def test_weather_points_empty_when_start_not_in_data():
    import datetime
    from services import weather_chart as wc
    assert wc.build_points(_weather_data(1), datetime.datetime(2026, 10, 9, 9, 0)) == []


def test_weather_render_returns_png_and_caption_fits():
    import datetime
    from services import weather_chart as wc
    start = datetime.datetime(2026, 10, 6, 9, 0)
    pts = wc.build_points(_weather_data(), start)
    wins = wc.split_windows(pts)
    assert wc.render(wins, wc.title_for(start))[:8] == b"\x89PNG\r\n\x1a\n"
    caption = wc.caption_for(pts, wins, start)
    assert "Астана" in caption and len(caption) <= 1024
    rows = dict((label, text) for label, text, _ in wc.footer_rows(pts))
    assert "Одежда" in rows
    assert wc._fmt(-0.4) == "0°" and wc._fmt(-3.2) == "−3°"


def test_weather_image_start_hours():
    import datetime
    from services.timezone import ASTANA_TZ
    from services.weather import _image_start
    now = datetime.datetime(2026, 10, 6, 14, 20, tzinfo=ASTANA_TZ)
    assert _image_start("morning", now).hour == 9 and _image_start("evening", now).hour == 21
    assert _image_start("tomorrow", now) == datetime.datetime(2026, 10, 7, 9, 0)
    assert _image_start("today", now) == datetime.datetime(2026, 10, 6, 14, 0)
    assert _image_start("today", now.replace(hour=6)).hour == 9


def test_weather_image_falls_back_to_none_on_failure(monkeypatch):
    import asyncio
    from services import weather

    def boom(days):
        raise RuntimeError("нет сети")

    monkeypatch.setattr(weather, "_request_open_meteo", boom)
    assert asyncio.run(weather.get_weather_image("morning")) is None
    assert asyncio.run(weather.get_weather_image("week")) is None


def test_umbrella_text_rain_snow_and_dry():
    import datetime
    from services import weather_chart as wc
    base = datetime.datetime(2026, 10, 6, 9, 0)

    def pts(code, rain):
        return [{"dt": base + datetime.timedelta(hours=i), "temp": 3, "feels": 0, "rain": rain, "wind": 10,
                 "code": code, "spread": 0} for i in range(5)]
    assert wc.umbrella_text(pts(63, 80))[0].startswith("Зонт нужен")
    assert "Зонт не нужен" in wc.umbrella_text(pts(73, 80))[0] and "снег" in wc.umbrella_text(pts(73, 80))[0]
    assert wc.umbrella_text(pts(3, 10)) is None                      # сухо — про зонт не пишем вовсе
    assert "Зонт" not in wc.caption_for(pts(3, 10), [], base)
    assert "Зонт" not in dict((l, t) for l, t, _ in wc.footer_rows(pts(3, 10)))
    assert "на всякий случай" in wc.umbrella_text(pts(3, 70))[0]


def test_sun_info_and_moon_phase():
    import datetime
    from services import weather_chart as wc
    data = {"daily": {"time": ["2026-10-07"], "sunrise": ["2026-10-07T06:45"], "sunset": ["2026-10-07T18:12"]}}
    assert wc.sun_info(data, datetime.datetime(2026, 10, 7, 9)) == ("06:45", "18:12")
    assert wc.sun_info(data, datetime.datetime(2026, 10, 9, 9)) is None
    assert wc.sun_info({}, datetime.datetime(2026, 10, 7, 9)) is None
    phase, name = wc.moon_phase(datetime.datetime(2000, 1, 21, 12))        # полнолуние 21.01.2000
    assert name == "Полнолуние" and abs(phase - 0.5) < 0.04
    assert wc.moon_phase(datetime.datetime(2026, 10, 10, 12))[1] in ("Новолуние", "Убывающий серп")
    wins_start = datetime.datetime(2026, 10, 7, 9)
    pts = wc.build_points(_weather_data(), datetime.datetime(2026, 10, 6, 9))
    png = wc.render(wc.split_windows(pts), "t", wc.footer_rows(pts), "s", ("06:45", "18:12"), wc.moon_phase(wins_start))
    assert png[:4] == b"\x89PNG"


def test_umbrella_ignores_night_hours():
    import datetime
    from services import weather_chart as wc
    base = datetime.datetime(2026, 10, 7, 0, 0)
    night = [{"dt": base + datetime.timedelta(hours=i), "temp": 5, "feels": 3, "rain": 90, "wind": 10,
              "code": 63, "spread": 0} for i in range(0, 6)]            # дождь только 00:00–05:00
    assert wc.umbrella_text(night) is None
    day = night + [{"dt": base.replace(hour=8), "temp": 5, "feels": 3, "rain": 90, "wind": 10, "code": 63, "spread": 0}]
    assert "08:00" in wc.umbrella_text(day)[0]

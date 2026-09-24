import datetime

from services.timezone import ASTANA_TZ
from services.weather import format_forecast

NOW = datetime.datetime(2026, 1, 5, 8, 0, tzinfo=ASTANA_TZ)
DATE = "2026-01-05"
HOURS = [9, 12, 15, 18, 21]


def _hourly(codes, rain=70, temp=0.0, feels=None):
    """codes: список WMO-кодов на каждый час из HOURS (той же длины)."""
    times = [f"{DATE}T{h:02d}:00" for h in HOURS]
    feels_list = [feels if feels is not None else temp] * len(HOURS)
    return {
        "time": times,
        "temperature_2m": [temp] * len(HOURS),
        "apparent_temperature": feels_list,
        "precipitation_probability": [rain] * len(HOURS),
        "weather_code": codes,
        "wind_speed_10m": [10] * len(HOURS),
    }


def _data(codes, rain=70, temp=0.0, feels=None, current_code=0):
    return {
        "current": {"temperature_2m": temp, "apparent_temperature": temp, "weather_code": current_code},
        "hourly": _hourly(codes, rain=rain, temp=temp, feels=feels),
    }


def test_snow_only_day_does_not_suggest_umbrella():
    # 73 = снег
    data = _data([73, 73, 73, 73, 73])
    text = format_forecast(data, "today", NOW)
    assert "Зонт:" not in text
    assert "снег" in text.lower()
    assert "❄️" in text


def test_rain_only_day_suggests_umbrella():
    # 63 = дождь
    data = _data([63, 63, 63, 63, 63])
    text = format_forecast(data, "today", NOW)
    assert "Зонт:" in text
    assert "☂️" in text


def test_mixed_rain_and_snow_day_mentions_both():
    # утром снег, днём дождь
    data = _data([73, 73, 63, 63, 63])
    text = format_forecast(data, "today", NOW)
    assert "дождь" in text.lower() and "снег" in text.lower()
    assert "Зонт:" not in text  # это не простой rain-only случай
    assert "☂️" in text and "❄️" in text


def test_clear_day_has_no_precipitation_line():
    # 0 = ясно, дождя/снега нет вообще
    data = _data([0, 0, 1, 1, 0], rain=5)
    text = format_forecast(data, "today", NOW)
    assert "Зонт" not in text
    assert "снег" not in text.lower()


def test_fog_high_probability_does_not_falsely_claim_rain():
    # 45 = туман — это не дождь и не снег, вероятность может быть высокой,
    # но советовать зонт от тумана не нужно.
    data = _data([45, 45, 45, 45, 45], rain=80)
    text = format_forecast(data, "today", NOW)
    assert "Зонт:" not in text
    assert "❄️" not in text


def test_compact_format_has_no_old_verbose_labels():
    data = _data([0, 0, 0, 0, 0], rain=5, temp=13.0, feels=11.0)
    text = format_forecast(data, "today", NOW)
    assert "Динамика дня" not in text
    assert "Погода в Астане на" not in text
    assert "ощущается как" not in text  # заменено на компактное "ощущ."
    assert "Что надеть:" not in text
    assert "ощущ." in text

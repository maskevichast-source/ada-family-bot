import datetime

from services.timezone import ASTANA_TZ
from services.weather import format_owm_forecast, get_weather_forecast

NOW = datetime.datetime(2026, 1, 5, 8, 0, tzinfo=ASTANA_TZ)


def _owm_current(temp=12.0, feels=9.0, owm_id=800):
    return {"main": {"temp": temp, "feels_like": feels}, "weather": [{"id": owm_id}]}


def _owm_block(dt_txt, temp=13.0, feels=11.0, owm_id=800, pop=0.1, wind_ms=2.8):
    return {
        "dt_txt": dt_txt,
        "main": {"temp": temp, "feels_like": feels},
        "weather": [{"id": owm_id}],
        "pop": pop,
        "wind": {"speed": wind_ms},
    }


def _owm_forecast(blocks):
    return {"list": blocks}


def test_owm_fallback_today_basic_format():
    current = _owm_current(temp=12.0, feels=9.0, owm_id=800)
    forecast = _owm_forecast([
        _owm_block("2026-01-05 09:00:00", temp=13.0, feels=11.0, owm_id=800, pop=0.05),
        _owm_block("2026-01-05 12:00:00", temp=18.0, feels=16.0, owm_id=801, pop=0.1),
        _owm_block("2026-01-05 15:00:00", temp=19.0, feels=16.0, owm_id=803, pop=0.1),
        _owm_block("2026-01-05 18:00:00", temp=17.0, feels=15.0, owm_id=803, pop=0.1),
        _owm_block("2026-01-05 21:00:00", temp=15.0, feels=15.0, owm_id=803, pop=0.1),
    ])
    text = format_owm_forecast(current, forecast, "today", NOW)
    assert "резервный источник" in text
    assert "13°C" in text and "18°C" in text
    assert "Зонт" not in text  # вероятность низкая


def test_owm_fallback_rain_suggests_umbrella():
    current = _owm_current()
    forecast = _owm_forecast([
        _owm_block("2026-01-05 09:00:00", owm_id=500, pop=0.75),
        _owm_block("2026-01-05 12:00:00", owm_id=500, pop=0.75),
        _owm_block("2026-01-05 15:00:00", owm_id=500, pop=0.75),
        _owm_block("2026-01-05 18:00:00", owm_id=500, pop=0.75),
        _owm_block("2026-01-05 21:00:00", owm_id=500, pop=0.75),
    ])
    text = format_owm_forecast(current, forecast, "today", NOW)
    assert "☂️" in text and "Зонт:" in text


def test_owm_fallback_snow_does_not_suggest_umbrella():
    current = _owm_current()
    forecast = _owm_forecast([
        _owm_block("2026-01-05 09:00:00", owm_id=601, pop=0.75),
        _owm_block("2026-01-05 12:00:00", owm_id=601, pop=0.75),
        _owm_block("2026-01-05 15:00:00", owm_id=601, pop=0.75),
        _owm_block("2026-01-05 18:00:00", owm_id=601, pop=0.75),
        _owm_block("2026-01-05 21:00:00", owm_id=601, pop=0.75),
    ])
    text = format_owm_forecast(current, forecast, "today", NOW)
    assert "Зонт:" not in text
    assert "❄️" in text and "снег" in text.lower()


def test_owm_fallback_week_groups_by_day():
    blocks = []
    base = datetime.date(2026, 1, 5)
    for day_offset in range(3):
        d = base + datetime.timedelta(days=day_offset)
        for h in (0, 3, 6, 9, 12, 15, 18, 21):
            blocks.append(_owm_block(f"{d.isoformat()} {h:02d}:00:00", temp=10 + day_offset, owm_id=800))
    text = format_owm_forecast(_owm_current(), _owm_forecast(blocks), "week", NOW, days=7)
    assert "резервный источник" in text
    assert text.count("•") == 3


def test_get_weather_forecast_falls_back_when_open_meteo_down(monkeypatch):
    import services.weather as weather_module

    def _boom(*args, **kwargs):
        raise RuntimeError("сеть недоступна")

    def _fake_current(lat, lon, key):
        return _owm_current(temp=7.0, feels=4.0, owm_id=800)

    def _fake_forecast(lat, lon, key):
        return _owm_forecast([
            _owm_block("2026-01-05 09:00:00", temp=7.0, owm_id=800, pop=0.05),
            _owm_block("2026-01-05 12:00:00", temp=9.0, owm_id=800, pop=0.05),
        ])

    monkeypatch.setattr(weather_module, "_request_open_meteo", _boom)
    monkeypatch.setattr(weather_module, "OPENWEATHERMAP_API_KEY", "test-key")
    monkeypatch.setattr(weather_module, "_request_openweathermap_current", _fake_current)
    monkeypatch.setattr(weather_module, "_request_openweathermap_forecast", _fake_forecast)

    import asyncio
    text = asyncio.run(weather_module.get_weather_forecast(target="today"))
    assert text is not None
    assert "резервный источник" in text


def test_get_weather_forecast_returns_none_when_no_fallback_key(monkeypatch):
    import services.weather as weather_module

    def _boom(*args, **kwargs):
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr(weather_module, "_request_open_meteo", _boom)
    monkeypatch.setattr(weather_module, "OPENWEATHERMAP_API_KEY", "")

    import asyncio
    text = asyncio.run(weather_module.get_weather_forecast(target="today"))
    assert text is None

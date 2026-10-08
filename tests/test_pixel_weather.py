import asyncio
import datetime
import io
import math

from PIL import Image

from services.pixel_weather import gfx, model, phrase, render


def synth(code=3, base=6.0, rain=10, gust_kmh=30, cape=200):
    d0 = datetime.datetime(2026, 10, 8)
    times = [(d0 + datetime.timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(72)]
    t = [base + 4 * math.sin(h / 24 * 2 * math.pi) for h in range(72)]
    hourly = {
        "time": times, "temperature_2m": t, "apparent_temperature": [x - 3 for x in t],
        "precipitation_probability": [rain] * 72, "weather_code": [code] * 72,
        "wind_speed_10m": [20] * 72, "wind_gusts_10m": [gust_kmh] * 72, "relative_humidity_2m": [70] * 72,
        "surface_pressure": [950 - i * 0.2 for i in range(72)], "uv_index": [2] * 72, "visibility": [20000] * 72,
        "cape": [cape] * 72, "precipitation": [0] * 72,
    }
    daily = {"time": ["2026-10-08", "2026-10-09", "2026-10-10"], "sunrise": ["2026-10-08T07:12"] * 3,
             "sunset": ["2026-10-08T18:31"] * 3}
    return {"hourly": hourly, "daily": daily}


START = datetime.datetime(2026, 10, 8, 9)


def test_model_calm_day():
    m = model.build(synth(), START)
    assert m["threat"] == 0 and m["thunder"][0] == 0 and m["ice"][0] == 0
    assert m["precip"] == "СУХО"
    assert 5 < m["wind_max"] < 6.1                      # 20 км/ч ≈ 5.6 м/с


def test_model_thunder_and_wind_raise_threat():
    m = model.build(synth(code=95, gust_kmh=75), START)
    assert m["thunder"][0] == 2 and m["threat"] >= 2
    assert "гроза" in m["reasons"]


def test_model_ice_by_freezing_rain_code():
    m = model.build(synth(code=66, base=-1), START)
    assert m["ice"][0] == 2 and "гололёд" in m["reasons"]


def test_model_returns_none_without_data():
    assert model.build({"hourly": {"time": []}}, START) is None


def test_aqi_labels():
    assert model.aqi_label(None)[1] == -1
    assert model.aqi_label(30)[0] == "ЧИСТО"
    assert model.aqi_label(120)[1] == 3


def test_wrap_limits_lines():
    lines = gfx.wrap("очень " * 30, 20, 3)
    assert len(lines) == 3 and lines[-1].endswith("…") and all(len(x) <= 20 for x in lines)


def test_fallback_phrase_and_dedicated_phrase(monkeypatch):
    m = model.build(synth(code=95), START)
    assert "гроз" in phrase.fallback_phrase(m).lower()

    async def broken(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr("services.deepseek_service.generate_weather_phrase", broken)
    assert asyncio.run(phrase.make_phrase(m, "morning")) == phrase.fallback_phrase(m)

    async def long(*a, **k):
        return "х" * 300

    monkeypatch.setattr("services.deepseek_service.generate_weather_phrase", long)
    assert asyncio.run(phrase.make_phrase(m, "morning")) == phrase.fallback_phrase(m)

    async def good(*a, **k):
        return '"> Куртку надень, героиня."'

    monkeypatch.setattr("services.deepseek_service.generate_weather_phrase", good)
    assert asyncio.run(phrase.make_phrase(m, "morning")) == "Куртку надень, героиня."


def test_build_set_renders_gif_and_pages(monkeypatch):
    async def good(*a, **k):
        return "Дождь после обеда, зонт с собой."

    monkeypatch.setattr("services.deepseek_service.generate_weather_phrase", good)
    result = asyncio.run(render.build_set(synth(code=61, rain=80), START, "morning", {"aqi": 50, "pm25": 12}))
    gif = Image.open(io.BytesIO(result.gif))
    assert gif.n_frames > 30 and gif.size == (640, 360) and len(result.gif) < 2_000_000
    assert Image.open(io.BytesIO(result.chart_png)).size == (960, 540)
    assert Image.open(io.BytesIO(result.modules_png)).size == (960, 540)
    assert "Астана" in result.caption and len(result.caption) <= 1000


def test_render_without_air_and_night():
    m = model.build(synth(), datetime.datetime(2026, 10, 8, 21), None)
    assert m["night"] is True
    assert m["aqi_value"] is None
    from services.pixel_weather import pages
    pages.page_modules(m)
    pages.page_chart(m)

import asyncio
import datetime
import io
import math

from PIL import Image

from services.pixel_weather import gfx, insights, model, phrase, rad_pages, render


def synth(code=3, base=6.0, rain=10, gust_kmh=30, cape=200):
    d0 = datetime.datetime(2026, 10, 8)
    times = [(d0 + datetime.timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(72)]
    t = [base + 4 * math.sin(h / 24 * 2 * math.pi) for h in range(72)]
    hourly = {
        "time": times, "temperature_2m": t, "apparent_temperature": [x - 3 for x in t],
        "precipitation_probability": [rain] * 72, "weather_code": [code] * 72,
        "wind_speed_10m": [20] * 72, "wind_gusts_10m": [gust_kmh] * 72, "relative_humidity_2m": [70] * 72,
        "surface_pressure": [950 - i * 0.2 for i in range(72)], "uv_index": [2] * 72, "visibility": [20000] * 72,
        "cape": [cape] * 72, "precipitation": [0] * 72, "dew_point_2m": [t_ - 2 for t_ in t],
        "wind_direction_10m": [224] * 72, "pressure_msl": [1010] * 72, "cloud_cover": [80] * 72,
    }
    daily = {"time": ["2026-10-08", "2026-10-09", "2026-10-10"], "sunrise": ["2026-10-08T07:12"] * 3,
             "sunset": ["2026-10-08T18:31"] * 3, "weather_code": [3, 61, 0], "temperature_2m_max": [12, 10, 9],
             "temperature_2m_min": [3, 2, 1], "precipitation_probability_max": [10, 70, 5]}
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


def test_phrases_fallback_parse_and_clean(monkeypatch):
    m = render.build_model(synth(code=95), START, "morning")
    assert "роза" in phrase.fallback_phrase(m).lower() or "ГРОЗ" in phrase.fallback_phrase(m).upper()

    async def broken(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr("services.deepseek_service.generate_weather_phrase", broken)
    street, dispatch = asyncio.run(phrase.make_phrases(m, "morning"))
    assert street == insights.street_text(m) and dispatch == phrase.fallback_phrase(m)

    async def long(*a, **k):
        return "х" * 300 + "\n" + "у" * 300

    monkeypatch.setattr("services.deepseek_service.generate_weather_phrase", long)
    assert asyncio.run(phrase.make_phrases(m, "morning"))[1] == phrase.fallback_phrase(m)

    async def good(*a, **k):
        return '1. "Идёт дождь. Ровно и серо."\n> Дождь. Твои кроссовки тебе этого не простят.'

    monkeypatch.setattr("services.deepseek_service.generate_weather_phrase", good)
    assert asyncio.run(phrase.make_phrases(m, "morning")) == ("Идёт дождь. Ровно и серо.", "Дождь. Твои кроссовки тебе этого не простят.")
    assert phrase.parse_lines("одна строка") is None


def test_insights_helpers():
    assert insights.compass_name(224) == "ЮЗ" and insights.compass_name(0) == "С" and insights.compass_name(None) == "—"
    assert insights.wind_level(30)[1] == "Умеренный" and insights.wind_level(1)[1] == "Штиль"
    k, label = insights.gust_factor(30, 63)
    assert round(k, 1) == 2.1 and label == "очень турбулентно"


def test_enrich_fields_calm_and_rain():
    m = render.build_model(synth(code=61, rain=80), START, "morning")
    assert m["advice"].startswith("Дождь") or "Без осадков" in m["advice"] or "Мороз" in m["advice"] or m["advice"]
    assert len(m["daily"]) == 3 and m["daily"][0]["tmax"] == 12
    assert m["arc"]["rise"] == "07:12" and m["arc"]["golden"] == "18:01"
    assert m["intel"]["name"] in insights.THREAT_NAMES and 1 <= m["intel"]["seg"] <= 10
    assert m["confidence"][0] in ("ВЫСОКИЙ", "СРЕДНИЙ", "НИЗКИЙ")
    assert m["layer"]["level"] in ("Нет", "Слабый", "Умеренный", "Сильный")
    assert render.build_model(synth(), START, "evening")["tomorrow"] is True


def test_build_set_renders_everything(monkeypatch):
    async def good(*a, **k):
        return "Идёт дождь. Ровно и серо.\nДождь. Твои кроссовки тебе этого не простят."

    monkeypatch.setattr("services.deepseek_service.generate_weather_phrase", good)
    result = asyncio.run(render.build_set(synth(code=61, rain=80), START, "morning", {"aqi": 50, "pm25": 12}))
    gif = Image.open(io.BytesIO(result.gif))
    assert gif.n_frames > 30 and gif.size == (640, 360) and len(result.gif) < 3_000_000
    assert len(result.pages) == 1 and result.names == ["day"]
    for png in result.pages:
        assert Image.open(io.BytesIO(png)).size[0] == 1080
    assert result.dispatch == b""
    assert "Астана" in result.caption and len(result.caption) <= 1000


def test_render_without_air_night_and_missing_extras():
    data = synth()
    for var in ("wind_gusts_10m", "dew_point_2m", "wind_direction_10m", "pressure_msl", "uv_index", "visibility",
                "cloud_cover", "relative_humidity_2m", "surface_pressure"):
        data["hourly"].pop(var)
    data["daily"] = {"time": ["2026-10-08"], "sunrise": [], "sunset": []}
    m = render.build_model(data, datetime.datetime(2026, 10, 8, 21), "evening", None)
    assert m["night"] is True and m["aqi_value"] is None and m["arc"] is None
    for page in (rad_pages.page_day(m), rad_pages.page_now(m, "тест", None), rad_pages.page_terminal(m), rad_pages.page_wind_rain(m),
                 rad_pages.page_spread_sun(m), rad_pages.page_days(m), rad_pages.page_tiles(m)):
        assert page[:4] == b"\x89PNG"


def test_delivery_sends_in_order_and_survives_partial_failure():
    from services.pixel_weather import delivery
    pix = type("P", (), {"gif": b"g", "pages": [b"1", b"2"], "names": ["a", "b"], "dispatch": b"d", "caption": "c"})()
    log = []

    async def anim(f, c):
        log.append("anim")

    async def group(g):
        log.append(("group", len(g)))
        raise RuntimeError("album failed")

    async def photo(f, k):
        log.append("photo")

    assert asyncio.run(delivery.deliver(pix, "morning", anim, group, photo)) is True
    assert log == ["anim", ("group", 2), "photo"]

    async def bad_anim(f, c):
        raise RuntimeError("no")

    assert asyncio.run(delivery.deliver(pix, "morning", bad_anim, group, photo)) is False


def test_delivery_single_page_goes_as_photo():
    from services.pixel_weather import delivery
    pix = type("P", (), {"gif": b"g", "pages": [b"1"], "names": ["day"], "dispatch": b"", "caption": "c"})()
    log = []

    async def anim(f, c):
        log.append("anim")

    async def group(g):
        log.append("group")

    async def photo(f, k):
        log.append(("photo", k))

    assert asyncio.run(delivery.deliver(pix, "morning", anim, group, photo)) is True
    assert log == ["anim", ("photo", None)]

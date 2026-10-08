"""Данные для пиксельной погоды: почасовые точки и производные показатели (код считает, не нейросеть)."""
import datetime

from services import weather as w
from services import weather_chart as wc

KMH = 1 / 3.6
HPA_TO_MMHG = 0.750062
EXTRA_HOURLY = ("relative_humidity_2m,surface_pressure,uv_index,visibility,wind_gusts_10m,cape,"
                "precipitation,snowfall,cloud_cover")
FREEZING_CODES = {56, 57, 66, 67}


def _at(hourly: dict, var: str, i: int):
    try:
        v = (hourly.get(var) or [])[i]
    except (IndexError, TypeError):
        return None
    return float(v) if isinstance(v, (int, float)) else None


def build_points(data: dict, start: datetime.datetime, hours: int = 24) -> list[dict]:
    base = wc.build_points(data, start, hours)
    hourly = (data or {}).get("hourly") or {}
    times = hourly.get("time") or []
    for p in base:
        key = p["dt"].strftime("%Y-%m-%dT%H:00")
        i = times.index(key) if key in times else -1
        p["wind_ms"] = (p.get("wind") or 0.0) * KMH
        gust = _at(hourly, "wind_gusts_10m", i) if i >= 0 else None
        p["gust_ms"] = (gust * KMH) if gust is not None else p["wind_ms"]
        for name, var in (("hum", "relative_humidity_2m"), ("pres", "surface_pressure"), ("uv", "uv_index"),
                          ("vis", "visibility"), ("cape", "cape"), ("precip", "precipitation"),
                          ("snowfall", "snowfall"), ("cloud", "cloud_cover")):
            p[name] = _at(hourly, var, i) if i >= 0 else None
        if p["pres"] is not None:
            p["pres"] *= HPA_TO_MMHG
    return base


def _span(hours: list[datetime.datetime]) -> str:
    """Компактно для плитки: «14-17ч» или «в 14ч»."""
    if not hours:
        return ""
    a, b = min(hours), max(hours)
    return f"в {a:%H}ч" if a == b else f"{a:%H}-{b:%H}ч"


def _num(v, default=0.0):
    return default if v is None else v


def thunder_risk(points: list[dict]) -> tuple[int, str]:
    codes = [p for p in points if p.get("code") in wc._STORM]
    if codes:
        return 2, f"ГРОЗА {_span([p['dt'] for p in codes])}"
    risky = [p for p in points if _num(p.get("cape")) >= 1000 and _num(p.get("rain")) >= 40]
    if risky:
        return 1, f"ВОЗМОЖНА {_span([p['dt'] for p in risky])}"
    return 0, "НЕТ"


def ice_risk(points: list[dict]) -> tuple[int, str]:
    frz = [p for p in points if p.get("code") in FREEZING_CODES]
    if frz:
        return 2, f"ВЫСОКИЙ {_span([p['dt'] for p in frz])}"
    wet = []
    for prev, cur in zip(points, points[1:]):
        near_zero = -3 <= cur["temp"] <= 1
        crossed = (prev["temp"] > 0 >= cur["temp"]) or (prev["temp"] < 0 <= cur["temp"])
        wet_now = _num(cur.get("rain")) >= 50 or _num(cur.get("precip")) >= 0.2
        humid = _num(cur.get("hum")) >= 90 and cur["temp"] <= 0
        if (near_zero and wet_now) or (crossed and (wet_now or humid)):
            wet.append(cur["dt"])
    if wet:
        return 1, f"ВОЗМОЖЕН {_span(wet)}"
    return 0, "НЕТ"


def uv_info(points: list[dict]) -> tuple[float, str]:
    vals = [p["uv"] for p in points if p.get("uv") is not None]
    if not vals:
        return 0.0, "—"
    top = max(vals)
    label = "НИЗКИЙ" if top < 3 else "УМЕРЕН." if top < 6 else "ВЫСОКИЙ" if top < 8 else "ОЧ.ВЫС."
    return top, label


def precip_text(points: list[dict]) -> str:
    snow = wc._precip_hours(points, wc._SNOW)
    rain = wc._precip_hours(points, wc._RAIN | wc._STORM)
    if snow:
        return f"СНЕГ {_span(snow)}"
    if rain:
        return f"ДОЖДЬ {_span(rain)}"
    peak = max((_num(p.get("rain")) for p in points), default=0)
    return "СУХО" if peak < 30 else f"ВОЗМ.{peak:.0f}%"


def aqi_label(value) -> tuple[str, int]:
    """(подпись, уровень 0..3) по европейскому индексу."""
    if value is None:
        return "НЕТ ДАННЫХ", -1
    if value <= 40:
        return "ЧИСТО", 0
    if value <= 60:
        return "НОРМА", 0
    if value <= 80:
        return "СРЕДНЕ", 1
    if value <= 100:
        return "ПЛОХО", 2
    return "ОЧЕНЬ ПЛОХО", 3


def pressure_trend(points: list[dict]) -> tuple[float | None, str]:
    vals = [p["pres"] for p in points if p.get("pres") is not None]
    if len(vals) < 2:
        return None, "—"
    diff = vals[min(len(vals) - 1, 12)] - vals[0]
    arrow = "▲" if diff > 1.5 else "▼" if diff < -1.5 else "="
    return vals[0], f"{arrow}{abs(diff):.0f} за 12ч"


def threat(points: list[dict], thunder: int, ice: int) -> tuple[int, list[str]]:
    reasons, level = [], 0

    def bump(lv, why):
        nonlocal level
        level = max(level, lv)
        reasons.append(why)

    tmin = min(p["temp"] for p in points)
    tmax = max(p["temp"] for p in points)
    fmin = min(p["feels"] for p in points)
    gust = max(p["gust_ms"] for p in points)
    if tmin <= -35 or fmin <= -40:
        bump(3, "лютый мороз")
    elif tmin <= -25 or fmin <= -30:
        bump(2, "сильный мороз")
    elif tmin <= -15:
        bump(1, "мороз")
    if tmax >= 38:
        bump(3, "экстремальная жара")
    elif tmax >= 33:
        bump(2, "жара")
    elif tmax >= 30:
        bump(1, "жарко")
    if gust >= 25:
        bump(3, "ураганный ветер")
    elif gust >= 17:
        bump(2, "сильный ветер")
    elif gust >= 12:
        bump(1, "порывистый ветер")
    if thunder == 2:
        bump(2, "гроза")
    elif thunder == 1:
        bump(1, "возможна гроза")
    if ice == 2:
        bump(2, "гололёд")
    elif ice == 1:
        bump(1, "возможен гололёд")
    heavy = [p for p in points if _num(p.get("precip")) >= 4 or p.get("code") in (65, 75, 82, 86)]
    if heavy:
        bump(2, "сильные осадки")
    vis = [p["vis"] for p in points if p.get("vis") is not None]
    if vis and min(vis) < 500:
        bump(1, "туман, видимость <500 м")
    return level, reasons


def golden_hour(sun: tuple[str, str] | None) -> str:
    """Золотой час вечером: за час до заката."""
    if not sun:
        return "—"
    h, m = map(int, sun[1].split(":"))
    t = datetime.datetime(2000, 1, 1, h, m) - datetime.timedelta(hours=1)
    return f"{t:%H:%M}"


def build(data: dict, start: datetime.datetime, aqi: dict | None = None) -> dict | None:
    points = build_points(data, start, 24)
    if len(points) < 4:
        return None
    sun = wc.sun_info(data, start)
    thunder = thunder_risk(points)
    ice = ice_risk(points)
    level, reasons = threat(points, thunder[0], ice[0])
    first = points[0]
    night = False
    if sun:
        night = not (sun[0] <= f"{start:%H:%M}" < sun[1])
    else:
        night = start.hour < 6 or start.hour >= 21
    pres, ptrend = pressure_trend(points)
    uv = uv_info(points)
    return {
        "start": start,
        "points": points,
        "first": first,
        "night": night,
        "sun": sun,
        "golden": golden_hour(sun),
        "moon": wc.moon_phase(start),
        "tmin": min(p["temp"] for p in points),
        "tmax": max(p["temp"] for p in points),
        "wind_max": max(p["wind_ms"] for p in points),
        "gust_max": max(p["gust_ms"] for p in points),
        "thunder": thunder,
        "ice": ice,
        "uv": uv,
        "precip": precip_text(points),
        "rain_peak": max(_num(p.get("rain")) for p in points),
        "aqi": aqi,
        "aqi_value": (aqi or {}).get("aqi"),
        "pm25": (aqi or {}).get("pm25"),
        "pres": pres,
        "pres_trend": ptrend,
        "hum": first.get("hum"),
        "vis_min": min((p["vis"] for p in points if p.get("vis") is not None), default=None),
        "threat": level,
        "reasons": reasons,
        "kind": None,
        "clothes": wc.clothes_text(points),
    }


def facts_line(m: dict) -> str:
    """Короткое описание для фразы Ады: только числа и события, посчитанные кодом."""
    f = m["first"]
    parts = [f"температура {f['temp']:.0f}°, ощущается {f['feels']:.0f}°, за сутки от {m['tmin']:.0f}° до {m['tmax']:.0f}°",
             f"ветер до {m['wind_max']:.0f} м/с", f"осадки: {m['precip'].lower()}"]
    if m["reasons"]:
        parts.append("предупреждения: " + ", ".join(m["reasons"]))
    if m["aqi_value"] is not None:
        parts.append(f"воздух: {aqi_label(m['aqi_value'])[0].lower()}")
    return "; ".join(parts)

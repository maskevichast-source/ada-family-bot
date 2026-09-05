"""Простой прогноз погоды для Астаны через Open-Meteo.

Формат специально короткий: семья должна быстро понять день, осадки и одежду,
а не читать метеосводку на пол-экрана.
"""

import asyncio
import math
import datetime
import urllib.parse
import urllib.request
from services.timezone import ASTANA_TZ

ASTANA_LATITUDE = 51.169392
ASTANA_LONGITUDE = 71.449074

WEATHER_DESCRIPTIONS = {
    0: "ясно ☀️",
    1: "почти ясно 🌤",
    2: "облачно с прояснениями ⛅",
    3: "пасмурно ☁️",
    45: "туман 🌫",
    48: "туман/изморозь 🌫",
    51: "морось 🌧",
    53: "морось 🌧",
    55: "сильная морось 🌧",
    56: "ледяная морось 🌧",
    57: "ледяная морось 🌧",
    61: "небольшой дождь 🌦",
    63: "дождь 🌧",
    65: "сильный дождь 🌧",
    66: "ледяной дождь 🌧",
    67: "ледяной дождь 🌧",
    71: "небольшой снег 🌨",
    73: "снег 🌨",
    75: "сильный снег ❄️",
    77: "снежная крупа ❄️",
    80: "ливень 🌦",
    81: "ливень 🌧",
    82: "сильный ливень 🌧",
    85: "снежный ливень 🌨",
    86: "сильный снежный ливень ❄️",
    95: "гроза ⛈",
    96: "гроза с градом ⛈",
    99: "гроза с сильным градом ⛈",
}

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _describe(code: object) -> str:
    try:
        return WEATHER_DESCRIPTIONS.get(int(code), "непонятно по небу")
    except (TypeError, ValueError):
        return "непонятно по небу"


def _num(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _format_temp(val: object) -> str:
    value = _num(val)
    if value is None:
        return "нет данных"
    return f"{value:.0f}°C"


def _request_open_meteo(forecast_days: int = 7) -> dict:
    params = urllib.parse.urlencode({
        "latitude": ASTANA_LATITUDE,
        "longitude": ASTANA_LONGITUDE,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max",
        "timezone": "Asia/Almaty",
        "wind_speed_unit": "kmh",
        "forecast_days": max(1, min(int(forecast_days), 7)),
    })
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    with urllib.request.urlopen(url, timeout=12) as response:
        import json
        return json.loads(response.read().decode("utf-8"))


def _clothes_advice(temp_min: float | None, temp_max: float | None, rain: int | None, wind: float | None) -> str:
    if temp_max is None:
        return "Что надеть: ориентируйтесь по фактической температуре за окном."

    parts = []
    if temp_max <= -15:
        parts.append("очень тёплая куртка, шапка и перчатки")
    elif temp_max <= -5:
        parts.append("зимняя куртка и шапка")
    elif temp_max <= 5:
        parts.append("тёплая куртка")
    elif temp_max <= 12:
        parts.append("куртка или плотная ветровка")
    elif temp_max <= 20:
        parts.append("худи/лёгкая куртка")
    else:
        parts.append("лёгкая одежда")

    if rain is not None and rain >= 45:
        parts.append("зонт лучше взять")
    if wind is not None and wind >= 30:
        parts.append("будет ветрено — капюшон или шарф пригодятся")

    return "Что надеть: " + ", ".join(parts) + "."


def _day_title(dt: datetime.date, today: datetime.date) -> str:
    if dt == today:
        return "Сегодня"
    if dt == today + datetime.timedelta(days=1):
        return "Завтра"
    return f"{WEEKDAYS_RU[dt.weekday()].capitalize()}, {dt.strftime('%d.%m')}"


def _at(block, field, idx):
    values = block.get(field) or []
    return values[idx] if idx < len(values) else None

def _percent(value):
    n = _num(value)
    return f"{n:.0f}%" if n is not None else "нет данных"

def _wind(value):
    n = _num(value)
    return f"{n:.0f} км/ч" if n is not None else "нет данных"

def format_forecast(data, target="today", now=None):
    now = now or datetime.datetime.now(ASTANA_TZ)
    daily = data.get("daily") or {}
    times = daily.get("time") or []
    if not times:
        return None
    if target == "week":
        blocks = ["📅 Астана · прогноз на 7 дней"]
        for i, raw_date in enumerate(times[:7]):
            date = datetime.date.fromisoformat(raw_date)
            blocks.append(
                f"{_day_title(date, now.date())} · {date.strftime('%d.%m')}\n"
                f"{_describe(_at(daily, 'weather_code', i))}\n"
                f"Мин. {_format_temp(_at(daily, 'temperature_2m_min', i))} / "
                f"макс. {_format_temp(_at(daily, 'temperature_2m_max', i))}\n"
                f"Осадки {_percent(_at(daily, 'precipitation_probability_max', i))} · "
                f"ветер до {_wind(_at(daily, 'wind_speed_10m_max', i))}")
        blocks.append("Источник: Open-Meteo · время Астаны")
        return "\n\n".join(blocks)
    offset = {"today": 0, "tomorrow": 1, "after_tomorrow": 2}.get(target, 0)
    date = now.date() + datetime.timedelta(days=offset)
    if date.isoformat() not in times:
        return None
    i = times.index(date.isoformat())
    lo = _num(_at(daily, "temperature_2m_min", i))
    hi = _num(_at(daily, "temperature_2m_max", i))
    rain = _num(_at(daily, "precipitation_probability_max", i))
    wind = _num(_at(daily, "wind_speed_10m_max", i))
    blocks = [f"📍 Астана · {_day_title(date, now.date())}, {date.strftime('%d.%m')}",
        f"{_describe(_at(daily, 'weather_code', i))}\n"
        f"🌡 Мин. {_format_temp(lo)} / макс. {_format_temp(hi)}\n"
        f"💧 Вероятность осадков: {_percent(rain)}\n"
        f"💨 Ветер: до {_wind(wind)}"]
    current = data.get("current") or {}
    if offset == 0 and current:
        blocks.append(f"Сейчас · {_format_temp(current.get('temperature_2m'))}\n"
                      f"Ощущается как {_format_temp(current.get('apparent_temperature'))}")
    hourly = data.get("hourly") or {}
    slots = []
    for idx, timestamp in enumerate(hourly.get("time") or []):
        try:
            point = datetime.datetime.fromisoformat(timestamp).replace(tzinfo=ASTANA_TZ)
        except (TypeError, ValueError):
            continue
        if point.date() != date or point.hour not in (9, 14, 19) or (offset == 0 and point < now):
            continue
        slots.append(f"{point.strftime('%H:%M')} · {_format_temp(_at(hourly, 'temperature_2m', idx))} · "
                     f"{_describe(_at(hourly, 'weather_code', idx))}")
    if slots:
        blocks.append("По времени\n" + "\n".join(slots))
    # Use lower daytime temperature instead of daily max alone.
    daytime = [_num(_at(hourly, "temperature_2m", idx)) for idx, raw in enumerate(hourly.get("time") or [])
               if str(raw).startswith(date.isoformat()) and str(raw)[11:13] in {"09", "14", "19"}]
    daytime = [v for v in daytime if v is not None]
    advice_temp = min(daytime) if daytime else lo
    advice = _clothes_advice(lo, advice_temp, rain, wind)
    if advice_temp is not None and advice_temp <= 0:
        advice = advice.replace("зонт лучше взять", "обувь с нескользящей подошвой")
    blocks.append("🧥 " + advice)
    blocks.append("Источник: Open-Meteo · время Астаны")
    return "\n\n".join(blocks)

async def get_weather_forecast(target="today", days=1):
    try:
        if days >= 4:
            target = "week"
        required = {"today": 1, "tomorrow": 2, "after_tomorrow": 3, "week": 7}.get(target, 1)
        data = await asyncio.to_thread(_request_open_meteo, required)
        return format_forecast(data, target)
    except Exception:
        import logging
        logging.exception("Open-Meteo unavailable")
        return None

async def get_tomorrow_forecast():
    return await get_weather_forecast("tomorrow")

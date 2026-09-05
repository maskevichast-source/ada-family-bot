"""Простой прогноз погоды для Астаны через Open-Meteo.

Формат специально короткий: семья должна быстро понять день, осадки и одежду,
а не читать метеосводку на пол-экрана.
"""

import asyncio
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
        return float(value)
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


async def get_weather_forecast(target: str = "today", days: int = 1) -> str | None:
    """Вернуть короткий прогноз.

    target:
    - today
    - tomorrow
    - after_tomorrow
    - week
    """
    try:
        req_days = 7 if target in {"week", "after_tomorrow"} or days >= 4 else (2 if target == "tomorrow" else 1)
        data = await asyncio.to_thread(_request_open_meteo, req_days)

        now = datetime.datetime.now(ASTANA_TZ)
        today_date = now.date()

        if target == "week" or days >= 4:
            daily = data.get("daily", {})
            times = daily.get("time", [])
            lines = ["📅 Погода в Астане на неделю:"]
            for i, d_str in enumerate(times[:7]):
                dt = datetime.datetime.strptime(d_str, "%Y-%m-%d").date()
                t_min = _num(daily.get("temperature_2m_min", [None])[i])
                t_max = _num(daily.get("temperature_2m_max", [None])[i])
                desc = _describe(daily.get("weather_code", [None])[i])
                rain = daily.get("precipitation_probability_max", [None])[i]
                lines.append(f"• {_day_title(dt, today_date)}: {_format_temp(t_min)}…{_format_temp(t_max)}, {desc}, осадки {rain}%")
            lines.append("\nЕсли планируете поездку — скажи куда и когда, я сверю прогноз под даты.")
            return "\n".join(lines)

        offset = 0
        if target == "tomorrow":
            offset = 1
        elif target == "after_tomorrow":
            offset = 2

        target_date = today_date + datetime.timedelta(days=offset)
        date_str = target_date.strftime("%Y-%m-%d")
        title = "сегодня" if offset == 0 else ("завтра" if offset == 1 else "послезавтра")

        hourly = data.get("hourly", {})
        hourly_map = {str(t): i for i, t in enumerate(hourly.get("time", []))}

        temps, rains, winds = [], [], []
        hours = [9, 12, 15, 18, 21]
        if offset == 0:
            hours = [h for h in hours if h >= now.hour] or [18, 21]

        cur = data.get("current", {})
        lines = [f"🌤 Погода в Астане на {title}"]

        if offset == 0:
            lines.append(
                f"Сейчас: {_format_temp(cur.get('temperature_2m'))}, {_describe(cur.get('weather_code'))}. "
                f"Ощущается как {_format_temp(cur.get('apparent_temperature'))}."
            )

        period_names = {9: "утро", 12: "день", 15: "день", 18: "вечер", 21: "вечер"}
        for h in hours:
            key = f"{date_str}T{h:02d}:00"
            idx = hourly_map.get(key)
            if idx is None:
                continue
            temp = _num(hourly["temperature_2m"][idx])
            rain = hourly["precipitation_probability"][idx]
            wind = _num(hourly["wind_speed_10m"][idx])
            code = hourly["weather_code"][idx]
            if temp is not None:
                temps.append(temp)
            rains.append(rain)
            if wind is not None:
                winds.append(wind)
            lines.append(f"• {period_names.get(h, str(h))} {h:02d}:00 — {_format_temp(temp)}, {_describe(code)}, осадки {rain}%")

        if temps:
            lines.append("")
            lines.append(_clothes_advice(min(temps), max(temps), max(rains) if rains else None, max(winds) if winds else None))

        return "\n".join(lines)

    except Exception as error:
        print(f"[Погода] Ошибка Open-Meteo: {error}")
        return None


async def get_tomorrow_forecast() -> str | None:
    """Для фоновой рассылки в 22:30."""
    return await get_weather_forecast(target="tomorrow")

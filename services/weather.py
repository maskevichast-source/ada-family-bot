"""Почасовой и многодневный прогноз погоды для Астаны через Open-Meteo (до 7 дней)."""

import asyncio
import datetime
import json
import urllib.parse
import urllib.request
from services.timezone import ASTANA_TZ

ASTANA_LATITUDE = 51.169392
ASTANA_LONGITUDE = 71.449074

WEATHER_DESCRIPTIONS = {
    0: "ясно ☀️",
    1: "преимущественно ясно 🌤",
    2: "переменная облачность ⛅",
    3: "пасмурно ☁️",
    45: "туман 🌫",
    48: "изморозь и туман 🌫",
    51: "слабая морось 🌧",
    53: "морось 🌧",
    55: "сильная морось 🌧",
    56: "слабая ледяная морось 🌧",
    57: "сильная ледяная морось 🌧",
    61: "небольшой дождь 🌦",
    63: "дождь 🌧",
    65: "сильный дождь 🌧",
    66: "небольшой ледяной дождь 🌧",
    67: "сильный ледяной дождь 🌧",
    71: "небольшой снег 🌨",
    73: "снег 🌨",
    75: "сильный снег ❄️",
    77: "снежные зёрна ❄️",
    80: "небольшой ливень 🌦",
    81: "ливень 🌧",
    82: "сильный ливень 🌧",
    85: "слабый снежный ливень 🌨",
    86: "сильный снежный ливень ❄️",
    95: "гроза ⛈",
    96: "гроза с небольшим градом ⛈",
    99: "гроза с сильным градом ⛈",
}

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _describe(code: object) -> str:
    try:
        return WEATHER_DESCRIPTIONS.get(int(code), "неизвестно")
    except (TypeError, ValueError):
        return "неизвестно"


def _format_temp(val: object) -> str:
    try:
        return f"{float(val):.0f}°C"
    except (TypeError, ValueError):
        return "нет данных"


def _request_open_meteo(forecast_days: int = 7) -> dict:
    params = urllib.parse.urlencode({
        "latitude": ASTANA_LATITUDE,
        "longitude": ASTANA_LONGITUDE,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max",
        "forecast_days": max(1, min(forecast_days, 7)),
        "timezone": "Asia/Almaty",
    })
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "FamilyFinanceBot/2.4"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def get_weather_forecast(target: str = "today", days: int = 1) -> str | None:
    """Универсальный запрос погоды:
    - target="today": сегодня (с учётом текущего часа)
    - target="tomorrow": на завтра
    - target="after_tomorrow": на послезавтра
    - target="week": сводка на 5-7 дней
    """
    try:
        req_days = 7 if target in ["week", "after_tomorrow"] or days > 2 else (2 if target == "tomorrow" else 1)
        data = await asyncio.to_thread(_request_open_meteo, req_days)

        now = datetime.datetime.now(ASTANA_TZ)
        today_date = now.date()

        # ── 1. ПРОГНОЗ НА НЕДЕЛЮ ──
        if target == "week" or days >= 4:
            daily = data.get("daily", {})
            times = daily.get("time", [])
            lines = ["📅 **ПРОГНОЗ ПОГОДЫ В АСТАНЕ НА НЕДЕЛЮ**\n"]
            for i, d_str in enumerate(times[:7]):
                dt = datetime.datetime.strptime(d_str, "%Y-%m-%d").date()
                w_day = WEEKDAYS_RU[dt.weekday()].capitalize()
                desc = _describe(daily["weather_code"][i])
                t_min = _format_temp(daily["temperature_2m_min"][i])
                t_max = _format_temp(daily["temperature_2m_max"][i])
                rain = daily["precipitation_probability_max"][i]
                wind = daily["wind_speed_10m_max"][i]
                day_title = "Сегодня" if dt == today_date else ("Завтра" if dt == today_date + datetime.timedelta(days=1) else f"{w_day}, {dt.strftime('%d.%m')}")
                lines.append(f"• **{day_title}**: от {t_min} до {t_max}, {desc} (осадки {rain}%, ветер до {wind} км/ч)")
            lines.append("\nОдевайтесь по погоде, планируйте неделю уверенно!")
            return "\n".join(lines)

        # ── 2. ПРОГНОЗ НА ЗАВТРА ИЛИ ПОСЛЕЗАВТРА ──
        if target in ["tomorrow", "after_tomorrow"]:
            offset = 1 if target == "tomorrow" else 2
            target_date = today_date + datetime.timedelta(days=offset)
            date_str = target_date.strftime("%Y-%m-%d")
            w_name = WEEKDAYS_RU[target_date.weekday()].upper()
            title_word = "ЗАВТРА" if offset == 1 else "ПОСЛЕЗАВТРА"

            hourly = data.get("hourly", {})
            hourly_map = {str(t): i for i, t in enumerate(hourly.get("time", []))}

            lines = [f"🌙 **ПРОГНОЗ НА {title_word}, {w_name} ({target_date.strftime('%d.%m')})**"]
            day_temps = []
            rain_probs = []

            for h in [6, 9, 12, 15, 18, 21]:
                key = f"{date_str}T{h:02d}:00"
                idx = hourly_map.get(key)
                if idx is not None:
                    t = hourly["temperature_2m"][idx]
                    w = _describe(hourly["weather_code"][idx])
                    p = hourly["precipitation_probability"][idx]
                    wind = hourly["wind_speed_10m"][idx]
                    day_temps.append(t)
                    rain_probs.append(p)
                    lines.append(f"{h:02d}:00 — **{_format_temp(t)}**, {w}; осадки {p}%, ветер {wind} км/ч")

            if day_temps:
                lines.append(f"\nЗа сутки: от {min(day_temps):.0f}°C до {max(day_temps):.0f}°C, макс. вероятность осадков {max(rain_probs)}%.")
            return "\n".join(lines)

        # ── 3. ПРОГНОЗ НА СЕГОДНЯ (ДИНАМИЧЕСКИЙ) ──
        curr = data.get("current", {})
        hourly = data.get("hourly", {})
        hourly_map = {str(t): i for i, t in enumerate(hourly.get("time", []))}
        date_str = today_date.strftime("%Y-%m-%d")

        cur_temp = _format_temp(curr.get("temperature_2m"))
        app_temp = _format_temp(curr.get("apparent_temperature"))
        cur_desc = _describe(curr.get("weather_code"))
        cur_wind = curr.get("wind_speed_10m", "-")

        lines = [
            "☀️ **ПРОГНОЗ ДЛЯ АСТАНЫ НА СЕГОДНЯ**",
            f"Сейчас: **{cur_temp}**, {cur_desc} (ощущается как {app_temp}), ветер {cur_wind} км/ч.\n",
        ]

        # Если вечер (после 19:00), показываем остаток вечера и ночь, а не утро
        if now.hour >= 19:
            hours_to_show = [h for h in [20, 22, 23] if h >= now.hour] or [22]
            lines.append("До конца дня:")
            for h in hours_to_show:
                key = f"{date_str}T{h:02d}:00"
                idx = hourly_map.get(key)
                if idx is not None:
                    lines.append(f"{h:02d}:00 — **{_format_temp(hourly['temperature_2m'][idx])}**, {_describe(hourly['weather_code'][idx])}, осадки {hourly['precipitation_probability'][idx]}%")
            lines.append("\n_День подходит к концу. Чтобы узнать погоду на утро, спроси: «прогноз на завтра»._")
        else:
            # Показываем только предстоящие часы
            hours_to_show = [h for h in [9, 12, 15, 18, 21] if h >= now.hour]
            if not hours_to_show:
                hours_to_show = [15, 18, 21]
            for h in hours_to_show:
                key = f"{date_str}T{h:02d}:00"
                idx = hourly_map.get(key)
                if idx is not None:
                    lines.append(f"{h:02d}:00 — **{_format_temp(hourly['temperature_2m'][idx])}**, {_describe(hourly['weather_code'][idx])}; осадки {hourly['precipitation_probability'][idx]}%, ветер {hourly['wind_speed_10m'][idx]} км/ч")
            lines.append("\nОдевайтесь по погоде, а не по оптимизму!")

        return "\n".join(lines)

    except Exception as error:
        print(f"[Погода] Ошибка Open-Meteo: {error}")
        return None


async def get_tomorrow_forecast() -> str | None:
    """Для фоновой рассылки в 22:30."""
    return await get_weather_forecast(target="tomorrow")
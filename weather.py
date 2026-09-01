"""Почасовой прогноз погоды для Астаны через Open-Meteo."""

import asyncio
import datetime
import json
import urllib.parse
import urllib.request
from services.timezone import ASTANA_TZ


ASTANA_LATITUDE = 51.169392
ASTANA_LONGITUDE = 71.449074
WEATHER_DESCRIPTIONS = {
    0: "ясно",
    1: "преимущественно ясно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "изморозь и туман",
    51: "слабая морось",
    53: "морось",
    55: "сильная морось",
    56: "слабая ледяная морось",
    57: "сильная ледяная морось",
    61: "небольшой дождь",
    63: "дождь",
    65: "сильный дождь",
    66: "небольшой ледяной дождь",
    67: "сильный ледяной дождь",
    71: "небольшой снег",
    73: "снег",
    75: "сильный снег",
    77: "снежные зёрна",
    80: "небольшой ливень",
    81: "ливень",
    82: "сильный ливень",
    85: "слабый снежный ливень",
    86: "сильный снежный ливень",
    95: "гроза",
    96: "гроза с небольшим градом",
    99: "гроза с сильным градом",
}


def _request_forecast(forecast_days: int = 1) -> dict:
    parameters = urllib.parse.urlencode(
        {
            "latitude": ASTANA_LATITUDE,
            "longitude": ASTANA_LONGITUDE,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "hourly": "temperature_2m,apparent_temperature,precipitation_probability,weather_code,wind_speed_10m",
            "forecast_days": forecast_days,
            "timezone": "Asia/Almaty",
        }
    )
    url = f"https://api.open-meteo.com/v1/forecast?{parameters}"
    request = urllib.request.Request(url, headers={"User-Agent": "FamilyFinanceBot/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _describe(code: object) -> str:
    try:
        return WEATHER_DESCRIPTIONS.get(int(code), "неизвестные погодные условия")
    except (TypeError, ValueError):
        return "неизвестные погодные условия"


def _format_temperature(value: object) -> str:
    try:
        return f"{float(value):.0f}°C"
    except (TypeError, ValueError):
        return "нет данных"


async def get_weather_forecast() -> str | None:
    """Получить и сформировать утренний прогноз (на сегодня); при сбое вернуть None."""
    try:
        forecast = await asyncio.to_thread(_request_forecast, 1)
        current = forecast["current"]
        hourly = forecast["hourly"]
        hourly_by_time = {
            str(timestamp): index
            for index, timestamp in enumerate(hourly["time"])
        }
        today = datetime.datetime.now(ASTANA_TZ).strftime("%Y-%m-%d")
        requested_hours = [10, 14, 16, 18, 20]

        lines = [
            "☀️ **УТРЕННИЙ ПРОГНОЗ ДЛЯ АСТАНЫ**",
            f"Сейчас: **{_format_temperature(current.get('temperature_2m'))}**, "
            f"{_describe(current.get('weather_code'))}; "
            f"ощущается как {_format_temperature(current.get('apparent_temperature'))}, "
            f"ветер {current.get('wind_speed_10m', 'нет данных')} км/ч.",
        ]
        for hour in requested_hours:
            timestamp = f"{today}T{hour:02d}:00"
            index = hourly_by_time.get(timestamp)
            if index is None:
                continue
            lines.append(
                f"{hour:02d}:00 — **{_format_temperature(hourly['temperature_2m'][index])}**, "
                f"{_describe(hourly['weather_code'][index])}; "
                f"осадки {hourly['precipitation_probability'][index]}%, "
                f"ветер {hourly['wind_speed_10m'][index]} км/ч."
            )
        lines.append("\nОдевайтесь по погоде, а не по оптимизму.")
        return "\n".join(lines)
    except Exception as error:
        print(f"[Погода] Не удалось получить прогноз: {error}")
        return None


async def get_tomorrow_forecast() -> str | None:
    """Прогноз на ЗАВТРА с разбивкой по всему дню — для вечерней рассылки в 22:30.

    Нужно 2 дня прогноза от API (forecast_days=2), чтобы в ответе была
    почасовая раскладка не только на сегодня, но и на следующие сутки.
    """
    try:
        forecast = await asyncio.to_thread(_request_forecast, 2)
        hourly = forecast["hourly"]
        hourly_by_time = {
            str(timestamp): index
            for index, timestamp in enumerate(hourly["time"])
        }
        tomorrow_date = datetime.datetime.now(ASTANA_TZ) + datetime.timedelta(days=1)
        tomorrow = tomorrow_date.strftime("%Y-%m-%d")
        weekday_names = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        weekday = weekday_names[tomorrow_date.weekday()]
        requested_hours = [6, 9, 12, 15, 18, 21]

        lines = [f"🌙 **ПРОГНОЗ НА ЗАВТРА, {weekday.upper()} ({tomorrow_date.strftime('%d.%m')})**"]
        day_temperatures = []
        rain_chances = []
        for hour in requested_hours:
            timestamp = f"{tomorrow}T{hour:02d}:00"
            index = hourly_by_time.get(timestamp)
            if index is None:
                continue
            temperature = hourly["temperature_2m"][index]
            precipitation = hourly["precipitation_probability"][index]
            day_temperatures.append(temperature)
            rain_chances.append(precipitation)
            lines.append(
                f"{hour:02d}:00 — **{_format_temperature(temperature)}**, "
                f"{_describe(hourly['weather_code'][index])}; "
                f"осадки {precipitation}%, "
                f"ветер {hourly['wind_speed_10m'][index]} км/ч."
            )

        if not day_temperatures:
            print("[Погода] API не вернул почасовые данные на завтра (возможно, forecast_days не сработал)")
            return None

        lines.append(
            f"\nЗа день: от {min(day_temperatures):.0f}°C до {max(day_temperatures):.0f}°C, "
            f"максимальная вероятность осадков {max(rain_chances)}%."
        )
        lines.append("Планируйте день заранее — сюрпризов от погоды быть не должно.")
        return "\n".join(lines)
    except Exception as error:
        print(f"[Погода] Не удалось получить прогноз на завтра: {error}")
        return None

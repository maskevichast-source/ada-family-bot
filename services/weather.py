"""Простой и читаемый прогноз погоды для Астаны через Open-Meteo."""

import asyncio
import datetime
import urllib.parse
import urllib.request
from services.timezone import ASTANA_TZ

ASTANA_LATITUDE = 51.169392
ASTANA_LONGITUDE = 71.449074

WEATHER_DESCRIPTIONS = {
    0: "ясно",
    1: "почти ясно",
    2: "облачно с прояснениями",
    3: "пасмурно",
    45: "туман",
    48: "туман/изморозь",
    51: "морось",
    53: "морось",
    55: "сильная морось",
    56: "ледяная морось",
    57: "ледяная морось",
    61: "небольшой дождь",
    63: "дождь",
    65: "сильный дождь",
    66: "ледяной дождь",
    67: "ледяной дождь",
    71: "небольшой снег",
    73: "снег",
    75: "сильный снег",
    77: "снежная крупа",
    80: "ливень",
    81: "ливень",
    82: "сильный ливень",
    85: "снежный ливень",
    86: "сильный снежный ливень",
    95: "гроза",
    96: "гроза с градом",
    99: "гроза с сильным градом",
}

WEATHER_EMOJIS = {
    0: "☀️",
    1: "🌤",
    2: "⛅",
    3: "☁️",
    45: "🌫",
    48: "🌫",
    51: "🌧",
    53: "🌧",
    55: "🌧",
    56: "🌧",
    57: "🌧",
    61: "🌦",
    63: "🌧",
    65: "🌧",
    66: "🌧",
    67: "🌧",
    71: "🌨",
    73: "🌨",
    75: "❄️",
    77: "❄️",
    80: "🌦",
    81: "🌧",
    82: "🌧",
    85: "🌨",
    86: "❄️",
    95: "⛈",
    96: "⛈",
    99: "⛈",
}

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

# Коды WMO (Open-Meteo) по типу осадков — нужно, чтобы не советовать зонт
# от снега и не советовать "непромокаемую обувь от дождя" при ясной погоде.
SNOW_WMO_CODES = {71, 73, 75, 77, 85, 86}
RAIN_WMO_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}


def _code_int(code: object) -> int | None:
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def _describe(code: object) -> str:
    try:
        return WEATHER_DESCRIPTIONS.get(int(code), "переменная облачность")
    except (TypeError, ValueError):
        return "переменная облачность"


def _code_to_emoji(code: object, hour: int = 12) -> str:
    try:
        c = int(code)
    except (TypeError, ValueError):
        return "⛅"
    if hour >= 21 or hour < 6:
        if c in (0, 1):
            return "🌙"
        if c == 2:
            return "☁️"
    return WEATHER_EMOJIS.get(c, "⛅")


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


def _clothes_advice(
    temp_min: float | None,
    temp_max: float | None,
    wind: float | None,
    rain: float | None = None,
    has_rain: bool = False,
    has_snow: bool = False,
) -> str:
    if temp_max is None:
        return "Ориентируйтесь по фактической температуре за окном."

    parts = []
    if temp_max <= -15:
        parts.append("очень тёплая зимняя куртка, шапка, шарф и тёплые перчатки — открытые участки кожи на таком морозе мёрзнут за минуты")
    elif temp_max <= -5:
        parts.append("зимняя тёплая куртка, шапка и перчатки")
    elif temp_max <= 5:
        parts.append("тёплая демисезонная куртка и шапка")
    elif temp_max <= 12:
        parts.append("куртка или плотная ветровка")
    elif temp_max <= 18:
        parts.append("худи, свитшот или лёгкая куртка")
    elif temp_max <= 23:
        parts.append("лёгкая одежда (футболка/рубашка)")
    else:
        parts.append("лёгкая одежда (футболка/шорты), в жару не забывайте воду")

    # Разница между утренней/дневной и вечерней температурой — если она
    # заметная, одного слоя на весь день не хватит.
    if temp_min is not None:
        swing = temp_max - temp_min
        if swing >= 10:
            parts.append("днём и вечером температура заметно разная — возьмите с собой лёгкую кофту или толстовку про запас")

    if wind is not None:
        if wind >= 50:
            parts.append("сильный, почти штормовой ветер — капюшон и что-то прилегающее, не парусящее")
        elif wind >= 30:
            parts.append("ветрено — пригодится капюшон или шапка, чтобы не сдувало")
        elif wind >= 20:
            parts.append("лёгкий ветер — не помешает что-то, прикрывающее шею")

    if rain is not None and rain >= 20:
        strong = rain >= 60
        if has_snow and not has_rain:
            # Снег — зонт тут ни при чём, важнее не скользить и не промочить ноги.
            if strong:
                parts.append("снег почти наверняка — непромокаемая нескользящая обувь и куртка с капюшоном, зонт от снега не спасёт")
            else:
                parts.append("возможен снег — выбирайте обувь понадёжнее, чтобы не скользить")
        elif has_rain and has_snow:
            # В течение дня возможна смена осадков — универсальный совет.
            parts.append("возможны и дождь, и снег в течение дня — непромокаемая куртка и обувь с протектором выручат в обоих случаях")
        else:
            if strong:
                parts.append("осадки почти наверняка — непромокаемая куртка или дождевик будут не лишними, зонта может не хватить при таком ветре")
            else:
                parts.append("вероятны осадки — возьмите что-то непромокаемое сверху")

    result = ", ".join(parts) + "."
    return result[0].upper() + result[1:] if result else result


def _day_title(dt: datetime.date, today: datetime.date) -> str:
    if dt == today:
        return "Сегодня"
    if dt == today + datetime.timedelta(days=1):
        return "Завтра"
    return f"{WEEKDAYS_RU[dt.weekday()].capitalize()}, {dt.strftime('%d.%m')}"


def _arr(container: dict, key: str, length: int) -> list:
    """Достаёт список по ключу; если ключ целиком отсутствует в ответе API —
    отдаёт список из None нужной длины вместо IndexError на второй день."""
    value = container.get(key)
    if not isinstance(value, list):
        return [None] * length
    if len(value) < length:
        value = value + [None] * (length - len(value))
    return value


def format_forecast(data: dict, target: str = "today", now: datetime.datetime | None = None, days: int = 1) -> str:
    """Чистое форматирование ответа Open-Meteo в текст — без сетевых вызовов.

    Вынесено отдельно от get_weather_forecast(), чтобы формат можно было
    тестировать напрямую и чтобы легче было чинить конкретные тексты, не
    трогая логику похода в сеть.
    """
    now = now or datetime.datetime.now(ASTANA_TZ)
    today_date = now.date()

    if target == "week" or days >= 4:
        daily = data.get("daily", {})
        times = daily.get("time", [])
        n = len(times)
        t_mins = _arr(daily, "temperature_2m_min", n)
        t_maxs = _arr(daily, "temperature_2m_max", n)
        codes = _arr(daily, "weather_code", n)
        rains = _arr(daily, "precipitation_probability_max", n)
        lines = ["📅 **Погода в Астане на неделю:**\n"]
        for i, d_str in enumerate(times[:7]):
            day_dt = datetime.datetime.strptime(d_str, "%Y-%m-%d").date()
            t_min = _num(t_mins[i])
            t_max = _num(t_maxs[i])
            code = codes[i]
            desc = _describe(code)
            emoji = _code_to_emoji(code, 12)
            rain = rains[i]
            rain_info = f", осадки {rain}%" if rain is not None and rain > 20 else ""
            lines.append(f"• {_day_title(day_dt, today_date)}: {_format_temp(t_min)}…{_format_temp(t_max)} {emoji} ({desc}{rain_info})")
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
    # На "сегодня" не показываем уже прошедшие часы — иначе в 22:00 прогноз
    # на 09:00 выглядит так, будто он ещё впереди.
    if offset == 0:
        hours = [h for h in hours if h > now.hour]

    cur = data.get("current", {})
    if offset == 0:
        cur_temp = _format_temp(cur.get("temperature_2m"))
        cur_app = _format_temp(cur.get("apparent_temperature"))
        cur_desc = _describe(cur.get("weather_code"))
        lines = [f"🌤 Астана сейчас: {cur_temp}, {cur_desc} (ощущ. {cur_app})", ""]
    else:
        lines = [f"🌤 Астана, {title}:", ""]

    has_rain = False
    has_snow = False
    for h in hours:
        key = f"{date_str}T{h:02d}:00"
        idx = hourly_map.get(key)
        if idx is None:
            continue
        temp = _num(hourly["temperature_2m"][idx])
        feels = _num(hourly["apparent_temperature"][idx])
        rain = hourly["precipitation_probability"][idx]
        wind = _num(hourly["wind_speed_10m"][idx])
        code = hourly["weather_code"][idx]
        emoji = _code_to_emoji(code, h)

        if temp is not None:
            temps.append(temp)
        if rain is not None:
            rains.append(rain)
        if wind is not None:
            winds.append(wind)

        # Отдельно помечаем снег и дождь — от этого зависит совет про зонт
        # и про обувь ниже: советовать зонт от снега бессмысленно.
        if rain is not None and rain >= 20:
            code_int = _code_int(code)
            if code_int in SNOW_WMO_CODES:
                has_snow = True
            elif code_int in RAIN_WMO_CODES:
                has_rain = True

        line = f"{h:02d}:00  {_format_temp(temp)} {emoji}"
        if feels is not None and temp is not None and abs(feels - temp) >= 2:
            line += f" (ощущ. {_format_temp(feels)})"
        lines.append(line)

    lines.append("")
    min_t = min(temps) if temps else None
    max_t = max(temps) if temps else None
    max_wind = max(winds) if winds else None
    max_rain = max(rains) if rains else 0
    lines.append(
        f"👕 {_clothes_advice(min_t, max_t, max_wind, max_rain, has_rain=has_rain, has_snow=has_snow)}"
    )

    if max_rain > 20:
        if has_snow and not has_rain:
            lines.append(f"❄️ Ожидается снег (вероятность {max_rain}%) — зонт не нужен, важнее непромокаемая обувь.")
        elif has_rain and has_snow:
            lines.append(f"☂️❄️ Возможны и дождь, и снег (вероятность {max_rain}%) — зонт пригодится для дождя, для снега — обувь понадёжнее.")
        elif has_rain:
            lines.append(f"☂️ Зонт: лучше взять с собой (вероятность осадков {max_rain}%).")
        # Если рядом с высоким max_rain нет ни одного распознанного
        # rain/snow-кода (has_rain=has_snow=False) — не гадаем и молчим,
        # чем врать про зонт от осадков непонятного типа (туман/град).

    return "\n".join(lines)


async def get_weather_forecast(target: str = "today", days: int = 1) -> str | None:
    """Вернуть короткий и структурированный прогноз."""
    try:
        req_days = 7 if target in {"week", "after_tomorrow"} or days >= 4 else (2 if target == "tomorrow" else 1)
        data = await asyncio.to_thread(_request_open_meteo, req_days)
        now = datetime.datetime.now(ASTANA_TZ)
        return format_forecast(data, target=target, now=now, days=days)
    except Exception as error:
        print(f"[Погода] Ошибка Open-Meteo: {error}")
        return None


async def get_tomorrow_forecast() -> str | None:
    """Для фоновой рассылки в 22:30."""
    return await get_weather_forecast(target="tomorrow")

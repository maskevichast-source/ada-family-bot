"""Простой и читаемый прогноз погоды для Астаны через Open-Meteo."""

import asyncio
import datetime
import os
import urllib.parse
import urllib.request
from services.timezone import ASTANA_TZ

ASTANA_LATITUDE = 51.169392
ASTANA_LONGITUDE = 71.449074

# Резервный источник на случай, если Open-Meteo целиком недоступен (сеть,
# авария, лимиты). Ключ живёт только в переменных окружения Railway —
# никогда не хардкодить его в код и не коммитить в репозиторий.
OPENWEATHERMAP_API_KEY = os.environ.get("OPENWEATHERMAP_API_KEY", "").strip()

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

# Астана — резко континентальный климат, отдельные модели тут регулярно
# расходятся заметнее, чем в Европе. Берём 3 независимые модели от разных
# национальных метеослужб (не просто общий best_match) и сверяем их между
# собой, примерно как это делает RAD Weather: там где модели соглашаются —
# доверяем консенсусу, там где сильно расходятся — говорим об этом прямо,
# а не выдаём один из вариантов за точный факт.
CONSENSUS_MODELS = ["ecmwf_ifs025", "gfs_seamless", "icon_seamless"]
CONSENSUS_HOURLY_VARS = "temperature_2m,apparent_temperature,precipitation_probability,weather_code,wind_speed_10m"
# Пороги, начиная с которых расхождение моделей достаточно большое, чтобы
# об этом стоило сказать прямым текстом, а не тихо усреднять.
TEMP_DISAGREEMENT_C = 3.0
RAIN_DISAGREEMENT_PP = 30


def _code_int(code: object) -> int | None:
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def _model_values_at(hourly: dict, var: str, idx: int) -> list[float]:
    """Значения var на индексе idx по каждой из CONSENSUS_MODELS.

    Open-Meteo при явном перечислении нескольких моделей (&models=a,b,c)
    возвращает каждую переменную с суффиксом модели в имени ключа
    (например temperature_2m_icon_seamless). Мы не завязываемся жёстко на
    точный формат суффикса — ищем по вхождению названия модели в ключ,
    так функция не ломается, если Open-Meteo слегка изменит написание.
    Если суффиксов вообще нет (например в тестах или если модельный
    запрос не выполнялся) — тихо откатываемся на обычный "bare"-ключ,
    как было раньше с одной моделью best_match."""
    values: list[float] = []
    for model in CONSENSUS_MODELS:
        for key, arr in hourly.items():
            if key.startswith(var + "_") and model in key and isinstance(arr, list) and idx < len(arr):
                v = _num(arr[idx])
                if v is not None:
                    values.append(v)
                break
    if values:
        return values
    arr = hourly.get(var)
    if isinstance(arr, list) and idx < len(arr):
        v = _num(arr[idx])
        if v is not None:
            return [v]
    return []


def _model_codes_at(hourly: dict, idx: int) -> list[int]:
    codes: list[int] = []
    for model in CONSENSUS_MODELS:
        for key, arr in hourly.items():
            if key.startswith("weather_code_") and model in key and isinstance(arr, list) and idx < len(arr):
                c = _code_int(arr[idx])
                if c is not None:
                    codes.append(c)
                break
    if codes:
        return codes
    arr = hourly.get("weather_code")
    if isinstance(arr, list) and idx < len(arr):
        c = _code_int(arr[idx])
        if c is not None:
            return [c]
    return []


def _consensus(values: list[float]) -> float | None:
    """Медиана по моделям — устойчивее к одной модели-выбросу, чем среднее."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _consensus_code(codes: list[int]) -> int | None:
    """При разногласии моделей по типу погоды берём наиболее часто
    встречающийся код; при ничьей — более "серьёзный" (осадки/гроза
    важнее показать, чем пропустить, если половина моделей их не видит)."""
    if not codes:
        return None
    counts: dict[int, int] = {}
    for c in codes:
        counts[c] = counts.get(c, 0) + 1
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _spread(values: list[float]) -> float:
    return (max(values) - min(values)) if values else 0.0


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


def _request_open_meteo_consensus(forecast_days: int = 7) -> dict:
    """Отдельный запрос за почасовыми данными сразу от 3 независимых
    моделей (ECMWF/GFS/ICON) — для консенсуса на однодневный вид прогноза.
    Возвращает hourly с суффиксами моделей в ключах."""
    params = urllib.parse.urlencode({
        "latitude": ASTANA_LATITUDE,
        "longitude": ASTANA_LONGITUDE,
        "hourly": CONSENSUS_HOURLY_VARS,
        "models": ",".join(CONSENSUS_MODELS),
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
    max_temp_spread = 0.0
    max_rain_spread = 0.0
    for h in hours:
        key = f"{date_str}T{h:02d}:00"
        idx = hourly_map.get(key)
        if idx is None:
            continue

        temp_values = _model_values_at(hourly, "temperature_2m", idx)
        feels_values = _model_values_at(hourly, "apparent_temperature", idx)
        rain_values = _model_values_at(hourly, "precipitation_probability", idx)
        wind_values = _model_values_at(hourly, "wind_speed_10m", idx)
        codes = _model_codes_at(hourly, idx)

        temp = _consensus(temp_values)
        feels = _consensus(feels_values)
        rain = _consensus(rain_values)
        wind = _consensus(wind_values)
        code = _consensus_code(codes)
        emoji = _code_to_emoji(code, h) if code is not None else "⛅"

        if temp is not None:
            temps.append(temp)
            max_temp_spread = max(max_temp_spread, _spread(temp_values))
        if rain is not None:
            rains.append(rain)
            max_rain_spread = max(max_rain_spread, _spread(rain_values))
        if wind is not None:
            winds.append(wind)

        # Отдельно помечаем снег и дождь — от этого зависит совет про зонт
        # и про обувь ниже: советовать зонт от снега бессмысленно.
        if rain is not None and rain >= 20:
            if code in SNOW_WMO_CODES:
                has_snow = True
            elif code in RAIN_WMO_CODES:
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

    # Модели заметно разошлись — честно предупреждаем, а не выдаём
    # усреднённое число за точный факт (как расхождение показывает RAD Weather).
    if max_temp_spread >= TEMP_DISAGREEMENT_C or max_rain_spread >= RAIN_DISAGREEMENT_PP:
        bits = []
        if max_temp_spread >= TEMP_DISAGREEMENT_C:
            bits.append(f"по температуре до {max_temp_spread:.0f}°")
        if max_rain_spread >= RAIN_DISAGREEMENT_PP:
            bits.append(f"по осадкам до {max_rain_spread:.0f} п.п.")
        lines.append(f"🔀 Модели расходятся ({', '.join(bits)}) — прогноз может измениться.")

    return "\n".join(lines)


# --- Резервный источник: OpenWeatherMap -----------------------------------
# Независимая от Open-Meteo инфраструктура — используется только если
# Open-Meteo целиком недоступен. У OWM своя система кодов погоды (не WMO),
# поэтому классификация ясно/дождь/снег переведена отдельно.

def _owm_describe(owm_id: object) -> str:
    c = _code_int(owm_id)
    if c is None:
        return "переменная облачность"
    if 200 <= c < 300:
        return "гроза"
    if 300 <= c < 400:
        return "морось"
    if 500 <= c < 600:
        return "дождь" if c < 520 else "ливень"
    if 600 <= c < 700:
        return "снег"
    if 700 <= c < 800:
        return "туман/дымка"
    if c == 800:
        return "ясно"
    if 801 <= c <= 802:
        return "облачно с прояснениями"
    if c in (803, 804):
        return "пасмурно"
    return "переменная облачность"


def _owm_emoji(owm_id: object) -> str:
    c = _code_int(owm_id)
    if c is None:
        return "⛅"
    if 200 <= c < 300:
        return "⛈"
    if 300 <= c < 400:
        return "🌦"
    if 500 <= c < 600:
        return "🌧"
    if 600 <= c < 700:
        return "🌨"
    if 700 <= c < 800:
        return "🌫"
    if c == 800:
        return "☀️"
    if 801 <= c <= 802:
        return "⛅"
    return "☁️"


def _owm_is_snow(owm_id: object) -> bool:
    c = _code_int(owm_id)
    return c is not None and 600 <= c < 700


def _owm_is_rain(owm_id: object) -> bool:
    c = _code_int(owm_id)
    return c is not None and (200 <= c < 600)


def _request_openweathermap_current(lat: float, lon: float, api_key: str) -> dict:
    params = urllib.parse.urlencode({"lat": lat, "lon": lon, "appid": api_key, "units": "metric", "lang": "ru"})
    url = f"https://api.openweathermap.org/data/2.5/weather?{params}"
    with urllib.request.urlopen(url, timeout=12) as response:
        import json
        return json.loads(response.read().decode("utf-8"))


def _request_openweathermap_forecast(lat: float, lon: float, api_key: str) -> dict:
    """3-часовые блоки на 5 дней вперёд — бесплатный тариф OWM."""
    params = urllib.parse.urlencode({"lat": lat, "lon": lon, "appid": api_key, "units": "metric", "lang": "ru"})
    url = f"https://api.openweathermap.org/data/2.5/forecast?{params}"
    with urllib.request.urlopen(url, timeout=12) as response:
        import json
        return json.loads(response.read().decode("utf-8"))


def _nearest_owm_block(blocks: list, target_dt: datetime.datetime) -> dict | None:
    best = None
    best_diff = None
    for block in blocks:
        dt_txt = block.get("dt_txt")
        try:
            block_dt = datetime.datetime.strptime(dt_txt, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        diff = abs((block_dt - target_dt).total_seconds())
        if best_diff is None or diff < best_diff:
            best, best_diff = block, diff
    return best


def format_owm_forecast(
    current: dict,
    forecast: dict,
    target: str = "today",
    now: datetime.datetime | None = None,
    days: int = 1,
) -> str:
    """Форматирование ответа OpenWeatherMap — только когда Open-Meteo
    недоступен целиком. Своя, более простая версия format_forecast: у OWM
    нет мульти-модельного консенсуса и только 3-часовая детализация, зато
    это полностью независимая инфраструктура на случай сбоя основной."""
    now = now or datetime.datetime.now(ASTANA_TZ)
    today_date = now.date()

    if target == "week" or days >= 4:
        blocks = forecast.get("list", []) if isinstance(forecast, dict) else []
        by_day: dict[datetime.date, list] = {}
        for block in blocks:
            dt_txt = block.get("dt_txt")
            try:
                block_dt = datetime.datetime.strptime(dt_txt, "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                continue
            by_day.setdefault(block_dt.date(), []).append(block)

        lines = ["📅 Погода в Астане на неделю (резервный источник):\n"]
        for day in sorted(by_day)[:7]:
            day_blocks = by_day[day]
            temps = [_num(b.get("main", {}).get("temp")) for b in day_blocks]
            temps = [t for t in temps if t is not None]
            t_min = min(temps) if temps else None
            t_max = max(temps) if temps else None
            ids = [(b.get("weather") or [{}])[0].get("id") for b in day_blocks]
            ids = [_code_int(i) for i in ids if i is not None]
            code = max(set(ids), key=ids.count) if ids else None
            emoji = _owm_emoji(code)
            desc = _owm_describe(code)
            lines.append(f"• {_day_title(day, today_date)}: {_format_temp(t_min)}…{_format_temp(t_max)} {emoji} ({desc})")
        return "\n".join(lines)

    offset = 0
    if target == "tomorrow":
        offset = 1
    elif target == "after_tomorrow":
        offset = 2
    target_date = today_date + datetime.timedelta(days=offset)
    title = "сегодня" if offset == 0 else ("завтра" if offset == 1 else "послезавтра")

    if offset == 0 and isinstance(current, dict):
        cur_main = current.get("main", {})
        cur_weather = (current.get("weather") or [{}])[0]
        cur_temp = _format_temp(cur_main.get("temp"))
        cur_app = _format_temp(cur_main.get("feels_like"))
        cur_desc = _owm_describe(cur_weather.get("id"))
        lines = [f"🌤 Астана сейчас: {cur_temp}, {cur_desc} (ощущ. {cur_app}) — резервный источник", ""]
    else:
        lines = [f"🌤 Астана, {title} (резервный источник):", ""]

    hours = [9, 12, 15, 18, 21]
    if offset == 0:
        hours = [h for h in hours if h > now.hour]

    blocks = forecast.get("list", []) if isinstance(forecast, dict) else []
    temps, rains, winds = [], [], []
    has_rain = False
    has_snow = False
    for h in hours:
        target_dt = datetime.datetime.combine(target_date, datetime.time(h, 0))
        block = _nearest_owm_block(blocks, target_dt)
        if not block:
            continue
        main = block.get("main", {})
        weather0 = (block.get("weather") or [{}])[0]
        temp = _num(main.get("temp"))
        feels = _num(main.get("feels_like"))
        pop = block.get("pop")
        rain = round(pop * 100) if isinstance(pop, (int, float)) else None
        wind = _num((block.get("wind") or {}).get("speed"))
        if wind is not None:
            wind *= 3.6  # м/с -> км/ч, для единообразия с основным прогнозом
        code = weather0.get("id")
        emoji = _owm_emoji(code)

        if temp is not None:
            temps.append(temp)
        if rain is not None:
            rains.append(rain)
        if wind is not None:
            winds.append(wind)
        if rain is not None and rain >= 20:
            if _owm_is_snow(code):
                has_snow = True
            elif _owm_is_rain(code):
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

    return "\n".join(lines)


async def get_weather_forecast(target: str = "today", days: int = 1) -> str | None:
    """Вернуть короткий и структурированный прогноз.

    Основной путь — Open-Meteo (best_match + консенсус 3 моделей). Если он
    целиком недоступен (сеть, авария, лимиты) — тихо переключаемся на
    OpenWeatherMap как независимый резервный источник, если для него задан
    ключ. Человеку в сообщении честно видно, что это резервный источник."""
    now = datetime.datetime.now(ASTANA_TZ)
    req_days = 7 if target in {"week", "after_tomorrow"} or days >= 4 else (2 if target == "tomorrow" else 1)
    try:
        data = await asyncio.to_thread(_request_open_meteo, req_days)

        # Консенсус 3 моделей нужен только для однодневного вида (today/
        # tomorrow/after_tomorrow) — недельный прогноз строится из daily
        # best_match и там детальный почасовой консенсус не используется.
        if target != "week" and days < 4:
            try:
                consensus = await asyncio.to_thread(_request_open_meteo_consensus, req_days)
                c_hourly = consensus.get("hourly")
                if isinstance(c_hourly, dict):
                    data.setdefault("hourly", {})
                    for var_key, values in c_hourly.items():
                        if var_key != "time":
                            data["hourly"][var_key] = values
            except Exception as consensus_error:
                # Не страшно: format_forecast сам откатится на обычный
                # best_match hourly, если суффиксов по моделям не найдётся.
                print(f"[Погода] Консенсус-запрос не удался, использую best_match: {consensus_error}")

        return format_forecast(data, target=target, now=now, days=days)
    except Exception as error:
        print(f"[Погода] Open-Meteo недоступен ({error}), пробую резервный источник")
        if not OPENWEATHERMAP_API_KEY:
            print("[Погода] OPENWEATHERMAP_API_KEY не задан — резервного источника нет")
            return None
        try:
            current = await asyncio.to_thread(
                _request_openweathermap_current, ASTANA_LATITUDE, ASTANA_LONGITUDE, OPENWEATHERMAP_API_KEY
            )
            forecast = await asyncio.to_thread(
                _request_openweathermap_forecast, ASTANA_LATITUDE, ASTANA_LONGITUDE, OPENWEATHERMAP_API_KEY
            )
            return format_owm_forecast(current, forecast, target=target, now=now, days=days)
        except Exception as owm_error:
            print(f"[Погода] Резервный источник тоже недоступен: {owm_error}")
            return None


async def get_tomorrow_forecast() -> str | None:
    """Для фоновой рассылки в 22:30."""
    return await get_weather_forecast(target="tomorrow")

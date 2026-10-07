"""Предупреждения о резкой погоде: гроза, сильный ветер, перепад температуры, сильные осадки.

Смотрим ближайшие 6 часов прогноза (Open-Meteo, консенсус моделей). Бот раз в час вызывает collect(); отправку и
правило «каждое событие не чаще раза в сутки» делает main.py."""
import datetime

WIND_KMH = 54.0                 # 15 м/с
TEMP_SWING_C = 10.0             # перепад за 3 часа
SWING_HOURS = 3
LOOKAHEAD_HOURS = 6
ACTIVE_HOURS = range(7, 23)     # с 07:00 до 23:00 не беспокоим
_STORM = {95, 96, 99}
_HEAVY = {65, 67, 75, 82, 86}   # сильный дождь/ледяной дождь, сильный снег, сильные ливни и снегопад


def in_active_hours(now: datetime.datetime) -> bool:
    return now.hour in ACTIVE_HOURS


def _at(p) -> str:
    return f"{p['dt']:%H:%M}"


def find_events(points: list[dict]) -> list[tuple[str, str]]:
    """points — почасовые точки, начиная с ближайшего часа (build_points). Возвращает [(вид события, текст)]."""
    events = []
    storm = [p for p in points if p["code"] is not None and int(p["code"]) in _STORM]
    if storm:
        events.append(("storm", f"⛈ Ожидается гроза около {_at(storm[0])}. Лучше не оставлять вещи на улице и "
                                "не быть на открытом месте."))
    windy = [p for p in points if p["wind"] >= WIND_KMH]
    if windy:
        peak = max(windy, key=lambda p: p["wind"])
        events.append(("wind", f"💨 Сильный ветер с {_at(windy[0])}: до {peak['wind']:.0f} км/ч. "
                               "Окна и балкон лучше закрыть, на улице осторожнее."))
    swing = None
    for i, a in enumerate(points):
        window = points[i:i + SWING_HOURS + 1]
        temps = [p["temp"] for p in window]
        if len(window) > 1 and max(temps) - min(temps) >= TEMP_SWING_C:
            swing = (a, window)
            break
    if swing:
        a, window = swing
        first, last = window[0]["temp"], window[-1]["temp"]
        direction = "потеплеет" if last > first else "похолодает"
        events.append(("swing", f"🌡 Резкая смена температуры: с {_at(a)} за {SWING_HOURS} часа {direction} "
                                f"с {first:.0f}° до {last:.0f}°. Одевайтесь по новой погоде."))
    heavy = [p for p in points if p["code"] is not None and int(p["code"]) in _HEAVY]
    if heavy:
        snow = int(heavy[0]["code"]) in {75, 86}
        events.append(("heavy", f"{'❄️ Сильный снег' if snow else '🌧 Сильный дождь'} около {_at(heavy[0])}. "
                                f"{'Дорога будет скользкой, ' if snow else ''}Зонт и непромокаемая обувь пригодятся."))
    return events


async def collect(now: datetime.datetime) -> list[tuple[str, str]]:
    """События на ближайшие часы. Сеть недоступна или данных нет — пустой список."""
    import asyncio
    from services import weather, weather_chart
    try:
        data = await weather._fetch_open_meteo(2)
        start = now.replace(minute=0, second=0, microsecond=0, tzinfo=None) + datetime.timedelta(hours=1)
        points = weather_chart.build_points(data, start, hours=LOOKAHEAD_HOURS - 1)
        points = [p for p in points if p["dt"].hour in ACTIVE_HOURS]           # ночные события не тревожат
        return find_events(points) if points else []
    except Exception as error:
        print(f"[Погода-предупреждения] Нет данных: {error}")
        return []

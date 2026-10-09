"""Производные показатели для страниц в стиле RAD: считает код, не нейросеть."""
import datetime

from services import weather as w
from services import weather_chart as wc

COMPASS = ["С", "ССВ", "СВ", "ВСВ", "В", "ВЮВ", "ЮВ", "ЮЮВ", "Ю", "ЮЮЗ", "ЮЗ", "ЗЮЗ", "З", "ЗСЗ", "СЗ", "ССЗ"]
WIND_SCALE = [(2, "Штиль"), (12, "Слабый"), (20, "Лёгкий"), (39, "Умеренный"), (62, "Сильный"),
              (75, "Крепкий"), (89, "Шторм"), (10_000, "Ураган")]
THREAT_NAMES = ["СПОКОЙНО", "ВНИМАНИЕ", "ПРЕДОСТЕРЕЖЕНИЕ", "ОПАСНО"]
THREAT_SEGMENTS = [1, 4, 7, 10]
_SNOW = wc._SNOW
_ICE = {56, 57, 66, 67}
_RAIN = wc._RAIN | wc._STORM


def compass_name(deg) -> str:
    if deg is None:
        return "—"
    return COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def wind_level(kmh: float) -> tuple[int, str]:
    for i, (limit, name) in enumerate(WIND_SCALE):
        if kmh < limit:
            return i, name
    return len(WIND_SCALE) - 1, WIND_SCALE[-1][1]


def gust_factor(speed: float, gust: float) -> tuple[float, str]:
    if speed < 1:
        return 1.0, "штиль"
    k = gust / speed
    label = "очень турбулентно" if k >= 2 else "порывисто" if k >= 1.5 else "ровный ветер"
    return k, label


def _precip_kind(code) -> str | None:
    try:
        code = int(code)
    except (TypeError, ValueError):
        return None
    if code in _ICE:
        return "ice"
    if code in _SNOW:
        return "snow"
    if code in _RAIN:
        return "rain"
    return None


def rain_models(data: dict, points: list[dict]) -> None:
    """Для каждого часа: разброс вероятности осадков между моделями (rain_lo/rain_hi) и точка росы, направление ветра."""
    hourly = (data or {}).get("hourly") or {}
    times = hourly.get("time") or []
    for p in points:
        key = p["dt"].strftime("%Y-%m-%dT%H:00")
        i = times.index(key) if key in times else -1
        vals = w._model_values_at(hourly, "precipitation_probability", i) if i >= 0 else []
        p["rain_lo"] = min(vals) if vals else p["rain"]
        p["rain_hi"] = max(vals) if vals else p["rain"]
        for name, var in (("dew", "dew_point_2m"), ("wdir", "wind_direction_10m"), ("pmsl", "pressure_msl")):
            try:
                v = (hourly.get(var) or [])[i] if i >= 0 else None
            except IndexError:
                v = None
            p[name] = float(v) if isinstance(v, (int, float)) else None
        p["wind_kmh"] = p.get("wind") or 0.0
        gust_ms = p.get("gust_ms")
        p["gust_kmh"] = (gust_ms * 3.6) if gust_ms is not None else p["wind_kmh"]


def daily_list(data: dict, start: datetime.datetime, days: int = 8) -> list[dict]:
    daily = (data or {}).get("daily") or {}
    out = []
    for i, day in enumerate(daily.get("time") or []):
        try:
            d = datetime.datetime.strptime(day, "%Y-%m-%d")
            if d.date() < start.date():
                continue
            out.append({"date": d, "code": (daily.get("weather_code") or [None] * 99)[i],
                        "tmax": daily["temperature_2m_max"][i], "tmin": daily["temperature_2m_min"][i],
                        "rain": (daily.get("precipitation_probability_max") or [0] * 99)[i]})
        except (KeyError, IndexError, ValueError, TypeError):
            continue
    return out[:days]


def next_rain(points: list[dict]) -> tuple[str, int | None]:
    """(текст, часов до осадков): «Идут сейчас» / «около 14:00» / «не ожидаются»."""
    def wet(p):
        return (p.get("rain") or 0) >= 50 or (p.get("precip") or 0) >= 0.3 or _precip_kind(p.get("code"))
    if wet(points[0]):
        return "идут сейчас", 0
    for i, p in enumerate(points):
        if wet(p):
            return f"около {p['dt']:%H:00}", i
    return "в ближайшие сутки не ожидаются", None


def precip_layer(points: list[dict]) -> dict:
    total = sum((p.get("precip") or 0) for p in points)
    kinds = [(_precip_kind(p.get("code")) if (p.get("precip") or 0) >= 0.05 or (p.get("rain") or 0) >= 60 else None)
             for p in points]
    hours = sum(1 for p, k in zip(points, kinds) if k or (p.get("precip") or 0) >= 0.1)
    level = "Нет" if total < 0.1 else "Слабый" if total < 2 else "Умеренный" if total < 15 else "Сильный"
    if hours >= 8:
        text = f"Затяжные осадки: впереди {hours} ч непогоды"
    elif hours >= 1:
        text = f"Кратковременные осадки: всего около {hours} ч"
    else:
        text = "Осадков не ожидается"
    return {"total": total, "kinds": kinds, "hours": hours, "level": level, "text": text}


def rain_spread(points: list[dict]) -> dict:
    peak = max(points, key=lambda p: p["rain"])
    next_txt, in_h = next_rain(points)
    widest = max((p["rain_hi"] - p["rain_lo"]) for p in points)
    return {"peak": peak, "widest": widest, "in_hours": in_h,
            "title": ("Осадки сейчас" if in_h == 0 else f"Дождь примерно через {in_h} ч" if in_h is not None else "Осадков не ожидается")}


def confidence(points: list[dict]) -> tuple:
    """Согласие моделей: (метка, число моделей, процент, разброс температуры, разброс осадков). Это не проверка по станциям."""
    t = sum(p.get("spread", 0) for p in points) / len(points)
    r = sum((p["rain_hi"] - p["rain_lo"]) for p in points) / len(points)
    pct = int(max(30, min(99, 100 - t * 8 - r * 0.6)))
    label = "ВЫСОКИЙ" if t <= 1.5 and r <= 15 else "СРЕДНИЙ" if t <= 3 and r <= 30 else "НИЗКИЙ"
    return label, len(w.CONSENSUS_MODELS), pct, round(t, 1), round(r)


def advice(m: dict) -> str:
    f = m["first"]
    if m["thunder"][0] == 2:
        return "Гроза. Лучше остаться дома."
    if m["ice"][0] == 2:
        return "Гололёд. Идите осторожно."
    if "СНЕГ" in m["precip"]:
        return "Снег. Нужна нескользкая обувь."
    if "ДОЖДЬ" in m["precip"]:
        return "Дождь. Капюшон или зонт."
    if m["gust_max"] >= 17:
        return "Сильный ветер. Держите шапку."
    if m["tmin"] <= -20:
        return "Мороз. Одевайтесь теплее."
    if m["tmax"] >= 30:
        return "Жара. Пейте больше воды."
    if m["ice"][0] == 1:
        return "Возможен гололёд. Под ноги."
    return "Без осадков. Можно налегке."


def headline(m: dict, tomorrow: bool) -> tuple[str, str]:
    """«Утренний выпуск»: заголовок и строка-итог."""
    day = "Завтра" if tomorrow else "Сегодня"
    pr = m["precip"]
    if "ГРОЗА" in m["thunder"][1]:
        title = "Впереди грозовой день"
    elif "ДОЖДЬ" in pr:
        title = "Впереди дождливый день"
    elif "СНЕГ" in pr:
        title = "Впереди снежный день"
    elif m["tmax"] >= 28:
        title = "Впереди жаркий день"
    elif m["tmin"] <= -15:
        title = "Впереди морозный день"
    elif m["wind_max"] >= 9:
        title = "Впереди ветреный день"
    else:
        title = "Впереди спокойный день"
    what = "дождь" if "ДОЖДЬ" in pr else "снег" if "СНЕГ" in pr else "без осадков"
    return title, f"{day} {what}. Максимум {m['tmax']:.0f}°, минимум {m['tmin']:.0f}°."


def street_text(m: dict) -> str:
    """Запасной текст блока «На улице»."""
    kind = m["precip"]
    if "ДОЖДЬ" in kind:
        return "Идёт дождь. Ровно и серо."
    if "СНЕГ" in kind:
        return "Идёт снег. Тихо и белое всё."
    if m["gust_max"] >= 17:
        return "Ветер рвёт. Держитесь за шапки."
    return "На улице спокойно."


def intel(m: dict) -> dict:
    """Интеллект-терминал: уровень, сегменты, приоритет и сводка аналитики."""
    pts = m["points"]
    items = []
    f = m["first"]
    dew = f.get("dew")
    vis = f.get("vis")
    if dew is not None and vis is not None and f["temp"] - dew <= 2.5 and vis < 10000:
        items.append(("УСЛОВИЯ ТУМАНА", f"Малый дефицит точки росы при видимости {vis / 1000:.0f} км: туман", "fog"))
    k, label = gust_factor(f["wind_kmh"], max(p["gust_kmh"] for p in pts[:6]))
    if m["gust_max"] >= 12:
        items.append(("ПОРЫВИСТЫЙ ВЕТЕР", f"Порывы до {m['gust_max'] * 3.6:.0f} км/ч, коэффициент порывистости {k:.1f}x", "wind"))
    if m["thunder"][0]:
        items.append(("РИСК ГРОЗЫ", m["thunder"][1].capitalize(), "storm"))
    if m["ice"][0]:
        items.append(("ГОЛОЛЁД", m["ice"][1].capitalize(), "snow"))
    if m["tmin"] <= -15:
        items.append(("МОРОЗ", f"Минимум {m['tmin']:.0f}°, ощущается до {min(p['feels'] for p in pts):.0f}°", "snow"))
    if m["tmax"] >= 30:
        items.append(("ЖАРА", f"Максимум {m['tmax']:.0f}°, пейте воду и держитесь в тени", "sun"))
    heavy = precip_layer(pts)
    if heavy["total"] >= 10:
        items.append(("СИЛЬНЫЕ ОСАДКИ", f"За сутки около {heavy['total']:.0f} мм", "rain"))
    if not items:
        items.append(("ВСЁ СПОКОЙНО", "Опасных явлений в ближайшие 24 часа не ожидается", "ok"))
    level = m["threat"]
    seg = THREAT_SEGMENTS[level]
    if level and len(m["reasons"]) > 1:
        seg = min(10, seg + 1)
    return {"level": level, "name": THREAT_NAMES[level], "seg": seg, "items": items[:2],
            "prior": f"ПРИОР-{level}", "head": items[0][0]}


def sun_arc(m: dict) -> dict | None:
    """Дуга солнца: длина дня, до заката, полдень, золотой час (за 30 минут до заката), доля пройденного пути."""
    if not m["sun"]:
        return None
    rise = datetime.datetime.strptime(m["start"].strftime("%Y-%m-%d ") + m["sun"][0], "%Y-%m-%d %H:%M")
    sset = datetime.datetime.strptime(m["start"].strftime("%Y-%m-%d ") + m["sun"][1], "%Y-%m-%d %H:%M")
    day = sset - rise
    noon = rise + day / 2
    golden = sset - datetime.timedelta(minutes=30)
    now = m["start"]
    frac = max(0.0, min(1.0, (now - rise).total_seconds() / day.total_seconds()))
    left = max(datetime.timedelta(0), sset - now)

    def hm(td):
        s = int(td.total_seconds() // 60)
        return f"{s // 60}ч {s % 60:02d}м"

    return {"rise": m["sun"][0], "set": m["sun"][1], "length": hm(day), "left": hm(left),
            "noon": f"{noon:%H:%M}", "golden": f"{golden:%H:%M}", "frac": frac, "above": rise <= now < sset}


def enrich(m: dict, data: dict, kind: str) -> dict:
    pts = m["points"]
    rain_models(data, pts)
    m["kind_req"] = kind
    m["tomorrow"] = kind in ("evening", "tomorrow")
    m["daily"] = daily_list(data, m["start"], 8)
    m["next_rain"] = next_rain(pts)
    m["layer"] = precip_layer(pts)
    m["spread"] = rain_spread(pts)
    m["confidence"] = confidence(pts)
    m["advice"] = advice(m)
    m["headline"] = headline(m, m["tomorrow"])
    m["intel"] = intel(m)
    m["arc"] = sun_arc(m)
    f = m["first"]
    m["wind_dir"] = f.get("wdir")
    m["wind_kmh"] = f["wind_kmh"]
    m["gust_kmh"] = max(p["gust_kmh"] for p in pts[:6])
    m["day_wind_kmh"] = max(p["wind_kmh"] for p in pts)
    return m

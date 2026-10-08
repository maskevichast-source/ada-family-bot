"""Статичные страницы: почасовой график на 24 часа и плитки-модули."""
from services.pixel_weather import gfx, theme as T
from services.pixel_weather.model import aqi_label
from services.pixel_weather.scene import _signed

DAYS = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]


def _header(p: gfx.Page, title: str, right: str):
    p.rect(0, 0, T.LW - 1, 12, fill=T.PANEL)
    p.text(5, 3, title, T.GREEN)
    p.text(T.LW - 5, 3, right, T.DIM, anchor="r")


def _is_night(m, dt) -> bool:
    if m["sun"]:
        return not (m["sun"][0] <= f"{dt:%H:%M}" < m["sun"][1])
    return dt.hour < 6 or dt.hour >= 21


def page_chart(m: dict) -> gfx.Page:
    p = gfx.Page()
    pts = m["points"]
    st = m["start"]
    _header(p, "ПРОГНОЗ 24Ч", f"{DAYS[st.weekday()]} {st:%d.%m} С {st:%H:%M}")
    X0, X1 = 38, 304
    n = len(pts)
    dx = (X1 - X0) / (n - 1)
    xs = [X0 + i * dx for i in range(n)]
    CT, CB = 30, 86                                    # область температуры
    lo = min(min(q["temp"] for q in pts), min(q["feels"] for q in pts))
    hi = max(max(q["temp"] for q in pts), max(q["feels"] for q in pts))
    lo, hi = lo - 1, hi + 1
    if hi - lo < 6:
        mid = (hi + lo) / 2
        lo, hi = mid - 3, mid + 3

    def ty(v):
        return CB - (v - lo) / (hi - lo) * (CB - CT)

    # ночные полосы (дизеринг) и сетка
    for i in range(n - 1):
        if _is_night(m, pts[i]["dt"]):
            p.dither(int(xs[i]), CT - 6, int(xs[i + 1]) - 1, CB + 4, T.BG, (16, 20, 44), 0.5)
    step = 5 if hi - lo > 12 else 2
    v = int(lo // step * step)
    while v <= hi:
        if lo <= v <= hi:
            p.line(X0, ty(v), X1, ty(v), T.BORDER if v else T.DIM, dashed=True)
            p.text(X0 - 4, ty(v) - 4, _signed(v), T.DIM, anchor="r")
        v += step
    p.polyline([(xs[i], ty(q["feels"])) for i, q in enumerate(pts)], T.CYAN, dashed=True)
    p.polyline([(xs[i], ty(q["temp"])) for i, q in enumerate(pts)], T.AMBER)
    for i, q in enumerate(pts):
        p.rect(int(xs[i]) - 1, int(ty(q["temp"])) - 1, int(xs[i]) + 1, int(ty(q["temp"])) + 1, fill=T.AMBER)
        if q["dt"].hour % 3 == 0:
            p.text(xs[i], ty(q["temp"]) - 11, _signed(q["temp"]), T.AMBER, anchor="m")
    p.text(5, 17, "ТЕМПЕРАТУРА", T.AMBER)
    p.text(T.LW - 5, 17, "ОЩУЩАЕТСЯ", T.CYAN, anchor="r")
    # часы, иконки
    for i, q in enumerate(pts):
        if q["dt"].hour % 3 == 0:
            p.text(xs[i], 94, f"{q['dt'].hour:02d}", T.DIM, anchor="m")
            night = _is_night(m, q["dt"])
            gfx.weather_icon(p, gfx.kind_of(q.get("code"), night), int(xs[i]), 112)
    # осадки
    p.text(5, 134, "ДОЖ%", T.CYAN)
    for i, q in enumerate(pts):
        h = int(round(q["rain"] / 100 * 16))
        if h:
            p.rect(int(xs[i]) - 3, 150 - h, int(xs[i]) + 3, 150, fill=T.CYAN if q["rain"] < 70 else T.VIOLET)
        if q["dt"].hour % 3 == 0 and q["rain"] >= 10 and xs[i] > 52:
            p.text(xs[i], 132, f"{q['rain']:.0f}", T.DIM, anchor="m")
    p.line(X0, 151, X1, 151, T.BORDER)
    # ветер
    p.text(5, 160, "М/С", T.GREEN)
    for i, q in enumerate(pts):
        w = q["wind_ms"]
        col = T.GREEN if w < 8 else T.AMBER if w < 14 else T.RED
        h = max(1, min(14, int(round(w / 16 * 14))))
        p.rect(int(xs[i]) - 3, 176 - h, int(xs[i]) + 3, 176, fill=col)
        if q["dt"].hour % 3 == 0 and xs[i] > 52:
            p.text(xs[i], 160, f"{w:.0f}", T.DIM, anchor="m")
    return p


def _tile(p: gfx.Page, col: int, row: int, title: str, lines: list[tuple[str, tuple]]) -> tuple[int, int]:
    x0 = 4 + col * 104
    y0 = 16 + row * 54
    p.panel(x0, y0, x0 + 99, y0 + 49, title=title)
    y = y0 + 15
    for text, color in lines[:3]:
        p.text(x0 + 4, y, text[:12], color)
        y += 11
    return x0, y0


LEVEL_COLOR = {0: T.GREEN, 1: T.AMBER, 2: T.RED}


def page_modules(m: dict) -> gfx.Page:
    p = gfx.Page()
    st = m["start"]
    _header(p, "МОДУЛИ", m["moon"][1].upper())
    name, tcol = T.THREAT_LEVELS[m["threat"]]
    reasons = m["reasons"] or ["всё тихо"]
    x0, y0 = _tile(p, 0, 0, "УГРОЗА", [(name, tcol), (reasons[0], T.TEXT), (reasons[1] if len(reasons) > 1 else "", T.DIM)])
    for i in range(4):
        p.rect(x0 + 4 + i * 23, y0 + 40, x0 + 4 + i * 23 + 19, y0 + 44, fill=(T.THREAT_LEVELS[i][1] if i <= m["threat"] else T.PANEL2))
    _tile(p, 1, 0, "ОСАДКИ", [(m["precip"], T.CYAN if m["precip"] != "СУХО" else T.GREEN), (f"ДО {m['rain_peak']:.0f}%", T.TEXT), ("", T.DIM)])
    lv, tx = m["thunder"]
    _tile(p, 2, 0, "ГРОЗА", [(tx, LEVEL_COLOR[lv]), ("", T.DIM)])
    lv, tx = m["ice"]
    _tile(p, 0, 1, "ГОЛОЛЁД", [(tx, LEVEL_COLOR[lv]), ("на дорогах" if lv else "дороги ок", T.DIM)])
    wmax, gmax = m["wind_max"], m["gust_max"]
    wc = T.GREEN if gmax < 12 else T.AMBER if gmax < 17 else T.RED
    _tile(p, 1, 1, "ВЕТЕР", [(f"{wmax:.0f} М/С", wc), (f"ПОРЫВЫ {gmax:.0f}", T.TEXT), ("", T.DIM)])
    uv, uvl = m["uv"]
    _tile(p, 2, 1, "УФ-ИНДЕКС", [(f"{uv:.1f}", T.AMBER if uv >= 6 else T.GREEN), (uvl, T.TEXT), ("", T.DIM)])
    label, lvl = aqi_label(m["aqi_value"])
    aq_col = [T.GREEN, T.AMBER, T.RED, T.RED][lvl] if lvl >= 0 else T.DIM
    pm = m.get("pm25")
    _tile(p, 0, 2, "ВОЗДУХ", [(f"AQI {m['aqi_value']:.0f}" if m["aqi_value"] is not None else "AQI —", aq_col), (label, aq_col),
                              (f"PM2.5 {pm:.0f}" if pm is not None else "", T.DIM)])
    pres = m["pres"]
    _tile(p, 1, 2, "ДАВЛЕНИЕ", [(f"{pres:.0f} ММ" if pres else "—", T.TEXT), (m["pres_trend"], T.DIM),
                                (f"ВЛАЖН {m['hum']:.0f}%" if m["hum"] is not None else "", T.DIM)])
    sun = m["sun"] or ("—", "—")
    x0, y0 = _tile(p, 2, 2, "СОЛНЦЕ", [(f"ВОСХ {sun[0]}", T.AMBER), (f"ЗАКАТ {sun[1]}", T.ORANGE), (f"ЗОЛОТ. {m['golden']}", T.DIM)])
    gfx.draw_moon(p, x0 + 87, y0 + 7, 6, m["moon"][0])
    return p

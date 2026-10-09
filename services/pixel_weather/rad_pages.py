"""Страницы в стиле RAD: «Сейчас», «Подробно», «По дням», плитки датчиков, карточка Dispatch."""
import datetime
import math

from PIL import Image

from services.pixel_weather import icons, insights as ins, ui
from services.pixel_weather.ui import (AMBER, AMBER_DIM, BG, BORDER, CARD, CARD2, CYAN, CYAN_DIM, DIM, FAINT, GREEN,
                                       GREEN_BG, PINK, RED, WHITE, Canvas)

WEEK = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
ORANGE = (255, 130, 0)
LEVEL_COLOR = [GREEN, AMBER, ORANGE, RED]
CARD_X0, CARD_X1 = 40, 1040


def _sgn(v) -> str:
    r = round(v)
    return f"{'+' if r > 0 else ''}{r}"


def _dotted(cv: Canvas, y, x0=60, x1=1020):
    for x in range(x0, x1, 12):
        cv.rect(x, y, x + 5, y + 1, fill=FAINT)


def _is_night(m, dt) -> bool:
    if m["sun"]:
        return not (m["sun"][0] <= f"{dt:%H:%M}" < m["sun"][1])
    return dt.hour < 6 or dt.hour >= 21


def _kind(m, p) -> str:
    return icons.kind_of(p.get("code"), _is_night(m, p["dt"]))


def _title(cv, y, text, right=None, right_color=AMBER, icon=None):
    x = 64
    if icon:
        icons.draw_icon(cv, icon, 82, y + 16, 36, mono=AMBER)
        x = 112
    cv.text(x, y, text.upper(), 27, WHITE, 500, track=2)
    if right:
        cv.text(1016, y + 2, right, 24, right_color, 600, track=1, anchor="r")


COND_RU = {"sun": "ЯСНО", "moon": "ЯСНО", "sun_cloud": "МАЛООБЛАЧНО", "moon_cloud": "МАЛООБЛАЧНО", "cloud": "ПАСМУРНО",
           "rain": "ДОЖДЬ", "snow": "СНЕГ", "storm": "ГРОЗА", "fog": "ТУМАН"}


# ───────────────────────────── Страница 1: СЕЙЧАС ─────────────────────────────
def page_now(m: dict, street: str, thumb: Image.Image | None) -> bytes:
    cv = Canvas()
    ui.header(cv)
    f = m["first"]
    kind = icons.kind_of(f.get("code"), m["night"])
    # Герой
    y0 = 150
    cv.card(CARD_X0, y0, CARD_X1, y0 + 330)
    num = _sgn(f["temp"]).replace("+", "")
    w = cv.text(66, y0 + 14, num, 250, WHITE, 200)
    for k in range(10):                                           # пунктирное «°»
        a = k / 10 * 2 * math.pi
        cv.dot(66 + w + 30 + math.cos(a) * 22, y0 + 70 + math.sin(a) * 22, 4, DIM)
    icons.draw_icon(cv, kind, 900, y0 + 130, 150)
    cv.text(900, y0 + 215, COND_RU.get(kind, "ОБЛАЧНО"), 30, WHITE, 500, track=2, anchor="m")
    cv.text(66, y0 + 275, f"↑{m['tmax']:.0f}° ↓{m['tmin']:.0f}°", 25, WHITE, 500)
    cv.text(300, y0 + 275, f"ОЩУЩАЕТСЯ {_sgn(f['feels'])}°", 22, DIM, 500, track=1)
    cv.text(610, y0 + 275, f"ВЕТЕР {m['wind_kmh']:.0f} КМ/Ч", 22, DIM, 500, track=1)
    if f.get("hum") is not None:
        cv.text(1016, y0 + 275, f"ВЛАЖН {f['hum']:.0f}%", 22, DIM, 500, track=1, anchor="r")
    # На улице
    y1 = y0 + 350
    cv.card(CARD_X0, y1, CARD_X1, y1 + 220)
    cv.text(64, y1 + 18, "НА УЛИЦЕ", 26, WHITE, 500, track=2)
    cv.badge(260, y1 + 10, "LIVE", size=20)
    if thumb is not None:
        t = thumb.resize((340, 140), Image.NEAREST)
        cv.im.paste(t.resize((680, 280), Image.NEAREST), (64 * 2, (y1 + 64) * 2))
        cv.d = ui.ImageDraw.Draw(cv.im)
        cv.rect(64, y1 + 64, 404, y1 + 204, outline=BORDER, r=4, width=2)
    yy = cv.wrap(440, y1 + 78, street, 30, WHITE, 580, 400, max_lines=3)
    label, n, pct, _, _ = m["confidence"]
    cv.text(440, y1 + 166, f"{pct}%: {'стабильно' if pct >= 80 else 'меняется'}", 24, DIM, 500)
    cv.badge(660, y1 + 156, label, size=18)
    # Выпуск
    y2 = y1 + 240
    cv.card(CARD_X0, y2, CARD_X1, y2 + 200)
    icons.draw_icon(cv, "sun", 82, y2 + 34, 36, mono=AMBER)
    cv.text(112, y2 + 20, ("ВЕЧЕРНИЙ" if m["tomorrow"] else "УТРЕННИЙ") + " ВЫПУСК", 25, AMBER, 600, track=2)
    cv.text(64, y2 + 66, f"АСТАНА · {'ВЕЧЕРНИЙ' if m['tomorrow'] else 'УТРЕННИЙ'} ВЫПУСК · МЕТЕОРЕДАКЦИЯ", 17, DIM, 500, track=2)
    title, line = m["headline"]
    cv.text(64, y2 + 100, title, 38, AMBER, 700)
    cv.text(64, y2 + 152, line, 26, DIM, 400)
    # Совет
    y3 = y2 + 220
    cv.card(CARD_X0, y3, CARD_X1, y3 + 170)
    cv.text(64, y3 + 22, m["advice"], 40, WHITE, 500)
    cv.badge(64, y3 + 84, label, size=22)
    cv.text(64 + 250, y3 + 94, "ПОЧЕМУ?  ›", 23, DIM, 500, track=1)
    cv.text(64, y3 + 138, f"{n} МОДЕЛИ · СОГЛАСИЕ {pct}%", 20, AMBER, 500, track=1)
    # Следующие 6 часов
    y4 = y3 + 190
    cv.card(CARD_X0, y4, CARD_X1, y4 + 190)
    cv.caps(64, y4 + 16, "Следующие 6 часов", 22, DIM)
    for i, p in enumerate(m["points"][:6]):
        x = 130 + i * 164
        cv.text(x, y4 + 52, f"{p['dt'].hour}", 24, WHITE, 500, anchor="m")
        icons.draw_icon(cv, _kind(m, p), x, y4 + 118, 60, mono=None)
        cv.text(x, y4 + 154, f"{p['rain']:.0f}%", 22, CYAN, 500, anchor="m")
    return cv.finish()


# ───────────────────── Страница 2: ТЕРМИНАЛ + ТЕМПЕРАТУРА ─────────────────────
def page_terminal(m: dict) -> bytes:
    cv = Canvas()
    ui.header(cv, "ПОДРОБНО", f"АСТАНА · {m['start']:%d.%m}")
    intel = m["intel"]
    col = LEVEL_COLOR[intel["level"]]
    y0 = 150
    cv.card(CARD_X0, y0, CARD_X1, y0 + 690)
    cv.rect(66, y0 + 26, 110, y0 + 56, outline=CYAN, r=3, width=3)
    cv.rect(80, y0 + 60, 96, y0 + 64, fill=CYAN)
    cv.text(130, y0 + 24, "RAD ИНТЕЛЛЕКТ-ТЕРМИНАЛ", 26, WHITE, 600, track=4)
    short = {"СПОКОЙНО": "СПОКОЙНО", "ВНИМАНИЕ": "ВНИМАНИЕ", "ПРЕДОСТЕРЕЖЕНИЕ": "ПРЕДОСТ.", "ОПАСНО": "ОПАСНО"}[intel["name"]]
    w = cv.tw(short, 20, 700, 3) + 50
    cv.rect(1016 - w, y0 + 22, 1016, y0 + 62, outline=col, r=6, width=2)
    cv.dot(1016 - w + 20, y0 + 42, 7, col)
    cv.text(1016 - w + 34, y0 + 32, short, 20, col, 700, track=3)
    _dotted(cv, y0 + 84)
    cv.text(66, y0 + 106, "►", 18, CYAN, 600)
    cv.caps(94, y0 + 104, "Анализ угроз", 22, CYAN, 600)
    cv.text(1016, y0 + 104, intel["name"], 24, col, 700, track=3, anchor="r")
    for i in range(10):
        x = 66 + i * 95
        cv.rect(x, y0 + 140, x + 88, y0 + 154, fill=(col if i < intel["seg"] else (46, 46, 46)), r=2)
    cv.text(66, y0 + 168, f"{intel['prior']} // {intel['head']}", 21, col, 500, track=1)
    _dotted(cv, y0 + 206)
    cv.text(66, y0 + 226, "►", 18, CYAN, 600)
    cv.caps(94, y0 + 224, "Сводка аналитики", 22, CYAN, 600)
    y = y0 + 264
    for n, (title, text, ik) in enumerate(intel["items"], 1):
        cv.text(66, y + 6, f"{n:02d}", 22, FAINT, 500)
        cv.rect(110, y, 116, y + 40, fill=col)
        icons.draw_icon(cv, {"fog": "fog", "wind": "cloud", "storm": "storm", "snow": "snow", "sun": "sun", "rain": "rain"}.get(ik, "sun_cloud"),
                        154, y + 22, 40, mono=AMBER)
        cv.text(190, y + 4, title, 24, AMBER, 700, track=2)
        y = cv.wrap(190, y + 40, text, 21, WHITE, 800, 400, max_lines=2)
        y += 4
    y = max(y, y0 + 396)
    _dotted(cv, y + 2)
    cv.text(66, y + 22, "►", 18, CYAN, 600)
    cv.caps(94, y + 20, "Массив датчиков", 22, CYAN, 600)
    f = m["first"]
    dew = f.get("dew")
    pres = f.get("pmsl")
    tiles = [(f"{f['temp']:.0f}°C", "ТЕМП."), (f"{dew:.0f}°C" if dew is not None else "—", "РОСА"),
             (f"{f['uv']:.0f}" if f.get("uv") is not None else "—", "УФ"),
             (f"{m['wind_kmh']:.0f} КМ/Ч", "ВЕТЕР"),
             (f"{'↓' if m['pres_trend'].startswith('▼') else '↑' if m['pres_trend'].startswith('▲') else ''} {pres:.0f} HPA" if pres else "—", "BARO"),
             (f"{f['cloud']:.0f}%" if f.get("cloud") is not None else "—", "ОБЛАКО")]
    ty = y + 56
    for i, (val, lab) in enumerate(tiles):
        x = 66 + (i % 3) * 320
        yy = ty + (i // 3) * 94
        cv.rect(x, yy, x + 308, yy + 84, fill=CARD2, outline=(44, 44, 44), r=6, width=2)
        cv.text(x + 154, yy + 10, val, 30, CYAN, 600, anchor="m")
        cv.text(x + 154, yy + 52, lab, 19, DIM, 400, anchor="m", track=1)
    cv.text(66, y0 + 662, "RAD.INTEL.v2 // АНАЛИЗ ЗАВЕРШЁН", 17, FAINT, 400, track=2)
    # Температура
    y1 = y0 + 706
    _temp_card(cv, m, y1, 1330 - y1)
    return cv.finish()


def _temp_card(cv: Canvas, m: dict, y0: int, h: int):
    pts = m["points"]
    cv.card(CARD_X0, y0, CARD_X1, y0 + h)
    _title(cv, y0 + 20, "Температура")
    cv.dot(740, y0 + 36, 8, AMBER)
    cv.text(756, y0 + 24, "ФАКТ", 20, DIM, 500)
    cv.dot(850, y0 + 36, 8, CYAN)
    cv.text(866, y0 + 24, "ОЩУЩАЕТСЯ", 20, DIM, 500)
    CX0, CX1 = 150, 1010
    CT, CB = y0 + 96, y0 + h - 100
    lo = min(min(p["temp"] for p in pts), min(p["feels"] for p in pts))
    hi = max(max(p["temp"] for p in pts), max(p["feels"] for p in pts))
    pad = max(1.0, (hi - lo) * 0.12)
    lo, hi = lo - pad, hi + pad
    n = len(pts)
    xs = [CX0 + i * (CX1 - CX0) / (n - 1) for i in range(n)]

    def ty(v):
        return CB - (v - lo) / (hi - lo) * (CB - CT)

    for k in range(3):
        v = lo + (hi - lo) * k / 2
        cv.text(CX0 - 20, ty(v) - 12, f"{v:.0f}°", 20, DIM, 400, anchor="r")
    line = [(xs[i], ty(p["temp"])) for i, p in enumerate(pts)]
    cv.fill_under(line, CB, (46, 38, 24), (12, 20, 24))
    cv.line([(xs[i], ty(p["feels"])) for i, p in enumerate(pts)], (30, 130, 160), 3, dashed=True)
    cv.line(line, AMBER, 4)
    imax = max(range(n), key=lambda i: pts[i]["temp"])
    imin = min(range(n), key=lambda i: pts[i]["temp"])
    cv.text(xs[imax], ty(pts[imax]["temp"]) - 56, "H", 20, WHITE, 700, anchor="m")
    cv.text(xs[imax], ty(pts[imax]["temp"]) - 32, f"{pts[imax]['temp']:.0f}°", 22, WHITE, 500, anchor="m")
    cv.text(xs[imin], ty(pts[imin]["temp"]) + 12, f"{pts[imin]['temp']:.0f}°", 22, WHITE, 500, anchor="m")
    cv.text(xs[imin], ty(pts[imin]["temp"]) + 36, "L", 20, WHITE, 700, anchor="m")
    for i, p in enumerate(pts):
        if p["dt"].hour % 4 == 0:
            cv.text(xs[i], CB + 18, f"{p['dt'].hour}", 20, DIM, 400, anchor="m")
    diff = pts[0]["temp"] - pts[0]["feels"]
    if diff >= 2:
        msg = f"Ветер сделает на {diff:.0f}° холоднее фактической"
    elif diff <= -2:
        msg = f"Ощущается на {-diff:.0f}° теплее фактической"
    else:
        msg = "Ощущается примерно как фактическая"
    cv.text(64, y0 + h - 50, msg, 24, WHITE, 400)


# ───────────────────── Страница 3: ВЕТЕР, ОСАДКИ ─────────────────────
def _compass(cv: Canvas, cx, cy, r, deg, speed, gust):
    cv.dot(cx, cy, r, None, outline=(78, 78, 78))
    cv.d.ellipse([(cx - r) * ui.S, (cy - r) * ui.S, (cx + r) * ui.S, (cy + r) * ui.S], outline=(78, 78, 78), width=3 * ui.S)
    for k in range(16):
        a = k / 16 * 2 * math.pi
        l = 16 if k % 4 == 0 else 8
        cv.line([(cx + math.sin(a) * (r - l), cy - math.cos(a) * (r - l)), (cx + math.sin(a) * r, cy - math.cos(a) * r)], (110, 110, 110), 3)
    for lab, a, c in (("С", 0, AMBER), ("В", 90, WHITE), ("Ю", 180, WHITE), ("З", 270, WHITE)):
        a = math.radians(a)
        cv.text(cx + math.sin(a) * (r - 40), cy - math.cos(a) * (r - 40) - 14, lab, 24, c, 700, anchor="m")
    cv.dot(cx, cy, 50, (14, 14, 14))
    cv.d.ellipse([(cx - 50) * ui.S, (cy - 50) * ui.S, (cx + 50) * ui.S, (cy + 50) * ui.S], outline=(78, 78, 78), width=2 * ui.S)
    cv.text(cx, cy - 26, f"{speed:.0f}", 44, WHITE, 500, anchor="m")
    cv.text(cx, cy + 18, f"G{gust:.0f}", 20, RED, 500, anchor="m")
    if deg is not None:
        a = math.radians(deg)                                    # откуда дует; стрелка — куда
        fx, fy = cx + math.sin(a) * (r - 18), cy - math.cos(a) * (r - 18)
        tx, ty_ = cx - math.sin(a) * (r - 18), cy + math.cos(a) * (r - 18)
        cv.line([(fx, fy), (cx + math.sin(a) * 52, cy - math.cos(a) * 52)], AMBER, 6)
        cv.line([(cx - math.sin(a) * 52, cy + math.cos(a) * 52), (tx, ty_)], AMBER, 6)
        cv.rect(fx - 9, fy - 9, fx + 9, fy + 9, fill=AMBER)
        ux, uy = (tx - cx) / max(1, math.hypot(tx - cx, ty_ - cy)), (ty_ - cy) / max(1, math.hypot(tx - cx, ty_ - cy))
        px_, py_ = -uy, ux
        cv.d.polygon([((tx + ux * 14) * ui.S, (ty_ + uy * 14) * ui.S), ((tx - ux * 10 + px_ * 14) * ui.S, (ty_ - uy * 10 + py_ * 14) * ui.S),
                      ((tx - ux * 10 - px_ * 14) * ui.S, (ty_ - uy * 10 - py_ * 14) * ui.S)], fill=AMBER)


def _drop(cv: Canvas, x, y, s=14, color=CYAN):
    cv.d.polygon([(x * ui.S, (y - s) * ui.S), ((x - s * 0.7) * ui.S, (y + s * 0.2) * ui.S), ((x + s * 0.7) * ui.S, (y + s * 0.2) * ui.S)], fill=color)
    cv.dot(x, y + s * 0.2, s * 0.7, color)


def page_wind_rain(m: dict) -> bytes:
    cv = Canvas()
    pts = m["points"]
    # Ветер
    y0 = 40
    cv.card(CARD_X0, y0, CARD_X1, y0 + 470)
    _title(cv, y0 + 22, "Ветер", icon="cloud")
    _compass(cv, 210, y0 + 240, 130, m["wind_dir"], m["wind_kmh"], m["gust_kmh"])
    cv.caps(440, y0 + 98, "Скорость", 19, DIM)
    cv.text(440, y0 + 124, f"{m['wind_kmh']:.0f} КМ/Ч", 40, WHITE, 500)
    cv.caps(440, y0 + 190, "Порывы", 19, DIM)
    cv.text(440, y0 + 216, f"{m['gust_kmh']:.0f} КМ/Ч", 40, RED, 500)
    cv.caps(440, y0 + 282, "Из", 19, DIM)
    d = m["wind_dir"]
    cv.text(440, y0 + 306, f"{ins.compass_name(d)} ({d:.0f}°)" if d is not None else "—", 36, WHITE, 500)
    lvl, name = ins.wind_level(m["wind_kmh"])
    cv.caps(440, y0 + 360, "Шкала", 19, DIM)
    cv.text(560, y0 + 352, name, 30, AMBER, 500)
    scale = [(150, 150, 150), CYAN, GREEN, AMBER, ORANGE, RED, (150, 40, 40), (110, 20, 20)]
    for i in range(8):
        x = 440 + i * 74
        cv.rect(x, y0 + 392, x + 66, y0 + 404, fill=(scale[i] if i <= lvl else (48, 48, 48)), r=2)
    k, klabel = ins.gust_factor(m["wind_kmh"], m["gust_kmh"])
    cv.text(64, y0 + 430, f"Коэффициент порывистости {k:.1f}x: {klabel}", 24, WHITE, 400)
    # Вероятность осадков
    y1 = y0 + 490
    cv.card(CARD_X0, y1, CARD_X1, y1 + 376)
    _drop(cv, 80, y1 + 36, 14)
    cv.text(112, y1 + 22, "ВЕРОЯТНОСТЬ ОСАДКОВ", 27, WHITE, 500, track=2)
    peak = max(p["rain"] for p in pts)
    cv.text(64, y1 + 78, f"Вероятность сегодня: {peak:.0f}%", 31, WHITE, 400)
    cv.text(1016, y1 + 84, f"ожидается {m['layer']['total']:.1f} мм", 24, WHITE, 400, anchor="r")
    cv.text(64, y1 + 128, f"Следующие осадки {m['next_rain'][0]}", 25, DIM, 400)
    _bars(cv, pts, y1 + 180, y1 + 320, lambda p: p["rain"] / 100, CYAN, 66, 1014, midnight=True)
    # Слой осадков
    y2 = y1 + 396
    L = m["layer"]
    cv.card(CARD_X0, y2, CARD_X1, y2 + 1330 - y2)
    icons.draw_icon(cv, "rain", 84, y2 + 36, 44)
    cv.text(112, y2 + 20, "СЛОЙ ОСАДКОВ", 27, WHITE, 500, track=2)
    cv.text(1016, y2 + 22, L["level"], 26, AMBER, 500, anchor="r")
    cv.caps(64, y2 + 70, "Всего", 19, DIM)
    cv.text(64, y2 + 92, f"{L['total']:.1f} мм", 50, WHITE, 500)
    cv.text(64, y2 + 160, "осадки за 24 ч", 20, DIM, 400)
    mx = max(1.0, max((p.get("precip") or 0) for p in pts))
    colors = {"rain": (60, 170, 210), "snow": WHITE, "ice": PINK, None: (60, 170, 210)}
    _bars(cv, pts, y2 + 190, y2 + 330, lambda p: (p.get("precip") or 0) / mx, CYAN, 66, 1014,
          colors=[colors[k] for k in L["kinds"]])
    ly = min(y2 + 372, 1286)
    cv.dot(76, ly + 12, 7, (60, 170, 210)); cv.text(92, ly, "ДОЖДЬ", 20, WHITE, 400)
    cv.dot(236, ly + 12, 7, WHITE); cv.text(252, ly, "СНЕГ", 20, WHITE, 400)
    cv.dot(366, ly + 12, 7, PINK); cv.text(382, ly, "ЛЁД/ЛЕДЯНАЯ КРУПА", 20, WHITE, 400)
    return cv.finish()


def _bars(cv, pts, ytop, ybot, val, color, x0, x1, midnight=False, colors=None, hours_step=4):
    n = len(pts)
    bw = (x1 - x0) / n
    cv.rect(x0, ybot, x1, ybot + 1, fill=(60, 60, 60))
    for i, p in enumerate(pts):
        v = max(0.0, min(1.0, val(p)))
        h = (ybot - ytop) * v
        c = colors[i] if colors else color
        if h >= 1:
            cv.rect(x0 + i * bw + 3, ybot - h, x0 + (i + 1) * bw - 3, ybot, fill=c)
        elif v > 0:
            cv.rect(x0 + i * bw + 3, ybot - 3, x0 + (i + 1) * bw - 3, ybot, fill=c)
        if p["dt"].hour % hours_step == 0:
            cv.text(x0 + (i + 0.5) * bw, ybot + 12, f"{p['dt'].hour}", 19, DIM, 400, anchor="m")
        if midnight and p["dt"].hour == 0 and i:
            x = x0 + i * bw
            cv.line([(x, ytop - 20), (x, ybot)], (200, 200, 200), 2, dashed=True, dash=(6, 6))
            cv.text(x + 14, ytop - 22, "ЗАВТРА", 17, DIM, 400)


# ───────────────────── Страница 4: РАЗБРОС, СОЛНЦЕ ─────────────────────
def page_spread_sun(m: dict) -> bytes:
    cv = Canvas()
    pts = m["points"]
    sp = m["spread"]
    y0 = 40
    cv.card(CARD_X0, y0, CARD_X1, y0 + 200)
    cv.caps(64, y0 + 20, "Осадки", 21, AMBER, 600)
    cv.text(64, y0 + 62, sp["title"], 44, WHITE, 700)
    pk = sp["peak"]
    cv.text(64, y0 + 130, f"{pk['rain']:.0f} % в {pk['dt'].hour}", 25, DIM, 400)
    y1 = y0 + 220
    cv.card(CARD_X0, y1, CARD_X1, y1 + 440)
    cv.caps(64, y1 + 22, "Ближайшие 24 часа", 21, DIM)
    cv.text(1016, y1 + 20, f"пик {pk['rain']:.0f} %", 23, WHITE, 400, anchor="r")
    cv.text(64, y1 + 64, f"{pk['dt'].hour}", 26, WHITE, 500)
    cv.text(110, y1 + 64, f"{pk['rain']:.0f}%", 26, CYAN, 700)
    cv.text(200, y1 + 66, f"модели {pk['rain_lo']:.0f}–{pk['rain_hi']:.0f} %", 22, DIM, 400)
    ytop, ybot = y1 + 130, y1 + 320
    n = len(pts)
    x0, x1 = 66, 1014
    bw = (x1 - x0) / n
    cv.rect(x0, ybot, x1, ybot + 1, fill=(60, 60, 60))
    for i, p in enumerate(pts):
        xm = x0 + (i + 0.5) * bw
        h = (ybot - ytop) * p["rain"] / 100
        cv.rect(xm - bw * 0.3, ybot - max(h, 3), xm + bw * 0.3, ybot, fill=CYAN)
        if p["rain_hi"] - p["rain_lo"] >= 6:
            ylo = ybot - (ybot - ytop) * p["rain_lo"] / 100
            yhi = ybot - (ybot - ytop) * p["rain_hi"] / 100
            cv.line([(xm, ylo), (xm, yhi)], (210, 210, 210), 2)
            cv.line([(xm - 8, yhi), (xm + 8, yhi)], (210, 210, 210), 2)
            cv.line([(xm - 8, ylo), (xm + 8, ylo)], (210, 210, 210), 2)
        cv.text(xm, ybot + 12, f"{p['dt'].hour}", 15, DIM, 400, anchor="m")
    cv.rect(64, y1 + 370, 84, y1 + 390, fill=CYAN)
    cv.text(96, y1 + 368, "ADA CORTEX", 20, WHITE, 500, track=2)
    cv.text(330, y1 + 368, "I МОДЕЛИ, ОТ НИЗА ДО ВЕРХА", 20, WHITE, 400, track=1)
    cv.text(64, y1 + 405, f"Сегодня модели расходятся до {sp['widest']:.0f} пунктов", 22, DIM, 400)
    # Солнце
    y2 = y1 + 460
    cv.card(CARD_X0, y2, CARD_X1, 1330)
    icons.draw_icon(cv, "sun", 84, y2 + 36, 44, mono=AMBER)
    cv.text(112, y2 + 20, "СОЛНЦЕ", 27, WHITE, 500, track=2)
    arc = m["arc"]
    if not arc:
        cv.text(64, y2 + 100, "нет данных о восходе и закате", 26, DIM, 400)
        return cv.finish()
    cx, cy, rx, ry = 540, y2 + 340, 440, 230
    pts_arc = [(cx - rx * math.cos(math.pi * t / 60), cy - ry * math.sin(math.pi * t / 60)) for t in range(61)]
    cv.line(pts_arc, AMBER_DIM, 4, dashed=True, dash=(8, 6))
    cv.rect(cx - rx - 8, cy, cx + rx + 8, cy + 1, fill=(70, 70, 70))
    t = arc["frac"]
    done = [(cx - rx * math.cos(math.pi * k / 60 * t), cy - ry * math.sin(math.pi * k / 60 * t)) for k in range(61)]
    if t > 0:
        cv.line(done, AMBER, 5)
    sx, sy = cx - rx * math.cos(math.pi * t), cy - ry * math.sin(math.pi * t)
    cv.dot(sx, sy, 26, (70, 50, 10))
    cv.dot(sx, sy, 17, AMBER if arc["above"] else (110, 90, 40))
    cv.text(64, y2 + 400, arc["rise"], 48, WHITE, 600)
    cv.text(1016, y2 + 400, arc["set"], 48, WHITE, 600, anchor="r")
    cv.text(540, y2 + 398, arc["length"], 28, DIM, 400, anchor="m")
    icons.draw_icon(cv, "sun", 84, y2 + 376, 30, mono=AMBER)
    cv.rect(64, y2 + 470, 1016, y2 + 471, fill=(60, 60, 60))
    for i, (lab, val) in enumerate((("ДО ЗАКАТА", arc["left"]), ("СОЛНЕЧНЫЙ ПОЛДЕНЬ", arc["noon"]), ("ЗОЛОТОЙ ЧАС", arc["golden"]))):
        x = 64 + i * 330
        cv.caps(x, y2 + 486, lab, 18, DIM)
        cv.text(x, y2 + 514, val, 34, WHITE, 500)
    return cv.finish()


# ───────────────────── Страница 5: ПО ДНЯМ, ГРОЗЫ, ДОСТОВЕРНОСТЬ ─────────────────────
def page_days(m: dict) -> bytes:
    cv = Canvas()
    days = m["daily"]
    y0 = 40
    cv.card(CARD_X0, y0, CARD_X1, y0 + 590)
    _title(cv, y0 + 22, "Прогноз по дням")
    if len(days) >= 2:
        n = len(days)
        step = 920 / n
        mx_hi, mx_lo = max(d["tmax"] for d in days), min(d["tmax"] for d in days)
        mn_hi, mn_lo = max(d["tmin"] for d in days), min(d["tmin"] for d in days)

        def ymax(v):
            return y0 + 250 - (v - mx_lo) / max(1.0, mx_hi - mx_lo) * 70

        def ymin(v):
            return y0 + 400 - (v - mn_lo) / max(1.0, mn_hi - mn_lo) * 60

        xs = [60 + step * (i + 0.5) for i in range(n)]
        cv.line([(xs[i], ymax(d["tmax"])) for i, d in enumerate(days)], AMBER_DIM, 3, dashed=True)
        cv.line([(xs[i], ymin(d["tmin"])) for i, d in enumerate(days)], (24, 90, 110), 3, dashed=True)
        for i, d in enumerate(days):
            x = xs[i]
            code = d["code"]
            icons.draw_icon(cv, icons.kind_of(code if code is not None else 3, False), x, ymax(d["tmax"]) - 64, 64)
            cv.dot(x, ymax(d["tmax"]), 7, AMBER if i == 0 else WHITE)
            cv.text(x, ymax(d["tmax"]) + 16, f"{d['tmax']:.0f}°", 26, AMBER if i == 0 else WHITE, 500, anchor="m")
            cv.dot(x, ymin(d["tmin"]), 6, DIM)
            cv.text(x, ymin(d["tmin"]) + 14, f"{d['tmin']:.0f}°", 24, DIM, 500, anchor="m")
            name = ("СЕГОДНЯ" if i == 0 else "ЗАВТРА" if i == 1 else WEEK[d["date"].weekday()]) if not m["tomorrow"] else \
                   ("ЗАВТРА" if i == 0 else WEEK[d["date"].weekday()])
            cv.text(x, y0 + 478, name, 17 if len(name) > 3 else 22, WHITE if i == 0 else DIM, 600, anchor="m")
            cv.text(x, y0 + 508, f"{(d['rain'] or 0):.0f}%", 18, CYAN, 400, anchor="m")
        cv.text(64, y0 + 552, "Под днём: вероятность осадков", 19, DIM, 400)
    else:
        cv.text(64, y0 + 100, "нет данных по дням", 26, DIM, 400)
    # Риск гроз
    y1 = y0 + 610
    lv, tx = m["thunder"]
    cv.card(CARD_X0, y1, CARD_X1, y1 + 250)
    icons.draw_icon(cv, "storm", 84, y1 + 36, 40, mono=AMBER)
    cv.text(112, y1 + 20, "РИСК ГРОЗ", 27, WHITE, 500, track=2)
    cv.text(1016, y1 + 24, "БЛИЖАЙШИЕ 24 Ч", 20, DIM, 500, anchor="r", track=1)
    cv.text(64, y1 + 70, "НЕТ" if lv == 0 else "ВОЗМОЖЕН" if lv == 1 else "ЕСТЬ", 78, WHITE if lv == 0 else AMBER if lv == 1 else RED, 700)
    expl = ("Организованной конвекции сегодня не ожидается" if lv == 0 else
            f"Грозовая активность: {tx.lower()}")
    cv.text(64, y1 + 170, expl, 24, WHITE, 400)
    cv.text(64, y1 + 204, "Неустойчивость атмосферы " + ("низкая" if lv == 0 else "повышенная") + " в ближайшие 24 часа.", 20, WHITE, 400)
    cv.text(64, y1 + 228, "SRC: OPEN-METEO", 15, DIM, 400, track=1)
    # Достоверность
    y2 = y1 + 270
    label, n, pct, t, r = m["confidence"]
    cv.card(CARD_X0, y2, CARD_X1, 1330)
    cv.caps(64, y2 + 20, "Достоверность прогноза", 22, AMBER, 600)
    cv.text(1016, y2 + 18, f"{pct}%", 40, WHITE, 600, anchor="r")
    cv.text(64, y2 + 62, f"СОГЛАСИЕ {n} МОДЕЛЕЙ: ECMWF · GFS · ICON", 19, DIM, 500, track=1)
    for i, (lab, val) in enumerate((("СРЕДНИЙ РАЗБРОС ПО ТЕМПЕРАТУРЕ", f"{t:.1f}°C"), ("СРЕДНИЙ РАЗБРОС ПО ОСАДКАМ", f"{r} п.п."), ("ОЦЕНКА СОГЛАСИЯ", label))):
        yy = y2 + 108 + i * 52
        cv.text(64, yy, lab, 21, WHITE, 400, track=1)
        cv.text(1016, yy - 2, val, 27, AMBER if i < 2 else GREEN, 600, anchor="r")
    cv.text(64, y2 + 276, "Это согласие моделей между собой,", 18, DIM, 400)
    cv.text(64, y2 + 300, "а не проверка по метеостанциям.", 18, DIM, 400)
    return cv.finish()


# ───────────────────── Страница 6: ПЛИТКИ ─────────────────────
def _moon(cv: Canvas, cx, cy, r, phase):
    S = ui.S
    lay = Image.new("RGBA", (int(r * 2 * S) + 4, int(r * 2 * S) + 4), (0, 0, 0, 0))
    k = math.cos(2 * math.pi * phase)
    right = phase < 0.5
    R = r * S
    c = R + 2
    for y in range(lay.height):
        for x in range(lay.width):
            dx, dy = x - c, y - c
            if dx * dx + dy * dy > R * R:
                continue
            half = math.sqrt(max(0.0, R * R - dy * dy))
            edge = k * half
            lit = (dx >= edge) if right else (dx <= -edge)
            lay.putpixel((x, y), (236, 232, 205, 255) if lit else (60, 60, 70, 255))
    cv.im.paste(lay, (int(cx * S - c), int(cy * S - c)), lay)
    cv.d = ui.ImageDraw.Draw(cv.im)


def _tile(cv, col, row, title, value, sub, vcolor=WHITE, sub2=None, locked_icon=None):
    x0 = 40 + col * 510
    y0 = 40 + row * 320
    cv.card(x0, y0, x0 + 490, y0 + 300)
    cv.text(x0 + 24, y0 + 22, title, 22, DIM, 500, track=3)
    cv.text(x0 + 24, y0 + 78, value, 54, vcolor, 500)
    cv.text(x0 + 24, y0 + 168, sub, 26, WHITE, 400)
    if sub2:
        cv.text(x0 + 24, y0 + 212, sub2, 21, DIM, 400)
    return x0, y0


def page_tiles(m: dict) -> bytes:
    cv = Canvas(1080, 1340)
    f = m["first"]
    pres = f.get("pmsl")
    mm = f.get("pres")
    import re
    trend = m["pres_trend"]
    num = re.search(r"\d+", trend)
    tword = ("падает" if trend.startswith("▼") else "растёт" if trend.startswith("▲") else "стабильно")
    ttext = f"{tword} на {num.group()} мм за 12 ч" if num and tword != "стабильно" else "стабильно"
    _tile(cv, 0, 0, "ДАВЛЕНИЕ", f"{pres:.0f} гПа" if pres else "—", ttext,
          sub2=f"≈ {mm:.0f} мм рт. ст. на станции" if mm else None)
    vis = m.get("vis_min")
    vk = (vis / 1000) if vis is not None else None
    _tile(cv, 1, 0, "ВИДИМОСТЬ", f"{vk:.0f} км" if vk is not None else "—",
          "хорошая" if vk is None or vk >= 10 else "сниженная" if vk >= 2 else "туман",
          vcolor=WHITE if vk is None or vk >= 10 else AMBER, sub2="минимум за 24 часа")
    hum = f.get("hum")
    _tile(cv, 0, 1, "ВЛАЖНОСТЬ", f"{hum:.0f}%" if hum is not None else "—",
          ("сухо" if hum is not None and hum < 40 else "комфортно" if hum is not None and hum < 70 else "влажно") if hum is not None else "",
          sub2=f"точка росы {f['dew']:.0f}°" if f.get("dew") is not None else None, vcolor=CYAN)
    uv, uvl = m["uv"]
    x0, y0 = _tile(cv, 1, 1, "УФ-ИНДЕКС", f"{uv:.1f}", uvl.capitalize(), vcolor=AMBER if uv >= 6 else GREEN)
    for i in range(11):
        c = GREEN if i < 3 else AMBER if i < 6 else ORANGE if i < 8 else RED
        cv.rect(x0 + 24 + i * 40, y0 + 230, x0 + 24 + i * 40 + 34, y0 + 242, fill=(c if i < uv else (46, 46, 46)), r=2)
    cloud = f.get("cloud")
    _tile(cv, 0, 2, "ОБЛАЧНОСТЬ", f"{cloud:.0f}%" if cloud is not None else "—",
          ("ясно" if cloud < 20 else "малооблачно" if cloud < 50 else "облачно" if cloud < 85 else "пасмурно") if cloud is not None else "",
          sub2="в начале прогноза")
    label, lvl = ins_aqi(m)
    _tile(cv, 1, 2, "ВОЗДУХ", f"AQI {m['aqi_value']:.0f}" if m["aqi_value"] is not None else "нет данных",
          label, vcolor=[GREEN, AMBER, RED, RED][lvl] if lvl >= 0 else DIM,
          sub2=f"PM2.5 {m['pm25']:.0f} мкг/м³" if m.get("pm25") is not None else "источник: Open-Meteo")
    x0, y0 = _tile(cv, 0, 3, "ФАЗА ЛУНЫ", "", m["moon"][1], sub2=None)
    _moon(cv, x0 + 400, y0 + 90, 52, m["moon"][0])
    ilv, itx = m["ice"]
    _tile(cv, 1, 3, "ГОЛОЛЁД", "нет" if ilv == 0 else "возможен" if ilv == 1 else "высокий",
          "дороги в порядке" if ilv == 0 else itx.lower(), vcolor=GREEN if ilv == 0 else AMBER if ilv == 1 else RED,
          sub2="по температуре и осадкам")
    return cv.finish()


def ins_aqi(m):
    from services.pixel_weather.model import aqi_label
    label, lvl = aqi_label(m["aqi_value"])
    return label.capitalize(), lvl


# ───────────────────── DISPATCH ─────────────────────
def dispatch_card(m: dict, text: str, frame: Image.Image | None, tag: str) -> bytes:
    cv = Canvas(1080, 1080)
    cv.rect(40, 40, 1040, 1040, fill=CARD, outline=(110, 80, 20), r=18, width=3)
    cv.rect(40, 40, 1040, 120, fill=(26, 24, 20), r=18)
    cv.rect(40, 100, 1040, 120, fill=(26, 24, 20))
    for i, c in enumerate(((200, 50, 50), (210, 150, 20), (50, 170, 70))):
        cv.dot(80 + i * 34, 80, 11, c)
    cv.text(540, 62, "ADA DISPATCH", 28, AMBER, 700, track=6, anchor="m")
    if frame is not None:
        img = frame.convert("RGB").resize((1000, 560), Image.NEAREST)
        gray = img.convert("L")
        tinted = Image.merge("RGB", (gray.point(lambda v: min(255, int(v * 1.1))), gray.point(lambda v: int(v * 0.95)), gray.point(lambda v: int(v * 0.85))))
        cv.im.paste(tinted.resize((2000, 1120), Image.NEAREST), (80, 240))
        cv.d = ui.ImageDraw.Draw(cv.im)
    w = cv.tw(tag, 26, 700, 4) + 36
    cv.rect(1016 - w, 140, 1016, 186, fill=(18, 16, 12), r=6)
    cv.text(998, 150, tag, 26, AMBER, 700, track=4, anchor="r")
    cv.wrap(76, 750, text, 44, AMBER, 930, 400, max_lines=3)
    cv.text(76, 1000, "СДЕЛАНО В АДЕ · СЕМЕЙНЫЙ БОТ", 17, FAINT, 500, track=2)
    return cv.finish()


# ───────────────────── ОДНА КАРТИНКА: ПРОГНОЗ НА 24 ЧАСА ─────────────────────
def _window_card(cv: Canvas, m: dict, win: list[dict], y0: int, label: str):
    cv.card(CARD_X0, y0, CARD_X1, y0 + 424)
    cv.text(64, y0 + 20, label, 27, WHITE, 600, track=2)
    cv.dot(740, y0 + 36, 8, AMBER)
    cv.text(756, y0 + 24, "ТЕМП.", 19, DIM, 500)
    cv.dot(850, y0 + 36, 8, CYAN)
    cv.text(866, y0 + 24, "ОЩУЩАЕТСЯ", 19, DIM, 500)
    n = len(win)
    X0, X1 = 150, 1004
    xs = [X0 + i * (X1 - X0) / (n - 1) for i in range(n)]
    CT, CB = y0 + 96, y0 + 190
    lo = min(min(p["temp"] for p in win), min(p["feels"] for p in win))
    hi = max(max(p["temp"] for p in win), max(p["feels"] for p in win))
    pad = max(1.5, (hi - lo) * 0.15)
    lo, hi = lo - pad, hi + pad

    def ty(v):
        return CB - (v - lo) / (hi - lo) * (CB - CT)

    if lo < 0 < hi:
        cv.line([(X0, ty(0)), (X1, ty(0))], (70, 70, 70), 2, dashed=True, dash=(6, 6))
    line = [(xs[i], ty(p["temp"])) for i, p in enumerate(win)]
    cv.fill_under(line, CB, (46, 38, 24), (14, 18, 20))
    cv.line([(xs[i], ty(p["feels"])) for i, p in enumerate(win)], (30, 130, 160), 3, dashed=True)
    cv.line(line, AMBER, 4)
    for i, p in enumerate(win):
        if p["dt"].hour % 3 == 0:
            cv.dot(xs[i], ty(p["temp"]), 6, AMBER)
            cv.text(xs[i], ty(p["temp"]) - 36, f"{_sgn(p['temp'])}°", 24, WHITE, 600, anchor="m")
            cv.text(xs[i], y0 + 204, f"{p['dt'].hour:02d}", 20, DIM, 500, anchor="m")
            icons.draw_icon(cv, _kind(m, p), xs[i], y0 + 250, 46)
    cv.text(24, y0 + 306, "ДОЖДЬ", 15, CYAN, 600, track=1)
    cv.text(24, y0 + 324, "%", 14, DIM, 400)
    base = y0 + 346
    for i, p in enumerate(win):
        h = 40 * p["rain"] / 100
        if h >= 1:
            cv.rect(xs[i] - 12, base - h, xs[i] + 12, base, fill=CYAN if p["rain"] < 70 else (150, 120, 255))
        if p["dt"].hour % 3 == 0:
            cv.text(xs[i], base + 6, f"{p['rain']:.0f}%", 17, DIM, 400, anchor="m")
    cv.rect(X0 - 14, base, X1 + 14, base + 1, fill=(60, 60, 60))
    cv.text(24, y0 + 384, "ВЕТЕР", 15, GREEN, 600, track=1)
    cv.text(24, y0 + 402, "км/ч", 14, DIM, 400)
    for i, p in enumerate(win):
        if p["dt"].hour % 3 == 0:
            w = p["wind_kmh"]
            cv.text(xs[i], y0 + 386, f"{w:.0f}", 21, GREEN if w < 30 else AMBER if w < 50 else RED, 600, anchor="m")


def page_day(m: dict) -> bytes:
    """Одна картинка: два окна по 12 часов (температура, ощущается, осадки, ветер) + одежда, зонт при необходимости, солнце и луна."""
    from services import weather_chart as wc
    cv = Canvas(1080, 1450)
    st = m["start"]
    ui.header(cv, "АСТАНА", f"{wc.WEEKDAYS_RU[st.weekday()]}, {st.day} {wc.MONTHS_RU_GEN[st.month - 1]}".upper())
    pts = m["points"]
    wins = [pts[:13], pts[12:25]]
    y = 150
    for win in wins:
        if len(win) >= 2:
            _window_card(cv, m, win, y, wc.window_label(win).upper())
            y += 444
    # нижний блок
    cv.card(CARD_X0, y, CARD_X1, 1430)
    yy = y + 22
    cv.caps(64, yy, "Одежда", 19, AMBER, 600)
    yy = cv.wrap(64, yy + 28, m["clothes"], 23, WHITE, 940, 400, max_lines=3) + 8
    umb = wc.umbrella_text(pts)
    if umb:
        cv.caps(64, yy, "Зонт" if "снег" not in umb[0].lower() else "Снег", 19, CYAN, 600)
        yy = cv.wrap(64, yy + 28, umb[0], 23, WHITE, 940, 400, max_lines=3) + 8
    if m["sun"]:
        cv.caps(64, yy, "Солнце", 19, AMBER, 600)
        cv.text(64, yy + 28, f"восход {m['sun'][0]}  ·  закат {m['sun'][1]}  ·  золотой час {m['golden']}", 23, WHITE, 400)
        yy += 70
    cv.caps(64, yy, "Луна", 19, AMBER, 600)
    cv.text(64, yy + 28, m["moon"][1], 23, WHITE, 400)
    _moon(cv, 960, yy + 20, 26, m["moon"][0])
    spread = max(p.get("spread", 0) for p in pts)
    if spread >= 3:
        cv.text(64, yy + 68, f"Модели расходятся до {spread:.0f}°: прогноз может поплыть", 20, DIM, 400)
    return cv.finish()

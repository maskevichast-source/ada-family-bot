"""Анимированная карточка: небо с погодой, силуэт Астаны (Байтерек), температура, фраза Ады «печатается»."""
import io
import math
import random

from PIL import Image

from services.pixel_weather import gfx, theme as T
from services.pixel_weather.model import aqi_label

FRAMES = 64
TYPE_FRAMES = 26
SX0, SY0, SX1, SY1 = 5, 17, 155, 98          # окно сцены (внутренняя область)
SW, SH = SX1 - SX0 + 1, SY1 - SY0 + 1
PHRASE_WIDTH = 36
SKYLINE_SEED = 7


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _wrap_x(x0, travel, phase, span, lo):
    """Периодический дрейф: за цикл ровно целое число оборотов — склейка петли незаметна."""
    return lo + int((x0 + phase * travel) % span)


def _sky(p: gfx.Page, night: bool, flash: bool):
    top, hor = ((8, 10, 30), (26, 30, 70)) if night else ((28, 70, 130), (96, 160, 205))
    if flash:
        top, hor = (120, 130, 170), (190, 200, 225)
    bands = 9
    bh = SH // bands + 1
    for b in range(bands):
        y0 = SY0 + b * bh
        c1 = _lerp(top, hor, b / bands)
        c2 = _lerp(top, hor, min(1.0, (b + 1) / bands))
        p.dither(SX0, y0, SX1, min(SY1, y0 + bh - 1), c1, c2, 0.5)


def _skyline(p: gfx.Page, night: bool, phase: float):
    rnd = random.Random(SKYLINE_SEED)
    base = SY1
    col = (14, 18, 38) if night else (30, 44, 78)
    x = SX0
    while x < SX1:
        w = rnd.randint(8, 14)
        h = rnd.randint(10, 26)
        if abs((x + w // 2) - (SX0 + SW // 2)) < 14:           # место под Байтерек
            x += w
            continue
        p.rect(x, base - h, min(x + w - 1, SX1), base, fill=col)
        for wy in range(base - h + 3, base - 2, 4):
            for wx in range(x + 2, x + w - 2, 4):
                if night and rnd.random() < 0.55:
                    lit = (T.AMBER if (int(phase * 8) + wx + wy) % 11 else col)
                    p.px(wx, wy, lit)
                else:
                    rnd.random()
        x += w
    # Байтерек: тонкая опора + шар
    cx = SX0 + SW // 2
    pole = (20, 26, 52) if night else (44, 60, 100)
    p.rect(cx - 1, base - 34, cx, base, fill=pole)
    p.rect(cx - 4, base - 6, cx + 3, base, fill=pole)
    p.disc(cx, base - 40, 6, pole)
    p.disc(cx, base - 40, 3, T.AMBER if night else (150, 170, 210))
    p.rect(cx - 1, base - 52, cx, base - 46, fill=pole)
    p.px(cx - 1, base - 53, T.AMBER)
    p.rect(SX0, base - 1, SX1, base, fill=(10, 12, 26) if night else (24, 34, 60))


def _stars(p: gfx.Page, phase: float, density: float):
    rnd = random.Random(3)
    for i in range(int(40 * density)):
        x = rnd.randint(SX0 + 1, SX1 - 1)
        y = rnd.randint(SY0 + 1, SY0 + SH * 2 // 3)
        tw = (int(phase * 8) + i) % 6
        p.px(x, y, T.WHITE if tw else (90, 100, 150))


def _cloud(p, x, y, w, color, shade):
    gfx.icon_cloud(p, x, y, w, color=color, shade=shade)


def _clouds(p: gfx.Page, phase: float, count: int, dark: bool, night: bool):
    if count <= 0:
        return
    base = (60, 66, 92) if dark else ((150, 160, 195) if night else T.WHITE)
    shade = (40, 46, 70) if dark else ((110, 120, 155) if night else (190, 205, 230))
    rnd = random.Random(11)
    span = SW + 60
    for i in range(count):
        w = rnd.randint(14, 22)
        y = SY0 + 8 + rnd.randint(0, 30)
        x = _wrap_x(rnd.randint(0, span), span * (1 + i % 2), phase, span, SX0 - 30)
        _cloud(p, x, y, w, base, shade)


def _rain(p: gfx.Page, phase: float, heavy: bool, slant: int):
    rnd = random.Random(5)
    n = 70 if heavy else 40
    for i in range(n):
        x0, y0 = rnd.randint(0, SW + 10), rnd.randint(0, SH)
        speed = 2 + i % 2
        y = int((y0 + phase * SH * speed) % SH)
        x = SX0 + int(x0 - slant * y / 6) % SW
        for k in range(3):
            p.px(x - (k * slant) // 3 if slant else x, SY0 + (y + k) % SH, T.CYAN if k else (190, 235, 255))


def _snow(p: gfx.Page, phase: float, heavy: bool):
    rnd = random.Random(9)
    for i in range(60 if heavy else 32):
        x0, y0 = rnd.randint(0, SW), rnd.randint(0, SH)
        y = int((y0 + phase * SH * (1 + i % 2)) % SH)
        wob = round(2 * math.sin(2 * math.pi * (phase * (1 + i % 2) + i / 7)))
        x = SX0 + (x0 + wob) % SW
        p.px(x, SY0 + y, T.WHITE)
        if i % 3 == 0:
            p.px(x + 1, SY0 + y, T.WHITE)
            p.px(x, SY0 + y + 1, T.WHITE)
            p.px(x + 1, SY0 + y + 1, (170, 190, 230))


def _fog(p: gfx.Page, phase: float):
    for b in range(4):
        y = SY0 + 40 + b * 10
        shift = int(phase * (24 + b * 8)) % 8
        for x in range(SX0, SX1 + 1):
            if ((x + shift * (1 if b % 2 else -1)) // 4 + b) % 3:
                for dy in (0, 1):
                    p.px(x, y + dy, (170, 180, 200))


def _wind(p: gfx.Page, phase: float, strength: float):
    rnd = random.Random(13)
    n = 5 if strength < 12 else 9
    for i in range(n):
        y = SY0 + 10 + rnd.randint(0, SH - 30)
        ln = rnd.randint(10, 22)
        x = _wrap_x(rnd.randint(0, SW + 30), (SW + 30) * 2, phase, SW + 30, SX0 - 20)
        p.line(x, y, x + ln, y, (200, 215, 240))


def _bolt(p: gfx.Page):
    cx = SX0 + 40
    pts = [(cx, SY0 + 22), (cx - 5, SY0 + 38), (cx + 1, SY0 + 38), (cx - 6, SY0 + 58)]
    for dx in (0, 1):
        p.polyline([(x + dx, y) for x, y in pts], T.AMBER)
    p.polyline([(x + 1, y) for x, y in pts], T.WHITE)


def draw_scene(p: gfx.Page, m: dict, phase: float):
    kind = gfx.kind_of(m["first"].get("code"), m["night"])
    night = m["night"]
    thunder = m["thunder"][0] == 2 and kind == "storm"
    flash = thunder and 0.50 <= phase < 0.56
    _sky(p, night, flash)
    cloudy = kind in ("cloud", "rain", "snow", "storm", "fog", "moon_cloud", "sun_cloud")
    if night and kind in ("moon", "moon_cloud", "sun_cloud"):
        _stars(p, phase, 1.0 if kind == "moon" else 0.5)
    elif night and not cloudy:
        _stars(p, phase, 1.0)
    # светило
    if kind in ("sun", "sun_cloud"):
        gfx.icon_sun(p, SX0 + 112, SY0 + 22, 9, phase=phase * 8)
    elif kind in ("moon", "moon_cloud") or (night and kind == "cloud"):
        gfx.draw_moon(p, SX0 + 112, SY0 + 22, 9, m["moon"][0])
    count = {"sun_cloud": 2, "moon_cloud": 2, "cloud": 4, "fog": 1}.get(kind, 4 if kind in ("rain", "snow", "storm") else 0)
    _clouds(p, phase, count, dark=kind in ("rain", "storm"), night=night)
    if kind == "fog":
        _fog(p, phase)
    _skyline(p, night, phase)
    if kind in ("rain", "storm"):
        _rain(p, phase, heavy=m["first"].get("code") in (65, 82, 95, 96, 99), slant=2 if m["wind_max"] >= 6 else 0)
    elif kind == "snow":
        _snow(p, phase, heavy=m["first"].get("code") in (75, 86))
    if m["wind_max"] >= 9 and kind not in ("fog",):
        _wind(p, phase, m["wind_max"])
    if flash:
        _bolt(p)
    # рамка окна сцены поверх
    p.d.rectangle([SX0 - 1, SY0 - 1, SX1 + 1, SY1 + 1], outline=T.BORDER)


def _signed(v: float) -> str:
    r = round(v)
    return f"{'+' if r > 0 else ''}{r}"


CONDITION_RU = {
    "sun": "ЯСНО", "moon": "ЯСНО", "sun_cloud": "МАЛООБЛАЧНО", "moon_cloud": "МАЛООБЛАЧНО", "cloud": "ПАСМУРНО",
    "rain": "ДОЖДЬ", "snow": "СНЕГ", "storm": "ГРОЗА", "fog": "ТУМАН",
}


def draw_card(m: dict, phrase: str, phase: float, typed: float, cursor: bool) -> gfx.Page:
    p = gfx.Page()
    # шапка
    p.rect(0, 0, T.LW - 1, 12, fill=T.PANEL)
    p.text(5, 3, "ADA//ПОГОДА", T.GREEN)
    st = m["start"]
    p.text(T.LW - 5, 3, f"{['ПН','ВТ','СР','ЧТ','ПТ','СБ','ВС'][st.weekday()]} {st:%d.%m} {st:%H:%M}", T.DIM, anchor="r")
    sp = gfx.Page()
    draw_scene(sp, m, phase)
    p.im.paste(sp.im.crop((SX0 - 1, SY0 - 1, SX1 + 2, SY1 + 2)), (SX0 - 1, SY0 - 1))      # обрезка по рамке окна
    # правая панель
    p.panel(160, 16, 315, 99, title="АСТАНА")
    f = m["first"]
    col = T.CYAN if f["temp"] < 0 else T.AMBER
    w = p.big_text(166, 30, _signed(f['temp']), col, k=4)
    p.rect(166 + w + 3, 31, 166 + w + 11, 39, fill=col)
    p.rect(166 + w + 6, 34, 166 + w + 8, 36, fill=T.PANEL)
    kind = gfx.kind_of(f.get("code"), m["night"])
    p.text(166, 66, f"ОЩУЩ. {_signed(f['feels'])}°", T.TEXT)
    p.text(166, 77, CONDITION_RU.get(kind, "ОБЛАЧНО"), T.TEXT)
    name, tcol = T.THREAT_LEVELS[m["threat"]]
    p.text(166, 88, name, tcol)
    for i in range(4):
        p.rect(236 + i * 18, 88, 236 + i * 18 + 14, 95, fill=(T.THREAT_LEVELS[i][1] if i <= m["threat"] else T.PANEL2))
    gfx.weather_icon(p, kind, 292, 42, phase * 8)
    # фраза Ады
    p.panel(4, 102, 315, 154, title="ADA>", title_color=T.GREEN)
    lines = gfx.wrap(phrase, PHRASE_WIDTH, 3)
    total = sum(len(x) for x in lines)
    left = int(round(total * typed))
    y = 115
    cur = (26, y)
    for li, line in enumerate(lines):
        shown = line[:left] if left > 0 else ""
        p.text(10, y, ">" if li == 0 else " ", T.GREEN)
        p.text(26, y, shown, T.TEXT)
        if left > 0 or li == 0:
            cur = (26 + len(shown) * 8, y)
        left -= len(line)
        y += 12
    if cursor:
        p.rect(cur[0], cur[1], cur[0] + 6, cur[1] + 7, fill=T.GREEN)
    # нижняя полоса
    p.rect(4, 158, 315, 175, fill=T.PANEL)
    p.text(10, 163, f"МИН {_signed(m['tmin'])}°  МАКС {_signed(m['tmax'])}°", T.AMBER)
    p.text(T.LW - 10, 163, f"ВЕТЕР {m['wind_max']:.0f}м/с", T.GREEN if m["wind_max"] < 9 else T.AMBER, anchor="r")
    return p


def render_gif(m: dict, phrase: str, scale: int = T.GIF_SCALE) -> bytes:
    frames = []
    for i in range(FRAMES):
        phase = i / FRAMES
        typed = min(1.0, (i + 1) / TYPE_FRAMES)
        cursor = (i // 4) % 2 == 0
        frames.append(draw_card(m, phrase, phase, typed, cursor).scaled(scale))
    # единая палитра по нескольким кадрам, без дизеринга — цвета не «плывут»
    sample = Image.new("RGB", (frames[0].width, frames[0].height * 4))
    for k, idx in enumerate((0, FRAMES // 4, FRAMES // 2, FRAMES - 1)):
        sample.paste(frames[idx], (0, k * frames[0].height))
    pal = sample.quantize(colors=96, method=Image.MEDIANCUT, dither=Image.NONE)
    out = [fr.quantize(palette=pal, dither=Image.NONE) for fr in frames]
    durations = [70 if i < TYPE_FRAMES else 110 for i in range(FRAMES)]
    durations[-1] = 1800
    buf = io.BytesIO()
    out[0].save(buf, format="GIF", save_all=True, append_images=out[1:], duration=durations, loop=0,
                disposal=1, optimize=False)
    return buf.getvalue()

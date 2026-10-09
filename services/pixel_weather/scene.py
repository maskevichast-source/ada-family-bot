"""Анимированная карточка: небо с погодой, силуэт Астаны (Байтерек), температура, фраза Ады «печатается»."""
import io
import math
import random

from PIL import Image

from services.pixel_weather import gfx, theme as T
from services.pixel_weather.model import aqi_label

FRAMES = 64
TYPE_FRAMES = 26
SX0, SY0, SX1, SY1 = 0, 0, 159, 89           # сцена занимает весь холст 160×90
SW, SH = SX1 - SX0 + 1, SY1 - SY0 + 1
PHRASE_WIDTH = 36
SKYLINE_SEED = 7


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _wrap_x(x0, travel, phase, span, lo):
    """Периодический дрейф: за цикл ровно целое число оборотов — склейка петли незаметна."""
    return lo + int((x0 + phase * travel) % span)


def _sky(p: gfx.Page, night: bool, flash: bool):
    top, hor = ((6, 8, 12), (22, 26, 36)) if night else ((34, 46, 60), (88, 102, 116))
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
    col = (12, 14, 18) if night else (26, 32, 40)
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
    pole = (18, 20, 26) if night else (38, 46, 58)
    p.rect(cx - 1, base - 34, cx, base, fill=pole)
    p.rect(cx - 4, base - 6, cx + 3, base, fill=pole)
    p.disc(cx, base - 40, 6, pole)
    p.disc(cx, base - 40, 3, T.AMBER if night else (190, 150, 70))
    p.rect(cx - 1, base - 52, cx, base - 46, fill=pole)
    p.px(cx - 1, base - 53, T.AMBER)
    p.rect(SX0, base - 5, SX1, base, fill=(8, 8, 10) if night else (20, 24, 30))


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
    _season(p, m, kind, night, phase)
    if kind in ("rain", "storm"):
        _rain(p, phase, heavy=m["first"].get("code") in (65, 82, 95, 96, 99), slant=2 if m["wind_max"] >= 6 else 0)
    elif kind == "snow":
        _snow(p, phase, heavy=m["first"].get("code") in (75, 86))
    if m["wind_max"] >= 9 and kind not in ("fog",):
        _wind(p, phase, m["wind_max"])
    if flash:
        _bolt(p)


def _signed(v: float) -> str:
    r = round(v)
    return f"{'+' if r > 0 else ''}{r}"


CONDITION_RU = {
    "sun": "ЯСНО", "moon": "ЯСНО", "sun_cloud": "МАЛООБЛАЧНО", "moon_cloud": "МАЛООБЛАЧНО", "cloud": "ПАСМУРНО",
    "rain": "ДОЖДЬ", "snow": "СНЕГ", "storm": "ГРОЗА", "fog": "ТУМАН",
}


def season_of(month: int) -> str:
    return "winter" if month in (12, 1, 2) else "spring" if month in (3, 4, 5) else "summer" if month in (6, 7, 8) else "autumn"


def _tree(p: gfx.Page, x: int, base: int, season: str, night: bool):
    trunk = (70, 48, 28) if not night else (36, 26, 16)
    p.rect(x, base - 9, x + 1, base, fill=trunk)
    crown = {"autumn": (214, 128, 24), "summer": (50, 130, 60), "spring": (226, 170, 190), "winter": None}[season]
    if night and crown:
        crown = tuple(int(c * 0.55) for c in crown)
    if crown:
        p.disc(x, base - 13, 6, crown)
        p.disc(x - 3, base - 10, 4, crown)
        p.disc(x + 4, base - 10, 4, crown)
        light = tuple(min(255, int(c * 1.25)) for c in crown)
        for dx, dy in ((-2, -15), (2, -13), (0, -11), (4, -9), (-4, -9)):
            p.px(x + dx, base + dy, light)
    else:
        for dx, dy in ((-3, -12), (3, -12), (-5, -9), (5, -9), (0, -14)):
            p.line(x, base - 8, x + dx, base + dy, trunk)
            p.px(x + dx, base + dy - 1, T.WHITE)


def _season(p: gfx.Page, m: dict, kind: str, night: bool, phase: float):
    season = season_of(m["start"].month)
    base = SY1 - 5
    for x in (14, 48, 112, 146):
        _tree(p, x, base, season, night)
    if season == "autumn":
        for x in (30, 128):                                           # тыквы
            p.rect(x, base - 3, x + 4, base, fill=(210, 100, 20))
            p.px(x + 2, base - 4, (60, 120, 40))
    elif season == "winter":
        for x in (30,):                                               # снеговик
            p.disc(x, base - 2, 3, T.WHITE)
            p.disc(x, base - 6, 2, T.WHITE)
            p.px(x + 1, base - 6, T.ORANGE)
    elif season in ("spring", "summer"):
        for i, x in enumerate((24, 36, 124, 136)):
            p.px(x, base, (60, 150, 70))
            p.px(x, base - 1, (60, 150, 70))
            p.px(x, base - 2, (255, 200, 60) if season == "summer" else (240, 150, 190))
    if season == "autumn" and kind not in ("rain", "storm", "snow"):    # падающие листья
        rnd = random.Random(21)
        for i in range(14):
            x0, y0 = rnd.randint(0, SW), rnd.randint(0, SH - 12)
            y = int((y0 + phase * (SH - 12)) % (SH - 12))
            x = (x0 + round(3 * math.sin(2 * math.pi * (phase + i / 5)))) % SW
            p.px(x, y, (214, 128, 24) if i % 2 else (170, 80, 20))
    # прохожий (с зонтом, если дождь)
    wx = int(phase * (SW + 24)) - 12
    leg = int(phase * FRAMES / 2) % 2
    body = (150, 160, 175) if not night else (90, 98, 110)
    p.rect(wx, base - 7, wx + 2, base - 3, fill=body)
    p.px(wx + 1, base - 9, body)
    p.px(wx + 1, base - 8, body)
    p.px(wx + (0 if leg else 2), base - 2, body)
    p.px(wx + (2 if leg else 0), base - 2, body)
    p.px(wx + (0 if leg else 2), base - 1, body)
    p.px(wx + (2 if leg else 0), base - 1, body)
    if kind in ("rain", "storm", "snow"):
        p.line(wx - 2, base - 10, wx + 4, base - 10, T.AMBER)
        p.line(wx - 1, base - 11, wx + 3, base - 11, T.AMBER)
        p.px(wx + 1, base - 12, T.AMBER)


def scene_image(m: dict, phase: float = 0.35) -> Image.Image:
    p = gfx.Page(size=(SW, SH))
    draw_scene(p, m, phase)
    return p.im


def _overlay(img: Image.Image, text: str, shown: float, cursor: bool, tag: str, foot: str) -> Image.Image:
    from PIL import ImageDraw
    from services.pixel_weather import ui
    d = ImageDraw.Draw(img, "RGBA")
    W, H = img.size
    f_s, f_t = ui.font_px(15, 600), ui.font_px(25, 500)
    d.rectangle([0, 0, W, 44], fill=(10, 10, 10, 205))
    d.text((18, 11), "НА УЛИЦЕ", font=ui.font_px(17, 600), fill=ui.WHITE)
    d.ellipse([172, 17, 184, 29], fill=ui.GREEN)
    d.text((192, 13), "LIVE", font=f_s, fill=ui.GREEN)
    d.text((W - 18, 13), tag, font=f_s, fill=ui.AMBER, anchor="ra")
    d.rectangle([0, H - 112, W, H], fill=(10, 10, 10, 225))
    lines = gfx.wrap(text, 38, 2)
    total = sum(len(x) for x in lines)
    left = int(round(total * shown))
    y = H - 98
    cur = (18, y)
    for line in lines:
        part = line[:left] if left > 0 else ""
        d.text((18, y), part, font=f_t, fill=ui.WHITE)
        cur = (18 + d.textlength(part, font=f_t), y)
        left -= len(line)
        y += 34
    if cursor:
        d.rectangle([cur[0] + 2, cur[1] + 3, cur[0] + 14, cur[1] + 26], fill=ui.AMBER)
    d.text((18, H - 34), foot, font=ui.font_px(17, 500), fill=ui.DIM)
    return img


def render_gif(m: dict, text: str, foot: str = "", tag: str = "", scale: int = 4) -> bytes:
    frames = []
    for i in range(FRAMES):
        phase = i / FRAMES
        img = scene_image(m, phase).resize((SW * scale, SH * scale), Image.NEAREST)
        shown = min(1.0, (i + 1) / TYPE_FRAMES)
        frames.append(_overlay(img, text, shown, (i // 4) % 2 == 0, tag, foot))
    sample = Image.new("RGB", (frames[0].width, frames[0].height * 4))
    for k, idx in enumerate((0, FRAMES // 4, FRAMES // 2, FRAMES - 1)):
        sample.paste(frames[idx], (0, k * frames[0].height))
    pal = sample.quantize(colors=128, method=Image.MEDIANCUT, dither=Image.NONE)
    out = [fr.quantize(palette=pal, dither=Image.NONE) for fr in frames]
    durations = [70 if i < TYPE_FRAMES else 110 for i in range(FRAMES)]
    durations[-1] = 1800
    buf = io.BytesIO()
    out[0].save(buf, format="GIF", save_all=True, append_images=out[1:], duration=durations, loop=0,
                disposal=1, optimize=False)
    return buf.getvalue()

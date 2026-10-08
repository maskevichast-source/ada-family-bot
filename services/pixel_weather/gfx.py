"""Рисование на низкоразрешающем холсте: текст шрифтом 8 px без сглаживания, пиксельные линии,
процедурные иконки, фаза луны. Итог увеличивается целым множителем (NEAREST) — пиксели остаются квадратными."""
import math

from PIL import Image, ImageDraw

from services.pixel_weather import theme as T


class Page:
    def __init__(self, bg=T.BG, size=(T.LW, T.LH)):
        self.im = Image.new("RGB", size, bg)
        self.d = ImageDraw.Draw(self.im)
        self.d.fontmode = "1"

    # ── примитивы ──
    def rect(self, x0, y0, x1, y1, fill=None, outline=None):
        self.d.rectangle([x0, y0, x1, y1], fill=fill, outline=outline)

    def panel(self, x0, y0, x1, y1, fill=T.PANEL, border=T.BORDER, title=None, title_color=T.DIM):
        """Рамка в 1 px со срезанными углами, как «окно» терминала."""
        self.rect(x0, y0, x1, y1, fill=fill)
        self.d.line([(x0 + 1, y0), (x1 - 1, y0)], fill=border)
        self.d.line([(x0 + 1, y1), (x1 - 1, y1)], fill=border)
        self.d.line([(x0, y0 + 1), (x0, y1 - 1)], fill=border)
        self.d.line([(x1, y0 + 1), (x1, y1 - 1)], fill=border)
        if title:
            self.text(x0 + 4, y0 + 3, title, title_color)

    def px(self, x, y, color):
        if 0 <= x < self.im.width and 0 <= y < self.im.height:
            self.im.putpixel((int(x), int(y)), color)

    def line(self, x0, y0, x1, y1, color, dashed=False):
        """Линия Брезенхэма в 1 пиксель (опционально пунктир 2/2)."""
        x0, y0, x1, y1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err, n = dx + dy, 0
        while True:
            if not dashed or (n // 2) % 2 == 0:
                self.px(x0, y0, color)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy
            n += 1

    def polyline(self, pts, color, dashed=False):
        for a, b in zip(pts, pts[1:]):
            self.line(a[0], a[1], b[0], b[1], color, dashed)

    def disc(self, cx, cy, r, color):
        for y in range(int(cy - r), int(cy + r) + 1):
            for x in range(int(cx - r), int(cx + r) + 1):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r + 0.5:
                    self.px(x, y, color)

    def dither(self, x0, y0, x1, y1, c1, c2, level):
        """Заливка байеровским 4×4 дизерингом: level 0..1 — доля c2."""
        bayer = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                self.px(x, y, c2 if (bayer[y % 4][x % 4] + 0.5) / 16 < level else c1)

    # ── текст ──
    def text(self, x, y, s, color=T.TEXT, anchor="l"):
        w = text_w(s)
        if anchor == "r":
            x -= w
        elif anchor == "m":
            x -= w // 2
        self.d.text((x, y), s, font=T.font(8), fill=color)
        return w

    def big_text(self, x, y, s, color=T.TEXT, k=3, anchor="l"):
        """Крупные пиксельные буквы: рисуем 8-px текст и увеличиваем в k раз (остаются квадратными)."""
        w = text_w(s)
        tmp = Image.new("L", (max(1, w), 8), 0)
        td = ImageDraw.Draw(tmp)
        td.fontmode = "1"
        td.text((0, 0), s, font=T.font(8), fill=255)
        tmp = tmp.resize((tmp.width * k, 8 * k), Image.NEAREST)
        if anchor == "r":
            x -= tmp.width
        elif anchor == "m":
            x -= tmp.width // 2
        self.im.paste(Image.new("RGB", tmp.size, color), (int(x), int(y)), tmp)
        return tmp.width

    def bar(self, x, y, w, h, frac, color, back=T.PANEL2):
        self.rect(x, y, x + w - 1, y + h - 1, fill=back)
        fw = int(round(w * max(0.0, min(1.0, frac))))
        if fw > 0:
            self.rect(x, y, x + fw - 1, y + h - 1, fill=color)

    def scaled(self, k: int) -> Image.Image:
        return self.im.resize((self.im.width * k, self.im.height * k), Image.NEAREST)


def text_w(s: str) -> int:
    return len(s) * 8


def wrap(s: str, width: int, max_lines: int = 3) -> list[str]:
    """Перенос по словам на width символов; лишнее обрезаем с «…»."""
    lines, cur = [], ""
    for word in s.split():
        while len(word) > width:                        # очень длинное слово
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:width])
            word = word[width:]
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= width:
            cur += " " + word
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:max(0, width - 1)].rstrip() + "…"
    return lines


# ── иконки (центр cx, cy; «размер» ~ 16 px) ──
def icon_sun(p: Page, cx, cy, r=5, color=T.AMBER, rays=True, phase=0):
    p.disc(cx, cy, r, color)
    if rays:
        n = 8
        for i in range(n):
            a = (i / n + phase / n) * 2 * math.pi
            for k in (r + 2, r + 3, r + 4):
                p.px(round(cx + math.cos(a) * k), round(cy + math.sin(a) * k), color)


def icon_cloud(p: Page, cx, cy, w=9, color=T.WHITE, shade=None):
    p.disc(cx - w * 0.45, cy + 1, w * 0.4, color)
    p.disc(cx + w * 0.05, cy - w * 0.2, w * 0.5, color)
    p.disc(cx + w * 0.5, cy + 1, w * 0.38, color)
    p.rect(int(cx - w * 0.45), int(cy + 1), int(cx + w * 0.5), int(cy + w * 0.38), fill=color)
    if shade:
        p.line(cx - w * 0.6, cy + w * 0.4, cx + w * 0.8, cy + w * 0.4, shade)


def draw_moon(p: Page, cx, cy, r, phase: float):
    """Фаза луны: тёмный диск + освещённая часть (phase 0 новолуние, 0.5 полнолуние)."""
    dark, light = (62, 72, 120), (235, 232, 205)
    k = math.cos(2 * math.pi * phase)                   # 1 → новолуние, -1 → полнолуние
    right = phase < 0.5
    for y in range(int(cy - r), int(cy + r) + 1):
        for x in range(int(cx - r), int(cx + r) + 1):
            dx, dy = x - cx, y - cy
            if dx * dx + dy * dy > r * r + 0.5:
                continue
            half = math.sqrt(max(0.0, r * r - dy * dy))
            edge = k * half                               # граница терминатора
            lit = (dx >= edge) if right else (dx <= -edge)
            p.px(x, y, light if lit else dark)


_ICON_GROUPS = {
    "storm": {95, 96, 99},
    "snow": {71, 73, 75, 77, 85, 86},
    "rain": {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82},
    "fog": {45, 48},
}


def kind_of(code, night=False) -> str:
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "cloud"
    for name, codes in _ICON_GROUPS.items():
        if code in codes:
            return name
    if code == 0:
        return "moon" if night else "sun"
    if code in (1, 2):
        return "moon_cloud" if night else "sun_cloud"
    return "cloud"


def weather_icon(p: Page, kind: str, cx, cy, phase=0):
    """Маленькая иконка 16×16 по виду погоды."""
    if kind == "sun":
        icon_sun(p, cx, cy, 4, phase=phase)
    elif kind == "moon":
        draw_moon(p, cx, cy, 5, 0.3)
    elif kind == "sun_cloud":
        icon_sun(p, cx - 3, cy - 3, 3, rays=False)
        icon_cloud(p, cx + 1, cy + 2, 8)
    elif kind == "moon_cloud":
        draw_moon(p, cx - 3, cy - 3, 4, 0.3)
        icon_cloud(p, cx + 1, cy + 2, 8, color=(170, 180, 210))
    elif kind in ("rain", "snow", "storm"):
        icon_cloud(p, cx, cy - 3, 8, color=(160, 172, 205) if kind != "snow" else T.WHITE)
        if kind == "rain":
            for dx in (-4, 0, 4):
                p.line(cx + dx, cy + 3, cx + dx - 1, cy + 6, T.CYAN)
        elif kind == "snow":
            for dx in (-4, 0, 4):
                p.px(cx + dx, cy + 4, T.WHITE)
                p.px(cx + dx + 1, cy + 6, T.WHITE)
        else:
            for pt in [(1, 2), (-1, 4), (1, 4), (0, 6), (-1, 7)]:
                p.px(cx + pt[0], cy + pt[1], T.AMBER)
    elif kind == "fog":
        icon_cloud(p, cx, cy - 3, 8, color=(150, 160, 185))
        for i, dy in enumerate((3, 5, 7)):
            p.line(cx - 6 + (i % 2) * 2, cy + dy, cx + 6 - (i % 2) * 2, cy + dy, (150, 160, 185))
    else:
        icon_cloud(p, cx, cy, 9, color=(165, 175, 205))

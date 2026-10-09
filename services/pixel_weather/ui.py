"""Холст в стиле RAD: тёмный фон, янтарный акцент, моноширинный шрифт с разрядкой.
Рисуем в 2× и сжимаем — линии и текст получаются гладкими."""
import io
import math
import os
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from services.pixel_weather.theme import FONT_DIR

S = 2
BG = (10, 10, 10)
CARD = (20, 20, 20)
CARD2 = (28, 28, 28)
BORDER = (56, 56, 56)
AMBER = (255, 168, 0)
AMBER_DIM = (120, 82, 14)
CYAN = (24, 196, 245)
CYAN_DIM = (16, 70, 90)
GREEN = (72, 222, 110)
GREEN_BG = (18, 52, 30)
RED = (244, 67, 67)
WHITE = (238, 238, 238)
DIM = (150, 150, 150)
FAINT = (86, 86, 86)
PINK = (240, 140, 190)


@lru_cache(maxsize=64)
def font(size: int, weight: int = 400) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(os.path.join(FONT_DIR, "JetBrainsMono-VF.ttf"), int(size * S))
    try:
        f.set_variation_by_axes([weight])
    except Exception:
        pass
    return f


def font_px(px: int, weight: int = 400) -> ImageFont.FreeTypeFont:
    """Шрифт точно в px пикселей (для рисования прямо на финальных картинках, без 2×)."""
    f = ImageFont.truetype(os.path.join(FONT_DIR, "JetBrainsMono-VF.ttf"), int(px))
    try:
        f.set_variation_by_axes([weight])
    except Exception:
        pass
    return f


class Canvas:
    def __init__(self, w: int = 1080, h: int = 1350):
        self.w, self.h = w, h
        self.im = Image.new("RGB", (w * S, h * S), BG)
        self.d = ImageDraw.Draw(self.im)

    # ── текст ──
    def tw(self, s: str, size: int, weight: int = 400, track: float = 0) -> float:
        f = font(size, weight)
        return sum(f.getlength(c) / S + track for c in s) - (track if s else 0)

    def text(self, x, y, s, size=26, color=WHITE, weight=400, track=0.0, anchor="l") -> float:
        w = self.tw(s, size, weight, track)
        if anchor == "r":
            x -= w
        elif anchor == "m":
            x -= w / 2
        f = font(size, weight)
        if track == 0:
            self.d.text((x * S, y * S), s, font=f, fill=color)
        else:
            cx = x
            for c in s:
                self.d.text((cx * S, y * S), c, font=f, fill=color)
                cx += f.getlength(c) / S + track
        return w

    def caps(self, x, y, s, size=22, color=DIM, weight=500, anchor="l") -> float:
        return self.text(x, y, s.upper(), size, color, weight, track=size * 0.14, anchor=anchor)

    def wrap(self, x, y, s, size, color, width, weight=400, line=1.35, max_lines=4, track=0.0) -> int:
        """Перенос по ширине width (px); возвращает y после текста."""
        lines, cur = [], ""
        for word in s.split():
            test = (cur + " " + word).strip()
            if self.tw(test, size, weight, track) <= width or not cur:
                cur = test
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = lines[-1].rstrip(" .,") + "…"
        for ln in lines:
            self.text(x, y, ln, size, color, weight, track)
            y += size * line
        return y

    # ── фигуры ──
    def rect(self, x0, y0, x1, y1, fill=None, outline=None, r=0, width=2):
        box = [x0 * S, y0 * S, x1 * S, y1 * S]
        if r:
            self.d.rounded_rectangle(box, r * S, fill=fill, outline=outline, width=width * S // 2 if outline else 0)
        else:
            self.d.rectangle(box, fill=fill, outline=outline, width=width * S // 2 if outline else 0)

    def card(self, x0, y0, x1, y1, fill=CARD, border=BORDER):
        self.rect(x0, y0, x1, y1, fill=fill, outline=border, r=10, width=2)

    def line(self, pts, color, width=3, dashed=False, dash=(10, 8)):
        pts = [(x * S, y * S) for x, y in pts]
        if not dashed:
            self.d.line(pts, fill=color, width=int(width * S), joint="curve")
            return
        on, off = dash[0] * S, dash[1] * S
        carry, draw = 0.0, True
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            seg = math.hypot(x1 - x0, y1 - y0)
            pos = 0.0
            while pos < seg:
                step = min((on if draw else off) - carry, seg - pos)
                if draw:
                    a, b = pos / seg, (pos + step) / seg
                    self.d.line([(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a), (x0 + (x1 - x0) * b, y0 + (y1 - y0) * b)],
                                fill=color, width=int(width * S))
                pos += step
                carry += step
                if carry >= (on if draw else off) - 1e-6:
                    draw, carry = not draw, 0.0

    def dot(self, x, y, r, fill, outline=None):
        self.d.ellipse([(x - r) * S, (y - r) * S, (x + r) * S, (y + r) * S], fill=fill, outline=outline)

    def fill_under(self, pts, base_y, top_color, bottom_color):
        """Заливка под линией градиентом (как под графиком температуры в RAD)."""
        xs = [p[0] for p in pts]
        x0, x1 = int(min(xs) * S), int(max(xs) * S)
        top = int(min(p[1] for p in pts) * S)
        bot = int(base_y * S)
        mask = Image.new("L", (self.im.width, self.im.height), 0)
        ImageDraw.Draw(mask).polygon([(x * S, y * S) for x, y in pts] + [(x1, bot), (x0, bot)], fill=255)
        grad = Image.new("RGB", (self.im.width, self.im.height), bottom_color)
        gd = ImageDraw.Draw(grad)
        for y in range(top, bot):
            t = (y - top) / max(1, bot - top)
            gd.line([(x0, y), (x1, y)], fill=tuple(int(top_color[i] + (bottom_color[i] - top_color[i]) * t) for i in range(3)))
        self.im.paste(grad, (0, 0), mask)
        self.d = ImageDraw.Draw(self.im)

    def paste(self, img: Image.Image, x, y):
        """Вставить готовую картинку (в финальных пикселях)."""
        img = img.resize((img.width * S, img.height * S), Image.NEAREST) if img.width < 400 else img.resize((img.width * S, img.height * S), Image.LANCZOS)
        self.im.paste(img, (int(x * S), int(y * S)))
        self.d = ImageDraw.Draw(self.im)

    def badge(self, x, y, s, fg=GREEN, bg=GREEN_BG, size=24, anchor="l") -> float:
        w = self.tw(s, size, 700, size * 0.1) + 34
        if anchor == "r":
            x -= w
        self.rect(x, y, x + w, y + size + 20, fill=bg, r=8)
        self.dot(x + 16, y + (size + 20) / 2, 6, fg)
        self.text(x + 30, y + 9, s, size, fg, 700, size * 0.1)
        return w

    def finish(self) -> bytes:
        out = self.im.resize((self.w, self.h), Image.LANCZOS)
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


def header(cv: Canvas, title: str = "АСТАНА", right: str = "LIVE") -> int:
    cv.text(40, 34, title, 50, WHITE, 700, track=8)
    cv.rect(40, 100, 1040, 102, fill=AMBER_DIM)                    # янтарная пунктирная линия под заголовком
    for x in range(40, 1040, 14):
        cv.rect(x, 100, x + 6, 102, fill=BG)
    cv.dot(48, 128, 7, GREEN)
    cv.text(66, 114, right, 24, DIM, 500, track=3)
    return 150

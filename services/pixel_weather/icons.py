"""Иконки погоды для страниц RAD (рисуются в 2× с прозрачным фоном)."""
import math

from PIL import Image, ImageDraw

from services.pixel_weather import ui
from services.pixel_weather.ui import S


def _layer(size):
    return Image.new("RGBA", (size * S, size * S), (0, 0, 0, 0))


def _cloud(d, cx, cy, w, color):
    d.ellipse([cx - w * 0.55, cy - w * 0.05, cx - w * 0.05, cy + w * 0.35], fill=color)
    d.ellipse([cx - w * 0.3, cy - w * 0.45, cx + w * 0.25, cy + w * 0.15], fill=color)
    d.ellipse([cx + w * 0.0, cy - w * 0.2, cx + w * 0.55, cy + w * 0.35], fill=color)
    d.rectangle([cx - w * 0.32, cy + w * 0.05, cx + w * 0.32, cy + w * 0.35], fill=color)


def _sun(d, cx, cy, r, color, rays=True, width=3):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    if rays:
        for i in range(8):
            a = i * math.pi / 4
            d.line([cx + math.cos(a) * r * 1.45, cy + math.sin(a) * r * 1.45,
                    cx + math.cos(a) * r * 1.85, cy + math.sin(a) * r * 1.85], fill=color, width=int(width * S))


def draw_icon(cv: ui.Canvas, kind: str, cx: float, cy: float, size: int = 64, mono: str | None = None) -> None:
    """kind: sun, moon, sun_cloud, moon_cloud, cloud, rain, snow, storm, fog. mono: один цвет (иначе янтарь/голубой/белый)."""
    lay = _layer(size)
    d = ImageDraw.Draw(lay)
    u = size * S
    c = u / 2
    amber = mono or ui.AMBER
    blue = mono or ui.CYAN
    white = mono or ui.WHITE
    grey = mono or (190, 190, 190)
    lw = max(2, size // 20)
    if kind == "sun":
        _sun(d, c, c, u * 0.19, amber, width=lw)
    elif kind == "moon":
        d.ellipse([c - u * 0.28, c - u * 0.28, c + u * 0.28, c + u * 0.28], fill=white)
        d.ellipse([c - u * 0.12, c - u * 0.36, c + u * 0.44, c + u * 0.2], fill=(0, 0, 0, 0))
    elif kind in ("sun_cloud", "moon_cloud"):
        if kind == "sun_cloud":
            _sun(d, c + u * 0.2, c - u * 0.17, u * 0.13, amber, width=lw)
        else:
            d.ellipse([c + u * 0.05, c - u * 0.4, c + u * 0.4, c - u * 0.05], fill=white)
            d.ellipse([c + u * 0.18, c - u * 0.46, c + u * 0.52, c - u * 0.1], fill=(0, 0, 0, 0))
        _cloud(d, c - u * 0.05, c + u * 0.1, u * 0.7, white)
    elif kind in ("cloud", "fog"):
        _cloud(d, c, c - (u * 0.06 if kind == "fog" else 0), u * 0.78, grey)
        if kind == "fog":
            for i, dy in enumerate((0.3, 0.42, 0.54)):
                d.line([c - u * 0.3 + i * u * 0.05, c + u * dy, c + u * 0.3 - i * u * 0.05, c + u * dy], fill=white, width=int(lw * S))
    elif kind in ("rain", "snow", "storm"):
        _cloud(d, c, c - u * 0.14, u * 0.78, amber if (mono is None and kind == "rain") else white if kind == "snow" else grey)
        if kind == "rain":
            for dx in (-0.22, 0.0, 0.22):
                d.line([c + u * dx + u * 0.05, c + u * 0.22, c + u * dx - u * 0.05, c + u * 0.42], fill=blue, width=int(lw * S))
        elif kind == "snow":
            for dx, dy in ((-0.2, 0.28), (0.02, 0.4), (0.22, 0.28)):
                r = u * 0.035
                d.ellipse([c + u * dx - r, c + u * dy - r, c + u * dx + r, c + u * dy + r], fill=blue)
        else:
            bolt = [(c + u * 0.04, c + u * 0.12), (c - u * 0.1, c + u * 0.34), (c + u * 0.0, c + u * 0.34),
                    (c - u * 0.06, c + u * 0.52), (c + u * 0.14, c + u * 0.26), (c + u * 0.03, c + u * 0.26)]
            d.polygon(bolt, fill=amber)
    cv.im.paste(lay, (int((cx - size / 2) * S), int((cy - size / 2) * S)), lay)
    cv.d = ImageDraw.Draw(cv.im)


def kind_of(code, night=False) -> str:
    from services.pixel_weather import gfx
    return gfx.kind_of(code, night)

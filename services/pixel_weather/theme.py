"""Цвета, размеры и шрифты пиксельной погоды."""
import os
from functools import lru_cache

from PIL import ImageFont

LW, LH = 320, 180                  # логический холст (пиксели «арта»)
PAGE_SCALE = 3                     # страницы: 960×540
GIF_SCALE = 2                      # анимация: 640×360

BG = (11, 16, 32)
PANEL = (18, 26, 51)
PANEL2 = (24, 34, 66)
BORDER = (42, 58, 107)
TEXT = (215, 227, 255)
DIM = (107, 122, 168)
GREEN = (92, 255, 157)
AMBER = (255, 200, 87)
ORANGE = (255, 140, 66)
RED = (255, 92, 108)
CYAN = (77, 216, 255)
VIOLET = (180, 140, 255)
SKY_DAY = (30, 60, 110)
SKY_NIGHT = (8, 10, 30)
WHITE = (240, 246, 255)

FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "assets", "fonts")


@lru_cache(maxsize=4)
def font(size: int = 8) -> ImageFont.FreeTypeFont:
    path = os.path.join(FONT_DIR, "PressStart2P-Regular.ttf")
    return ImageFont.truetype(path, size)


THREAT_LEVELS = [
    ("СПОКОЙНО", GREEN),
    ("ВНИМАНИЕ", AMBER),
    ("ОПАСНО", ORANGE),
    ("ЭКСТРЕМ", RED),
]

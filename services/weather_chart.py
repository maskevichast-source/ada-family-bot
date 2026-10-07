"""Прогноз погоды картинкой: два графика по 12 часов (температура, «ощущается», осадки, ветер, иконки).

Данные берутся из тех же ответов Open-Meteo, что и у текстового прогноза (services/weather.py),
консенсус трёх моделей — медиана, как и в тексте. Рисует matplotlib (как графики расходов).
"""
import datetime
import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from services.plot_lock import serialized_plot

BG = "#14152b"
PANEL = "#1f2142"
NIGHT = "#0c0d24"
TEXT = "#ecf0f1"
MUTED = "#8f98b8"
TEMP_COLOR = "#D17C1E"
FEELS_COLOR = "#5A94EC"
RAIN_COLOR = "#8E9BC4"

WINDOW_HOURS = 12
MONTHS_RU_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
                 "сентября", "октября", "ноября", "декабря"]
WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

_SNOW = {71, 73, 75, 77, 85, 86}
_RAIN = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}
_STORM = {95, 96, 99}
_FOG = {45, 48}


def _icon(code, hour: int):
    """(символ, цвет) для кода погоды WMO. Символы есть в шрифте DejaVu (цветных эмодзи на сервере нет)."""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return "☁", MUTED
    night = hour >= 21 or hour < 6
    if c in _STORM:
        return "⚡", "#FFD93D"
    if c in _SNOW:
        return "❄", "#CFEFFF"
    if c in _RAIN:
        return "☂", FEELS_COLOR
    if c in _FOG:
        return "≡", MUTED
    if c in (0, 1):
        return ("☾", "#F5E6A8") if night else ("☀", "#FFD93D")
    if c == 2:
        return ("☁", "#C9CEA0") if night else ("☁", "#E6D98C")
    return "☁", "#B0BAC4"


def _fmt(value) -> str:
    rounded = round(value)
    return "0°" if rounded == 0 else f"{rounded}°".replace("-", "−")


def build_points(data: dict, start: datetime.datetime, hours: int = WINDOW_HOURS * 2) -> list[dict]:
    """Почасовые точки от start на hours часов вперёд (включая обе границы). Пустой список, если данных нет."""
    from services import weather as w
    hourly = (data or {}).get("hourly") or {}
    times = hourly.get("time") or []
    key = start.strftime("%Y-%m-%dT%H:00")
    if key not in times:
        return []
    first = times.index(key)
    points = []
    for i in range(first, min(first + hours + 1, len(times))):
        temps = w._model_values_at(hourly, "temperature_2m", i)
        if not temps:
            continue
        feels_values = w._model_values_at(hourly, "apparent_temperature", i)
        rains = w._model_values_at(hourly, "precipitation_probability", i)
        winds = w._model_values_at(hourly, "wind_speed_10m", i)
        temp = w._consensus(temps)
        points.append({
            "dt": datetime.datetime.strptime(times[i], "%Y-%m-%dT%H:%M"),
            "temp": temp,
            "feels": w._consensus(feels_values) if feels_values else temp,
            "rain": w._consensus(rains) if rains else 0.0,
            "wind": w._consensus(winds) if winds else 0.0,
            "code": w._consensus_code(w._model_codes_at(hourly, i)),
            "spread": w._spread(temps),
        })
    return points


def split_windows(points: list[dict]) -> list[list[dict]]:
    """Два окна по 12 часов; общая точка на границе входит в оба. Окно из одной точки не рисуем."""
    windows = [points[:WINDOW_HOURS + 1], points[WINDOW_HOURS:WINDOW_HOURS * 2 + 1]]
    return [win for win in windows if len(win) >= 2]


_DAY_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def window_label(win: list[dict]) -> str:
    """«09:00 → 21:00 · вт» или «21:00 → 09:00 · вт → ср», если окно переходит через полночь."""
    a, b = win[0]["dt"], win[-1]["dt"]
    days = _DAY_SHORT[a.weekday()] if a.date() == b.date() else f"{_DAY_SHORT[a.weekday()]} → {_DAY_SHORT[b.weekday()]}"
    return f"{a:%H:%M} → {b:%H:%M} · {days}"


def title_for(start: datetime.datetime) -> str:
    return f"Астана · {WEEKDAYS_RU[start.weekday()]}, {start.day} {MONTHS_RU_GEN[start.month - 1]}"


def _precip_hours(points: list[dict], codes: set[int]) -> list[datetime.datetime]:
    return [p["dt"] for p in points if p["code"] is not None and int(p["code"]) in codes]


def _span(hours: list[datetime.datetime]) -> str:
    first, last = min(hours), max(hours)
    return f"около {first:%H:%M}" if first == last else f"с {first:%H:%M} до {last:%H:%M}"


def umbrella_text(points: list[dict]) -> tuple[str, str] | None:
    """(текст про зонт, цвет), только если зонт или непромокаемая обувь реально нужны; иначе None."""
    rain_hours = _precip_hours(points, _RAIN | _STORM)
    snow_hours = _precip_hours(points, _SNOW)
    max_rain = max((p["rain"] for p in points), default=0)
    if rain_hours and snow_hours:
        return (f"Зонт пригодится: дождь {_span(rain_hours)} и снег {_span(snow_hours)} (осадки до {max_rain:.0f}%). "
                "Обувь непромокаемая, куртка с капюшоном."), FEELS_COLOR
    if rain_hours:
        return f"Зонт нужен: дождь {_span(rain_hours)}, вероятность осадков до {max_rain:.0f}%.", FEELS_COLOR
    if snow_hours:
        return (f"Ожидается снег {_span(snow_hours)} (до {max_rain:.0f}%). "
                "Зонт не нужен, важнее непромокаемая нескользящая обувь и капюшон."), "#CFEFFF"
    if max_rain >= 60:
        return f"Явных осадков не видно, но вероятность до {max_rain:.0f}%: зонт на всякий случай.", "#E6D98C"
    return None


def clothes_text(points: list[dict]) -> str:
    """Что надеть на все 24 часа окна: по самой низкой/высокой температуре и самому сильному ветру.
    Про осадки здесь не пишем — для них отдельная строка про зонт."""
    from services import weather as w
    temps = [p["temp"] for p in points]
    return w._clothes_advice(min(temps), max(temps), max(p["wind"] for p in points))


def footer_rows(points: list[dict]) -> list[tuple[str, str, str]]:
    """Строки нижнего блока картинки: (подпись, текст, цвет подписи)."""
    from services import weather as w
    rows = [("Одежда", clothes_text(points), TEMP_COLOR)]
    umbrella = umbrella_text(points)
    if umbrella:
        rows.append(("Зонт", umbrella[0], umbrella[1]))
    if max(p["spread"] for p in points) >= w.TEMP_DISAGREEMENT_C:
        rows.append(("Модели", "расходятся по температуре — прогноз может измениться.", MUTED))
    return rows


def caption_for(points: list[dict], windows: list[list[dict]], start: datetime.datetime) -> str:
    """Короткая подпись (видна в уведомлении): заголовок и, если нужен, вывод про зонт."""
    umbrella = umbrella_text(points)
    return f"🌤 {title_for(start)}" + (f"\n☂ {umbrella[0].split('.')[0]}." if umbrella else "")



def sun_info(data: dict, start: datetime.datetime) -> tuple[str, str] | None:
    """(восход, закат) «HH:MM» для даты start из daily Open-Meteo; None, если данных нет."""
    daily = (data or {}).get("daily") or {}
    try:
        i = (daily.get("time") or []).index(start.strftime("%Y-%m-%d"))
        rise, sset = daily["sunrise"][i], daily["sunset"][i]
        return rise[11:16], sset[11:16]
    except (ValueError, KeyError, IndexError, TypeError):
        return None


_MOON_NAMES = ["Новолуние", "Растущий серп", "Первая четверть", "Растущая луна",
               "Полнолуние", "Убывающая луна", "Последняя четверть", "Убывающий серп"]


def moon_phase(when: datetime.datetime) -> tuple[float, str]:
    """(фаза 0..1, название): 0 — новолуние, 0.5 — полнолуние. Простая формула по синодическому месяцу
    (точность около суток — для картинки достаточно)."""
    ref = datetime.datetime(2000, 1, 6, 18, 14)                     # известное новолуние
    age = ((when.replace(tzinfo=None) - ref).total_seconds() / 86400.0) % 29.530588853
    phase = age / 29.530588853
    return phase, _MOON_NAMES[int((phase * 8) + 0.5) % 8]


def _draw_moon(fig, cx, cy, r, phase: float) -> None:
    """Луна в дюймах: тёмный диск и освещённая часть по фазе."""
    import numpy as np
    from matplotlib.patches import Circle, Polygon
    fig.add_artist(Circle((cx, cy), r, transform=fig.dpi_scale_trans, facecolor="#2a2d55", edgecolor="#5b628f",
                          linewidth=0.8, zorder=3))
    k = np.cos(2 * np.pi * phase)                                    # 1 новолуние … -1 полнолуние
    t = np.linspace(-np.pi / 2, np.pi / 2, 60)
    right = phase < 0.5                                              # растёт — светится правая сторона
    side = 1 if right else -1
    outer = np.column_stack([cx + side * r * np.cos(t), cy + r * np.sin(t)])
    inner = np.column_stack([cx + side * r * k * np.cos(t[::-1]), cy + r * np.sin(t[::-1])])
    if abs(k) > 0.985 and k > 0:                                     # новолуние — без света
        return
    fig.add_artist(Polygon(np.vstack([outer, inner]), closed=True, transform=fig.dpi_scale_trans,
                           facecolor="#F3E7B3", edgecolor="none", zorder=4))


CARD_EDGE = "#2c2f5c"
SUB = "#c4cbe6"
# цвета значков осадков/неба и подписей на тёмном фоне чуть ярче линий (это иконки и текст, не серии)
TEMP_TEXT = "#FFC27A"
FEELS_TEXT = "#9CC2FF"


def summary_line(points: list[dict]) -> str:
    t = [p["temp"] for p in points]
    f = [p["feels"] for p in points]
    return (f"{_fmt(min(t))} … {_fmt(max(t))}  ·  ощущается {_fmt(min(f))} … {_fmt(max(f))}  ·  "
            f"осадки до {max(p['rain'] for p in points):.0f}%  ·  ветер до {max(p['wind'] for p in points):.0f} км/ч")


def _smooth(xs, ys, per_segment: int = 12):
    """Плавная кривая Catmull-Rom через точки (без выбросов за диапазон соседей)."""
    import numpy as np
    pts = list(zip(xs, ys))
    if len(pts) < 3:
        return list(xs), list(ys)
    out_x, out_y = [], []
    for i in range(len(pts) - 1):
        p0 = pts[i - 1] if i > 0 else pts[i]
        p1, p2 = pts[i], pts[i + 1]
        p3 = pts[i + 2] if i + 2 < len(pts) else pts[i + 1]
        lo, hi = min(p1[1], p2[1]), max(p1[1], p2[1])
        for t in np.linspace(0, 1, per_segment, endpoint=False):
            t2, t3 = t * t, t * t * t
            x = p1[0] + (p2[0] - p1[0]) * t
            y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                       + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            out_x.append(x)
            out_y.append(min(max(y, lo - 0.6), hi + 0.6))        # не вылезаем заметно за соседние значения
    out_x.append(pts[-1][0])
    out_y.append(pts[-1][1])
    return out_x, out_y


def _card(fig, x, y, w, h, color=PANEL, edge=CARD_EDGE, radius=0.16):
    """Скруглённая карточка; координаты в дюймах от левого нижнего угла."""
    from matplotlib.patches import FancyBboxPatch
    patch = FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={radius}",
                           transform=fig.dpi_scale_trans, facecolor=color, edgecolor=edge, linewidth=1.0, zorder=0)
    fig.add_artist(patch)
    return patch


def _text_in(fig, x, y, text, **kw):
    return fig.text(x, y, text, transform=fig.dpi_scale_trans, **kw)


@serialized_plot
def render(windows: list[list[dict]], title: str, footer: list[tuple[str, str, str]] | None = None,
           subtitle: str = "", sun: tuple[str, str] | None = None,
           moon: tuple[float, str] | None = None) -> bytes:
    import textwrap
    import numpy as np
    footer = footer or []
    W, M = 8.2, 0.3
    chart_h, gap = 4.05, 0.22
    wrapped = [textwrap.wrap(text, 66, break_on_hyphens=False) or [""] for _, text, _ in footer]
    footer_hs = [0.62 + 0.25 * len(lines) for lines in wrapped]
    header_h = 1.0
    H = header_h + len(windows) * chart_h + (len(windows) - 1) * gap + (gap + sum(footer_hs) + gap * (len(footer) - 1) if footer else 0) + 0.3
    fig = plt.figure(figsize=(W, H), dpi=150, facecolor=BG)

    bg = fig.add_axes([0, 0, 1, 1], zorder=-5)                     # мягкий вертикальный градиент фона
    bg.axis("off")
    grad = np.linspace(0, 1, 256).reshape(-1, 1)
    from matplotlib.colors import LinearSegmentedColormap
    bg.imshow(grad, aspect="auto", extent=[0, 1, 0, 1], origin="upper",
              cmap=LinearSegmentedColormap.from_list("bg", ["#101126", "#1a1c3a"]))

    _text_in(fig, M + 0.05, H - 0.5, title, fontsize=18, fontweight="bold", color=TEXT, va="center")
    if subtitle:
        _text_in(fig, M + 0.05, H - 0.86, subtitle, fontsize=9.6, color=SUB, va="center")

    right = W - M - 0.1
    if moon:                                                       # справа в шапке: луна, восход, закат
        _draw_moon(fig, right - 0.28, H - 0.5, 0.2, moon[0])
        _text_in(fig, right - 0.6, H - 0.5, moon[1], fontsize=9, color=SUB, va="center", ha="right")
    if sun:
        _text_in(fig, W - M - 0.1, H - 0.86, f"Восход {sun[0]}  ·  Закат {sun[1]}", fontsize=9.6, color=TEMP_TEXT,
                 va="center", ha="right")

    y_top = header_h
    for win in windows:
        y_bottom = H - y_top - chart_h
        _card(fig, M, y_bottom, W - 2 * M, chart_h)
        ax = fig.add_axes([(M + 0.2) / W, (y_bottom + 0.95) / H, (W - 2 * M - 0.4) / W, (chart_h - 1.85) / H])
        _draw_window(ax, win)
        y_top += chart_h + gap

    for (label, text, color), lines, h in zip(footer, wrapped, footer_hs):
        y_bottom = H - y_top - h
        _card(fig, M, y_bottom, W - 2 * M, h, radius=0.14)
        from matplotlib.patches import FancyBboxPatch
        fig.add_artist(FancyBboxPatch((M + 0.16, y_bottom + 0.16), 0.07, h - 0.32, boxstyle="round,pad=0,rounding_size=0.035",
                                      transform=fig.dpi_scale_trans, facecolor=color, edgecolor="none", zorder=1))
        _text_in(fig, M + 0.42, y_bottom + h - 0.26, label.upper(), fontsize=8.5, fontweight="bold",
                 color=MUTED, va="center")
        for k, line in enumerate(lines):
            _text_in(fig, M + 0.42, y_bottom + h - 0.52 - 0.25 * k, line, fontsize=10.8, color=TEXT, va="center")
        y_top += h + gap

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


def _draw_window(ax, win: list[dict]) -> None:
    import numpy as np
    from matplotlib.patches import Polygon
    xs = list(range(len(win)))
    temps = [p["temp"] for p in win]
    feels = [p["feels"] for p in win]
    ax.set_facecolor("none")
    for spine in ax.spines.values():
        spine.set_visible(False)

    lo, hi = min(temps + feels), max(temps + feels)
    pad = max(3.0, (hi - lo) * 0.35)
    span = (hi - lo) + 2 * pad
    ymin, ymax = lo - pad - span * 0.62, hi + pad + 10
    ax.set_ylim(ymin, ymax)
    ax.set_xlim(-0.6, len(win) - 0.4)

    for i, p in enumerate(win):                                    # ночные часы чуть темнее
        if p["dt"].hour >= 21 or p["dt"].hour < 6:
            ax.axvspan(i - 0.5, i + 0.5, color=NIGHT, alpha=0.55, zorder=0, linewidth=0)

    if ymin < 0 < ymax:                                            # линия нуля — где начинается мороз
        ax.axhline(0, color="white", alpha=0.16, linewidth=0.9, linestyle=(0, (4, 4)), zorder=1)
        ax.text(len(win) - 0.42, 0, "0°", color=MUTED, fontsize=8, ha="right", va="bottom", zorder=1)

    ax2 = ax.twinx()                                               # осадки: скруглённые столбики внизу
    ax2.set_ylim(0, 620)
    ax2.set_xlim(ax.get_xlim())
    rainy = [(i, p["rain"]) for i, p in enumerate(win) if p["rain"] >= 3]
    if rainy:
        ax2.bar([i for i, _ in rainy], [r for _, r in rainy], width=0.56, color=RAIN_COLOR, alpha=0.42, zorder=1,
                linewidth=0)
    ax2.axis("off")

    sx, sy = _smooth(xs, temps)
    fx, fy = _smooth(xs, feels)
    grad = np.zeros((64, 1, 4))                                    # градиентная заливка под температурой
    grad[..., :3] = [0xFF / 255, 0xA8 / 255, 0x3C / 255]
    grad[..., 3] = np.linspace(0.30, 0.0, 64).reshape(-1, 1)
    im = ax.imshow(grad, extent=[-0.6, len(win) - 0.4, ymin, ymax], aspect="auto", origin="upper", zorder=2)
    clip = Polygon(np.column_stack([sx + [sx[-1], sx[0]], sy + [ymin, ymin]]), closed=True,
                   transform=ax.transData, facecolor="none", edgecolor="none")
    ax.add_patch(clip)
    im.set_clip_path(clip)
    ax.set_xlim(-0.6, len(win) - 0.4)
    ax.set_ylim(ymin, ymax)

    ax.plot(sx, sy, color=TEMP_COLOR, linewidth=2.4, solid_capstyle="round", zorder=4)
    ax.plot(fx, fy, color=FEELS_COLOR, linewidth=2.0, linestyle=(0, (5, 3)), solid_capstyle="round", zorder=4)

    key = [i for i in xs if i % 3 == 0]
    ax.scatter(key, [temps[i] for i in key], s=46, color=TEMP_COLOR, edgecolors="#1f2142", linewidths=2, zorder=6)
    ax.scatter(key, [feels[i] for i in key], s=34, color=FEELS_COLOR, edgecolors="#1f2142", linewidths=2, zorder=6)

    pill = dict(boxstyle="round,pad=0.22,rounding_size=0.7", fc="#1f2142", ec="none", alpha=0.88)
    for i in key:
        p = win[i]
        above_temp = p["temp"] >= p["feels"]
        ax.annotate(_fmt(p["temp"]), (i, p["temp"]), textcoords="offset points", xytext=(0, 12 if above_temp else -17),
                    ha="center", va="center", fontsize=10.8, fontweight="bold", color=TEXT, bbox=pill, zorder=7)
        ax.annotate(_fmt(p["feels"]), (i, p["feels"]), textcoords="offset points", xytext=(0, -17 if above_temp else 12),
                    ha="center", va="center", fontsize=9.6, color=FEELS_TEXT, bbox=pill, zorder=7)
        glyph, color = _icon(p["code"], p["dt"].hour)
        tr = ax.get_xaxis_transform()
        ax.scatter([i], [0.9], s=1150, color="white", alpha=0.055, transform=tr, zorder=3, linewidths=0)
        ax.text(i, 0.9, glyph, transform=tr, ha="center", va="center", fontsize=21, color=color, zorder=4)
        if p["rain"] >= 20:
            ax2.text(i, p["rain"] + 14, f"{p['rain']:.0f}%", ha="center", fontsize=8.6, color=SUB, zorder=4)
        ax.text(i, -0.235, f"ветер {p['wind']:.0f} км/ч", transform=tr, ha="center", fontsize=8.6, color=MUTED)

    ax.set_xticks(xs)                                              # подпись времени под каждым часом
    ax.set_xticklabels([f"{p['dt']:%H}" for p in win], fontsize=8.4)
    for i, label in enumerate(ax.get_xticklabels()):
        label.set_color(TEXT if i % 3 == 0 else MUTED)
        label.set_fontweight("bold" if i % 3 == 0 else "normal")
    ax.tick_params(axis="x", length=0, pad=6)
    ax.set_yticks([])
    ax.set_title(window_label(win), loc="left", color=TEXT, fontsize=12.5, fontweight="bold", pad=22, x=-0.02)

    handles = [
        plt.Line2D([], [], color=TEMP_COLOR, linewidth=2.4, label="температура"),
        plt.Line2D([], [], color=FEELS_COLOR, linewidth=2.0, linestyle=(0, (5, 3)), label="ощущается"),
        plt.Line2D([], [], color=RAIN_COLOR, linewidth=7, alpha=0.6, solid_capstyle="round", label="осадки, %"),
    ]
    ax.legend(handles=handles, loc="lower right", bbox_to_anchor=(1.02, 1.0), ncol=3, frameon=False, fontsize=8.2,
              labelcolor=SUB, handlelength=1.8, columnspacing=1.1, borderaxespad=0.1)

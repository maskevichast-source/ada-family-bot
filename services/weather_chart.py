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

BG = "#1a1a2e"
PANEL = "#23233d"
NIGHT = "#0f0f26"
TEXT = "#ecf0f1"
MUTED = "#9aa5b1"
TEMP_COLOR = "#FF9F43"
FEELS_COLOR = "#54A0FF"
RAIN_COLOR = "#4ECDC4"

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


def window_label(win: list[dict]) -> str:
    a, b = win[0]["dt"], win[-1]["dt"]
    return f"{a:%H:%M} → {b:%H:%M}"


def title_for(start: datetime.datetime) -> str:
    return f"Астана · {WEEKDAYS_RU[start.weekday()]}, {start.day} {MONTHS_RU_GEN[start.month - 1]}"


def caption_for(points: list[dict], windows: list[list[dict]], start: datetime.datetime) -> str:
    """Короткая подпись под картинкой: размах температуры по окнам, осадки, ветер, что надеть."""
    from services import weather as w
    lines = [f"🌤 {title_for(start)}"]
    for win in windows:
        t = [p["temp"] for p in win]
        f = [p["feels"] for p in win]
        rain = max(p["rain"] for p in win)
        wind = max(p["wind"] for p in win)
        lines.append(f"{window_label(win)}: {_fmt(min(t))}…{_fmt(max(t))} (ощущ. {_fmt(min(f))}…{_fmt(max(f))}), "
                     f"осадки до {rain:.0f}%, ветер до {wind:.0f} км/ч")
    codes = {int(p["code"]) for p in points if p["code"] is not None}
    has_snow, has_rain = bool(codes & _SNOW), bool(codes & (_RAIN | _STORM))
    temps = [p["temp"] for p in points]
    advice = w._clothes_advice(min(temps), max(temps), max(p["wind"] for p in points),
                               max(p["rain"] for p in points), has_rain=has_rain, has_snow=has_snow)
    lines.append(f"👕 {advice}")
    if max(p["spread"] for p in points) >= w.TEMP_DISAGREEMENT_C:
        lines.append("🔀 Модели расходятся по температуре — прогноз может измениться.")
    text = "\n".join(lines)
    return text if len(text) <= 1000 else text[:997] + "…"


@serialized_plot
def render(windows: list[list[dict]], title: str) -> bytes:
    n = len(windows)
    fig, axes = plt.subplots(n, 1, figsize=(8.2, 4.7 * n + 0.8), dpi=150, facecolor=BG)
    if n == 1:
        axes = [axes]
    fig.suptitle(title, fontsize=16, fontweight="bold", color=TEXT, y=0.985 if n == 2 else 0.97)
    for ax, win in zip(axes, windows):
        _draw_window(ax, win)
    fig.tight_layout(rect=(0, 0, 1, 0.965 if n == 2 else 0.93), h_pad=2.2)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


def _draw_window(ax, win: list[dict]) -> None:
    xs = list(range(len(win)))
    temps = [p["temp"] for p in win]
    feels = [p["feels"] for p in win]
    ax.set_facecolor(PANEL)
    for spine in ax.spines.values():
        spine.set_visible(False)

    for i, p in enumerate(win):                                   # ночные часы чуть темнее
        if p["dt"].hour >= 21 or p["dt"].hour < 6:
            ax.axvspan(i - 0.5, i + 0.5, color=NIGHT, alpha=0.75, zorder=0)

    lo, hi = min(temps + feels), max(temps + feels)
    pad = max(3.0, (hi - lo) * 0.35)
    span = (hi - lo) + 2 * pad
    ax.set_ylim(lo - pad - span * 0.42, hi + pad + 4)             # снизу — зона осадков, сверху — иконки
    ax.set_xlim(-0.6, len(win) - 0.4)

    ax2 = ax.twinx()                                              # осадки — столбики внизу панели
    ax2.set_ylim(0, 480)                                          # 100% осадков ≈ пятая часть панели
    ax2.bar(xs, [p["rain"] for p in win], color=RAIN_COLOR, alpha=0.35, width=0.8, zorder=1)
    ax2.axis("off")

    ax.plot(xs, temps, color=TEMP_COLOR, linewidth=2.6, marker="o", markersize=5, zorder=3)
    ax.plot(xs, feels, color=FEELS_COLOR, linewidth=2.0, linestyle="--", marker="o", markersize=3.5, zorder=3)

    for i, p in enumerate(win):
        if i % 3:
            continue
        above_temp = p["temp"] >= p["feels"]
        ax.annotate(_fmt(p["temp"]), (i, p["temp"]), textcoords="offset points",
                    xytext=(0, 9 if above_temp else -16), ha="center", fontsize=10.5, fontweight="bold",
                    color=TEMP_COLOR, zorder=4)
        ax.annotate(_fmt(p["feels"]), (i, p["feels"]), textcoords="offset points",
                    xytext=(0, -16 if above_temp else 9), ha="center", fontsize=9.5, color=FEELS_COLOR, zorder=4)
        glyph, color = _icon(p["code"], p["dt"].hour)
        ax.text(i, 0.93, glyph, transform=ax.get_xaxis_transform(), ha="center", va="center",
                fontsize=19, color=color, zorder=4)
        if p["rain"] >= 20:
            ax2.text(i, p["rain"] + 6, f"{p['rain']:.0f}%", ha="center", fontsize=8.5, color=RAIN_COLOR, zorder=4)
        ax.text(i, -0.2, f"{p['wind']:.0f} км/ч", transform=ax.get_xaxis_transform(), ha="center",
                fontsize=9, color=MUTED)

    ax.set_xticks([i for i in xs if i % 3 == 0])
    ax.set_xticklabels([f"{win[i]['dt']:%H:%M}" for i in xs if i % 3 == 0], color=TEXT, fontsize=10)
    ax.tick_params(axis="x", length=0, pad=4)
    ax.set_yticks([])
    ax.set_title(window_label(win), loc="left", color=TEXT, fontsize=12.5, fontweight="bold", pad=10)

    handles = [
        plt.Line2D([], [], color=TEMP_COLOR, linewidth=2.6, label="температура"),
        plt.Line2D([], [], color=FEELS_COLOR, linewidth=2.0, linestyle="--", label="ощущается"),
        plt.Rectangle((0, 0), 1, 1, color=RAIN_COLOR, alpha=0.5, label="вероятность осадков"),
    ]
    ax.legend(handles=handles, loc="upper right", bbox_to_anchor=(1.0, 1.13), ncol=3, frameon=False,
              fontsize=8, labelcolor=MUTED, handlelength=1.6, columnspacing=1.0)

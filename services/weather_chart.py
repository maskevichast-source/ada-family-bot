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


def umbrella_text(points: list[dict]) -> tuple[str, str]:
    """(текст про зонт, цвет): нужен ли зонт и почему. Снег — зонт ни при чём, важнее обувь и капюшон."""
    rain_hours = _precip_hours(points, _RAIN | _STORM)
    snow_hours = _precip_hours(points, _SNOW)
    max_rain = max((p["rain"] for p in points), default=0)
    if rain_hours and snow_hours:
        return (f"Зонт пригодится: дождь {_span(rain_hours)} и снег {_span(snow_hours)} (осадки до {max_rain:.0f}%). "
                "Обувь непромокаемая, куртка с капюшоном."), FEELS_COLOR
    if rain_hours:
        return f"Зонт нужен: дождь {_span(rain_hours)}, вероятность осадков до {max_rain:.0f}%.", FEELS_COLOR
    if snow_hours:
        return (f"Зонт не нужен, ожидается снег {_span(snow_hours)} (до {max_rain:.0f}%). "
                "Важнее непромокаемая нескользящая обувь и капюшон."), "#CFEFFF"
    if max_rain >= 50:
        return f"Явных осадков не видно, но вероятность до {max_rain:.0f}%: зонт на всякий случай.", "#E6D98C"
    return "Зонт не нужен: осадков не ожидается.", "#7BD88F"


def clothes_text(points: list[dict]) -> str:
    """Что надеть на все 24 часа окна: по самой низкой/высокой температуре и самому сильному ветру.
    Про осадки здесь не пишем — для них отдельная строка про зонт."""
    from services import weather as w
    temps = [p["temp"] for p in points]
    return w._clothes_advice(min(temps), max(temps), max(p["wind"] for p in points))


def footer_rows(points: list[dict]) -> list[tuple[str, str, str]]:
    """Строки нижнего блока картинки: (подпись, текст, цвет подписи)."""
    from services import weather as w
    umbrella, umbrella_color = umbrella_text(points)
    rows = [("Одежда", clothes_text(points), TEMP_COLOR), ("Зонт", umbrella, umbrella_color)]
    if max(p["spread"] for p in points) >= w.TEMP_DISAGREEMENT_C:
        rows.append(("Модели", "расходятся по температуре — прогноз может измениться.", MUTED))
    return rows


def caption_for(points: list[dict], windows: list[list[dict]], start: datetime.datetime) -> str:
    """Короткая подпись (видна в уведомлении): заголовок и вывод про зонт. Всё остальное — на картинке."""
    umbrella, _ = umbrella_text(points)
    return f"🌤 {title_for(start)}\n☂ {umbrella.split('.')[0]}."


@serialized_plot
def render(windows: list[list[dict]], title: str, footer: list[tuple[str, str, str]] | None = None) -> bytes:
    n = len(windows)
    footer = footer or []
    footer_h = 0.5 + 0.55 * len(footer) if footer else 0.0
    chart_h = 4.15
    height = chart_h * n + footer_h + 0.8
    fig = plt.figure(figsize=(8.2, height), dpi=150, facecolor=BG)
    fig.suptitle(title, fontsize=16, fontweight="bold", color=TEXT, y=1 - 0.15 / height)
    top = 1 - 0.45 / height
    for k, win in enumerate(windows):
        ax = fig.add_axes([0.04, top - (k + 1) * chart_h / height + 0.8 / height, 0.92, (chart_h - 1.8) / height])
        _draw_window(ax, win)
    if footer:
        _draw_footer(fig, footer, footer_h / height)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


def _draw_footer(fig, rows, rel_height: float) -> None:
    import textwrap
    ax = fig.add_axes([0.04, 0.01, 0.92, rel_height - 0.01])
    ax.set_facecolor(PANEL)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    lines_total = sum(max(1, len(textwrap.wrap(text, 62, break_on_hyphens=False))) for _, text, _ in rows)
    unit = 1 / (lines_total + 0.6 * len(rows) + 0.6)
    y = 1 - 0.7 * unit
    for label, text, color in rows:
        wrapped = textwrap.wrap(text, 62, break_on_hyphens=False) or [""]
        ax.text(0.03, y, label, color=color, fontsize=11, fontweight="bold", va="center", ha="left")
        for line in wrapped:
            ax.text(0.17, y, line, color=TEXT, fontsize=10.5, va="center", ha="left")
            y -= unit
        y -= 0.6 * unit


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

    ax.set_xticks(xs)                                              # подпись времени под каждым часом
    ax.set_xticklabels([f"{p['dt']:%H:%M}" for p in win], fontsize=7.6)
    for i, label in enumerate(ax.get_xticklabels()):
        label.set_color(TEXT if i % 3 == 0 else MUTED)
        label.set_fontweight("bold" if i % 3 == 0 else "normal")
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

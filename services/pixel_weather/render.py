"""Фасад: из данных Open-Meteo делает набор — анимированная карточка (GIF) и две страницы (PNG)."""
import asyncio
import datetime
import io
from dataclasses import dataclass

from services.pixel_weather import model, pages, phrase, scene, theme as T


@dataclass
class PixelSet:
    gif: bytes
    chart_png: bytes
    modules_png: bytes
    caption: str
    phrase: str


def _png(page, scale=T.PAGE_SCALE) -> bytes:
    buf = io.BytesIO()
    page.scaled(scale).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def caption_for(m: dict, text: str) -> str:
    from services import weather_chart as wc
    st = m["start"]
    head = f"Астана · {wc.WEEKDAYS_RU[st.weekday()]}, {st.day} {wc.MONTHS_RU_GEN[st.month - 1]}"
    name = T.THREAT_LEVELS[m["threat"]][0].capitalize()
    lines = [head, f"{scene._signed(m['tmin'])}…{scene._signed(m['tmax'])}°, ощущается {scene._signed(m['first']['feels'])}°",
             f"Уровень угрозы: {name}" + (f" ({', '.join(m['reasons'][:2])})" if m["reasons"] else "")]
    umb = wc.umbrella_text(m["points"])
    if umb:
        lines.append(umb[0])
    lines.append(f"Одежда: {m['clothes']}")
    return "\n".join(lines)[:1000]


async def build_set(data: dict, start: datetime.datetime, kind: str, aqi: dict | None = None) -> PixelSet | None:
    m = model.build(data, start, aqi)
    if m is None:
        return None
    text = await phrase.make_phrase(m, kind)

    def work():
        return PixelSet(
            gif=scene.render_gif(m, text),
            chart_png=_png(pages.page_chart(m)),
            modules_png=_png(pages.page_modules(m)),
            caption=caption_for(m, text),
            phrase=text,
        )

    return await asyncio.to_thread(work)

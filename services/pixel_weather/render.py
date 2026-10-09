"""Фасад: из данных Open-Meteo делает набор — GIF «На улице», 6 страниц в стиле RAD и карточку Dispatch."""
import asyncio
import datetime
from dataclasses import dataclass, field

from services.pixel_weather import insights, model, phrase, rad_pages, scene


@dataclass
class PixelSet:
    gif: bytes
    pages: list[bytes]
    caption: str
    street: str
    dispatch_text: str
    dispatch: bytes = b""
    names: list[str] = field(default_factory=lambda: ["day"])


def caption_for(m: dict) -> str:
    from services import weather_chart as wc
    st = m["start"]
    head = f"Астана · {wc.WEEKDAYS_RU[st.weekday()]}, {st.day} {wc.MONTHS_RU_GEN[st.month - 1]}"
    sgn = rad_pages._sgn
    lines = [head, f"{sgn(m['tmin'])}…{sgn(m['tmax'])}°, ощущается {sgn(m['first']['feels'])}°",
             f"Уровень угрозы: {insights.THREAT_NAMES[m['threat']].capitalize()}"
             + (f" ({', '.join(m['reasons'][:2])})" if m["reasons"] else ""), m["advice"]]
    umb = wc.umbrella_text(m["points"])
    if umb:
        lines.append(umb[0])
    lines.append(f"Одежда: {m['clothes']}")
    return "\n".join(lines)[:1000]


def make_dispatch(m: dict, text: str) -> bytes:
    kind = rad_pages.icons.kind_of(m["first"].get("code"), m["night"])
    tag = rad_pages.COND_RU.get(kind, "ПОГОДА")
    return rad_pages.dispatch_card(m, text, scene.scene_image(m, 0.4), tag)


def build_model(data: dict, start: datetime.datetime, kind: str, aqi: dict | None = None) -> dict | None:
    m = model.build(data, start, aqi)
    if m is None:
        return None
    return insights.enrich(m, data, kind)


async def build_set(data: dict, start: datetime.datetime, kind: str, aqi: dict | None = None) -> PixelSet | None:
    m = build_model(data, start, kind, aqi)
    if m is None:
        return None
    street, dispatch_text = await phrase.make_phrases(m, kind)

    def work():
        sgn = rad_pages._sgn
        f = m["first"]
        threat = insights.THREAT_NAMES[m["threat"]].capitalize()
        foot = f"{threat}  ·  ветер {m['wind_kmh']:.0f} км/ч  ·  {sgn(m['tmin'])}…{sgn(m['tmax'])}°"
        kind_icon = rad_pages.icons.kind_of(f.get("code"), m["night"])
        gif = scene.render_gif(m, street, foot=foot, tag=rad_pages.COND_RU.get(kind_icon, ""),
                               big=f"{sgn(f['temp'])}°", sub=f"ощущается {sgn(f['feels'])}°")
        return PixelSet(gif=gif, pages=[rad_pages.page_day(m)], caption=caption_for(m), street=street,
                        dispatch_text=dispatch_text)

    return await asyncio.to_thread(work)

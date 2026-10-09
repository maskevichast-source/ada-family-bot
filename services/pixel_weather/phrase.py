"""Фразы Ады для «На улице» и Dispatch: коротко, с юмором. Цифры не выдумывает — получает готовые факты."""
import asyncio

from services.pixel_weather import insights

STREET_MAX = 44
DISPATCH_MAX = 95


def fallback_phrase(m: dict) -> str:
    """Запасная колкая фраза для Dispatch из шаблонов по самому важному."""
    f = m["first"]
    if m["thunder"][0] == 2:
        return "Гроза. Зонт — для слабаков, лучше сидеть дома."
    if m["ice"][0] == 2:
        return "Гололёд. Идём как пингвины и никуда не торопимся."
    if m["threat"] >= 2 and m["reasons"]:
        return f"Опасно: {m['reasons'][0]}. Береги себя и своих."
    if f["temp"] <= -25:
        return "Мороз. Шапку надеть, нос не показывать."
    if f["temp"] >= 30:
        return "Жара. Вода, тень и никаких героических подвигов."
    if "СНЕГ" in m["precip"]:
        return "Снег. Красиво, но обувь лучше выбрать нескользкую."
    if "ДОЖДЬ" in m["precip"]:
        return "Дождь. Твои кроссовки тебе этого не простят."
    if m["wind_max"] >= 12:
        return "Ветер. Держите шапки и планы покрепче."
    return "Без сюрпризов. Живём спокойно, как и положено."


def _clean(line: str) -> str:
    line = (line or "").strip()
    for prefix in (">", "-", "•", "1.", "2.", "1)", "2)"):
        if line.startswith(prefix):
            line = line[len(prefix):].strip()
    line = line.strip('"«»').strip()
    return " ".join(line.split())


def parse_lines(raw: str) -> tuple[str, str] | None:
    lines = [_clean(x) for x in (raw or "").splitlines() if _clean(x)]
    if len(lines) < 2:
        return None
    street, dispatch = lines[0], lines[1]
    if len(street) > STREET_MAX or len(dispatch) > DISPATCH_MAX:
        return None
    return street, dispatch


async def make_phrases(m: dict, kind: str) -> tuple[str, str]:
    """(текст «На улице», текст Dispatch). При любой неудаче — запасные из шаблонов."""
    from services.deepseek_service import generate_weather_phrase
    from services.pixel_weather.model import facts_line
    try:
        raw = await asyncio.wait_for(generate_weather_phrase(facts_line(m), kind), timeout=25)
    except Exception as error:
        print(f"[Погода] Фраза Ады не получилась: {error}")
        raw = ""
    parsed = parse_lines(raw)
    if parsed:
        return parsed
    return insights.street_text(m), fallback_phrase(m)


async def make_phrase(m: dict, kind: str) -> str:            # для совместимости: только Dispatch
    return (await make_phrases(m, kind))[1]

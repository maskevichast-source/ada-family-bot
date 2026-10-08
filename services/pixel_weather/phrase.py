"""Фраза Ады для карточки: коротко, с юмором, в стиле терминала. Цифры не выдумывает — получает готовые факты."""
import asyncio

MAX_LEN = 108


def fallback_phrase(m: dict) -> str:
    """Запасная фраза, если нейросеть недоступна: из шаблонов по самому важному."""
    f = m["first"]
    if m["thunder"][0] == 2:
        return "Грозу обещают всерьёз. Зонт — для слабаков, лучше сидеть дома."
    if m["ice"][0] == 2:
        return "Гололёд. Идём как пингвины и никуда не торопимся."
    if m["threat"] >= 2 and m["reasons"]:
        return f"Сегодня опасно: {m['reasons'][0]}. Береги себя и своих."
    if f["temp"] <= -25:
        return "Мороз крепкий. Шапку надеть, нос не показывать."
    if f["temp"] >= 30:
        return "Жара. Вода, тень и никаких героических подвигов."
    if "СНЕГ" in m["precip"]:
        return "Снег идёт. Красиво, но обувь лучше выбрать нескользкую."
    if "ДОЖДЬ" in m["precip"]:
        return "Дождь будет. Зонт с собой, и настроение тоже сухое."
    if m["wind_max"] >= 12:
        return "Ветер злой. Держите шапки и планы покрепче."
    return "Погода без сюрпризов. Живём спокойно, как и положено."


def _clean(text: str) -> str:
    text = (text or "").strip().strip('"«»').replace("\n", " ")
    text = " ".join(text.split())
    if text.startswith(">"):
        text = text[1:].strip()
    return text


async def make_phrase(m: dict, kind: str) -> str:
    from services.deepseek_service import generate_weather_phrase
    from services.pixel_weather.model import facts_line
    try:
        text = _clean(await asyncio.wait_for(generate_weather_phrase(facts_line(m), kind), timeout=25))
    except Exception as error:
        print(f"[Погода] Фраза Ады не получилась: {error}")
        text = ""
    if not text or len(text) > MAX_LEN:
        return fallback_phrase(m)
    return text

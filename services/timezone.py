import datetime
import re

ASTANA_TZ = datetime.timezone(datetime.timedelta(hours=5), name="Asia/Astana")


def now_astana():
    return datetime.datetime.now(ASTANA_TZ)


_RU_NUMBERS = {
    "ноль": 0,
    "час": 1,
    "один": 1,
    "одну": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
}

_WEEKDAYS_RU = {
    "понедельник": 0,
    "понедельника": 0,
    "вторник": 1,
    "вторника": 1,
    "среда": 2,
    "среду": 2,
    "четверг": 3,
    "четверга": 3,
    "пятница": 4,
    "пятницу": 4,
    "суббота": 5,
    "субботу": 5,
    "воскресенье": 6,
}


def parse_flexible_datetime(value):
    """Устойчиво разобрать время напоминания/табличную дату.

    Возвращает datetime с таймзоной Астаны, либо None, если разобрать не удалось.
    """
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=ASTANA_TZ)

    text = str(value).strip()
    if not text:
        return None

    # ISO 8601, в т.ч. с "Z" (UTC) — например из внешних интеграций.
    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.datetime.fromisoformat(iso_text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ASTANA_TZ)
        return parsed.astimezone(ASTANA_TZ)
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
    ):
        try:
            return datetime.datetime.strptime(text, fmt).replace(tzinfo=ASTANA_TZ)
        except ValueError:
            continue
    return None


_MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def parse_ru_relative_datetime(text: str, base_now: datetime.datetime | None = None) -> datetime.datetime | None:
    """Парсит бытовые русские даты для напоминаний.

    Примеры:
    - сегодня в 21:00 / сегодня в 21.00
    - сегодня в девять вечера
    - завтра в 9 утра
    - послезавтра в 18:30
    - в понедельник в 10
    - через 15 минут / через два часа / через полчаса
    - 06.10.2026 в 12:00 (точная дата)

    Возвращает None, если время в прошлом для явно названного "сегодня",
    время/дата некорректны, в тексте два разных времени сразу (неоднозначно),
    или дата названа в формате, который не поддерживается (день + название
    месяца словами) — чтобы не подставить наугад неверную дату.
    """
    if not text:
        return None

    now = base_now or now_astana()
    t = str(text).lower().replace("ё", "е")

    # Названием месяца ("10 октября") пока не умеем — лучше не гадать,
    # чем молча поставить неверную дату.
    if any(re.search(rf"\d+\s*{m}\b", t) for m in _MONTHS_RU):
        return None

    # ── Точная дата "06.10.2026" ──
    explicit_date = None
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", t)
    if m:
        try:
            explicit_date = datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
        # Не даём цифрам самой даты попасть в разбор времени ниже.
        t = t[:m.start()] + " " * (m.end() - m.start()) + t[m.end():]

    # Два разных времени в одной фразе ("в 9:00 и 18:00") — неоднозначно.
    time_mentions = re.findall(r"\d{1,2}[:.]\d{2}", t)
    if len(set(time_mentions)) > 1:
        return None

    # ── Относительное время: "через 15 минут", "через два часа", "через полчаса" ──
    if "через" in t:
        if "полчаса" in t:
            return now + datetime.timedelta(minutes=30)
        m = re.search(r"через\s+(\d+|[а-я]+)\s*минут", t)
        if m:
            n = int(m.group(1)) if m.group(1).isdigit() else _RU_NUMBERS.get(m.group(1))
            if n is not None:
                return now + datetime.timedelta(minutes=n)
        m = re.search(r"через\s+(\d+|[а-я]+)\s*час", t)
        if m:
            n = int(m.group(1)) if m.group(1).isdigit() else _RU_NUMBERS.get(m.group(1))
            if n is not None:
                return now + datetime.timedelta(hours=n)

    target_date = explicit_date or now.date()
    is_today_explicit = False

    if explicit_date is None:
        if "послезавтра" in t:
            target_date = now.date() + datetime.timedelta(days=2)
        elif "завтра" in t:
            target_date = now.date() + datetime.timedelta(days=1)
        elif "сегодня" in t:
            target_date = now.date()
            is_today_explicit = True
        else:
            for word, weekday in _WEEKDAYS_RU.items():
                if re.search(rf"\b{re.escape(word)}\b", t):
                    days_ahead = (weekday - now.weekday()) % 7
                    if days_ahead == 0:
                        days_ahead = 7
                    target_date = now.date() + datetime.timedelta(days=days_ahead)
                    break

    hour = None
    minute = 0

    # "21:00" / "21.00", "в 9:30", "на 9"
    m = re.search(r"(?:\bв\b|\bна\b)?\s*(\d{1,2})[:.](\d{2})\b", t)
    if not m:
        m = re.search(r"(?:\bв\b|\bна\b)\s+(\d{1,2})(?!\d)", t)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.lastindex and m.lastindex >= 2 and m.group(2) else 0

    # "в девять", "на девять"
    if hour is None:
        m = re.search(r"(?:\bв\b|\bна\b)\s+([а-я]+)", t)
        if m:
            hour = _RU_NUMBERS.get(m.group(1))
            minute = 0

    if hour is None:
        return None

    if any(x in t for x in ["вечера", "вечером"]) and 1 <= hour <= 11:
        hour += 12
    if any(x in t for x in ["дня", "днем"]) and 1 <= hour <= 7:
        hour += 12
    if any(x in t for x in ["утра", "утром"]) and hour == 12:
        hour = 0

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    result = datetime.datetime.combine(
        target_date,
        datetime.time(hour=hour, minute=minute),
        tzinfo=ASTANA_TZ,
    )

    if is_today_explicit:
        # Сказали "сегодня" явно — если время уже прошло, это, скорее всего,
        # ошибка/неоднозначность, а не "значит, завтра". Не подставляем.
        if result <= now:
            return None
        return result

    has_explicit_day = (
        explicit_date is not None
        or any(x in t for x in ["завтра", "послезавтра"])
        or any(re.search(rf"\b{re.escape(w)}\b", t) for w in _WEEKDAYS_RU)
    )
    if not has_explicit_day and result <= now:
        result += datetime.timedelta(days=1)

    return result

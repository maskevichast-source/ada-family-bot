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

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
    ):
        try:
            return datetime.datetime.strptime(text, fmt).replace(tzinfo=ASTANA_TZ)
        except ValueError:
            continue
    return None


def parse_ru_relative_datetime(text: str, base_now: datetime.datetime | None = None) -> datetime.datetime | None:
    """Парсит бытовые русские даты для напоминаний.

    Примеры:
    - сегодня в 21:00
    - сегодня в девять вечера
    - завтра в 9 утра
    - послезавтра в 18:30
    - в понедельник в 10
    """
    if not text:
        return None

    now = base_now or now_astana()
    t = str(text).lower().replace("ё", "е")
    target_date = now.date()

    if "послезавтра" in t:
        target_date = now.date() + datetime.timedelta(days=2)
    elif "завтра" in t:
        target_date = now.date() + datetime.timedelta(days=1)
    elif "сегодня" in t:
        target_date = now.date()
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

    # "21:00", "в 9:30", "на 9"
    m = re.search(r"(?:\bв\b|\bна\b)?\s*(\d{1,2})(?::(\d{2}))", t)
    if not m:
        m = re.search(r"(?:\bв\b|\bна\b)\s+(\d{1,2})(?!\d)", t)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) if m.lastindex and m.lastindex >= 2 and m.group(2) else 0)

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

    has_explicit_day = (
        any(x in t for x in ["сегодня", "завтра", "послезавтра"])
        or any(re.search(rf"\b{re.escape(w)}\b", t) for w in _WEEKDAYS_RU)
    )
    if not has_explicit_day and result <= now:
        result += datetime.timedelta(days=1)

    return result

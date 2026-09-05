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
    if isinstance(value, datetime.datetime):
        return value.astimezone(ASTANA_TZ) if value.tzinfo else value.replace(tzinfo=ASTANA_TZ)
    if isinstance(value, datetime.date):
        return datetime.datetime.combine(value, datetime.time(), ASTANA_TZ)
    text = str(value or "").strip()
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.astimezone(ASTANA_TZ) if dt.tzinfo else dt.replace(tzinfo=ASTANA_TZ)
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
                "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
        try:
            return datetime.datetime.strptime(text, fmt).replace(tzinfo=ASTANA_TZ)
        except ValueError:
            pass
    return None

def parse_ru_relative_datetime(text, base_now=None):
    now = base_now or now_astana()
    if not now.tzinfo:
        now = now.replace(tzinfo=ASTANA_TZ)
    t = str(text or "").lower().replace("ё", "е")
    relative = re.search(r"через\s+(\d+|полчаса|час|минуту|день|[а-я]+)(?:\s+(мин\w*|час\w*|дн\w*|день))?", t)
    if relative:
        token, unit = relative.groups()
        n = int(token) if token.isdigit() else _RU_NUMBERS.get(token)
        if token == "полчаса":
            n, unit = 30, "мин"
        elif token in ("час", "минуту", "день"):
            n, unit = 1, token
        if n and unit:
            seconds = 60 if unit.startswith("мин") else 3600 if unit.startswith("час") else 86400
            return now + datetime.timedelta(seconds=n * seconds)
        return None

    target = now.date()
    explicit = False
    date_match = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", t)
    short_date = re.search(r"(?:\bна|\bдата)\s+(\d{1,2})\.(\d{1,2})(?![.\d])", t)
    try:
        if date_match:
            d, m, y = map(int, date_match.groups())
            target = datetime.date(y, m, d)
            t = t[:date_match.start()] + t[date_match.end():]
            explicit = True
        elif short_date:
            d, m = map(int, short_date.groups())
            target = datetime.date(now.year, m, d)
            t = t[:short_date.start()] + t[short_date.end():]
            explicit = True
        elif "послезавтра" in t:
            target += datetime.timedelta(days=2); explicit = True
        elif "завтра" in t:
            target += datetime.timedelta(days=1); explicit = True
        elif "сегодня" in t:
            explicit = True
        else:
            for word, weekday in _WEEKDAYS_RU.items():
                if re.search(rf"\b{word}\b", t):
                    target += datetime.timedelta(days=(weekday-now.weekday()) % 7)
                    explicit = True
                    break
    except ValueError:
        return None
    # Reject unsupported date expressions rather than scheduling a guessed day.
    if not explicit and re.search(r"\b\d{1,2}\s+(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)|следующ", t):
        return None
    matches = list(re.finditer(r"(?<![\d.])(\d{1,2})[:.](\d{2})(?![\d.])", t))
    if len(matches) > 1:
        return None
    hour, minute = None, 0
    if matches:
        hour, minute = map(int, matches[0].groups())
    else:
        for m in re.finditer(r"\b(?:в|во|на)\s+(\d{1,2}|[а-я]+)\b", t):
            token = m.group(1)
            h = int(token) if token.isdigit() else _RU_NUMBERS.get(token)
            if h is not None:
                if hour is not None:
                    return None
                hour = h
    if hour is None:
        return None
    if re.search(r"вечер|\bдня\b|\bднем\b", t) and 1 <= hour <= 11:
        hour += 12
    if re.search(r"утр", t) and hour == 12:
        hour = 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    result = datetime.datetime.combine(target, datetime.time(hour, minute), ASTANA_TZ)
    if result <= now:
        if explicit:
            # A named weekday today means the next occurrence.
            if any(re.search(rf"\b{w}\b", t) for w in _WEEKDAYS_RU):
                result += datetime.timedelta(days=7)
            else:
                return None
        else:
            result += datetime.timedelta(days=1)
    return result


MONTH_NAMES_RU = ("январь", "февраль", "март", "апрель", "май", "июнь",
                  "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")
def month_label(value):
    return f"{MONTH_NAMES_RU[value.month-1]} {value.year}"

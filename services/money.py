"""Парсинг и форматирование денежных сумм."""

import math
import re

# "10 тыс" -> 10 * 1000, "2,5 тысячи" -> 2.5 * 1000
_MULTIPLIERS = [
    (re.compile(r'(-?\d[\d\s.,]*\d|-?\d)\s*(?:тыс\.?|тысяч[а-я]*)\b', re.IGNORECASE), 1000),
    (re.compile(r'(-?\d[\d\s.,]*\d|-?\d)\s*(?:млн\.?|миллион[а-я]*)\b', re.IGNORECASE), 1_000_000),
]

_NUMBER_RE = re.compile(r'-?\d[\d\s.,]*\d|-?\d')


def _parse_number_token(token: str) -> float:
    """Разобрать один числовой токен: '184400,50' / '184,400' / '1.234,56' / '1,234.56'."""
    token = token.strip()
    if not token:
        return 0.0
    neg = token.startswith('-')
    token = token.lstrip('-').strip().replace(' ', '')
    if not token:
        return 0.0

    seps = [m.start() for m in re.finditer(r'[.,]', token)]
    if not seps:
        try:
            val = float(token)
        except ValueError:
            val = 0.0
        return -val if neg else val

    last_pos = seps[-1]
    after = token[last_pos + 1:]
    before = token[:last_pos]
    if after.isdigit() and 1 <= len(after) <= 2:
        # Последний разделитель — десятичный (1-2 цифры после него).
        integer_clean = re.sub(r'[.,]', '', before) or '0'
        try:
            val = float(f"{integer_clean}.{after}")
        except ValueError:
            val = 0.0
    else:
        # Все разделители — тысячные (например "184,400" или "1.234.567").
        digits = re.sub(r'[.,]', '', token)
        try:
            val = float(digits) if digits else 0.0
        except ValueError:
            val = 0.0
    return -val if neg else val


def parse_amount(raw) -> float:
    """Преобразовать сумму из любого формата (в т.ч. из фразы на русском) в float.

    Поддерживает:
    - "184,400" / "184 400" -> 184400.0 (запятая/пробел как разделитель тысяч)
    - "184400,50" -> 184400.5 (запятая как десятичный разделитель — по числу
      цифр ПОСЛЕ последнего разделителя: 1-2 цифры = десятичное, иначе тысячи)
    - "1.234,56" (европейский) и "1,234.56" (американский) — оба варианта
    - "10 тыс" / "2,5 тысячи" / "1 млн" — множители словами
    - "Кола 500, чек 123" -> 500.0 (берёт первое число в тексте, а не склеивает
      его с последующим текстом через запятую)
    - float('nan') и текст без цифр -> 0.0
    """
    if raw is None:
        return 0.0
    if isinstance(raw, float) and math.isnan(raw):
        return 0.0

    text = str(raw).strip()
    if not text or text.lower() == "nan":
        return 0.0

    for pattern, mult in _MULTIPLIERS:
        m = pattern.search(text)
        if m:
            return _parse_number_token(m.group(1)) * mult

    m = _NUMBER_RE.search(text)
    if not m:
        return 0.0
    return _parse_number_token(m.group(0))


def to_clean_number(raw) -> float:
    """Алиас для parse_amount."""
    return parse_amount(raw)


def format_currency(value, currency: str = "KZT") -> str:
    """Форматировать сумму для отображения."""
    try:
        num = float(value)
        formatted = f"{num:,.0f}".replace(",", " ")
        return f"{formatted} {currency}"
    except (ValueError, TypeError):
        return str(value)


_KZT_ALIASES = {"KZT", "TENGE", "TG", "ТГ", "ТЕНГЕ", "Т", "₸"}


def normalize_currency_code(raw) -> str:
    """Свести любое обозначение тенге (₸, кириллическая "Т" — казахстанский
    символ тенге на чеках, "тг", "тенге") к единому коду "KZT". Без этого
    ИИ иногда возвращает валюту буквально как написано на чеке ("Т"), и код
    ошибочно принимал казахстанский тенге за неизвестную иностранную валюту,
    пытался узнать курс и просил прислать сумму вручную — хотя чек и так
    уже был в тенге."""
    cur = str(raw or "KZT").strip().upper()
    return "KZT" if cur in _KZT_ALIASES else cur



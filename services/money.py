"""Парсинг и форматирование денежных сумм."""

import re


def parse_amount(raw) -> float:
    """Преобразовать сумму из любого формата в float.

    Поддерживает:
    - "184,400" -> 184400.0 (запятая как разделитель тысяч)
    - "184 400" -> 184400.0 (пробел как разделитель тысяч)
    - "184400" -> 184400.0
    - "184.40" -> 184.4 (точка как десятичный разделитель)
    - "184,40" -> 184.4 (запятая как десятичный разделитель, 1-2 цифры после)
    """
    if raw is None:
        return 0.0

    text = str(raw).strip()
    if not text:
        return 0.0

    # Убираем валюту и лишние символы
    text = re.sub(r'[тгтенгеkztKZT\s]', '', text, flags=re.IGNORECASE)
    text = text.replace('₸', '')

    # Определяем: запятая/точка — разделитель тысяч или десятичный
    # Если после запятой/точки 1-2 цифры — это десятичная часть
    # Если 3+ цифры — это разделитель тысяч

    # Пробуем найти десятичную часть
    match = re.match(r'^(\d{1,3}(?:[\s,\.]\d{3})*)([\.,](\d{1,2}))?$', text)
    if match:
        integer_part = match.group(1)
        decimal_part = match.group(3) if match.group(2) else ""

        # Убираем разделители тысяч
        integer_clean = re.sub(r'[\s,\.]', '', integer_part)

        if decimal_part:
            return float(f"{integer_clean}.{decimal_part}")
        return float(integer_clean)

    # Fallback: просто убираем всё кроме цифр и точки
    clean = re.sub(r'[^\d.]', '', text)
    try:
        return float(clean) if clean else 0.0
    except ValueError:
        return 0.0


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


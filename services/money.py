"""Разбор денежных сумм, устойчивый к разным разделителям тысяч/десятичных.

Баг, который это чинит: пользователь пишет "184,400тг" (запятая как
разделитель ТЫСЯЧ — обычное дело в казахстанском контексте). Наивный
`float(str.replace(",", "."))` превращает это в "184.400", то есть в
число 184.4 — а не 184 400, как имелось в виду. На выходе транзакция
записывалась на 0 (после дальнейшего некорректного округления/парсинга).

Правило, которое мы используем: если после запятой/точки идут РОВНО три
цифры и больше ничего — это разделитель тысяч, убираем его. Если после
запятой/точки одна-две цифры — это десятичная часть (копейки/тиыны).
"""

import re

_CURRENCY_SUFFIXES = ("тенге", "kzt", "тг.", "тг", "₸", "руб.", "руб", "rub", "₽", "usd", "$")


def parse_amount(raw) -> float:
    """Превратить произвольную строку/число суммы в float. Никогда не бросает исключение."""
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)

    text = str(raw).strip()
    if not text:
        return 0.0

    text = text.replace(" ", "").replace("\u00a0", "")  # обычные и неразрывные пробелы
    lowered = text.lower()
    for suffix in _CURRENCY_SUFFIXES:
        if lowered.endswith(suffix):
            text = text[: len(text) - len(suffix)]
            lowered = text.lower()
            break
    text = text.strip()
    if not text:
        return 0.0

    # "184,400" / "184.400" — ровно 3 цифры после разделителя = разделитель тысяч, убираем.
    match = re.fullmatch(r"(\d{1,3})[.,](\d{3})", text)
    if match:
        text = match.group(1) + match.group(2)
    else:
        # "1,234,567" / "1.234.567" — несколько групп по 3 цифры подряд.
        match = re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", text)
        if match:
            text = re.sub(r"[.,]", "", text)
        else:
            # Одна запятая НЕ с тремя цифрами после — это десятичный разделитель.
            text = text.replace(",", ".")

    try:
        return float(text)
    except ValueError:
        return 0.0


def to_clean_number(raw):
    """Как parse_amount, но возвращает int, если число целое — для аккуратного вывода без ".0"."""
    value = parse_amount(raw)
    return int(value) if value == int(value) else value

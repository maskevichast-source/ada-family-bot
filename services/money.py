"""Money parsing: preserve signs and decimals; never concatenate unrelated numbers."""
import math
import re

def parse_amount(raw) -> float:
    if isinstance(raw, (int, float)):
        return float(raw) if math.isfinite(raw) else 0.0
    text = str(raw or "").replace("\u00a0", " ").replace("\u202f", " ").strip()
    m = re.search(r"(?<!\d)[+-]?\d+(?:[ .,]\d+)*", text)
    if not m:
        return 0.0
    number = m.group().strip().replace(" ", "")
    if "." in number and "," in number:
        decimal = "." if number.rfind(".") > number.rfind(",") else ","
        thousands = "," if decimal == "." else "."
        number = number.replace(thousands, "").replace(decimal, ".")
    elif "," in number or "." in number:
        separator = "," if "," in number else "."
        chunks = number.split(separator)
        if len(chunks) == 2 and len(chunks[-1]) <= 2:
            number = ".".join(chunks)
        elif all(len(x) == 3 for x in chunks[1:]):
            number = "".join(chunks)
        else:
            return 0.0
    try:
        amount = float(number)
        tail = text[m.end():].strip().lower()
        if re.match(r"^(?:тыс\b|тысяч\w*\b|к\b|k\b)", tail):
            amount *= 1000
        return amount if math.isfinite(amount) else 0.0
    except ValueError:
        return 0.0

def to_clean_number(raw):
    return parse_amount(raw)

def format_currency(value, currency="KZT"):
    amount = parse_amount(value)
    places = 0 if amount.is_integer() else 2
    return f"{amount:,.{places}f} {currency}".replace(",", " ")

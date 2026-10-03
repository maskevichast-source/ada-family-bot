"""Правка записи кнопками («✏️ Изменить»): что можно менять и как это превращается в запись в таблице.

Чистая логика без Telegram: варианты выбора (банки, категории), разбор введённых значений
(сумма, дата, текст) и сборка набора изменений для sheets.update_transaction_fields.
Производные поля пересчитывает код, а не модель: смена банка меняет source, resource и тип
средств («Рассрочка» только для карт рассрочки); смена категории сбрасывает подкатегорию на
первую из справочника services/categories.py (её можно уточнить следующим шагом).
"""
import datetime
import re

from services.banks import funds_type_for_source, normalize_bank_source
from services.categories import (
    EXPENSE_CATEGORIES, INCOME_CATEGORIES, SUBCATEGORIES_MAP, is_income_type,
)
from services.loan_rules import LOAN_SUBCATEGORY
from services.money import parse_amount
from services.timezone import ASTANA_TZ, now_astana, parse_flexible_datetime

# Поля, которые можно менять: код → подпись на кнопке. Порядок = порядок кнопок.
FIELDS = [
    ("b", "🏦 Банк"),
    ("c", "🗂 Категория"),
    ("a", "💰 Сумма"),
    ("m", "🏪 Магазин"),
    ("n", "💬 Комментарий"),
    ("d", "📅 Дата"),
    ("u", "👤 Кто"),
    ("w", "⚖️ Нужность"),
]
FIELD_LABELS = dict(FIELDS)
TEXT_FIELDS = {"a", "m", "n", "d"}          # значение вводится текстом
CHOICE_FIELDS = {"b", "c", "u", "w"}        # значение выбирается кнопкой

FAMILY_NAMES = ["Влад", "Диана"]
NECESSITY_CHOICES = [("Нужное", "Need"), ("Хотелка", "Want")]
MAX_TEXT_LEN = 200
MAX_AMOUNT = 1_000_000_000

# Банки и карты на выбор: (подпись, bank, source, resource). Значения — как их пишет бот
# при распознавании (services/banks.py), чтобы правка не плодила новые названия.
BANK_CHOICES = [
    ("Kaspi Gold", "Kaspi", "Kaspi Gold", "Карта"),
    ("Kaspi Red", "Kaspi", "Kaspi Red", "Карта"),
    ("Halyk", "Halyk", "Halyk Card", "Карта"),
    ("Halyk рассрочка", "Halyk", "Halyk Рассрочка", "Карта"),
    ("Forte", "Forte", "Forte Card", "Карта"),
    ("ForteBlack", "Forte", "ForteBlack", "Карта"),
    ("BCC", "BCC", "BCC Pay", "Карта"),
    ("BCC Картакарта", "BCC", "Картакарта", "Карта"),
    ("Freedom", "Freedom", "Freedom Card", "Карта"),
    ("Home Credit Ozen", "Home Credit", "Ozen", "Карта"),
    ("Bereke", "Bereke", "Bereke Card", "Карта"),
    ("Евразийский", "Евразийский", "Евразийский Card", "Карта"),
    ("Евразийский SmartCard", "Евразийский", "SmartCard", "Карта"),
    ("Bank RBK", "Bank RBK", "Bank RBK Card", "Карта"),
    ("Нурбанк", "Нурбанк", "Нурбанк Card", "Карта"),
    ("Altyn Bank", "Altyn Bank", "Altyn Card", "Карта"),
    ("Alatau City Bank", "Alatau City Bank", "Alatau Card", "Карта"),
    ("Наличные", "Не указан", "Основная карта", "Наличные"),
    ("Не указан", "Не указан", "Основная карта", "Карта"),
]


def category_choices(record: dict) -> list[str]:
    return INCOME_CATEGORIES if is_income_type(record.get("type")) else EXPENSE_CATEGORIES


def subcategory_choices(category: str) -> list[str]:
    return SUBCATEGORIES_MAP.get(category, [])


def bank_changes(record: dict, index: int) -> dict:
    """Изменения при выборе банка №index. Тип средств пересчитывается кодом по карте."""
    _, bank, source, resource = BANK_CHOICES[index]
    changes = {"bank": bank, "source": normalize_bank_source(bank, source), "resource": resource}
    if not is_income_type(record.get("type")):
        installment = funds_type_for_source(changes["source"])
        if installment and str(record.get("subcategory") or "").strip() != LOAN_SUBCATEGORY:
            changes["funds_type"] = installment
        elif str(record.get("funds_type") or "").strip() == "Рассрочка":
            changes["funds_type"] = "Собственные"
    return changes


def category_changes(record: dict, index: int) -> dict:
    """Новая категория; подкатегория — первая из справочника (для доходов подкатегорий нет)."""
    category = category_choices(record)[index]
    subs = [] if is_income_type(record.get("type")) else subcategory_choices(category)
    return {"category": category, "subcategory": subs[0] if subs else ""}


def subcategory_changes(record: dict, index: int) -> dict:
    subs = subcategory_choices(str(record.get("category") or ""))
    return {"subcategory": subs[index]}


def who_changes(index: int) -> dict:
    return {"user": FAMILY_NAMES[index]}


def necessity_changes(index: int) -> dict:
    return {"necessity": NECESSITY_CHOICES[index][1]}


# ── разбор введённого текста ───────────────────────────────────────────────

_AMOUNT_ONLY_RE = re.compile(r"^[\d\s.,]+(?:тыс\.?|тысяч[а-я]*|к)?$", re.IGNORECASE)


def parse_amount_input(text: str):
    """(сумма, None) или (None, текст ошибки). Принимает только «чистую» сумму: «1750», «1 750 ₸»,
    «2,5 тыс». Фраза вроде «кофе 500» не подходит — чтобы новая трата не превратилась в правку."""
    cleaned = re.sub(r"(?i)\b(?:тенге|тг|kzt)\b|[₸т]\.?$", "", str(text or "")).strip()
    if not cleaned or not _AMOUNT_ONLY_RE.match(cleaned):
        return None, "Не поняла сумму. Напиши только число, например 1750."
    if cleaned.lower().endswith("к"):
        cleaned = cleaned[:-1].strip() + " тыс"
    value = parse_amount(cleaned)
    if value <= 0 or value > MAX_AMOUNT:
        return None, "Сумма должна быть больше нуля. Напиши число, например 1750."
    return (int(value) if value == int(value) else round(value, 2)), None


# Время «15:41» или «15.41», но не кусок даты «06.09» из «06.09.2026».
_TIME_RE = re.compile(r"(?<![\d.])(\d{1,2})[:.](\d{2})(?![\d.])")


def parse_date_input(text: str, current: str = "", now: datetime.datetime | None = None):
    """(строка даты для таблицы, None) или (None, ошибка). Форматы: «06.09.2026 15:41», «06.09.2026»,
    «2026-09-06», «вчера», «сегодня» (с необязательным временем). Дата без времени сохраняет
    время записи; будущее не принимается."""
    now = now or now_astana()
    raw = " ".join(str(text or "").lower().split())
    old = parse_flexible_datetime(current) if current else None
    parsed = None
    day_word = "вчера" if "вчера" in raw else ("сегодня" if "сегодня" in raw else "")
    if day_word:
        day = now.date() - datetime.timedelta(days=1 if day_word == "вчера" else 0)
        match = _TIME_RE.search(raw)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
        else:
            hour, minute = (old.hour, old.minute) if old else (12, 0)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            parsed = datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=ASTANA_TZ)
    else:
        parsed = parse_flexible_datetime(raw)
        if parsed and not _TIME_RE.search(raw) and old:
            parsed = parsed.replace(hour=old.hour, minute=old.minute, second=old.second)
    if not parsed:
        return None, "Не поняла дату. Напиши, например: 06.09.2026 15:41, 06.09.2026 или «вчера»."
    if parsed > now + datetime.timedelta(minutes=15):
        return None, "Дата в будущем. Напиши прошедшую дату."
    if parsed < now - datetime.timedelta(days=3 * 366):
        return None, "Слишком старая дата. Напиши дату не старше трёх лет."
    return parsed.strftime("%Y-%m-%d %H:%M:%S"), None


def parse_text_input(text: str):
    """(текст, None) или (None, ошибка). «-» или «нет» очищают поле."""
    value = " ".join(str(text or "").split())
    if value.lower() in {"-", "нет", "убери", "очисти"}:
        return "", None
    if not value:
        return None, "Пустое значение. Напиши текст или «-», чтобы очистить поле."
    if len(value) > MAX_TEXT_LEN:
        return None, f"Слишком длинно (больше {MAX_TEXT_LEN} символов). Напиши короче."
    return value, None


def changes_for_text_field(field: str, text: str, record: dict):
    """(изменения, None) или (None, ошибка) для полей, вводимых текстом."""
    if field == "a":
        value, error = parse_amount_input(text)
        return ({"amount": value}, None) if error is None else (None, error)
    if field == "d":
        value, error = parse_date_input(text, str(record.get("date") or ""))
        return ({"date": value}, None) if error is None else (None, error)
    if field in ("m", "n"):
        value, error = parse_text_input(text)
        column = "merchant" if field == "m" else "user_comment"
        return ({column: value}, None) if error is None else (None, error)
    return None, "Это поле меняется кнопками."


# ── показ записи ──────────────────────────────────────────────────────────

def _money(value) -> str:
    try:
        return f"{float(str(value).replace(' ', '').replace(',', '.')):,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def _date_text(value) -> str:
    parsed = parse_flexible_datetime(value) if value else None
    return parsed.strftime("%d.%m.%Y %H:%M") if parsed else str(value or "")


def describe(record: dict) -> str:
    """Компактное описание записи для сообщения (простой текст)."""
    income = is_income_type(record.get("type"))
    card = "Наличные" if str(record.get("resource") or "") == "Наличные" else \
        str(record.get("source") or record.get("bank") or "Не указан")
    lines = [f"{'+ ' if income else ''}{_money(record.get('amount'))} ₸ · {card}"]
    category = str(record.get("category") or "").strip()
    subcategory = str(record.get("subcategory") or "").strip()
    if category:
        lines.append(f"Категория: {category}" + (f" › {subcategory}" if subcategory else ""))
    for label, key in (("Магазин", "merchant"), ("Комментарий", "user_comment")):
        value = str(record.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    if record.get("date"):
        lines.append(f"Дата: {_date_text(record.get('date'))}")
    if record.get("user"):
        lines.append(f"Кто: {record.get('user')}")
    return "\n".join(lines)


def position_label(record: dict) -> str:
    """Подпись кнопки выбора позиции чека."""
    what = str(record.get("merchant") or record.get("user_comment") or record.get("category") or "").strip()
    text = f"{_money(record.get('amount'))} ₸ · {what}" if what else f"{_money(record.get('amount'))} ₸"
    return text if len(text) <= 40 else text[:39] + "…"

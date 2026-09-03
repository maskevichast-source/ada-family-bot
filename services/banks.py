"""Единый словарь банков (Казахстан).

Раньше этот словарь жил только в vision.py (для фото/PDF чеков) — из-за
этого распознавание ТЕКСТОВЫХ банковских push-уведомлений через DeepSeek
не имело вообще никакой подсказки про банки/карту-vs-наличные. Теперь оба
места (vision.py и deepseek_service.py) берут текст отсюда, чтобы не
разъехаться, как уже один раз случилось со списком категорий.
"""

BANK_ALIASES_PROMPT = """СЛОВАРЬ БАНКОВ (Казахстан) — сопоставляй похожие названия/форматы с этим списком:
- BCC, ЦентрКредит, bccpay → bank: "BCC", source: "BCC Pay"
- Kaspi Gold, Kaspi → bank: "Kaspi", source: "Kaspi Gold"
- Kaspi Red → bank: "Kaspi", source: "Kaspi Red", funds_type: "Рассрочка"
- Forte → bank: "Forte", source: "Forte Card"
- Halyk, Народный → bank: "Halyk", source: "Halyk Card"
- Freedom, FFIN → bank: "Freedom", source: "Freedom Card"
- Если банк не указан или не распознан → bank: "Не указан", source: "Основная карта"
- Если оплата наличными → resource: "Наличные", bank: "Не указан", source: "Основная карта"
  (НИКОГДА не пиши "Наличные" в поле bank — это resource, а не bank!)"""

# Структурированная версия того же словаря — для проверки в коде, а не только
# в промпте. Промпт один раз уже не уберёг: ИИ записал source как "Kaspi.kz"
# (буквально скопировав название интернет-магазина) вместо "Kaspi Gold".
# Ключи — в нижнем регистре, для регистронезависимого сравнения.
KNOWN_SOURCES_BY_BANK = {
    "bcc": "BCC Pay",
    "kaspi": "Kaspi Gold",
    "forte": "Forte Card",
    "halyk": "Halyk Card",
    "freedom": "Freedom Card",
}
# Отдельные источники, которые сами по себе валидны и НЕ должны заменяться
# (например Kaspi Red — рассрочка, это не то же самое, что обычный Kaspi Gold).
VALID_SOURCES_LOWER = {"bcc pay", "kaspi gold", "kaspi red", "forte card", "halyk card", "freedom card", "основная карта"}


def normalize_bank_source(bank: str | None, source: str | None) -> str:
    """Привести "source" к одному из известных значений, если оно похоже на
    что-то другое (например, домен/название магазина) — не трогая уже
    валидные источники вроде "Kaspi Red".

    Также обрабатывает случай наличных: если bank == "Наличные" — 
    возвращает "Основная карта" как source, а bank должен быть "Не указан".
    """
    source_norm = str(source or "").strip()
    if source_norm.lower() in VALID_SOURCES_LOWER:
        return source_norm

    bank_norm = str(bank or "").strip().lower()

    # Обработка наличных
    if bank_norm in ("наличные", "нал", "cash"):
        return "Основная карта"

    if bank_norm in KNOWN_SOURCES_BY_BANK:
        return KNOWN_SOURCES_BY_BANK[bank_norm]

    return source_norm or "Основная карта"

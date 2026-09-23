"""Единый словарь банков (Казахстан).

Раньше этот словарь жил только в vision.py (для фото/PDF чеков) — из-за
этого распознавание ТЕКСТОВЫХ банковских push-уведомлений через DeepSeek
не имело вообще никакой подсказки про банки/карту-vs-наличные. Теперь оба
места (vision.py и deepseek_service.py) берут текст отсюда, чтобы не
разъехаться, как уже один раз случилось со списком категорий.
"""

BANK_ALIASES_PROMPT = """СЛОВАРЬ БАНКОВ (Казахстан) — сопоставляй похожие названия/форматы с этим списком:
- BCC, ЦентрКредит, bccpay → bank: "BCC", source: "BCC Pay"
- BCC #Картакарта, Картакарта → bank: "BCC", source: "Картакарта", funds_type: "Рассрочка"
- Kaspi Gold, Kaspi → bank: "Kaspi", source: "Kaspi Gold"
- Kaspi Red → bank: "Kaspi", source: "Kaspi Red", funds_type: "Рассрочка"
- Forte → bank: "Forte", source: "Forte Card"
- ForteBlack → bank: "Forte", source: "ForteBlack", funds_type: "Рассрочка"
- Halyk, Народный, Homebank → bank: "Halyk", source: "Halyk Card"
- Halyk рассрочка → bank: "Halyk", source: "Halyk Рассрочка", funds_type: "Рассрочка"
- Freedom, FFIN → bank: "Freedom", source: "Freedom Card"
- Ozen, Özen, Home Credit, HCB, Хоум Кредит → bank: "Home Credit", source: "Ozen", funds_type: "Рассрочка"
  (карта рассрочки Home Credit Bank — на чеке обычно видно "Home Credit OZEN" или просто "OZEN")
- Bereke, Береке (бывший Сбербанк РК) → bank: "Bereke", source: "Bereke Card"
- Евразийский, Eurasian Bank → bank: "Евразийский", source: "Евразийский Card"
- Евразийский SmartCard, PayDa → bank: "Евразийский", source: "SmartCard", funds_type: "Рассрочка"
- Bank RBK, РБК → bank: "Bank RBK", source: "Bank RBK Card"
- Нурбанк, Nurbank → bank: "Нурбанк", source: "Нурбанк Card"
- Altyn Bank, Алтын Банк → bank: "Altyn Bank", source: "Altyn Card"
- Alatau City Bank, Алатау → bank: "Alatau City Bank", source: "Alatau Card"
- Если оплата наличными → resource: "Наличные", bank: "Не указан", source: "Основная карта"
  (НИКОГДА не пиши "Наличные" в поле bank — это resource, а не bank!)
- Если банк ЯВНО НЕ ВИДЕН на чеке/тексте вообще (нет ни названия, ни логотипа) →
  bank: "Не указан", source: "Основная карта"
- Если на чеке/тексте ЕСТЬ название банка или карты, но оно НЕ совпадает ни с одним
  из вышеперечисленных (новый/незнакомый банк) → НЕ пиши "Не указан". Вместо этого
  впиши название банка ТАК, КАК ОНО НАПИСАНО на чеке, и в bank, и в source — дословно.
  Так эти операции не потеряются и их легко найти и доразметить позже."""

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
    "home credit": "Ozen",
    "bereke": "Bereke Card",
    "евразийский": "Евразийский Card",
    "eurasian": "Евразийский Card",
    "bank rbk": "Bank RBK Card",
    "нурбанк": "Нурбанк Card",
    "nurbank": "Нурбанк Card",
    "altyn bank": "Altyn Card",
    "alatau city bank": "Alatau Card",
}
# Отдельные источники, которые сами по себе валидны и НЕ должны заменяться
# (например Kaspi Red — рассрочка, это не то же самое, что обычный Kaspi Gold).
VALID_SOURCES_LOWER = {
    "bcc pay", "картакарта", "kaspi gold", "kaspi red", "forte card", "forteblack",
    "halyk card", "halyk рассрочка", "freedom card", "ozen", "основная карта",
    "bereke card", "евразийский card", "smartcard", "payda",
    "bank rbk card", "нурбанк card", "altyn card", "alatau card",
}


BANK_NAME_ALIASES = {
    "bcc": "BCC", "бцк": "BCC", "центркредit": "BCC", "центркредит": "BCC",
    "kaspi": "Kaspi", "каспи": "Kaspi", "kaspi gold": "Kaspi", "каспи голд": "Kaspi",
    "kaspi red": "Kaspi", "каспи ред": "Kaspi",
    "forte": "Forte", "форте": "Forte",
    "halyk": "Halyk", "халык": "Halyk", "народный": "Halyk", "халик": "Halyk",
    "freedom": "Freedom", "фридом": "Freedom", "ffin": "Freedom",
    "ozen": "Home Credit", "özen": "Home Credit", "озен": "Home Credit",
    "home credit": "Home Credit", "хоум кредит": "Home Credit", "hcb": "Home Credit",
    "bereke": "Bereke", "береке": "Bereke",
    "евразийский": "Евразийский", "евразийский банк": "Евразийский", "eurasian": "Евразийский", "eurasian bank": "Евразийский",
    "bank rbk": "Bank RBK", "рбк": "Bank RBK", "рбк банк": "Bank RBK",
    "нурбанк": "Нурбанк", "nurbank": "Нурбанк",
    "altyn bank": "Altyn Bank", "altyn": "Altyn Bank", "алтын банк": "Altyn Bank", "алтын": "Altyn Bank",
    "alatau city bank": "Alatau City Bank", "alatau": "Alatau City Bank", "алатау": "Alatau City Bank", "алатау сити банк": "Alatau City Bank",
    "нал": "Наличные", "наличные": "Наличные", "cash": "Наличные",
}


def canonical_bank_name(raw: str | None) -> str | None:
    """Свободный ввод банка ("халык", "каспи голд", "BCC") -> каноничное имя.

    None, если не распознали — вызывающий код тогда не должен молча
    игнорировать команду, а должен переспросить.
    """
    key = str(raw or "").strip().lower().replace("ё", "е")
    return BANK_NAME_ALIASES.get(key)


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



"""Единый справочник категорий, подкатегорий и контекстных правил семьи."""

TYPE_EXPENSE = "РАСХОД"
TYPE_INCOME = "ДОХОД"


def is_income_type(raw_type: object) -> bool:
    """Универсальная проверка дохода вне зависимости от регистра и языка."""
    if not raw_type:
        return False
    val = str(raw_type).strip().lower()
    return val in {"доход", "income", "in", "+", "пополнение", "зачисление"}


# 19 категорий расходов
EXPENSE_CATEGORIES: list[str] = [
    "Еда и продукты",
    "Кафе, рестораны и доставка еды",
    "Алкоголь, табак и энергетики",
    "Транспорт и авто",
    "Жильё и коммунальные услуги",
    "Связь и подписки",
    "Дом и быт",
    "Здоровье и медицина",
    "Красота и уход",
    "Одежда и обувь",
    "Спорт и фитнес",
    "Образование",
    "Электроника и техника",
    "Развлечения и хобби",
    "Путешествия",
    "Питомцы",
    "Подарки, праздники и благотворительность",
    "Финансовые расходы и переводы",
    "Обязательные платежи и прочее",
]

# Строгие подкатегории (для построения иерархий в Power BI)
SUBCATEGORIES_MAP: dict[str, list[str]] = {
    "Еда и продукты": [
        "Супермаркет и рынок", "Напитки и вода", "Мясо и полуфабрикаты",
        "Выпечка и хлеб", "Снеки и сладости"
    ],
    "Кафе, рестораны и доставка еды": [
        "Перекус и фастфуд", "Кафе и рестораны", "Доставка готовой еды", "Кофе и напитки на вынос"
    ],
    "Алкоголь, табак и энергетики": [
        "Сигареты и стики", "Энергетики", "Алкоголь и пиво"
    ],
    "Транспорт и авто": [
        "Общественный транспорт", "Такси", "Бензин и сервис", "Доставка курьером"
    ],
    "Жильё и коммунальные услуги": [
        "Коммунальные услуги и КСК", "Аренда и жилье", "Лифт и ключ-карты"
    ],
    "Связь и подписки": [
        "Мобильная связь и интернет", "Цифровые подписки и сервисы"
    ],
    "Дом и быт": [
        "Ремонт и стройматериалы", "Инструменты и крепеж", "Текстиль и шторы",
        "Товары для дома", "Бытовая химия"
    ],
    "Здоровье и медицина": [
        "Аптека и лекарства", "Медтехника и расходники", "Врачи и анализы", "Витамины и БАДы"
    ],
    "Красота и уход": [
        "Стрижка и барбершоп", "Косметика и парфюмерия", "Салонные услуги"
    ],
    "Одежда и обувь": [
        "Одежда", "Обувь", "Аксессуары"
    ],
    "Спорт и фитнес": [
        "Абонементы и залы", "Спортинвентарь"
    ],
    "Образование": [
        "Курсы и обучение", "Книги"
    ],
    "Электроника и техника": [
        "Гаджеты и техника", "Ремонт техники и аксессуары"
    ],
    "Развлечения и хобби": [
        "Кино и мероприятия", "Игры и хобби"
    ],
    "Путешествия": [
        "Билеты и отели", "Расходы в поездках"
    ],
    "Питомцы": [
        "Корм и лакомства", "Ветеринария и уход"
    ],
    "Подарки, праздники и благотворительность": [
        "Подарки", "Праздники", "Благотворительность"
    ],
    "Финансовые расходы и переводы": [
        "Переводы физлицам", "Банковские комиссии"
    ],
    "Обязательные платежи и прочее": [
        "Штрафы и пошлины", "Налоги", "Непредвиденное"
    ],
}

SUBCATEGORY_TO_CATEGORY: dict[str, str] = {}
for _cat, _subs in SUBCATEGORIES_MAP.items():
    for _sub in _subs:
        SUBCATEGORY_TO_CATEGORY[_sub] = _cat

INCOME_CATEGORIES: list[str] = [
    "Зарплата", "Премия и бонусы", "Фриланс и подработка", "Бизнес-доход",
    "Инвестиции и дивиденды", "Возврат долга", "Подарки и переводы (входящие)",
    "Кэшбэк и прочие поступления",
]

DEFAULT_EXPENSE_LIMITS: dict[str, int] = {
    "Еда и продукты": 150000,
    "Кафе, рестораны и доставка еды": 80000,
    "Алкоголь, табак и энергетики": 20000,
    "Транспорт и авто": 70000,
    "Жильё и коммунальные услуги": 150000,
    "Связь и подписки": 25000,
    "Дом и быт": 40000,
    "Здоровье и медицина": 30000,
    "Красота и уход": 30000,
    "Одежда и обувь": 40000,
    "Спорт и фитнес": 25000,
    "Образование": 30000,
    "Электроника и техника": 30000,
    "Развлечения и хобби": 50000,
    "Путешествия": 50000,
    "Питомцы": 20000,
    "Подарки, праздники и благотворительность": 25000,
    "Финансовые расходы и переводы": 15000,
    "Обязательные платежи и прочее": 25000,
}

AMBIGUOUS_TRIGGERS: dict[str, list[dict]] = {
    "сендвич": [
        {"label": "🍔 Перекус на работе / кафе", "category": "Кафе, рестораны и доставка еды", "subcategory": "Перекус и фастфуд", "necessity": "Want"},
        {"label": "🏠 Еда домой (продукты)", "category": "Еда и продукты", "subcategory": "Супермаркет и рынок", "necessity": "Need"}
    ],
    "сэндвич": [
        {"label": "🍔 Перекус на работе / кафе", "category": "Кафе, рестораны и доставка еды", "subcategory": "Перекус и фастфуд", "necessity": "Want"},
        {"label": "🏠 Еда домой (продукты)", "category": "Еда и продукты", "subcategory": "Супермаркет и рынок", "necessity": "Need"}
    ],
    "самса": [
        {"label": "🍔 Перекус / кафе", "category": "Кафе, рестораны и доставка еды", "subcategory": "Перекус и фастфуд", "necessity": "Want"},
        {"label": "🏠 Ужин / еда домой", "category": "Еда и продукты", "subcategory": "Супермаркет и рынок", "necessity": "Need"}
    ],
    "пицца": [
        {"label": "🍕 Доставка / кафе", "category": "Кафе, рестораны и доставка еды", "subcategory": "Доставка готовой еды", "necessity": "Want"},
        {"label": "🏠 Полуфабрикат домой", "category": "Еда и продукты", "subcategory": "Мясо и полуфабрикаты", "necessity": "Need"}
    ],
    "бургер": [
        {"label": "🍔 В заведении / фастфуд", "category": "Кафе, рестораны и доставка еды", "subcategory": "Перекус и фастфуд", "necessity": "Want"},
        {"label": "🏠 Домой", "category": "Еда и продукты", "subcategory": "Супермаркет и рынок", "necessity": "Need"}
    ],
    "шаурма": [
        {"label": "🌯 Фастфуд на вынос", "category": "Кафе, рестораны и доставка еды", "subcategory": "Перекус и фастфуд", "necessity": "Want"},
        {"label": "🏠 Еда домой", "category": "Еда и продукты", "subcategory": "Супермаркет и рынок", "necessity": "Need"}
    ],
    "донер": [
        {"label": "🌯 Фастфуд на вынос", "category": "Кафе, рестораны и доставка еды", "subcategory": "Перекус и фастфуд", "necessity": "Want"},
        {"label": "🏠 Еда домой", "category": "Еда и продукты", "subcategory": "Супермаркет и рынок", "necessity": "Need"}
    ],
    "кофе": [
        {"label": "☕ Кофе на вынос / в кафе", "category": "Кафе, рестораны и доставка еды", "subcategory": "Кофе и напитки на вынос", "necessity": "Want"},
        {"label": "🏠 Пачка кофе домой", "category": "Еда и продукты", "subcategory": "Напитки и вода", "necessity": "Need"}
    ],
    "кроссовки": [
        {"label": "👟 Обычная обувь на каждый день", "category": "Одежда и обувь", "subcategory": "Обувь", "necessity": "Need"},
        {"label": "🏃 Спортивная обувь для зала/бега", "category": "Спорт и фитнес", "subcategory": "Спортинвентарь", "necessity": "Want"}
    ],
    "массаж": [
        {"label": "🏥 Лечебный массаж по назначению", "category": "Здоровье и медицина", "subcategory": "Врачи и анализы", "necessity": "Need"},
        {"label": "💆 Релакс / СПА / Уход", "category": "Красота и уход", "subcategory": "Салонные услуги", "necessity": "Want"}
    ],
    "яндекс": [
        {"label": "🚕 Поездка на такси", "category": "Транспорт и авто", "subcategory": "Такси", "necessity": "Need"},
        {"label": "📦 Доставка курьером (груз/объект)", "category": "Транспорт и авто", "subcategory": "Доставка курьером", "necessity": "Need"}
    ],
}

FALLBACK_EXPENSE_CATEGORY = "Обязательные платежи и прочее"
FALLBACK_INCOME_CATEGORY = "Кэшбэк и прочие поступления"


def get_ambiguous_options(text: str) -> list[dict] | None:
    text_lower = text.lower()
    clear_markers = [
        "на работу", "на работе", "с собой", "в офис", "на объект", "в кафе",
        "на ходу", "домой", "в дом", "дома", "для дома", "продукты", "семье",
        "для зала", "для бега", "на каждый день", "по назначению врача", "такси"
    ]
    if any(m in text_lower for m in clear_markers):
        return None

    for trigger, options in AMBIGUOUS_TRIGGERS.items():
        if trigger in text_lower:
            return options
    return None


def is_ambiguous_item(text: str) -> tuple[bool, list[str]]:
    options = get_ambiguous_options(text)
    if options:
        return True, [opt["category"] for opt in options]
    return False, []


def format_category_list(categories: list[str]) -> str:
    return "\n".join(f"{index}. {name}" for index, name in enumerate(categories, start=1))


def normalize_category(raw: str | None, categories: list[str], fallback: str) -> str:
    if not raw:
        return fallback
    raw_norm = str(raw).strip().lower()

    aliases = {
        "алкоголь и табак": "Алкоголь, табак и энергетики",
        "алкоголь, табак": "Алкоголь, табак и энергетики",
        "энергетики": "Алкоголь, табак и энергетики",
        "табак": "Алкоголь, табак и энергетики",
        "сигареты": "Алкоголь, табак и энергетики",
        "прочие": "Обязательные платежи и прочее",
        "прочее": "Обязательные платежи и прочее",
        "кафе и рестораны": "Кафе, рестораны и доставка еды",
        "рестораны": "Кафе, рестораны и доставка еды",
        "доставка еды": "Кафе, рестораны и доставка еды",
        "еда и продукты": "Еда и продукты",
        "продукты": "Еда и продукты",
        "супермаркет": "Еда и продукты",
        "транспорт и авто": "Транспорт и авто",
        "транспорт": "Транспорт и авто",
        "авто": "Транспорт и авто",
        "такси": "Транспорт и авто",
        "ремонт": "Дом и быт",
        "стройматериалы": "Дом и быт",
        "стройка": "Дом и быт",
        "инструменты": "Дом и быт",
        "дом и быт": "Дом и быт",
        "быт": "Дом и быт",
        "жилье и кск": "Жильё и коммунальные услуги",
        "жилье": "Жильё и коммунальные услуги",
        "коммуналка": "Жильё и коммунальные услуги",
        "коммунальные услуги": "Жильё и коммунальные услуги",
        "кск": "Жильё и коммунальные услуги",
        "аренда": "Жильё и коммунальные услуги",
        "квартплата": "Жильё и коммунальные услуги",
        "связь": "Связь и подписки",
        "подписки": "Связь и подписки",
        "интернет": "Связь и подписки",
        "мобильная связь": "Связь и подписки",
    }

    if raw_norm in aliases:
        target = aliases[raw_norm]
        if target in categories:
            return target

    for cat in categories:
        if cat.lower() == raw_norm:
            return cat

    if len(raw_norm) >= 4:
        for cat in categories:
            if raw_norm in cat.lower() or cat.lower() in raw_norm:
                return cat

    return fallback


def normalize_subcategory(raw: str | None, category: str | None, fallback: str = "") -> str:
    if not raw or not category:
        return fallback
    raw_sub = str(raw).strip().lower()
    valid_subs = SUBCATEGORIES_MAP.get(category, [])
    for sub in valid_subs:
        if sub.lower() == raw_sub:
            return sub
    if len(raw_sub) >= 4:
        for sub in valid_subs:
            if raw_sub in sub.lower() or sub.lower() in raw_sub:
                return sub
    return valid_subs[0] if valid_subs else fallback


def validate_transaction_category_subcategory(
    category: str,
    subcategory: str | None,
    categories: list[str] | None = None,
    fallback: str | None = None,
) -> tuple[str, str]:
    categories = categories or EXPENSE_CATEGORIES
    fallback = fallback or (FALLBACK_INCOME_CATEGORY if categories is INCOME_CATEGORIES else FALLBACK_EXPENSE_CATEGORY)
    
    sub_raw = str(subcategory or "").strip()
    if sub_raw in SUBCATEGORY_TO_CATEGORY:
        correct_parent = SUBCATEGORY_TO_CATEGORY[sub_raw]
        if correct_parent in categories:
            return correct_parent, sub_raw

    norm_cat = normalize_category(category, categories, fallback)
    valid_subs = SUBCATEGORIES_MAP.get(norm_cat, [])
    if not valid_subs:
        return norm_cat, ""

    for sub in valid_subs:
        if sub.lower() == sub_raw.lower():
            return norm_cat, sub

    return norm_cat, valid_subs[0]


def get_time_context_hint(hour: int, weekday: int) -> str:
    hints = []
    is_weekend = weekday in (0, 6)
    if is_weekend:
        hints.append("СЕГОДНЯ ВЫХОДНОЙ ДЕНЬ ВЛАДА И ДИАНЫ (воскресенье/понедельник). Семья дома или отдыхает.")
    else:
        hints.append("СЕГОДНЯ РАБОЧИЙ ДЕНЬ ВЛАДА И ДИАНЫ (вторник-суббота). Перекусы, кофе вне дома днем — это 'Кафе и перекус'.")

    if 6 <= hour < 11:
        hints.append("утро — дорога, кофе, энергетики, завтрак")
    elif 11 <= hour < 15:
        hints.append("обед — ланч, перекус на работе, доставка, кафе")
    elif 15 <= hour < 19:
        hints.append("после обеда — покупки для дома/ремонта, аптека, рабочие дела")
    elif 19 <= hour < 23:
        hints.append("вечер — ужин дома (продукты) или отдых")
    else:
        hints.append("ночное время")

    return "; ".join(hints)

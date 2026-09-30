"""Единое правило: что считать ПОГАШЕНИЕМ кредита или рассрочки.

Погашение — это платёж банку по уже взятому кредиту/рассрочке. Он всегда относится к
«Финансовые расходы и переводы › Кредиты и рассрочки», независимо от того, на что взят кредит.
Покупка ЧЕРЕЗ рассрочку (funds_type = «Рассрочка»/«Кредитные») — обычная покупка в своей
категории, это не погашение.
"""

LOAN_CATEGORY = "Финансовые расходы и переводы"
LOAN_SUBCATEGORY = "Кредиты и рассрочки"
PURCHASE_ON_CREDIT_FUNDS = ("Рассрочка", "Кредитные")

# Погашением считаем запись, где есть «погашение» И слово про кредит/рассрочку/займ.
# Одного слова «погашение» мало: «погашение долга Саше» — это личный долг, он ведётся отдельно.
_CREDIT_WORDS = ("кредит", "рассрочк", "займ", "ипотек", "лизинг", "kaspi red", "каспи ред", "каспи рэд", "ozen")


def looks_like_repayment(merchant: str = "", comment: str = "", subcategory: str = "", funds_type: str = "") -> bool:
    if str(funds_type or "").strip() in PURCHASE_ON_CREDIT_FUNDS:
        return False                                   # покупка через рассрочку, а не платёж по ней
    if str(subcategory or "").strip() == LOAN_SUBCATEGORY:
        return True
    text = f"{merchant or ''} {comment or ''}".lower()
    return "погашени" in text and any(word in text for word in _CREDIT_WORDS)

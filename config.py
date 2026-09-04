import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GOOGLE_SHEETS_KEY = os.getenv("GOOGLE_SHEETS_KEY")
VLAD_TELEGRAM_ID = os.getenv("VLAD_TELEGRAM_ID")
DIANA_TELEGRAM_ID = os.getenv("DIANA_TELEGRAM_ID")
FAMILY_CHAT_ID = int(os.getenv("FAMILY_CHAT_ID", "-5349521972"))
CREDENTIALS_FILE = "credentials.json"

# Приватный семейный бот: в таблицу должны попадать нормальные семейные имена,
# а не короткие Telegram first_name вроде "D".
FAMILY_USER_ALIASES = {
    "влад": "Влад",
    "владислав": "Влад",
    "vlad": "Влад",
    "vladislav": "Влад",
    "диана": "Диана",
    "diana": "Диана",
    "d": "Диана",
    "di": "Диана",
    "ди": "Диана",
}


class Settings:
    TELEGRAM_BOT_TOKEN = TELEGRAM_BOT_TOKEN
    OPENAI_API_KEY = OPENAI_API_KEY
    DEEPSEEK_API_KEY = DEEPSEEK_API_KEY
    GOOGLE_SHEETS_KEY = GOOGLE_SHEETS_KEY
    VLAD_TELEGRAM_ID = VLAD_TELEGRAM_ID
    DIANA_TELEGRAM_ID = DIANA_TELEGRAM_ID
    FAMILY_CHAT_ID = FAMILY_CHAT_ID
    CREDENTIALS_FILE = CREDENTIALS_FILE

    telegram_bot_token = TELEGRAM_BOT_TOKEN
    openai_api_key = OPENAI_API_KEY
    deepseek_api_key = DEEPSEEK_API_KEY
    google_sheets_key = GOOGLE_SHEETS_KEY
    vlad_telegram_id = VLAD_TELEGRAM_ID
    diana_telegram_id = DIANA_TELEGRAM_ID
    family_chat_id = FAMILY_CHAT_ID
    credentials_file = CREDENTIALS_FILE


def get_settings():
    return Settings()


def normalize_family_user_name(raw_name: str | None) -> str | None:
    """Приводит Telegram first_name/алиас к семейному имени для таблиц."""
    if not raw_name:
        return None
    value = str(raw_name).strip()
    if not value:
        return None
    normalized = value.lower().replace("@", "").strip()
    return FAMILY_USER_ALIASES.get(normalized)


def get_authorized_user_name(user_id, fallback_name: str | None = None):
    """Возвращает имя члена семьи по Telegram ID или по безопасному алиасу.

    Старый интерфейс get_authorized_user_name(user_id) сохранён: второй аргумент
    необязательный, поэтому существующие вызовы не ломаются.
    """
    uid = str(user_id)

    if VLAD_TELEGRAM_ID and uid == str(VLAD_TELEGRAM_ID):
        return "Влад"
    if DIANA_TELEGRAM_ID and uid == str(DIANA_TELEGRAM_ID):
        return "Диана"

    by_fallback = normalize_family_user_name(fallback_name)
    if by_fallback:
        return by_fallback

    return None

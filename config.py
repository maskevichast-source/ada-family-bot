import os


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GOOGLE_SHEETS_KEY = os.getenv("GOOGLE_SHEETS_KEY")
VLAD_TELEGRAM_ID = os.getenv("VLAD_TELEGRAM_ID")
DIANA_TELEGRAM_ID = os.getenv("DIANA_TELEGRAM_ID")
def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


FAMILY_CHAT_ID = _env_int("FAMILY_CHAT_ID", 0)
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "credentials.json")
STATE_DIR = os.getenv("ADA_STATE_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "./data"
CHAT_HISTORY_FILE = os.getenv("CHAT_HISTORY_FILE", os.path.join(STATE_DIR, "chat_history.json"))

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
    CHAT_HISTORY_FILE = CHAT_HISTORY_FILE

    telegram_bot_token = TELEGRAM_BOT_TOKEN
    openai_api_key = OPENAI_API_KEY
    deepseek_api_key = DEEPSEEK_API_KEY
    google_sheets_key = GOOGLE_SHEETS_KEY
    vlad_telegram_id = VLAD_TELEGRAM_ID
    diana_telegram_id = DIANA_TELEGRAM_ID
    family_chat_id = FAMILY_CHAT_ID
    credentials_file = CREDENTIALS_FILE
    chat_history_file = CHAT_HISTORY_FILE


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
    """Возвращает имя члена семьи только по доверенному Telegram ID.

    Старый интерфейс get_authorized_user_name(user_id) сохранён: второй аргумент
    необязательный, поэтому существующие вызовы не ломаются.
    """
    uid = str(user_id)

    if VLAD_TELEGRAM_ID and uid == str(VLAD_TELEGRAM_ID):
        return "Влад"
    if DIANA_TELEGRAM_ID and uid == str(DIANA_TELEGRAM_ID):
        return "Диана"

    return None


def validate_settings():
    required = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "GOOGLE_SHEETS_KEY": GOOGLE_SHEETS_KEY,
        "DEEPSEEK_API_KEY": DEEPSEEK_API_KEY,
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "VLAD_TELEGRAM_ID": VLAD_TELEGRAM_ID,
        "DIANA_TELEGRAM_ID": DIANA_TELEGRAM_ID,
        "FAMILY_CHAT_ID": FAMILY_CHAT_ID,
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError("Не заданы настройки: " + ", ".join(missing))
    if not all(str(x).isdigit() and int(x) > 0 for x in (VLAD_TELEGRAM_ID, DIANA_TELEGRAM_ID)):
        raise RuntimeError("Telegram ID участников должны быть положительными числами")
    if str(VLAD_TELEGRAM_ID) == str(DIANA_TELEGRAM_ID):
        raise RuntimeError("Telegram ID участников должны различаться")

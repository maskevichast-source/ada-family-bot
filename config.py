import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GOOGLE_SHEETS_KEY = os.getenv("GOOGLE_SHEETS_KEY")
VLAD_TELEGRAM_ID = os.getenv("VLAD_TELEGRAM_ID")
DIANA_TELEGRAM_ID = os.getenv("DIANA_TELEGRAM_ID")
FAMILY_CHAT_ID = int(os.getenv("FAMILY_CHAT_ID", "-5349521972"))
CREDENTIALS_FILE = "credentials.json"

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

def get_authorized_user_name(user_id):
    uid = str(user_id)
    if uid == str(VLAD_TELEGRAM_ID):
        return "Влад"
    if uid == str(DIANA_TELEGRAM_ID):
        return "Диана"
    return None

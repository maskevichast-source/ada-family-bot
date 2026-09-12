"""Deployment checks; import/run does not start Telegram polling."""
from config import validate_settings
TRANSACTION_HEADERS = ["transaction_id", "date", "user", "type", "amount", "currency",
                       "bank", "source", "funds_type", "resource", "category",
                       "subcategory", "merchant", "necessity", "user_comment", "ai_comment"]
EXPECTED = {
    "Transactions": TRANSACTION_HEADERS,
    "Reminders": ["reminder_id", "created_at", "target_user", "remind_at", "text", "status", "recurrence"],
    "Limits": ["category", "limit_amount"],
    "ShoppingList": ["item_id", "date_added", "added_by", "item", "status"],
    "Subscriptions": ["id", "name", "amount", "bank", "day_of_month", "last_paid", "status"],
    "Trips": ["trip_id", "destination", "dates", "budget"],
    "Installments": ["id", "date", "user", "bank", "kind", "description", "total_amount",
                     "monthly_payment", "payments_count", "next_payment", "status"],
    "Debts": ["event_id", "debt_id", "date", "owner", "counterparty", "direction",
              "event_type", "amount", "currency", "due_date", "note"],
}
def check_schema():
    import gspread
    from services.sheets import get_db
    db = get_db()
    for name, headers in EXPECTED.items():
        try:
            actual = db.worksheet(name).row_values(1)
        except gspread.WorksheetNotFound:
            if name == "Transactions":
                raise RuntimeError("Обязательный лист Transactions отсутствует")
            continue
        if name == "Reminders" and actual and actual[0] == "id":
            actual[0] = "reminder_id"
        if actual[:len(headers)] != headers:
            raise RuntimeError(f"Неожиданная схема {name}: требуется ручная проверка")
    return True

if __name__ == "__main__":
    validate_settings()
    check_schema()
    print("Настройки и схема Google Sheets проверены. Записи не изменялись.")


def initialize_optional():
    from services.sheets import _get_or_create_worksheet, SUBSCRIPTION_HEADERS
    for name, headers in EXPECTED.items():
        if name in {"Transactions", "Reminders"}:
            continue
        if name == "Subscriptions":
            headers = SUBSCRIPTION_HEADERS
        _get_or_create_worksheet(name, headers, rows=100, cols=len(headers))

    # Trips retains A:D used by Power BI; notes are additive in E.
    ws = _get_or_create_worksheet("Trips", EXPECTED["Trips"], rows=100, cols=5)
    header = ws.row_values(1)
    if len(header) < 5:
        if ws.col_count < 5:
            ws.resize(cols=5)
        ws.update_cell(1, 5, "notes")
    elif header[4] != "notes":
        raise RuntimeError("Колонка E Trips занята; нужна ручная миграция примечаний")

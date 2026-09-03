import datetime
import json
import os
import re
import gspread
from gspread import utils as gspread_utils
from oauth2client.service_account import ServiceAccountCredentials
from config import GOOGLE_SHEETS_KEY, CREDENTIALS_FILE
from services.categories import TYPE_EXPENSE, SUBCATEGORIES_MAP, DEFAULT_EXPENSE_LIMITS
from services.money import parse_amount, to_clean_number
from services.banks import normalize_bank_source

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
ASTANA_TZ = datetime.timezone(datetime.timedelta(hours=5), name="Asia/Astana")

_cached_client = None
_cached_db = None


def _digits_only(text) -> str:
    return "".join(c for c in str(text) if c.isdigit())


def _table_range(num_columns: int) -> str:
    return f"A1:{gspread_utils.rowcol_to_a1(1, num_columns)}"


def _get_all_records_safe(ws):
    all_values = ws.get_all_values()
    if not all_values:
        return []
    headers = all_values[0]
    records = []
    for row in all_values[1:]:
        padded = row + [""] * (len(headers) - len(row))
        records.append(dict(zip(headers, padded)))
    return records


def _load_google_credentials():
    raw_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if raw_json:
        try:
            info = json.loads(raw_json)
            return ServiceAccountCredentials.from_json_keyfile_dict(info, scope)
        except Exception as error:
            raise RuntimeError("GOOGLE_CREDENTIALS_JSON задан некорректно.") from error
    return ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)


def get_db():
    global _cached_client, _cached_db
    try:
        if not _cached_client or not _cached_db:
            creds = _load_google_credentials()
            _cached_client = gspread.authorize(creds)
            _cached_db = _cached_client.open_by_key(GOOGLE_SHEETS_KEY)
        else:
            _cached_db.title
    except Exception as e:
        print(f"[Google Таблицы] Переподключение: {e}")
        creds = _load_google_credentials()
        _cached_client = gspread.authorize(creds)
        _cached_db = _cached_client.open_by_key(GOOGLE_SHEETS_KEY)
    return _cached_db


def normalize_necessity(raw, category=None):
    if not raw:
        return "Want"
    s = str(raw).strip().lower()
    if s in ("need", "нужно", "нужное", "обязательно", "must"):
        return "Need"
    return "Want"


def append_transaction(data: dict):
    now = datetime.datetime.now(ASTANA_TZ)
    raw_amount = data.get("amount", 0)
    amount = parse_amount(raw_amount)
    if amount == int(amount):
        amount = int(amount)

    if not data.get("transaction_id"):
        data["transaction_id"] = f"TRX_{now.strftime('%Y%m%d_%H%M%S')}_{amount}"

    data["date"] = now.strftime("%Y-%m-%d %H:%M:%S")
    if not data.get("user"):
        data["user"] = "Влад"

    if not data.get("type") or str(data.get("type")).strip() == "":
        data["type"] = TYPE_EXPENSE

    if not data.get("currency"):
        data["currency"] = "KZT"

    bank_value = str(data.get("bank") or "").strip()
    if bank_value.lower() in ("наличные", "нал", "cash"):
        data["resource"] = "Наличные"
        data["bank"] = "Не указан"

    if not data.get("bank"):
        data["bank"] = "Не указан"

    data["source"] = normalize_bank_source(data.get("bank"), data.get("source"))
    if not data.get("funds_type"):
        data["funds_type"] = "Собственные"
    if not data.get("resource"):
        data["resource"] = "Карта"

    columns = [
        "transaction_id", "date", "user", "type", "amount", "currency", 
        "bank", "source", "funds_type", "resource", "category", 
        "subcategory", "merchant", "necessity", "user_comment", "ai_comment"
    ]
    row = [str(data.get(col, "") or "") for col in columns]

    try:
        ws = get_db().worksheet("Transactions")
        ws.append_row(row, table_range=_table_range(len(columns)))
    except Exception as e:
        print(f"[Транзакции] Не удалось добавить запись: {e}")


def ensure_power_bi_dimension_table():
    """Создаёт лист Dim_Categories со строго уникальными категориями (связь 1:* для Power BI)."""
    try:
        db = get_db()
        try:
            ws = db.worksheet("Dim_Categories")
        except Exception:
            ws = db.add_worksheet(title="Dim_Categories", rows=30, cols=3)

        rows = [["category", "default_limit", "type"]]
        for cat, limit in DEFAULT_EXPENSE_LIMITS.items():
            rows.append([cat, limit, "РАСХОД"])

        ws.clear()
        ws.update("A1", rows)
    except Exception as e:
        print(f"[PowerBI Dimension] Ошибка создания справочника: {e}")


def debug_transactions_snapshot(limit: int = 6) -> str:
    try:
        ws = get_db().worksheet("Transactions")
        records = _get_all_records_safe(ws)
        non_empty = [r for r in records if str(r.get("transaction_id", "")).strip()]
        lines = [
            f"Всего строк в таблице: {len(records)}",
            f"Непустых записей: {len(non_empty)}",
            "",
            "Последние записи:",
        ]
        for r in non_empty[-limit:]:
            lines.append(f"- {r.get('date')} | {r.get('user')} | {r.get('amount')} KZT | {r.get('category')} ({r.get('subcategory')})")
        return "\n".join(lines)
    except Exception as e:
        return f"Ошибка снятия диагностики: {e}"


def get_last_200_transactions():
    try:
        ws = get_db().worksheet("Transactions")
        records = _get_all_records_safe(ws)
        non_empty = [r for r in records if str(r.get("transaction_id", "")).strip()]
        return [{
            "date": r.get("date"), "user": r.get("user"),
            "type": r.get("type") or TYPE_EXPENSE,
            "amt": r.get("amount"),
            "curr": r.get("currency"), "bank": r.get("bank"), "cat": r.get("category"),
            "subcat": r.get("subcategory"), "nec": r.get("necessity"), "comm": r.get("user_comment")
        } for r in non_empty[-200:]]
    except Exception as e:
        print(f"[Транзакции] Ошибка чтения: {e}")
        return []


def get_transactions_for_period(start_date: str, end_date: str) -> list[dict]:
    try:
        ws = get_db().worksheet("Transactions")
        records = _get_all_records_safe(ws)
        result = []
        for r in records:
            date_str = str(r.get("date", ""))
            if start_date <= date_str < end_date:
                result.append({
                    "date": r.get("date"), "user": r.get("user"),
                    "type": r.get("type") or TYPE_EXPENSE,
                    "amt": r.get("amount"),
                    "curr": r.get("currency"), "bank": r.get("bank"),
                    "cat": r.get("category"), "subcat": r.get("subcategory"),
                    "nec": r.get("necessity"), "comm": r.get("user_comment"),
                })
        return result
    except Exception as e:
        print(f"[Транзакции] Ошибка чтения периода: {e}")
        return []


def find_recent_duplicate_transaction(amount, comment: str = "", minutes: int = 5):
    """Ищет дубли только при совпадении суммы И похожего комментария за последние 5 минут."""
    try:
        target = to_clean_number(amount)
        if not target:
            return None
        ws = get_db().worksheet("Transactions")
        records = _get_all_records_safe(ws)
        non_empty = [r for r in records if str(r.get("transaction_id", "")).strip()]
        now = datetime.datetime.now(ASTANA_TZ)
        comm_clean = comment.strip().lower()

        for r in reversed(non_empty[-30:]):
            try:
                r_amount = to_clean_number(r.get("amount"))
                if r_amount != target:
                    continue
                r_date = datetime.datetime.strptime(str(r.get("date")), "%Y-%m-%d %H:%M:%S").replace(tzinfo=ASTANA_TZ)
                if (now - r_date).total_seconds() <= minutes * 60:
                    r_comm = str(r.get("user_comment", "")).lower()
                    if not comm_clean or comm_clean in r_comm or r_comm in comm_clean:
                        return r
            except (ValueError, TypeError):
                continue
        return None
    except Exception as e:
        print(f"[Транзакции] Ошибка поиска дублей: {e}")
        return None


def delete_record_by_keyword(worksheet_name: str, search_query: str, search_from_recent: bool = True):
    """Умное удаление: понимает фразы 'последняя', 'крайняя' и удаляет именно последнюю строку."""
    try:
        ws = get_db().worksheet(worksheet_name)
        records = _get_all_records_safe(ws)
        if not records:
            return None

        search_lower = str(search_query or "").strip().lower()
        indexed_records = list(enumerate(records, start=2))

        is_generic_last = any(w in search_lower for w in ["последн", "крайн", "предыдущ", "last"]) or not search_lower
        specific_keywords = [w for w in re.findall(r'\w+', search_lower) if w not in ["удали", "удалить", "последнюю", "последний", "запись", "трату", "покупку"]]

        if is_generic_last and not specific_keywords:
            last_idx, last_rec = indexed_records[-1]
            ws.delete_rows(last_idx)
            return last_rec

        query_digits = _digits_only(search_lower)
        for idx, r in reversed(indexed_records):
            row_values = [str(v) for v in r.values()]
            row_str = " ".join(row_values).lower()
            matched = any(k in row_str for k in specific_keywords) if specific_keywords else False
            if not matched and query_digits:
                matched = any(_digits_only(v) == query_digits for v in row_values if _digits_only(v))
            if matched:
                ws.delete_rows(idx)
                return r
        return None
    except Exception as e:
        print(f"[Таблицы] Ошибка удаления: {e}")
        return None


def find_and_update_record(worksheet_name: str, search_query, field, new_value, search_from_recent: bool = True):
    try:
        ws = get_db().worksheet(worksheet_name)
        all_values = ws.get_all_values()
        if not all_values or len(all_values) <= 1:
            return None
        headers_lower = [str(h).strip().lower() for h in all_values[0]]
        col_idx = None
        if isinstance(field, int):
            col_idx = field
        else:
            field_str = str(field).strip()
            if field_str.isdigit():
                col_idx = int(field_str)
            else:
                if field_str.lower() in headers_lower:
                    col_idx = headers_lower.index(field_str.lower()) + 1
        if not col_idx:
            return None

        records = _get_all_records_safe(ws)
        search_lower = str(search_query or "").strip().lower()
        indexed_records = list(enumerate(records, start=2))

        is_generic_last = any(w in search_lower for w in ["последн", "крайн", "предыдущ", "last"]) or not search_lower
        specific_keywords = [w for w in re.findall(r'\w+', search_lower) if w not in ["поменяй", "измени", "последнюю", "последний", "запись", "трату"]]

        if is_generic_last and not specific_keywords:
            last_idx, last_rec = indexed_records[-1]
            ws.update_cell(last_idx, col_idx, new_value)
            return last_rec

        query_digits = _digits_only(search_lower)
        for idx, r in reversed(indexed_records):
            row_values = [str(v) for v in r.values()]
            row_str = " ".join(row_values).lower()
            matched = any(k in row_str for k in specific_keywords) if specific_keywords else False
            if not matched and query_digits:
                matched = any(_digits_only(v) == query_digits for v in row_values if _digits_only(v))
            if matched:
                ws.update_cell(idx, col_idx, new_value)
                return r
        return None
    except Exception as e:
        print(f"[Таблицы] Ошибка правки: {e}")
        return None


def mark_reminder_done(row_idx: int, recurrence: str = "once", remind_at: str = ""):
    """Переносит не только daily, но и monthly напоминания ровно на месяц вперёд."""
    try:
        ws = get_db().worksheet("Reminders")
        rec_norm = str(recurrence or "once").lower()

        if rec_norm == "daily" and remind_at:
            try:
                old_dt = datetime.datetime.strptime(remind_at, "%Y-%m-%d %H:%M:%S")
                next_dt = old_dt + datetime.timedelta(days=1)
                ws.update_cell(row_idx, 4, next_dt.strftime("%Y-%m-%d %H:%M:%S"))
                return
            except Exception:
                pass
        elif rec_norm == "monthly" and remind_at:
            try:
                old_dt = datetime.datetime.strptime(remind_at, "%Y-%m-%d %H:%M:%S")
                new_month = old_dt.month + 1 if old_dt.month < 12 else 1
                new_year = old_dt.year if old_dt.month < 12 else old_dt.year + 1
                next_dt = old_dt.replace(year=new_year, month=new_month)
                ws.update_cell(row_idx, 4, next_dt.strftime("%Y-%m-%d %H:%M:%S"))
                return
            except Exception:
                pass

        ws.update_cell(row_idx, 6, "sent")
    except Exception as e:
        print(f"[Напоминания] Ошибка завершения: {e}")


def split_last_transaction_by_amount(target_amount: float, part1_amt: float, part1_cat: str, part1_comm: str, part2_amt: float, part2_cat: str, part2_comm: str):
    try:
        ws = get_db().worksheet("Transactions")
        records = _get_all_records_safe(ws)
        target_idx = -1
        target_record = None

        for idx, r in enumerate(reversed(records), start=0):
            try:
                amt_val = float(str(r.get("amount", 0)).replace(",", "."))
                if abs(amt_val - target_amount) < 1.0:
                    target_idx = len(records) - idx
                    target_record = r
                    break
            except ValueError:
                continue

        if target_idx == -1 or not target_record:
            return False

        now = datetime.datetime.now(ASTANA_TZ)
        base_date = target_record.get("date", now.strftime("%Y-%m-%d %H:%M:%S"))
        base_user = target_record.get("user", "Влад")
        base_type = target_record.get("type") or TYPE_EXPENSE
        base_bank = target_record.get("bank") or "BCC"
        base_source = target_record.get("source") or "BCC Pay"
        base_funds = target_record.get("funds_type") or "Собственные"
        base_resource = target_record.get("resource") or "Карта"
        base_merchant = target_record.get("merchant", "")
        base_necessity = target_record.get("necessity", "Want")

        from services.categories import validate_transaction_category_subcategory
        _, p1_sub = validate_transaction_category_subcategory(part1_cat, "")
        _, p2_sub = validate_transaction_category_subcategory(part2_cat, "")

        ws.delete_rows(target_idx + 1)

        row1 = [
            f"TRX_{now.strftime('%Y%m%d_%H%M%S')}_1", base_date, base_user, base_type, part1_amt, "KZT",
            base_bank, base_source, base_funds, base_resource, part1_cat, p1_sub, base_merchant, base_necessity, part1_comm, "Разделено по запросу."
        ]
        row2 = [
            f"TRX_{now.strftime('%Y%m%d_%H%M%S')}_2", base_date, base_user, base_type, part2_amt, "KZT",
            base_bank, base_source, base_funds, base_resource, part2_cat, p2_sub, base_merchant, base_necessity, part2_comm, "Разделено по запросу."
        ]

        ws.insert_row(row1, target_idx + 1)
        ws.insert_row(row2, target_idx + 2)
        return True
    except Exception as e:
        print(f"[Транзакции] Ошибка разделения: {e}")
        return False


INSTALLMENT_COLUMNS = ["id", "date", "user", "bank", "kind", "description", "total_amount", "monthly_payment", "payments_count", "next_payment", "status"]
INSTALLMENT_STATUS_COLUMN = INSTALLMENT_COLUMNS.index("status") + 1

def _get_or_create_installments_sheet():
    db = get_db()
    try:
        ws = db.worksheet("Installments")
    except Exception:
        ws = db.add_worksheet(title="Installments", rows=100, cols=len(INSTALLMENT_COLUMNS))
        ws.append_row(INSTALLMENT_COLUMNS)
        return ws
    if not ws.row_values(1):
        ws.append_row(INSTALLMENT_COLUMNS)
    return ws

def get_installments():
    try:
        ws = _get_or_create_installments_sheet()
        records = _get_all_records_safe(ws)
        return [r for r in records if str(r.get("status", "active")).lower() not in {"closed", "done", "завершена"}]
    except Exception as error:
        print(f"[Рассрочки] Ошибка: {error}")
        return []

def add_installment(data: dict | None = None, **kwargs):
    payload = dict(data or {})
    payload.update(kwargs)
    now = datetime.datetime.now(ASTANA_TZ)
    try:
        ws = _get_or_create_installments_sheet()
        values = {
            "id": payload.get("id") or f"INST_{now.strftime('%Y%m%d_%H%M%S')}",
            "date": payload.get("date") or now.strftime("%Y-%m-%d %H:%M:%S"),
            "user": payload.get("user") or "Влад",
            "bank": payload.get("bank") or "Kaspi",
            "kind": payload.get("kind") or payload.get("type") or "Рассрочка",
            "description": payload.get("description") or payload.get("merchant") or "Рассрочка",
            "total_amount": payload.get("total_amount", payload.get("amount", "")),
            "monthly_payment": payload.get("monthly_payment", ""),
            "payments_count": payload.get("payments_count", ""),
            "next_payment": payload.get("next_payment", ""),
            "status": payload.get("status") or "active",
        }
        ws.append_row([str(values[c] or "") for c in INSTALLMENT_COLUMNS], table_range=_table_range(len(INSTALLMENT_COLUMNS)))
        return values
    except Exception as error:
        print(f"[Рассрочки] Ошибка записи: {error}")
        return None

def close_installment(search_query: str) -> bool:
    return find_and_update_record("Installments", search_query, INSTALLMENT_STATUS_COLUMN, "closed", search_from_recent=True)

def get_category_limits():
    try:
        ws = get_db().worksheet("Limits")
        records = _get_all_records_safe(ws)
        return {str(r.get("category")).strip(): float(r.get("limit_amount", 0)) for r in records if r.get("category")}
    except Exception as e:
        print(f"[Лимиты] Ошибка: {e}")
        return {}

def save_category_limits(new_limits: dict):
    try:
        db = get_db()
        try:
            ws = db.worksheet("Limits")
        except Exception:
            ws = db.add_worksheet(title="Limits", rows=20, cols=2)
            ws.append_row(["category", "limit_amount"])
        ws.clear()
        ws.append_row(["category", "limit_amount"])
        for cat, limit in new_limits.items():
            ws.append_row([str(cat), float(limit)], table_range=_table_range(2))
    except Exception as e:
        print(f"[Лимиты] Ошибка сохранения: {e}")

def add_or_update_subscription(name: str, amount: float, bank: str, day_of_month: int):
    try:
        ws = get_db().worksheet("Subscriptions")
        records = _get_all_records_safe(ws)
        now = datetime.datetime.now(ASTANA_TZ)
        today_str = now.strftime("%Y-%m-%d")
        for idx, r in enumerate(records, start=2):
            if str(r.get("name")).lower() == name.lower():
                ws.update_cell(idx, 3, amount)
                ws.update_cell(idx, 5, day_of_month)
                ws.update_cell(idx, 6, today_str)
                ws.update_cell(idx, 7, "active")
                return
        sub_id = f"SUB_{now.strftime('%Y%m%d_%H%M%S')}"
        ws.append_row([sub_id, name, amount, bank, day_of_month, today_str, "active"], table_range=_table_range(7))
    except Exception as e:
        print(f"[Подписки] Ошибка: {e}")

def get_active_subscriptions():
    try:
        ws = get_db().worksheet("Subscriptions")
        records = _get_all_records_safe(ws)
        return [r for r in records if str(r.get("status")).lower() == "active"]
    except Exception as e:
        print(f"[Подписки] Ошибка чтения: {e}")
        return []

def deactivate_subscription(name: str):
    try:
        ws = get_db().worksheet("Subscriptions")
        records = _get_all_records_safe(ws)
        name_lower = str(name).lower()
        for idx, r in enumerate(records, start=2):
            if str(r.get("status")).lower() == "active" and name_lower in str(r.get("name", "")).lower():
                ws.update_cell(idx, 7, "cancelled")
                return r
        return None
    except Exception as e:
        print(f"[Подписки] Ошибка отмены: {e}")
        return None

def add_trip_plan(destination: str, dates: str, budget: float, notes: str):
    try:
        ws = get_db().worksheet("Trips")
        now = datetime.datetime.now(ASTANA_TZ)
        trip_id = f"TRIP_{now.strftime('%Y%m%d_%H%M%S')}"
        ws.append_row([trip_id, destination, dates, budget, notes, "planned"], table_range=_table_range(6))
    except Exception as e:
        print(f"[Поездки] Ошибка: {e}")

def get_planned_trips():
    try:
        ws = get_db().worksheet("Trips")
        records = _get_all_records_safe(ws)
        return [r for r in records if str(r.get("status")).lower() == "planned"]
    except Exception as e:
        print(f"[Поездки] Ошибка: {e}")
        return []

def add_shopping_items(items: list, user_name: str):
    try:
        ws = get_db().worksheet("ShoppingList")
        now = datetime.datetime.now(ASTANA_TZ)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        for item in items:
            item_id = f"SHOP_{now.strftime('%Y%m%d_%H%M%S')}"
            ws.append_row([item_id, now_str, user_name, item, "active"], table_range=_table_range(5))
    except Exception as e:
        print(f"[Покупки] Ошибка: {e}")

def get_shopping_items():
    try:
        ws = get_db().worksheet("ShoppingList")
        records = _get_all_records_safe(ws)
        return [r for r in records if str(r.get("status")).lower() == "active"]
    except Exception as e:
        print(f"[Покупки] Ошибка чтения: {e}")
        return []

def mark_shopping_items_done(items_to_remove: list):
    try:
        ws = get_db().worksheet("ShoppingList")
        records = _get_all_records_safe(ws)
        for idx, r in enumerate(records, start=2):
            if str(r.get("status")).lower() == "active":
                for item_name in items_to_remove:
                    if item_name.lower() in str(r.get("item")).lower():
                        ws.update_cell(idx, 5, "done")
    except Exception as e:
        print(f"[Покупки] Ошибка отметки: {e}")

def add_reminder(target_user: str, remind_at_str: str, text: str, recurrence: str = "once"):
    try:
        ws = get_db().worksheet("Reminders")
    except Exception:
        ws = get_db().add_worksheet(title="Reminders", rows=20, cols=7)
        ws.append_row(["id", "created_at", "target_user", "remind_at", "text", "status", "recurrence"])

    now = datetime.datetime.now(ASTANA_TZ)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    rem_id = f"REM_{now.strftime('%Y%m%d_%H%M%S')}"
    ws.append_row([rem_id, now_str, target_user, remind_at_str, text, "pending", recurrence], table_range=_table_range(7))

def get_pending_reminders():
    try:
        ws = get_db().worksheet("Reminders")
        records = _get_all_records_safe(ws)
        pending = []
        for idx, r in enumerate(records, start=2):
            if str(r.get("status")).lower() == "pending":
                r["row_idx"] = idx
                pending.append(r)
        return pending
    except Exception as e:
        print(f"[Напоминания] Ошибка чтения: {e}")
        return []
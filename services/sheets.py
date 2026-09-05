import datetime
import json
import os
import re
from typing import Optional
import gspread
from gspread import utils as gspread_utils
from oauth2client.service_account import ServiceAccountCredentials
from config import GOOGLE_SHEETS_KEY, CREDENTIALS_FILE, normalize_family_user_name
from services.categories import TYPE_EXPENSE, SUBCATEGORIES_MAP, DEFAULT_EXPENSE_LIMITS
from services.money import parse_amount, to_clean_number
from services.banks import normalize_bank_source
from services.timezone import parse_flexible_datetime, ASTANA_TZ

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

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


def _get_or_create_worksheet(title: str, headers: list[str], rows: int = 50, cols: int | None = None):
    """Получить лист или создать его с корректной шапкой.

    Если лист уже есть, но пустой — шапка добавляется. Если первый заголовок
    старого Reminders был "id", он приводится к "reminder_id" без потери строк.
    """
    db = get_db()
    try:
        ws = db.worksheet(title)
    except Exception:
        ws = db.add_worksheet(title=title, rows=rows, cols=cols or len(headers))
        ws.append_row(headers, table_range=_table_range(len(headers)))
        return ws

    values = ws.get_all_values()
    if not values:
        ws.append_row(headers, table_range=_table_range(len(headers)))
    elif title == "Reminders" and values[0]:
        first = str(values[0][0]).strip().lower()
        if first == "id":
            ws.update_cell(1, 1, "reminder_id")
    return ws


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
        data["transaction_id"] = f"TRX_{now.strftime('%Y%m%d_%H%M%S_%f')}_{amount}"

    data["date"] = now.strftime("%Y-%m-%d %H:%M:%S")
    if not data.get("user"):
        data["user"] = "Влад"
    else:
        data["user"] = normalize_family_user_name(data.get("user")) or str(data.get("user")).strip()

    raw_type = str(data.get("type") or "").strip().lower()
    if raw_type in {"income", "доход", "in"}:
        data["type"] = "ДОХОД"
    elif raw_type in {"expense", "расход", "out", ""}:
        data["type"] = TYPE_EXPENSE
    else:
        data["type"] = str(data.get("type")).strip().upper()

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
        raise


def update_last_transaction_bank_and_source(new_bank: str) -> Optional[dict]:
    """Мгновенно обновляет банк и источник в последней записи таблицы."""
    try:
        ws = get_db().worksheet("Transactions")
        records = _get_all_records_safe(ws)
        if not records:
            return None

        last_row_idx = len(records) + 1
        bank_norm = new_bank.strip()
        source_norm = normalize_bank_source(bank_norm, "")

        ws.update_cell(last_row_idx, 7, bank_norm)
        ws.update_cell(last_row_idx, 8, source_norm)

        last_rec = records[-1]
        last_rec["bank"] = bank_norm
        last_rec["source"] = source_norm
        return last_rec
    except Exception as e:
        print(f"[Таблицы] Ошибка обновления банка: {e}")
        return None


def ensure_power_bi_dimension_table():
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
            "amt": parse_amount(r.get("amount")),
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

        start_dt = parse_flexible_datetime(f"{start_date} 00:00:00")
        end_dt = parse_flexible_datetime(f"{end_date} 00:00:00")

        for r in records:
            raw_date = r.get("date")
            if not raw_date:
                continue

            r_dt = parse_flexible_datetime(raw_date)
            matched = False

            if r_dt and start_dt and end_dt:
                matched = (start_dt <= r_dt < end_dt)
            else:
                d_str = str(raw_date).strip()
                matched = (start_date <= d_str < end_date)

            if matched:
                result.append({
                    "transaction_id": r.get("transaction_id", ""),
                    "date": r.get("date"),
                    "user": r.get("user"),
                    "type": r.get("type") or TYPE_EXPENSE,
                    "amt": parse_amount(r.get("amount", 0)),
                    "curr": r.get("currency", "KZT"),
                    "bank": r.get("bank", ""),
                    "cat": r.get("category", "Прочее"),
                    "subcat": r.get("subcategory", ""),
                    "nec": r.get("necessity", "Want"),
                    "comm": r.get("user_comment", ""),
                })
        return result
    except Exception as e:
        print(f"[Транзакции] Ошибка чтения периода: {e}")
        return []


def find_recent_duplicate_transaction(amount, comment: str = "", minutes: int = 5):
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
                r_date = parse_flexible_datetime(r.get("date"))
                if r_date and (now - r_date).total_seconds() <= minutes * 60:
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

            matched = all(k in row_str for k in specific_keywords) if specific_keywords else False
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
            matched = all(k in row_str for k in specific_keywords) if specific_keywords else False
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
    try:
        ws = get_db().worksheet("Reminders")
        rec_norm = str(recurrence or "once").lower()

        if rec_norm == "daily" and remind_at:
            try:
                old_dt = parse_flexible_datetime(remind_at)
                if old_dt:
                    next_dt = old_dt + datetime.timedelta(days=1)
                    ws.update_cell(row_idx, 4, next_dt.strftime("%Y-%m-%d %H:%M:%S"))
                    return
            except Exception:
                pass
        elif rec_norm == "monthly" and remind_at:
            try:
                old_dt = parse_flexible_datetime(remind_at)
                if old_dt:
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
                amt_val = parse_amount(r.get("amount", 0))
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


def process_due_subscriptions(now: datetime.datetime) -> list:
    due_processed = []
    try:
        ws = _get_or_create_subscriptions_sheet()
        records = _get_all_records_safe(ws)
        current_month_prefix = now.strftime("%Y-%m")
        today_str = now.strftime("%Y-%m-%d")

        for idx, r in enumerate(records, start=2):
            if str(r.get("status")).lower() != "active":
                continue

            try:
                day = int(parse_amount(r.get("day_of_month", 1)))
                last_paid = str(r.get("last_paid", ""))

                if now.day >= day and not last_paid.startswith(current_month_prefix):
                    amt = parse_amount(r.get("amount", 0))
                    name = str(r.get("name", "Подписка"))
                    bank = str(r.get("bank", "Не указан"))

                    append_transaction({
                        "type": "РАСХОД",
                        "amount": amt,
                        "currency": "KZT",
                        "bank": bank,
                        "source": "Основная карта",
                        "funds_type": "Собственные",
                        "resource": "Карта",
                        "category": "Связь и подписки",
                        "subcategory": "Цифровые подписки и сервисы",
                        "merchant": name,
                        "necessity": "Want",
                        "user_comment": f"Автосписание: {name}",
                        "ai_comment": f"Ежемесячная подписка {name}",
                    })

                    ws.update_cell(idx, 6, today_str)
                    due_processed.append(name)
            except Exception as e:
                print(f"[Подписки] Ошибка строки {idx}: {e}")

    except Exception as e:
        print(f"[Подписки] Ошибка цикла: {e}")
    return due_processed


def get_category_limits():
    try:
        ws = get_db().worksheet("Limits")
        records = _get_all_records_safe(ws)
        limits = {}
        for r in records:
            cat = str(r.get("category", "")).strip()
            if cat:
                limits[cat] = parse_amount(r.get("limit_amount", 0))
        return limits
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
            ws.append_row([str(cat), parse_amount(limit)], table_range=_table_range(2))
    except Exception as e:
        print(f"[Лимиты] Ошибка сохранения: {e}")


def get_installments():
    try:
        ws = _get_or_create_installments_sheet()
        records = _get_all_records_safe(ws)
        items = []
        for r in records:
            if str(r.get("status", "active")).lower() not in {"closed", "done", "завершена"}:
                r["total_amount"] = parse_amount(r.get("total_amount", 0))
                r["monthly_payment"] = parse_amount(r.get("monthly_payment", 0))
                items.append(r)
        return items
    except Exception as error:
        print(f"[Рассрочки] Ошибка: {error}")
        return []


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
            "total_amount": parse_amount(payload.get("total_amount", payload.get("amount", 0))),
            "monthly_payment": parse_amount(payload.get("monthly_payment", 0)),
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

SUBSCRIPTION_HEADERS = ["id", "name", "amount", "bank", "day_of_month", "last_paid", "status", "last_warning"]


def _get_or_create_subscriptions_sheet():
    ws = _get_or_create_worksheet("Subscriptions", SUBSCRIPTION_HEADERS, rows=100, cols=len(SUBSCRIPTION_HEADERS))
    headers = ws.row_values(1)
    # Миграция старой схемы из 7 колонок: добавляем last_warning в H.
    if len(headers) < len(SUBSCRIPTION_HEADERS):
        for col_idx, header in enumerate(SUBSCRIPTION_HEADERS, start=1):
            if col_idx > len(headers) or not headers[col_idx - 1]:
                ws.update_cell(1, col_idx, header)
    return ws


def add_or_update_subscription(name: str, amount: float, bank: str, day_of_month: int):
    ws = _get_or_create_subscriptions_sheet()
    records = _get_all_records_safe(ws)
    now = datetime.datetime.now(ASTANA_TZ)
    today_str = now.strftime("%Y-%m-%d")
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Название подписки пустое")
    day = max(1, min(int(parse_amount(day_of_month) or 1), 31))
    amt = parse_amount(amount)
    for idx, r in enumerate(records, start=2):
        if str(r.get("name", "")).strip().lower() == clean_name.lower():
            ws.update_cell(idx, 3, amt)
            ws.update_cell(idx, 4, bank or "Не указан")
            ws.update_cell(idx, 5, day)
            ws.update_cell(idx, 7, "active")
            return {"id": r.get("id"), "name": clean_name, "amount": amt, "bank": bank or "Не указан", "day_of_month": day, "status": "active"}
    sub_id = f"SUB_{now.strftime('%Y%m%d_%H%M%S')}"
    row = [sub_id, clean_name, amt, bank or "Не указан", day, today_str, "active", ""]
    ws.append_row(row, table_range=_table_range(len(SUBSCRIPTION_HEADERS)), value_input_option="USER_ENTERED")
    return {"id": sub_id, "name": clean_name, "amount": amt, "bank": bank or "Не указан", "day_of_month": day, "status": "active"}


def get_active_subscriptions():
    try:
        ws = _get_or_create_subscriptions_sheet()
        records = _get_all_records_safe(ws)
        return [r for r in records if str(r.get("status") or "").strip().lower() == "active"]
    except Exception as e:
        print(f"[Подписки] Ошибка чтения: {e}")
        return []


def deactivate_subscription(name: str):
    try:
        ws = _get_or_create_subscriptions_sheet()
        records = _get_all_records_safe(ws)
        name_lower = str(name or "").lower()
        for idx, r in enumerate(records, start=2):
            if str(r.get("status") or "").lower() == "active" and name_lower in str(r.get("name", "")).lower():
                ws.update_cell(idx, 7, "cancelled")
                return r
        return None
    except Exception as e:
        print(f"[Подписки] Ошибка отмены: {e}")
        return None


def mark_subscription_warning_sent(row_idx: int, warning_date: str):
    try:
        ws = _get_or_create_subscriptions_sheet()
        ws.update_cell(row_idx, 8, warning_date)
    except Exception as e:
        print(f"[Подписки] Не смогла отметить предупреждение: {e}")


def get_subscription_warnings(now: datetime.datetime, days_before: int = 2) -> list[dict]:
    """Вернуть подписки, о которых пора предупредить за N дней до списания."""
    try:
        ws = _get_or_create_subscriptions_sheet()
        records = _get_all_records_safe(ws)
        today = now.date()
        warnings = []
        for idx, r in enumerate(records, start=2):
            if str(r.get("status") or "").strip().lower() != "active":
                continue
            try:
                raw_day = int(parse_amount(r.get("day_of_month", 1)) or 1)
                year, month = today.year, today.month
                import calendar
                day = min(max(raw_day, 1), calendar.monthrange(year, month)[1])
                due = datetime.date(year, month, day)
                if due < today:
                    month = 1 if month == 12 else month + 1
                    year = year + 1 if today.month == 12 else year
                    day = min(max(raw_day, 1), calendar.monthrange(year, month)[1])
                    due = datetime.date(year, month, day)
                if (due - today).days != days_before:
                    continue
                if str(r.get("last_warning", "")).strip() == today.strftime("%Y-%m-%d"):
                    continue
                item = dict(r)
                item["row_idx"] = idx
                item["due_date"] = due.strftime("%Y-%m-%d")
                warnings.append(item)
            except Exception as row_error:
                print(f"[Подписки] Ошибка предупреждения строки {idx}: {row_error}")
        return warnings
    except Exception as e:
        print(f"[Подписки] Ошибка чтения предупреждений: {e}")
        return []

def normalize_existing_family_table_values() -> dict:
    """Разовая мягкая санация старых строк без изменения структуры таблицы.

    Исправляет уже записанные значения:
    - Transactions.user: D/d/Diana -> Диана, Vlad/Vladislav -> Влад
    - Transactions.type: expense/income -> РАСХОД/ДОХОД
    - Reminders.target_user и ShoppingList.added_by — те же алиасы.
    """
    stats = {"transactions_users": 0, "transactions_types": 0, "reminders": 0, "shopping": 0}

    # Transactions
    try:
        ws = get_db().worksheet("Transactions")
        records = _get_all_records_safe(ws)
        for idx, r in enumerate(records, start=2):
            normalized_user = normalize_family_user_name(r.get("user"))
            if normalized_user and normalized_user != r.get("user"):
                ws.update_cell(idx, 3, normalized_user)
                stats["transactions_users"] += 1

            raw_type = str(r.get("type") or "").strip().lower()
            new_type = None
            if raw_type in {"expense", "расход", "out"}:
                new_type = "РАСХОД"
            elif raw_type in {"income", "доход", "in"}:
                new_type = "ДОХОД"
            if new_type and new_type != r.get("type"):
                ws.update_cell(idx, 4, new_type)
                stats["transactions_types"] += 1
    except Exception as e:
        print(f"[Санация] Transactions: {e}")

    # Reminders
    try:
        ws = get_db().worksheet("Reminders")
        records = _get_all_records_safe(ws)
        for idx, r in enumerate(records, start=2):
            normalized = normalize_family_user_name(r.get("target_user"))
            if normalized and normalized != r.get("target_user"):
                ws.update_cell(idx, 3, normalized)
                stats["reminders"] += 1
    except Exception as e:
        print(f"[Санация] Reminders: {e}")

    # ShoppingList
    try:
        ws = get_db().worksheet("ShoppingList")
        records = _get_all_records_safe(ws)
        for idx, r in enumerate(records, start=2):
            normalized = normalize_family_user_name(r.get("added_by"))
            if normalized and normalized != r.get("added_by"):
                ws.update_cell(idx, 3, normalized)
                stats["shopping"] += 1
    except Exception as e:
        print(f"[Санация] ShoppingList: {e}")

    return stats


def add_trip_plan(destination: str, dates: str, budget: float, notes: str):
    # Сохраняем схему Trips как в текущей таблице/PowerBI: trip_id, destination, dates, budget.
    headers = ["trip_id", "destination", "dates", "budget"]
    try:
        ws = _get_or_create_worksheet("Trips", headers, rows=50, cols=4)
        now = datetime.datetime.now(ASTANA_TZ)
        trip_id = f"TRIP_{now.strftime('%Y%m%d_%H%M%S_%f')}"
        ws.append_row(
            [trip_id, destination, dates, parse_amount(budget)],
            table_range=_table_range(4),
            value_input_option="USER_ENTERED",
        )
        return {
            "trip_id": trip_id,
            "destination": destination,
            "dates": dates,
            "budget": parse_amount(budget),
            "notes": notes,
            "status": "planned",
        }
    except Exception as e:
        print(f"[Поездки] Ошибка: {e}")
        raise


def get_planned_trips():
    try:
        ws = get_db().worksheet("Trips")
        records = _get_all_records_safe(ws)
        return [r for r in records if r.get("destination") or r.get("dates")]
    except Exception as e:
        print(f"[Поездки] Ошибка: {e}")
        return []


def add_shopping_items(items: list, user_name: str):
    headers = ["item_id", "date_added", "added_by", "item", "status"]
    try:
        ws = _get_or_create_worksheet("ShoppingList", headers, rows=100, cols=5)
        now = datetime.datetime.now(ASTANA_TZ)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        saved = []
        clean_user = normalize_family_user_name(user_name) or user_name or "Семья"
        for idx, item in enumerate(items or []):
            item_text = str(item).strip()
            if not item_text:
                continue
            item_id = f"SHOP_{now.strftime('%Y%m%d_%H%M%S_%f')}_{idx}"
            row = [item_id, now_str, clean_user, item_text, "active"]
            ws.append_row(row, table_range=_table_range(5), value_input_option="USER_ENTERED")
            saved.append({
                "item_id": item_id,
                "date_added": now_str,
                "added_by": clean_user,
                "item": item_text,
                "status": "active",
            })
        return saved
    except Exception as e:
        print(f"[Покупки] Ошибка: {e}")
        raise


def get_shopping_items():
    try:
        ws = get_db().worksheet("ShoppingList")
        records = _get_all_records_safe(ws)
        return [r for r in records if str(r.get("status") or "").strip().lower() in {"active", ""} and r.get("item")]
    except Exception as e:
        print(f"[Покупки] Ошибка чтения: {e}")
        return []


def mark_shopping_items_done(items_to_remove: list):
    try:
        ws = get_db().worksheet("ShoppingList")
        records = _get_all_records_safe(ws)
        for idx, r in enumerate(records, start=2):
            if str(r.get("status") or "").lower() in {"active", ""}:
                for item_name in items_to_remove:
                    if str(item_name).lower() in str(r.get("item")).lower():
                        ws.update_cell(idx, 5, "done")
    except Exception as e:
        print(f"[Покупки] Ошибка отметки: {e}")
        raise


def add_reminder(target_user: str, remind_at_str: str, text: str, recurrence: str = "once") -> dict:
    """Добавляет напоминание и возвращает сохранённую запись.

    Важно: исключения не проглатываются. Хендлер должен честно сказать, если
    Google Sheets не сохранил строку.
    """
    headers = ["reminder_id", "created_at", "target_user", "remind_at", "text", "status", "recurrence"]
    ws = _get_or_create_worksheet("Reminders", headers, rows=100, cols=7)

    now = datetime.datetime.now(ASTANA_TZ)
    parsed_dt = parse_flexible_datetime(remind_at_str)
    if not parsed_dt:
        raise ValueError(f"Некорректное время напоминания: {remind_at_str}")

    clean_target = normalize_family_user_name(target_user) or str(target_user or "Семья").strip() or "Семья"
    reminder_text = str(text or "Напоминание").strip() or "Напоминание"
    rec = str(recurrence or "once").strip().lower() or "once"
    if rec not in {"once", "daily", "monthly"}:
        rec = "once"

    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    remind_at_norm = parsed_dt.strftime("%Y-%m-%d %H:%M:%S")
    rem_id = f"REM_{now.strftime('%Y%m%d_%H%M%S_%f')}"

    row = [rem_id, now_str, clean_target, remind_at_norm, reminder_text, "pending", rec]
    ws.append_row(row, table_range=_table_range(7), value_input_option="USER_ENTERED")

    return {
        "reminder_id": rem_id,
        "created_at": now_str,
        "target_user": clean_target,
        "remind_at": remind_at_norm,
        "text": reminder_text,
        "status": "pending",
        "recurrence": rec,
    }


def get_pending_reminders():
    try:
        ws = get_db().worksheet("Reminders")
        records = _get_all_records_safe(ws)
        pending = []
        for idx, r in enumerate(records, start=2):
            status = str(r.get("status") or "").strip().lower()
            remind_at = r.get("remind_at")
            text = r.get("text")

            # Старые строки без статуса, но с датой и текстом, считаем активными.
            if status in {"pending", "active", ""} and remind_at and text:
                r["row_idx"] = idx
                if not r.get("status"):
                    r["status"] = "pending"
                if not r.get("reminder_id") and r.get("id"):
                    r["reminder_id"] = r.get("id")
                target = normalize_family_user_name(r.get("target_user"))
                if target:
                    r["target_user"] = target
                pending.append(r)
        return pending
    except Exception as e:
        print(f"[Напоминания] Ошибка чтения: {e}")
        return []

import datetime
import json
import os
import re
import math
import calendar
import threading
from typing import Optional
import gspread
from gspread import utils as gspread_utils
from oauth2client.service_account import ServiceAccountCredentials
from config import GOOGLE_SHEETS_KEY, CREDENTIALS_FILE, normalize_family_user_name
from services.categories import TYPE_EXPENSE, SUBCATEGORIES_MAP, DEFAULT_EXPENSE_LIMITS
from services.money import parse_amount, to_clean_number
from services.banks import normalize_bank_source, normalize_bank
from services.timezone import parse_flexible_datetime, ASTANA_TZ

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

_cached_client = None
_cached_db = None


def _digits_only(text) -> str:
    return "".join(c for c in str(text) if c.isdigit())


def _table_range(num_columns: int) -> str:
    return f"A1:{gspread_utils.rowcol_to_a1(1, num_columns)}"


class _WorksheetProxy:
    """Cache worksheet metadata and short-lived values; invalidate on every bot write."""
    def __init__(self, ws):
        self._ws, self._values, self._at = ws, None, 0
    def __getattr__(self, name):
        value = getattr(self._ws, name)
        if name in {"append_row", "append_rows", "update", "update_cell", "batch_update",
                    "delete_rows", "insert_row", "resize", "clear"}:
            def mutate(*args, **kwargs):
                self._values = None
                return value(*args, **kwargs)
            return mutate
        return value
    def get_all_values(self):
        import time
        ttl = float(os.getenv("SHEETS_READ_CACHE_SECONDS", "10"))
        if self._values is None or time.monotonic()-self._at >= ttl:
            self._values = self._ws.get_all_values()
            self._at = time.monotonic()
        return [list(row) for row in self._values]
    def row_values(self, row):
        values = self.get_all_values()
        return list(values[row-1]) if row <= len(values) else []

_worksheets = {}

def _worksheet(title):
    db = get_db()
    key = (db, title)
    if key not in _worksheets:
        _worksheets[key] = _WorksheetProxy(db.worksheet(title))
    return _worksheets[key]


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
        ws = _worksheet(title)
    except gspread.WorksheetNotFound:
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
            _cached_client.set_timeout(20)
            _cached_db = _cached_client.open_by_key(GOOGLE_SHEETS_KEY)
        else:
            _cached_db.title
    except Exception as e:
        print(f"[Google Таблицы] Переподключение: {e}")
        creds = _load_google_credentials()
        _cached_client = gspread.authorize(creds)
        _cached_client.set_timeout(20)
        _cached_db = _cached_client.open_by_key(GOOGLE_SHEETS_KEY)
    return _cached_db


def normalize_necessity(raw, category=None):
    if category in {"Алкоголь, табак и энергетики", "Красота и уход", "Развлечения и хобби"}:
        return "Want"
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
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("Сумма должна быть положительным конечным числом")
    if amount == int(amount):
        amount = int(amount)

    if not data.get("transaction_id"):
        data["transaction_id"] = f"TRX_{now.strftime('%Y%m%d_%H%M%S_%f')}_{amount}"

    data["date"] = data.get("date") or now.strftime("%Y-%m-%d %H:%M:%S")
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
        raise ValueError("Неизвестный тип операции: нужен ДОХОД или РАСХОД")

    if not data.get("currency"):
        data["currency"] = "KZT"
    if str(data["currency"]).upper() != "KZT":
        raise ValueError("Таблица и отчёты ведутся в KZT; сначала укажите сумму в тенге.")

    bank_value = str(data.get("bank") or "").strip()
    if bank_value.lower() in ("наличные", "нал", "cash"):
        data["resource"] = "Наличные"
        data["bank"] = "Не указан"

    if not data.get("bank"):
        data["bank"] = "Не указан"

    data["bank"] = normalize_bank(data["bank"])
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
    data["amount"] = amount
    row = [data.get(col, "") if data.get(col) is not None else "" for col in columns]

    try:
        ws = _worksheet("Transactions")
        if any(r.get("transaction_id") == data["transaction_id"] for r in _get_all_records_safe(ws)):
            return data
        ws.append_row(row, table_range=_table_range(len(columns)), value_input_option="RAW")
        return data
    except Exception as e:
        print(f"[Транзакции] Не удалось добавить запись: {e}")
        raise


def update_last_transaction_bank_and_source(new_bank: str, user_name: str = "") -> Optional[dict]:
    """Мгновенно обновляет банк и источник в последней записи таблицы."""
    try:
        ws = _worksheet("Transactions")
        records = _get_all_records_safe(ws)
        if not records:
            return None

        candidates = [(i, r) for i, r in enumerate(records, 2)
                      if r.get("transaction_id") and (not user_name or r.get("user") == user_name)]
        if not candidates:
            return None
        last_row_idx, last_rec = candidates[-1]
        bank_norm = normalize_bank(new_bank)
        source_norm = normalize_bank_source(bank_norm, "")

        resource = "Наличные" if bank_norm == "Наличные" else "Карта"
        if bank_norm == "Наличные":
            bank_norm, source_norm = "Не указан", ""
        ws.update(range_name=f"G{last_row_idx}:J{last_row_idx}",
                  values=[[bank_norm, source_norm, last_rec.get("funds_type") or "Собственные", resource]],
                  value_input_option="RAW")
        last_rec.update(bank=bank_norm, source=source_norm, resource=resource)
        return last_rec
    except Exception as e:
        print(f"[Таблицы] Ошибка обновления банка: {e}")
        return None


def ensure_power_bi_dimension_table():
    """Keep Power BI dimension and user defaults; append missing categories only."""
    ws = _get_or_create_worksheet("Dim_Categories", ["category", "default_limit", "type"], rows=50, cols=3)
    headers = ws.row_values(1)
    if headers[:3] != ["category", "default_limit", "type"]:
        raise ValueError("Несовместимые заголовки Dim_Categories")
    present = {str(r.get("category") or "") for r in _get_all_records_safe(ws)}
    missing = [[cat, limit, "РАСХОД"] for cat,limit in DEFAULT_EXPENSE_LIMITS.items() if cat not in present]
    if missing:
        ws.append_rows(missing, value_input_option="RAW")
    return len(missing)


def debug_transactions_snapshot(limit: int = 6) -> str:
    try:
        ws = _worksheet("Transactions")
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
        ws = _worksheet("Transactions")
        records = _get_all_records_safe(ws)
        non_empty = [r for r in records if str(r.get("transaction_id", "")).strip()]
        return [{
            "transaction_id": r.get("transaction_id"),
            "date": r.get("date"), "user": r.get("user"),
            "type": r.get("type") or TYPE_EXPENSE,
            "amt": parse_amount(r.get("amount")),
            "curr": r.get("currency"), "bank": r.get("bank"), "cat": r.get("category"),
            "subcat": r.get("subcategory"), "nec": r.get("necessity"), "comm": r.get("user_comment")
        } for r in non_empty[-200:]]
    except Exception as e:
        print(f"[Транзакции] Ошибка чтения: {e}")
        raise


def get_transactions_for_period(start_date: str, end_date: str) -> list[dict]:
    try:
        ws = _worksheet("Transactions")
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
                    "source": r.get("source", ""),
                    "funds_type": r.get("funds_type", ""),
                    "resource": r.get("resource", ""),
                    "merchant": r.get("merchant", ""),
                    "ai_comment": r.get("ai_comment", ""),
                    "cat": r.get("category", "Прочее"),
                    "subcat": r.get("subcategory", ""),
                    "nec": r.get("necessity", "Want"),
                    "comm": r.get("user_comment", ""),
                })
        return result
    except Exception as e:
        print(f"[Транзакции] Ошибка чтения периода: {e}")
        raise


def find_recent_duplicate_transaction(amount, comment: str = "", minutes: int = 5):
    try:
        target = to_clean_number(amount)
        if not target:
            return None
        ws = _worksheet("Transactions")
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
                if r_date and 0 <= (now - r_date).total_seconds() <= minutes * 60:
                    r_comm = str(r.get("user_comment", "")).lower()
                    if not comm_clean or comm_clean in r_comm or r_comm in comm_clean:
                        return r
            except (ValueError, TypeError):
                continue
        return None
    except Exception as e:
        print(f"[Транзакции] Ошибка поиска дублей: {e}")
        return None


def _select_unique(records, query):
    terms = re.findall(r"\w+", str(query or "").lower())
    if not terms:
        return None
    nonempty = [(i,r) for i,r in enumerate(records, 2) if any(str(v).strip() for v in r.values())]
    if str(query).strip().lower() in {"последняя", "последнюю", "последний", "last"}:
        return nonempty[-1] if nonempty else None
    ignored = {"удали", "удалить", "запись", "трату", "покупку", "измени", "поменяй"}
    terms = [t for t in terms if t not in ignored]
    if not terms:
        return None
    found = [(i,r) for i,r in nonempty if all(t in " ".join(str(v) for v in r.values()).lower() for t in terms)]
    return found[0] if len(found) == 1 else None

def delete_record_by_keyword(worksheet_name, search_query, search_from_recent=True):
    if worksheet_name not in {"Transactions", "ShoppingList", "Trips", "Installments", "Subscriptions"}:
        raise ValueError("Этот раздел нельзя удалять общим редактором")
    ws = _worksheet(worksheet_name)
    found = _select_unique(_get_all_records_safe(ws), search_query)
    if not found:
        return None
    idx, record = found
    ws.delete_rows(idx)
    return record

def find_and_update_record(worksheet_name, search_query, field, new_value, search_from_recent=True):
    allowed = {
         "Transactions": {"date", "user", "type", "amount", "currency", "bank", "source", "funds_type",
                          "resource", "category", "subcategory", "merchant", "necessity", "user_comment", "ai_comment"},
        "ShoppingList": {"item", "status"}, "Trips": {"destination", "dates", "budget", "notes"},
        "Installments": {"description","bank","kind","total_amount","monthly_payment","payments_count","status", "next_payment"}, "Subscriptions": {"name","bank","status", "amount", "day_of_month"},
        "Limits": {"limit_amount"},
    }
    if worksheet_name not in allowed:
        raise ValueError("Раздел не поддерживает общую правку")
    ws = _worksheet(worksheet_name)
    headers = ws.row_values(1)
    if str(field).isdigit():
        idx = int(field)-1
        field = headers[idx] if 0 <= idx < len(headers) else ""
    if field not in allowed[worksheet_name]:
        raise ValueError("Эту колонку нельзя изменять")
    if field in {"amount", "budget", "limit_amount", "day_of_month","total_amount","monthly_payment","payments_count"}:
        new_value = parse_amount(new_value)
        if new_value <= 0 or (field == "day_of_month" and (new_value > 31 or new_value != int(new_value))):
            raise ValueError("Некорректное числовое значение")
    found = _select_unique(_get_all_records_safe(ws), search_query)
    if not found:
        return None
    row, record = found
    updates = {field: new_value}
    if worksheet_name == "Transactions":
        from services.categories import (EXPENSE_CATEGORIES, INCOME_CATEGORIES, TYPE_INCOME,
                                         validate_transaction_category_subcategory)
        if field == "type":
            raw = str(new_value).lower()
            if raw not in {"доход","income","расход","expense"}: raise ValueError("Неизвестный тип операции")
            updates["type"] = "ДОХОД" if raw in {"доход","income"} else "РАСХОД"
        if field == "currency" and str(new_value).upper() != "KZT":
            raise ValueError("Конвертация не включена. Запись должна быть в KZT.")
        if field == "date" and not parse_flexible_datetime(new_value):
            raise ValueError("Некорректная дата")
        if field == "user":
            user = normalize_family_user_name(new_value)
            if not user: raise ValueError("Участник: Влад или Диана")
            updates["user"] = user
        if field == "bank":
            updates["bank"] = normalize_bank(new_value)
            updates["source"] = normalize_bank_source(updates["bank"], "")
        if field in {"type","category","subcategory"}:
            typ = updates.get("type",record.get("type"))
            valid = INCOME_CATEGORIES if typ == TYPE_INCOME else EXPENSE_CATEGORIES
            cat, sub = validate_transaction_category_subcategory(
                updates.get("category",record.get("category")),updates.get("subcategory",record.get("subcategory")),valid)
            updates.update(category=cat,subcategory=sub)
        if field == "necessity":
            updates["necessity"] = normalize_necessity(new_value,record.get("category"))
    result = dict(record,**updates)
    values = [result.get(h,"") for h in headers]
    ws.update(range_name=f"A{row}", values=[values], value_input_option="RAW")
    return result


def mark_reminder_done(row_idx, recurrence="once", remind_at=""):
    """Legacy post-delivery helper. Main uses deliver_due with delivery acknowledgements."""
    from services.reminders import next_occurrence, update_by_id
    ws = _worksheet("Reminders")
    rows = _get_all_records_safe(ws)
    index = int(row_idx)-2
    if not 0 <= index < len(rows):
        return False
    row = rows[index]
    when = remind_at or row.get("remind_at")
    future = next_occurrence(when, recurrence, datetime.datetime.now(ASTANA_TZ), row.get("anchor_day"))
    changes = {"status":"sent"} if future is None else {"remind_at":future.strftime("%Y-%m-%d %H:%M:%S"),
                "status":"pending","anchor_day":row.get("anchor_day") or parse_flexible_datetime(when).day,"deliveries":"{}"}
    return update_by_id(row["reminder_id"], changes)

def split_last_transaction_by_amount(target_amount, part1_amt, part1_cat, part1_comm,
                                      part2_amt, part2_cat, part2_comm, transaction_id="", operation_id=""):
    """One atomic Sheets batch: insert one row and replace original with two parts."""
    from decimal import Decimal
    import uuid
    from services.categories import validate_transaction_category_subcategory
    amounts = [Decimal(str(x)) for x in (target_amount, part1_amt, part2_amt)]
    if any(not x.is_finite() or x <= 0 for x in amounts) or amounts[1]+amounts[2] != amounts[0]:
        return False
    ws = _worksheet("Transactions")
    all_rows = _get_all_records_safe(ws)
    if operation_id and all(any(r.get("transaction_id") == f"{operation_id}_{n}" for r in all_rows) for n in (0,1)):
        return True
    candidates = [(i, row) for i, row in enumerate(all_rows, 2)
                  if row.get("transaction_id") and Decimal(str(parse_amount(row.get("amount")))) == amounts[0]
                  and (not transaction_id or row.get("transaction_id") == transaction_id)]
    if len(candidates) != 1:
        return False
    idx, original = candidates[0]
    headers = ws.row_values(1)[:16]
    rows = []
    for part_idx, (amount, cat, comment) in enumerate(((part1_amt, part1_cat, part1_comm), (part2_amt, part2_cat, part2_comm))):
        cat, sub = validate_transaction_category_subcategory(cat, "")
        item = dict(original, transaction_id=f"{operation_id}_{part_idx}" if operation_id else "SPLIT_"+uuid.uuid4().hex[:16], amount=float(amount),
                    category=cat, subcategory=sub, user_comment=comment,
                    ai_comment=f"Часть исходной операции {original['transaction_id']}")
        cells = []
        for header in headers:
            value = item.get(header, "")
            entered = {"numberValue": value} if isinstance(value, (int, float)) else {"stringValue": str(value)}
            cells.append({"userEnteredValue": entered})
        rows.append({"values": cells})
    get_db().batch_update({"requests": [
        {"insertDimension": {"range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": idx, "endIndex": idx+1},
                             "inheritFromBefore": True}},
        {"updateCells": {"start": {"sheetId": ws.id, "rowIndex": idx-1, "columnIndex": 0},
                         "rows": rows, "fields": "userEnteredValue"}}
    ]})
    if isinstance(ws, _WorksheetProxy): ws._values = None
    return True


def process_due_subscriptions(now: datetime.datetime) -> list:
    due_processed = []
    errors = []
    try:
        ws = _get_or_create_subscriptions_sheet()
        records = _get_all_records_safe(ws)
        current_month_prefix = now.strftime("%Y-%m")
        today_str = now.strftime("%Y-%m-%d")

        for idx, r in enumerate(records, start=2):
            if str(r.get("status")).lower() != "active":
                continue

            try:
                if not r.get("id"):
                    raise ValueError("У подписки отсутствует ID")
                day = int(parse_amount(r.get("day_of_month", 1)))
                last_paid = str(r.get("last_paid", ""))

                day = min(max(1, day), calendar.monthrange(now.year, now.month)[1])
                if now.day >= day and not last_paid.startswith(current_month_prefix):
                    amt = parse_amount(r.get("amount", 0))
                    name = str(r.get("name", "Подписка"))
                    bank = str(r.get("bank", "Не указан"))

                    append_transaction({
                        "transaction_id": f"SUBPAY_{r.get('id')}_{current_month_prefix}",
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
                errors.append(e)

    except Exception as e:
        print(f"[Подписки] Ошибка цикла: {e}")
        raise
    if errors:
        raise RuntimeError(f"Не обработано подписок: {len(errors)}")
    return due_processed


def get_category_limits():
    try:
        ws = _worksheet("Limits")
        records = _get_all_records_safe(ws)
        limits = {}
        for r in records:
            cat = str(r.get("category", "")).strip()
            if cat:
                limits[cat] = parse_amount(r.get("limit_amount", 0))
        return limits
    except Exception as e:
        print(f"[Лимиты] Ошибка: {e}")
        raise


def save_category_limits(new_limits):
    if not new_limits or any(parse_amount(v) < 0 for v in new_limits.values()):
        raise ValueError("Некорректные лимиты")
    ws = _get_or_create_worksheet("Limits", ["category", "limit_amount"], rows=100, cols=2)
    rows = [["category", "limit_amount"]] + [[str(k), parse_amount(v)] for k, v in new_limits.items()]
    old_size = len(ws.get_all_values())
    rows.extend([["", ""] for _ in range(max(0, old_size-len(rows)))])
    ws.update(range_name="A1", values=rows, value_input_option="RAW")


def get_installments():
    try:
        ws = _get_or_create_installments_sheet()
        records = _get_all_records_safe(ws)
        items = []
        for r in records:
            if r.get("id") and str(r.get("status", "active")).lower() not in {"closed", "done", "завершена"}:
                r["total_amount"] = parse_amount(r.get("total_amount", 0))
                r["monthly_payment"] = parse_amount(r.get("monthly_payment", 0))
                items.append(r)
        return items
    except Exception as error:
        print(f"[Рассрочки] Ошибка: {error}")
        raise


INSTALLMENT_COLUMNS = ["id", "date", "user", "bank", "kind", "description", "total_amount", "monthly_payment", "payments_count", "next_payment", "status"]
INSTALLMENT_STATUS_COLUMN = INSTALLMENT_COLUMNS.index("status") + 1

def _get_or_create_installments_sheet():
    return _get_or_create_worksheet("Installments", INSTALLMENT_COLUMNS, rows=100, cols=len(INSTALLMENT_COLUMNS))

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
        if values["total_amount"] <= 0 or values["monthly_payment"] < 0:
            raise ValueError("Некорректная сумма рассрочки")
        if not any(r.get("id") == values["id"] for r in _get_all_records_safe(ws)):
            ws.append_row([values[c] for c in INSTALLMENT_COLUMNS], table_range=_table_range(len(INSTALLMENT_COLUMNS)), value_input_option="RAW")
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
        if ws.col_count < len(SUBSCRIPTION_HEADERS):
            ws.resize(cols=len(SUBSCRIPTION_HEADERS))
        for col_idx, header in enumerate(SUBSCRIPTION_HEADERS, start=1):
            if col_idx > len(headers) or not headers[col_idx - 1]:
                ws.update_cell(1, col_idx, header)
    return ws


def add_or_update_subscription(name: str, amount: float, bank: str, day_of_month: int, paid_this_month: bool = False):
    ws = _get_or_create_subscriptions_sheet()
    records = _get_all_records_safe(ws)
    now = datetime.datetime.now(ASTANA_TZ)
    today_str = now.strftime("%Y-%m-%d")
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Название подписки пустое")
    day = parse_amount(day_of_month)
    amt = parse_amount(amount)
    if not 1 <= day <= 31 or int(day) != day or amt <= 0:
        raise ValueError("Нужны положительная сумма и день списания 1–31")
    day = int(day)
    for idx, r in enumerate(records, start=2):
        if str(r.get("name", "")).strip().lower() == clean_name.lower():
            ws.update(range_name=f"C{idx}:H{idx}", values=[[amt, bank or "Не указан", day,
                today_str if paid_this_month else r.get("last_paid", ""), "active", r.get("last_warning", "")]],
                value_input_option="RAW")
            return {"id": r.get("id"), "name": clean_name, "amount": amt, "bank": bank or "Не указан", "day_of_month": day, "status": "active"}
    sub_id = f"SUB_{now.strftime('%Y%m%d_%H%M%S_%f')}"
    row = [sub_id, clean_name, amt, bank or "Не указан", day, today_str if paid_this_month else "", "active", ""]
    ws.append_row(row, table_range=_table_range(len(SUBSCRIPTION_HEADERS)), value_input_option="RAW")
    return {"id": sub_id, "name": clean_name, "amount": amt, "bank": bank or "Не указан", "day_of_month": day, "status": "active"}


def get_active_subscriptions():
    try:
        ws = _get_or_create_subscriptions_sheet()
        records = _get_all_records_safe(ws)
        return [r for r in records if str(r.get("status") or "").strip().lower() == "active"]
    except Exception as e:
        print(f"[Подписки] Ошибка чтения: {e}")
        raise


def deactivate_subscription(name: str):
    try:
        ws = _get_or_create_subscriptions_sheet()
        records = _get_all_records_safe(ws)
        name_lower = str(name or "").strip().lower()
        if not name_lower:
            return None
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
    """Original startup sanitation, batched and limited to documented aliases/types."""
    from services.reminders import normalize_target
    stats = {"transactions_users": 0, "transactions_types": 0, "reminders": 0, "shopping": 0}
    for title, field, column, stat in (
        ("Transactions", "user", "C", "transactions_users"),
        ("Reminders", "target_user", "C", "reminders"),
        ("ShoppingList", "added_by", "C", "shopping")):
        ws = _worksheet(title)
        updates = []
        for idx, r in enumerate(_get_all_records_safe(ws), 2):
            raw = r.get(field)
            normalized = normalize_family_user_name(raw)
            if title == "Reminders" and raw:
                try: normalized = normalize_target(raw)
                except ValueError: pass
            if normalized and normalized != raw:
                updates.append({"range": f"{column}{idx}", "values": [[normalized]]})
                stats[stat] += 1
            if title == "Transactions":
                t = str(r.get("type") or "").strip().lower()
                new_type = "ДОХОД" if t in {"income","in","доход"} else "РАСХОД" if t in {"expense","out","расход"} else None
                if new_type and new_type != r.get("type"):
                    updates.append({"range": f"D{idx}", "values": [[new_type]]})
                    stats["transactions_types"] += 1
        if updates:
            ws.batch_update(updates, value_input_option="RAW")
    return stats


def add_trip_plan(destination: str, dates: str, budget: float, notes: str):
    # Сохраняем схему Trips как в текущей таблице/PowerBI: trip_id, destination, dates, budget.
    headers = ["trip_id", "destination", "dates", "budget", "notes"]
    try:
        ws = _get_or_create_worksheet("Trips", headers, rows=50, cols=5)
        existing_headers = ws.row_values(1)
        if len(existing_headers) < 5:
            if ws.col_count < 5: ws.resize(cols=5)
            ws.update_cell(1, 5, "notes")
        elif existing_headers[4] != "notes":
            raise ValueError("Колонка E Trips занята другой схемой")
        now = datetime.datetime.now(ASTANA_TZ)
        trip_id = f"TRIP_{now.strftime('%Y%m%d_%H%M%S_%f')}"
        ws.append_row(
            [trip_id, destination, dates, parse_amount(budget), notes],
            table_range=_table_range(5),
            value_input_option="RAW",
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
        ws = _worksheet("Trips")
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
            ws.append_row(row, table_range=_table_range(5), value_input_option="RAW")
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
        ws = _worksheet("ShoppingList")
        records = _get_all_records_safe(ws)
        return [r for r in records if str(r.get("status") or "").strip().lower() in {"active", ""} and r.get("item")]
    except Exception as e:
        print(f"[Покупки] Ошибка чтения: {e}")
        raise


def mark_shopping_items_done(items_to_remove: list):
    ws = _worksheet("ShoppingList")
    records = _get_all_records_safe(ws)
    terms = [str(x).strip().lower() for x in items_to_remove if str(x).strip()]
    done = []
    for idx, r in enumerate(records, 2):
        if r.get("item") and str(r.get("status") or "").lower() in {"active", ""}:
            if any(t in str(r["item"]).lower() or t == str(r.get("item_id","")).lower() for t in terms):
                ws.update_cell(idx, 5, "done")
                done.append(r)
    return done


def add_reminder(target_user, remind_at_str, text, recurrence="once"):
    from services.reminders import add
    return add(target_user, remind_at_str, text, recurrence)

def get_pending_reminders():
    from services.reminders import pending
    return pending()

# gspread's shared HTTP session and read-modify-write operations must be serialized.
_sheet_lock = threading.RLock()
def _serialized(fn):
    from functools import wraps
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _sheet_lock:
            return fn(*args, **kwargs)
    return wrapped

for _name, _value in list(globals().items()):
    import inspect
    if inspect.isfunction(_value) and getattr(_value, "__module__", None) == __name__ and _name != "_serialized":
        globals()[_name] = _serialized(_value)

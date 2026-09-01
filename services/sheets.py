import datetime
import json
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from config import GOOGLE_SHEETS_KEY, CREDENTIALS_FILE
from services.categories import TYPE_EXPENSE
from services.money import parse_amount, to_clean_number
from services.banks import normalize_bank_source

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

ASTANA_TZ = datetime.timezone(datetime.timedelta(hours=5), name="Asia/Astana")


def _digits_only(text) -> str:
    return "".join(c for c in str(text) if c.isdigit())


def _table_range(num_columns: int) -> str:
    """"A1:<last_col>1" для заданного числа колонок.

    Передаётся в append_row(..., table_range=...), чтобы Google Sheets
    API не пытался сам "угадывать" границы таблицы. Без этого при
    определённых обстоятельствах (например, если в какой-то строке
    случайно оказались данные в колонках правее ожидаемых — как один
    раз уже случилось с Transactions) API мог "расширить" определяемую
    ширину таблицы, и все последующие append_row начинали писать со
    сдвигом вправо, а не с колонки A.
    """
    return f"A1:{gspread.utils.rowcol_to_a1(1, num_columns)}"


def _get_all_records_safe(ws):
    """Как ws.get_all_records(), но без строгой проверки заголовков на дубли/пустоту.

    Подтверждённая причина бага "графики всегда пустые": ws.get_all_records()
    требует, чтобы ВСЕ заголовки в строке 1 были уникальными и непустыми, и
    кидает исключение, если это не так. Однажды в таблицу попали лишние
    данные правее нужных 16 колонок (баг с "уехавшей" таблицей), из-за чего
    строка заголовков получила несколько пустых ячеек — и ЛЮБОЕ чтение
    таблицы стало падать целиком, включая графики, сводки, поиск дублей и
    саму диагностику /debug. Эта функция просто сопоставляет строку 1 с
    остальными строками как есть, без такой проверки, и не ломается от
    лишних/пустых заголовков правее нужных колонок.
    """
    all_values = ws.get_all_values()
    if not all_values:
        return []
    headers = all_values[0]
    records = []
    for row in all_values[1:]:
        padded = row + [""] * (len(headers) - len(row))
        records.append(dict(zip(headers, padded)))
    return records

_cached_client = None
_cached_db = None

def _load_google_credentials():
    """Загрузить сервисный ключ Google — из переменной окружения (для облачного
    хостинга вроде Railway, где нет удобной загрузки файлов) или из файла
    (для Replit/локального запуска, как раньше).

    Приоритет: если задана переменная окружения GOOGLE_CREDENTIALS_JSON
    (весь credentials.json одной строкой) — используем её. Иначе — старое
    поведение, файл по пути CREDENTIALS_FILE.
    """
    raw_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if raw_json:
        try:
            info = json.loads(raw_json)
            return ServiceAccountCredentials.from_json_keyfile_dict(info, scope)
        except (json.JSONDecodeError, ValueError) as error:
            raise RuntimeError(
                "GOOGLE_CREDENTIALS_JSON задан, но не разбирается как JSON — "
                "проверь, что скопирован весь файл credentials.json целиком."
            ) from error
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
        print(f"[Google Таблицы] Ошибка переподключения: {e}")
        creds = _load_google_credentials()
        _cached_client = gspread.authorize(creds)
        _cached_db = _cached_client.open_by_key(GOOGLE_SHEETS_KEY)
    return _cached_db

# --- ТРАНЗАКЦИИ И АНАЛИТИКА ---
def append_transaction(data: dict):
    now = datetime.datetime.now(ASTANA_TZ)
    from services.categories import (
        normalize_subcategory, normalize_necessity, normalize_strict_bank, TYPE_INCOME, TYPE_EXPENSE
    )
    
    raw_amount = data.get("amount", 0)
    amount = parse_amount(raw_amount)
    if amount == int(amount): amount = int(amount)

    data["transaction_id"] = data.get("transaction_id") or f"TRX_{now.strftime('%Y%m%d_%H%M%S')}_{amount}"
    data["date"] = now.strftime("%Y-%m-%d %H:%M:%S")
    data["user"] = data.get("user") or "Влад"
    
    typ = data.get("type") or TYPE_EXPENSE
    if typ not in (TYPE_EXPENSE, TYPE_INCOME): typ = TYPE_EXPENSE
    data["type"] = typ

    cat = data.get("category", "")
    data["subcategory"] = normalize_subcategory(cat, data.get("subcategory"))
    data["necessity"] = normalize_necessity(data.get("necessity"), cat) if typ != TYPE_INCOME else ""
    
    res = data.get("resource")
    data["resource"] = res if res in ["Карта", "Наличные", "Счет"] else "Карта"
    data["bank"] = normalize_strict_bank(data.get("bank"), data["resource"])
    data["source"] = normalize_bank_source(data["bank"], data.get("source"))
    
    funds = data.get("funds_type")
    data["funds_type"] = funds if funds in ["Собственные", "Кредитные", "Рассрочка"] else "Собственные"
    data["currency"] = data.get("currency") or "KZT"

    try:
        ws = get_db().worksheet("Transactions")
        ws.append_row(row, table_range=_table_range(len(columns)))
    except Exception as e:
        print(f"[Транзакции] Не удалось добавить запись: {e}")

def debug_transactions_snapshot(limit: int = 6) -> str:
    """Диагностический снимок листа Transactions — для команды /debug.

    Показывает то, что РЕАЛЬНО видит код: сколько всего строк, сколько
    из них считаются "настоящими" (есть transaction_id), заголовки и
    сырые значения последних записей — включая ТИП значения даты
    (str/datetime), потому что именно скрытая смена типа/формата ячейки
    один раз уже ломала фильтрацию по месяцу молча, без единой ошибки.
    """
    try:
        ws = get_db().worksheet("Transactions")
        all_values = ws.get_all_values()
        records = _get_all_records_safe(ws)
        non_empty = [r for r in records if str(r.get("transaction_id", "")).strip()]
        now = datetime.datetime.now(ASTANA_TZ)
        current_month = now.strftime("%Y-%m")

        lines = [
            f"Всего строк в get_all_records(): {len(records)}",
            f"Из них с непустым transaction_id: {len(non_empty)}",
            f"Текущий месяц (фильтр графика/сводки): {current_month}",
            f"Заголовки (строка 1): {all_values[0] if all_values else '—'}",
            "",
            "Последние записи (что реально прочитано):",
        ]
        for r in non_empty[-limit:]:
            date_val = r.get("date")
            lines.append(
                f"- id={r.get('transaction_id')!r}\n"
                f"  date={date_val!r} (тип: {type(date_val).__name__})\n"
                f"  type={r.get('type')!r} amount={r.get('amount')!r} (тип: {type(r.get('amount')).__name__})\n"
                f"  category={r.get('category')!r}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Ошибка при снятии диагностики: {e}"

def get_last_200_transactions():
    """Последние 200 операций (расходы и доходы) — для контекста ИИ, графика и чата.

    Для точных денежных подсчётов за конкретный период (месяц/неделя)
    используйте get_transactions_for_period — здесь возможна потеря
    старых записей при очень высокой активности (>200 операций за период).
    """
    try:
        ws = get_db().worksheet("Transactions")
        records = _get_all_records_safe(ws)
        # Google Sheets по умолчанию резервирует куда больше строк, чем реально
        # используется — get_all_records() возвращает и эти пустые "хвостовые"
        # строки тоже. Без фильтра срез "последние 200" мог целиком состоять
        # из пустых строк вместо настоящих последних операций (из-за чего
        # график/сводки показывали "данных нет" при полной таблице).
        non_empty = [r for r in records if str(r.get("transaction_id", "")).strip()]
        return [{
            "date": r.get("date"), "user": r.get("user"),
            "type": r.get("type") or TYPE_EXPENSE,
            "amt": r.get("amount"),
            "curr": r.get("currency"), "bank": r.get("bank"), "cat": r.get("category"), "comm": r.get("user_comment")
        } for r in non_empty[-200:]]
    except Exception as e:
        print(f"[Транзакции] Не удалось прочитать записи: {e}")
        return []

def get_transactions_for_period(start_date: str, end_date: str) -> list[dict]:
    """Все операции с датой в диапазоне [start_date, end_date) в формате YYYY-MM-DD.

    В отличие от get_last_200_transactions читает ВСЮ таблицу и не
    ограничена 200 последними строками — используйте для точных
    сумм за месяц/неделю (сводки, дайджесты, лимиты).
    """
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
                    "cat": r.get("category"), "comm": r.get("user_comment"),
                })
        return result
    except Exception as e:
        print(f"[Транзакции] Не удалось прочитать период {start_date}–{end_date}: {e}")
        return []

# --- РАССРОЧКИ И KASPI RED ---
INSTALLMENT_COLUMNS = [
    "id", "date", "user", "bank", "kind", "description",
    "total_amount", "monthly_payment", "payments_count",
    "next_payment", "status",
]
INSTALLMENT_STATUS_COLUMN = INSTALLMENT_COLUMNS.index("status") + 1  # 1-индексация для gspread

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
        return [
            record for record in records
            if str(record.get("status", "active")).lower() not in {"closed", "done", "завершена"}
        ]
    except Exception as error:
        print(f"[Рассрочки] Не удалось получить данные: {error}")
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
        ws.append_row([str(values[column] or "") for column in INSTALLMENT_COLUMNS], table_range=_table_range(len(INSTALLMENT_COLUMNS)))
        return values
    except Exception as error:
        print(f"[Рассрочки] Не удалось добавить запись: {error}")
        return None

def close_installment(search_query: str) -> bool:
    """Отметить рассрочку/Kaspi Red как закрытую (выплаченную) по ключевому слову."""
    return find_and_update_record(
        "Installments", search_query, INSTALLMENT_STATUS_COLUMN, "closed", search_from_recent=True
    )

def get_category_limits():
    try:
        ws = get_db().worksheet("Limits")
        records = _get_all_records_safe(ws)
        return {str(r.get("category")).strip(): float(r.get("limit_amount", 0)) for r in records if r.get("category")}
    except Exception as e:
        print(f"[Лимиты] Не удалось прочитать лимиты: {e}")
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
        print(f"[Лимиты] Не удалось сохранить лимиты: {e}")

def get_current_month_spending_by_category(category_name: str) -> float:
    """Сумма РАСХОДОВ (не доходов) по категории за текущий месяц."""
    try:
        ws = get_db().worksheet("Transactions")
        records = _get_all_records_safe(ws)
        now = datetime.datetime.now(ASTANA_TZ)
        current_year_month = now.strftime("%Y-%m")

        total = 0.0
        for r in records:
            date_str = str(r.get("date", ""))
            if not date_str.startswith(current_year_month):
                continue
            if str(r.get("type") or TYPE_EXPENSE) != TYPE_EXPENSE:
                continue
            cat = str(r.get("category", "")).strip()
            if cat.lower() == category_name.lower():
                try:
                    total += float(r.get("amount", 0))
                except ValueError:
                    pass
        return total
    except Exception as e:
        print(f"[Лимиты] Не удалось посчитать расходы за месяц: {e}")
        return 0.0

def find_recent_duplicate_transaction(amount, minutes: int = 10):
    """Проверить, не записывали ли уже такую же сумму совсем недавно.

    Не блокирует запись — только предупреждает. Пригодится, когда одну и
    ту же покупку записали дважды разными путями (например, сначала
    текстом пуш-уведомления, потом тем же чеком-скриншотом) — раньше это
    тихо создавало два ряда в таблице без единого намёка пользователю.

    Возвращает похожую запись (dict) или None.
    """
    try:
        target = to_clean_number(amount)
        if not target:
            return None
        ws = get_db().worksheet("Transactions")
        records = _get_all_records_safe(ws)
        non_empty = [r for r in records if str(r.get("transaction_id", "")).strip()]
        now = datetime.datetime.now(ASTANA_TZ)

        for r in reversed(non_empty[-50:]):
            try:
                r_amount = to_clean_number(r.get("amount"))
                if r_amount != target:
                    continue
                r_date = datetime.datetime.strptime(str(r.get("date")), "%Y-%m-%d %H:%M:%S").replace(tzinfo=ASTANA_TZ)
                if (now - r_date).total_seconds() <= minutes * 60:
                    return r
            except (ValueError, TypeError):
                continue
        return None
    except Exception as e:
        print(f"[Транзакции] Не удалось проверить на дубли: {e}")
        return None

# --- РАЗДЕЛЕНИЕ ТРАНЗАКЦИИ СО СДВИГОМ ВНИЗ ---
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

        ws.delete_rows(target_idx + 1)

        row1 = [
            f"TRX_{now.strftime('%Y%m%d_%H%M%S')}_1", base_date, base_user, base_type, part1_amt, "KZT",
            base_bank, base_source, base_funds, base_resource, part1_cat, "", base_merchant, base_necessity, part1_comm, "Разделено по запросу."
        ]
        row2 = [
            f"TRX_{now.strftime('%Y%m%d_%H%M%S')}_2", base_date, base_user, base_type, part2_amt, "KZT",
            base_bank, base_source, base_funds, base_resource, part2_cat, "", base_merchant, base_necessity, part2_comm, "Разделено по запросу."
        ]

        ws.insert_row(row1, target_idx + 1)
        ws.insert_row(row2, target_idx + 2)
        return True
    except Exception as e:
        print(f"[Транзакции] Не удалось разделить запись: {e}")
        return False

# --- УНИВЕРСАЛЬНЫЕ ПРАВКИ ---
def find_and_update_record(worksheet_name: str, search_query, field, new_value, search_from_recent: bool = True):
    """Найти строку по ключевому слову/сумме и обновить одну ячейку.

    'field' — это ИМЯ поля из заголовка листа (например "amount",
    "category", "bank"), а не номер колонки: номер определяется САМ по
    первой строке листа, чтобы ИИ не приходилось угадывать/хардкодить
    номера столбцов. Можно передать и число напрямую — тогда оно
    используется как есть.

    По умолчанию ищет от САМОЙ СВЕЖЕЙ записи к самой старой
    (search_from_recent=True), потому что в чате "исправь"/"удали" почти
    всегда означает "то, что я только что записал". Если текстовое
    совпадение не нашлось, а запрос похож на число — дополнительно
    пробуем сравнить только цифры (устойчивее к разнице в форматировании
    сумм, например "8,000" против "8000").

    Возвращает НАЙДЕННУЮ (до правки) строку в виде dict, либо None, если
    совпадений не найдено или колонку не удалось определить.
    """
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
                field_lower = field_str.lower()
                if field_lower in headers_lower:
                    col_idx = headers_lower.index(field_lower) + 1

        if not col_idx:
            print(f"[Таблицы] Не удалось определить колонку «{field}» в {worksheet_name} (заголовки: {headers_lower})")
            return None

        records = _get_all_records_safe(ws)
        search_query_lower = str(search_query).strip().lower()
        query_digits = _digits_only(search_query_lower)

        indexed_records = list(enumerate(records, start=2))
        if search_from_recent:
            indexed_records = list(reversed(indexed_records))

        for idx, r in indexed_records:
            row_values = [str(v) for v in r.values()]
            row_str = " ".join(row_values).lower()
            matched = search_query_lower in row_str
            if not matched and query_digits:
                matched = any(_digits_only(v) == query_digits for v in row_values if _digits_only(v))
            if matched:
                ws.update_cell(idx, col_idx, new_value)
                return r
        return None
    except Exception as e:
        print(f"[Таблицы] Не удалось обновить запись в {worksheet_name}: {e}")
        return None

def delete_record_by_keyword(worksheet_name: str, search_query: str, search_from_recent: bool = True):
    """Найти строку по ключевому слову (или по сумме) и удалить её. См. find_and_update_record
    про порядок поиска, цифровое сравнение и про то, что возвращается удалённая строка
    (или None), а не просто True/False."""
    try:
        ws = get_db().worksheet(worksheet_name)
        records = _get_all_records_safe(ws)
        search_query_lower = str(search_query).strip().lower()
        query_digits = _digits_only(search_query_lower)

        indexed_records = list(enumerate(records, start=2))
        if search_from_recent:
            indexed_records = list(reversed(indexed_records))

        for idx, r in indexed_records:
            row_values = [str(v) for v in r.values()]
            row_str = " ".join(row_values).lower()
            matched = search_query_lower in row_str
            if not matched and query_digits:
                matched = any(_digits_only(v) == query_digits for v in row_values if _digits_only(v))
            if matched:
                ws.delete_rows(idx)
                return r
        return None
    except Exception as e:
        print(f"[Таблицы] Не удалось удалить запись в {worksheet_name}: {e}")
        return None

# --- РЕГУЛЯРНЫЕ ПЛАТЕЖИ ---
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
        print(f"[Подписки] Не удалось обновить подписку: {e}")

def get_active_subscriptions():
    try:
        ws = get_db().worksheet("Subscriptions")
        records = _get_all_records_safe(ws)
        return [r for r in records if str(r.get("status")).lower() == "active"]
    except Exception as e:
        print(f"[Подписки] Не удалось прочитать подписки: {e}")
        return []

def deactivate_subscription(name: str):
    """Отметить подписку/регулярный платёж как отменённые (без удаления истории).

    Возвращает найденную запись (dict) или None, если активной подписки
    с таким именем не нашлось.
    """
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
        print(f"[Подписки] Не удалось отменить подписку: {e}")
        return None

# --- ПОЕЗДКИ ---
def add_trip_plan(destination: str, dates: str, budget: float, notes: str):
    try:
        ws = get_db().worksheet("Trips")
        now = datetime.datetime.now(ASTANA_TZ)
        trip_id = f"TRIP_{now.strftime('%Y%m%d_%H%M%S')}"
        ws.append_row([trip_id, destination, dates, budget, notes, "planned"], table_range=_table_range(6))
    except Exception as e:
        print(f"[Поездки] Не удалось добавить поездку: {e}")

def get_planned_trips():
    try:
        ws = get_db().worksheet("Trips")
        records = _get_all_records_safe(ws)
        return [r for r in records if str(r.get("status")).lower() == "planned"]
    except Exception as e:
        print(f"[Поездки] Не удалось прочитать поездки: {e}")
        return []

# --- СПИСОК ПОКУПОК И НАПОМИНАНИЯ ---
def add_shopping_items(items: list, user_name: str):
    try:
        ws = get_db().worksheet("ShoppingList")
        now = datetime.datetime.now(ASTANA_TZ)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        for item in items:
            item_id = f"SHOP_{now.strftime('%Y%m%d_%H%M%S')}"
            ws.append_row([item_id, now_str, user_name, item, "active"], table_range=_table_range(5))
    except Exception as e:
        print(f"[Покупки] Не удалось добавить товары: {e}")

def get_shopping_items():
    try:
        ws = get_db().worksheet("ShoppingList")
        records = _get_all_records_safe(ws)
        return [r for r in records if str(r.get("status")).lower() == "active"]
    except Exception as e:
        print(f"[Покупки] Не удалось прочитать список: {e}")
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
        print(f"[Покупки] Не удалось обновить список: {e}")

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
        print(f"[Напоминания] Не удалось прочитать напоминания: {e}")
        return []

def mark_reminder_done(row_idx: int, recurrence: str = "once", remind_at: str = ""):
    try:
        ws = get_db().worksheet("Reminders")
        if recurrence == "daily" and remind_at:
            try:
                old_dt = datetime.datetime.strptime(remind_at, "%Y-%m-%d %H:%M:%S")
                next_dt = old_dt + datetime.timedelta(days=1)
                ws.update_cell(row_idx, 4, next_dt.strftime("%Y-%m-%d %H:%M:%S"))
            except Exception as dt_err:
                print(f"[Напоминания] Не удалось перенести ежедневное напоминание: {dt_err}")
                ws.update_cell(row_idx, 6, "sent")
        else:
            ws.update_cell(row_idx, 6, "sent")
    except Exception as e:
        print(f"[Напоминания] Не удалось отметить напоминание: {e}")

def fix_entire_table_by_strict_rules():
    try:
        from services.categories import (
            normalize_category, normalize_subcategory, normalize_necessity, 
            normalize_strict_bank, EXPENSE_CATEGORIES, INCOME_CATEGORIES, 
            FALLBACK_EXPENSE_CATEGORY, TYPE_EXPENSE, TYPE_INCOME
        )
        ws = get_db().worksheet("Transactions")
        all_values = ws.get_all_values()
        if len(all_values) <= 1: return "Таблица пуста, нечего исправлять."
            
        headers = [str(h).strip().lower() for h in all_values[0]]
        col_map = {h: i for i, h in enumerate(headers)}
        
        updates = []
        for row_idx, row in enumerate(all_values[1:], start=2):
            padded = row + [""] * (len(headers) - len(row))
            
            cat_idx, sub_idx, nec_idx, bank_idx, res_idx, type_idx, curr_idx, funds_idx = (
                col_map.get(k) for k in ["category", "subcategory", "necessity", "bank", "resource", "type", "currency", "funds_type"]
            )
            
            def get_val(idx): return padded[idx] if idx is not None else ""
            cat, sub, nec, bank, res, typ, curr, funds = map(get_val, [cat_idx, sub_idx, nec_idx, bank_idx, res_idx, type_idx, curr_idx, funds_idx])
            
            new_type = typ if typ in [TYPE_EXPENSE, TYPE_INCOME] else TYPE_EXPENSE
            if new_type == TYPE_INCOME:
                new_cat = normalize_category(cat, INCOME_CATEGORIES, "Кэшбэк и прочие поступления")
                new_nec = "" 
            else:
                new_cat = normalize_category(cat, EXPENSE_CATEGORIES, FALLBACK_EXPENSE_CATEGORY)
                new_nec = normalize_necessity(nec, new_cat)
                
            new_sub = normalize_subcategory(new_cat, sub)
            new_res = res if res in ["Карта", "Наличные", "Счет"] else "Карта"
            new_bank = normalize_strict_bank(bank, new_res)
            new_curr = curr if curr else "KZT"
            new_funds = funds if funds in ["Собственные", "Кредитные", "Рассрочка"] else "Собственные"
            
            def add_update(idx, new_val):
                if idx is not None and padded[idx] != new_val:
                    updates.append({'range': gspread.utils.rowcol_to_a1(row_idx, idx+1), 'values': [[new_val]]})
            
            add_update(cat_idx, new_cat)
            add_update(sub_idx, new_sub)
            add_update(nec_idx, new_nec)
            add_update(bank_idx, new_bank)
            add_update(type_idx, new_type)
            add_update(curr_idx, new_curr)
            add_update(res_idx, new_res)
            add_update(funds_idx, new_funds)
            
        if updates:
            ws.batch_update(updates)
            return f"✅ Таблица отполирована по строгим правилам! Изменено ячеек: {len(updates)}.\nБольше никаких пустых банков и русских 'нужное'."
        else:
            return "✅ Таблица проверена. Всё уже идеально заполнено по правилам."
    except Exception as e:
        print(f"[Таблицы] Ошибка массовой проверки: {e}")
        return f"⚠️ Не смогла обновить таблицу целиком: {e}"

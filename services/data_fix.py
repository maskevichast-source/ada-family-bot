"""Разовая (и повторяемая) чистка таблицы Transactions.

/fix_data                — показать, что изменится (ничего не пишет)
/fix_data да             — применить (перед этим резервная копия, если настроена)
/fix_data откат          — вернуть прежние значения
/fix_data удалить <ID>   — удалить одну запись по точному transaction_id (только по команде)

Что чинит:
  1. Карта рассрочки (Kaspi Red, Ozen…), а тип средств «Собственные» → «Рассрочка»
     (платёж банку по кредиту не трогаем).
  2. Источник «Основная карта» у записи с известным банком → «BCC Pay», «Kaspi Gold» и т.п.
  3. Доходы: нужность (Need/Want) очищается, пустая подкатегория = категория дохода.
  4. Очевидные ошибки категории: запись, где в комментарии ТОЛЬКО «стики»/«сигареты»/«пачка сигарет»
     (а категория другая), и корм/наполнитель в зоомагазине вне «Питомцев».
  5. Даты, записанные текстом, → настоящая дата-время (и формат колонки).
Дубли только показываются, сами не удаляются.

Строки находятся по transaction_id, не по номеру строки.
"""
import datetime
import re

from services import state
from services.banks import INSTALLMENT_SOURCES_LOWER, KNOWN_SOURCES_BY_BANK
from services.loan_rules import LOAN_SUBCATEGORY
from services.money import parse_amount

NAMESPACE = "data_fix"
MAX_PREVIEW_ROWS = 8
_TEXT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}(:\d{2})?$")
DATE_PATTERN = "yyyy-mm-dd hh:mm:ss"

# какое поле → заголовок колонки
FIELDS = ("funds_type", "source", "necessity", "subcategory")
_SMOKE_ONLY = {"стики", "пачка стиков", "сигареты", "пачка сигарет", "пачка сиг", "стики для iqos", "стики iqos",
               "сигарета", "пачка сигарет.", "стики."}
_PET_STORE_RE = re.compile(r"zoo|зоо|petshop|pet shop|zootrade", re.IGNORECASE)
_PET_ITEM_RE = re.compile(r"корм|наполнител|лоток|когтеточк|лакомств", re.IGNORECASE)
HARMFUL_CAT = "Алкоголь, табак и энергетики"
KIND_TITLES = {
    "funds": "карта рассрочки, но тип «Собственные» → «Рассрочка»",
    "source": "источник «Основная карта» → настоящая карта банка",
    "income": "доходы: чистка нужности и подкатегории",
    "date": "дата записана текстом → настоящая дата",
    "category": "очевидная ошибка категории (только стики/сигареты и корм/наполнитель для питомца)",
}


def _ws_values():
    from services import sheets

    ws = sheets.get_db().worksheet("Transactions")
    return ws, ws.get_all_values()


def raw_date_cells(ws) -> list:
    """Колонка B без форматирования: строка — текст, число — настоящая дата. Список с шапки."""
    result = ws.get("B1:B", value_render_option="UNFORMATTED_VALUE")
    return [row[0] if row else "" for row in result]


def _col_letter(number: int) -> str:
    from services.sheets import _column_letter
    return _column_letter(number)


def find_changes() -> dict:
    """{"fields": [...], "dates": [...], "duplicates": [...]}. Ничего не пишет."""
    ws, values = _ws_values()
    if not values:
        return {"fields": [], "dates": [], "duplicates": []}
    header = [str(h).strip() for h in values[0]]
    col = {name: header.index(name) for name in header if name}

    def cell(row, name):
        i = col.get(name)
        return str(row[i]).strip() if i is not None and i < len(row) else ""

    fields, rows_by_id = [], {}
    for row_number, row in enumerate(values[1:], start=2):
        tid = cell(row, "transaction_id")
        if not tid:
            continue
        rows_by_id[tid] = row_number
        typ = cell(row, "type").upper()
        source, bank = cell(row, "source"), cell(row, "bank")
        changes = {}
        kinds = []
        if (typ == "РАСХОД" and source.lower() in INSTALLMENT_SOURCES_LOWER
                and cell(row, "funds_type") != "Рассрочка" and cell(row, "subcategory") != LOAN_SUBCATEGORY):
            changes["funds_type"] = ("Рассрочка", cell(row, "funds_type"))
            kinds.append("funds")
        known = KNOWN_SOURCES_BY_BANK.get(bank.lower())
        if source == "Основная карта" and known and cell(row, "resource") != "Наличные":
            changes["source"] = (known, source)
            kinds.append("source")
        if typ == "ДОХОД":
            if cell(row, "necessity"):
                changes["necessity"] = ("", cell(row, "necessity"))
                kinds.append("income")
            if not cell(row, "subcategory") and cell(row, "category"):
                changes["subcategory"] = (cell(row, "category"), "")
                if "income" not in kinds:
                    kinds.append("income")
        category, comment = cell(row, "category"), cell(row, "user_comment")
        if typ == "РАСХОД":
            if comment.lower().strip(" .!\n") in _SMOKE_ONLY and category != HARMFUL_CAT:
                changes["category"] = (HARMFUL_CAT, category)
                changes["subcategory"] = ("Сигареты и стики", cell(row, "subcategory"))
                kinds.append("category")
            elif (_PET_STORE_RE.search(cell(row, "merchant")) and _PET_ITEM_RE.search(comment)
                  and category != "Питомцы"):
                changes["category"] = ("Питомцы", category)
                changes["subcategory"] = ("Корм и лакомства", cell(row, "subcategory"))
                kinds.append("category")
        if changes:
            fields.append({"id": tid, "row": row_number, "date": cell(row, "date")[:10],
                           "amount": parse_amount(cell(row, "amount")),
                           "label": cell(row, "merchant") or cell(row, "user_comment")[:40],
                           "kinds": kinds, "changes": changes})

    dates = []
    try:
        raw = raw_date_cells(ws)
    except Exception as e:                       # без неформатированных значений даты не трогаем
        print(f"[fix_data] не удалось прочитать даты: {e}")
        raw = []
    id_col = col.get("transaction_id", 0)
    for row_number, value in enumerate(raw, start=1):
        if row_number == 1 or row_number > len(values):
            continue
        if isinstance(value, str) and _TEXT_DATE_RE.match(value.strip()):
            tid = str(values[row_number - 1][id_col]).strip() if len(values[row_number - 1]) > id_col else ""
            if tid:
                dates.append({"id": tid, "row": row_number, "old": value.strip()})

    return {"fields": fields, "dates": dates, "duplicates": _find_duplicates(values, header)}


def _find_duplicates(values, header) -> list:
    """Автосписание, повторяющее уже записанную в этом месяце трату (сумма+магазин). Только показ."""
    col = {name: i for i, name in enumerate(header)}

    def cell(row, name):
        i = col.get(name)
        return str(row[i]).strip() if i is not None and i < len(row) else ""

    seen, found = {}, []
    for row_number, row in enumerate(values[1:], start=2):
        if cell(row, "type").upper() != "РАСХОД" or not cell(row, "transaction_id"):
            continue
        key = (cell(row, "date")[:7], cell(row, "user"), parse_amount(cell(row, "amount")),
               cell(row, "merchant").lower())
        auto = cell(row, "user_comment").lower().startswith("автосписание")
        seen.setdefault(key, []).append((row_number, row, auto))
    for key, items in seen.items():
        if len(items) > 1 and any(a for _, _, a in items) and key[3]:
            for row_number, row, auto in items:
                if auto:
                    found.append({"id": cell(row, "transaction_id"), "row": row_number,
                                  "date": cell(row, "date")[:16], "amount": key[2], "merchant": cell(row, "merchant")})
    return found


def _money(v: float) -> str:
    return f"{int(round(v)):,}".replace(",", " ")


def preview_text(found: dict) -> str:
    fields, dates, dups = found["fields"], found["dates"], found["duplicates"]
    if not (fields or dates or dups):
        return "В таблице всё чисто: менять нечего."
    lines = ["Нашла в таблице:"]
    by_kind = {}
    for f in fields:
        for k in f["kinds"]:
            by_kind.setdefault(k, []).append(f)
    for kind in ("funds", "source", "income", "category"):
        items = by_kind.get(kind, [])
        if items:
            lines.append(f"\n• {KIND_TITLES[kind]}: {len(items)}")
            for f in items[:MAX_PREVIEW_ROWS]:
                lines.append(f"   {f['date']} · {_money(f['amount'])} ₸ · {f['label'] or '—'}")
            if len(items) > MAX_PREVIEW_ROWS:
                lines.append(f"   …и ещё {len(items) - MAX_PREVIEW_ROWS}")
    if dates:
        lines.append(f"\n• {KIND_TITLES['date']}: {len(dates)}")
    if dups:
        lines.append("\n⚠️ Возможный дубль автосписания (сама не удаляю):")
        for d in dups:
            lines.append(f"   {d['date']} · {_money(d['amount'])} ₸ · {d['merchant']}\n"
                         f"   Удалить: /fix_data удалить {d['id']}")
    if fields or dates:
        lines.append("\nПока ничего не изменено. Применить: /fix_data да")
    return "\n".join(lines)


def apply_changes(found: dict) -> dict:
    """Пишет по transaction_id. Возвращает {"fields": N, "dates": M}."""
    from services import sheets

    @sheets._serialized
    def _apply():
        fields, dates = found["fields"], found["dates"]
        if not (fields or dates):
            return {"fields": 0, "dates": 0}
        ws, values = _ws_values()
        header = [str(h).strip() for h in values[0]]
        position = {str(r[0]).strip(): n for n, r in enumerate(values, start=1) if r}
        snapshot = {"at": datetime.datetime.now().isoformat(), "fields": [], "dates": []}
        raw_updates, done_fields = [], 0
        for f in fields:
            row = position.get(f["id"])
            if not row:
                continue
            old = {}
            for name, (new_value, old_value) in f["changes"].items():
                if name not in header:
                    continue
                raw_updates.append({"range": f"{_col_letter(header.index(name) + 1)}{row}", "values": [[new_value]]})
                old[name] = old_value
            if old:
                snapshot["fields"].append({"id": f["id"], "old": old})
                done_fields += 1
        date_updates, done_dates = [], 0
        date_col = _col_letter(header.index("date") + 1)
        for d in dates:
            row = position.get(d["id"])
            if not row:
                continue
            date_updates.append({"range": f"{date_col}{row}", "values": [[d["old"]]]})
            snapshot["dates"].append({"id": d["id"], "old": d["old"]})
            done_dates += 1
        state.put(NAMESPACE, "last", snapshot)
        if raw_updates:
            sheets._retry_write(ws.batch_update, raw_updates, value_input_option="RAW")
        if date_updates:
            sheets._retry_write(ws.format, f"{date_col}2:{date_col}",
                                {"numberFormat": {"type": "DATE_TIME", "pattern": DATE_PATTERN}})
            sheets._retry_write(ws.batch_update, date_updates, value_input_option="USER_ENTERED")
        return {"fields": done_fields, "dates": done_dates}

    return _apply()


def undo_last() -> str:
    from services import sheets

    @sheets._serialized
    def _undo():
        snap = state.get(NAMESPACE, "last")
        if not snap or not (snap.get("fields") or snap.get("dates")):
            return "Нечего откатывать: сохранённого состояния нет."
        ws, values = _ws_values()
        header = [str(h).strip() for h in values[0]]
        position = {str(r[0]).strip(): n for n, r in enumerate(values, start=1) if r}
        updates, restored = [], 0
        for f in snap.get("fields", []):
            row = position.get(f["id"])
            if not row:
                continue
            for name, old in f["old"].items():
                if name in header:
                    updates.append({"range": f"{_col_letter(header.index(name) + 1)}{row}", "values": [[old]]})
            restored += 1
        date_col = _col_letter(header.index("date") + 1)
        for d in snap.get("dates", []):
            row = position.get(d["id"])
            if row:
                updates.append({"range": f"{date_col}{row}", "values": [[d["old"]]]})
        if updates:
            sheets._retry_write(ws.batch_update, updates, value_input_option="RAW")
        state.delete(NAMESPACE, "last")
        return (f"Вернула прежние значения у {restored} записей"
                f"{' и тексты дат' if snap.get('dates') else ''}.")

    return _undo()


def delete_one(transaction_id: str) -> str:
    """Удаляет ровно одну запись по точному ID (команда /fix_data удалить <ID>).
    Только точное совпадение: записи <ID>_1, <ID>_2 (после разбивки) не затрагиваются."""
    from services import sheets

    tid = str(transaction_id or "").strip()
    if not tid:
        return "Укажи ID: /fix_data удалить <ID>"

    @sheets._serialized
    def _delete():
        ws, values = _ws_values()
        header = [str(h).strip() for h in values[0]]
        matches = [(n, r) for n, r in enumerate(values, start=1) if n > 1 and r and str(r[0]).strip() == tid]
        if len(matches) != 1:
            return f"Не нашла ровно одну запись с ID {tid} — ничего не удалила."
        row_number, row = matches[0]
        record = dict(zip(header, list(row) + [""] * (len(header) - len(row))))
        sheets._retry_write(ws.delete_rows, row_number)
        return (f"Удалила: {str(record.get('date', ''))[:16]} · {record.get('amount')} ₸ · "
                f"{record.get('merchant') or record.get('user_comment')} (ID {tid}).")

    return _delete()

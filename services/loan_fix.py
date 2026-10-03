"""Разовая (и повторяемая) правка: погашения кредитов и рассрочек — в «Кредиты и рассрочки».

/fix_loans          — показать, что изменится (ничего не пишет)
/fix_loans да       — применить (перед этим — резервная копия, если она настроена)
/fix_loans откат    — вернуть прежние категории

Строки находятся по transaction_id, а не по номеру строки. Прежние значения сохраняются
в state.py и в комментарии ai_comment записи.
"""
import datetime

from services import state
from services.timezone import ASTANA_TZ
from services.loan_rules import LOAN_CATEGORY, LOAN_SUBCATEGORY, looks_like_repayment
from services.money import parse_amount

NAMESPACE = "loan_fix"
# Номера колонок Transactions (1-based): см. TRANSACTION_HEADERS
COL_ID, COL_CATEGORY, COL_SUBCATEGORY, COL_AI_COMMENT = 1, 11, 12, 16
MAX_PREVIEW_ROWS = 15


def _rows():
    from services import sheets

    ws = sheets.get_db().worksheet("Transactions")
    return ws, ws.get_all_values()


def find_changes() -> list[dict]:
    """Что нужно исправить: погашения не в «Кредиты и рассрочки»."""
    _, values = _rows()
    if not values:
        return []
    header = [str(h).strip() for h in values[0]]
    idx = {name: header.index(name) for name in
           ("transaction_id", "date", "type", "amount", "category", "subcategory", "merchant",
            "user_comment", "funds_type", "ai_comment") if name in header}
    changes = []
    for row_number, row in enumerate(values[1:], start=2):
        def cell(name):
            i = idx.get(name)
            return str(row[i]).strip() if i is not None and i < len(row) else ""
        if not cell("transaction_id") or cell("type").upper() != "РАСХОД":
            continue
        if not looks_like_repayment(cell("merchant"), cell("user_comment"), cell("subcategory"), cell("funds_type")):
            continue
        if cell("category") == LOAN_CATEGORY and cell("subcategory") == LOAN_SUBCATEGORY:
            continue
        changes.append({
            "id": cell("transaction_id"), "row": row_number, "date": cell("date")[:10],
            "amount": parse_amount(cell("amount")), "merchant": cell("merchant") or cell("user_comment")[:40],
            "old_category": cell("category"), "old_subcategory": cell("subcategory"),
            "old_ai_comment": cell("ai_comment"),
        })
    return changes


def _money(v: float) -> str:
    return f"{int(round(v)):,}".replace(",", " ")


def preview_text(changes: list[dict]) -> str:
    if not changes:
        return "Все погашения кредитов и рассрочек уже в «Финансовые расходы и переводы › Кредиты и рассрочки». Менять нечего."
    total = sum(c["amount"] for c in changes)
    lines = [f"Нашла {len(changes)} платежа(ей) по кредитам и рассрочкам на {_money(total)} ₸ не в той категории:"]
    for c in changes[:MAX_PREVIEW_ROWS]:
        lines.append(f"• {c['date']} · {_money(c['amount'])} ₸ · {c['merchant']}\n"
                     f"   {c['old_category']} › {c['old_subcategory'] or '—'}  →  {LOAN_CATEGORY} › {LOAN_SUBCATEGORY}")
    if len(changes) > MAX_PREVIEW_ROWS:
        lines.append(f"…и ещё {len(changes) - MAX_PREVIEW_ROWS}.")
    lines.append("\nПока ничего не изменено. Чтобы применить: /fix_loans да")
    return "\n".join(lines)


def apply_changes(changes: list[dict]) -> int:
    """Пишет новые значения по transaction_id. Возвращает число исправленных строк."""
    from services import sheets

    if not changes:
        return 0
    ws, values = _rows()
    position = {str(r[0]).strip(): n for n, r in enumerate(values, start=1) if r}   # id -> номер строки на момент записи
    state.put(NAMESPACE, "last", {
        "at": datetime.datetime.now(ASTANA_TZ).isoformat(),
        "changes": [{k: c[k] for k in ("id", "old_category", "old_subcategory", "old_ai_comment")} for c in changes],
    })
    done = 0
    for c in changes:
        row = position.get(c["id"])
        if not row:
            continue
        note = f"(перекатегорировано: было {c['old_category']} › {c['old_subcategory'] or '—'})"
        sheets._retry_write(ws.update_cell, row, COL_CATEGORY, LOAN_CATEGORY)
        sheets._retry_write(ws.update_cell, row, COL_SUBCATEGORY, LOAN_SUBCATEGORY)
        sheets._retry_write(ws.update_cell, row, COL_AI_COMMENT, f"{c['old_ai_comment']} {note}".strip())
        done += 1
    return done


def undo_last() -> str:
    from services import sheets

    snap = state.get(NAMESPACE, "last")
    if not snap or not snap.get("changes"):
        return "Нечего откатывать: сохранённого состояния нет."
    ws, values = _rows()
    position = {str(r[0]).strip(): n for n, r in enumerate(values, start=1) if r}
    restored = 0
    for c in snap["changes"]:
        row = position.get(c["id"])
        if not row:
            continue
        sheets._retry_write(ws.update_cell, row, COL_CATEGORY, c["old_category"])
        sheets._retry_write(ws.update_cell, row, COL_SUBCATEGORY, c["old_subcategory"])
        sheets._retry_write(ws.update_cell, row, COL_AI_COMMENT, c["old_ai_comment"])
        restored += 1
    state.delete(NAMESPACE, "last")
    return f"Вернула прежние категории у {restored} записей."

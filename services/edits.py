"""Preview newest matching family records; modify only confirmed stable IDs."""
import asyncio
import re
from services import state, sheets
from services.telegram_safe import safe_answer

SHEETS = {"Transactions":"transaction_id","ShoppingList":"item_id","Trips":"trip_id",
          "Installments":"id","Subscriptions":"id","Limits":"category"}

def candidates(title, query):
    if title not in SHEETS:
        raise ValueError("Этот раздел редактируется отдельной командой.")
    rows = sheets._get_all_records_safe(sheets._worksheet(title))
    rows = [r for r in rows if r.get(SHEETS[title])]
    exact = [r for r in rows if str(r[SHEETS[title]]).lower() == str(query).lower().strip()]
    if exact: return exact
    terms = re.findall(r"\w+", str(query or "").lower().replace("ё","е"))
    latest = {"последняя","последнюю","последний","последнее","последней","last"}
    ignored = latest | {"удали","удалить","запись","операцию","трату","покупку","измени","поменяй"}
    words = [w for w in terms if w not in ignored]
    if not words and not set(terms).intersection(latest):
        return []
    found = [r for r in reversed(rows) if all(w in " ".join(str(v) for v in r.values()).lower().replace("ё","е") for w in words)]
    return found

async def handle_confirmation(message, text):
    key = state.dialogue_key(message.chat.id,message.from_user.id)
    plan = state.get("edit_plan",key)
    if not plan or text.strip().lower() not in {"да","подтверждаю","нет","отмена"}:
        return False
    if text.strip().lower() in {"нет","отмена"}:
        state.delete("edit_plan",key)
        await safe_answer(message,"Правку отменена." .replace("Правку","Правка"))
        return True
    count=0
    for i,op in enumerate(plan["operations"]):
        if i < plan.get("completed",0): continue
        if op["action"]=="delete":
            result=await asyncio.to_thread(sheets.delete_record_by_keyword,op["worksheet"],op["id"])
        else:
            result=await asyncio.to_thread(sheets.find_and_update_record,op["worksheet"],op["id"],op["field"],op["value"])
        if not result:
            await safe_answer(message,"Подтверждённая запись уже изменилась или удалена. Проверь таблицу, затем повтори запрос.")
            state.delete("edit_plan",key)
            return True
        count+=1
        plan["completed"]=i+1
        state.put("edit_plan",key,plan)
    state.delete("edit_plan",key)
    await safe_answer(message,f"✅ Выполнено изменений: {plan['completed']}.")
    return True

async def propose(message, parsed):
    operations = parsed.get("updates") or []
    if not operations and parsed.get("intent")=="delete_transaction":
        operations=[{"worksheet":"Transactions","search_query":parsed.get("search_query",""),"action":"delete"}]
    if not isinstance(operations,list) or len(operations)>20:
        raise ValueError("Слишком много изменений за один запрос")
    planned, lines = [], []
    for op in operations:
        title = op.get("worksheet","Transactions")
        if title not in SHEETS:
            raise ValueError("Напоминания и долги изменяются отдельными командами.")
        rows=await asyncio.to_thread(candidates,title,op.get("search_query",""))
        if not rows:
            await safe_answer(message,f"Не нашла запись в {title}. Уточни ID или ключевые слова.")
            return
        row=rows[0] # Original newest-first behavior, now visible before mutation.
        action=op.get("action","update")
        if action not in {"update","delete"}: raise ValueError("Неподдерживаемое действие")
        planned.append({"worksheet":title,"id":row[SHEETS[title]],"action":action,
                        "field":op.get("column_to_update",""),"value":op.get("new_value","")})
        description = row.get("user_comment") or row.get("item") or row.get("description") or row.get("name") or row.get("destination") or row.get("category")
        lines.append(f"{title} · {row[SHEETS[title]]}\n{description} · {row.get('amount','')}\n"
                     + ("Удалить" if action=="delete" else f"{op.get('column_to_update')}: {row.get(op.get('column_to_update'),'')} → {op.get('new_value')}"))
    if not planned:
        await safe_answer(message,"Уточни запись и что изменить."); return
    key=state.dialogue_key(message.chat.id,message.from_user.id)
    for ns in ("reminder_plan","reminder_delete","debt_draft"):
        state.delete(ns,key)
    state.put("edit_plan",key,{"operations":planned,"completed":0})
    await safe_answer(message,"\n\n".join(lines)+"\n\nПодтвердить? «Да» / «Нет».",parse_mode=None)

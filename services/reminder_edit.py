"""Edit existing reminders by stable ID; keep date when only time is changed."""
import asyncio
import copy
import datetime as dt
import re
import time
from services import state
from services.timezone import now_astana, ASTANA_TZ, parse_flexible_datetime
from services.ingest_models import InputProblem

NS="hf_reminder_edit"
YES={"да","подтверждаю"}
NO={"нет","отмена"}

def norm(text): return str(text or "").lower().replace("ё","е")
def is_edit(text):
    t=norm(text)
    return bool(re.search(r"измени|изменить|поменяй|перенеси|перенести|исправь|сдвинь|переставь",t)
                and re.search(r"напомин|напомни|rem_",t))
def has_clock(text):
    t=norm(text)
    return bool(re.search(r"(?<![\d.])\d{1,2}[:.]\d{2}(?![\d.])|\b(?:в|во|на)\s+(?:\d{1,2}|час|два|три|четыре|пять|шесть|семь|восемь|девять|десять|одиннадцать|двенадцать)\b|\bчерез\s+(?:\d+|час|полчаса|два|две)\b",t))
def has_day(text):
    return bool(re.search(r"сегодня|завтра|послезавтра|\d{1,2}\.\d{1,2}\.\d{4}|\d{4}-\d{2}-\d{2}|понедель|вторник|сред|четверг|пятниц|суббот|воскрес",norm(text)))

def change_time(text, old, now=None):
    now=now or now_astana()
    original=parse_flexible_datetime(old)
    if not original: raise InputProblem("У исходного напоминания повреждена дата.")
    if not original.tzinfo: original=original.replace(tzinfo=ASTANA_TZ)
    t=norm(text);date=original.astimezone(ASTANA_TZ).date()
    if "послезавтра" in t: date=now.date()+dt.timedelta(days=2)
    elif "завтра" in t: date=now.date()+dt.timedelta(days=1)
    elif "сегодня" in t: date=now.date()
    else:
        m=re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b",t)
        iso=re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b",t)
        try:
            if m: date=dt.date(int(m[3]),int(m[2]),int(m[1]));t=t.replace(m[0],"")
            elif iso: date=dt.date(*map(int,iso.groups()));t=t.replace(iso[0],"")
            else:
                weekdays={"понедель":0,"вторник":1,"сред":2,"четверг":3,"пятниц":4,"суббот":5,"воскрес":6}
                for name,number in weekdays.items():
                    if name in t:
                        date=now.date()+dt.timedelta(days=(number-now.weekday())%7);break
        except ValueError: raise InputProblem("Такой календарной даты нет.")
    matches=list(re.finditer(r"(?<![\d.])(\d{1,2})[:.](\d{2})(?![\d.])",t))
    if len(matches)>1: raise InputProblem("Для одного изменения укажи одно время.")
    if matches: hour,minute=map(int,matches[0].groups())
    else:
        match=re.search(r"\b(?:в|во|на)\s+(\d{1,2})(?!\d)",t)
        if not match: raise InputProblem("Во сколько перенести? Например «завтра в 11:00».")
        hour,minute=int(match[1]),0
    if "вечер" in t and 1<=hour<=11: hour+=12
    if not 0<=hour<=23 or not 0<=minute<=59: raise InputProblem("Укажи часы 00–23 и минуты 00–59.")
    candidate=dt.datetime.combine(date,dt.time(hour,minute),ASTANA_TZ)
    if candidate<=now: raise InputProblem("Это время уже прошло. Укажи новую будущую дату.")
    return candidate.strftime("%Y-%m-%d %H:%M:%S")

def select(items,text):
    t=norm(text)
    ids=re.findall(r"\brem_[\w-]+\b",t)
    if ids: return [r for r in items if str(r["reminder_id"]).lower() in ids]
    # Prefer the task phrase after 'про/о', not incidental profanity/politeness before the command.
    match=re.search(r"(?:напоминани\w*|напоминал\w*)\s*(?:про|о|об)?\s*(.*)",t)
    query=match[1] if match else t
    query=re.sub(r"\b(?:на|в|во)\s+\d{1,2}(?:[:.]\d{2})?.*$","",query)
    query=re.sub(r"\b(?:время|дату|дата|напоминани\w*|про|сегодня|завтра|послезавтра|пожалуйста|мне|все|это|то|для|на|в)\b"," ",query)
    words=re.findall(r"[а-яa-z]{3,}",query)
    words=[w for w in words if w not in {"диане","дианы","владу","влада"}]
    target="Диана" if re.search(r"диан",t) else "Влад" if re.search(r"влад",t) else None
    candidates=[r for r in items if all(w[:5] in norm(r.get("text")) for w in words)]
    if target:
        candidates=[r for r in candidates if target in str(r.get("target_user")) or str(r.get("target_user")) in {"Семья","Владислав и Диана"}]
    return candidates if words or target else items

def show(items):
    return "\n\n".join(f"{r['reminder_id']}\n{r.get('target_user')} · {r.get('remind_at')}\n{r.get('text')}" for r in items)

def save_draft(key,data):
    data["expires_at"]=time.time()+1800
    state.put(NS,key,data)
    state.put("hf_focus",key,{"kind":"reminder_edit"})

async def propose(message,items,changes):
    from services.telegram_safe import safe_answer
    key=state.dialogue_key(message.chat.id,message.from_user.id)
    before=[{k:r.get(k,"") for k in ("reminder_id","text","remind_at","target_user","recurrence","status")} for r in items]
    save_draft(key,{"before":before,"changes":changes,"stage":"confirm"})
    await safe_answer(message,"Изменить существующее напоминание:\n\n"+show(items)+"\n\n"
        +"\n".join(f"Новое {k}: {v}" for k,v in changes.items() if k not in {"deliveries","anchor_day"})
        +"\n\nID сохранится. Новая строка не создаётся. Подтвердить? «Да» / «Нет».",parse_mode=None)

async def handle(message,text):
    from services import reminders as r
    from services.telegram_safe import safe_answer
    key=state.dialogue_key(message.chat.id,message.from_user.id)
    low=norm(text).strip();draft=state.get(NS,key)
    if draft and draft.get("expires_at",0)<time.time():
        state.delete(NS,key);draft=None
    focused=(state.get("hf_focus",key) or {}).get("kind")=="reminder_edit"
    if draft and focused and low in NO:
        state.delete(NS,key);state.delete("hf_focus",key)
        await safe_answer(message,"Изменение отменено. Исходное напоминание сохранено.");return True
    if draft and focused and low in YES and draft.get("stage")=="confirm":
        candidate=parse_flexible_datetime(draft["changes"].get("remind_at"))
        if not candidate or candidate<=now_astana():
            await safe_answer(message,"Новое время уже прошло. Повтори перенос с будущей датой.");return True
        async with r.dispatch_lock:
            current={row["reminder_id"]:row for row in await asyncio.to_thread(r.pending)}
            for old in draft["before"]:
                row=current.get(old["reminder_id"])
                if not row:
                    await safe_answer(message,"Напоминание уже отправлено/отменено. Обнови список /reminders.");return True
                # Retry after a lost ack: desired fields may already be committed.
                changes=draft["changes"]
                already=all(str(row.get(k,""))==str(v) for k,v in changes.items() if k not in {"deliveries"})
                if not already and any(str(row.get(k,""))!=str(old.get(k,"")) for k in ("text","remind_at","target_user","recurrence")):
                    await safe_answer(message,"Напоминание изменилось после предпросмотра. Повтори запрос, чтобы не затереть правку.");return True
                if not already:
                    if not await asyncio.to_thread(r.update_by_id,old["reminder_id"],changes):
                        raise RuntimeError("Не найден ID при изменении")
            result=[current[o["reminder_id"]] | draft["changes"] for o in draft["before"]]
        state.delete(NS,key);state.delete("hf_focus",key)
        for ns in ("reminder_draft","reminder_plan"): state.delete(ns,key)
        await safe_answer(message,"✅ Изменила существующую запись, без нового напоминания.\n\n"+show(result),parse_mode=None)
        return True
    edit=is_edit(text)
    follow=draft and focused and (has_clock(text) or re.search(r"\brem_",low))
    if not edit and not follow: return False
    items=await asyncio.to_thread(r.pending)
    if edit:
        matches=select(items,text)
    else:
        ids={x["reminder_id"] for x in draft.get("before",[])}
        matches=[row for row in items if row["reminder_id"] in ids]
        if re.search(r"\brem_",low): matches=select(matches,text)
    if not matches:
        await safe_answer(message,"Не нашла активное напоминание для правки. Укажи ID из /reminders.");return True
    if len(matches)>1 and not re.search(r"\bвсе\b",low):
        save_draft(key,{"before":matches,"stage":"select","requested":text if edit else draft.get("requested","")})
        await safe_answer(message,"Нашла несколько записей. Укажи один ID (дубли не удаляю молча):\n\n"+show(matches),parse_mode=None)
        return True
    if len(matches)>1 and not has_day(text):
        dates={str(row.get("remind_at",""))[:10] for row in matches}
        if len(dates)>1:
            await safe_answer(message,"У этих напоминаний разные даты. Укажи одну новую дату явно или меняй каждое по ID.")
            return True
    time_text=text
    if follow and re.search(r"\brem_",low) and not has_clock(low):
        time_text=draft.get("requested","")
    try:
        newtime=change_time(time_text,matches[0]["remind_at"])
    except InputProblem as error:
        save_draft(key,{"before":matches,"stage":"time","requested":text})
        await safe_answer(message,str(error),parse_mode=None);return True
    changes={"remind_at":newtime,"deliveries":"{}"}
    if has_day(time_text): changes["anchor_day"]=parse_flexible_datetime(newtime).day
    await propose(message,matches,changes)
    return True

async def from_model(message,parsed,author,source):
    """Catches model 'add_reminder' while editing and legacy common-editor output."""
    from services import reminders as r
    from services.telegram_safe import safe_answer
    key=state.dialogue_key(message.chat.id,message.from_user.id)
    draft=state.get(NS,key)
    intent=parsed.get("intent")
    operations=[op for op in (parsed.get("updates") or []) if op.get("worksheet")=="Reminders"]
    editing=intent=="update_reminder" or (operations and any(op.get("action","update")=="update" for op in operations)) or (
        intent=="add_reminder" and (is_edit(source) or
        (draft and (state.get("hf_focus",key) or {}).get("kind")=="reminder_edit"
         and not re.search(r"\bнапомни(?:ть)?\b|(?:создай|добавь|поставь)\s+напомин",norm(source)))))
    if not editing: return False
    if await handle(message,source): return True
    items=await asyncio.to_thread(r.pending)
    ids=parsed.get("reminder_ids") or [x.get("reminder_id") for x in (draft or {}).get("before",[])]
    if isinstance(ids,str): ids=[ids]
    selected=[row for row in items if row["reminder_id"] in ids]
    if not selected and operations: selected=select(items,operations[0].get("search_query",""))
    if len(selected)!=1:
        await safe_answer(message,"Для изменения укажи конкретный ID из /reminders. Новое напоминание не создавала.");return True
    if not has_clock(source):
        save_draft(key,{"before":selected,"stage":"time"})
        await safe_answer(message,"Во сколько перенести? Дата останется прежней, если не укажешь другую.");return True
    try: value=change_time(source,selected[0]["remind_at"])
    except InputProblem as error:
        await safe_answer(message,str(error));return True
    await propose(message,selected,{"remind_at":value,"deliveries":"{}"})
    return True

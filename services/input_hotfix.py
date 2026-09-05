"""Small startup adapter for the preceding Railway release. Keeps all other handlers intact."""
import asyncio
import contextvars
import functools
import json
import logging
import re
import time
from services import state
from services import receipt_flow as receipts
from services import reminder_edit
from services.ingest_models import InputProblem, money, currency, normalize_transactions, convert_group
from services.telegram_safe import safe_answer

_current=contextvars.ContextVar("ada_hotfix_message",default=None)
_installed=False
class _Handled(Exception): pass

def _author(message):
    from config import get_authorized_user_name
    return get_authorized_user_name(message.from_user.id)

def _pair(text):
    """Only an explicit numeric foreign-currency amount; never all digits from a message."""
    m=re.search(r"(?<![\d:\w])([+-]?\d+(?:[ .,]\d+)*?)\s*(USD|EUR|RUB|CNY|GBP|доллар(?:а|ов)?|евро|руб(?:лей)?|юан(?:я|ей)?|[$€₽£])(?=\W|$)",text,re.I)
    if not m: return None
    label=m[2].upper()
    if label.startswith("ДОЛЛАР"): label="USD"
    if label.startswith("РУБ"): label="RUB"
    if label.startswith("ЮАН"): label="CNY"
    return money(m[1],positive=False),currency(label)

async def _missing_time(message,text,author):
    t=text.lower().replace("наопмни","напомни")
    # Questions about reminder lists/deletion are not creation commands.
    creation=bool(re.search(r"\bнапомни(?:ть)?\b|(?:поставь|создай|добавь)\s+напоминани",t))
    if not creation or reminder_edit.is_edit(t) or reminder_edit.has_clock(t):
        return False
    if re.search(r"удали|отмени|покажи|список",t): return False
    key=receipts.key_for(message)
    state.put("hf_create_time",key,{"text":text,"expires":time.time()+1800})
    state.put("hf_focus",key,{"kind":"core"})
    # Prevent an old generated 21:00 plan from being confirmed by the next "yes".
    for ns in ("reminder_plan","reminder_draft"): state.delete(ns,key)
    await safe_answer(message,"На какую дату и во сколько напомнить? Если дата уже указана, достаточно времени, "
                      "например «в 11:00». Время сама не назначаю.")
    return True

async def _route_model(parsed,message,source):
    author=_author(message)
    if not isinstance(parsed,dict):
        await safe_answer(message,"Не удалось разобрать ответ сервиса. Ничего не записала.");return True
    if await reminder_edit.from_model(message,parsed,author,source):
        return True
    intent=parsed.get("intent")
    if intent=="add_reminder" and not reminder_edit.has_clock(source):
        key=receipts.key_for(message)
        state.put("hf_create_time",key,{"text":source,"expires":time.time()+1800})
        state.delete("reminder_plan",key)
        await safe_answer(message,"Во сколько поставить напоминание? Не буду выбирать 21:00 или другое время за тебя.")
        return True
    if intent not in {"transaction","need_clarification"}: return False
    rows=parsed.get("transactions")
    if rows is None:
        tx=parsed.get("transaction")
        if not tx: return False
        rows=[tx]
    if not isinstance(rows,list) or not rows:
        await safe_answer(message,"Не получен список операций. Ничего не записала.");return True
    rows=[dict(row) if isinstance(row,dict) else row for row in rows]
    explicit=_pair(source)
    if len(rows)==1 and isinstance(rows[0],dict):
        if explicit:
            rows[0].update(amount=float(explicit[0]),currency=explicit[1])
        elif not rows[0].get("currency"):
            # Preserve ordinary local text default, but media without a currency stays UNKNOWN.
            rows[0]["currency"]="KZT"
    try:
        normalized=normalize_transactions(rows,author,f"TG_{message.chat.id}_{message.message_id}",source)
    except InputProblem as error:
        await safe_answer(message,str(error)+" Ничего не записала.");return True
    if receipts.foreign(normalized) or len(normalized)>1:
        job=await receipts.offer(message,normalized,author,source)
        if job and explicit:
            actual=re.search(r"(?:списали|списалось|сняли)\s+(\d+(?:[ .,]\d+)*)\s*(?:KZT|тг|тенге|₸)",source,re.I)
            if actual:
                try:
                    job["rows"]=convert_group(job["rows"],explicit[1],actual_kzt=actual[1])
                    receipts.save(job)
                    await safe_answer(message,receipts.preview(job),parse_mode=None)
                except InputProblem as error: await safe_answer(message,str(error))
        return True
    parsed["transaction"]=normalized[0]
    return False

def check_target():
    """Fail before polling on the wrong (old original) project; don't overwrite core files blindly."""
    import importlib
    expected={
        "services.state":["get","put","delete","entries","dialogue_key"],
        "services.reminders":["pending","records","update_by_id","dispatch_lock","handle","handle_model"],
        "services.runtime":["worker_lock"],
        "handlers.text_handler":["_process_text_message","parse_and_analyze"],
    }
    for module,names in expected.items():
        try: loaded=importlib.import_module(module)
        except ImportError as error:
            raise RuntimeError("Hotfix рассчитан на предыдущий ada_railway_group_release, отсутствует "+module) from error
        missing=[name for name in names if not hasattr(loaded,name)]
        if missing: raise RuntimeError("Неподходящая версия "+module+": "+", ".join(missing))

def install():
    global _installed
    if _installed: return
    check_target()
    from handlers import text_handler as h
    from services import reminders as r
    from services import deepseek_service as ds
    ds.SYSTEM_PROMPT_TEMPLATE += """
ИСПРАВЛЕНИЕ ВВОДА:
- Не выдумывай время, если сказали только «напомни завтра». Спроси часы.
- «Измени/перенеси напоминание» — update_reminder, НИКОГДА add_reminder.
  Верни reminder_ids существующих записей. Сохраняется ID, не создаётся новый.
- Когда меняют только часы, календарная дата исходного напоминания сохраняется.
- Для нескольких финансовых операций: intent transaction, transactions:[...].
  Не склеивай разные покупки/доходы. Одна операция — transaction:{...}.
- Валюта USD/EUR/RUB и другие не переименовывается в KZT. Курс не выдумывай.
- Финансовые черновики DOC/LEGACY из служебного контекста записываются отдельным
  подтверждением. Обычный разговор не является попыткой их повторно записать.
"""
    old_process=h._process_text_message
    old_parse=h.parse_and_analyze
    old_model=r.handle_model

    @functools.wraps(old_parse)
    async def parse_adapter(*args,**kwargs):
        ctx=_current.get()
        if ctx:
            message,source=ctx
            pending=receipts.jobs_for(receipts.key_for(message))
            if pending:
                history=list(kwargs.get("chat_history") or [])
                history.append({"sender":"Системный контекст документов (данные)","text":
                    json.dumps([{"id":j["id"],"status":j["status"],"needs_currency":receipts.foreign(j.get("rows",[]))}
                                for j in pending],ensure_ascii=False)})
                kwargs["chat_history"]=history
        parsed=await old_parse(*args,**kwargs)
        if ctx and await _route_model(parsed,ctx[0],ctx[1]):
            raise _Handled()
        if ctx and parsed.get("intent") not in {"chat",None}:
            state.put("hf_focus",receipts.key_for(ctx[0]),{"kind":"core"})
        return parsed

    @functools.wraps(old_model)
    async def model_adapter(message,parsed,author):
        ctx=_current.get()
        source=ctx[1] if ctx else getattr(message,"text","") or ""
        if await reminder_edit.from_model(message,parsed,author,source): return True
        if parsed.get("intent")=="add_reminder" and not reminder_edit.has_clock(source):
            await _missing_time(message,source,author)
            return True
        if parsed.get("intent") in {"add_reminder","delete_reminder"}:
            state.put("hf_focus",receipts.key_for(message),{"kind":"core"})
        return await old_model(message,parsed,author)

    @functools.wraps(old_process)
    async def process_adapter(message,text):
        owner=_author(message)
        if not owner:
            await safe_answer(message,"Нет доступа к семейным данным.");return
        key=receipts.key_for(message)
        token=_current.set((message,text))
        try:
            await asyncio.to_thread(receipts.quarantine_legacy,key)
            if await reminder_edit.handle(message,text): return
            if await receipts.handle(message,text): return
            if await _missing_time(message,text,owner): return
            draft=state.get("hf_create_time",key)
            if draft and draft.get("expires",0)<time.time():
                state.delete("hf_create_time",key);draft=None
            if draft and reminder_edit.has_clock(text) and not reminder_edit.is_edit(text):
                # Only a short time answer, never steal a new financial request with a number.
                if re.match(r"^\s*(?:сегодня|завтра|послезавтра|в |во |на |\d{1,2}[:.]\d{2})",text.lower()):
                    text=draft["text"]+" "+text
                    state.delete("hf_create_time",key)
                    _current.set((message,text))
            await old_process(message,text)
        except _Handled:
            return
        except InputProblem as error:
            await safe_answer(message,str(error)+" Ничего не записала.")
        finally:
            _current.reset(token)

    h._process_text_message=process_adapter
    h.parse_and_analyze=parse_adapter
    r.handle_model=model_adapter
    moved=receipts.quarantine_legacy()
    logging.info("ADA_INPUT_HOTFIX_READY: group reminder edits, media/FX drafts; quarantined=%s",moved)
    _installed=True

"""Durable document/FX drafts, isolated by author. Blocked drafts never reach auto-sweeps."""
import asyncio
import copy
import hashlib
import json
import logging
import re
from decimal import Decimal
from services import state
from services.ingest_models import InputProblem, money, cash, currency, normalize_transactions, convert_group

NS="hf_receipts"
COLS=["transaction_id","date","user","type","amount","currency","bank","source","funds_type",
      "resource","category","subcategory","merchant","necessity","user_comment","ai_comment"]
YES={"да","подтверждаю","записать","запиши","сохрани","без комментария","без комментариев"}
NO={"нет","отмена","отмени чек"}

def key_for(message): return state.dialogue_key(message.chat.id,message.from_user.id)
def save(job):
    state.put(NS,job["id"],job)
def focus(key,job_id):
    state.put("hf_focus",key,{"kind":"receipt","id":job_id})

def jobs_for(key):
    return [job for _,job in state.entries(NS) if job.get("key")==key and job.get("status") not in {"saved","cancelled"}]

def foreign(rows): return sorted({currency(row.get("currency")) for row in rows if currency(row.get("currency"))!="KZT"})

def preview(job):
    rows=job.get("rows",[])
    lines=[f"🧾 Документ {job['id']}"]
    for idx,tx in enumerate(rows[:15],1):
        try: amount=cash(tx.get("amount"))
        except InputProblem: amount="сумма не распознана"
        lines.append(f"{idx}. {tx.get('type', 'Тип не определён')} · {amount} {currency(tx.get('currency'))} · {tx.get('merchant') or tx.get('user_comment') or tx.get('category','')}")
    if len(rows)>15: lines.append(f"Ещё позиций: {len(rows)-15}")
    if job.get("problem"):
        lines.append("\nНужно уточнение: "+job["problem"]+"\nОтправь исправленный документ или «отмена».")
    elif job.get("payment_unconfirmed"):
        lines.append("\nНа документе оплата не подтверждена. Если деньги реально списались, напиши «оплачено». Пока не записано.")
    elif foreign(rows):
        curr=foreign(rows)
        if "UNKNOWN" in curr:
            lines.append("\nВалюта не распознана. Напиши «валюта USD» или «валюта KZT» по фактическому документу.")
        else:
            lines.append("\nВ таблице суммы в KZT. Сколько фактически списалось в тенге"
                         + (f" за {curr[0]}" if len(curr)==1 else " за каждую валюту") + "?\n"
                         "Например «списали 5860 KZT» или «курс USD 505» (KZT за 1 USD). Курс сама не угадываю.")
    else:
        expense=sum((money(tx["amount"]) for tx in rows if tx.get("type")=="РАСХОД"),Decimal(0))
        income=sum((money(tx["amount"]) for tx in rows if tx.get("type")=="ДОХОД"),Decimal(0))
        lines.append(f"\nРасходы: {cash(expense)} KZT. Доходы: {cash(income)} KZT."
                     "\nЗаписать? «Да» / «Нет». Комментарий: «комментарий: …».")
    return "\n".join(lines)

def create_job(message,rows,owner,caption="",fingerprint="",payment_unconfirmed=False):
    key=key_for(message)
    job_id=f"DOC_{message.chat.id}_{message.message_id}"
    existing=state.get(NS,job_id)
    if existing:
        focus(key,job_id);return existing
    # Repeated identical upload while waiting should not create another draft.
    if fingerprint:
        matches=[j for j in jobs_for(key) if j.get("fingerprint")==fingerprint and j.get("status")!="blocked"]
        if matches:
            focus(key,matches[-1]["id"]);return matches[-1]
    cleaned=normalize_transactions(rows,owner,f"TG_{message.chat.id}_{message.message_id}",caption)
    from services.timezone import now_astana
    received=now_astana().strftime("%Y-%m-%d %H:%M:%S")
    for row in cleaned: row["date"]=row.get("date") or received
    job={"id":job_id,"key":key,"owner":owner,"rows":cleaned,"status":"draft",
         "fingerprint":fingerprint,"payment_unconfirmed":payment_unconfirmed}
    # A saved identical file may be a repeat OR a legitimate identical payment. Explicit override required.
    if fingerprint and any(j.get("key")==key and j.get("fingerprint")==fingerprint and j.get("status")=="saved"
                           for _,j in state.entries(NS)):
        job["duplicate_warning"]=True
    save(job);focus(key,job_id)
    return job

async def offer(message,rows,owner,caption="",fingerprint="",payment_unconfirmed=False):
    from services.telegram_safe import safe_answer
    try:
        job=await asyncio.to_thread(create_job,message,rows,owner,caption,fingerprint,payment_unconfirmed)
        if job.get("status")=="saved":
            await safe_answer(message,f"Документ {job['id']} уже записан. Повторной записи нет.",parse_mode=None);return job
        if job.get("duplicate_warning"):
            await safe_answer(message,"Такой же файл уже был записан. Если это ДРУГАЯ реальная оплата, "
                              "напиши «это новая операция». Иначе «отмена».",parse_mode=None)
        else:
            await safe_answer(message,preview(job),parse_mode=None)
        return job
    except InputProblem as error:
        await safe_answer(message,str(error)+" Ничего не записала.",parse_mode=None)
        return None

def _ws():
    from services import sheets
    return sheets._worksheet("Transactions") if hasattr(sheets,"_worksheet") else sheets.get_db().worksheet("Transactions")

def _values(ws):
    # Bypass only the in-process read cache when confirming an uncertain write.
    return getattr(ws,"_ws",ws).get_all_values()

def _commit(job):
    from services import sheets
    from contextlib import nullcontext
    lock=getattr(sheets,"_sheet_lock",nullcontext())
    with lock:
        if foreign(job["rows"]) or job.get("payment_unconfirmed") or job.get("problem"):
            raise InputProblem("Документ ещё требует уточнения.")
        ws=_ws()
        values=_values(ws)
        if not values or values[0][:16]!=COLS:
            raise InputProblem("Структура Transactions отличается. Сохранение остановлено без очистки таблицы.")
        existing={str(row[0]):row for row in values[1:] if row and row[0]}
        missing=[]
        for tx in job["rows"]:
            amt=money(tx["amount"])
            if tx["transaction_id"] in existing:
                row=existing[tx["transaction_id"]]
                if len(row)<6 or money(row[4])!=amt or currency(row[5])!="KZT":
                    raise InputProblem("Уже существует запись с этим ID, но другой суммой. Нужна проверка таблицы.")
                continue
            output=dict(tx,amount=float(amt),currency="KZT")
            missing.append([output.get(col,"") for col in COLS])
        if missing:
            # Single append for the remaining rows. If response is lost, keep IDs for a checked retry.
            ws.append_rows(missing,value_input_option="RAW",table_range="A1:P1")
        verify={str(row[0]) for row in _values(ws)[1:] if row and row[0]}
        if not all(tx["transaction_id"] in verify for tx in job["rows"]):
            raise RuntimeError("Запись не подтверждена повторным чтением")
    return True

async def commit(message,job):
    from services.telegram_safe import safe_answer
    job["status"]="writing";save(job)
    try:
        await asyncio.to_thread(_commit,job)
    except InputProblem as error:
        job["status"]="blocked";job["problem"]=str(error);save(job)
        await safe_answer(message,preview(job),parse_mode=None);return
    except Exception:
        job["status"]="uncertain";save(job)
        logging.exception("HF_SHEETS_WRITE_UNCERTAIN %s",job["id"])
        await safe_answer(message,f"Не получила подтверждение Google Sheets для {job['id']}. "
                          "Черновик сохранён; повторное фото не нужно.\n"
                          "Проверь таблицу, затем «повтори сохранение». Проверю ID перед дозаписью. "
                          "Обычный разговор не будет повторять эту ошибку.",parse_mode=None)
        return
    job["status"]="saved";save(job)
    state.delete("hf_focus",job["key"])
    await safe_answer(message,f"✅ Записала документ {job['id']}: {len(job['rows'])} операций.\n"
                      + "\n".join(f"{tx['type']} · {cash(tx['amount'])} KZT · {tx['category']}" for tx in job["rows"][:15]),
                      parse_mode=None)

async def handle(message,text):
    from services.telegram_safe import safe_answer
    key=key_for(message);low=text.strip().lower().replace("ё","е")
    if low in {"/receipts","покажи ожидающие чеки","чеки на уточнении"}:
        jobs=jobs_for(key)
        await safe_answer(message,"\n\n".join(preview(j) for j in jobs) if jobs else "Ожидающих документов нет.",parse_mode=None)
        return True
    choice=re.fullmatch(r"(?:чек|документ)\s+((?:doc|legacy)_[\w-]+)",low)
    if choice:
        wanted=choice[1].lower()
        job=next((j for _,j in state.entries(NS) if j.get("id","").lower()==wanted),None)
        # IDs consist of DOC_ plus digits/minus/underscore.
        if not job or job.get("key")!=key:
            await safe_answer(message,"Не нашла твой документ. /receipts — список.");return True
        focus(key,job["id"]);await safe_answer(message,preview(job),parse_mode=None);return True
    active=state.get("hf_focus",key) or {}
    if active.get("kind")!="receipt": return False
    job=state.get(NS,active.get("id",""))
    if not job or job.get("status") in {"saved","cancelled"}: return False
    if low in NO:
        job["status"]="cancelled";save(job);state.delete("hf_focus",key)
        await safe_answer(message,"Черновик отменён. Уже записанные в таблице строки не удалялись.");return True
    if job.get("status")=="uncertain":
        if low in {"повтори сохранение","повторить сохранение","проверь запись"}:
            await commit(message,job);return True
        if low in YES:
            await safe_answer(message,"Сначала проверь запись; для безопасной дозаписи напиши «повтори сохранение».");return True
        return False
    if job.get("duplicate_warning"):
        if low=="это новая операция":
            job.pop("duplicate_warning",None);save(job)
            await safe_answer(message,preview(job),parse_mode=None);return True
        if low in YES:
            await safe_answer(message,"Для новой отдельной оплаты напиши «это новая операция», иначе «отмена».");return True
        return False
    if low=="оплачено" and job.get("payment_unconfirmed"):
        job["payment_unconfirmed"]=False;save(job)
        await safe_answer(message,preview(job),parse_mode=None);return True
    curr=foreign(job["rows"])
    if curr:
        try:
            m=re.fullmatch(r"валюта\s+([a-zа-яё$€₽₸]+)",low)
            if m:
                new=currency(m[1])
                if new=="UNKNOWN": raise InputProblem("Укажи ISO-код валюты, например USD, EUR, KZT.")
                unknown=[row for row in job["rows"] if row["currency"]=="UNKNOWN"]
                if not unknown: raise InputProblem("Исходная валюта уже указана. Нужна сумма списания в KZT, не смена ярлыка.")
                for row in unknown: row["currency"]=new
            else:
                rate_match=re.fullmatch(r"курс(?:\s+([a-z]{3}))?\s+(\d+(?:[.,]\d{1,2})?)",low)
                actual_match=re.fullmatch(r"(?:(?:списали|списалось|итого)\s+)?(\d+(?:[ .,]\d+)*)(?:\s*(?:kzt|тг|тенге|₸))?(?:\s+за\s+([a-z]{3}))?",low)
                if not rate_match and not actual_match:
                    if low in YES:
                        await safe_answer(message,preview(job),parse_mode=None);return True
                    return False
                selected=(rate_match[1] if rate_match else actual_match[2])
                if selected: selected=currency(selected)
                elif len(curr)==1: selected=curr[0]
                else: raise InputProblem("Несколько валют. Укажи валюту: «списали 5900 KZT за USD».")
                if selected not in curr: raise InputProblem("Эта валюта не найдена в текущем документе.")
                job["rows"]=convert_group(job["rows"],selected,
                    rate=rate_match[2] if rate_match else None,
                    actual_kzt=actual_match[1] if actual_match else None)
            save(job)
            await safe_answer(message,preview(job),parse_mode=None);return True
        except InputProblem as error:
            await safe_answer(message,str(error),parse_mode=None);return True
    if low.startswith("комментарий:"):
        comment=text.split(":",1)[1].strip()
        for row in job["rows"]:
            row["user_comment"]=(row.get("user_comment","")+"; "+comment).strip("; ")
        save(job);await safe_answer(message,preview(job),parse_mode=None);return True
    if low in YES:
        if job.get("payment_unconfirmed") or job.get("problem"):
            await safe_answer(message,preview(job),parse_mode=None);return True
        await commit(message,job);return True
    return False

def quarantine_legacy(key=None):
    """Move poisoned old queue before deleting it. Ready KZT queues remain with old sweeper."""
    queued=[]
    for namespace in ("receipts","clarifications"):
        entries=state.entries(namespace) if key is None else [(key,state.get(namespace,key))]
        for oldkey,entry in entries:
            if not entry: continue
            rows=(entry.get("transactions") or []) if namespace=="receipts" else [entry.get("transaction") or {}]
            queued.append((namespace,oldkey,entry,rows))
    moved=0
    for namespace,oldkey,entry,rows in queued:
        try:
            bad=not rows or any(currency(r.get("currency"))!="KZT" or money(r.get("amount"))<=0 for r in rows)
        except (InputProblem,AttributeError): bad=True
        if not bad: continue
        digest=hashlib.sha256((namespace+str(oldkey)+json.dumps(entry,ensure_ascii=False,sort_keys=True)).encode()).hexdigest()[:12]
        jobid="LEGACY_"+digest
        if not state.get(NS,jobid):
            owner=entry.get("user_name") or (rows[0].get("user") if rows and isinstance(rows[0],dict) else None) or "Влад"
            problem=""
            try: cleaned=normalize_transactions(rows,owner,jobid)
            except InputProblem as error:
                cleaned=[r for r in rows if isinstance(r,dict)]
                problem=str(error)
            from services.timezone import now_astana
            for row in cleaned:
                row["date"]=row.get("date") or now_astana().strftime("%Y-%m-%d %H:%M:%S")
            job={"id":jobid,"key":str(oldkey),"owner":owner,"rows":cleaned,"status":"draft","problem":problem,"raw_legacy":entry}
            save(job);focus(str(oldkey),jobid)
        state.delete(namespace,oldkey);moved+=1
    return moved

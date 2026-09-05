"""Strict money/document validation shared by media, text and voice. No guessed FX rates."""
import copy
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

class InputProblem(ValueError):
    """Expected bad/missing input, safe to show to the user."""

CURRENCIES = {"₸":"KZT","ТГ":"KZT","ТЕНГЕ":"KZT","KZT":"KZT",
              "$":"USD","USD":"USD","ДОЛЛАР":"USD","ДОЛЛАРОВ":"USD",
              "€":"EUR","EUR":"EUR","ЕВРО":"EUR","₽":"RUB","RUB":"RUB","РУБ":"RUB",
              "РУБЛЕЙ":"RUB","CNY":"CNY","ЮАНЕЙ":"CNY","GBP":"GBP","£":"GBP"}

def currency(value):
    raw = str(value or "").strip().upper()
    return CURRENCIES.get(raw, raw if re.fullmatch(r"[A-Z]{3}",raw) else "UNKNOWN")

def money(value, *, positive=True):
    if isinstance(value, bool) or value is None:
        raise InputProblem("Сумма не распознана.")
    raw = str(value).replace("\u00a0"," ").replace("\u202f"," ").strip()
    raw = re.sub(r"\s*(?:KZT|USD|EUR|RUB|CNY|GBP|тенге|тг|долларов|рублей|[$€₽₸])\s*", "", raw, flags=re.I)
    # Group separators must be thousands, never concatenate two unrelated values.
    if " " in raw:
        signless = raw.lstrip("+-")
        if not re.fullmatch(r"\d{1,3}(?: \d{3})+(?:[.,]\d{1,2})?",signless):
            raise InputProblem("Неоднозначная сумма.")
        raw = raw.replace(" ","")
    if "," in raw and "." in raw:
        decimal = "," if raw.rfind(",") > raw.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        if not re.fullmatch(r"[+-]?\d{1,3}(?:"+re.escape(thousands)+r"\d{3})+"+re.escape(decimal)+r"\d{1,2}",raw):
            raise InputProblem("Неоднозначные разделители суммы.")
        raw = raw.replace(thousands,"").replace(decimal,".")
    elif "," in raw or "." in raw:
        sep = "," if "," in raw else "."
        pieces = raw.split(sep)
        if len(pieces)==2 and len(pieces[-1])<=2:
            raw=raw.replace(sep,".")
        elif len(pieces)>1 and all(len(p)==3 for p in pieces[1:]):
            raw="".join(pieces)
        else:
            raise InputProblem("Укажи сумму с точностью до двух знаков.")
    if not re.fullmatch(r"[+-]?\d+(?:\.\d{1,2})?",raw):
        raise InputProblem("Нужна числовая сумма, а не дата или текст.")
    try: result=Decimal(raw)
    except InvalidOperation: raise InputProblem("Сумма не распознана.")
    if not result.is_finite() or abs(result)>Decimal("1000000000000") or (positive and result<=0):
        raise InputProblem("Нужна положительная сумма.")
    return result.quantize(Decimal(".01"))

def cash(value):
    amount=money(value,positive=False)
    return f"{amount:,.2f}".replace(","," ").replace(".",",")

def normalize_transactions(rows, owner, prefix, comment=""):
    from services.categories import (EXPENSE_CATEGORIES, INCOME_CATEGORIES,
        FALLBACK_EXPENSE_CATEGORY,FALLBACK_INCOME_CATEGORY,validate_transaction_category_subcategory)
    from services.banks import normalize_bank_source
    if not isinstance(rows,list) or not rows or len(rows)>250:
        raise InputProblem("Не получен корректный список операций. Раздели длинный документ на части.")
    result=[]
    for idx,raw in enumerate(rows):
        if not isinstance(raw,dict): raise InputProblem("В ответе распознавания повреждена строка.")
        tx=copy.deepcopy(raw)
        amt=money(tx.get("amount"),positive=False)
        typ=str(tx.get("type") or "").lower()
        if typ not in {"expense","расход","income","доход","in","out"}:
            raise InputProblem("Не определено, это расход, доход или собственный перевод.")
        tx["type"]="ДОХОД" if typ in {"income","доход","in"} else "РАСХОД"
        # Explicit signed debit on a bank statement is converted to positive expense magnitude.
        if amt<0 and tx["type"]=="РАСХОД" and tx.get("signed_debit") is True:
            amt=abs(amt)
        if amt<=0: raise InputProblem("Есть нулевая/отрицательная сумма без понятного типа списания.")
        tx["amount"]=float(amt)
        tx["currency"]=currency(tx.get("currency"))
        tx["user"]=owner
        tx["transaction_id"]=str(tx.get("transaction_id") or f"{prefix}_{idx}")
        valid=INCOME_CATEGORIES if tx["type"]=="ДОХОД" else EXPENSE_CATEGORIES
        fallback=FALLBACK_INCOME_CATEGORY if tx["type"]=="ДОХОД" else FALLBACK_EXPENSE_CATEGORY
        cat,sub=validate_transaction_category_subcategory(tx.get("category"),tx.get("subcategory"),valid,fallback)
        tx.update(category=cat,subcategory=sub)
        tx["necessity"]="Need" if str(tx.get("necessity")).lower()=="need" else "Want"
        if cat in {"Алкоголь, табак и энергетики","Красота и уход","Развлечения и хобби"}:
            tx["necessity"]="Want"
        tx["bank"]=str(tx.get("bank") or "Не указан")
        from services import banks
        if hasattr(banks,"normalize_bank"):
            tx["bank"]=banks.normalize_bank(tx["bank"])
        tx["source"]=normalize_bank_source(tx["bank"],tx.get("source"))
        tx["resource"]=str(tx.get("resource") or "Карта")
        if tx["resource"].lower() in {"наличные","нал","cash"} or tx["bank"].lower() in {"наличные","нал","cash"}:
            tx["resource"]="Наличные";tx["bank"]="Не указан";tx["source"]=""
        tx["funds_type"]=str(tx.get("funds_type") or "Собственные")
        tx["merchant"]=str(tx.get("merchant") or "")
        old=str(tx.get("user_comment") or "").strip()
        tx["user_comment"]=old if not comment or comment in old else (old+"; "+comment).strip("; ")
        tx["ai_comment"]=str(tx.get("ai_comment") or "")
        # Do not invent a transaction timestamp from the upload time or a malformed OCR date.
        if tx.get("date"):
            from services.timezone import parse_flexible_datetime
            if not parse_flexible_datetime(tx["date"]): tx.pop("date",None)
        result.append(tx)
    return result

def validate_totals(rows, totals):
    """Only totals explicitly reported as comparable by OCR; never sum an account balance."""
    if not totals: return
    if not isinstance(totals,list): raise InputProblem("Не удалось проверить итоги документа.")
    for item in totals:
        if not isinstance(item,dict) or item.get("kind")!="receipt_total":
            continue
        indices=item.get("transaction_indices")
        if not isinstance(indices,list) or not indices or any(type(i) is not int or i<0 or i>=len(rows) for i in indices):
            raise InputProblem("Не удалось сопоставить итог чека с позициями.")
        cur=currency(item.get("currency"))
        if any(currency(rows[i].get("currency"))!=cur for i in indices):
            raise InputProblem("В итогах чека смешаны валюты.")
        total=sum((money(rows[i]["amount"]) for i in indices),Decimal(0))
        if abs(total-money(item.get("amount")))>Decimal(".01"):
            raise InputProblem("Сумма позиций не совпала с итогом чека. Ничего не записала: пришли чёткий чек или уточни позиции.")

def convert_group(rows, cur, *, actual_kzt=None, rate=None):
    """Explicit user total/rate only. Allocate cents deterministically, preserving the total."""
    result=copy.deepcopy(rows)
    indices=[i for i,r in enumerate(result) if r["currency"]==cur]
    if not indices or cur in {"KZT","UNKNOWN"}: raise InputProblem("Сначала уточни исходную валюту.")
    if rate is not None:
        rate=money(rate)
        amounts={i:(money(result[i]["amount"])*rate).quantize(Decimal(".01"),rounding=ROUND_HALF_UP) for i in indices}
        method=f"курс пользователя {rate} KZT за 1 {cur}"
    else:
        total=money(actual_kzt)
        weights=[money(result[i]["amount"]) for i in indices]
        weight_sum=sum(weights)
        cents=int(total*100)
        exact=[Decimal(cents)*w/weight_sum for w in weights]
        shares=[int(v) for v in exact]
        left=cents-sum(shares)
        for j in sorted(range(len(indices)),key=lambda j:exact[j]-shares[j],reverse=True)[:left]:
            shares[j]+=1
        amounts={i:Decimal(shares[j])/100 for j,i in enumerate(indices)}
        method="фактическое списание со слов пользователя"
        if len(indices)>1: method+="; итог распределён пропорционально позициям"
    for i,amount in amounts.items():
        if amount<=0: raise InputProblem("После пересчёта получается нулевая позиция; уточни распределение сумм.")
        old=result[i]
        note=f"Оригинал: {money(old['amount'])} {cur}; {method}"
        old["original_amount"]=old["amount"];old["original_currency"]=cur
        old["amount"]=float(amount);old["currency"]="KZT"
        old["user_comment"]=(old.get("user_comment","")+"; "+note).strip("; ")
    return result

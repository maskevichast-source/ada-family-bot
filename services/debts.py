"""Append-only debt event ledger: loans and principal repayments are not income/expense."""
import re
import uuid
from decimal import Decimal
from services.money import parse_amount, format_currency
from services.timezone import now_astana
from services import state

HEADERS = ["event_id", "debt_id", "date", "owner", "counterparty", "direction",
           "event_type", "amount", "currency", "due_date", "note"]
HELP = ("🤝 Долги\n\n"
        "«Дал Саше в долг 10 000»\n«Взял у Саши в долг 5 000»\n"
        "«Саша вернул мне 2 000 в счёт долга»\n«Вернул Саше долг 1 000»\n"
        "«Покажи долги»\n«Погаси долг DEBT_… на 1 000»\n\n"
        "Перед записью покажу направление, человека и сумму и попрошу подтверждение. "
        "Если долгов несколько, укажи ID. Срок можно добавить: «до 20.10.2026». "
        "Поддерживается KZT; проценты учитывай отдельным доходом/расходом.")

def worksheet():
    from services.sheets import _get_or_create_worksheet
    ws = _get_or_create_worksheet("Debts", HEADERS, rows=100, cols=len(HEADERS))
    if ws.row_values(1) != HEADERS:
        raise RuntimeError("Неожиданная схема Debts")
    return ws

def events():
    from services.sheets import _get_all_records_safe
    return _get_all_records_safe(worksheet())

def balances(rows=None):
    result = {}
    for r in events() if rows is None else rows:
        if not r.get("debt_id"):
            continue
        if r["event_type"] == "open":
            if r["debt_id"] in result:
                raise ValueError("Повторяющийся ID долга")
            result[r["debt_id"]] = dict(r, balance=Decimal(str(r["amount"])))
        elif r["event_type"] == "repay":
            if r["debt_id"] not in result:
                raise ValueError("Возврат без исходного долга")
            result[r["debt_id"]]["balance"] -= Decimal(str(r["amount"]))
    for item in result.values():
        if item["balance"] < 0:
            raise ValueError("Отрицательный остаток долга — проверьте журнал")
        item["balance"] = float(item["balance"])
    return list(result.values())

def record(payload, event_id):
    rows = events()
    previous = next((r for r in rows if r["event_id"] == event_id), None)
    if previous:
        return previous
    amount = Decimal(str(payload["amount"]))
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal(".01")):
        raise ValueError("Нужна положительная сумма, не более двух знаков после запятой.")
    p = dict(payload)
    if p.get("currency", "KZT") != "KZT":
        raise ValueError("Пока поддерживается только KZT")
    if p["event_type"] == "repay":
        debt = next((d for d in balances(rows) if d["debt_id"] == p["debt_id"]), None)
        if not debt or debt["owner"] != p["owner"]:
            raise ValueError("Долг не найден у текущего пользователя.")
        if amount > Decimal(str(debt["balance"])):
            raise ValueError("Возврат превышает остаток долга.")
        p.update({key: debt[key] for key in ("counterparty", "direction", "currency", "due_date")})
    elif p["event_type"] == "open":
        if p["direction"] not in {"lent", "borrowed"} or not p.get("counterparty"):
            raise ValueError("Не определены направление или участник долга")
        p["debt_id"] = p.get("debt_id") or "DEBT_" + uuid.uuid4().hex[:10]
    else:
        raise ValueError("Неизвестная операция")
    p.update(event_id=event_id, date=now_astana().strftime("%Y-%m-%d %H:%M:%S"),
             amount=float(amount), currency="KZT")
    worksheet().append_row([p.get(h, "") for h in HEADERS], table_range="A1:K1", value_input_option="RAW")
    return p

def person_key(name):
    s = re.sub(r"[^а-яa-z]", "", name.lower().replace("ё", "е"))
    aliases = {"саше": "саша", "саши": "саша", "сашей": "саша",
               "диане": "диана", "дианы": "диана", "владу": "влад", "влада": "влад"}
    return aliases.get(s, s)

def is_debt_request(text):
    low = text.lower().replace("ё", "е")
    # "я должен работать" and "вернул товар" are not loan events.
    return bool(re.search(r"\bдолг(?:а|и|ов|ом|у|е)?\b|\bвзаймы\b|\bdebt_|\bодолж\w*|\bзанял[аи]?\s+у\b", low)
                or re.search(r"\b(?:кто|кому|сколько)\b.{0,25}\bдолж(?:ен|на|ны)\b",low))

def parse_local(text, owner):
    t = text.strip()
    low = t.lower().replace("ё", "е")
    if re.search(r"\b(?:usd|eur|руб|рублей|доллар\w*|евро)\b|[$€₽]", low):
        raise ValueError("Пока поддерживается только KZT. Не буду записывать другую валюту как тенге.")
    # Separate dates/IDs before amount extraction.
    clean = re.sub(r"debt_[a-z0-9]+", "", low)
    clean = re.sub(r"\d{1,2}\.\d{1,2}\.\d{4}", "", clean)
    numeric_groups = re.findall(r"\d+(?:[ .,]\d+)*", clean)
    if len(numeric_groups) > 1:
        raise ValueError("Несколько сумм в одном сообщении. Запиши каждый долг или возврат отдельно.")
    amount = parse_amount(clean)
    if amount <= 0:
        raise ValueError("Укажи положительную сумму долга или возврата.")
    repayment = bool(re.search(r"вернул|возврат|погас", low))
    ids = re.findall(r"debt_[a-z0-9]+", low)
    direction = None
    name = None
    # Explicit lending/borrowing only; "одолжил" alone is ambiguous.
    if re.search(r"(?:взял[аи]?|занял[аи]?)\s+у\b", low):
        direction = "borrowed"
        m = re.search(r"\bу\s+([а-яa-z-]+)", low)
        name = m.group(1) if m else None
    elif re.search(r"\b(?:дал[аи]?|выдал[аи]?)\s+", low):
        direction = "lent"
        m = re.search(r"\b(?:дал[аи]?|выдал[аи]?)\s+([а-яa-z-]+)", low)
        name = m.group(1) if m else None
        if name in {"в", "взаймы"}:
            m = re.search(r"(?:в\s+долг|взаймы)\s+([а-яa-z-]+)", low)
            name = m.group(1) if m else None
    if repayment:
        if re.search(r"вернул[аи]?\s+мне|мне\s+вернул", low):
            direction = "lent"
            m = re.search(r"^([а-яa-z-]+)\s+вернул", low)
            if not m:
                m = re.search(r"мне\s+вернул[аи]?\s+([а-яa-z-]+)", low)
            name = m.group(1) if m else None
        elif re.search(r"^(?:я\s+)?вернул", low):
            direction = "borrowed"
            m = re.search(r"вернул[аи]?\s+([а-яa-z-]+)", low)
            name = m.group(1) if m else None
            if name == "долг":
                m = re.search(r"вернул[аи]?\s+долг\s+([а-яa-z-]+)", low)
                name = m.group(1) if m else None
        items = [d for d in balances() if d["owner"] == owner and d["balance"] > 0]
        if ids:
            items = [d for d in items if d["debt_id"].lower() in ids]
        else:
            if not direction or not name:
                raise ValueError("Уточни, кто кому вернул, или укажи ID: «Погаси долг DEBT_… на 1000».")
            items = [d for d in items if d["direction"] == direction and person_key(d["counterparty"]) == person_key(name)]
        if len(items) != 1:
            raise ValueError("Нужен один конкретный долг. Напиши «Покажи долги», затем «Погаси долг DEBT_… на 1000».")
        d = items[0]
        if amount > d["balance"]:
            raise ValueError(f"Возврат больше остатка: {format_currency(d['balance'])}.")
        return dict(d, event_type="repay", amount=amount, note=text)
    if not direction or not name or name in {"долг", "в", "у"}:
        raise ValueError("Уточни направление: «Дал Саше в долг 1000» или «Взял у Саши в долг 1000».")
    due = ""
    m = re.search(r"\bдо\s+(\d{1,2}\.\d{1,2}\.\d{4})", low)
    if m:
        from services.timezone import parse_flexible_datetime
        parsed = parse_flexible_datetime(m.group(1))
        if not parsed:
            raise ValueError("Некорректная дата возврата.")
        due = parsed.date().isoformat()
    return {"owner": owner, "counterparty": name.capitalize(), "direction": direction,
            "event_type": "open", "amount": amount, "currency": "KZT", "due_date": due, "note": text}

def format_balances(items):
    active = [d for d in items if d["balance"] > 0]
    if not active:
        return "🤝 Непогашенных долгов нет."
    lines = ["🤝 Долги семьи"]
    for direction, title in (("lent", "Нам должны"), ("borrowed", "Мы должны")):
        group = [d for d in active if d["direction"] == direction]
        lines.append(f"\n{title}: {format_currency(sum(d['balance'] for d in group))}")
        for d in group:
            overdue = d.get("due_date") and d["due_date"] < now_astana().date().isoformat()
            lines.append(f"\n{d['owner']} ↔ {d['counterparty']}: {format_currency(d['balance'])}\n"
                         f"ID: {d['debt_id']}" +
                         (f"\nСрок: {d['due_date']}" + (" · просрочен" if overdue else "") if d.get("due_date") else ""))
    lines.append("\nЭто остатки основного долга, не доходы и не расходы. Автонапоминания о сроках не создаются.")
    return "\n".join(lines)

async def handle(message, text, owner):
    import asyncio
    from services.telegram_safe import safe_answer
    key = state.dialogue_key(message.chat.id, message.from_user.id)
    t = text.lower().strip().replace("ё", "е")
    draft = state.get("debt_draft", key)
    if draft and t in {"да", "подтверждаю", "нет", "отмена"}:
        if t in {"нет", "отмена"}:
            state.delete("debt_draft", key)
            await safe_answer(message, "Запись долга отменена."); return True
        try:
            saved = await asyncio.to_thread(record, draft["payload"], draft["event_id"])
        except ValueError as error:
            await safe_answer(message, str(error)); return True
        state.delete("debt_draft", key)
        await safe_answer(message, f"✅ {'Долг' if saved['event_type']=='open' else 'Возврат'} записан: "
                          f"{format_currency(saved['amount'])}\nID: {saved['debt_id']}\n"
                          "Обычные доходы и расходы не изменены.", parse_mode=None)
        return True
    if not is_debt_request(t) and t not in {"/debts", "/debts_help"}:
        return False
    if t == "/debts_help":
        await safe_answer(message, HELP); return True
    if re.search(r"покажи|список|сколько|какие|кто.*должен|кому.*долж", t) or t in {"долги", "/debts"}:
        await safe_answer(message, format_balances(await asyncio.to_thread(balances)), parse_mode=None)
        return True
    try:
        payload = await asyncio.to_thread(parse_local, text, owner)
    except ValueError:
        # The conversational model can resolve pronouns/names and draft a safe proposal.
        # Model proposal still requires validation and confirmation.
        return False
    # One confirmation at a time, never confirm an unrelated stale action.
    state.delete("reminder_delete", key)
    state.delete("reminder_plan", key)
    state.delete("reminder_draft", key)
    state.delete("edit_plan", key)
    state.put("debt_draft", key, {"payload": payload, "event_id": f"DE_{message.chat.id}_{message.message_id}"})
    action = ("Выдача в долг" if payload["direction"] == "lent" else "Получение в долг") if payload["event_type"] == "open" else (
        "Возврат вам" if payload["direction"] == "lent" else "Ваш возврат")
    await safe_answer(message, f"Проверь запись\n\n{action}\n"
        f"Участник семьи: {owner}\nКонтрагент: {payload['counterparty']}\n"
        f"Сумма: {format_currency(payload['amount'])}\n"
        + (f"Долг: {payload['debt_id']}\n" if payload.get("debt_id") else "")
        + (f"Срок: {payload['due_date']}\n" if payload.get("due_date") else "")
        + "\nЗаписать? «Да» / «Нет».", parse_mode=None)
    return True


async def handle_model(message, parsed, owner):
    import asyncio
    from services.telegram_safe import safe_answer
    intent = parsed.get("intent")
    if intent == "get_debts":
        await safe_answer(message, format_balances(await asyncio.to_thread(balances)), parse_mode=None)
        return True
    if intent != "debt":
        return False
    p = dict(parsed.get("debt") or {})
    p["owner"] = owner
    p["amount"] = parse_amount(p.get("amount"))
    p["currency"] = str(p.get("currency") or "KZT").upper()
    try:
        if p["currency"] != "KZT" or p["amount"] <= 0:
            raise ValueError("Укажи положительную сумму в KZT.")
        if p.get("event_type") == "repay":
            active = [d for d in await asyncio.to_thread(balances) if d["owner"] == owner and d["balance"] > 0]
            active = [d for d in active if d["debt_id"] == p.get("debt_id")]
            if len(active) != 1:
                raise ValueError("Уточни конкретный долг по ID из /debts.")
            d = active[0]
            if p["amount"] > d["balance"]:
                raise ValueError(f"Остаток только {format_currency(d['balance'])}.")
            p.update({k: d.get(k,"") for k in ("counterparty","direction","currency","due_date")})
        elif p.get("event_type") == "open":
            if p.get("direction") not in {"lent","borrowed"} or not str(p.get("counterparty") or "").strip():
                raise ValueError("Уточни, кто кому дал деньги и сколько.")
            p.pop("debt_id", None)
        else:
            raise ValueError("Это выдача/получение долга или возврат?")
        if p.get("due_date"):
            from services.timezone import parse_flexible_datetime
            date = parse_flexible_datetime(p["due_date"])
            if not date: raise ValueError("Некорректный срок возврата.")
            p["due_date"] = date.date().isoformat()
    except ValueError as error:
        await safe_answer(message, str(error))
        return True
    key = state.dialogue_key(message.chat.id, message.from_user.id)
    for namespace in ("reminder_delete","reminder_plan","reminder_draft"):
        state.delete(namespace,key)
    state.delete("edit_plan", key)
    state.put("debt_draft",key,{"payload":p,"event_id":f"DE_{message.chat.id}_{message.message_id}"})
    direction = "вам должны" if p["direction"] == "lent" else "вы должны"
    await safe_answer(message, f"Проверь долг\n\nУчастник: {owner}\nКонтрагент: {p['counterparty']}\n"
        f"Направление: {direction}\nОперация: {'возврат' if p['event_type']=='repay' else 'новый долг'}\n"
        f"Сумма: {format_currency(p['amount'])}\n"
        + (f"Срок: {p['due_date']}\n" if p.get("due_date") else "")
        + "\nЗаписать? «Да» / «Нет».",parse_mode=None)
    return True


from services.sheets import _serialized
worksheet = _serialized(worksheet)
events = _serialized(events)
balances = _serialized(balances)
record = _serialized(record)
parse_local = _serialized(parse_local)

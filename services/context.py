"""Collect all family knowledge, with explicit unavailable-vs-empty distinction."""
import asyncio
from services import sheets, reminders, debts, state
from services.timezone import now_astana

async def collect(message):
    import datetime
    now = now_astana()
    def today():
        return sheets.get_transactions_for_period(now.date().isoformat(),
                    (now.date()+datetime.timedelta(days=1)).isoformat())
    loaders = {
        "history": sheets.get_last_200_transactions, "today_transactions": today,
        "limits": sheets.get_category_limits, "reminders": reminders.pending,
        "shopping_list": sheets.get_shopping_items, "trips": sheets.get_planned_trips,
        "subscriptions": sheets.get_active_subscriptions, "installments": sheets.get_installments,
        "debts": debts.balances}
    values = await asyncio.gather(*(asyncio.to_thread(fn) for fn in loaders.values()), return_exceptions=True)
    context, errors = {}, []
    for name, value in zip(loaders, values):
        if isinstance(value, Exception):
            import logging
            logging.error("Context source unavailable: %s (%s)", name, type(value).__name__)
            errors.append(name)
            context[name] = None
        else:
            context[name] = value
    key = state.dialogue_key(message.chat.id, message.from_user.id)
    context["dialogue_state"] = {name: state.get(name, key) for name in (
        "reminder_draft", "reminder_plan", "reminder_delete", "reminder_list", "debt_draft", "edit_plan")}
    reply = getattr(message, "reply_to_message", None)
    if reply:
        context["reply_to"] = {"text": getattr(reply, "text", None) or getattr(reply, "caption", "")}
    context["pending_receipt"] = state.get("receipts", key)
    context["pending_clarification"] = state.get("clarifications", key)
    context["context_errors"] = errors
    return context

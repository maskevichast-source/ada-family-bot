"""Compatible legacy queue API. Foreign/invalid drafts are excluded from old auto-save."""
from services import state
PENDING_TTL_MINUTES=20

def set_pending(chat_id,transactions,user_name):
    old=state.get("receipts",chat_id) or {}
    rows=list(old.get("transactions",[]))
    seen={r.get("transaction_id") for r in rows if isinstance(r,dict) and r.get("transaction_id")}
    for tx in transactions:
        if not tx.get("transaction_id") or tx["transaction_id"] not in seen:
            rows.append(tx)
            if tx.get("transaction_id"): seen.add(tx["transaction_id"])
    state.put("receipts",chat_id,{"transactions":rows,"user_name":user_name})
    from services.receipt_flow import quarantine_legacy
    quarantine_legacy(chat_id)

def has_pending(chat_id):
    from services.receipt_flow import quarantine_legacy
    quarantine_legacy(chat_id)
    return state.get("receipts",chat_id) is not None

def pop_pending(chat_id):
    if not has_pending(chat_id): return None
    entry=state.get("receipts",chat_id)
    return (entry["transactions"],entry["user_name"]) if entry else None

def ack_pending(chat_id):
    state.delete("receipts",chat_id)

def sweep_expired():
    from services.receipt_flow import quarantine_legacy
    quarantine_legacy()
    return [(key,entry["transactions"],entry["user_name"])
            for key,entry in state.entries("receipts",PENDING_TTL_MINUTES*60)]

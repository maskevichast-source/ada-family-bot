"""Durable receipt queue, scoped to chat + author; acknowledge only after saving."""
from services import state
PENDING_TTL_MINUTES = 20

def set_pending(chat_id, transactions, user_name):
    old = state.get("receipts", chat_id) or {}
    combined = old.get("transactions", [])[:]
    seen = {tx.get("transaction_id") for tx in combined if tx.get("transaction_id")}
    for tx in transactions:
        if not tx.get("transaction_id") or tx["transaction_id"] not in seen:
            combined.append(tx)
            if tx.get("transaction_id"): seen.add(tx["transaction_id"])
    state.put("receipts", chat_id, {"transactions": combined, "user_name": user_name})

def has_pending(chat_id):
    return state.get("receipts", chat_id) is not None

def pop_pending(chat_id):
    # Compatibility name: do NOT delete until ack_pending.
    entry = state.get("receipts", chat_id)
    return (entry["transactions"], entry["user_name"]) if entry else None

def ack_pending(chat_id):
    state.delete("receipts", chat_id)

def sweep_expired():
    return [(key, value["transactions"], value["user_name"])
            for key, value in state.entries("receipts", PENDING_TTL_MINUTES * 60)]

"""Durable author-scoped category drafts; old keyboard cannot resolve a new draft."""
from services import state
import uuid
CLARIFICATION_TTL_MINUTES = 10

def set_clarification(chat_id, transaction, options, message_id=None):
    token = uuid.uuid4().hex[:10]
    state.put("clarifications", chat_id, {"transaction": transaction, "options": options,
               "message_id": message_id, "token": token})
    return token

def get_clarification(chat_id):
    return state.get("clarifications", chat_id)

def has_clarification(chat_id):
    return get_clarification(chat_id) is not None

def pop_clarification(chat_id):
    return get_clarification(chat_id)

def ack_clarification(chat_id):
    state.delete("clarifications", chat_id)

def sweep_expired_clarifications():
    return [(key, value["transaction"]) for key,value in state.entries("clarifications", CLARIFICATION_TTL_MINUTES*60)]

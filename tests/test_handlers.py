"""Orchestration tests with fake Sheets/messages; not live Telegram or LLM tests."""
import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

@pytest.fixture
def handler(monkeypatch, db):
    import config
    monkeypatch.setattr(config,"VLAD_TELEGRAM_ID","11")
    monkeypatch.setattr(config,"DIANA_TELEGRAM_ID","22")
    monkeypatch.setattr(config,"CHAT_HISTORY_FILE","/tmp/unused-test-history.json")
    from services.preflight import initialize_optional
    initialize_optional()
    from handlers import text_handler as h
    monkeypatch.setattr(h, "add_chat_message", lambda *args:None)
    monkeypatch.setattr(h, "get_chat_history", lambda *args:[])
    return h

def message(text, uid=11, mid=50):
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=uid,first_name="Влад" if uid==11 else "Диана"),
          chat=SimpleNamespace(id=-100,type="supergroup"), message_id=mid,
          answer=AsyncMock(), bot=SimpleNamespace(send_chat_action=AsyncMock()))

def test_purchase_with_card_does_not_edit_previous(handler,db,monkeypatch):
    from services.sheets import append_transaction
    append_transaction({"transaction_id":"OLD","amount":500,"bank":"BCC"})
    monkeypatch.setattr(handler,"parse_and_analyze",AsyncMock(return_value={
        "intent":"transaction","transaction":{"amount":1200,"bank":"Kaspi","category":"Транспорт","type":"РАСХОД"},
        "reply":""}))
    asyncio.run(handler.handle_text(message("Оплатил такси 1200 с карты Kaspi")))
    data=db.worksheet("Transactions").data
    assert len(data)==3 and data[1][6]=="BCC" and data[2][6]=="Kaspi"

def test_reminder_not_swallowed_by_receipt(handler,db):
    from services import state, pending_receipts as p, reminders
    key=state.dialogue_key(-100,11)
    p.set_pending(key,[{"transaction_id":"R","amount":500}],"Влад")
    asyncio.run(handler.handle_text(message("наопмни диане завтра в 12.00 написать Салтанат")))
    assert len(reminders.pending())==1 and p.has_pending(key)
    assert len(db.worksheet("Transactions").data)==1

def test_other_users_message_not_receipt_comment(handler,db,monkeypatch):
    from services import pending_receipts as p
    p.set_pending("-100:11",[{"transaction_id":"R","amount":500}],"Влад")
    monkeypatch.setattr(handler,"parse_and_analyze",AsyncMock(return_value={"intent":"chat","reply":"Привет"}))
    asyncio.run(handler.handle_text(message("Привет",22)))
    assert p.has_pending("-100:11") and len(db.worksheet("Transactions").data)==1

def test_delete_reminder_never_delete_transaction(handler,db,monkeypatch):
    from services.sheets import append_transaction
    append_transaction({"transaction_id":"TX1","amount":500,"user_comment":"витамины"})
    parse=AsyncMock()
    monkeypatch.setattr(handler,"parse_and_analyze",parse)
    asyncio.run(handler.handle_text(message("Удали напоминание про витамины")))
    assert len(db.worksheet("Transactions").data)==2 and not parse.called

def test_stale_callback_does_not_resolve_new_draft(handler,db):
    from services.pending_clarifications import set_clarification,get_clarification
    set_clarification("-100:11",{"amount":500},[{"label":"еда","category":"Еда и продукты"}])
    callback=SimpleNamespace(message=message(""),from_user=SimpleNamespace(id=11),
                             data="clarify_opt:old_token:0",answer=AsyncMock())
    asyncio.run(handler.handle_category_clarification_callback(callback))
    assert get_clarification("-100:11") is not None
    assert len(db.worksheet("Transactions").data)==1

def test_privacy_requires_id_and_safe_chat(handler, monkeypatch):
    import config
    settings={"TELEGRAM_BOT_TOKEN":"123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
              "FAMILY_CHAT_ID":-100,"OPENAI_API_KEY":"test","DEEPSEEK_API_KEY":"test",
              "GOOGLE_SHEETS_KEY":"test"}
    for key,value in settings.items(): monkeypatch.setattr(config,key,value)
    main=importlib.import_module("main")
    middleware=main.FamilyPrivacyMiddleware()
    fn=AsyncMock()
    stranger=message("hi",uid=99)
    asyncio.run(middleware(fn,stranger,{}))
    assert not fn.called
    authorized=message("hi")
    asyncio.run(middleware(fn,authorized,{}))
    assert fn.call_count==1
    authorized.chat.id=-999
    asyncio.run(middleware(fn,authorized,{}))
    assert fn.call_count==1

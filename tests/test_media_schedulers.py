import asyncio
import datetime as dt
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from test_handlers import handler, message
from services import sheets, state, reminders
from services.pending_receipts import has_pending, pop_pending
from services.timezone import now_astana, ASTANA_TZ
from services.pending_clarifications import set_clarification

def media_message(caption="", doc=True):
    m=message("")
    m.caption=caption
    m.document=SimpleNamespace(file_name="receipt.pdf",file_size=10,file_id="DOC") if doc else None
    m.photo=[] if doc else [SimpleNamespace(file_id="PHOTO")]
    m.bot=SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(file_path="file")),
                          download_file=AsyncMock(return_value=io.BytesIO(b"fake-image")))
    return m

def test_photo_and_pdf_handlers_queue(handler,db,monkeypatch):
    from handlers import media_handler as mh
    parse=AsyncMock(return_value={"transactions":[{"amount":1000,"user_comment":"Еда","category":"Еда и продукты"}]})
    monkeypatch.setattr(mh,"parse_receipt",parse)
    for doc in (True,False):
        m=media_message(doc=doc)
        m.message_id=10 if doc else 11
        asyncio.run(mh.handle_media(m))
    assert len(pop_pending("-100:11")[0])==2
    assert len(db.worksheet("Transactions").data)==1

def test_captioned_receipt_partial_failure_queue_retained(handler,db,monkeypatch):
    from handlers import media_handler as mh
    monkeypatch.setattr(mh,"parse_receipt",AsyncMock(return_value={"transactions":[
        {"amount":1000,"user_comment":"еда"},{"amount":500,"user_comment":"напиток"}]}))
    original=mh.append_transaction
    count=0
    def append(tx):
        nonlocal count
        count+=1
        if count==2: raise RuntimeError("network")
        return original(tx)
    monkeypatch.setattr(mh,"append_transaction",append)
    m=media_message("Обед")
    with pytest.raises(RuntimeError):
        asyncio.run(mh.handle_media(m))
    assert has_pending("-100:11") and len(db.worksheet("Transactions").data)==2
    monkeypatch.setattr(mh,"append_transaction",original)
    asyncio.run(mh.handle_media(m))
    assert not has_pending("-100:11") and len(db.worksheet("Transactions").data)==3

def test_voice_uses_same_text_router(handler,db,monkeypatch):
    monkeypatch.setattr(handler,"transcribe_voice",AsyncMock(return_value="такси 1000"))
    monkeypatch.setattr(handler,"parse_and_analyze",AsyncMock(return_value={"intent":"transaction","transaction":{"amount":1000}}))
    m=media_message()
    m.voice=SimpleNamespace(file_id="VOICE",file_size=100)
    m.audio=None
    asyncio.run(handler.handle_voice(m))
    assert len(db.worksheet("Transactions").data)==2

def test_pdf_render_all_pages_and_refuse_overlimit(tmp_path):
    import fitz
    from services.vision import _render_pdf_pages
    doc=fitz.open()
    for i in range(3):
        page=doc.new_page(); page.insert_text((30,30),f"Page {i}")
    pdf=tmp_path/"three.pdf"; doc.save(pdf); doc.close()
    images=_render_pdf_pages(pdf,tmp_path)
    assert len(images)==3 and all(p.exists() for p in images)
    doc=fitz.open()
    for _ in range(13): doc.new_page()
    pdf=tmp_path/"thirteen.pdf"; doc.save(pdf); doc.close()
    with pytest.raises(ValueError): _render_pdf_pages(pdf,tmp_path)

def test_vision_does_not_silently_accept_truncated_response(monkeypatch):
    from services import vision
    completion=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="length",message=SimpleNamespace(content='{"transactions":[]}'))]))
    close=AsyncMock()
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)),close=close)
    monkeypatch.setattr(vision,"AsyncOpenAI",lambda **kwargs:client)
    result=asyncio.run(vision.parse_receipt(b"fake","receipt.jpg"))
    assert result["transactions"]==[] and "частями" in result["reply"]
    assert close.called

class StopTick(BaseException): pass

@pytest.fixture
def app(handler,monkeypatch):
    import config
    for key,value in {"TELEGRAM_BOT_TOKEN":"123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
                      "FAMILY_CHAT_ID":-100,"OPENAI_API_KEY":"test","DEEPSEEK_API_KEY":"test","GOOGLE_SHEETS_KEY":"test"}.items():
        monkeypatch.setattr(config,key,value)
    import main
    monkeypatch.setattr(main,"bot",SimpleNamespace(send_message=AsyncMock()))
    monkeypatch.setattr(main.asyncio,"sleep",AsyncMock(side_effect=StopTick()))
    return main

def frozen(monkeypatch,app,when):
    class Clock(dt.datetime):
        @classmethod
        def now(cls,tz=None): return when
    monkeypatch.setattr(app,"datetime",SimpleNamespace(datetime=Clock,timedelta=dt.timedelta,date=dt.date))

def tick(fn):
    with pytest.raises(StopTick): asyncio.run(fn())

def test_weather_windows_once_and_restart_marker(app,monkeypatch):
    when=dt.datetime(2026,9,5,8,35,tzinfo=ASTANA_TZ)
    frozen(monkeypatch,app,when)
    weather=AsyncMock(return_value="Погода сегодня")
    monkeypatch.setattr(app,"get_weather_forecast",weather)
    tick(app.weather_scheduler)
    tick(app.weather_scheduler)
    assert app.bot.send_message.call_count==1
    assert state.get("scheduler","weather_morning:2026-09-05")
    frozen(monkeypatch,app,when.replace(hour=22,minute=35))
    monkeypatch.setattr(app,"get_tomorrow_forecast",AsyncMock(return_value="Погода завтра"))
    tick(app.weather_scheduler)
    assert app.bot.send_message.call_count==2

def test_subscription_hourly_retry_after_missed_minute5(app,monkeypatch,db):
    sheets.add_or_update_subscription("Test",1000,"Kaspi",1)
    when=dt.datetime(2026,9,5,10,7,tzinfo=ASTANA_TZ)
    frozen(monkeypatch,app,when)
    tick(app.check_subscriptions)
    tick(app.check_subscriptions)
    assert len(db.worksheet("Transactions").data)==2
    assert state.get("scheduler","subscriptions:2026-09-05T10")

def test_receipt_ttl_auto_save(app,monkeypatch,db):
    from services.pending_receipts import set_pending
    set_pending("-100:11",[{"transaction_id":"TTL","amount":500}],"Влад")
    with state.connection() as conn: conn.execute("UPDATE state SET created=0 WHERE namespace='receipts'")
    tick(app.sweep_pending_receipts)
    assert not has_pending("-100:11")
    assert len(db.worksheet("Transactions").data)==2

def test_clarification_ttl_auto_save(app,db):
    set_clarification("-100:11",{"transaction_id":"TTL_C","amount":500},[])
    with state.connection() as conn: conn.execute("UPDATE state SET created=0 WHERE namespace='clarifications'")
    tick(app.sweep_clarifications)
    assert state.get("clarifications","-100:11") is None
    assert len(db.worksheet("Transactions").data)==2

def test_weekly_summary_and_monthly_limits(app,monkeypatch):
    when=dt.datetime(2026,9,7,9,15,tzinfo=ASTANA_TZ) # Monday
    frozen(monkeypatch,app,when)
    calls=[]
    monkeypatch.setattr(app,"_period_summary_text",lambda *args: calls.append(args) or "Отчет")
    tick(app.finance_report_scheduler)
    assert calls[0][1:] == ("2026-08-31","2026-09-07","2026-08-24","2026-08-31")
    frozen(monkeypatch,app,when.replace(day=1,minute=25))
    tick(app.finance_report_scheduler)
    assert calls[-1][1:]==("2026-08-01","2026-09-01","2026-07-01","2026-08-01")
    gen=AsyncMock(return_value={"Транспорт":1000})
    monkeypatch.setattr(app,"generate_limits_from_history",gen)
    monkeypatch.setenv("AUTO_GENERATE_LIMITS","true")
    tick(app.monthly_limits_scheduler)
    assert gen.called and state.get("scheduler","limits:2026-09")

def test_pdf_text_no_emoji_and_real_newlines():
    from services.reports import _wrap_pdf
    text=_wrap_pdf("🔎 Анализ бюджета\n💡 Совет",80)
    assert "🔎" not in text and "\n" in text and "\\n" not in text

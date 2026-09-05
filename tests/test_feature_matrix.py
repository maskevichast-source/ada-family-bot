"""Feature preservation and Railway regression tests; external APIs are replaced."""
import asyncio
import datetime as dt
import importlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from test_handlers import handler, message
from test_release import NOW, seed_reminder
from services import sheets, reminders, debts, state
from services.timezone import now_astana
from services.pending_receipts import set_pending, has_pending
from services.pending_clarifications import set_clarification, get_clarification

def model(monkeypatch, handler, result):
    mocked=AsyncMock(return_value=result)
    monkeypatch.setattr(handler,"parse_and_analyze",mocked)
    return mocked

def test_income_path(handler, db, monkeypatch):
    from services.categories import INCOME_CATEGORIES
    model(monkeypatch,handler,{"intent":"transaction","transaction":{"amount":300000,"type":"income","category":INCOME_CATEGORIES[0]},"reply":"Доход"})
    asyncio.run(handler.handle_text(message("Зарплата 300000")))
    row=db.worksheet("Transactions").data[1]
    assert row[3]=="ДОХОД" and row[4]==300000 and row[10] in INCOME_CATEGORIES

@pytest.mark.parametrize("intent,expected",[
    ("get_summary","Доходы"),("get_income","Доходы"),("get_limits","лимит"),
    ("get_installments","рассроч"),("get_subscriptions","подпис"),("get_shopping","покуп"),
    ("get_trips","поезд"),("get_debts","долг")])
def test_read_intents(handler,db,monkeypatch,intent,expected):
    model(monkeypatch,handler,{"intent":intent,"reply":"MODEL MUST NOT FABRICATE NUMBERS"})
    m=message("Покажи данные")
    asyncio.run(handler.handle_text(m))
    assert expected.lower() in m.answer.call_args.args[0].lower()
    assert "MODEL MUST" not in m.answer.call_args.args[0]

def test_add_close_installment(handler,db,monkeypatch):
    parse=model(monkeypatch,handler,{"intent":"add_installment","installment":{
        "description":"Телефон","total_amount":120000,"monthly_payment":10000,
        "payments_count":12,"bank":"Kaspi","kind":"Kaspi Red"}})
    asyncio.run(handler.handle_text(message("Телефон в рассрочку 120000",mid=1)))
    row=db.worksheet("Installments").data[1]
    assert row[6]==120000 and row[7]==10000 and row[4]=="Kaspi Red"
    parse.return_value={"intent":"close_installment","search_query":row[0]}
    asyncio.run(handler.handle_text(message("Закрой рассрочку телефон",mid=2)))
    assert db.worksheet("Installments").data[1][10]=="closed"

def test_subscriptions_add_list_cancel(handler,db,monkeypatch):
    parse=model(monkeypatch,handler,{"intent":"add_subscription","subscription":{
        "name":"YouTube","amount":2500,"bank":"Kaspi","day_of_month":31}})
    asyncio.run(handler.handle_text(message("Добавь подписку YouTube 2500 31 числа")))
    row=db.worksheet("Subscriptions").data[1]
    assert row[2]==2500 and row[4]==31 and row[5]==""
    parse.return_value={"intent":"get_subscriptions"}
    m=message("Что за подписки?")
    asyncio.run(handler.handle_text(m))
    assert "YouTube" in m.answer.call_args.args[0]
    parse.return_value={"intent":"cancel_subscription","subscription_name":"YouTube"}
    asyncio.run(handler.handle_text(message("Отмени подписку YouTube")))
    assert db.worksheet("Subscriptions").data[1][6]=="cancelled"

def test_shopping_add_clear_and_empty_does_not_clear_all(handler,db,monkeypatch):
    parse=model(monkeypatch,handler,{"intent":"add_shopping","shopping_items":["молоко","хлеб"]})
    asyncio.run(handler.handle_text(message("В покупки молоко хлеб")))
    assert len(sheets.get_shopping_items())==2
    parse.return_value={"intent":"clear_shopping","shopping_items":[],"reply":"Все убрала"}
    m=message("Убери это из покупок")
    asyncio.run(handler.handle_text(m))
    assert len(sheets.get_shopping_items())==2 and "Не нашла" in m.answer.call_args.args[0]
    parse.return_value={"intent":"clear_shopping","shopping_items":["молоко"]}
    asyncio.run(handler.handle_text(message("Купили молоко")))
    assert [r["item"] for r in sheets.get_shopping_items()]==["хлеб"]

def test_trip_notes_preserved(handler,db,monkeypatch):
    model(monkeypatch,handler,{"intent":"add_trip","destination":"Алматы","dates":"10–12 октября",
                               "budget":30000,"notes":"Едем вдвоём, билеты куплены"})
    asyncio.run(handler.handle_text(message("Запланируй поездку в Алматы")))
    trip=sheets.get_planned_trips()[0]
    assert trip["notes"]=="Едем вдвоём, билеты куплены" and trip["budget"]==30000
    assert db.worksheet("Trips").data[0][:4]==["trip_id","destination","dates","budget"]

def test_latest_keyword_delete_with_confirmation(handler,db,monkeypatch):
    sheets.append_transaction({"transaction_id":"OLD","amount":500,"user_comment":"такси"})
    sheets.append_transaction({"transaction_id":"NEW","amount":1000,"user_comment":"такси"})
    model(monkeypatch,handler,{"intent":"delete_transaction","search_query":"такси"})
    m=message("Удали последнее такси")
    asyncio.run(handler.handle_text(m))
    assert len(db.worksheet("Transactions").data)==3 and "NEW" in m.answer.call_args.args[0]
    asyncio.run(handler.handle_text(message("Да",mid=51)))
    assert [r[0] for r in db.worksheet("Transactions").data[1:]]==["OLD"]

def test_contextual_edit_without_imperative(handler,db,monkeypatch):
    sheets.append_transaction({"transaction_id":"X","amount":1000,"bank":"Kaspi"})
    model(monkeypatch,handler,{"intent":"correct_any_record","updates":[{"worksheet":"Transactions",
         "search_query":"X","column_to_update":"bank","new_value":"BCC","action":"update"}]})
    asyncio.run(handler.handle_text(message("Нет, это было с BCC")))
    assert db.worksheet("Transactions").data[1][6]=="Kaspi"
    asyncio.run(handler.handle_text(message("Да",mid=52)))
    row=db.worksheet("Transactions").data[1]
    assert row[6]=="BCC" and row[7]=="BCC Pay"

def test_split_model_and_atomic_batch(handler,db,monkeypatch):
    sheets.append_transaction({"transaction_id":"S","amount":1000})
    db.worksheet("Transactions").id=123
    requests=[]
    db.batch_update=lambda payload:requests.append(payload)
    model(monkeypatch,handler,{"intent":"split_transaction","split":{"transaction_id":"S","amount":1000,"parts":[
        {"amount":400,"category":"Еда и продукты","comment":"еда"},
        {"amount":600,"category":"Транспорт","comment":"такси"}]}})
    asyncio.run(handler.handle_text(message("Раздели последнюю на еду 400 и такси 600")))
    assert len(requests)==1
    rows=requests[0]["requests"][1]["updateCells"]["rows"]
    assert sum(row["values"][4]["userEnteredValue"]["numberValue"] for row in rows)==1000

def test_duplicate_warns_but_saves_new_message(handler,db,monkeypatch):
    sheets.append_transaction({"transaction_id":"ORIG","amount":500,"user_comment":"Купил молоко за 500"})
    model(monkeypatch,handler,{"intent":"transaction","transaction":{"amount":500,"category":"Еда и продукты"}})
    m=message("Купил молоко за 500",mid=2)
    asyncio.run(handler.handle_text(m))
    assert len(db.worksheet("Transactions").data)==3
    assert "дубль" in m.answer.call_args.args[0]

def test_purchase_umbrella_not_weather(handler,db,monkeypatch):
    parse=model(monkeypatch,handler,{"intent":"transaction","transaction":{"amount":500}})
    weather=AsyncMock()
    monkeypatch.setattr(handler,"get_weather_forecast",weather)
    asyncio.run(handler.handle_text(message("Купил зонт за 500")))
    assert parse.called and not weather.called
    assert len(db.worksheet("Transactions").data)==2

def test_weather_llm_fallback(handler,db,monkeypatch):
    model(monkeypatch,handler,{"intent":"get_weather","weather_target":"tomorrow"})
    weather=AsyncMock(return_value="Завтра солнечно")
    monkeypatch.setattr(handler,"get_weather_forecast",weather)
    m=message("Что завтра надеть?")
    asyncio.run(handler.handle_text(m))
    weather.assert_awaited_once_with(target="tomorrow")
    assert "солнечно" in m.answer.call_args.args[0]

def test_pending_receipt_does_not_swallow_chat_or_new_purchase(handler,db,monkeypatch):
    set_pending("-100:11",[{"transaction_id":"R","amount":500}],"Влад")
    parse=model(monkeypatch,handler,{"intent":"chat","reply":"Привет! Как дела?"})
    asyncio.run(handler.handle_text(message("Как дела?")))
    assert has_pending("-100:11") and len(db.worksheet("Transactions").data)==1
    assert parse.call_args.kwargs["pending_receipt"]["transactions"][0]["transaction_id"]=="R"
    parse.return_value={"intent":"transaction","transaction":{"amount":1000},"reply":"Ок"}
    asyncio.run(handler.handle_text(message("Такси 1000",mid=70)))
    assert has_pending("-100:11") and len(db.worksheet("Transactions").data)==2

def test_receipt_comment_and_category(handler,db,monkeypatch):
    set_pending("-100:11",[{"transaction_id":"R","amount":500,"category":"Еда и продукты"}],"Влад")
    model(monkeypatch,handler,{"intent":"receipt_comment","comment":"обед на работе",
          "receipt_updates":[{"index":0,"category":"Кафе и перекус","necessity":"Want"}]})
    asyncio.run(handler.handle_text(message("Это обед на работе")))
    assert not has_pending("-100:11")
    assert "обед на работе" in db.worksheet("Transactions").data[1][14]

def test_category_draft_not_swallow_unrelated_words(handler,db,monkeypatch):
    set_clarification("-100:11",{"transaction_id":"C","amount":1000},
                      [{"label":"еда дома","category":"Еда и продукты"}])
    model(monkeypatch,handler,{"intent":"chat","reply":"Отдохни после работы"})
    asyncio.run(handler.handle_text(message("Сегодня работа утомила")))
    assert get_clarification("-100:11") and len(db.worksheet("Transactions").data)==1

@pytest.mark.parametrize("text",["Я должен сегодня работать","Я вернул товар в магазин","Обсудим, почему долги вызывают стресс"])
def test_debt_does_not_consume_unrelated_talk(handler,db,monkeypatch,text):
    parse=model(monkeypatch,handler,{"intent":"chat","reply":"Понимаю тебя."})
    asyncio.run(handler.handle_text(message(text)))
    assert parse.called and not debts.balances()

def test_group_delivery_both_one_message(db,monkeypatch):
    import config
    monkeypatch.setattr(config,"FAMILY_CHAT_ID",-100)
    monkeypatch.setattr(config,"VLAD_TELEGRAM_ID","11")
    monkeypatch.setattr(config,"DIANA_TELEGRAM_ID","22")
    seed_reminder(db,"Семья")
    bot=SimpleNamespace(send_message=AsyncMock())
    asyncio.run(reminders.deliver_due(bot,NOW))
    assert bot.send_message.call_count==1
    kw=bot.send_message.call_args.kwargs
    assert kw["chat_id"]==-100 and 'id=11' in kw["text"] and 'id=22' in kw["text"]
    assert kw["parse_mode"]=="HTML"

def test_group_html_escape_task(db,monkeypatch):
    import config
    monkeypatch.setattr(config,"FAMILY_CHAT_ID",-100)
    ws=seed_reminder(db)
    ws.update_cell(2,5,"Взять <документы> & крем")
    bot=SimpleNamespace(send_message=AsyncMock())
    asyncio.run(reminders.deliver_due(bot,NOW))
    assert "&lt;документы&gt; &amp;" in bot.send_message.call_args.kwargs["text"]

def test_recipient_task_mentions_us_not_both():
    assert reminders.extract_target("Напомни Диане завтра в 12 рассказать нам о школе","Влад")=="Диана"

def test_ai_reminder_multiple_times_confirm_and_correct_diana(handler,db,monkeypatch):
    now=now_astana()
    times=[(now+dt.timedelta(hours=h)).strftime("%Y-%m-%d %H:%M:%S") for h in (3,6)]
    model(monkeypatch,handler,{"intent":"add_reminder","reminder_target":"Диана",
                              "reminder_times":times,"reminder_text":"Написать Салтанат","recurrence":"daily"})
    asyncio.run(handler.handle_text(message("Пусть она вспомнит об этом в три и шесть")))
    assert not reminders.pending()
    asyncio.run(handler.handle_text(message("Да",mid=2)))
    items=reminders.pending()
    assert len(items)==2 and all(x["target_user"]=="Диана" and x["recurrence"]=="daily" for x in items)

def test_draft_keeps_target_after_question(handler,db,monkeypatch):
    model(monkeypatch,handler,{"intent":"add_reminder","reminder_target":"Диана","reminder_text":"Написать Салтанат",
                              "reminder_times":[],"clarification_question":"Когда Диане напомнить?"})
    asyncio.run(handler.handle_text(message("Напомни ей написать Салтанат")))
    asyncio.run(handler.handle_text(message("Завтра в 12.00",mid=2)))
    assert reminders.pending()[0]["target_user"]=="Диана"

def test_semantic_reminder_delete_requires_confirm(handler,db,monkeypatch):
    seed_reminder(db,"Диана")
    model(monkeypatch,handler,{"intent":"delete_reminder","reminder_ids":["REM_X"]})
    asyncio.run(handler.handle_text(message("Отмени ту задачу, что обсуждали")))
    assert len(reminders.pending())==1
    asyncio.run(handler.handle_text(message("Да",mid=2)))
    assert not reminders.pending()

def test_powerbi_additive_and_normalization_idempotent(db):
    from services.preflight import initialize_optional
    initialize_optional()
    from conftest import Sheet
    db.sheets["Dim_Categories"]=Sheet("Dim_Categories",[["category","default_limit","type"],["Транспорт",123,"РАСХОД"]],3)
    sheets.ensure_power_bi_dimension_table()
    sheets.ensure_power_bi_dimension_table()
    values=db.worksheet("Dim_Categories").data
    assert [row for row in values if row[0]=="Транспорт"]==[["Транспорт",123,"РАСХОД"]]
    assert len({row[0] for row in values})==len(values)
    tx=db.worksheet("Transactions")
    tx.append_row(["ID","2026-09-05","D","expense",100,"KZT"])
    reminders.worksheet()
    assert sheets.normalize_existing_family_table_values()["transactions_users"]==1
    assert tx.data[1][2:4]==["Диана","РАСХОД"]
    assert not any(sheets.normalize_existing_family_table_values().values())

def test_context_all_sources_and_200_plus_today(handler,db):
    from services.context import collect
    now=now_astana().strftime("%Y-%m-%d %H:%M:%S")
    ws=db.worksheet("Transactions")
    for i in range(205):
        ws.append_row([f"T{i}",now,"Влад","РАСХОД",100,"KZT"])
    data=asyncio.run(collect(message("Как дела?")))
    assert len(data["history"])==200 and len(data["today_transactions"])==205
    assert all(key in data for key in ("limits","reminders","shopping_list","trips","subscriptions","installments","debts","dialogue_state"))
    assert data["context_errors"]==[]

def test_context_failure_does_not_fake_empty(handler,db,monkeypatch):
    from services.context import collect
    def fail(): raise RuntimeError("quota")
    monkeypatch.setattr(sheets,"get_category_limits",fail)
    data=asyncio.run(collect(message("Поболтаем")))
    assert data["limits"] is None and data["context_errors"]==["limits"]
    assert data["history"]==[]

def test_deepseek_prompt_all_data_and_no_fixed_2026(monkeypatch):
    from services import deepseek_service as d
    completion=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content='{"intent":"chat","reply":"Привет"}'),finish_reason="stop")]))
    monkeypatch.setattr(d,"client",SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion))))
    asyncio.run(d.parse_and_analyze(user_text="А ей?",user_name="Влад",
        history=[],today_transactions=[],limits={},reminders=[],shopping_list=[],trips=[],
        installments=[],subscriptions=[],debts=[],chat_history=[{"sender":"Диана","text":"Нужно позвонить Салтанат"}],
        dialogue_state={"reminder_draft":{"target":"Диана"}},context_errors=["today_transactions"]))
    prompt=completion.call_args.kwargs["messages"][0]["content"]
    assert "Салтанат" in prompt and "СОСТОЯНИЕ ДИАЛОГА" in prompt and "ДОЛГИ" in prompt
    assert "Источник недоступен" in prompt
    assert "Текущий год: 2026" not in prompt
    assert "Рабочее время обычно с 10:00 до 19:00" in prompt
    assert "reply" in json.loads(completion.return_value.choices[0].message.content)

def test_successful_local_answers_persist_history(db,monkeypatch):
    from services.memory import get_chat_history
    from services.telegram_safe import safe_answer
    m=message("x")
    asyncio.run(safe_answer(m,"Поставила напоминание Диане в группу."))
    assert get_chat_history(-100)[-1]["text"].endswith("в группу.")

def test_memory_reload_survives_restart(monkeypatch):
    from services import memory
    from collections import defaultdict
    memory.add_chat_message(-100,"Влад","Помни диалог")
    memory._loaded=False
    memory._chat_history=defaultdict(list)
    assert memory.get_chat_history(-100)[0]["text"]=="Помни диалог"
    for i in range(60): memory.add_chat_message(-100,"Ада",str(i))
    assert len(memory.get_chat_history(-100))==50

def test_railway_config_and_worker_entry():
    root=Path(__file__).resolve().parents[1]
    cfg=json.loads((root/"railway.json").read_text())
    assert cfg["build"]["builder"]=="DOCKERFILE"
    assert cfg["deploy"]["startCommand"]=="python run.py"
    assert cfg["deploy"]["numReplicas"]==1
    assert "python:3.11" in (root/"Dockerfile").read_text()
    assert "healthcheckPath" not in cfg["deploy"]

def test_worksheet_cache_invalidated_on_write(db,monkeypatch):
    monkeypatch.setenv("SHEETS_READ_CACHE_SECONDS","60")
    ws=sheets._worksheet("Transactions")
    assert len(ws.get_all_values())==1
    sheets.append_transaction({"transaction_id":"CACHED","amount":100})
    assert len(ws.get_all_values())==2

def test_bank_source_consistency_and_instant_alias(handler,db):
    sheets.append_transaction({"transaction_id":"BANK","amount":100,"bank":"Kaspi","user":"Влад"})
    asyncio.run(handler.handle_text(message("Поменяй банк на халык")))
    row=db.worksheet("Transactions").data[1]
    assert row[6]=="Halyk" and row[7]=="Halyk Card"

def test_all_original_loops_and_init_retained():
    import ast
    source=(Path(__file__).resolve().parents[1]/"main.py").read_text()
    tree=ast.parse(source)
    functions={node.name for node in tree.body if isinstance(node,ast.AsyncFunctionDef)}
    loops={"check_reminders","check_subscriptions","weather_scheduler","finance_report_scheduler",
           "monthly_limits_scheduler","sweep_pending_receipts","sweep_clarifications"}
    assert loops<=functions
    main=next(node for node in tree.body if isinstance(node,ast.AsyncFunctionDef) and node.name=="main")
    names={n.id for n in ast.walk(main) if isinstance(n,ast.Name)}
    assert loops | {"ensure_power_bi_dimension_table","normalize_existing_family_table_values"} <= names

def test_reports_and_six_panel_chart_render(handler,db):
    from services.charts import generate_expense_chart, generate_trend_chart
    from services.reports import generate_pdf_report, generate_excel_export
    from services.analytics import analyze_budget_leaks
    sheets.append_transaction({"transaction_id":"REPORT1","amount":1000,"category":"Еда и продукты","necessity":"Need","user":"Влад"})
    sheets.append_transaction({"transaction_id":"REPORT2","amount":500,"category":"Транспорт","necessity":"Want","user":"Диана"})
    png=generate_expense_chart()
    assert png.startswith(b"\x89PNG")
    pdf=generate_pdf_report()
    assert pdf.startswith(b"%PDF")
    # PDF object dictionaries explicitly declare page count.
    assert b"/Count 2" in pdf
    xlsx=generate_excel_export()
    assert xlsx.startswith(b"PK")
    assert "text" in analyze_budget_leaks()
    trend=generate_trend_chart()
    assert trend.startswith(b"\x89PNG")

def test_limits_generation_kept(handler,db,monkeypatch):
    model(monkeypatch,handler,{"intent":"generate_limits"})
    generator=AsyncMock(return_value={"Транспорт":10000})
    monkeypatch.setattr(handler,"generate_limits_from_history",generator)
    m=message("Пересчитай лимиты")
    asyncio.run(handler.handle_text(m))
    assert generator.called and "10 000" in m.answer.call_args.args[0]

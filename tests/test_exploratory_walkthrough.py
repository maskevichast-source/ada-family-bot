import asyncio
from unittest.mock import AsyncMock
import pytest
from test_handlers import handler, message
from test_feature_matrix import model
from services import debts, state


# ── Долги: "одолжил X" без явного "у" - локальный парсер должен уступить ИИ ──

def test_debt_odolzhil_without_u_falls_through_to_ai_and_completes(handler, db, monkeypatch):
    parse = model(monkeypatch, handler, {
        "intent": "debt",
        "debt": {"event_type": "open", "direction": "lent", "counterparty": "Ануар", "amount": 1500, "currency": "KZT"},
    })
    asyncio.run(handler.handle_text(message("одолжил Ануару 1500")))
    key = state.dialogue_key(-100, 11)
    draft = state.get("debt_draft", key)
    assert draft is not None, "черновик долга не создался - ИИ-фолбэк не сработал"
    assert draft["payload"]["direction"] == "lent"
    assert draft["payload"]["counterparty"] == "Ануар"

    asyncio.run(handler.handle_text(message("да")))
    balances = debts.balances()
    assert len(balances) == 1
    assert balances[0]["direction"] == "lent"
    assert balances[0]["balance"] == 1500


def test_debt_zanyal_u_borrowed_handled_locally_without_ai(handler, db, monkeypatch):
    """'занял у Саши' - должно поймать локальным regex, ИИ вообще не звать."""
    parse = AsyncMock(side_effect=AssertionError("ИИ не должен был вызываться - это ловит локальный парсер"))
    monkeypatch.setattr(handler, "parse_and_analyze", parse)
    asyncio.run(handler.handle_text(message("занял у Саши 5000")))
    key = state.dialogue_key(-100, 11)
    draft = state.get("debt_draft", key)
    assert draft is not None
    assert draft["payload"]["direction"] == "borrowed"
    assert draft["payload"]["counterparty"] == "Саша"
    assert not parse.called


# ── Отслеживание цен: get/stop через полный ИИ-диспетчер (не через прямой перехват ссылки) ──

def test_get_price_tracking_intent_end_to_end_empty(handler, db, monkeypatch):
    model(monkeypatch, handler, {"intent": "get_price_tracking"})
    m = message("что я отслеживаю по ценам?")
    asyncio.run(handler.handle_text(m))
    reply = m.answer.call_args.args[0]
    assert "Пока ничего не отслеживаю" in reply
    assert "Kaspi" in reply  # подсказка, как начать


def test_get_price_tracking_intent_end_to_end_with_items(handler, db, monkeypatch):
    from services.sheets import add_price_tracking
    add_price_tracking({"user": "Влад", "url": "https://kaspi.kz/shop/p/-1/", "product_name": "Пылесос Deerma", "price": 45000, "target_price": 35000})
    model(monkeypatch, handler, {"intent": "get_price_tracking"})
    m = message("что я отслеживаю?")
    asyncio.run(handler.handle_text(m))
    reply = m.answer.call_args.args[0]
    assert "Пылесос Deerma" in reply
    assert "45" in reply.replace("\u2009", "").replace(" ", "")


def test_stop_price_tracking_intent_end_to_end(handler, db, monkeypatch):
    from services.sheets import add_price_tracking, get_user_price_trackings
    add_price_tracking({"user": "Влад", "url": "https://kaspi.kz/shop/p/-1/", "product_name": "Холодильник Bosch", "price": 300000})
    model(monkeypatch, handler, {"intent": "stop_price_tracking", "query": "холодильник"})
    asyncio.run(handler.handle_text(message("хватит следить за холодильником")))
    assert get_user_price_trackings("Влад") == []


def test_debt_lent_dal_stores_name_in_nominative_case(handler, db, monkeypatch):
    """'Дал Саше 1000' - имя извлекается в дательном падеже ('саше'),
    раньше сохранялось буквально как 'Саше' вместо 'Саша'."""
    asyncio.run(handler.handle_text(message("Дал Саше в долг 1000")))
    key = state.dialogue_key(-100, 11)
    draft = state.get("debt_draft", key)
    assert draft["payload"]["counterparty"] == "Саша"


def test_stop_price_tracking_not_found_gives_honest_answer(handler, db, monkeypatch):
    model(monkeypatch, handler, {"intent": "stop_price_tracking", "query": "чайник"})
    m = message("убери чайник из отслеживания")
    asyncio.run(handler.handle_text(m))
    reply = m.answer.call_args.args[0]
    assert "не нашла" in reply.lower()


# ── Прямой перехват ссылки + "следи за ценой" в одном сообщении (без ИИ вообще) ──

def test_link_plus_track_phrase_saves_tracking_without_ai(handler, db, monkeypatch):
    import handlers.text_handler as th
    parse = AsyncMock(side_effect=AssertionError("ИИ не должен вызываться - прямой перехват по ссылке"))
    monkeypatch.setattr(handler, "parse_and_analyze", parse)
    monkeypatch.setattr(th, "fetch_product_info", AsyncMock(return_value={
        "marketplace": "Kaspi", "title": "Пылесос Deerma", "price": 45000, "in_stock": True,
        "url": "https://kaspi.kz/shop/p/-1/", "image_url": "https://img.example/1.png",
    }))
    m = message("https://kaspi.kz/shop/p/-1/ следи за ценой, пока не упадёт до 35000")
    asyncio.run(handler.handle_text(m))
    reply = m.answer.call_args.args[0]
    assert "Буду следить" in reply
    assert "35" in reply.replace("\u2009", "").replace(" ", "")
    from services.sheets import get_user_price_trackings
    items = get_user_price_trackings("Влад")
    assert len(items) == 1
    assert items[0]["target_price"] == 35000
    assert not parse.called


def test_link_ozon_track_request_is_honest_about_limitation(handler, db, monkeypatch):
    import handlers.text_handler as th
    monkeypatch.setattr(th, "fetch_product_info", AsyncMock(return_value={
        "marketplace": "Ozon", "title": "Чайник Xiaomi", "price": 12000, "in_stock": True,
        "url": "https://ozon.kz/product/1",
    }))
    m = message("https://ozon.kz/t/abc123 следи за ценой")
    asyncio.run(handler.handle_text(m))
    reply = m.answer.call_args.args[0]
    assert "блокирует ботов" in reply
    from services.sheets import get_user_price_trackings
    assert get_user_price_trackings("Влад") == []  # не сохранили несуществующее отслеживание


# ── Живая реплика: не ломает обычный ответ, если DeepSeek недоступен ──

def test_anomaly_reflection_failure_does_not_break_normal_transaction_reply(handler, db, monkeypatch):
    import handlers.text_handler as th
    model(monkeypatch, handler, {
        "intent": "transaction", "reply": "Записала.",
        "transaction": {"amount": 99999, "category": "Кафе, рестораны и доставка еды",
                        "subcategory": "Кафе", "necessity": "Want", "user_comment": "Ужин", "type": "РАСХОД"},
    })
    monkeypatch.setattr(th, "generate_budget_reflection", AsyncMock(side_effect=RuntimeError("DeepSeek недоступен")))
    m = message("Ужин в ресторане 99999")
    # не должно кидать исключение наружу, несмотря на сбой generate_budget_reflection
    asyncio.run(handler.handle_text(m))
    assert m.answer.called
    reply = m.answer.call_args.args[0]
    assert "Записала" in reply

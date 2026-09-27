import asyncio
from unittest.mock import AsyncMock
from test_handlers import handler, message
from test_media_schedulers import media_message
from services import debts, state


def test_photo_debt_hint_creates_draft_not_transaction(handler, db, monkeypatch):
    from handlers import media_handler as mh
    parse = AsyncMock(return_value={
        "transactions": [],
        "reply": "",
        "debt_hint": {"direction": "lent", "counterparty": "Ануар", "amount": 1500},
    })
    monkeypatch.setattr(mh, "parse_receipt", parse)
    m = media_message("занял Ануару, перевел ему на халык")
    asyncio.run(mh.handle_media(m))

    assert len(db.worksheet("Transactions").data) == 1  # только заголовок, обычная трата не записана
    assert debts.balances() == []  # и в Debts тоже ещё не записано - ждёт подтверждения

    key = state.dialogue_key(m.chat.id, m.from_user.id)
    draft = state.get("debt_draft", key)
    assert draft is not None
    assert draft["payload"]["direction"] == "lent"
    assert draft["payload"]["counterparty"] == "Ануар"
    assert draft["payload"]["amount"] == 1500
    assert draft["payload"]["owner"] == "Влад"


def test_photo_debt_hint_confirmed_via_text_da_round_trip(handler, db, monkeypatch):
    """Черновик долга, заведённый с фото, подтверждается обычным текстовым
    'Да' через тот же путь, что и текстовый ввод долгов - state.dialogue_key
    общий для обоих обработчиков."""
    from handlers import media_handler as mh
    parse = AsyncMock(return_value={
        "transactions": [],
        "reply": "",
        "debt_hint": {"direction": "borrowed", "counterparty": "Саша", "amount": 5000},
    })
    monkeypatch.setattr(mh, "parse_receipt", parse)
    m = media_message("занял у Саши 5000")
    asyncio.run(mh.handle_media(m))
    assert debts.balances() == []

    asyncio.run(handler.handle_text(message("да")))

    balances = debts.balances()
    assert len(balances) == 1
    assert balances[0]["direction"] == "borrowed"
    assert balances[0]["counterparty"] == "Саша"
    assert balances[0]["balance"] == 5000
    assert len(db.worksheet("Transactions").data) == 1  # только заголовок, обычная трата так и не появилась


def test_photo_debt_hint_declined_via_text_net(handler, db, monkeypatch):
    from handlers import media_handler as mh
    parse = AsyncMock(return_value={
        "transactions": [],
        "reply": "",
        "debt_hint": {"direction": "lent", "counterparty": "Ануар", "amount": 1500},
    })
    monkeypatch.setattr(mh, "parse_receipt", parse)
    m = media_message("занял Ануару 1500")
    asyncio.run(mh.handle_media(m))

    asyncio.run(handler.handle_text(message("нет")))

    assert debts.balances() == []
    key = state.dialogue_key(m.chat.id, m.from_user.id)
    assert state.get("debt_draft", key) is None


def test_photo_without_debt_hint_still_records_normal_transaction(handler, db, monkeypatch):
    """Регресс-защита: обычные чеки без debt_hint работают как раньше."""
    from handlers import media_handler as mh
    parse = AsyncMock(return_value={
        "transactions": [{"amount": 1000, "user_comment": "Еда", "category": "Еда и продукты"}],
        "reply": "",
        "debt_hint": None,
    })
    monkeypatch.setattr(mh, "parse_receipt", parse)
    m = media_message("Купил продукты")
    asyncio.run(mh.handle_media(m))
    assert len(db.worksheet("Transactions").data) == 2

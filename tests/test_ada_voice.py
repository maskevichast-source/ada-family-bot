import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import ada_voice
from services.categories import HARMFUL_CATEGORY, TYPE_EXPENSE, TYPE_INCOME
from services.timezone import ASTANA_TZ
from test_handlers import handler, message  # noqa: F401
from test_feature_matrix import model

NOW = datetime.datetime(2026, 10, 4, 14, 30, tzinfo=ASTANA_TZ)


def h(days_ago=0.0, cat="Еда и продукты", amt=1000, user="Влад", comm="", sub="", hours_ago=None):
    dt = NOW.replace(tzinfo=None) - (datetime.timedelta(hours=hours_ago) if hours_ago is not None
                                     else datetime.timedelta(days=days_ago))
    return {"date": dt.strftime("%Y-%m-%d %H:%M:%S"), "user": user, "type": TYPE_EXPENSE, "amt": amt,
            "cat": cat, "subcat": sub, "comm": comm, "bank": "Kaspi", "nec": "Want"}


def tx(amount=1000, category="Еда и продукты", **kw):
    return {"type": TYPE_EXPENSE, "amount": amount, "category": category, "user": "Влад", "merchant": "", **kw}


# ───── повод считает код ─────

def test_no_occasion_for_ordinary_purchase():
    found = ada_voice.build_facts(tx(900), "Влад", [h(2, amt=800), h(3, amt=1100)], {}, NOW)
    assert found == {"facts": [], "strict": False}


def test_habit_counts_prior_week_only_for_this_user():
    hist = [h(1, HARMFUL_CATEGORY), h(2, HARMFUL_CATEGORY), h(3, HARMFUL_CATEGORY),
            h(10, HARMFUL_CATEGORY), h(1, HARMFUL_CATEGORY, user="Диана")]
    found = ada_voice.build_facts(tx(1300, HARMFUL_CATEGORY), "Влад", hist, {}, NOW)
    assert any("4-я" in f for f in found["facts"]) and not found["strict"]
    hist += [h(4, HARMFUL_CATEGORY), h(5, HARMFUL_CATEGORY)]
    assert ada_voice.build_facts(tx(1300, HARMFUL_CATEGORY), "Влад", hist, {}, NOW)["strict"]


def test_amount_spike_vs_own_median():
    hist = [h(d, "Транспорт и авто", 1500) for d in (2, 5, 9, 14, 20, 30)]
    found = ada_voice.build_facts(tx(6000, "Транспорт и авто"), "Влад", hist, {}, NOW)
    assert any("раз больше обычной" in f for f in found["facts"])
    # мало истории — не сравниваем
    assert ada_voice.build_facts(tx(6000, "Транспорт и авто"), "Влад", hist[:3], {}, NOW)["facts"] == []


def test_limit_exceeded_is_strict_and_warning_needs_pace():
    hist = [h(1, "Еда и продукты", 90000)]
    found = ada_voice.build_facts(tx(20000), "Влад", hist, {"Еда и продукты": 100000}, NOW)
    assert found["strict"] and any("превышен" in f for f in found["facts"])
    # 85% лимита, но месяц прошёл почти так же быстро (4 из 31 дня → 13%) — это уже опережение
    ok = ada_voice.build_facts(tx(5000), "Влад", [h(1, "Еда и продукты", 80000)], {"Еда и продукты": 100000}, NOW)
    assert any("85%" in f for f in ok["facts"]) and not ok["strict"]


def test_series_today_and_income():
    hist = [h(hours_ago=1, cat="Кафе, рестораны и доставка еды", amt=900, comm="кофе"),
            h(hours_ago=3, cat="Кафе, рестораны и доставка еды", amt=1100, comm="кофе")]
    found = ada_voice.build_facts(tx(1000, "Кафе, рестораны и доставка еды"), "Влад", hist, {}, NOW)
    assert any("3-я трата" in f for f in found["facts"])
    income = ada_voice.build_facts({"type": TYPE_INCOME, "amount": 250000, "category": "Зарплата"}, "Влад", [], {}, NOW)
    assert income["facts"] and not income["strict"]


def test_silence_tokens_and_cleaning():
    for token in ("", "-", "—", "…", "Нет", "молчу"):
        assert ada_voice._clean(token) == ""
    assert ada_voice._clean(' «Живая реплика» ') == "Живая реплика"


# ───── вызов модели ─────

def _fake_client(monkeypatch, behaviour):
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return behaviour(kwargs)

    from services import deepseek_service
    monkeypatch.setattr(deepseek_service, "client",
                        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    return calls


def _resp(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def test_call_disables_thinking_and_uses_voice_model(monkeypatch):
    calls = _fake_client(monkeypatch, lambda kw: _resp("Привет!"))
    assert asyncio.run(ada_voice._call("sys", "usr", max_tokens=50, timeout=5)) == "Привет!"
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert calls[0]["model"] == ada_voice._model()


def test_call_retries_without_extra_body_and_survives_failure(monkeypatch):
    def behaviour(kw):
        if "extra_body" in kw:
            raise RuntimeError("unsupported param")
        return _resp("Ок, тут я.")
    calls = _fake_client(monkeypatch, behaviour)
    assert asyncio.run(ada_voice._call("s", "u", max_tokens=50, timeout=5)) == "Ок, тут я."
    assert len(calls) == 2 and "extra_body" not in calls[1]

    def always_fail(kw): raise RuntimeError("down")
    _fake_client(monkeypatch, always_fail)
    assert asyncio.run(ada_voice._call("s", "u", max_tokens=50, timeout=5)) == ""


def test_comment_silent_without_occasion_and_calls_model_with_facts(monkeypatch):
    call = AsyncMock(return_value="Четвёртая пачка за неделю.")
    monkeypatch.setattr(ada_voice, "_call", call)
    monkeypatch.setattr(ada_voice, "now_astana", lambda: NOW)
    quiet = asyncio.run(ada_voice.comment_for_transaction(tx(900), "Влад", [], {}, []))
    assert quiet == "" and not call.called
    hist = [h(1, HARMFUL_CATEGORY), h(2, HARMFUL_CATEGORY), h(3, HARMFUL_CATEGORY)]
    chat = [{"sender": "Диана", "text": "ты куришь больше обычного"}]
    text = asyncio.run(ada_voice.comment_for_transaction(tx(1300, HARMFUL_CATEGORY), "Влад", hist, {}, chat))
    assert text == "Четвёртая пачка за неделю."
    system, user = call.call_args.args
    assert "4-я покупка" in user and "ты куришь больше обычного" in user
    assert "женского лица" in system and "ничего не записываешь" in system


def test_cooldown_blocks_second_ordinary_comment_but_not_strict(monkeypatch):
    call = AsyncMock(return_value="Реплика.")
    monkeypatch.setattr(ada_voice, "_call", call)
    monkeypatch.setattr(ada_voice, "now_astana", lambda: NOW)
    hist = [h(d, "Транспорт и авто", 1500) for d in (2, 5, 9, 14, 20, 30)]
    t = tx(6000, "Транспорт и авто")
    assert asyncio.run(ada_voice.comment_for_transaction(t, "Влад", hist, {}, [])) == "Реплика."
    assert asyncio.run(ada_voice.comment_for_transaction(t, "Влад", hist, {}, [])) == ""        # пауза
    strict_hist = [h(1, "Еда и продукты", 99000)]
    assert asyncio.run(ada_voice.comment_for_transaction(
        tx(5000), "Влад", strict_hist, {"Еда и продукты": 100000}, [])) == "Реплика."          # строгое — без паузы


def test_voice_off_returns_nothing_and_chat_falls_back(monkeypatch):
    monkeypatch.setenv("ADA_VOICE", "off")
    call = AsyncMock(return_value="x")
    monkeypatch.setattr(ada_voice, "_call", call)
    assert asyncio.run(ada_voice.comment_for_transaction(tx(1), "Влад", [], {}, [])) == ""
    assert asyncio.run(ada_voice.chat_reply("Влад", "привет", ["ctx"], fallback="старый")) == "старый"
    assert not call.called


def test_chat_reply_uses_context_and_falls_back_on_error(monkeypatch):
    call = AsyncMock(return_value="Да тут я, как ты?")
    monkeypatch.setattr(ada_voice, "_call", call)
    out = asyncio.run(ada_voice.chat_reply("Влад", "ты тут?", ["[ИСТОРИЯ ЧАТА]: Диана: привет"], fallback="Я на связи"))
    assert out == "Да тут я, как ты?"
    system, user = call.call_args.args
    assert "[ИСТОРИЯ ЧАТА]" in system and user == "Влад: ты тут?" and "не говори, что что-то сделала" in system
    call.return_value = ""
    assert asyncio.run(ada_voice.chat_reply("Влад", "ты тут?", ["ctx"], fallback="Я на связи")) == "Я на связи"
    assert asyncio.run(ada_voice.chat_reply("Влад", "ты тут?", None, fallback="Я на связи")) == "Я на связи"


# ───── в обработчике ─────

def test_handler_transaction_comment_only_with_occasion(handler, db, monkeypatch):
    from services.sheets import append_transaction
    for i in range(3):
        append_transaction({"transaction_id": f"H{i}", "amount": 1300, "bank": "BCC", "user": "Влад",
                            "category": HARMFUL_CATEGORY, "subcategory": "Сигареты и стики"})
    parsed = {"intent": "transaction", "reply": "Скучная фраза модели.",
              "transaction": {"amount": 1300, "category": HARMFUL_CATEGORY, "subcategory": "Сигареты и стики",
                              "type": "РАСХОД", "bank": "BCC"}}
    model(monkeypatch, handler, parsed)
    monkeypatch.setattr(ada_voice, "_call", AsyncMock(return_value="Четвёртая пачка за неделю, Влад."))
    m = message("пачка сигарет 1300")
    asyncio.run(handler.handle_text(m))
    report = m.answer.call_args.args[0]
    assert "Записано" in report and "💬 Четвёртая пачка за неделю, Влад." in report
    assert "Скучная фраза" not in report                      # дежурный ответ разбора больше не показывается


def test_handler_no_occasion_means_no_comment_line(handler, db, monkeypatch):
    model(monkeypatch, handler, {"intent": "transaction", "reply": "Скучная фраза модели.",
                                 "transaction": {"amount": 900, "category": "Еда и продукты",
                                                 "subcategory": "Напитки и вода", "type": "РАСХОД", "bank": "Kaspi"}})
    call = AsyncMock(return_value="не должно вызываться")
    monkeypatch.setattr(ada_voice, "_call", call)
    m = message("вода 900")
    asyncio.run(handler.handle_text(m))
    report = m.answer.call_args.args[0]
    assert "Записано" in report and "💬" not in report and not call.called
    assert db.worksheet("Transactions").data[-1][15] == "Скучная фраза модели."   # в таблице ai_comment остаётся


def test_handler_voice_failure_never_breaks_recording(handler, db, monkeypatch):
    from services.sheets import append_transaction
    for i in range(3):
        append_transaction({"transaction_id": f"H{i}", "amount": 1300, "bank": "BCC", "user": "Влад",
                            "category": HARMFUL_CATEGORY, "subcategory": "Сигареты и стики"})
    model(monkeypatch, handler, {"intent": "transaction", "reply": "x",
                                 "transaction": {"amount": 1300, "category": HARMFUL_CATEGORY,
                                                 "subcategory": "Сигареты и стики", "type": "РАСХОД", "bank": "BCC"}})
    monkeypatch.setattr(ada_voice, "_call", AsyncMock(side_effect=RuntimeError("boom")))
    m = message("пачка сигарет 1300")
    asyncio.run(handler.handle_text(m))
    assert "Записано" in m.answer.call_args.args[0]
    assert len(db.worksheet("Transactions").data) == 5


def test_handler_chat_without_context_keeps_parse_reply(handler, db, monkeypatch):
    model(monkeypatch, handler, {"intent": "chat", "reply": "Привет, я тут!"})
    m = message("как дела?")
    asyncio.run(handler.handle_text(m))
    assert m.answer.call_args.args[0] == "Привет, я тут!"

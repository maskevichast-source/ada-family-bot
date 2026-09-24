import datetime as dt
from services.timezone import ASTANA_TZ
from services.categories import HARMFUL_CATEGORY


def _seed_row(db, transaction_id, date, user, category, comm="", subcategory="", currency="KZT", amount=500):
    """Пишет строку транзакции напрямую в фейковый Sheets — в обход
    append_transaction (которая всегда подставляет now() в date и не даёт
    задать дату в прошлом, что нужно для проверки окна в 7 дней)."""
    row = [
        transaction_id, date, user, "РАСХОД", amount, currency,
        "Kaspi", "Kaspi Gold", "Собственные", "Карта", category,
        subcategory, "", "Want", comm, "",
    ]
    db.worksheet("Transactions").append_row(row)


def _fmt(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def test_count_recent_category_purchases_respects_window_user_and_category(db):
    from services.sheets import count_recent_category_purchases

    now = dt.datetime.now(ASTANA_TZ).replace(tzinfo=None)
    _seed_row(db, "T1", _fmt(now - dt.timedelta(days=1)), "Влад", HARMFUL_CATEGORY)
    _seed_row(db, "T2", _fmt(now - dt.timedelta(days=3)), "Влад", HARMFUL_CATEGORY)
    _seed_row(db, "T3", _fmt(now - dt.timedelta(days=6)), "Влад", HARMFUL_CATEGORY)
    # за окном 7 дней — не должна считаться
    _seed_row(db, "T4", _fmt(now - dt.timedelta(days=10)), "Влад", HARMFUL_CATEGORY)
    # другая категория — не считается
    _seed_row(db, "T5", _fmt(now - dt.timedelta(days=1)), "Влад", "Еда и продукты")
    # другой человек — не считается
    _seed_row(db, "T6", _fmt(now - dt.timedelta(days=1)), "Диана", HARMFUL_CATEGORY)

    assert count_recent_category_purchases("Влад", HARMFUL_CATEGORY, days=7) == 3
    assert count_recent_category_purchases("Диана", HARMFUL_CATEGORY, days=7) == 1
    assert count_recent_category_purchases("", HARMFUL_CATEGORY, days=7) == 0


def test_build_recent_context_transaction_history_bug_is_fixed(db, monkeypatch):
    """Раньше _build_recent_context читал t.get('category')/t.get('user_comment'),
    хотя get_last_200_transactions() отдаёт 'cat'/'comm' — история покупок в
    контекст НИКОГДА не попадала. Эта проверка ловит регресс, если баг вернётся."""
    from handlers import media_handler as mh

    monkeypatch.setattr(mh, "get_chat_history", lambda *a: [])
    now = dt.datetime.now(ASTANA_TZ).replace(tzinfo=None)
    _seed_row(db, "T1", _fmt(now - dt.timedelta(hours=1)), "Влад", "Еда и продукты", comm="маркер_теста_бага")

    context = mh._build_recent_context(chat_id=-100, user_name="Влад")
    assert "маркер_теста_бага" in context
    assert "Еда и продукты" in context


def test_build_recent_context_adds_harmful_fact_when_frequent(db, monkeypatch):
    from handlers import media_handler as mh

    monkeypatch.setattr(mh, "get_chat_history", lambda *a: [])
    now = dt.datetime.now(ASTANA_TZ).replace(tzinfo=None)
    _seed_row(db, "T1", _fmt(now - dt.timedelta(days=1)), "Влад", HARMFUL_CATEGORY, comm="сигареты")
    _seed_row(db, "T2", _fmt(now - dt.timedelta(days=2)), "Влад", HARMFUL_CATEGORY, comm="энергетик")

    context = mh._build_recent_context(chat_id=-100, user_name="Влад")
    assert "ФАКТ" in context
    assert "2 покупок" in context or "2 покупки" in context or "2 покупок(и)" in context


def test_build_recent_context_no_harmful_fact_when_single_purchase(db, monkeypatch):
    from handlers import media_handler as mh

    monkeypatch.setattr(mh, "get_chat_history", lambda *a: [])
    now = dt.datetime.now(ASTANA_TZ).replace(tzinfo=None)
    _seed_row(db, "T1", _fmt(now - dt.timedelta(days=1)), "Влад", HARMFUL_CATEGORY, comm="сигареты")

    context = mh._build_recent_context(chat_id=-100, user_name="Влад")
    assert "ФАКТ" not in context

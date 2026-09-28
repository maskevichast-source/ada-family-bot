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


def _seed_harmful(db, n, user="Влад"):
    now = dt.datetime.now(ASTANA_TZ).replace(tzinfo=None)
    for i in range(n):
        _seed_row(db, f"H{i}", _fmt(now - dt.timedelta(hours=i + 1)), user, HARMFUL_CATEGORY, comm="сигареты")


def test_habit_fact_added_when_frequent_and_text_matches(db, monkeypatch):
    from handlers import media_handler as mh

    monkeypatch.setattr(mh, "get_chat_history", lambda *a: [])
    _seed_harmful(db, 3)

    context = mh._build_recent_context(chat_id=-100, user_name="Влад", caption="Пачка сигарет")
    assert "ФАКТ" in context
    assert "3 таких покупок" in context


def test_habit_fact_not_repeated_within_cooldown(db, monkeypatch):
    """Ключевая защита от "заезженной пластинки": после замечания следующие
    72 часа факт в промпт вообще не попадает, как бы часто человек ни покупал."""
    from handlers import media_handler as mh

    monkeypatch.setattr(mh, "get_chat_history", lambda *a: [])
    _seed_harmful(db, 4)

    first = mh._build_recent_context(chat_id=-100, user_name="Влад", caption="Банка энергетика")
    second = mh._build_recent_context(chat_id=-100, user_name="Влад", caption="Пачка сигарет")
    assert "ФАКТ" in first
    assert "ФАКТ" not in second


def test_habit_fact_returns_after_cooldown_expires(db, monkeypatch):
    from services import state
    from services.analytics import habit_remark_due

    _seed_harmful(db, 3)
    assert habit_remark_due("Влад", "пачка сигарет") == 3
    assert habit_remark_due("Влад", "пачка сигарет") is None

    old = (dt.datetime.now(ASTANA_TZ) - dt.timedelta(hours=73)).isoformat()
    state.put("habit_remark", "Влад", {"at": old})
    assert habit_remark_due("Влад", "пачка сигарет") == 3


def test_unrelated_caption_does_not_burn_cooldown(db, monkeypatch):
    """Чек на хлеб не должен тратить паузу — иначе замечание пропадёт там,
    где оно действительно уместно (при следующей пачке сигарет)."""
    from services.analytics import habit_remark_due

    _seed_harmful(db, 3)
    assert habit_remark_due("Влад", "Хлеб и молоко") is None
    assert habit_remark_due("Влад", "") is None
    assert habit_remark_due("Влад", "пачка сигарет") == 3


def test_habit_fact_needs_three_prior_purchases(db, monkeypatch):
    from handlers import media_handler as mh

    monkeypatch.setattr(mh, "get_chat_history", lambda *a: [])
    _seed_harmful(db, 2)

    context = mh._build_recent_context(chat_id=-100, user_name="Влад", caption="Пачка сигарет")
    assert "ФАКТ" not in context


def test_habit_fact_is_per_user(db, monkeypatch):
    from services.analytics import habit_remark_due

    _seed_harmful(db, 3, user="Влад")
    assert habit_remark_due("Диана", "пачка сигарет") is None  # у Дианы нет истории
    assert habit_remark_due("Влад", "пачка сигарет") == 3

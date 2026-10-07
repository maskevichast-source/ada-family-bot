import asyncio
import datetime as dt
from unittest.mock import AsyncMock

from services import duplicates as dup, forecast as fc, state
from services.timezone import ASTANA_TZ, parse_flexible_datetime
from test_media_schedulers import StopTick, app, frozen, tick  # noqa: F401
from test_handlers import handler  # noqa: F401


def t(cat, amt, type="РАСХОД", date="2026-10-02 10:00:00", **kw):
    return {"cat": cat, "amt": amt, "type": type, "date": date, "merchant": kw.get("merchant", ""), "bank": kw.get("bank", ""),
            "user": kw.get("user", "Влад"), "transaction_id": kw.get("tid", "TRX_1")}


# ---------- прогноз ----------
def test_forecast_scales_variable_but_not_fixed_categories():
    now = dt.datetime(2026, 10, 10, 12, 0, tzinfo=ASTANA_TZ)
    f = fc.month_forecast(now, [t("Жильё и коммунальные услуги", 200000), t("Еда и продукты", 100000),
                                t("Доход", 500000, type="ДОХОД")])
    assert f["projected"]["Жильё и коммунальные услуги"] == 200000          # аренда не растягивается
    assert round(f["projected"]["Еда и продукты"]) == 310000                  # 100 000 за 10 дней → 31 день
    assert f["income"] == 500000 and round(f["projected_total"]) == 510000
    assert "510 000" in fc.forecast_line(f)
    assert fc.forecast_line(fc.month_forecast(now.replace(day=3), [t("Еда", 5000)])) is None   # рано для прогноза


def test_risky_categories_rules():
    now = dt.datetime(2026, 10, 10, 12, 0, tzinfo=ASTANA_TZ)
    f = fc.month_forecast(now, [t("Еда и продукты", 60000), t("Транспорт", 5000), t("Связь и подписки", 25000)])
    limits = {"Еда и продукты": 120000, "Транспорт": 50000, "Связь и подписки": 20000}
    risky = fc.risky_categories(f, limits)
    assert [r["cat"] for r in risky] == ["Еда и продукты"]                     # 60к*3.1=186к > 120к*1.15; постоянные — молчим
    assert "в таком темпе" in fc.warning_text(risky[0], f).lower()
    assert fc.risky_categories(fc.month_forecast(now.replace(day=5), [t("Еда и продукты", 60000)]), limits) == []
    already_over = fc.month_forecast(now, [t("Еда и продукты", 130000)])
    assert fc.risky_categories(already_over, limits) == []                      # уже превышено — это работа обычного предупреждения


def test_category_changes():
    text = fc.category_changes({"Еда": 150000, "Транспорт": 10000, "Кафе": 20000},
                               {"Еда": 100000, "Транспорт": 11000, "Одежда": 30000})
    assert "Еда: +50 000 тг, +50%" in text and "Кафе: +20 000 тг, раньше не было" in text
    assert "Одежда: −30 000 тг, −100%" in text and "Транспорт" not in text   # мелкое изменение не показываем
    assert fc.category_changes({"Еда": 100000}, {"Еда": 101000}) is None


def test_period_summary_includes_changes(app, monkeypatch):
    rows = {"2026-10-01": [t("Еда", 150000)], "2026-09-24": [t("Еда", 100000)]}
    monkeypatch.setattr(app, "get_transactions_for_period", lambda start, end: rows.get(start, []))
    text = app._period_summary_text("Тест", "2026-10-01", "2026-10-08", "2026-09-24", "2026-10-01")
    assert "Что выросло" in text and "Еда: +50 000 тг" in text


# ---------- дубли ----------
NEW = {"transaction_id": "TRX_20261007_100000_111111_5400", "date": "2026-10-07 10:00:00", "amount": 5400,
       "merchant": "Magnum", "bank": "Kaspi", "user": "Диана", "category": "Еда и продукты"}


def old(**kw):
    base = {"transaction_id": "TRX_20261007_090000_222222_5400", "date": "2026-10-07 09:30:00", "amt": 5400,
            "merchant": "Magnum Astana", "bank": "Kaspi", "user": "Влад", "type": "РАСХОД"}
    base.update(kw)
    return base


def test_duplicate_detection_rules():
    check = lambda o, n=NEW: dup.is_probable_duplicate(n, o, parse_flexible_datetime)
    assert check(old())
    assert check(old(merchant="", user="Влад"))                                  # разные люди, тот же банк
    assert not check(old(merchant="", user="Диана"))                             # один человек, места не видно — не спрашиваем
    assert not check(old(amt=5500))                                              # сумма другая
    assert not check(old(date="2026-10-05 09:30:00"))                            # позавчера
    assert not check(old(date="2026-10-07 09:29:59"))                            # больше 30 минут разницы
    assert not check(old(date="2026-10-07 22:00:00", user="Диана"))              # пачка сигарет утром и вечером — не дубль
    assert not check(old(date="2026-10-07 09:00:00", user="Влад"), dict(NEW, date="2026-10-07 21:30:00", user="Влад"))
    assert not check(old(type="ДОХОД"))
    assert not check(old(transaction_id=NEW["transaction_id"]))                  # сама запись
    assert not check(old(transaction_id="TRX_20261007_100000_111111_5400_1"))    # позиция того же чека
    assert not check(old(), dict(NEW, amount=900))                               # мелочь
    assert not check(old(merchant="Kaspi Gold"), dict(NEW, merchant="Magnum", bank="Не указан", user="Влад"))


def test_find_duplicate_uses_neighbor_days():
    seen = []
    def fetch(start, end):
        seen.append((start, end))
        return [old()]
    assert dup.find_duplicate(NEW, fetch, parse_flexible_datetime)["merchant"] == "Magnum Astana"
    assert seen == [("2026-10-06", "2026-10-09")]


def test_enqueue_rules(db):
    from services import sheets
    dup.PENDING.clear()
    sheets.append_transaction({"type": "РАСХОД", "amount": 5400, "merchant": "Magnum", "category": "Еда и продукты"})
    sheets.append_transaction({"type": "РАСХОД", "amount": 5400, "merchant": "Netflix", "user_comment": "Автосписание: Netflix"})
    sheets.append_transaction({"type": "ДОХОД", "amount": 900000, "merchant": "Работа"})
    assert len(dup.PENDING) == 1


def test_buttons_delete_or_keep(db):
    from services import sheets
    sheets.append_transaction({"type": "РАСХОД", "amount": 5400, "merchant": "Magnum", "category": "Еда", "transaction_id": "NEWTX"})
    offer = dup.suggest(dict(NEW, transaction_id="NEWTX"), old())
    assert "Похоже на дубль" in offer["text"] and "Magnum Astana" in offer["text"]
    kept = dup.apply_answer(offer["token"], False)
    assert kept["deleted"] == 0 and len(db.worksheet("Transactions").data) == 2
    assert dup.apply_answer(offer["token"], True) is None                        # токен одноразовый
    offer = dup.suggest(dict(NEW, transaction_id="NEWTX"), old())
    assert dup.apply_answer(offer["token"], True)["deleted"] == 1
    assert len(db.worksheet("Transactions").data) == 1


def test_duplicate_worker_offers_with_buttons(app, monkeypatch):
    dup.PENDING.clear()
    dup.enqueue(dict(NEW, type="РАСХОД"))
    monkeypatch.setattr(app, "get_transactions_for_period", lambda s, e: [old()])
    app.bot.send_message.reset_mock()
    tick(app.duplicate_worker)
    call = app.bot.send_message.call_args
    assert "Похоже на дубль" in call.kwargs["text"] and call.kwargs["reply_markup"] is not None


def test_forecast_warning_scheduler_once_per_category_per_month(app, monkeypatch):
    monkeypatch.setattr(app, "get_category_limits", lambda: {"Еда и продукты": 120000})
    monkeypatch.setattr(app, "get_transactions_for_period", lambda s, e: [t("Еда и продукты", 60000)])
    app.bot.send_message.reset_mock()
    frozen(monkeypatch, app, dt.datetime(2026, 10, 10, 19, 12, tzinfo=ASTANA_TZ))
    tick(app.forecast_warning_scheduler)
    frozen(monkeypatch, app, dt.datetime(2026, 10, 11, 19, 12, tzinfo=ASTANA_TZ))
    tick(app.forecast_warning_scheduler)                                          # на следующий день — молчим
    assert app.bot.send_message.call_count == 1
    assert "в таком темпе" in app.bot.send_message.call_args.kwargs["text"].lower()

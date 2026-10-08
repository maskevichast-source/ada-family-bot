import asyncio
import datetime as dt
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import alerts, backup_check, spending_query as sq, state, weather_alerts as wa
from services.timezone import ASTANA_TZ
from test_media_schedulers import StopTick, app, frozen, tick  # noqa: F401
from test_handlers import handler  # noqa: F401

TODAY = dt.date(2026, 10, 7)


def row(date, amt, cat="Еда и продукты", sub="Супермаркет", merchant="Magnum", bank="Kaspi", user="Влад", type="РАСХОД"):
    return {"date": date, "amt": amt, "cat": cat, "subcat": sub, "merchant": merchant, "bank": bank, "source": "",
            "user": user, "type": type, "comm": ""}


ROWS = [row("2026-09-03 10:00:00", 10000), row("2026-09-20 10:00:00", 5000, user="Диана"),
        row("2026-09-21 10:00:00", 4500, cat="Связь и подписки", sub="Мобильная связь и интернет", merchant="Beeline", bank="BCC"),
        row("2026-10-02 09:00:00", 4500, cat="Связь и подписки", sub="Мобильная связь и интернет", merchant="Beeline", bank="BCC"),
        row("2026-10-03 09:00:00", 700000, cat="Доход", sub="Зарплата", merchant="Работа", type="ДОХОД")]


def fetch(start, end):
    return [r for r in ROWS if start <= r["date"][:10] < end]


# ---------- 5. вопросы про траты ----------
def test_period_resolution():
    assert sq.resolve_period("last_month", TODAY)[:2] == (dt.date(2026, 9, 1), dt.date(2026, 10, 1))
    assert sq.resolve_period("2026-09", TODAY)[2] == "за сентябрь 2026"
    assert sq.resolve_period("yesterday", TODAY)[:2] == (dt.date(2026, 10, 6), TODAY)
    assert sq.resolve_period("this_week", TODAY)[0] == dt.date(2026, 10, 5)
    assert sq.resolve_period("2026-13", TODAY) is None and sq.resolve_period("абракадабра", TODAY) is None
    assert sq.resolve_period(None, TODAY)[2] == "за октябрь"


def test_sum_filters_and_not_found():
    out = sq.run({"kind": "sum", "period": "last_month", "category": "Еда"}, fetch, TODAY)
    assert "15 000 тг" in out and "2 операции" in out
    assert "5 000 тг" in sq.run({"kind": "sum", "period": "last_month", "category": "Еда", "user": "Диана"}, fetch, TODAY)
    assert "ничего не нашла" in sq.run({"kind": "sum", "period": "last_month", "merchant": "Netflix"}, fetch, TODAY)
    assert "ничего не нашла" in sq.run({"kind": "sum", "period": "this_month", "bank": "Kaspi"}, fetch, TODAY)


def test_last_top_breakdown_income():
    last = sq.run({"kind": "last", "period": "all", "merchant": "интернет"}, fetch, TODAY)
    assert "2 октября 2026" in last and "4 500 тг" in last
    top = sq.run({"kind": "top", "period": "all", "limit": 2}, fetch, TODAY)
    assert top.splitlines()[1].startswith("1. 10 000") and "700" not in top      # доход в топ трат не попадает
    breakdown = sq.run({"kind": "breakdown", "period": "all"}, fetch, TODAY)
    assert "Еда и продукты: 15 000 тг (62%)" in breakdown
    assert "700 000 тг" in sq.run({"kind": "sum", "period": "this_month", "type": "ДОХОД"}, fetch, TODAY)
    assert "Не поняла период" in sq.run({"period": "когда-нибудь"}, fetch, TODAY)


def test_handler_query_spending(handler, db, monkeypatch):
    from test_feature_matrix import model, message
    model(monkeypatch, handler, {"intent": "query_spending", "reply": "",
                                 "query": {"kind": "sum", "period": "2026-09", "category": "Еда"}})
    monkeypatch.setattr(handler, "get_transactions_for_period", fetch)
    m = message("Сколько потратили на еду в сентябре?")
    asyncio.run(handler.handle_text(m))
    assert "15 000 тг" in m.answer.call_args.args[0]


# ---------- 9. тревоги ----------
def test_alert_rate_limit_and_drain():
    alerts.PENDING.clear()
    assert alerts.report("k", "сломалось") is True
    assert alerts.report("k", "сломалось снова") is False                 # не чаще раза в час
    assert alerts.report("other", "другое") is True
    assert alerts.drain() == ["⚠️ сломалось", "⚠️ другое"] and alerts.drain() == []
    with state.connection() as conn:
        conn.execute("UPDATE state SET created=0, value='{\"at\": 0}' WHERE namespace='alert_sent'")
    assert alerts.report("k", "через час") is True


def test_restart_text_only_in_work_hours():
    assert alerts.restart_text(dt.datetime(2026, 10, 7, 14, 5)).startswith("Бот перезапустился в 14:05")
    assert alerts.restart_text(dt.datetime(2026, 10, 7, 3, 0)) is None


def test_failed_sheet_write_raises_alert(db, monkeypatch):
    from services import sheets
    alerts.PENDING.clear()
    monkeypatch.setattr(sheets, "_retry_write", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("quota")))
    with pytest.raises(RuntimeError):
        sheets.append_transaction({"type": "РАСХОД", "amount": 100, "merchant": "X", "category": "Прочее"})
    assert any("не удалось записать трату" in t.lower() for t in alerts.drain())


def test_alert_worker_sends_to_vlad_dm(app, monkeypatch):
    alerts.PENDING.clear()
    monkeypatch.setattr(app, "VLAD_TELEGRAM_ID", "777")
    app.bot.send_message.reset_mock()
    alerts.report("w", "Тест тревоги")
    tick(app.alert_worker)
    call = app.bot.send_message.call_args
    assert call.kwargs["chat_id"] == 777 and "Тест тревоги" in call.kwargs["text"]


def test_loop_error_raises_alert(app, monkeypatch):
    alerts.PENDING.clear()
    with state.connection() as conn:
        conn.execute("DELETE FROM state WHERE namespace='alert_sent'")
    frozen(monkeypatch, app, dt.datetime(2026, 10, 7, 10, 7, tzinfo=ASTANA_TZ))
    monkeypatch.setattr(app, "get_subscription_warnings", lambda *a: (_ for _ in ()).throw(RuntimeError("упало")))
    tick(app.check_subscriptions)
    assert any("Цикл подписок" in t and "упало" in t for t in alerts.drain())


# ---------- 8. проверка бэкапа ----------
def test_backup_check_compare():
    assert backup_check.compare(100, 102, ["Transactions", "Limits"])["ok"] is True
    assert "неполная" in backup_check.compare(50, 100, ["Transactions"])["reason"]
    assert "нет листа" in backup_check.compare(100, 100, ["Limits"])["reason"]
    assert "ПРОБЛЕМА" in backup_check.report_text({"ok": False, "reason": "х"})
    assert "всё в порядке" in backup_check.report_text(backup_check.compare(10, 10, ["Transactions"]))


def _xlsx_bytes(n):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Transactions"
    ws.append(["transaction_id", "date"])
    for i in range(n):
        ws.append([f"T{i}", "2026-10-01"])
    wb.create_sheet("Limits")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_verify_backup_with_real_xlsx(monkeypatch):
    from services import sheets
    live = [["transaction_id", "date"]] + [[f"T{i}", "x"] for i in range(7)]
    fake_db = SimpleNamespace(id="SRC", worksheet=lambda name: SimpleNamespace(get_all_values=lambda: live))
    monkeypatch.setattr(sheets, "get_db", lambda: fake_db)
    monkeypatch.setattr(sheets, "get_client", lambda: SimpleNamespace(export=lambda *_: _xlsx_bytes(7)))
    assert backup_check.verify_backup()["ok"] is True
    monkeypatch.setattr(sheets, "get_client", lambda: SimpleNamespace(export=lambda *_: _xlsx_bytes(1)))
    monkeypatch.setattr(sheets, "get_client", lambda: SimpleNamespace(export=lambda *_: b""))
    assert backup_check.verify_backup()["ok"] is False


def test_backup_check_scheduler_once_a_month(app, monkeypatch):
    monkeypatch.setattr(app, "VLAD_TELEGRAM_ID", "777")
    monkeypatch.setattr(backup_check, "verify_backup", lambda: {"ok": True, "exported": 10, "live": 10, "sheets": 5})
    app.bot.send_message.reset_mock()
    frozen(monkeypatch, app, dt.datetime(2026, 11, 1, 4, 12, tzinfo=ASTANA_TZ))
    tick(app.backup_check_scheduler)
    tick(app.backup_check_scheduler)
    assert app.bot.send_message.call_count == 1
    assert "всё в порядке" in app.bot.send_message.call_args.kwargs["text"]
    frozen(monkeypatch, app, dt.datetime(2026, 11, 2, 4, 12, tzinfo=ASTANA_TZ))
    tick(app.backup_check_scheduler)
    assert app.bot.send_message.call_count == 1


# ---------- 10. погодные предупреждения ----------
def pts(over=None):
    over = over or {}
    base = dt.datetime(2026, 10, 7, 14, 0)
    out = []
    for i in range(6):
        p = {"dt": base + dt.timedelta(hours=i), "temp": 10.0, "feels": 8.0, "rain": 0, "wind": 10.0, "code": 3, "spread": 0}
        p.update(over.get(i, {}))
        out.append(p)
    return out


def test_quiet_weather_has_no_events():
    assert wa.find_events(pts()) == []


def test_each_event_type_and_thresholds():
    kinds = lambda p: [k for k, _ in wa.find_events(p)]
    assert kinds(pts({2: {"code": 95}})) == ["storm"]
    assert kinds(pts({1: {"wind": 53.9}})) == []
    assert kinds(pts({1: {"wind": 54}})) == ["wind"]
    assert kinds(pts({3: {"temp": 20.0}, 4: {"temp": 20.0}, 5: {"temp": 20.0}})) == ["swing"]      # +10 за 3 часа
    assert kinds(pts({3: {"temp": 19.0}, 4: {"temp": 19.0}, 5: {"temp": 19.0}})) == []              # +9 — мало
    assert kinds(pts({2: {"code": 65}})) == ["heavy"]
    assert kinds(pts({2: {"code": 63}})) == []                                                   # обычный дождь — не сильный
    text = dict(wa.find_events(pts({2: {"code": 75}})))["heavy"]
    assert "снег" in text.lower() and "скользкой" in text


def test_active_hours():
    assert wa.in_active_hours(dt.datetime(2026, 10, 7, 7, 0)) and wa.in_active_hours(dt.datetime(2026, 10, 7, 22, 59))
    assert not wa.in_active_hours(dt.datetime(2026, 10, 7, 23, 0)) and not wa.in_active_hours(dt.datetime(2026, 10, 7, 6, 59))


def test_weather_alert_scheduler_sends_once_per_day(app, monkeypatch):
    calls = AsyncMock(return_value=[("storm", "⛈ гроза")])
    monkeypatch.setattr(wa, "collect", calls)
    app.bot.send_message.reset_mock()
    frozen(monkeypatch, app, dt.datetime(2026, 10, 7, 14, 12, tzinfo=ASTANA_TZ))
    tick(app.weather_alert_scheduler)
    tick(app.weather_alert_scheduler)                                      # тот же час — повторно не смотрим
    frozen(monkeypatch, app, dt.datetime(2026, 10, 7, 15, 12, tzinfo=ASTANA_TZ))
    tick(app.weather_alert_scheduler)                                      # следующий час, то же событие — молчим
    assert app.bot.send_message.call_count == 1
    frozen(monkeypatch, app, dt.datetime(2026, 10, 8, 15, 12, tzinfo=ASTANA_TZ))
    tick(app.weather_alert_scheduler)                                      # новый день — снова можно
    assert app.bot.send_message.call_count == 2
    frozen(monkeypatch, app, dt.datetime(2026, 10, 8, 23, 30, tzinfo=ASTANA_TZ))
    calls.reset_mock()
    tick(app.weather_alert_scheduler)                                      # ночью не смотрим вообще
    assert calls.call_count == 0


# ---------- починки: тревоги по временным сбоям, повтор напоминаний, откат подписки ----------
def test_transient_network_error_alerts_only_when_repeated():
    alerts.PENDING.clear()
    alerts._transient_hits.clear()
    with state.connection() as conn:
        conn.execute("DELETE FROM state WHERE namespace='alert_sent'")
    err = ConnectionError("('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))")
    assert alerts.is_transient(err) and not alerts.is_transient(KeyError("x"))
    assert alerts.report_error("rem", "Цикл напоминаний", err) is False
    assert alerts.report_error("rem", "Цикл напоминаний", err) is False
    assert alerts.report_error("rem", "Цикл напоминаний", err) is True        # третий раз за 20 минут — это уже серьёзно
    alerts._transient_hits.clear()
    assert alerts.report_error("other", "Другой", KeyError("боль")) is True    # обычная ошибка — сразу


def test_reminder_not_resent_when_sheet_update_failed(db, monkeypatch):
    from services import reminders
    sent = []

    class Bot:
        async def send_message(self, **kw):
            sent.append(kw)
    rows = [{"reminder_id": "R1", "remind_at": "2026-10-07 10:00:00", "target_user": "Семья", "text": "Тест",
             "recurrence": "once", "row_idx": 2}]
    monkeypatch.setattr(reminders, "pending", lambda: [dict(r) for r in rows])
    calls = {"n": 0}

    def flaky(r, when, now):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Google 503")
    monkeypatch.setattr(reminders, "_advance_after_send", flaky)
    now = dt.datetime(2026, 10, 7, 10, 5, tzinfo=ASTANA_TZ)
    asyncio.run(reminders.deliver_due(Bot(), now))
    asyncio.run(reminders.deliver_due(Bot(), now))           # следующая минута: таблица опять доступна
    assert len(sent) == 1 and calls["n"] == 2                 # сообщение ушло один раз, статус дообновили


def test_undo_reverts_subscription_mark(db):
    from services import sheets, subscriptions_auto as sa, undo
    sheets.add_or_update_subscription("Netflix", 4500, "Kaspi", 5)
    tx = {"merchant": "Netflix", "amount": 4500, "date": "2026-10-02 12:00:00", "category": "Связь и подписки",
          "subcategory": "Цифровые подписки и сервисы", "bank": "Kaspi", "type": "РАСХОД", "transaction_id": "TRXNF"}
    sheets.append_transaction(dict(tx, user_comment="Netflix"))
    assert sa.process_tx(tx)["kind"] == "paid"
    assert db.worksheet("Subscriptions").data[1][8] == "2026-10"
    assert sa.revert_for_ids(["TRXNF"]) == ["Netflix"]
    assert db.worksheet("Subscriptions").data[1][8] == "" and db.worksheet("Subscriptions").data[1][5] == ""
    sheets.delete_transactions_by_ids(["TRXNF"])                              # сама трата при отмене тоже удаляется
    assert sheets.process_due_subscriptions(dt.datetime(2026, 10, 5, 10, 0, tzinfo=ASTANA_TZ)) == ["Netflix"]


def test_operations_plural():
    from services.spending_query import _ops
    assert [_ops(n) for n in (1, 2, 5, 11, 21, 71, 112)] == ["1 операция", "2 операции", "5 операций", "11 операций",
                                                              "21 операция", "71 операция", "112 операций"]

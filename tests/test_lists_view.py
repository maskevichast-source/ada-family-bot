import asyncio
import datetime

import config
from aiohttp.test_utils import TestClient, TestServer

from services import debts, goals, lists_view, sheets, webapp
from services.timezone import ASTANA_TZ
from test_webapp import TOKEN, make_init_data

NOW = datetime.datetime(2026, 9, 29, 12, 0, tzinfo=ASTANA_TZ)


def _open_debt(debt_id, who, direction, amount, due=""):
    debts.record({"event_type": "open", "debt_id": debt_id, "owner": "Влад", "counterparty": who,
                  "direction": direction, "amount": amount, "currency": "KZT", "due_date": due,
                  "note": "на ремонт" if who == "Саша" else ""}, "E_" + debt_id)


def test_debts_sorted_by_due_with_totals_and_partial_repay(db):
    _open_debt("D1", "Саша", "borrowed", 100000, "2026-09-27")     # просрочен на 2 дня
    _open_debt("D2", "Ануар", "lent", 50000, "2026-10-05")
    _open_debt("D3", "Диана", "lent", 20000)                        # без срока
    debts.record({"event_type": "repay", "debt_id": "D2", "owner": "Влад", "amount": 15000}, "E_R1")
    sec = lists_view._debts(NOW)
    names = [d["counterparty"] for d in sec["items"]]
    assert names == ["Саша", "Ануар", "Диана"]                      # просрочка, ближайший срок, без срока
    sasha, anuar, _ = sec["items"]
    assert sasha["days_left"] == -2 and sasha["direction"] == "borrowed" and sasha["note"] == "на ремонт"
    assert anuar["balance"] == 35000 and anuar["amount"] == 50000 and anuar["due_date"] == "05.10.2026"
    assert sec["lent_total"] == 55000 and sec["borrowed_total"] == 100000


def test_closed_debts_are_hidden(db):
    _open_debt("D1", "Саша", "lent", 10000)
    debts.record({"event_type": "repay", "debt_id": "D1", "owner": "Влад", "amount": 10000}, "E_R")
    assert lists_view._debts(NOW)["items"] == []


def test_goals_progress_and_totals(db):
    goals.add_goal("Отпуск", 800000, "2026-12-31")
    goals.add_goal("Ноутбук", 300000)
    goals.deposit_to_goal("Отпуск", 200000)
    sec = lists_view._goals(NOW)
    vac = next(g for g in sec["items"] if g["name"] == "Отпуск")
    assert vac["pct"] == 25 and vac["remaining"] == 600000 and vac["deadline"] == "31.12.2026"
    assert vac["days_left"] == 93
    assert sec["saved_total"] == 200000


def test_next_payment_date_rules():
    d = datetime.date
    assert lists_view.next_payment_date(15, "", d(2026, 9, 10)) == d(2026, 9, 15)
    assert lists_view.next_payment_date(15, "2026-09-15", d(2026, 9, 20)) == d(2026, 10, 15)   # уже списано
    assert lists_view.next_payment_date(5, "", d(2026, 9, 10)) == d(2026, 9, 10)              # срок прошёл, спишется на проверке
    assert lists_view.next_payment_date(31, "2026-09", d(2026, 9, 30)) == d(2026, 10, 31)
    assert lists_view.next_payment_date(31, "2026-01", d(2026, 2, 10)) == d(2026, 2, 28)       # короткий месяц
    assert lists_view.next_payment_date(10, "2026-12", d(2026, 12, 20)) == d(2027, 1, 10)      # переход года


def test_subscriptions_sorted_and_month_total(db):
    sheets.add_or_update_subscription("Netflix", 4500, "Kaspi", 30)
    sheets.add_or_update_subscription("Спортзал", 20000, "Halyk", 1)
    sheets._get_or_create_subscriptions_sheet().update_cell(3, 6, "2026-09-01")   # в сентябре уже списано
    sec = lists_view._subscriptions(NOW)
    assert [s["name"] for s in sec["items"]] == ["Netflix", "Спортзал"]     # 30 сентября ближе, чем 1 октября
    assert sec["items"][0]["next_date"] == "30.09.2026" and sec["items"][0]["days_left"] == 1
    assert sec["items"][1]["next_date"] == "01.10.2026" and sec["items"][1]["days_left"] == 2
    assert sec["month_total"] == 24500
    # не списанная в срок подписка: спишется на ближайшей проверке, то есть «сегодня»
    sheets._get_or_create_subscriptions_sheet().update_cell(3, 6, "")
    assert lists_view._subscriptions(NOW)["items"][0]["name"] == "Спортзал"


def test_prices_only_safe_links_and_status_order(db):
    sheets.add_price_tracking({"user": "Влад", "url": "https://kaspi.kz/shop/p/-1/", "product_name": "Холодильник Bosch",
                               "price": 300000, "target_price": 250000})
    sheets.add_price_tracking({"user": "Влад", "url": "https://evil.example/x", "product_name": "Пылесос", "price": 45000})
    ws = sheets._get_or_create_price_tracking_sheet()
    sheets.record_price_check_success(2, 270000)                     # холодильник подешевел
    sheets.set_price_tracking_status(3, "reached")
    sec = lists_view._prices(NOW)
    fridge, vac = sec["items"]
    assert fridge["status"] == "active" and fridge["change_pct"] == -10.0 and fridge["target_price"] == 250000
    assert fridge["url"].startswith("https://kaspi.kz/")
    assert vac["status"] == "reached" and vac["url"] == ""           # чужой домен не отдаём
    sheets.set_price_tracking_status(2, "stopped")
    assert [p["name"] for p in lists_view._prices(NOW)["items"]] == ["Пылесос"]


def test_one_broken_section_does_not_break_the_rest(db, monkeypatch):
    def boom(now): raise RuntimeError("лист недоступен")
    monkeypatch.setitem(lists_view._SECTIONS, "goals", boom)
    data = lists_view.load_lists(NOW)
    assert data["goals"] == {"error": True, "items": []}
    assert data["debts"]["items"] == [] and "error" not in data["debts"]
    assert data["updated_at"] == "12:00"


def test_lists_cache_and_refresh_throttle():
    lists_view.reset_cache()
    calls, t = [], [100.0]
    loader = lambda: calls.append(1) or {"n": len(calls)}
    clock = lambda: t[0]
    lists_view.get_lists(loader=loader, clock=clock)
    t[0] += 30
    lists_view.get_lists(loader=loader, clock=clock)
    assert len(calls) == 1
    lists_view.get_lists(force=True, loader=loader, clock=clock)
    assert len(calls) == 2
    t[0] += 3
    lists_view.get_lists(force=True, loader=loader, clock=clock)      # слишком рано
    assert len(calls) == 2
    lists_view.reset_cache()


def test_lists_endpoint_auth_and_failure(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(config, "VLAD_TELEGRAM_ID", "111")
    seen = {}

    def fake(force=False):
        seen["force"] = force
        return {"debts": {"items": []}, "updated_at": "12:00"}

    monkeypatch.setattr(lists_view, "get_lists", fake)

    async def run():
        client = TestClient(TestServer(webapp.build_app()))
        await client.start_server()
        try:
            h = webapp.INIT_DATA_HEADER
            assert (await client.get("/api/lists")).status == 403
            assert (await client.get("/api/lists", headers={h: make_init_data(user_id=333)})).status == 403
            r = await client.get("/api/lists?refresh=1", headers={h: make_init_data(user_id=111)})
            assert r.status == 200 and (await r.json())["updated_at"] == "12:00" and seen["force"] is True

            def boom(force=False): raise RuntimeError("x")
            monkeypatch.setattr(lists_view, "get_lists", boom)
            r = await client.get("/api/lists", headers={h: make_init_data(user_id=111)})
            assert r.status == 503
        finally:
            await client.close()

    asyncio.run(run())

import asyncio
import datetime

import config
from aiohttp.test_utils import TestClient, TestServer

from services import dashboard, webapp
from services.timezone import ASTANA_TZ
from test_webapp import TOKEN, make_init_data

NOW = datetime.datetime(2026, 9, 15, 12, 0, tzinfo=ASTANA_TZ)


def tx(date, amt, cat="Еда и продукты", typ="РАСХОД"):
    return {"date": date, "amt": amt, "cat": cat, "type": typ}


TXS = [
    tx("2026-09-01 10:00:00", 30000),
    tx("2026-09-15 09:00:00", 20000, "Транспорт и авто"),      # сегодня — входит
    tx("2026-09-10 09:00:00", 10000, "Кафе, рестораны и доставка еды"),
    tx("2026-09-05 09:00:00", 500000, "Зарплата", "ДОХОД"),
    tx("2026-08-03 09:00:00", 24000),                            # прошлый месяц, в срок
    tx("2026-08-15 23:00:00", 16000, "Транспорт и авто"),        # 15 авг — ещё в срок
    tx("2026-08-20 09:00:00", 90000),                            # после 15-го — не входит
    tx("2026-08-10 09:00:00", 400000, "Зарплата", "ДОХОД"),
    {"date": "", "amt": 999, "cat": "X", "type": "РАСХОД"},      # без даты — пропуск
]
LIMITS = {"Еда и продукты": 32000, "Транспорт и авто": 15000, "Питомцы": 20000}


def test_totals_and_previous_period():
    d = dashboard.compute_dashboard(TXS, LIMITS, NOW)
    assert d["month_label"] == "Сентябрь 2026"
    assert d["expense"] == 60000 and d["income"] == 500000 and d["balance"] == 440000
    assert d["prev"]["label"] == "Август"
    assert d["prev"]["expense"] == 40000            # 24 000 + 16 000, без 20 августа
    assert d["prev"]["income"] == 400000
    assert d["prev"]["expense_delta_pct"] == 50     # 60 000 против 40 000
    assert d["operations"] == 4


def test_categories_sorted_with_limit_status():
    d = dashboard.compute_dashboard(TXS, LIMITS, NOW)
    names = [c["name"] for c in d["categories"]]
    assert names == ["Еда и продукты", "Транспорт и авто", "Кафе, рестораны и доставка еды"]
    food, transport, cafe = d["categories"]
    assert food["status"] == "warn" and food["limit_pct"] == 94      # 30 000 из 32 000
    assert transport["status"] == "over" and transport["limit_pct"] == 133
    assert cafe["status"] == "none" and cafe["limit"] is None
    assert food["share_pct"] == 50


def test_limit_status_edges():
    assert dashboard.limit_status(84, 100) == "ok"
    assert dashboard.limit_status(85, 100) == "warn"
    assert dashboard.limit_status(100, 100) == "warn"
    assert dashboard.limit_status(101, 100) == "over"
    assert dashboard.limit_status(50, None) == "none"
    assert dashboard.limit_status(50, 0) == "none"


def test_january_uses_december_and_empty_month():
    jan = datetime.datetime(2026, 1, 10, tzinfo=ASTANA_TZ)
    d = dashboard.compute_dashboard([tx("2025-12-05 10:00:00", 1000)], {}, jan)
    assert d["prev"]["label"] == "Декабрь" and d["prev"]["expense"] == 1000
    assert d["expense"] == 0 and d["categories"] == []
    assert d["prev"]["expense_delta_pct"] == -100
    empty = dashboard.compute_dashboard([], {}, NOW)
    assert empty["prev"]["expense_delta_pct"] is None


def test_cache_ttl_and_refresh_throttle():
    dashboard.reset_cache()
    calls = []
    t = [1000.0]
    data = {"operations": 1, "prev": {"expense": 1}}

    def loader():
        calls.append(1)
        return data

    clock = lambda: t[0]
    dashboard.get_dashboard(loader=loader, clock=clock)
    t[0] += 30
    dashboard.get_dashboard(loader=loader, clock=clock)          # из кэша
    assert len(calls) == 1
    dashboard.get_dashboard(force=True, loader=loader, clock=clock)  # свежий (30 с) — можно
    assert len(calls) == 2
    t[0] += 5
    dashboard.get_dashboard(force=True, loader=loader, clock=clock)  # слишком рано — из кэша
    assert len(calls) == 2
    t[0] += dashboard.CACHE_TTL_SECONDS
    dashboard.get_dashboard(loader=loader, clock=clock)          # протух
    assert len(calls) == 3
    dashboard.reset_cache()


def test_empty_result_gets_short_ttl():
    dashboard.reset_cache()
    empty = {"operations": 0, "prev": {"expense": 0}}
    dashboard.get_dashboard(loader=lambda: empty, clock=lambda: 0.0)
    assert dashboard._cache["ttl"] == dashboard.EMPTY_CACHE_TTL_SECONDS
    dashboard.reset_cache()


def test_dashboard_endpoint_auth_and_payload(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(config, "VLAD_TELEGRAM_ID", "111")
    monkeypatch.setattr(config, "DIANA_TELEGRAM_ID", "222")
    seen = {}

    def fake(force=False):
        seen["force"] = force
        return {"month_label": "Сентябрь 2026", "categories": []}

    monkeypatch.setattr(dashboard, "get_dashboard", fake)

    async def run():
        client = TestClient(TestServer(webapp.build_app()))
        await client.start_server()
        try:
            h = webapp.INIT_DATA_HEADER
            assert (await client.get("/api/dashboard")).status == 403
            r = await client.get("/api/dashboard", headers={h: make_init_data(user_id=333)})
            assert r.status == 403
            r = await client.get("/api/dashboard?refresh=1", headers={h: make_init_data(user_id=111)})
            assert r.status == 200 and (await r.json())["month_label"] == "Сентябрь 2026"
            assert seen["force"] is True
        finally:
            await client.close()

    asyncio.run(run())


def test_dashboard_endpoint_failure_is_503_not_crash(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(config, "VLAD_TELEGRAM_ID", "111")

    def boom(force=False):
        raise RuntimeError("sheets down")

    monkeypatch.setattr(dashboard, "get_dashboard", boom)

    async def run():
        client = TestClient(TestServer(webapp.build_app()))
        await client.start_server()
        try:
            r = await client.get("/api/dashboard", headers={webapp.INIT_DATA_HEADER: make_init_data(user_id=111)})
            assert r.status == 503
        finally:
            await client.close()

    asyncio.run(run())


def test_load_dashboard_end_to_end_on_fake_sheets(db):
    from conftest import Sheet
    from services.preflight import TRANSACTION_HEADERS

    def row(date, amount, category, typ="РАСХОД"):
        values = {"transaction_id": f"T{date}{amount}", "date": date, "user": "Влад", "type": typ,
                  "amount": amount, "currency": "KZT", "category": category}
        return [values.get(h, "") for h in TRANSACTION_HEADERS]

    ws = db.sheets["Transactions"]
    for r in (
        row("2026-09-02 10:00:00", 30000, "Еда и продукты"),
        row("2026-09-15 08:00:00", 20000, "Транспорт и авто"),   # сегодня
        row("2026-09-16 08:00:00", 77777, "Транспорт и авто"),   # завтра — не входит
        row("2026-09-05 10:00:00", 500000, "Зарплата", "ДОХОД"),
        row("2026-08-04 10:00:00", 40000, "Еда и продукты"),
        row("2026-08-25 10:00:00", 90000, "Еда и продукты"),     # после 15-го
    ):
        ws.append_row(r)
    db.sheets["Limits"] = Sheet("Limits", [["category", "limit_amount"], ["Еда и продукты", 32000]])

    d = dashboard.load_dashboard(NOW)
    assert d["expense"] == 50000 and d["income"] == 500000
    assert d["prev"]["expense"] == 40000
    food = d["categories"][0]
    assert food["name"] == "Еда и продукты" and food["status"] == "warn"

    # те же цифры, что видит бот: расходы месяца до сегодняшнего дня включительно
    from services.sheets import get_transactions_for_period
    txs = get_transactions_for_period("2026-09-01", "2026-09-16")
    assert sum(t["amt"] for t in txs if t["type"] == "РАСХОД") == d["expense"]

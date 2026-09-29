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


def _raw(n_tx=1):
    return {"transactions": [tx("2026-09-01 10:00:00", 1000)] * n_tx, "limits": {}, "now": NOW}


def test_cache_ttl_and_refresh_throttle():
    dashboard.reset_cache()
    calls = []
    t = [1000.0]

    def loader():
        calls.append(1)
        return _raw()

    clock = lambda: t[0]
    dashboard.get_dashboard(loader=loader, clock=clock)
    t[0] += 30
    dashboard.get_dashboard(loader=loader, clock=clock)              # из кэша
    dashboard.get_dashboard(months=6, person="Влад", loader=loader, clock=clock)  # другой вид — тот же кэш
    assert len(calls) == 1
    dashboard.get_dashboard(force=True, loader=loader, clock=clock)  # 30 с — можно обновить
    assert len(calls) == 2
    t[0] += 5
    dashboard.get_dashboard(force=True, loader=loader, clock=clock)  # слишком рано — из кэша
    assert len(calls) == 2
    t[0] += dashboard.CACHE_TTL_SECONDS
    dashboard.get_dashboard(loader=loader, clock=clock)              # протух
    assert len(calls) == 3
    dashboard.reset_cache()


def test_empty_result_gets_short_ttl():
    dashboard.reset_cache()
    dashboard.get_dashboard(loader=lambda: _raw(0), clock=lambda: 0.0)
    assert dashboard._cache["ttl"] == dashboard.EMPTY_CACHE_TTL_SECONDS
    dashboard.reset_cache()


def test_unknown_period_falls_back_to_month():
    dashboard.reset_cache()
    d = dashboard.get_dashboard(months=7, loader=_raw, clock=lambda: 0.0)
    assert d["period"] == 1
    dashboard.reset_cache()


def test_dashboard_endpoint_auth_and_payload(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(config, "VLAD_TELEGRAM_ID", "111")
    monkeypatch.setattr(config, "DIANA_TELEGRAM_ID", "222")
    seen = {}

    def fake(force=False, months=1, person=None):
        seen.update(force=force, months=months, person=person)
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
            body = await r.json()
            assert r.status == 200 and body["month_label"] == "Сентябрь 2026"
            assert seen == {"force": True, "months": 1, "person": None}
            assert body["viewer"] == "Влад" and body["other"] == "Диана"
            # «я» — тот, кто смотрит; «other» — второй член семьи
            await client.get("/api/dashboard?who=me&period=6", headers={h: make_init_data(user_id=222)})
            assert seen["person"] == "Диана" and seen["months"] == 6
            await client.get("/api/dashboard?who=other&period=12", headers={h: make_init_data(user_id=222)})
            assert seen["person"] == "Влад" and seen["months"] == 12
            # мусор в параметрах не ломает запрос
            r = await client.get("/api/dashboard?who=x&period=abc", headers={h: make_init_data(user_id=111)})
            assert r.status == 200 and seen["person"] is None and seen["months"] == 1
        finally:
            await client.close()

    asyncio.run(run())


def test_dashboard_endpoint_failure_is_503_not_crash(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(config, "VLAD_TELEGRAM_ID", "111")

    def boom(force=False, months=1, person=None):
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


def txu(date, amt, user, cat="Еда и продукты", text="", typ="РАСХОД"):
    return {"date": date, "amt": amt, "cat": cat, "type": typ, "user": user, "merchant": text}


MULTI = [
    txu("2026-09-10 10:00:00", 30000, "Влад", text="Magnum"),
    txu("2026-09-11 10:00:00", 50000, "Диана", "Красота и уход", "Салон"),
    txu("2026-08-05 10:00:00", 10000, "Влад", text="Small"),
    txu("2026-07-20 10:00:00", 70000, "Диана", "Одежда и обувь", "Zara"),
    txu("2026-04-02 10:00:00", 5000, "Влад"),
    txu("2026-03-02 10:00:00", 99999, "Влад"),                  # старше полугода
    txu("2025-11-02 10:00:00", 8000, "Диана"),                  # в пределах года
    txu("2025-09-30 10:00:00", 77777, "Влад"),                  # старше года
    txu("2026-09-05 10:00:00", 300000, "Влад", "Зарплата", typ="ДОХОД"),
]


def test_periods_are_calendar_months_including_current():
    m1 = dashboard.compute_dashboard(MULTI, {}, NOW, months=1)
    m3 = dashboard.compute_dashboard(MULTI, {}, NOW, months=3)
    m6 = dashboard.compute_dashboard(MULTI, {}, NOW, months=6)
    m12 = dashboard.compute_dashboard(MULTI, {}, NOW, months=12)
    assert m1["expense"] == 80000
    assert m3["expense"] == 160000               # июль + август + сентябрь
    assert m6["expense"] == 165000               # + апрель (5 000); март уже не входит
    assert m12["expense"] == 272999              # + март 2026 (99 999) и ноябрь 2025 (8 000); сентябрь 2025 не входит
    assert m3["month_label"] == "июл 2026 – сен 2026"
    assert m1["month_label"] == "Сентябрь 2026"


def test_person_filter_and_no_limits_for_person():
    limits = {"Еда и продукты": 20000}
    fam = dashboard.compute_dashboard(MULTI, limits, NOW, months=1)
    vlad = dashboard.compute_dashboard(MULTI, limits, NOW, months=1, person="Влад")
    diana = dashboard.compute_dashboard(MULTI, limits, NOW, months=1, person="Диана")
    assert (vlad["expense"], diana["expense"]) == (30000, 50000)
    assert vlad["income"] == 300000 and diana["income"] == 0
    assert vlad["who"] == "Влад" and fam["who"] == "family"
    assert fam["categories"][1]["limit"] == 20000                    # у семьи лимит есть
    assert vlad["categories"][0]["limit"] is None                    # у человека — нет
    assert vlad["categories"][0]["status"] == "none"


def test_limits_scale_with_period():
    limits = {"Еда и продукты": 20000}
    m3 = dashboard.compute_dashboard(MULTI, limits, NOW, months=3)
    food = next(c for c in m3["categories"] if c["name"] == "Еда и продукты")
    assert food["limit"] == 60000 and food["spent"] == 40000 and food["status"] == "ok"


def test_expenses_sorted_desc_with_details_and_cap():
    d = dashboard.compute_dashboard(MULTI, {}, NOW, months=3)
    amounts = [e["amount"] for e in d["expenses"]]
    assert amounts == sorted(amounts, reverse=True) == [70000, 50000, 30000, 10000]
    assert d["expenses"][0] == {"date": "20.07.2026", "amount": 70000, "category": "Одежда и обувь",
                                "text": "Zara", "user": "Диана"}
    assert d["expenses_total"] == 4
    many = [txu("2026-09-02 10:00:00", i + 1, "Влад") for i in range(dashboard.MAX_EXPENSE_ROWS + 25)]
    capped = dashboard.compute_dashboard(many, {}, NOW)
    assert len(capped["expenses"]) == dashboard.MAX_EXPENSE_ROWS
    assert capped["expenses_total"] == dashboard.MAX_EXPENSE_ROWS + 25
    assert capped["expenses"][0]["amount"] == dashboard.MAX_EXPENSE_ROWS + 25   # крупнейшие


def test_previous_period_comparison_for_three_months():
    txs = [
        txu("2026-09-02 10:00:00", 30000, "Влад"),
        txu("2026-06-10 10:00:00", 10000, "Влад"),      # предыдущий трёхмесячник (апр–июн)
        txu("2026-05-31 10:00:00", 10000, "Влад"),
    ]
    d = dashboard.compute_dashboard(txs, {}, NOW, months=3)
    assert d["prev"]["expense"] == 20000 and d["prev"]["expense_delta_pct"] == 50
    assert d["prev"]["phrase"] == "в предыдущие 3 мес."
    m = dashboard.compute_dashboard(txs, {}, NOW, months=1)
    assert m["prev"]["phrase"] == "в августе"


def test_january_boundaries_for_year_view():
    jan = datetime.datetime(2026, 1, 20, tzinfo=ASTANA_TZ)
    d = dashboard.compute_dashboard([txu("2025-02-03 10:00:00", 1000, "Влад"),
                                     txu("2025-01-31 10:00:00", 555, "Влад")], {}, jan, months=12)
    assert d["expense"] == 1000                       # фев 2025 … янв 2026 включительно
    assert d["month_label"] == "фев 2025 – янв 2026"


DYN = [
    txu("2026-09-01 10:00:00", 100, "Влад"),
    txu("2026-09-01 18:00:00", 50, "Диана"),
    txu("2026-09-03 10:00:00", 200, "Влад"),
    txu("2026-09-09 10:00:00", 70, "Влад"),
    txu("2026-09-15 09:00:00", 30, "Влад"),
    txu("2026-08-02 10:00:00", 40, "Влад"),
    txu("2026-08-31 10:00:00", 60, "Диана"),       # последний день прошлого месяца
    txu("2026-09-05 10:00:00", 999, "Влад", "Зарплата", typ="ДОХОД"),   # доход в расходах не участвует
]


def test_daily_dynamics_cumulative_and_previous_month_line():
    d = dashboard.compute_dashboard(DYN, {}, NOW, months=1)["dynamics"]
    assert d["kind"] == "daily" and d["days_in_month"] == 30
    assert len(d["cur"]) == 15                              # по сегодняшнее число
    assert d["cur"][0] == 150 and d["cur"][2] == 350 and d["cur"][8] == 420 and d["cur"][14] == 450
    assert d["cur"] == sorted(d["cur"])                     # накопление не убывает
    assert len(d["prev"]) == 31 and d["prev_label"] == "Август"
    assert d["prev"][1] == 40 and d["prev"][30] == 100      # 2 авг, и итог за август
    assert d["bars"] == [{"label": "1–7", "value": 350}, {"label": "8–14", "value": 70},
                         {"label": "15–21", "value": 30}]


def test_dynamics_respect_person_filter():
    d = dashboard.compute_dashboard(DYN, {}, NOW, months=1, person="Диана")["dynamics"]
    assert d["cur"][0] == 50 and d["cur"][-1] == 50
    assert d["prev"][-1] == 60


def test_monthly_bars_for_longer_periods():
    txs = [txu("2026-07-10 10:00:00", 10, "Влад"), txu("2026-09-10 10:00:00", 30, "Влад"),
           txu("2026-09-11 10:00:00", 5, "Влад")]
    d = dashboard.compute_dashboard(txs, {}, NOW, months=3)["dynamics"]
    assert d["kind"] == "monthly"
    assert d["bars"] == [{"label": "июл 26", "value": 10}, {"label": "авг", "value": 0},
                         {"label": "сен", "value": 35}]
    y = dashboard.compute_dashboard(txs, {}, NOW, months=12)["dynamics"]
    assert len(y["bars"]) == 12 and y["bars"][0]["label"] == "окт 25"
    assert [b["label"] for b in y["bars"]][3] == "янв 26"      # год меняется на январе
    assert sum(b["value"] for b in y["bars"]) == 45


def test_dynamics_empty_month():
    d = dashboard.compute_dashboard([], {}, NOW, months=1)["dynamics"]
    assert d["cur"] == [0.0] * 15 and d["bars"][0]["value"] == 0

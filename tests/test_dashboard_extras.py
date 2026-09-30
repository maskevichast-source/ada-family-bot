import asyncio
import datetime

import config
from aiohttp.test_utils import TestClient, TestServer

from services import dashboard, dashboard_alerts, loans_view, webapp
from services.timezone import ASTANA_TZ
from test_webapp import TOKEN, make_init_data

NOW = datetime.datetime(2026, 9, 29, 12, 0, tzinfo=ASTANA_TZ)


def t(date, amt, cat="Еда и продукты", user="Влад", merchant="", comm="", subcat="", typ="РАСХОД",
      source="Kaspi Gold", resource="Карта", bank="Kaspi"):
    return {"date": date, "amt": amt, "cat": cat, "user": user, "merchant": merchant, "comm": comm,
            "subcat": subcat, "type": typ, "source": source, "resource": resource, "bank": bank}


DATA = [
    t("2026-09-02 10:00:00", 30000, "Дом и быт", "Диана", "IdeaDecor", "шторы", "Текстиль и шторы"),
    t("2026-09-03 10:00:00", 20000, "Дом и быт", "Влад", "Ozon", "полки", "Товары для дома", source="Halyk Card", bank="Halyk"),
    t("2026-09-04 10:00:00", 16000, "Дом и быт", "Влад", "OZON", "органайзеры", "Товары для дома", resource="Наличные"),
    t("2026-09-05 10:00:00", 50000, "Еда и продукты", "Диана", "Magnum", "продукты на неделю", "Супермаркет и рынок"),
    t("2026-09-06 10:00:00", 480000, "Дом и быт", "Влад", "Есимбек Е.", "оплата прорабу", "Ремонт и стройматериалы", source=""),
    t("2026-09-07 10:00:00", 500000, "Зарплата", "Влад", typ="ДОХОД"),
    t("2026-08-04 10:00:00", 10000, "Дом и быт", "Диана", "IdeaDecor"),
]
LIMITS = {"Дом и быт": 300000, "Еда и продукты": 200000}


def full(**kw):
    return dashboard.compute_dashboard(DATA, LIMITS, NOW, **kw)


# ---------- разрезы ----------

def test_banks_split_cash_cards_and_unknown():
    banks = {b["name"]: b for b in full()["banks"]}
    assert banks["Наличные"]["spent"] == 16000
    assert banks["Halyk Card"]["spent"] == 20000
    assert banks["Kaspi Gold"]["spent"] == 80000 and banks["Kaspi Gold"]["count"] == 2
    assert banks["Kaspi"]["spent"] == 480000                       # source пуст -> банк
    assert [b["name"] for b in full()["banks"]][0] == "Kaspi"      # по убыванию суммы


def test_merchants_merge_spelling_and_are_capped():
    m = {x["name"]: x for x in full()["merchants"]}
    assert m["Ozon"]["spent"] == 36000 and m["Ozon"]["count"] == 2      # Ozon и OZON — один магазин
    many = [t("2026-09-02 10:00:00", i + 1, merchant=f"Shop{i}") for i in range(dashboard.MAX_MERCHANTS + 5)]
    assert len(dashboard.compute_dashboard(many, {}, NOW)["merchants"]) == dashboard.MAX_MERCHANTS


def test_people_block_only_in_family_view():
    fam = full()
    by = {p["name"]: p for p in fam["people"]}
    assert by["Влад"]["expense"] == 516000 and by["Диана"]["expense"] == 80000
    assert by["Влад"]["income"] == 500000 and by["Диана"]["income"] == 0
    assert by["Влад"]["share_pct"] == 87 and by["Диана"]["share_pct"] == 13
    assert by["Диана"]["top"][0] == {"category": "Еда и продукты", "spent": 50000}
    assert full(person="Влад")["people"] == []


# ---------- детализация категории ----------

def test_category_detail_scopes_everything_to_that_category():
    d = full(category="Дом и быт")
    assert d["category"] == "Дом и быт"
    assert d["expense"] == 546000 and d["income"] == 0             # доходы в детализации не участвуют
    assert [c["name"] for c in d["categories"]] == ["Дом и быт"]
    subs = {s["name"]: s for s in d["subcats"]}
    assert subs["Ремонт и стройматериалы"]["spent"] == 480000 and subs["Текстиль и шторы"]["count"] == 1
    assert d["prev"]["expense"] == 10000                            # август только этой категории
    assert d["category_limit"]["status"] == "over" and d["category_limit"]["limit"] == 300000
    assert all(e["category"] == "Дом и быт" for e in d["expenses"])
    assert full()["subcats"] == [] and full()["category_limit"] is None
    assert full(category="Дом и быт", person="Влад")["category_limit"] is None      # лимит — на семью


# ---------- поиск ----------

def test_search_by_text_note_and_amount_does_not_change_totals():
    base = full()
    by_shop = full(query="ozon")
    assert by_shop["search"] == {"q": "ozon", "count": 2, "sum": 36000}
    assert [e["amount"] for e in by_shop["expenses"]] == [20000, 16000]
    assert by_shop["expense"] == base["expense"]                                 # итоги периода прежние
    assert full(query="прорабу")["search"]["count"] == 1                        # по заметке
    assert full(query="480")["expenses"][0]["amount"] == 480000                 # по сумме (часть числа)
    assert full(query="текстиль")["search"]["count"] == 1                       # по подкатегории
    assert full(query="ничего такого")["search"] == {"q": "ничего такого", "count": 0, "sum": 0}
    assert base["search"] is None
    assert len(full(query="x" * 500)["search"]["q"]) == dashboard.MAX_QUERY_CHARS


# ---------- «Внимание» ----------

def test_alerts_limits_sorted_over_first():
    alerts = dashboard_alerts.compute_alerts(DATA, LIMITS, NOW)
    limit_alerts = [a for a in alerts if a["type"] == "limit"]
    assert [a["category"] for a in limit_alerts] == ["Дом и быт"] and limit_alerts[0]["status"] == "over"
    assert limit_alerts[0]["pct"] == 182
    assert dashboard_alerts.compute_alerts(DATA, {}, NOW) == []       # без лимитов и без истории — тихо


def test_alerts_big_purchase_needs_history_and_recent_date():
    hist = [t(f"2026-08-{d:02d} 10:00:00", 1000, "Кафе, рестораны и доставка еды") for d in (5, 10, 15)]
    recent = t("2026-09-28 20:00:00", 5000, "Кафе, рестораны и доставка еды", merchant="Ресторан")
    alerts = dashboard_alerts.compute_alerts(hist + [recent], {}, NOW)
    big = [a for a in alerts if a["type"] == "big"]
    assert len(big) == 1 and big[0]["amount"] == 5000 and big[0]["prev_max"] == 1000 and big[0]["text"] == "Ресторан"
    old = t("2026-09-10 20:00:00", 5000, "Кафе, рестораны и доставка еды")     # не за последнюю неделю
    assert [a for a in dashboard_alerts.compute_alerts(hist + [old], {}, NOW) if a["type"] == "big"] == []
    few = hist[:2] + [recent]                                                  # прошлых покупок меньше трёх
    assert [a for a in dashboard_alerts.compute_alerts(few, {}, NOW) if a["type"] == "big"] == []
    other_user = [dict(h, user="Диана") for h in hist] + [recent]              # история у другого человека
    assert [a for a in dashboard_alerts.compute_alerts(other_user, {}, NOW) if a["type"] == "big"] == []


def test_alerts_pace_only_for_categories_without_limit():
    def month(m, amount):
        return t(f"2026-{m:02d}-10 10:00:00", amount, "Развлечения и хобби")
    hist = [month(6, 10000), month(7, 10000), month(8, 10000)]
    now_spend = t("2026-09-15 10:00:00", 30000, "Развлечения и хобби")
    pace = [a for a in dashboard_alerts.compute_alerts(hist + [now_spend], {}, NOW) if a["type"] == "pace"]
    assert len(pace) == 1 and pace[0]["ratio"] == 3.0 and pace[0]["usual"] == 10000
    with_limit = dashboard_alerts.compute_alerts(hist + [now_spend], {"Развлечения и хобби": 100000}, NOW)
    assert [a for a in with_limit if a["type"] == "pace"] == []                # у категорий с лимитом — сигнал по лимиту
    assert [a for a in dashboard_alerts.compute_alerts(hist[:1] + [now_spend], {}, NOW) if a["type"] == "pace"] == []


def test_get_dashboard_adds_alerts_only_outside_category_view():
    dashboard.reset_cache()
    loader = lambda: {"transactions": DATA, "limits": LIMITS, "now": NOW}
    d = dashboard.get_dashboard(loader=loader, clock=lambda: 0.0)
    assert d["alerts"] and d["alerts"][0]["type"] == "limit"
    assert "alerts" not in dashboard.get_dashboard(category="Дом и быт", loader=loader, clock=lambda: 0.0)
    dashboard.reset_cache()


# ---------- кредитная нагрузка ----------

LOANS = [
    t("2026-09-08 10:00:00", 151304, "Электроника и техника", merchant="Погашение кредита Forte"),
    t("2026-09-11 10:00:00", 131347, "Финансовые расходы и переводы", merchant="По номеру телефона",
      comm="Погашение кредитной задолженности по карте ozen"),
    t("2026-09-25 10:00:00", 34450, "Финансовые расходы и переводы", merchant="Kaspi Red"),
    t("2026-08-08 10:00:00", 151304, "Электроника и техника", merchant="Погашение кредита Forte"),
    t("2026-09-05 10:00:00", 1119018, "Зарплата", typ="ДОХОД"),
    t("2026-09-06 10:00:00", 90000, "Еда и продукты", merchant="Magnum"),            # не кредит
    t("2026-09-07 10:00:00", 999, "Финансовые расходы и переводы", comm="погашение долга", typ="ДОХОД"),   # доход — не считаем
]


def test_loans_detects_payments_groups_payees_and_share_of_income():
    d = loans_view.compute_loans(LOANS, NOW)
    assert [m["label"] for m in d["months"]] == ["Июль", "Август", "Сентябрь"]
    sep = d["months"][-1]
    assert sep["total"] == 317101 and sep["count"] == 3 and sep["income"] == 1119018 + 999
    assert sep["share_pct"] == 28
    assert d["months"][1]["total"] == 151304 and d["months"][0]["total"] == 0
    forte = next(p for p in d["payees"] if p["name"] == "Погашение кредита Forte")
    assert forte["amount"] == 151304 and forte["months_seen"] == 2
    assert d["payees"][0]["name"] == "Погашение кредита Forte"                      # по убыванию
    assert "эвристика" not in d["note"] and "кредит" in d["note"]


def test_loans_share_is_none_without_income_and_no_false_positives():
    d = loans_view.compute_loans([t("2026-09-08 10:00:00", 5000, merchant="Погашение кредита")], NOW)
    assert d["months"][-1]["share_pct"] is None
    plain = loans_view.compute_loans([t("2026-09-08 10:00:00", 5000, merchant="Magnum")], NOW)
    assert plain["payees"] == [] and plain["months"][-1]["total"] == 0


# ---------- эндпоинты ----------

def test_endpoints_pass_category_query_and_serve_loans(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(config, "VLAD_TELEGRAM_ID", "111")
    seen = {}

    def fake(force=False, months=1, person=None, category=None, query=None):
        seen.update(category=category, query=query)
        return {"month_label": "x"}

    monkeypatch.setattr(dashboard, "get_dashboard", fake)
    monkeypatch.setattr(dashboard, "get_raw", lambda force=False: {"transactions": LOANS, "limits": {}, "now": NOW})

    async def run():
        client = TestClient(TestServer(webapp.build_app()))
        await client.start_server()
        try:
            h = {webapp.INIT_DATA_HEADER: make_init_data(user_id=111)}
            await client.get("/api/dashboard?cat=Дом и быт&q=ozon", headers=h)
            assert seen == {"category": "Дом и быт", "query": "ozon"}
            await client.get("/api/dashboard", headers=h)
            assert seen == {"category": None, "query": None}
            assert (await client.get("/api/loans")).status == 403
            r = await client.get("/api/loans", headers=h)
            assert r.status == 200 and (await r.json())["months"][-1]["total"] == 317101
            monkeypatch.setattr(dashboard, "get_raw", lambda force=False: 1 / 0)
            assert (await client.get("/api/loans", headers=h)).status == 503
        finally:
            await client.close()

    asyncio.run(run())

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from test_handlers import handler
from test_media_schedulers import app, frozen, tick, StopTick
from services.timezone import ASTANA_TZ
from services import sheets
import config

VLAD_ID, DIANA_ID = 11, 22


@pytest.fixture(autouse=True)
def family_ids(monkeypatch):
    monkeypatch.setattr(config, "VLAD_TELEGRAM_ID", str(VLAD_ID))
    monkeypatch.setattr(config, "DIANA_TELEGRAM_ID", str(DIANA_ID))


def _seed_tracking_row(db, **kwargs):
    defaults = dict(
        id="PRICE_1", user="Влад", url="https://kaspi.kz/shop/p/-123/",
        product_name="Тестовый товар", image_url="https://img.example/1.png",
        target_price="", first_price=10000, last_price=10000,
        last_checked_at="2026-09-01 10:00:00", fail_count=0,
        status="active", created_at="2026-09-01 10:00:00",
    )
    defaults.update(kwargs)
    row = [defaults.get(h, "") for h in sheets.PRICE_TRACKING_HEADERS]
    db.worksheet("PriceTracking").append_row(row)


def test_add_and_get_price_tracking(db):
    saved = sheets.add_price_tracking({
        "user": "Влад", "url": "https://kaspi.kz/shop/p/-1/", "product_name": "Пылесос",
        "image_url": "https://img/1.png", "price": 50000, "target_price": 40000,
    })
    assert saved["first_price"] == 50000
    items = sheets.get_user_price_trackings("Влад")
    assert len(items) == 1
    assert items[0]["product_name"] == "Пылесос"
    assert items[0]["target_price"] == 40000


def test_stop_price_tracking_by_query(db):
    sheets.add_price_tracking({"user": "Влад", "url": "https://kaspi.kz/shop/p/-1/", "product_name": "Холодильник Bosch", "price": 300000})
    assert sheets.stop_price_tracking("холодильник") is not None
    assert sheets.get_user_price_trackings("Влад") == []


def test_price_drop_sends_text_notification_when_no_send_photo(app, monkeypatch, db):
    now = dt.datetime(2026, 9, 20, 10, 0, tzinfo=ASTANA_TZ)
    frozen(monkeypatch, app, now)
    _seed_tracking_row(db, first_price=10000, last_price=10000,
                        last_checked_at="2026-09-20 06:00:00")  # 4ч назад - пора проверять
    monkeypatch.setattr(app, "fetch_product_info", AsyncMock(return_value={
        "price": 8000, "image_url": "https://img.example/1.png", "url": "https://kaspi.kz/shop/p/-123/",
    }))
    tick(app.check_price_tracking)
    assert app.bot.send_message.called
    text = app.bot.send_message.call_args.kwargs.get("text") or app.bot.send_message.call_args.args[-1]
    assert "Цена упала" in text and "**" not in text
    assert app.bot.send_message.call_args.kwargs["chat_id"] == VLAD_ID          # в личку, не в группу
    row = sheets.get_active_price_trackings()[0]
    assert int(row["last_price"]) == 8000 and int(row["notified_price"]) == 8000


def test_price_target_reached_marks_status(app, monkeypatch, db):
    now = dt.datetime(2026, 9, 20, 10, 0, tzinfo=ASTANA_TZ)
    frozen(monkeypatch, app, now)
    _seed_tracking_row(db, first_price=10000, last_price=10000, target_price=9000,
                        last_checked_at="2026-09-20 06:00:00")
    monkeypatch.setattr(app, "fetch_product_info", AsyncMock(return_value={
        "price": 8500, "image_url": "", "url": "https://kaspi.kz/shop/p/-123/",
    }))
    tick(app.check_price_tracking)
    text = app.bot.send_message.call_args.kwargs.get("text") or app.bot.send_message.call_args.args[-1]
    assert "Достигнута цель" in text and "9 000" in text
    assert app.bot.send_message.call_args.kwargs["chat_id"] == VLAD_ID
    row = sheets.get_active_price_trackings()
    assert row == []  # стал "reached", больше не active


def test_ordinary_price_no_drop_updates_silently(app, monkeypatch, db):
    now = dt.datetime(2026, 9, 20, 10, 0, tzinfo=ASTANA_TZ)
    frozen(monkeypatch, app, now)
    _seed_tracking_row(db, first_price=10000, last_price=10000,
                        last_checked_at="2026-09-20 06:00:00")
    monkeypatch.setattr(app, "fetch_product_info", AsyncMock(return_value={
        "price": 10500, "image_url": "", "url": "https://kaspi.kz/shop/p/-123/",
    }))
    tick(app.check_price_tracking)
    assert not app.bot.send_message.called
    row = sheets.get_active_price_trackings()[0]
    assert int(row["last_price"]) == 10500


def test_recently_checked_item_is_skipped(app, monkeypatch, db):
    now = dt.datetime(2026, 9, 20, 10, 0, tzinfo=ASTANA_TZ)
    frozen(monkeypatch, app, now)
    _seed_tracking_row(db, last_checked_at="2026-09-20 09:00:00")  # всего час назад
    fetch = AsyncMock(return_value={"price": 1, "image_url": "", "url": "x"})
    monkeypatch.setattr(app, "fetch_product_info", fetch)
    tick(app.check_price_tracking)
    assert not fetch.called
    assert not app.bot.send_message.called


def test_three_failures_marks_broken_and_notifies_once(app, monkeypatch, db):
    now = dt.datetime(2026, 9, 20, 10, 0, tzinfo=ASTANA_TZ)
    frozen(monkeypatch, app, now)
    _seed_tracking_row(db, fail_count=2, last_checked_at="2026-09-20 06:00:00")
    monkeypatch.setattr(app, "fetch_product_info", AsyncMock(return_value=None))
    tick(app.check_price_tracking)
    assert app.bot.send_message.called
    text = app.bot.send_message.call_args.kwargs.get("text") or app.bot.send_message.call_args.args[-1]
    assert "Отслеживание остановлено" in text
    assert app.bot.send_message.call_args.kwargs["chat_id"] == VLAD_ID
    assert sheets.get_active_price_trackings() == []


def test_photo_sent_when_image_and_send_photo_available(app, monkeypatch, db):
    now = dt.datetime(2026, 9, 20, 10, 0, tzinfo=ASTANA_TZ)
    frozen(monkeypatch, app, now)
    app.bot.send_photo = AsyncMock()
    _seed_tracking_row(db, first_price=10000, last_price=10000,
                        image_url="https://img.example/1.png",
                        last_checked_at="2026-09-20 06:00:00")
    monkeypatch.setattr(app, "fetch_product_info", AsyncMock(return_value={
        "price": 7000, "image_url": "https://img.example/1.png", "url": "https://kaspi.kz/shop/p/-123/",
    }))
    tick(app.check_price_tracking)
    assert app.bot.send_photo.called
    assert not app.bot.send_message.called
    kwargs = app.bot.send_photo.call_args.kwargs
    assert kwargs.get("photo") == "https://img.example/1.png" and kwargs["chat_id"] == VLAD_ID
    assert "**" not in kwargs.get("caption", "")
    assert "Цена упала" in kwargs.get("caption", "")



# ---------- спам: повторы, мелкие падения, группа ----------

def _age_row(db, when, row=2):
    col = sheets.PRICE_TRACKING_HEADERS.index("last_checked_at") + 1
    db.worksheet("PriceTracking").update_cell(row, col, when)


def _check(app, monkeypatch, db, price, at, image=""):
    """Одна проверка цены в момент at (предыдущая проверка — 4 часа назад)."""
    frozen(monkeypatch, app, at)
    monkeypatch.setattr(app, "fetch_product_info", AsyncMock(return_value={
        "price": price, "image_url": image, "url": "https://kaspi.kz/shop/p/-123/"}))
    _age_row(db, (at - dt.timedelta(hours=4)).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S"))
    app.bot.send_message.reset_mock()
    tick(app.check_price_tracking)
    return [c.kwargs for c in app.bot.send_message.call_args_list]


def test_same_drop_is_announced_once_not_every_check(app, monkeypatch, db):
    t0 = dt.datetime(2026, 9, 20, 10, 0, tzinfo=ASTANA_TZ)
    _seed_tracking_row(db, first_price=14690, last_price=14690, image_url="")
    assert _check(app, monkeypatch, db, 14390, t0) == []                              # −2%: «пара сотен» — молчим
    sent = _check(app, monkeypatch, db, 13900, t0 + dt.timedelta(hours=4))            # −5,4% от 14 690: один раз
    assert len(sent) == 1 and sent[0]["chat_id"] == VLAD_ID
    assert "14 690" in sent[0]["text"] and "13 900" in sent[0]["text"] and "Цена упала на 5.4%" in sent[0]["text"]
    for hours, price in ((8, 13900), (12, 13900), (16, 13850), (20, 13900)):          # дальше та же цена — тишина
        assert _check(app, monkeypatch, db, price, t0 + dt.timedelta(hours=hours)) == []


def test_cooldown_then_new_drop_and_rise_resets_baseline(app, monkeypatch, db):
    t0 = dt.datetime(2026, 9, 20, 10, 0, tzinfo=ASTANA_TZ)
    _seed_tracking_row(db, first_price=14690, last_price=14690, image_url="")
    assert len(_check(app, monkeypatch, db, 13900, t0)) == 1
    assert _check(app, monkeypatch, db, 12900, t0 + dt.timedelta(hours=8)) == []      # −7%, но суток ещё не прошло
    again = _check(app, monkeypatch, db, 12900, t0 + dt.timedelta(hours=26))          # прошли сутки, цена всё ещё ниже
    assert len(again) == 1 and "13 900" in again[0]["text"] and "12 900" in again[0]["text"]
    assert "С начала слежения: 14 690" in again[0]["text"]
    assert _check(app, monkeypatch, db, 15000, t0 + dt.timedelta(hours=30)) == []     # цена выросла — база поднялась
    assert _check(app, monkeypatch, db, 15000, t0 + dt.timedelta(hours=34)) == []
    third = _check(app, monkeypatch, db, 14000, t0 + dt.timedelta(hours=60))          # падение уже от 15 000
    assert len(third) == 1 and "15 000" in third[0]["text"]


def test_nothing_ever_goes_to_the_group_chat(app, monkeypatch, db):
    t0 = dt.datetime(2026, 9, 20, 10, 0, tzinfo=ASTANA_TZ)
    _seed_tracking_row(db, first_price=10000, last_price=10000, target_price=9000, image_url="")
    sent = _check(app, monkeypatch, db, 8000, t0)
    assert sent and all(c["chat_id"] == VLAD_ID for c in sent) and all(c["chat_id"] != app.FAMILY_CHAT_ID for c in sent)


def test_old_rows_do_not_get_a_repeat_right_after_the_update(app, monkeypatch, db):
    # Цена уже была ниже первоначальной, о ней уже писали много раз (старое поведение): новых колонок нет
    t0 = dt.datetime(2026, 9, 20, 10, 0, tzinfo=ASTANA_TZ)
    _seed_tracking_row(db, first_price=14690, last_price=14390, image_url="")
    assert _check(app, monkeypatch, db, 14390, t0) == []


def test_duplicate_rows_for_one_product_send_one_message(app, monkeypatch, db):
    t0 = dt.datetime(2026, 9, 20, 10, 0, tzinfo=ASTANA_TZ)
    _seed_tracking_row(db, id="PRICE_1", first_price=10000, last_price=10000, image_url="")
    _seed_tracking_row(db, id="PRICE_2", first_price=10000, last_price=10000, image_url="",
                       url="https://kaspi.kz/shop/p/-123/?c=750000000")             # та же страница в другом написании
    _age_row(db, "2026-09-20 02:00:00", row=3)
    sent = _check(app, monkeypatch, db, 7000, t0)
    assert len(sent) == 1
    rows = db.worksheet("PriceTracking").get_all_values()
    statuses = {r[0]: r[sheets.PRICE_TRACKING_HEADERS.index("status")] for r in rows[1:]}
    assert statuses == {"PRICE_1": "active", "PRICE_2": "stopped"}


def test_another_person_gets_their_own_message_for_the_same_link(app, monkeypatch, db):
    t0 = dt.datetime(2026, 9, 20, 10, 0, tzinfo=ASTANA_TZ)
    _seed_tracking_row(db, id="PRICE_1", user="Влад", first_price=10000, last_price=10000, image_url="")
    _seed_tracking_row(db, id="PRICE_2", user="Диана", first_price=10000, last_price=10000, image_url="")
    _age_row(db, "2026-09-20 02:00:00", row=3)
    sent = _check(app, monkeypatch, db, 7000, t0)
    assert sorted(c["chat_id"] for c in sent) == [VLAD_ID, DIANA_ID]


def test_dm_unavailable_hint_in_group_once_a_day_and_message_is_not_lost(app, monkeypatch, db):
    t0 = dt.datetime(2026, 9, 20, 10, 0, tzinfo=ASTANA_TZ)
    _seed_tracking_row(db, first_price=10000, last_price=10000, image_url="")
    state_ok = {"dm": False}

    async def send(**kwargs):
        if kwargs["chat_id"] == VLAD_ID and not state_ok["dm"]:
            raise RuntimeError("Forbidden: bot can't initiate conversation with a user")
        return SimpleNamespace(message_id=1)

    app.bot.send_message = AsyncMock(side_effect=send)
    sent = _check_raw(app, monkeypatch, db, 7000, t0)
    hints = [c for c in sent if c["chat_id"] == app.FAMILY_CHAT_ID]
    assert len(hints) == 1 and "личк" in hints[0]["text"] and "7 000" not in hints[0]["text"]        # без цен и названия
    _check_raw(app, monkeypatch, db, 7000, t0 + dt.timedelta(hours=4))          # ещё проверка в тот же день
    again = [c for c in _check_raw(app, monkeypatch, db, 7000, t0 + dt.timedelta(hours=8)) if c["chat_id"] == app.FAMILY_CHAT_ID]
    assert again == []                                                                              # второй раз за день — нет
    state_ok["dm"] = True
    delivered = [c for c in _check_raw(app, monkeypatch, db, 7000, t0 + dt.timedelta(hours=12)) if c["chat_id"] == VLAD_ID]
    assert len(delivered) == 1 and "Цена упала" in delivered[0]["text"]                           # не потеряно


def _check_raw(app, monkeypatch, db, price, at):
    frozen(monkeypatch, app, at)
    monkeypatch.setattr(app, "fetch_product_info", AsyncMock(return_value={
        "price": price, "image_url": "", "url": "https://kaspi.kz/shop/p/-123/"}))
    _age_row(db, (at - dt.timedelta(hours=4)).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S"))
    app.bot.send_message.reset_mock()
    tick(app.check_price_tracking)
    return [c.kwargs for c in app.bot.send_message.call_args_list]


# ---------- чистая логика и добавление ----------

@pytest.mark.parametrize("last,notified,new,expected", [
    (14690, "", 14390, None),            # −2%
    (14690, "", 14000, None),            # −4,7% (690 ₸), порог 5%
    (14690, "", 13900, "drop"),          # −5,4%
    (3000, "", 2800, None),              # −6,7%, но всего 200 ₸ (порог 500)
    (3000, "", 2400, "drop"),
    (14690, 13000, 13500, None),         # выросла относительно базы
    (14690, 13000, 12300, "drop"),
])
def test_decide_thresholds(last, notified, new, expected):
    from services import price_alerts
    item = {"first_price": 20000, "last_price": last, "notified_price": notified, "target_price": ""}
    assert price_alerts.decide(item, new, dt.datetime(2026, 9, 20, 10, 0))["reason"] == expected


def test_decide_target_wins_and_message_is_plain_text():
    from services import price_alerts
    item = {"product_name": "Дрель-шуруповерт " + "очень длинное название " * 8, "first_price": 15000, "last_price": 14690,
            "target_price": 14000}
    d = price_alerts.decide(item, 13900, dt.datetime(2026, 9, 20, 10, 0))
    assert d["reason"] == "target"
    text = price_alerts.build_message(item, d, 13900, "https://kaspi.kz/shop/p/-1/")
    assert "Достигнута цель 14 000" in text and "13 900" in text and "**" not in text
    assert len(text.splitlines()[0]) < 110 and "…" in text.splitlines()[0]          # длинное название обрезано


def test_adding_the_same_product_twice_keeps_one_row_and_updates_target(db):
    first = sheets.add_price_tracking({"user": "Влад", "url": "https://kaspi.kz/shop/p/-123/", "product_name": "Дрель",
                                       "price": 14690, "target_price": 12000})
    assert first and not first.get("already_tracking")
    second = sheets.add_price_tracking({"user": "Влад", "url": "https://kaspi.kz/shop/p/-123?c=750", "product_name": "Дрель",
                                        "price": 14390, "target_price": 11000})
    assert second["already_tracking"] is True and second["target_price"] == 11000
    rows = db.worksheet("PriceTracking").get_all_values()
    assert len(rows) == 2 and rows[1][sheets.PRICE_TRACKING_HEADERS.index("target_price")] == 11000
    other_person = sheets.add_price_tracking({"user": "Диана", "url": "https://kaspi.kz/shop/p/-123/", "price": 14390})
    assert not other_person.get("already_tracking") and len(db.worksheet("PriceTracking").get_all_values()) == 3


def test_older_sheet_without_new_columns_is_extended(db):
    from conftest import Sheet
    old_headers = sheets.PRICE_TRACKING_HEADERS[:12]
    db.sheets["PriceTracking"] = Sheet("PriceTracking", [old_headers], cols=12)
    ws = sheets._get_or_create_price_tracking_sheet()
    assert ws.get_all_values()[0][12:14] == ["notified_price", "notified_at"]

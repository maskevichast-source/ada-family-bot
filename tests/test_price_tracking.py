import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from test_handlers import handler
from test_media_schedulers import app, frozen, tick, StopTick
from services.timezone import ASTANA_TZ
from services import sheets


def _seed_tracking_row(db, **kwargs):
    defaults = dict(
        id="PRICE_1", user="Влад", url="https://kaspi.kz/shop/p/-123/",
        product_name="Тестовый товар", image_url="https://img.example/1.png",
        target_price="", first_price=10000, last_price=10000,
        last_checked_at="2026-09-01 10:00:00", fail_count=0,
        status="active", created_at="2026-09-01 10:00:00",
    )
    defaults.update(kwargs)
    row = [defaults[h] for h in sheets.PRICE_TRACKING_HEADERS]
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
    assert "Цена упала" in text
    row = sheets.get_active_price_trackings()[0]
    assert int(row["last_price"]) == 8000


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
    assert "Достигнута нужная цена" in text
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
    assert kwargs.get("photo") == "https://img.example/1.png"
    assert "Цена упала" in kwargs.get("caption", "")

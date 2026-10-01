import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_handlers import handler  # noqa: F401
from test_media_schedulers import app  # noqa: F401


def _say(app):
    msg = SimpleNamespace(chat=SimpleNamespace(id=1, type="private"), answer=AsyncMock(), message_id=1)
    asyncio.run(app.cmd_limits_apply(msg))
    return msg.answer.call_args.args[0]


def test_limits_apply_sends_summary(app, monkeypatch):
    monkeypatch.setattr(app, "recalc_and_apply", lambda: ({"Еда и продукты": 1000}, "Сводка пересчёта"))
    assert _say(app) == "Сводка пересчёта"


def test_limits_apply_failure_leaves_limits_alone(app, monkeypatch):
    monkeypatch.setattr(app, "recalc_and_apply", lambda: None)
    assert "Лимиты не тронуты" in _say(app)
    monkeypatch.setattr(app, "recalc_and_apply", lambda: 1 / 0)
    assert "Лимиты не тронуты" in _say(app)

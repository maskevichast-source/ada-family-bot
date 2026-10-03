"""Утренний статус: баланс DeepSeek/OpenAI, здоровье бота, текст и команда /status.
Офлайн: сеть заменена заглушкой, живых вызовов DeepSeek/OpenAI нет."""
import asyncio
import datetime
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import status_report as sr
from services.timezone import ASTANA_TZ

NOW = datetime.datetime(2026, 10, 4, 9, 5, tzinfo=ASTANA_TZ)
TODAY = NOW.date()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "OPENAI_ADMIN_KEY", "OPENAI_BALANCE_START_USD",
                 "OPENAI_BALANCE_START_DATE", "BACKUP_FOLDER_ID", "DEEPSEEK_LOW_BALANCE",
                 "OPENAI_LOW_BALANCE_USD", "STATUS_LOW_DAYS", "RAILWAY_GIT_COMMIT_SHA"):
        monkeypatch.delenv(name, raising=False)


def fake_http(monkeypatch, routes):
    """routes: {часть URL: ответ или список ответов (status, json) / исключение}."""
    calls = []

    async def fake(url, headers, params=None, timeout=20):
        calls.append((url, headers, params))
        for part, answer in routes.items():
            if part in url:
                if isinstance(answer, list):
                    answer = answer.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"неожиданный запрос {url}")
    monkeypatch.setattr(sr, "_http_get_json", fake)
    return calls


DEEPSEEK_OK = (200, {"is_available": True, "balance_infos": [
    {"currency": "USD", "total_balance": "12.40", "granted_balance": "2.40", "topped_up_balance": "10.00"}]})


# ── история и запас ────────────────────────────────────────────────────────

def test_burn_ignores_topups():
    history = [{"date": "2026-10-01", "total": 10.0}, {"date": "2026-10-02", "total": 9.0},
               {"date": "2026-10-03", "total": 20.0}, {"date": "2026-10-04", "total": 19.0}]
    assert sr.daily_burn(history) == pytest.approx(2 / 3)
    assert sr.daily_burn(history[:1]) is None
    assert sr.days_left(19.0, 2 / 3) == 28
    assert sr.days_left(5.0, None) is None


# ── DeepSeek ───────────────────────────────────────────────────────────────

def test_deepseek_balance_parsed_and_key_sent(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deep")
    calls = fake_http(monkeypatch, {"deepseek.com/user/balance": DEEPSEEK_OK})
    data = asyncio.run(sr.deepseek_status(TODAY))
    assert data["ok"] and data["available"]
    b = data["balances"][0]
    assert (b["currency"], b["total"], b["granted"], b["topped_up"]) == ("USD", 12.4, 2.4, 10.0)
    assert calls[0][1]["Authorization"] == "Bearer sk-deep"


def test_deepseek_burn_from_daily_history(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    answers = {"user/balance": [(200, {"balance_infos": [{"currency": "USD", "total_balance": "10.00"}]}),
                                (200, {"balance_infos": [{"currency": "USD", "total_balance": "9.50"}]})]}
    fake_http(monkeypatch, answers)
    asyncio.run(sr.deepseek_status(TODAY - datetime.timedelta(days=1)))
    data = asyncio.run(sr.deepseek_status(TODAY))
    b = data["balances"][0]
    assert b["burn"] == pytest.approx(0.5) and b["days"] == 19


@pytest.mark.parametrize("answer,expected", [((401, {}), "ключ не принят"), ((500, {}), "ответ 500"),
                                             (ConnectionError("x"), "не ответил")])
def test_deepseek_errors(monkeypatch, answer, expected):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    fake_http(monkeypatch, {"user/balance": answer})
    data = asyncio.run(sr.deepseek_status(TODAY))
    assert data["ok"] is False and expected in data["error"]


def test_deepseek_no_key():
    assert asyncio.run(sr.deepseek_status(TODAY)) == {"configured": False}


# ── OpenAI ─────────────────────────────────────────────────────────────────

def _bucket(day, usd):
    ts = int(datetime.datetime.combine(day, datetime.time(0, 0), tzinfo=ASTANA_TZ).timestamp())
    return {"start_time": ts, "results": [{"amount": {"value": usd, "currency": "usd"}}]}


def test_openai_key_ok_but_balance_not_configured(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    fake_http(monkeypatch, {"v1/models": (200, {})})
    data = asyncio.run(sr.openai_status(NOW))
    assert data["ok"] and data["balance"] is None


def test_openai_invalid_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    fake_http(monkeypatch, {"v1/models": (401, {})})
    data = asyncio.run(sr.openai_status(NOW))
    assert data["ok"] is False and "не принят" in data["error"]


def test_openai_remaining_from_costs_with_pagination(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-1")
    monkeypatch.setenv("OPENAI_BALANCE_START_USD", "20")
    monkeypatch.setenv("OPENAI_BALANCE_START_DATE", "2026-09-20")
    d = lambda n: TODAY - datetime.timedelta(days=n)
    page1 = (200, {"data": [_bucket(d(14), 1.0), _bucket(d(2), 0.7)], "has_more": True, "next_page": "P2"})
    page2 = (200, {"data": [_bucket(d(1), 0.7)], "has_more": False})
    calls = fake_http(monkeypatch, {"v1/models": (200, {}), "organization/costs": [page1, page2]})
    data = asyncio.run(sr.openai_status(NOW))
    balance = data["balance"]
    assert balance["spent"] == pytest.approx(2.4) and balance["remaining"] == pytest.approx(17.6)
    assert balance["burn"] == pytest.approx(1.4 / 7)                 # за последние 7 суток
    assert balance["days"] == int(17.6 / (1.4 / 7))
    costs_calls = [c for c in calls if "organization/costs" in c[0]]
    assert costs_calls[0][1]["Authorization"] == "Bearer sk-admin-1"      # админ-ключ, не обычный
    assert costs_calls[1][2]["page"] == "P2"


def test_openai_costs_forbidden_reports_error_not_crash(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-bad")
    monkeypatch.setenv("OPENAI_BALANCE_START_USD", "20")
    monkeypatch.setenv("OPENAI_BALANCE_START_DATE", "2026-09-20")
    fake_http(monkeypatch, {"v1/models": (200, {}), "organization/costs": (403, {})})
    data = asyncio.run(sr.openai_status(NOW))
    assert data["ok"] and data["balance"] is None and "админ-ключ" in data["balance_error"]


def test_openai_bad_start_date_means_not_configured(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-1")
    monkeypatch.setenv("OPENAI_BALANCE_START_USD", "20")
    monkeypatch.setenv("OPENAI_BALANCE_START_DATE", "вчера")
    fake_http(monkeypatch, {"v1/models": (200, {})})
    assert asyncio.run(sr.openai_status(NOW))["balance"] is None


# ── здоровье и текст ───────────────────────────────────────────────────────

def test_sheets_health_counts_yesterday(db):
    from services import sheets
    sheets.append_transaction({"transaction_id": "Y1", "amount": 100,
                               "occurred_at": "2026-10-03 12:00"})
    result = sr.sheets_health(NOW)
    assert result["ok"] is True and "yesterday" in result


def _data(**over):
    base = {
        "sheets": {"ok": True, "seconds": 0.8, "yesterday": 7},
        "backup": {"configured": True, "last": "2026-09-28T03:31:00+05:00"},
        "deepseek": {"configured": True, "ok": True, "available": True, "balances": [
            {"currency": "USD", "total": 12.4, "granted": 2.4, "topped_up": 10.0, "burn": 0.3, "days": 41}]},
        "openai": {"configured": True, "ok": True, "balance": {
            "start": 20.0, "since": "2026-09-20", "spent": 2.4, "remaining": 17.6, "burn": 0.2, "days": 88}},
    }
    base.update(over)
    return base


def test_report_all_good():
    text, problems = sr.format_report(_data(), NOW)
    assert problems == 0 and "Всё в порядке ✅" in text and "04.10.2026" in text
    assert "12,40 USD" in text and "около $17,60" in text and "вчера записано: 7" in text
    assert "хватит примерно на 41 дн." in text and "28.09.2026 03:31" in text


def test_report_flags_low_balances_and_no_backup():
    data = _data(
        backup={"configured": False},
        deepseek={"configured": True, "ok": True, "available": True,
                  "balances": [{"currency": "USD", "total": 1.2, "granted": 0, "topped_up": 1.2, "burn": 0.3, "days": 4}]},
        openai={"configured": True, "ok": True, "balance": {
            "start": 20.0, "since": "2026-09-20", "spent": 18.5, "remaining": 1.5, "burn": 0.5, "days": 3}})
    text, problems = sr.format_report(data, NOW)
    assert problems == 3 and "Есть что проверить ⚠️ (3)" in text
    assert text.count("пора пополнять") == 2 and "Резервные копии: не настроены ⚠️" in text


def test_report_thresholds_come_from_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_LOW_BALANCE", "20")
    text, problems = sr.format_report(_data(), NOW)
    assert problems == 1 and "DeepSeek: 12,40 USD" in text and "пора пополнять" in text


def test_report_when_openai_balance_not_set_up_points_to_help():
    data = _data(openai={"configured": True, "ok": True, "balance": None})
    text, problems = sr.format_report(data, NOW)
    assert problems == 0 and "/status настройка" in text


def test_report_broken_components():
    data = _data(sheets={"ok": False, "error": "APIError"},
                 deepseek={"configured": True, "ok": False, "error": "ключ не принят"},
                 openai={"configured": True, "ok": False, "error": "не ответил (TimeoutError)"})
    text, problems = sr.format_report(data, NOW)
    assert problems == 3 and "Google Sheets: не читается" in text and "ключ не принят ⚠️" in text


def test_report_never_contains_secrets(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret-deep")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-openai")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-secret")
    text, _ = sr.format_report(_data(), NOW)
    assert "secret" not in text


def test_collect_survives_one_failing_check(monkeypatch, db):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    fake_http(monkeypatch, {"user/balance": DEEPSEEK_OK, "v1/models": ConnectionError("down")})
    data = asyncio.run(sr.collect(NOW))
    assert data["deepseek"]["ok"] is True and data["openai"]["ok"] is False
    text = asyncio.run(sr.build_report(NOW))
    assert "Утренний статус Ады" in text


def test_status_window():
    at = lambda h: datetime.datetime(2026, 10, 4, h, 0, tzinfo=ASTANA_TZ)
    assert not sr.status_due(at(8)) and sr.status_due(at(9)) and sr.status_due(at(11)) and not sr.status_due(at(12))
    assert sr.status_due(at(7), start_hour=7) and not sr.status_due(at(6), start_hour=7)


# ── команда /status ────────────────────────────────────────────────────────

@pytest.fixture
def main_module(monkeypatch):
    import config
    settings = {"TELEGRAM_BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk", "FAMILY_CHAT_ID": -100,
                "OPENAI_API_KEY": "test", "DEEPSEEK_API_KEY": "test", "GOOGLE_SHEETS_KEY": "test"}
    for key, value in settings.items():
        monkeypatch.setattr(config, key, value)
    monkeypatch.setattr(config, "VLAD_TELEGRAM_ID", "11")
    monkeypatch.setattr(config, "DIANA_TELEGRAM_ID", "22")
    return importlib.import_module("main")


def _msg(uid, chat_type="supergroup"):
    return SimpleNamespace(from_user=SimpleNamespace(id=uid, first_name="Влад"),
                           chat=SimpleNamespace(id=-100, type=chat_type), answer=AsyncMock())


def test_status_command_only_for_vlad(main_module, monkeypatch):
    monkeypatch.setattr(main_module, "safe_answer", AsyncMock())
    build = AsyncMock(return_value="отчёт")
    monkeypatch.setattr(sr, "build_report", build)
    asyncio.run(main_module.cmd_status(_msg(22), None))
    assert not build.called and "только для Влада" in main_module.safe_answer.call_args.args[1]


def test_status_from_group_goes_to_dm_not_chat(main_module, monkeypatch):
    monkeypatch.setattr(main_module, "safe_answer", AsyncMock())
    monkeypatch.setattr(sr, "build_report", AsyncMock(return_value="ЦИФРЫ БАЛАНСА"))
    dm = AsyncMock(return_value=True)
    monkeypatch.setattr(main_module, "_send_status_dm", dm)
    asyncio.run(main_module.cmd_status(_msg(11), None))
    assert dm.call_args.args[0] == "ЦИФРЫ БАЛАНСА"
    assert "ЦИФРЫ" not in main_module.safe_answer.call_args.args[1]       # в группу цифры не попадают


def test_status_private_answers_directly(main_module, monkeypatch):
    monkeypatch.setattr(sr, "build_report", AsyncMock(return_value="ОТЧЁТ"))
    msg = _msg(11, "private")
    asyncio.run(main_module.cmd_status(msg, None))
    assert msg.answer.call_args.args[0] == "ОТЧЁТ"


def test_status_setup_help(main_module, monkeypatch):
    msg = _msg(11, "private")
    asyncio.run(main_module.cmd_status(msg, SimpleNamespace(args="настройка")))
    assert "OPENAI_ADMIN_KEY" in msg.answer.call_args.args[0]

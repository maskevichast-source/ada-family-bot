import gspread
import pytest

from services import sheets


class _Resp:
    status_code = 429
    text = "{}"

    def json(self):
        return {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}


def test_quota_retry_waits_and_succeeds(monkeypatch):
    from gspread import http_client as hc
    calls = []

    def flaky(self, *a, **k):
        calls.append(1)
        if len(calls) < 3:
            raise gspread.exceptions.APIError(_Resp())
        return "ok"

    flaky._ada_quota_retry = False
    monkeypatch.setattr(hc.HTTPClient, "request", flaky)
    monkeypatch.setattr(sheets, "QUOTA_RETRY_DELAYS", (0, 0, 0))
    sheets._install_quota_retry()
    assert hc.HTTPClient.request(object()) == "ok"
    assert len(calls) == 3


def test_quota_retry_gives_up(monkeypatch):
    from gspread import http_client as hc

    def always(self, *a, **k):
        raise gspread.exceptions.APIError(_Resp())

    always._ada_quota_retry = False
    monkeypatch.setattr(hc.HTTPClient, "request", always)
    monkeypatch.setattr(sheets, "QUOTA_RETRY_DELAYS", (0, 0))
    sheets._install_quota_retry()
    with pytest.raises(gspread.exceptions.APIError):
        hc.HTTPClient.request(object())


def test_worksheet_lookup_is_cached(monkeypatch):
    class WS:
        def get_all_values(self):
            return [["a"]]

    class DB:
        n = 0

        def worksheet(self, title):
            DB.n += 1
            return WS()

    db = DB()
    monkeypatch.setattr(sheets, "get_db", lambda: db)
    sheets._ws_cache.clear()
    a = sheets._get_or_create_worksheet("X", ["a"])
    b = sheets._get_or_create_worksheet("X", ["a"])
    assert a is b and DB.n == 1
    sheets._ws_cache.clear()

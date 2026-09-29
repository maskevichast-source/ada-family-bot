import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

import config
from services import webapp

TOKEN = "123456:TEST-TOKEN"


def make_init_data(user_id=111, token=TOKEN, auth_date=None, tamper=False):
    fields = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAH",
        "user": json.dumps({"id": user_id, "first_name": "X"}, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if tamper:
        fields["user"] = json.dumps({"id": 999, "first_name": "X"}, separators=(",", ":"))
    return urlencode(fields)


def test_verify_accepts_valid_signature():
    data = webapp.verify_init_data(make_init_data(), TOKEN)
    assert data is not None
    assert data["user"]["id"] == 111


def test_verify_rejects_wrong_token_tamper_missing_hash_and_empty():
    assert webapp.verify_init_data(make_init_data(), "999:OTHER") is None
    assert webapp.verify_init_data(make_init_data(tamper=True), TOKEN) is None
    assert webapp.verify_init_data("auth_date=1&user=%7B%7D", TOKEN) is None
    assert webapp.verify_init_data("", TOKEN) is None
    assert webapp.verify_init_data(make_init_data(), "") is None


def test_verify_rejects_expired():
    old = int(time.time()) - webapp.INIT_DATA_MAX_AGE_SECONDS - 60
    assert webapp.verify_init_data(make_init_data(auth_date=old), TOKEN) is None


def _with_client(coro):
    async def runner():
        server = TestServer(webapp.build_app())
        client = TestClient(server)
        await client.start_server()
        try:
            return await coro(client)
        finally:
            await client.close()
    return asyncio.run(runner())


def _setup(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(config, "VLAD_TELEGRAM_ID", "111")
    monkeypatch.setattr(config, "DIANA_TELEGRAM_ID", "222")


def test_health_and_page_are_public(monkeypatch):
    _setup(monkeypatch)

    async def check(client):
        r = await client.get("/health")
        assert r.status == 200 and (await r.json()) == {"ok": True}
        r = await client.get("/app")
        assert r.status == 200
        assert "telegram-web-app.js" in await r.text()

    _with_client(check)


def test_api_me_allows_only_family(monkeypatch):
    _setup(monkeypatch)

    async def check(client):
        h = webapp.INIT_DATA_HEADER
        r = await client.get("/api/me")
        assert r.status == 403
        r = await client.get("/api/me", headers={h: make_init_data(user_id=111)})
        assert r.status == 200 and (await r.json()) == {"name": "Влад"}
        r = await client.get("/api/me", headers={h: make_init_data(user_id=222)})
        assert (await r.json()) == {"name": "Диана"}
        # валидная подпись, но чужой Telegram ID
        r = await client.get("/api/me", headers={h: make_init_data(user_id=333)})
        assert r.status == 403
        # подделанные данные
        r = await client.get("/api/me", headers={h: make_init_data(user_id=111, tamper=True)})
        assert r.status == 403

    _with_client(check)


def test_webapp_url_and_disabled_flag(monkeypatch):
    monkeypatch.delenv("WEBAPP_URL", raising=False)
    assert webapp.webapp_url() == ""
    monkeypatch.setenv("WEBAPP_URL", "https://x.up.railway.app/")
    assert webapp.webapp_url() == "https://x.up.railway.app/app"
    monkeypatch.setenv("WEBAPP_ENABLED", "false")
    assert asyncio.run(webapp.start_webapp()) is None

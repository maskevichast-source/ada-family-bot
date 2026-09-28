import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from services import vision, deepseek_service


def _fake_client(captured):
    async def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content='{"transactions": []}'))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock())


def _user_text(captured):
    content = captured["messages"][1]["content"]
    return next(part["text"] for part in content if part["type"] == "text")


def test_caption_is_passed_as_what_was_bought(monkeypatch):
    captured = {}
    monkeypatch.setattr(vision, "AsyncOpenAI", lambda **kw: _fake_client(captured))
    asyncio.run(vision.parse_receipt(b"fake", "receipt.jpg", caption="мусорное ведро", user_name="Диана"))
    text = _user_text(captured)
    assert "мусорное ведро" in text
    assert "ЧТО КУПЛЕНО" in text
    assert "Подписи нет" not in text


def test_no_caption_is_stated_explicitly(monkeypatch):
    captured = {}
    monkeypatch.setattr(vision, "AsyncOpenAI", lambda **kw: _fake_client(captured))
    asyncio.run(vision.parse_receipt(b"fake", "receipt.jpg", caption="", user_name="Диана"))
    text = _user_text(captured)
    assert "Подписи нет" in text
    assert "ЧТО КУПЛЕНО" not in text


def test_vision_prompt_forbids_ignoring_caption_and_duplicating_header():
    prompt = vision.VISION_SYSTEM_PROMPT
    assert "Название магазина на скриншоте" in prompt and "НЕ заменяет подпись" in prompt
    assert '"возможно"' in prompt and "что-то полезное" in prompt
    assert "НЕ начинай" in prompt and "Записано по чеку" in prompt
    assert "приобретены" in prompt  # в списке запрещённого канцелярита


def test_text_prompt_forbids_bureaucratic_words():
    template = deepseek_service.SYSTEM_PROMPT_TEMPLATE
    assert "приобретены" in template
    assert "называй" in template

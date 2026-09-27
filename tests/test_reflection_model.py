import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from services import deepseek_service


def test_generate_budget_reflection_uses_reasoning_model(monkeypatch):
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        msg = SimpleNamespace(content="Живая реплика.")
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    monkeypatch.setattr(deepseek_service.client.chat.completions, "create", fake_create)
    monkeypatch.setattr(deepseek_service, "DEEPSEEK_REASONING_MODEL", "deepseek-v4-pro")
    monkeypatch.setattr(deepseek_service, "DEEPSEEK_MODEL", "deepseek-flash")

    result = asyncio.run(deepseek_service.generate_budget_reflection("weekly", "Отчёт: 100000 тг расходов"))

    assert result == "Живая реплика."
    assert captured["model"] == "deepseek-v4-pro"
    assert captured["model"] != "deepseek-flash"


def test_generate_budget_reflection_falls_back_to_empty_on_error(monkeypatch):
    async def fake_create(**kwargs):
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr(deepseek_service.client.chat.completions, "create", fake_create)
    result = asyncio.run(deepseek_service.generate_budget_reflection("weekly", "Отчёт"))
    assert result == ""

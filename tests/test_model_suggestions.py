"""The model hint path reuses constrained execution without changing interpretation."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from travel_agent.adapters.model_codex import CodexModelGateway, ModelGatewayError, OUTPUT_SCHEMA
from travel_agent.adapters.model_hf import HFModelGateway


HINTS = [{"id": "origin", "label": "Из Москвы", "text": "Поеду из Москвы."}]
CONTEXT = {
    "latest_assistant_question": "Из какого города поедете?",
    "recent_dialogue": [
        {"role": "user", "text": "хочу на концерт моргена"},
        {"role": "user", "text": "я один город и дата любые"},
        {"role": "assistant", "text": "Из какого города поедете?"},
    ],
}


def final_event(payload):
    return json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(payload)}}).encode()


async def test_codex_suggest_uses_actual_dialogue_once_with_short_deadline(monkeypatch):
    from travel_agent.graph.suggestions import SUGGESTION_SCHEMA
    gateway = CodexModelGateway(executable="/fake/codex", timeout=75)
    completion = AsyncMock(return_value={"suggestions": HINTS})
    monkeypatch.setattr(gateway, "_complete_json", completion)
    assert await gateway.suggest(CONTEXT) == HINTS
    completion.assert_awaited_once()
    prompt, schema = completion.call_args.args
    assert schema == SUGGESTION_SCHEMA
    assert "Из какого города поедете?" in prompt
    assert "хочу на концерт моргена" in prompt
    assert "я один город и дата любые" in prompt
    assert completion.call_args.kwargs["timeout"] == 20


async def test_codex_suggest_respects_smaller_gateway_deadline(monkeypatch):
    gateway = CodexModelGateway(executable="/fake/codex", timeout=3)
    completion = AsyncMock(return_value={"suggestions": []})
    monkeypatch.setattr(gateway, "_complete_json", completion)
    assert await gateway.suggest(CONTEXT) == []
    assert completion.call_args.kwargs["timeout"] == 3


async def test_codex_suggest_rejects_missing_list(monkeypatch):
    gateway = CodexModelGateway(executable="/fake/codex")
    monkeypatch.setattr(gateway, "_complete_json", AsyncMock(return_value={"suggestions": "bad"}))
    with pytest.raises(ModelGatewayError) as exc:
        await gateway.suggest(CONTEXT)
    assert exc.value.code == "model_invalid_response"


async def test_interpret_still_validates_and_removes_null_patch(monkeypatch):
    gateway = CodexModelGateway(executable="/fake/codex")
    completion = AsyncMock(return_value={"intent": "plan", "patch": {"origin": "Москва", "destination": None}})
    monkeypatch.setattr(gateway, "_complete_json", completion)
    result = await gateway.interpret("Из Москвы", {}, "2026-10-27T12:00:00+03:00", "Europe/Moscow")
    assert result["patch"] == {"origin": "Москва"}
    assert completion.call_args.args[1] == OUTPUT_SCHEMA
    completion.return_value = {"intent": "plan", "patch": {"children_ages": [True]}}
    with pytest.raises(ModelGatewayError) as exc:
        await gateway.interpret("С ребенком", {}, "2026-10-27T12:00:00+03:00", "Europe/Moscow")
    assert exc.value.code == "model_invalid_response"


async def test_json_helper_keeps_cli_restrictions_and_does_not_forward_secrets(monkeypatch):
    monkeypatch.setenv("CODEX_MODEL", "gpt-6.1-terra")
    monkeypatch.setenv("CODEX_REASONING_EFFORT", "medium")
    gateway = CodexModelGateway(executable="/fake/codex")
    monkeypatch.setenv("HF_TOKEN", "test-secret-never-forwarded")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-never-forwarded")
    captured = {}
    schema = {"type": "object", "properties": {"suggestions": {"type": "array"}}}

    async def spawn(*args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        assert not list(Path(kwargs["cwd"]).iterdir())
        path = Path(args[args.index("--output-schema") + 1])
        assert json.loads(path.read_text()) == schema
        captured["folder"] = Path(kwargs["cwd"])
        return SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(final_event({"suggestions": HINTS}), b"private stderr")))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert await gateway._complete_json("just a prompt", schema) == {"suggestions": HINTS}
    args, kwargs = captured["args"], captured["kwargs"]
    for flag in ("--ephemeral", "--ignore-user-config", "read-only", "features.shell_tool=false", "features.apps=false", "features.plugins=false", "features.multi_agent=false", 'web_search="disabled"'):
        assert flag in args
    assert args[args.index("--model") + 1] == "gpt-6.1-terra"
    effort_index = args.index('model_reasoning_effort="medium"')
    assert args[effort_index - 1] == "-c"
    assert "HF_TOKEN" not in kwargs["env"]
    assert "OPENAI_API_KEY" not in kwargs["env"]
    assert kwargs["start_new_session"] is True
    assert not captured["folder"].exists()


async def test_json_helper_rejects_tool_events_and_hides_stderr(monkeypatch):
    gateway = CodexModelGateway(executable="/fake/codex")
    event = json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "secret-value"}}).encode()
    process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(event, b"secret stderr")))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(ModelGatewayError) as exc:
        await gateway._complete_json("prompt", {})
    assert exc.value.code == "model_tool_violation"
    assert "secret" not in str(exc.value)
    process.returncode = 1
    with pytest.raises(ModelGatewayError) as exc:
        await gateway._complete_json("prompt", {})
    assert exc.value.code == "model_failed"
    assert "secret" not in str(exc.value)


async def test_json_helper_short_timeout_terminates_child(monkeypatch):
    gateway = CodexModelGateway(executable="/fake/codex", timeout=75)
    calls = 0

    async def communicate(_input=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(1)
        return b"", b""

    process = SimpleNamespace(pid=123456, returncode=None, communicate=communicate)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    killed = []
    monkeypatch.setattr("travel_agent.adapters.model_codex.os.killpg", lambda pid, sig: killed.append(pid))
    with pytest.raises(ModelGatewayError) as exc:
        await gateway._complete_json("prompt", {}, timeout=0.001)
    assert exc.value.code == "model_timeout"
    assert killed == [123456]
    assert calls == 2


async def test_hf_suggest_uses_one_structured_completion(monkeypatch):
    from travel_agent.graph.suggestions import SUGGESTION_SCHEMA
    gateway = HFModelGateway(token="test-token", model_id="test/model")
    response = SimpleNamespace(tool_calls=[{"name": "suggest_messages", "args": {"suggestions": HINTS}}])
    bound = SimpleNamespace(ainvoke=AsyncMock(return_value=response))
    captured = {}

    def bind_tools(tools, **kwargs):
        captured.update(tools=tools, kwargs=kwargs)
        return bound

    model = SimpleNamespace(bind_tools=bind_tools, ainvoke=AsyncMock())
    monkeypatch.setattr(gateway, "_get_model", lambda: model)
    assert await gateway.suggest(CONTEXT) == HINTS
    assert captured["tools"][0]["function"]["parameters"] == SUGGESTION_SCHEMA
    assert captured["kwargs"]["tool_choice"] == "suggest_messages"
    bound.ainvoke.assert_awaited_once()
    model.ainvoke.assert_not_awaited()
    assert "концерт моргена" in bound.ainvoke.call_args.args[0][1].content
    response.tool_calls = [{"name": "wrong_tool", "args": {"suggestions": HINTS}}]
    with pytest.raises(ModelGatewayError) as exc:
        await gateway.suggest(CONTEXT)
    assert exc.value.code == "model_invalid_response"


def test_codex_explicit_model_settings_override_environment(monkeypatch):
    monkeypatch.setenv("CODEX_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("CODEX_REASONING_EFFORT", "high")
    gateway = CodexModelGateway(executable="/fake/codex", model="gpt-6.1-terra", reasoning_effort="medium")
    assert gateway.model == "gpt-6.1-terra"
    assert gateway.reasoning_effort == "medium"


def test_codex_rejects_unknown_reasoning_effort(monkeypatch):
    monkeypatch.setenv("CODEX_REASONING_EFFORT", "typo")
    with pytest.raises(ModelGatewayError) as exc:
        CodexModelGateway(executable="/fake/codex")
    assert exc.value.code == "model_invalid_config"

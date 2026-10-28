"""Interpret requests through the user's already authenticated local Codex CLI.

The CLI receives only a bounded text prompt in an empty temporary working
folder. Shell, network-search, apps, plugins, memories and subagents are disabled.
It never receives provider credentials or raw travel API records.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import tempfile
from time import monotonic
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from travel_agent.interpretation import Interpretation, SYSTEM_INSTRUCTIONS


def _nullable(kind: str, **kwargs: Any) -> dict:
    return {"type": [kind, "null"], **kwargs}


def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


_constraint_schema = _object({
    "baggage_kg": _nullable("integer"), "breakfast": _nullable("boolean"),
    "refundable": _nullable("boolean"), "flight_refundable": _nullable("boolean"),
    "hotel_refundable": _nullable("boolean"), "free_cancellation": _nullable("boolean"), "pets": _nullable("boolean"),
    "direct": _nullable("boolean"), "hotel_name": _nullable("string"),
    "wheelchair_accessible": _nullable("boolean"), "quiet_room": _nullable("boolean"),
    "away_from_elevator": _nullable("boolean"),
    "prefer_cheaper": _nullable("boolean"),
    "prefer_shorter": _nullable("boolean"), "prefer_daytime": _nullable("boolean"),
    "prefer_hotel_rating": _nullable("number"),
})
_constraint_schema["type"] = ["object", "null"]
_patch_schema = _object({
    "origin": _nullable("string"), "destination": _nullable("string"),
    "departure_date": _nullable("string"), "return_date": _nullable("string"),
    "products": _nullable("array", items={"type": "string", "enum": ["avia", "rail", "bus", "hotel"]}),
    "event_date": _nullable("string"), "event_start_at": _nullable("string"),
    "adults": _nullable("integer"), "children_ages": _nullable("array", items={"type": "integer"}),
    "pets": _nullable("boolean"), "budget_minor": _nullable("integer"),
    "nightly_budget_minor": _nullable("integer"),
    "budget_scope": {"type": ["string", "null"], "enum": ["trip", "per_person", None]},
    "constraints": _constraint_schema,
})
_intake_schema = _object({
    "mode": {"type": ["string", "null"], "enum": ["trip", "information", "cancel", None]},
    "event_name": _nullable("string", maxLength=180), "open_to_suggestions": _nullable("boolean"),
    "period": _nullable("string", maxLength=240), "locations": _nullable("string", maxLength=240),
    "budget_flexible": _nullable("boolean"), "children_pending": _nullable("boolean"),
})
_intake_schema["type"] = ["object", "null"]
OUTPUT_SCHEMA = _object({
    "intent": {"type": "string", "enum": ["plan", "replan", "web_search", "question", "chat", "select", "select_component", "checkout", "confirm", "reject", "undo"]},
    "patch": _patch_schema, "option_index": _nullable("integer"),
    "web_query": _nullable("string", maxLength=400), "question": _nullable("string"), "keep_hotel": {"type": "boolean"}, "reset_dates": {"type": "boolean"},
    "keep_transport": {"type": "boolean"}, "component_type": {"type": ["string", "null"], "enum": ["avia", "rail", "bus", "hotel", None]},
    "question_text": _nullable("string"),
    "reply_text": _nullable("string", minLength=1, maxLength=1500),
    "warnings": {"type": "array", "items": {"type": "string"}},
    "concert_intake": _intake_schema,
})


def _without_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_nulls(v) for v in value]
    return value


class ModelGatewayError(RuntimeError):
    """Safe error: never embeds CLI stderr, credentials, or complete prompts."""
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class CodexModelGateway:
    name = "codex"

    def __init__(self, executable: str | None = None, model: str | None = None,
                 timeout: float = 75.0, *, reasoning_effort: str | None = None):
        self.executable = executable or os.environ.get("CODEX_BIN") or shutil.which("codex")
        self.model = model or os.environ.get("CODEX_MODEL") or None
        self.reasoning_effort = reasoning_effort or os.environ.get("CODEX_REASONING_EFFORT") or None
        if self.reasoning_effort not in {None, "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
            raise ModelGatewayError("model_invalid_config", "Неизвестный уровень рассуждений Codex. Проверьте CODEX_REASONING_EFFORT.")
        self.timeout = timeout
        if not self.executable:
            raise ModelGatewayError("model_unavailable", "Codex CLI не найден. Установите Codex и выполните codex login.")

    async def interpret(self, text: str, request: dict, now: str, timezone: str,
                        skills: list[dict] | None = None) -> dict:
        if len(text) > 12000:
            raise ModelGatewayError("input_too_large", "Сообщение слишком длинное для одного запроса.")
        selected_skills = [{"name": s["name"], "body": s["body"]} for s in (skills or [])]
        prompt = SYSTEM_INSTRUCTIONS + "\nНезаполненные поля схемы верни null; необязательные ограничения — null.\n" + json.dumps(
            {"now": now, "timezone": timezone, "current_request": request,
             "active_skills": selected_skills, "user_message": text}, ensure_ascii=False)
        deadline = monotonic() + self.timeout
        repair_hint = ""
        for attempt in range(2):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise ModelGatewayError("model_timeout", "Codex не ответил за отведенное время. Повторите запрос.")
            try:
                decoded = await self._complete_json(prompt + repair_hint, OUTPUT_SCHEMA, timeout=remaining)
                return Interpretation.model_validate(_without_nulls(decoded)).as_result()
            except ValidationError as exc:
                # Field names are allowlisted; no invalid values, model text or
                # arbitrary extra keys are copied into the repair instruction.
                known = set(OUTPUT_SCHEMA["properties"]) | set(_patch_schema["properties"]) | set(_constraint_schema["properties"])
                fields = sorted({str(part) for error in exc.errors(include_input=False)
                                 for part in error["loc"] if isinstance(part, str) and part in known})
                detail = ", ".join(fields) if fields else "структура ответа"
            except ModelGatewayError as exc:
                if exc.code != "model_invalid_response":
                    raise
                detail = "JSON-объект по схеме"
            if attempt:
                raise ModelGatewayError("model_invalid_response", "Codex вернул ответ, не соответствующий контракту поездки.") from None
            repair_hint = ("\nИсправь только формат и согласованность разбора того же сообщения. "
                           f"Проверка не пройдена: {detail}. Верни один JSON-объект строго по схеме. "
                           "Для chat нужны непустой reply_text и пустой patch без действий; "
                           "для остальных intent reply_text=null. question — уточнение агента, "
                           "question_text — только фактический вопрос пользователя. "
                           "Не вызывай инструменты, не добавляй параметры поездки или новые факты.\n")

    async def suggest(self, context: dict) -> list[dict]:
        """Generate drafts for the next user message from the actual dialogue."""
        from travel_agent.graph.suggestions import SUGGESTION_INSTRUCTIONS, SUGGESTION_SCHEMA

        prompt = SUGGESTION_INSTRUCTIONS + "\nКонтекст текущего диалога (данные):\n" + json.dumps(context, ensure_ascii=False)
        decoded = await self._complete_json(prompt, SUGGESTION_SCHEMA, timeout=min(20.0, self.timeout))
        suggestions = decoded.get("suggestions")
        if not isinstance(suggestions, list):
            raise ModelGatewayError("model_invalid_response", "Codex вернул некорректные подсказки следующего сообщения.")
        return suggestions

    async def research_plan(self, context: dict) -> dict:
        from travel_agent.research import RESEARCH_PLAN_INSTRUCTIONS, RESEARCH_PLAN_SCHEMA
        prompt = RESEARCH_PLAN_INSTRUCTIONS + "\nДанные поиска:\n" + json.dumps(context, ensure_ascii=False)
        return await self._complete_json(prompt, RESEARCH_PLAN_SCHEMA, timeout=min(12.0, self.timeout))

    async def research(self, context: dict) -> dict:
        from travel_agent.research import WEB_RESEARCH_INSTRUCTIONS, WEB_RESEARCH_SCHEMA
        prompt = WEB_RESEARCH_INSTRUCTIONS + "\nДанные поиска:\n" + json.dumps(context, ensure_ascii=False)
        return await self._complete_json(prompt, WEB_RESEARCH_SCHEMA, timeout=min(25.0, self.timeout))

    async def research_events(self, context: dict) -> dict:
        from travel_agent.event_research import EVENT_RESEARCH_INSTRUCTIONS, EVENT_RESEARCH_SCHEMA
        prompt = EVENT_RESEARCH_INSTRUCTIONS + "\nМатериалы поиска (данные):\n" + json.dumps(context, ensure_ascii=False)
        return await self._complete_json(prompt, EVENT_RESEARCH_SCHEMA, timeout=min(30.0, self.timeout))

    async def _complete_json(self, prompt: str, schema: dict, *, timeout: float | None = None) -> dict:
        """One bounded, tool-free CLI completion shared by interpretation and hints."""
        if len(prompt) > 50000:
            raise ModelGatewayError("context_too_large", "Контекст поездки превышает лимит интерпретатора.")
        # Keep existing CLI login through HOME/CODEX_HOME, without passing API keys.
        env_keys = {"PATH", "HOME", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "TMPDIR",
                    "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY",
                    "ALL_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "all_proxy", "no_proxy"}
        env = {k: v for k, v in os.environ.items() if k in env_keys}
        with tempfile.TemporaryDirectory(prefix="travel-agent-codex-") as temporary:
            root = Path(temporary)
            schema_file = root / "output-schema.json"
            schema_file.write_text(json.dumps(schema), encoding="utf-8")
            workdir = root / "empty"
            workdir.mkdir()
            args = [self.executable, "exec", "--json", "--ephemeral", "--ignore-user-config",
                    "--skip-git-repo-check", "--sandbox", "read-only", "-C", str(workdir),
                    "--output-schema", str(schema_file), "-c", 'web_search="disabled"',
                    "-c", "apps._default.enabled=false", "-c", "features.skip_host_skill_discovery=true"]
            for feature in ("shell_tool", "shell_snapshot", "apps", "plugins", "memories",
                            "multi_agent", "multi_agent_v2", "browser_use", "computer_use",
                            "in_app_browser", "view_image", "image_generation", "hooks"):
                args.extend(["-c", f"features.{feature}=false"])
            if self.model:
                args.extend(["--model", self.model])
            if self.reasoning_effort:
                args.extend(["-c", f'model_reasoning_effort="{self.reasoning_effort}"'])
            args.append("-")
            try:
                process = await asyncio.create_subprocess_exec(
                    *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, cwd=workdir, env=env, start_new_session=True,
                    limit=2 * 1024 * 1024)
            except OSError:
                raise ModelGatewayError("model_unavailable", "Не удалось запустить локальный Codex CLI.") from None
            try:
                stdout, _stderr = await asyncio.wait_for(process.communicate(prompt.encode("utf-8")), timeout=self.timeout if timeout is None else timeout)
            except (TimeoutError, asyncio.CancelledError) as error:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.communicate()
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise ModelGatewayError("model_timeout", "Codex не ответил за отведенное время. Повторите запрос.") from None
            if process.returncode:
                raise ModelGatewayError("model_failed", "Codex завершил запрос с ошибкой. Проверьте codex login status и доступность выбранной модели.")
            if len(stdout) > 2 * 1024 * 1024:
                raise ModelGatewayError("model_invalid_response", "Ответ Codex превышает допустимый размер.")
            final = None
            for line in stdout.decode("utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                item = event.get("item") or {}
                if not isinstance(item, dict):
                    continue
                if item.get("type") in {"command_execution", "mcp_tool_call", "web_search", "file_change"}:
                    raise ModelGatewayError("model_tool_violation", "Интерпретатор попытался использовать запрещенный инструмент.")
                if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                    final = item.get("text")
            try:
                decoded = json.loads(final) if isinstance(final, str) else None
                if not isinstance(decoded, dict):
                    raise ValueError("No final JSON")
                return decoded
            except (ValueError, TypeError):
                raise ModelGatewayError("model_invalid_response", "Codex вернул ответ, не соответствующий JSON-контракту.") from None

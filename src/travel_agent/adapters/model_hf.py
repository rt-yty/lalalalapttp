"""Optional Hugging Face gateway; Codex is the first prototype's model path."""
from __future__ import annotations

import asyncio
import json
import os

from travel_agent.adapters.model_codex import ModelGatewayError, OUTPUT_SCHEMA, _without_nulls
from travel_agent.interpretation import Interpretation, SYSTEM_INSTRUCTIONS


class HFModelGateway:
    name = "hf"

    def __init__(self, token: str | None = None, model_id: str | None = None,
                 provider: str | None = None, timeout: float = 60.0):
        self._token = token or os.environ.get("HF_TOKEN")
        self.model_id = model_id or os.environ.get("HF_MODEL_ID")
        self.provider = provider or os.environ.get("HF_INFERENCE_PROVIDER") or "auto"
        self.timeout = timeout
        self._model = None
        if not self._token or not self.model_id:
            raise ModelGatewayError("model_unconfigured", "Для HF нужны HF_TOKEN и HF_MODEL_ID; модель заранее не выбирается.")

    def _get_model(self):
        if self._model is None:
            try:
                from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint
            except ImportError:
                raise ModelGatewayError("model_unavailable", "Установите дополнительную зависимость langchain-huggingface.") from None
            endpoint = HuggingFaceEndpoint(repo_id=self.model_id, provider=self.provider,
                                          huggingfacehub_api_token=self._token, task="text-generation",
                                          max_new_tokens=1500, timeout=self.timeout)
            self._model = ChatHuggingFace(llm=endpoint)
        return self._model

    async def interpret(self, text: str, request: dict, now: str, timezone: str,
                        skills: list[dict] | None = None) -> dict:
        from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
        model = self._get_model()
        tool = {"type": "function", "function": {"name": "interpret_trip", "description": "Извлечь изменения поездки без внешних действий.", "parameters": OUTPUT_SCHEMA}}
        messages = [SystemMessage(content=SYSTEM_INSTRUCTIONS), HumanMessage(content=json.dumps(
            {"now": now, "timezone": timezone, "current_request": request,
             "active_skills": [{"name": s["name"], "body": s["body"]} for s in (skills or [])],
             "user_message": text}, ensure_ascii=False))]
        bound = model.bind_tools([tool], tool_choice="interpret_trip")
        for attempt in range(2):
            try:
                response = await asyncio.wait_for(bound.ainvoke(messages), self.timeout)
            except Exception:
                raise ModelGatewayError("model_failed", "HF не выполнил запрос. Проверьте доступ модели и провайдера.") from None
            calls = response.tool_calls or []
            try:
                if len(calls) != 1 or calls[0]["name"] != "interpret_trip":
                    raise ValueError("Expected a single interpretation call")
                result = Interpretation.model_validate(_without_nulls(calls[0]["args"]))
            except (ValueError, KeyError, TypeError):
                if attempt:
                    raise ModelGatewayError("model_invalid_response", "HF дважды вернул некорректный разбор запроса.") from None
                messages.append(HumanMessage(content="Исправь ответ: нужен ровно один вызов interpret_trip по схеме, без дополнительных инструментов."))
                continue
            # Return the validated local tool result to its exact tool_call_id.
            tool_result = ToolMessage(content=json.dumps(result.as_result(), ensure_ascii=False), tool_call_id=calls[0]["id"])
            try:
                await asyncio.wait_for(model.ainvoke(messages + [response, tool_result]), self.timeout)
            except Exception:
                raise ModelGatewayError("model_failed", "HF не завершил цикл вызова инструмента; состояние поездки не изменено.") from None
            return result.as_result()
        raise ModelGatewayError("model_invalid_response", "Модель не вернула корректный запрос.")


    async def suggest(self, context: dict) -> list[dict]:
        """One structured completion; suggestion drafts never execute an action."""
        from langchain_core.messages import HumanMessage, SystemMessage
        from travel_agent.graph.suggestions import SUGGESTION_INSTRUCTIONS, SUGGESTION_SCHEMA

        serialized = json.dumps(context, ensure_ascii=False)
        if len(serialized) + len(SUGGESTION_INSTRUCTIONS) > 50000:
            raise ModelGatewayError("context_too_large", "Контекст поездки превышает лимит генерации подсказок.")
        tool = {"type": "function", "function": {
            "name": "suggest_messages", "description": "Предложить до трех черновиков следующего сообщения пользователя.",
            "parameters": SUGGESTION_SCHEMA,
        }}
        messages = [SystemMessage(content=SUGGESTION_INSTRUCTIONS), HumanMessage(content=serialized)]
        try:
            model = self._get_model()
            bound = model.bind_tools([tool], tool_choice="suggest_messages")
            response = await asyncio.wait_for(bound.ainvoke(messages), min(20.0, self.timeout))
        except ModelGatewayError:
            raise
        except TimeoutError:
            raise ModelGatewayError("model_timeout", "HF не успел подготовить подсказки.") from None
        except Exception:
            raise ModelGatewayError("model_failed", "HF не выполнил запрос подсказок.") from None
        try:
            calls = response.tool_calls or []
            if len(calls) != 1 or calls[0]["name"] != "suggest_messages":
                raise ValueError("Expected one suggestion call")
            suggestions = calls[0]["args"]["suggestions"]
            if not isinstance(suggestions, list):
                raise ValueError("Expected a suggestions list")
            return suggestions
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ModelGatewayError("model_invalid_response", "HF вернул некорректные подсказки следующего сообщения.") from None

    async def research(self, context: dict) -> dict:
        from langchain_core.messages import HumanMessage, SystemMessage
        from travel_agent.research import WEB_RESEARCH_INSTRUCTIONS, WEB_RESEARCH_SCHEMA
        model = self._get_model()
        tool = {"type": "function", "function": {"name": "summarize_search", "description": "Кратко объяснить результаты Яндекса со ссылками на источники.", "parameters": WEB_RESEARCH_SCHEMA}}
        messages = [SystemMessage(content=WEB_RESEARCH_INSTRUCTIONS), HumanMessage(content=json.dumps(context, ensure_ascii=False))]
        response = await asyncio.wait_for(model.bind_tools([tool], tool_choice="summarize_search").ainvoke(messages), min(15.0, self.timeout))
        calls = response.tool_calls or []
        if len(calls) != 1 or calls[0]["name"] != "summarize_search":
            raise ModelGatewayError("model_invalid_response", "Не удалось составить ответ по источникам Яндекса.")
        return calls[0]["args"]

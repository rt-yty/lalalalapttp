"""One async boundary for CLI and a future HTTP backend."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta
import inspect
import os
import re
from time import perf_counter
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import ValidationError

from .domain.models import SessionSnapshot, TripRequest, TurnInput, TurnResult
from .domain.planning import check_components, diff_plans, options_from_search, plan_hash, rub, stable_components, refresh_plan_explanation, describe_components, component_options_from_search, option_from_components, component_matches_request
from .storage.sqlite import fingerprint
from .storage.factory import create_repository as Repository
from .skill_loader import SkillRegistry
from .graph.suggestions import suggest_next_messages
from .dialogue import bound_dialogue, turn_text
from .domain.clarifications import next_trip_question, normalize_clarification, normalize_session_question, answered_slot_question, optional_preferences_question

EVENT_CHECKOUT_QUESTION = "Останетесь ли вы в городе после выезда из отеля, чтобы попасть на концерт?"


class GraphState(TypedDict, total=False):
    session: dict
    turn: dict
    decision: dict
    result: dict


class AgentService:
    _locks: dict[tuple, asyncio.Lock] = {}

    def __init__(self, db_path="data/agent.sqlite", *, data_mode="replay", checkout_mode="demo", gateway=None, adapter=None, turn_timeout=120, history_loader=None, web_search_adapter=None, web_reader=None, profile_loader=None):
        if data_mode not in ("replay", "live"):
            raise ValueError("data_mode должен быть replay или live")
        if checkout_mode not in ("demo", "disabled"):
            raise ValueError("Неизвестный режим оформления")
        self.data_mode, self.checkout_mode = data_mode, checkout_mode
        self.repo = Repository(db_path)
        self.db_path = str(db_path)
        self.checkpoint_db = ":memory:" if str(db_path) == ":memory:" or str(db_path).startswith(("postgresql://", "postgres://")) else str(db_path) + ".checkpoints"
        if gateway is None:
            from .adapters.model_codex import CodexModelGateway
            gateway = CodexModelGateway()
        if adapter is None:
            if data_mode == "replay":
                from .adapters.replay import ReplayAdapter
                adapter = ReplayAdapter()
            else:
                from .adapters.tutu_mcp import TutuAdapter
                adapter = TutuAdapter(url=os.getenv("TUTU_MCP_URL", "https://mcp.tutu.ru/mcp"))
        self.gateway, self.adapter = gateway, adapter
        self.registry = SkillRegistry()
        self.turn_timeout = turn_timeout
        self.history_loader = history_loader
        self.profile_loader = profile_loader
        if web_search_adapter is None:
            from .adapters.search_yandex import YandexSearchAdapter
            web_search_adapter = YandexSearchAdapter()
        self.web_search_adapter = web_search_adapter
        if web_reader is None:
            from .adapters.web_reader import WebPageReader
            web_reader = WebPageReader()
        self.web_reader = web_reader

    def close(self):
        self.repo.close()

    async def get_session(self, user_id, session_id):
        state = self.repo.session(user_id, session_id)
        if state is None:
            raise KeyError("Сессия не найдена для этого пользователя")
        state["options"] = [o for o in state.get("options", []) if o.get("eligibility") == "satisfied"]
        return SessionSnapshot.model_validate(state)

    def _error(self, turn, version, code, message, retryable=False):
        from .response_policy import public_text
        message = public_text(message)
        return TurnResult(request_id=turn.request_id, session_id=turn.session_id, state_version=version, status="error", message=message, error={"code": code, "retryable": retryable, "explanation": message})

    async def run_turn(self, request: TurnInput, *, on_event=None) -> TurnResult:
        turn = TurnInput.model_validate(request)
        key = (str(Path(self.db_path).absolute()), turn.user_id, turn.session_id)
        async with self._locks.setdefault(key, asyncio.Lock()):
            digest = fingerprint(turn.model_dump(mode="json"))
            prior = self.repo.turn(turn.user_id, turn.session_id, turn.request_id)
            if prior:
                if prior[0] != digest:
                    return self._error(turn, prior[1]["state_version"], "request_id_conflict", "Этот request_id уже использован для другого запроса.")
                return TurnResult.model_validate(prior[1])
            stored = self.repo.session(turn.user_id, turn.session_id)
            actual = stored["state_version"] if stored else 0
            if actual != turn.expected_state_version:
                return self._error(turn, actual, "state_conflict", "Состояние уже изменилось. Загрузите текущую сессию и повторите новый запрос.")
            state = SessionSnapshot.model_validate(stored) if stored else SessionSnapshot(user_id=turn.user_id, session_id=turn.session_id, data_mode=self.data_mode, checkout_mode=self.checkout_mode)
            if stored is None and self.profile_loader:
                profile = self.profile_loader(turn.user_id)
                if inspect.isawaitable(profile):
                    profile = await profile
                if isinstance(profile, dict):
                    origin = profile.get("origin")
                    preferences = profile.get("preferences")
                    state.profile_context = {
                        "origin": origin.strip()[:120] if isinstance(origin, str) else None,
                        "preferences": [p.strip()[:200] for p in preferences[:12] if isinstance(p, str) and p.strip()] if isinstance(preferences, list) else [],
                    }
                    if state.profile_context["origin"]:
                        state.request.origin = state.profile_context["origin"]
            state.options = [o for o in state.options if o.get("eligibility") == "satisfied"]
            if state.data_mode != self.data_mode or state.checkout_mode != self.checkout_mode:
                return self._error(turn, actual, "mode_conflict", "Для смены режима создайте новую сессию.")
            # Legacy frontends can supply earlier dialogue without rewriting
            # receipts. It becomes durable with the next successful turn.
            if not state.dialogue and self.history_loader:
                state.dialogue = bound_dialogue(self.history_loader(turn.user_id, turn.session_id))
            from .domain.search_diagnostics import normalize_search_session
            state = SessionSnapshot.model_validate(normalize_search_session(normalize_session_question(state.model_dump(mode="json"))))
            events = []
            started = perf_counter()

            async def emit(kind, payload):
                event = {"event_id": str(uuid4()), "run_id": turn.request_id, "sequence": len(events) + 1, "timestamp": datetime.now(turn.now.tzinfo).isoformat(), "elapsed_ms": round((perf_counter() - started) * 1000), "type": kind, "payload": payload}
                events.append(event)
                if on_event:
                    try:
                        maybe = on_event(event)
                        if inspect.isawaitable(maybe):
                            await maybe
                    except Exception:
                        pass  # A disconnected event consumer cannot alter domain operations.

            async def interpret_node(g):
                await emit("stage_changed", {"stage": "interpret"})
                s = SessionSnapshot.model_validate(g["session"])
                names = ["replan-trip" if s.current_plan or s.plan_history else "plan-trip"]
                if turn.input in ("confirm_action", "reject_action"):
                    names = ["finalize-trip"]
                elif turn.input in ("select_option", "select_component"):
                    names = ["compare-offers"]
                skills = [self.registry.load(name) for name in names]
                s.active_skills = [{"name": x["name"], "version": x["version"]} for x in skills]
                for skill in s.active_skills:
                    await emit("skill_loaded", skill)
                if turn.input == "message":
                    text = turn.payload.get("text", "")
                    if not isinstance(text, str) or not text.strip() or len(text) > 12000:
                        raise ValueError("Нужно непустое сообщение длиной до 12 000 символов")
                    model_context = s.request.model_dump(mode="json")
                    if s.profile_context:
                        model_context["_profile"] = s.profile_context
                    model_context["_dialogue"] = {"history": s.dialogue, "last_question": s.last_question, "has_current_plan": s.current_plan is not None, "awaiting_confirmation": s.pending_action is not None, "options": [{"number": i, "title": o["title"], "total_minor": o["total_minor"], "eligibility": o["eligibility"]} for i, o in enumerate(s.options, 1)]}
                    for key in ("transport_options", "hotel_options", "selected_components"):
                        values = getattr(s, key)
                        counts = {}
                        compact = []
                        for item in values:
                            kind = item["product_type"]
                            counts[kind] = counts.get(kind, 0) + 1
                            compact.append({**{k: item.get(k) for k in ("component_id", "product_type", "title", "price_minor", "conditions", "departure_at", "arrival_at")}, "number": counts[kind]})
                        model_context["_dialogue"][key] = compact
                    from .web_research import search_context
                    model_context["_web_search"] = search_context(s.last_web_search)
                    model_context["_event_choices"] = s.event_choices
                    model_context["_selected_event"] = s.selected_event
                    model_context["_concert_intake"] = s.concert_intake.model_dump(mode="json")
                    model_context["_search_diagnostics"] = s.search_diagnostics
                    model_context["_capabilities"] = {"web_search": "yandex", "web_search_configured": bool(getattr(self.web_search_adapter, "configured", False)), "travel_data_mode": self.data_mode}
                    from .response_policy import policy_decision
                    guarded = policy_decision(text, model_context)
                    event_match = re.search(r"(?:выбираю|беру|выбрать)\s+(?:концерт|событие)\s*(?:№|номер)?\s*(\d+)\b", text, re.I) if s.event_choices else None
                    event_alternative = re.search(r"(?:другие|ещ[её]|позже|не сегодня).{0,25}(?:концерт|даты|города)", text, re.I)
                    general_alternative = re.fullmatch(r"(?:а\s+)?(?:(?:есть|покажи|найди|предложи)\s+)?(?:другие|ещ[её]|что\s+ещ[её])(?:\s+варианты)?[?!. ]*", text.strip(), re.I)
                    wants_other_events = bool(s.event_choices and (event_alternative or general_alternative and not s.transport_options and not s.hotel_options))
                    if guarded:
                        decision = guarded
                        if str(guarded.get("question") or "").startswith("Бюджет должен быть больше нуля"):
                            # An invalid amount must not erase separately valid
                            # route, party and dates already supplied in this turn.
                            from .interpretation import RuleBasedGateway
                            valid_text = re.sub(
                                r"((?:бюджет(?:ом)?(?:\s+теперь)?\s*[:—]?\s*(?:до\s+)?|(?:не более|до|по)\s+))[-−]\s*(?=\d)",
                                r"\1", text, flags=re.I)
                            valid_text = re.sub(
                                r"[-−](?=\s*\d[\d \u00a0\u202f]*(?:[.,]\d+)?\s*(?:тыс|руб|₽|за\s+ночь|в\s+ночь))",
                                "", valid_text, flags=re.I)
                            recovered = await RuleBasedGateway().interpret(valid_text, model_context, turn.now.isoformat(), turn.timezone)
                            keep = {"origin", "destination", "departure_date", "return_date", "adults", "children_ages", "pets", "products", "constraints"}
                            decision = {**guarded, "patch": {key: value for key, value in (recovered.get("patch") or {}).items() if key in keep}}
                        elif str(guarded.get("question") or "").startswith("Я пока не подбираю поездки для детей без взрослых"):
                            # Preserve independently explicit route/dates/child ages
                            # while rejecting only the invalid adult count.
                            from .interpretation import RuleBasedGateway
                            valid_text = re.sub(r"\b(?:0|ноль)\s+взросл\w*", "", text, flags=re.I)
                            recovered = await RuleBasedGateway().interpret(valid_text, model_context, turn.now.isoformat(), turn.timezone)
                            keep = {"origin", "destination", "departure_date", "return_date", "children_ages", "pets", "budget_minor", "budget_scope", "products", "constraints"}
                            decision = {**guarded, "patch": {key: value for key, value in (recovered.get("patch") or {}).items() if key in keep}}
                    elif s.last_question == EVENT_CHECKOUT_QUESTION and re.fullmatch(r"(?:да(?:[, ]+(?:останусь|останусь в городе|останусь в городе после выезда))?|останусь|останусь в городе(?: после выезда)?)[.! ]*", text.strip(), re.I):
                        from .domain.planning import event_checkout_key
                        s.request = s.request.model_copy(update={"event_checkout_ack": event_checkout_key(s.request.model_dump(mode="json"))})
                        decision = {"intent": "replan", "patch": {}}
                    elif s.last_question == EVENT_CHECKOUT_QUESTION and re.fullmatch(r"(?:нет|нет, не останусь|не останусь)[.! ]*", text.strip(), re.I):
                        decision = {"intent": "replan", "patch": {"return_date": None, "event_checkout_ack": None}}
                    elif event_match:
                        index = int(event_match[1]) - 1
                        decision = {"intent": "select_event", "event_id": s.event_choices[index]["event_id"] if 0 <= index < len(s.event_choices) else None, "patch": {}}
                    elif wants_other_events:
                        query = str((s.last_web_search or {}).get("query") or " ".join(event.get("artist", "") for event in s.event_choices[:1]))
                        decision = {"intent": "web_search", "web_query": (query[:280] + " другие даты города полное расписание тура")[:400], "other_events": True, "patch": {}}
                    elif text.strip().casefold().rstrip(".!?") == "подбери новые варианты по тем же пожеланиям":
                        decision = {"intent": "replan", "patch": {}, "refresh_options": True}
                    elif text.strip().casefold().rstrip(".!?") == "повтори поиск в интернете" and s.last_web_search:
                        decision = {"intent": "web_search", "web_query": s.last_web_search["query"], "patch": {}, "warnings": [], "retain_event_choices": True}
                    else:
                        decision = await self.gateway.interpret(text, model_context, turn.now.isoformat(), turn.timezone, skills=skills)
                    from .interpretation import apply_capability_guard
                    decision = apply_capability_guard(text, model_context, decision, now=turn.now)
                    if not decision.get("read_only_research"):
                        decision = self._collect_concert_preferences(s, text, decision)
                else:
                    intent = {"select_event": "select_event", "select_option": "select", "select_component": "select_component", "remove_component": "remove_component", "confirm_action": "confirm", "reject_action": "reject", "undo": "undo", "update_preference": "unsupported"}[turn.input]
                    decision = {"intent": intent, "patch": {}, "warnings": []}
                return {"session": s.model_dump(mode="json"), "decision": decision}

            async def search_node(g):
                s = SessionSnapshot.model_validate(g["session"])
                previous_dates = (s.request.departure_date, s.request.return_date)
                out = await self._search(s, turn, g["decision"], emit)
                dates = (s.request.departure_date, s.request.return_date)
                text = str(turn.payload.get("text") or "")
                if (all(dates) and dates != previous_dates
                        and re.search(r"дат[аы][^.!?]{0,25}(?:люб|гибк)|люб[^.!?]{0,20}дат|(?:выбери|подбери)[^.!?]{0,20}дат", text, re.I)):
                    start, end = (day.strftime("%d.%m.%Y") for day in dates)
                    out["message"] = f"Для начала беру даты {start}–{end}. " + out["message"]
                return {"session": s.model_dump(mode="json"), "result": out}

            async def action_node(g):
                s = SessionSnapshot.model_validate(g["session"])
                out = await self._action(s, turn, g["decision"], emit)
                return {"session": s.model_dump(mode="json"), "result": out}

            async def web_search_node(g):
                s = SessionSnapshot.model_validate(g["session"])
                out = await self._web_research(s, turn, g["decision"], emit, deadline)
                return {"session": s.model_dump(mode="json"), "result": out}

            deadline = asyncio.get_running_loop().time() + self.turn_timeout

            async def suggestions_node(g):
                remaining = max(0.0, deadline - asyncio.get_running_loop().time() - 0.25)
                return await suggest_next_messages(g, gateway=self.gateway, emit=emit, timeout=min(20.0, remaining))

            graph = StateGraph(GraphState)
            graph.add_node("interpret", interpret_node)
            graph.add_node("search_validate_rank", search_node)
            graph.add_node("act_or_answer", action_node)
            graph.add_node("web_search", web_search_node)
            graph.add_node("suggest_next_messages", suggestions_node)
            graph.add_edge(START, "interpret")
            graph.add_conditional_edges("interpret", lambda g: "web_search" if g["decision"].get("intent") == "web_search" else "search_validate_rank" if g["decision"].get("intent") in ("plan", "replan") else "act_or_answer")
            graph.add_edge("search_validate_rank", "suggest_next_messages")
            graph.add_edge("act_or_answer", "suggest_next_messages")
            graph.add_edge("web_search", "suggest_next_messages")
            graph.add_edge("suggest_next_messages", END)
            try:
                async with asyncio.timeout(self.turn_timeout):
                    async with AsyncSqliteSaver.from_conn_string(self.checkpoint_db) as checkpointer:
                        compiled = graph.compile(checkpointer=checkpointer)
                        g = await compiled.ainvoke({"session": state.model_dump(mode="json"), "turn": turn.model_dump(mode="json")}, {"configurable": {"thread_id": fingerprint([turn.user_id, turn.session_id, turn.request_id])}, "recursion_limit": 12})
                state = SessionSnapshot.model_validate(g["session"])
                state.state_version = actual + 1
                from .response_policy import public_result
                out = public_result(g["result"])
                out.setdefault("search_diagnostics", state.search_diagnostics)
                state.dialogue = bound_dialogue([*state.dialogue,
                    {"role": "user", "text": turn_text(turn, state)},
                    {"role": "assistant", "text": out["message"]}])
                result = TurnResult(request_id=turn.request_id, session_id=turn.session_id, state_version=state.state_version, current_plan=state.current_plan, pending_action=state.pending_action, options=state.options, transport_options=state.transport_options, hotel_options=state.hotel_options, selected_components=state.selected_components, component_errors=state.component_errors, search_coverage=state.search_coverage, event_choices=state.event_choices, event_choices_version=state.event_choices_version, selected_event=state.selected_event, **out)
                status, saved = self.repo.commit(turn, digest, state.model_dump(mode="json"), result.model_dump(mode="json"), events)
                if status in ("committed", "repeat"):
                    final = TurnResult.model_validate(saved)
                    if final.status == "completed_demo":
                        await emit("completed", {"local_id": final.demo_order["local_id"]})
                    return final
                return self._error(turn, actual, status, "Конкурирующий запрос изменил состояние; загрузите сессию заново.")
            except TimeoutError:
                return self._error(turn, actual, "timeout", "Превышено время ожидания. Состояние поездки не изменено; можно повторить запрос.", True)
            except (ValueError, ValidationError) as exc:
                # Validation reports may contain model values, field names and
                # raw input; none of those are appropriate traveller-facing text.
                message = ("Я не смог разобрать сообщение. Проверьте даты и состав путешественников и попробуйте ещё раз."
                           if isinstance(exc, ValidationError) else
                           "Я не смог принять этот запрос. Уточните сообщение и попробуйте ещё раз.")
                return self._error(turn, actual, "invalid_input", message)
            except Exception as exc:
                code = getattr(exc, "code", "dependency_error")
                message = "Я пока не смог обработать сообщение. Попробуйте ещё раз."
                return self._error(turn, actual, code, message, True)

    @staticmethod
    def _out(status, message, **kw):
        return {"status": status, "message": message, **kw}

    async def _skill(self, s, name, emit):
        skill = self.registry.load(name)
        entry = {"name": skill["name"], "version": skill["version"]}
        if entry not in s.active_skills:
            s.active_skills.append(entry)
            await emit("skill_loaded", entry)

    def _invalidate(self, s, *, clear_components=True):
        s.search_diagnostics = None
        s.search_coverage = []
        if s.current_plan:
            s.plan_history.append(deepcopy(s.current_plan))
            s.plan_history = s.plan_history[-20:]
        s.current_plan = None
        s.pending_action = None
        s.demo_order = None
        s.options = []
        if clear_components:
            s.transport_options = []
            s.hotel_options = []
            s.selected_components = []
            s.component_errors = {}

    def _collect_concert_preferences(self, s, text, decision):
        from .concert_intake import update_intake, missing_preferences, intake_message, research_query, EVENT, ConcertIntakeState, cancels_intake
        if s.concert_intake.mode != "inactive" and cancels_intake(text):
            s.concert_intake = ConcertIntakeState()
            s.last_question = None
            return {"intent": "chat", "patch": {}, "reply_text": "Понял, остановлю подбор поездки на концерт."}
        if decision.get("intent") not in {"plan", "replan", "web_search"}:
            return decision
        # An unrelated informational question may be answered without turning
        # it into a response to the unfinished concert questionnaire.
        if (decision.get("intent") == "web_search" and not EVENT.search(text)
                and not EVENT.search(str(decision.get("web_query") or ""))
                and not decision.get("concert_intake")):
            return decision
        patch = decision.get("patch") or {}
        current = s.request.model_dump(mode="json")
        if decision.get("reset_dates"):
            current.update(departure_date=None, return_date=None)
        merged = {**current, **patch}
        if "constraints" in patch:
            merged["constraints"] = {**current.get("constraints", {}), **patch["constraints"]}
        previous_name = s.concert_intake.event_name
        state = update_intake(s.concert_intake, text, decision, merged)
        if state.mode == "inactive":
            s.concert_intake = state
            return decision
        s.concert_intake = state
        if previous_name and state.event_name and previous_name.casefold() != state.event_name.casefold() and s.selected_event:
            s.selected_event = None
            s.event_choices = []
            s.event_choices_version += 1
            self._invalidate(s)
            for key in ("destination", "event_date", "event_start_at", "departure_date", "return_date"):
                if key not in patch:
                    merged[key] = None
            s.request = TripRequest.model_validate(merged)
        if state.budget_flexible:
            merged.update(budget_minor=None, budget_scope=None)
            decision = {**decision, "patch": {**patch, "budget_minor": None, "budget_scope": None}}
        if (merged.get("budget_minor") and not merged.get("budget_scope")
                and merged.get("adults") == 1 and not merged.get("children_ages") and not state.children_pending):
            merged["budget_scope"] = "trip"
            decision = {**decision, "patch": {**(decision.get("patch") or {}), "budget_scope": "trip"}}
        proposed = TripRequest.model_validate(merged)
        missing = missing_preferences(state, proposed.model_dump(mode="json"))
        if missing:
            if proposed != s.request:
                self._invalidate(s)
                s.request = proposed
            return {"intent": "concert_intake", "question": intake_message(state, missing), "patch": {}}
        if state.mode == "trip" and not s.selected_event and not (proposed.event_date and proposed.destination):
            return {**decision, "intent": "web_search", "question": None,
                    "web_query": research_query(state, proposed.model_dump(mode="json"))}
        if state.mode == "information" and EVENT.search(text):
            return {**decision, "intent": "web_search", "question": None,
                    "web_query": research_query(state, proposed.model_dump(mode="json"))}
        return decision

    async def _web_research(self, s, turn, decision, emit, deadline):
        from .research import answer_from_search
        from .web_research import enrich_search, public_search
        query = decision.get("web_query")
        if not isinstance(query, str) or not query.strip() or len(query) > 400:
            return self._out("needs_input", "Что найти в интернете? Укажите событие, исполнителя или вопрос.", questions=["Что найти в интернете?"])
        from .event_research import event_query
        if (event_query(query) and s.event_choices and not decision.get("other_events")
                and not decision.get("retain_event_choices") and not decision.get("read_only_research")):
            # A fresh artist/event request must not expose the old artist's
            # selectable cards if the new lookup fails or returns no dates.
            s.event_choices = []
            s.event_choices_version += 1
            s.selected_event = None
            self._invalidate(s)
            s.request = s.request.model_copy(update={"destination": None, "event_date": None, "event_start_at": None,
                                                      "departure_date": None, "return_date": None})
        read_only = bool(decision.get("read_only_research"))
        patch = {} if read_only else dict(decision.get("patch") or {})
        from .interpretation import nightly_budget, explicit_budget
        user_text = "" if read_only else str(turn.payload.get("text") or "")
        nightly = nightly_budget(user_text)
        if nightly is not None:
            patch.update(nightly_budget_minor=nightly[0], budget_scope=nightly[1])
        elif (grounded := explicit_budget(user_text)) is not None:
            patch["budget_minor"] = grounded[0]
            patch["nightly_budget_minor"] = None
            if grounded[1] is not None:
                patch["budget_scope"] = grounded[1]
        elif "budget_minor" in patch and re.search(r"бюджет|руб|₽|тысяч|тыс", user_text, re.I):
            patch["nightly_budget_minor"] = None
        current = s.request.model_dump(mode="json")
        if decision.get("reset_dates") and not read_only:
            current.update(departure_date=None, return_date=None)
        if "event_date" in patch and "event_start_at" not in patch:
            current["event_start_at"] = None
        merged = {**current, **patch}
        if "constraints" in patch:
            merged["constraints"] = {**current.get("constraints", {}), **patch["constraints"]}
        updated = TripRequest.model_validate(merged)
        if updated != s.request:
            self._invalidate(s)
            s.request = updated
        await emit("stage_changed", {"stage": "web_search"})
        await emit("tool_started", {"operation": "web_search", "provider": "yandex", "query": query})
        search = await self.web_search_adapter.search(query, turn.now.isoformat())
        retry_after = ((search.get("error") or {}).get("diagnostics") or {}).get("retry_after_seconds")
        if ((search.get("error") or {}).get("code") == "search_rate_limited" and deadline - asyncio.get_running_loop().time() > 35
                and (retry_after is None or isinstance(retry_after, (int, float)) and retry_after <= 5)):
            await asyncio.sleep(max(1.2, float(retry_after or 0)))
            search = await self.web_search_adapter.search(query, turn.now.isoformat())
        s.last_web_search = search
        await emit("tool_finished", {"operation": "web_search", "provider": "yandex", "count": len(search.get("results", [])), "error": (search.get("error") or {}).get("code")})
        if search.get("error"):
            error = search["error"]
            message = error["message"]
            if error["code"] == "search_unconfigured":
                message = "Я пока не могу выполнить поиск в интернете. Запрос я сохранил — к нему можно вернуться позже."
            elif error["code"] == "search_rate_limited":
                message = "Я пока не смог продолжить поиск. Запрос я сохранил; попробуйте повторить его немного позже."
            return self._out("partial", message, web_search=search, error=error)
        dialogue = s.dialogue + [{"role": "user", "text": turn.payload.get("text", "")}]
        enriched = await enrich_search(self.gateway, self.web_search_adapter, self.web_reader,
                                       search, dialogue, turn.now, deadline=deadline, emit=emit)
        if not enriched.get("results"):
            search = public_search(enriched)
            s.last_web_search = search
            message = ("Я не нашёл афишу по этому запросу. Уточните название события или исполнителя."
                       if re.search(r"концерт|афиш|фестивал|выступлен|тур\b", query, re.I) else
                       "Я не нашёл достаточно сведений по этому запросу. Как уточнить вопрос?")
            return self._out("needs_input", message, web_search=search, questions=["Как уточнить поисковый запрос?"])
        remaining = max(0, deadline - asyncio.get_running_loop().time() - 0.5)
        answer = await answer_from_search(self.gateway, enriched, dialogue, turn.now, timeout=min(25, remaining),
            event_preferences={**s.concert_intake.model_dump(mode="json"), "request": s.request.model_dump(mode="json")})
        if "event_choices" in answer:
            events = answer["event_choices"]
            if decision.get("other_events"):
                previous_ids = {event["event_id"] for event in s.event_choices}
                events = [event for event in events if event["event_id"] not in previous_ids]
                if not events:
                    answer["message"] = "Я пока не подтвердил другие будущие даты. Уже найденные концерты я сохранил; могу продолжить поиск по другому периоду или стране."
            if events or not decision.get("other_events"):
                from .event_research import event_message
                s.event_choices = events
                s.event_choices_version += 1
                answer["message"] = event_message(events)
        search = {**public_search(enriched), "source_ids": answer["source_ids"]}
        s.last_web_search = search
        s.last_question = None
        return self._out("research_ready", answer["message"], web_search=search, evidence=[r["url"] for r in search["results"] if r["id"] in answer["source_ids"]])

    async def _search(self, s, turn, decision, emit):
        from .domain.search_diagnostics import build_search_diagnostics, format_search_diagnostics
        # A new lookup cannot retain permission to confirm the old quote, even
        # when its request parameters are unchanged and the provider fails.
        s.pending_action = None
        old_request = s.request.model_dump(mode="json")
        previous_selected = deepcopy(s.selected_components or (s.current_plan or {}).get("components", []))
        old_cards = deepcopy(s.transport_options + s.hotel_options)
        question_answer = self._answer_question(s, decision.get("question_text", "")) if decision.get("question_text") else ""
        if decision.get("refresh_options"):
            self._invalidate(s, clear_components=False)
            previous_selected = []
        patch = dict(decision.get("patch") or {})
        from .interpretation import nightly_budget, explicit_budget
        user_text = str(turn.payload.get("text") or "")
        nightly = nightly_budget(user_text)
        if nightly is not None:
            patch.update(nightly_budget_minor=nightly[0], budget_scope=nightly[1])
        elif (grounded := explicit_budget(user_text)) is not None:
            patch["budget_minor"] = grounded[0]
            patch["nightly_budget_minor"] = None
            if grounded[1] is not None:
                patch["budget_scope"] = grounded[1]
        elif "budget_minor" in patch and re.search(r"бюджет|руб|₽|тысяч|тыс", user_text, re.I):
            patch["nightly_budget_minor"] = None
        current = s.request.model_dump(mode="json")
        if decision.get("reset_dates"):
            current.update(departure_date=None, return_date=None)
        if "event_date" in patch and "event_start_at" not in patch:
            current["event_start_at"] = None
        if decision.get("keep_hotel") and s.current_plan:
            pinned = next((c for c in s.current_plan["components"] if c["product_type"] == "hotel"), None)
            if pinned:
                s.pins["hotel"] = pinned["title"]
        merged = {**current, **patch}
        if "constraints" in patch:
            merged["constraints"] = {**current.get("constraints", {}), **patch["constraints"]}
        if any(kind in merged.get("products", ["avia", "hotel"]) for kind in ("avia", "rail", "bus")):
            from .interpretation import _city
            origin, destination = merged.get("origin"), merged.get("destination")
            if isinstance(origin, str) and isinstance(destination, str) and _city(origin).casefold() == _city(destination).casefold():
                merged["destination"] = None
                decision = {**decision, "question": "Город отправления и назначения совпадают. В какой другой город хотите поехать?"}
        from .domain.input_validation import calendar_correction, same_day_trip_requested
        corrected_dates, calendar_question = calendar_correction(user_text, merged, turn.now.date(), last_question=s.last_question)
        if calendar_question:
            merged.update(corrected_dates)
            decision = {**decision, "question": calendar_question}
            if not old_cards and not previous_selected:
                question_answer = ""
        same_day_requested = (same_day_trip_requested(user_text)
                              and any(kind in merged.get("products", []) for kind in ("avia", "rail", "bus")))
        if same_day_requested:
            merged["return_date"] = None
            decision = {**decision, "question": "Сейчас я могу подобрать поездку с возвращением не раньше следующего дня. Рассмотреть такой вариант?"}
            if not old_cards and not previous_selected:
                question_answer = ""
        no_children = re.search(r"без (?:детей|реб[её]нка)|(?:детей|реб[её]нка)\s+нет|"
                                r"нет\s+(?:детей|реб[её]нка)|(?:дети|реб[её]нок)\s+не\s+(?:едут|едет|поедут|поедет)", user_text, re.I)
        if no_children:
            s.children_pending = False
        elif re.search(r"\b(?:реб[её]нок|реб[её]нком|реб[её]нка|дети|детей|детьми)\b", user_text, re.I) and not merged.get("children_ages"):
            # An empty model list does not override an explicitly mentioned
            # child whose age still needs to be collected.
            s.children_pending = True
        elif "children_ages" in patch:
            s.children_pending = False
        if s.children_pending and "без взрослых" not in str(decision.get("question") or ""):
            decision = {**decision, "question": "Сколько лет каждому ребёнку на дату поездки?"}
        # Explain a recognizable date-order mistake before domain validation.
        # Keep volunteered route/party details, but never preserve old prices
        # or search with an invalid interval.
        try:
            departure = date.fromisoformat(str(merged.get("departure_date")))
            returning = date.fromisoformat(str(merged.get("return_date")))
        except ValueError:
            pass
        else:
            if returning <= departure:
                merged["return_date"] = None
                hotel_only = merged.get("products") == ["hotel"]
                noun = "выезда из отеля" if hotel_only else "возвращения"
                anchor = "заезда" if hotel_only else "отправления"
                question = (f"Дата {noun} должна быть позже {anchor} {departure.strftime('%d.%m.%Y')}. "
                            + ("Когда планируете выезд?" if hotel_only else "Когда планируете вернуться?"))
                decision = {**decision, "question": question}
        nightly_rate = merged.get("nightly_budget_minor")
        if ((isinstance(nightly_rate, int) and nightly_rate <= 0)
                or (isinstance(merged.get("budget_minor"), int) and merged["budget_minor"] <= 0)):
            merged.update(budget_minor=current.get("budget_minor"), nightly_budget_minor=current.get("nightly_budget_minor"), budget_scope=current.get("budget_scope"))
            decision = {**decision, "question": "Бюджет должен быть больше нуля. Какую сумму использовать или искать без ограничения по бюджету?"}
        elif nightly_rate is not None:
            if merged.get("products") != ["hotel"]:
                merged["budget_minor"] = None
                decision = {**decision, "question": "Лимит за ночь относится к проживанию. Подобрать сначала только отель с этим лимитом?"}
            else:
                try:
                    nights = (date.fromisoformat(str(merged.get("return_date"))) - date.fromisoformat(str(merged.get("departure_date")))).days
                except ValueError:
                    merged["budget_minor"] = None
                else:
                    merged["budget_minor"] = nightly_rate * nights if nights > 0 else None
                merged["budget_scope"] = merged.get("budget_scope") or "trip"
        # For one known traveller the two scopes are numerically identical;
        # keep the stated ceiling without asking a meaningless distinction.
        if (merged.get("budget_minor") and merged.get("budget_scope") is None
                and merged.get("adults") == 1 and not merged.get("children_ages") and not s.children_pending):
            merged["budget_scope"] = "trip"
        request = TripRequest.model_validate(merged)
        if request != s.request:
            self._invalidate(s, clear_components=False)
            if old_cards:
                s.options_version += 1
            s.transport_options = []
            s.hotel_options = []
            s.request = request
        query = s.request.model_dump(mode="json")
        from .interpretation import event_destination
        matching_event = next((event for event in s.event_choices if query.get("destination") in (event["city"], event_destination(event)) and event["date"] == query.get("event_date")), None)
        if matching_event:
            s.selected_event = deepcopy(matching_event)
        elif s.selected_event and (query.get("destination") not in (s.selected_event["city"], event_destination(s.selected_event)) or s.selected_event["date"] != query.get("event_date")):
            s.selected_event = None
        if query.get("destination") != old_request.get("destination"):
            s.pins = {}
        products = query["products"]
        previous_selected = [c for c in previous_selected if c["product_type"] in products]
        travel_changed = any(query.get(k) != old_request.get(k) for k in ("destination", "departure_date", "return_date", "adults", "children_ages", "pets"))
        s.selected_components = ([] if travel_changed else [c for c in previous_selected
                                 if query.get("origin") == old_request.get("origin") or c["product_type"] == "hotel"])
        s.selected_components = [c for c in s.selected_components if self._valid_component(c, query)]
        if s.selected_components and (not s.current_plan or s.current_plan["request"] != query
                                      or s.current_plan["components"] != s.selected_components):
            self._make_plan(s, option_from_components(s.selected_components, query, s.options_version))
        warnings = list(decision.get("warnings") or [])
        if s.concert_intake.mode == "trip":
            from .concert_intake import missing_preferences, intake_message
            missing = missing_preferences(s.concert_intake, query)
            if missing:
                question = intake_message(s.concert_intake, missing)
                s.last_question = question
                return self._out("needs_input", question, questions=[question], warnings=warnings)
        event_day = str(query.get("event_date") or query.get("event_start_at") or "")[:10]
        if event_day:
            event_label = datetime.fromisoformat(event_day).strftime("%d.%m.%Y")
            if ((query.get("departure_date") and query["departure_date"] > event_day)
                    or (query.get("return_date") and query["return_date"] < event_day)):
                question = f"На какие даты перенести поездку, чтобы попасть на концерт {event_label}?"
                s.last_question = question
                return self._out("needs_input", "Я вижу, что концерт не попадает в выбранные даты поездки. " + question,
                                 questions=[question], warnings=warnings)
            if products == ["hotel"] and query.get("return_date") == event_day:
                from .domain.planning import event_checkout_key
                if query.get("event_checkout_ack") != event_checkout_key(query):
                    s.last_question = EVENT_CHECKOUT_QUESTION
                    return self._out("needs_input", f"Я вижу, что концерт {event_label} совпадает с днём выезда из отеля. " + EVENT_CHECKOUT_QUESTION,
                                     questions=[EVENT_CHECKOUT_QUESTION], warnings=warnings)
        if decision.get("question") and not (answered_slot_question(decision["question"], query)
                                            or optional_preferences_question(decision["question"], query)):
            s.last_question = normalize_clarification(decision["question"], query)
            return self._out("needs_input", (question_answer + "\n\n" if question_answer else "") + s.last_question, questions=[s.last_question], warnings=warnings)
        if not request.destination:
            user_text = turn.payload.get("text", "")
            if re.search(r"на море|у моря|пляж|побереж", user_text, re.I):
                question = "В какой стране или городе хотите отдохнуть у моря?"
                s.last_question = question
                return self._out("needs_input", question, questions=[question], warnings=warnings)
            question = "Куда хотите поехать?"
            s.last_question = question
            return self._out("needs_input", "Идеи для начала: Ереван — городская поездка; Казань — прогулки и кухня; Сочи — отдых у моря. Это идеи, цены и наличие ещё не проверены. " + question, questions=[question], warnings=warnings)
        question = next_trip_question(query)
        if question:
            s.last_question = question
            return self._out("needs_input", question, questions=[question], warnings=warnings)
        if request.departure_date < turn.now.date() and self.data_mode == "live":
            return self._out("needs_input", "Эти даты уже прошли. На какие даты перенести поездку?", questions=["На какие даты перенести поездку?"])
        await emit("stage_changed", {"stage": "search"})
        await emit("tool_started", {"operation": "search", "data_mode": self.data_mode})
        search_products = list(products)
        travel_changed = any(query.get(k) != old_request.get(k) for k in ("destination", "departure_date", "return_date", "adults", "children_ages", "pets"))
        constraint_patch = patch.get("constraints", {})
        transport_keys = {"baggage_kg", "direct", "flight_refundable", "rail_refundable", "seat_category", "service_class", "prefer_shorter", "prefer_daytime"}
        hotel_keys = {"breakfast", "hotel_refundable", "free_cancellation", "hotel_name", "prefer_hotel_rating"}
        if old_cards and not travel_changed and not decision.get("refresh_options"):
            if decision.get("keep_transport") or (constraint_patch and set(constraint_patch) <= hotel_keys):
                search_products = [p for p in products if p == "hotel"]
            elif decision.get("keep_hotel") or (constraint_patch and set(constraint_patch) <= transport_keys) or (set(patch) == {"origin"}):
                search_products = [p for p in products if p != "hotel"]
        search_query = {**query, "products": search_products or products}
        pinned_hotel = next((c for c in previous_selected if c["product_type"] == "hotel"), None)
        if decision.get("keep_hotel") and pinned_hotel:
            hotel_id = pinned_hotel.get("provider_refs", {}).get("hotel_id")
            if hotel_id:
                search_query["_hotel_ids"] = [hotel_id]
        selected_transport = next((c for c in previous_selected if c["product_type"] in ("avia", "rail", "bus")), None)
        if search_products == ["hotel"] and selected_transport and not travel_changed:
            search_query["departure_date"] = selected_transport.get("arrival_at", query["departure_date"])[:10]
        result = await self.adapter.search(search_query, turn.now.isoformat())
        result = deepcopy(result)
        s.component_errors = {**{k: v for k, v in s.component_errors.items() if k in products and k not in search_products},
                              **dict(result.get("component_errors", {}))}
        result["component_errors"] = deepcopy(s.component_errors)
        for kind, key in (("avia", "flights"), ("rail", "trains"), ("bus", "buses"), ("hotel", "hotels")):
            if kind in products and kind not in search_products:
                result[key] = [c for c in old_cards if c["product_type"] == kind]
        await emit("tool_finished", {"operation": "search", "flights": len(result.get("flights", [])), "hotels": len(result.get("hotels", [])), "error": (result.get("error") or {}).get("code")})
        s.search_coverage = result.get("coverage", [])
        s.search_diagnostics = build_search_diagnostics(result, query, turn.now.isoformat())
        warnings += result.get("warnings", [])
        s.options_version += 1
        cards = component_options_from_search(result, query, s.options_version)
        s.transport_options, s.hotel_options = cards["transport_options"], cards["hotel_options"]
        available = s.transport_options + s.hotel_options
        if result.get("error") and not available:
            s.options = []
            s.selected_components = [c for c in s.selected_components if c["product_type"] not in search_products]
            if s.selected_components:
                self._make_plan(s, option_from_components(s.selected_components, query, s.options_version))
            elif s.current_plan:
                s.plan_history.append(deepcopy(s.current_plan))
                s.plan_history = s.plan_history[-20:]
                s.current_plan = s.demo_order = None
            error = result["error"]
            explained = format_search_diagnostics(s.search_diagnostics, data_mode=self.data_mode)
            s.last_question = explained["question"]
            return self._out("partial", (question_answer + "\n\n" if question_answer else "") + explained["message"], error=error, warnings=warnings,
                             questions=[s.last_question] if s.last_question else [])
        rejections = []
        s.options = options_from_search(result, query, s.options_version, s.pins, rejections=rejections)
        # Preserve the exact chosen component when it remains valid. A changed
        # query cannot leave an old quote silently selected.
        selected = []
        for old in previous_selected:
            ref = old.get("provider_refs", {}).get("reference_key")
            fresh = next((c for c in available if c.get("provider_refs", {}).get("reference_key") == ref), None)
            if fresh is not None:
                selected.append(fresh)
            elif old["product_type"] not in search_products and not travel_changed:
                candidate = option_from_components([old], {**query, "products": [old["product_type"]]})
                if candidate["eligibility"] == "satisfied":
                    selected.append(old)
        s.selected_components = selected
        if selected:
            self._make_plan(s, option_from_components(selected, query, s.options_version))
        elif s.current_plan:
            s.plan_history.append(deepcopy(s.current_plan))
            s.plan_history = s.plan_history[-20:]
            s.current_plan = s.pending_action = s.demo_order = None
        s.last_question = None
        if not s.options and not available:
            s.search_diagnostics = build_search_diagnostics(result, query, turn.now.isoformat(), rejections=rejections, pins=s.pins)
            explained = format_search_diagnostics(s.search_diagnostics, data_mode=self.data_mode)
            s.last_question = explained["question"]
            return self._out("partial", (question_answer + "\n\n" if question_answer else "") + explained["message"],
                             questions=[s.last_question] if s.last_question else [],
                             warnings=warnings + s.search_diagnostics.get("reasons", []))
        s.search_diagnostics = (build_search_diagnostics(result, query, turn.now.isoformat(), rejections=rejections, pins=s.pins)
                                if not s.options else None)
        await self._skill(s, "compare-offers", emit)
        await emit("options_updated", {"options_version": s.options_version, "count": len(available)})
        descriptions = []
        labels = {"avia": "перелётов туда и обратно", "rail": "поездок на поезде туда и обратно",
                  "bus": "поездок на автобусе туда и обратно", "hotel": "проживания"}
        for kind in products:
            count = sum(c["product_type"] == kind for c in available)
            if count:
                noun = "вариант" if count % 10 == 1 and count % 100 != 11 else "варианта" if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14) else "вариантов"
                descriptions.append(f"{count} {noun} {labels[kind]}")
        message = "Я нашёл " + " и ".join(descriptions) + ". " + ("Выберите дорогу и проживание." if "hotel" in products and len(products) > 1 else "Выберите подходящий вариант.")
        if selected:
            message += " Сохранил выбранное: " + ", ".join(c["title"] for c in selected) + "."
            if s.current_plan and s.current_plan.get("complete") and s.current_plan["eligibility"] != "satisfied":
                message += " Выбранные части требуют пересмотра: " + " ".join(c["explanation"] for c in s.current_plan["checks"] if c.get("hard") and c["status"] != "satisfied")
        for kind, error in s.component_errors.items():
            message += " " + error.get("message", "Часть поиска не удалось завершить.")
        if question_answer:
            message = question_answer + "\n\n" + message
        if not s.options:
            explained = format_search_diagnostics(s.search_diagnostics, data_mode=self.data_mode)
            message = explained["message"]
            s.last_question = explained["question"]
            warnings += s.search_diagnostics.get("reasons", [])
            if question_answer:
                message = question_answer + "\n\n" + message
        if self.data_mode == "replay":
            message += " Я использую архивные предложения от 26.10.2026; текущие цены и наличие нужно проверить заново."
        return self._out("options_ready" if s.options else "partial", message, questions=[s.last_question] if s.last_question else [], warnings=warnings, evidence=sorted({e for c in available for e in c.get("evidence_refs", [])}))

    def _make_plan(self, s, option, request=None):
        previous = s.current_plan or (s.plan_history[-1] if s.plan_history else None)
        if s.current_plan:
            s.plan_history.append(deepcopy(s.current_plan))
        s.plan_revision += 1
        plan = deepcopy(option)
        plan.update(plan_id=previous["plan_id"] if previous else str(uuid4()), version=s.plan_revision, parent_version=previous["version"] if previous else None, request=request or s.request.model_dump(mode="json"))
        plan["diff"] = diff_plans(previous, plan)
        s.current_plan, s.pending_action, s.demo_order = plan, None, None
        s.selected_components = deepcopy(plan["components"])
        return plan

    @staticmethod
    def _answer_question(s, text):
        """Answer the requested tariff fact without changing the trip or searching."""
        if not text:
            return ""
        query = text.casefold().replace("ё", "е")
        if (getattr(s, "search_diagnostics", None) or getattr(s, "search_coverage", None)) and (
                query.strip(" .!?") == "почему" or re.search(r"меша|не наш|не получ|почему.{0,25}нет|какое.{0,20}услов", query)):
            # A failed full-trip search can still have independent cards. Let
            # the existing diagnostic branch explain that failure, not facts.
            return ""
        selected = s.selected_components or (s.current_plan or {}).get("components", [])
        alternatives = s.transport_options + s.hotel_options
        summary = bool(re.search(r"что.{0,25}(?:выбрано|выбрал|выбрала|входит в (?:стоимость|сумму|поездку))|"
                                 r"(?:состав|итог|собран).{0,20}поездк|итогов.{0,15}(?:сумм|стоим)|"
                                 r"сколько.{0,15}(?:всего|получилось)|"
                                 r"расход.{0,35}(?:вход|учтен|учтён|дополн|пока)|"
                                 r"(?:вход|учтен|учтён|дополн).{0,25}расход", query))
        if summary:
            if not selected:
                return "Я пока не сохранил ни одного выбранного варианта. Выберите дорогу или отель, и я посчитаю сумму."
            total = sum(item["price_minor"] for item in selected)
            lines = ["Я сохранил ваш выбор:"]
            lines.extend(f"• {item['title']} — {rub(item['price_minor'])}." for item in selected)
            lines.append(f"Итого за выбранные варианты: {rub(total)}.")
            lines.append("В эту сумму я включил только выбранные варианты. Трансферы, страховку, питание сверх условий тарифа и личные расходы я ещё не оценил.")
            missing = (s.current_plan or {}).get("missing_products", [])
            if missing:
                lines.append("Я ещё не добавил " + ("проживание." if missing == ["hotel"] else "дорогу."))
            return "\n".join(lines)
        listing = bool(re.search(r"(?:какие|что|покажи|напомни|перечисли).{0,65}(?:нашел|найден|подобрал|подобран)", query))
        comparison = bool(re.search(r"сравн|разниц|отлич|дешевле|дороже", query))
        baggage = bool(re.search(r"багаж|ручн.{0,6}клад", query))
        food = bool(re.search(r"завтрак|питани|полупансион|пансион|все включено", query))
        meal = bool(re.search(r"питани|полупансион|пансион|все включено", query))
        hotel_stay = bool(re.search(r"заезд|заселен|выселен|даты проживания", query)
                          or (re.search(r"выезд", query) and re.search(r"отел|гостиниц", query)))
        pets = bool(re.search(r"питом|собак|кошк|животн", query))
        rating = bool(re.search(r"рейтинг|оценк", query))
        stars = bool(re.search(r"звезд", query))
        hotel_requirements = {"wheelchair_accessible": bool(re.search(r"коляск|\bступен|безбарьер|доступн\w*\s+вход", query)),
                              "quiet_room": bool(re.search(r"\bтих\w*\s+номер\w*|\bномер\w*\s+тих", query)),
                              "away_from_elevator": "лифт" in query}
        requirement_labels = {"wheelchair_accessible": "Доступный вход для человека на коляске",
                              "quiet_room": "Тихий номер", "away_from_elevator": "Номер вдали от лифта"}
        refund = bool(re.search(r"отмен|возвратн|вернуть (?:билет|деньг)|возврат (?:билет|денег)", query))
        timing = bool(re.search(r"когда|во сколько|время|прилет|прибыт|вылет|отправлен", query))
        price = bool(re.search(r"цен|стоим|сколько стоит|дешев|дороже", query))
        if not selected and not alternatives and not any((comparison, baggage, food, refund, timing, price, hotel_stay, pets, rating, stars, any(hotel_requirements.values()))):
            return ""
        kinds = set()
        if re.search(r"отел|гостиниц|прожив", query):
            kinds.add("hotel")
        if re.search(r"перелет|авиа|самолет|рейс", query):
            kinds.add("avia")
        if re.search(r"\bпоезд(?:а|е|ом|ы|ов|ам|ами|ах)?\b|железнодорож|ж/д", query):
            kinds.add("rail")
        if "автобус" in query:
            kinds.add("bus")
        if re.search(r"транспорт|дорог[ауеой]", query):
            kinds.update(("avia", "rail", "bus"))
        if not kinds:
            if baggage:
                kinds.add("avia")
            if food or hotel_stay or rating or stars or any(hotel_requirements.values()):
                kinds.add("hotel")
            if timing and not kinds:
                kinds.update(("avia", "rail", "bus"))
        if not kinds:
            kinds = {"avia", "rail", "bus", "hotel"}
        relevant_selected = [item for item in selected if item["product_type"] in kinds]
        relevant_alternatives = [item for item in alternatives if item["product_type"] in kinds]
        # Comparisons must include alternatives even after a complete plan was
        # selected. Fact questions prefer the selected component of EACH kind,
        # falling back to that kind's cards, never to an unrelated selection.
        if comparison or listing:
            items = relevant_alternatives + relevant_selected
        else:
            items = []
            for kind in ("avia", "rail", "bus", "hotel"):
                if kind in kinds:
                    items.extend([item for item in relevant_selected if item["product_type"] == kind]
                                 or [item for item in relevant_alternatives if item["product_type"] == kind])
        if not items:
            if any(hotel_requirements.values()):
                return "Я пока не подтвердил для конкретного тарифа: " + ", ".join(requirement_labels[key].lower() for key, needed in hotel_requirements.items() if needed) + "."
            label = {frozenset({"hotel"}): "отелей", frozenset({"avia"}): "перелётов",
                     frozenset({"rail"}): "поездов", frozenset({"avia", "rail"}): "транспорта"}.get(frozenset(kinds), "поездки")
            detail = " Возвратность не подтверждена." if refund else ""
            return f"Сохранённых вариантов {label} пока нет; эти условия ещё не проверены." + detail
        lines = []
        seen = set()
        for item in items:
            key = item.get("provider_refs", {}).get("reference_key", item["title"])
            if key in seen:
                continue
            seen.add(key)
            cond, kind = item.get("conditions", {}), item["product_type"]
            facts = []
            def flag(value):
                return "не указано в тарифе" if value is None else "да" if value else "нет"
            if baggage and kind == "avia":
                value = cond.get("baggage_kg")
                facts.append("Багаж: " + (f"{value} кг" if value is not None else "не указан в тарифе"))
                if "ручн" in query:
                    value = cond.get("cabin_baggage_kg")
                    facts.append("Ручная кладь: " + (f"{value} кг" if value is not None else "не указана в тарифе"))
            if food and kind == "hotel":
                if meal:
                    facts.append("Питание: " + (str(cond["meal"]) if cond.get("meal") else "не указано в тарифе"))
                if not meal or not cond.get("meal"):
                    facts.append("Завтрак включён: " + flag(cond.get("breakfast")))
            if hotel_stay and kind == "hotel":
                def stay_date(value):
                    try:
                        return date.fromisoformat(str(value)).strftime("%d.%m.%Y")
                    except ValueError:
                        return "дата не указана"
                stay = item.get("query", {})
                both = "даты проживания" in query
                if both or re.search(r"заезд|заселен", query):
                    facts.append("Заезд: " + stay_date(stay.get("departure_date")) +
                                 (", с " + str(cond["check_in_time"]) if cond.get("check_in_time") else "; время не указано в тарифе"))
                if both or re.search(r"выезд|выселен", query):
                    facts.append("Выезд: " + stay_date(stay.get("return_date")) +
                                 (", до " + str(cond["check_out_time"]) if cond.get("check_out_time") else "; время не указано в тарифе"))
            if kind == "hotel":
                for key, needed in hotel_requirements.items():
                    if needed:
                        facts.append(requirement_labels[key] + ": " + flag(cond.get(key)))
            if pets:
                facts.append(("Размещение с питомцем: " if kind == "hotel" else "Перевозка питомца: ") + flag(cond.get("pets")))
            if rating and kind == "hotel":
                facts.append("Рейтинг: " + (str(cond["rating"]) if cond.get("rating") is not None else "не указан в тарифе"))
            if stars and kind == "hotel":
                facts.append("Звёзд: " + (str(cond["stars"]) if cond.get("stars") is not None else "не указано в тарифе"))
            if refund:
                facts.append("Возвратный тариф: " + flag(cond.get("refundable")))
                if kind == "hotel":
                    facts.append("Бесплатная отмена: " + flag(cond.get("free_cancellation")))
                    if cond.get("free_cancellation_until"):
                        facts.append("До " + str(cond["free_cancellation_until"]))
            if timing and kind != "hotel":
                from .domain.planning import local_moment
                facts.append(f"Туда: {local_moment(item.get('departure_at'))} → {local_moment(item.get('arrival_at'))}")
                facts.append(f"Обратно: {local_moment(item.get('return_departure_at'))} → {local_moment(item.get('return_arrival_at'))}")
            if price or comparison or listing:
                facts.append(rub(item["price_minor"]) + (" за всё проживание" if kind == "hotel" else " за дорогу туда и обратно"))
                if comparison and not any((baggage, food, refund, timing)):
                    if kind == "hotel":
                        facts.extend(["Завтрак: " + flag(cond.get("breakfast")), "Бесплатная отмена: " + flag(cond.get("free_cancellation"))])
                    elif kind == "avia":
                        value = cond.get("baggage_kg")
                        facts.append("Багаж: " + (f"{value} кг" if value is not None else "не указан"))
                    elif kind == "rail":
                        categories = {"SEDENTARY": "сидячий", "RESERVED_SEAT": "плацкарт", "COMPARTMENT": "купе", "LUX": "СВ", "SOFT": "мягкий", "SHARED": "общий"}
                        category = cond.get("seat_category")
                        facts.append("Вагон: " + categories.get(category, "категория не указана"))
            if facts:
                line = re.sub(r"\s+", " ", item["title"] + ": " + ". ".join(facts[:5]) + ".").strip()
                lines.append(line if len(line) <= 400 else line[:397].rstrip() + "…")
            if len(lines) == 8:
                break
        return "\n".join(lines) or "В сохранённых тарифах нет подтверждённых сведений по этому вопросу."

    async def _select_component(self, s, turn, d, emit):
        cards = s.transport_options + s.hotel_options
        if not cards and turn.input == "message":
            question = next_trip_question(s.request.model_dump(mode="json")) or "Подобрать варианты по вашим пожеланиям?"
            s.last_question = question
            return self._out("needs_input", "Пока выбирать не из чего. " + question, questions=[question])
        if turn.input == "select_component":
            if turn.payload.get("options_version") != s.options_version:
                return self._out("error", "Эта выдача уже устарела. Выберите из текущих вариантов.", error={"code": "stale_options", "retryable": False})
            card = next((c for c in cards if c["component_id"] == turn.payload.get("component_id")), None)
        else:
            kind, index = d.get("component_type"), d.get("option_index")
            typed = [c for c in cards if c["product_type"] == kind]
            card = typed[index - 1] if type(index) is int and 1 <= index <= len(typed) else None
        if card is None or card.get("eligibility") != "satisfied":
            return self._out("needs_input", "Укажите вид и номер из текущей выдачи, например «выбираю отель 1».")
        query = s.request.model_dump(mode="json")
        if not self._valid_component(card, query):
            return self._out("error", "Этот вариант относится к другим условиям поездки. Обновите поиск.", error={"code": "stale_options", "retryable": False})
        s.pending_action = None
        is_hotel = card["product_type"] == "hotel"
        selected = [c for c in s.selected_components if (c["product_type"] == "hotel") != is_hotel]
        selected.append(deepcopy(card))
        note = ""
        if not is_hotel and "hotel" in query["products"]:
            check_in = card.get("arrival_at", "")[:10]
            hotel = next((c for c in selected if c["product_type"] == "hotel"), None)
            if hotel and hotel.get("query", {}).get("departure_date") != check_in:
                selected = [c for c in selected if c["product_type"] != "hotel"]
                note = " Проживание нужно подобрать с даты прибытия."
            if check_in and (not s.hotel_options or not any(c.get("query", {}).get("departure_date") == check_in for c in s.hotel_options)):
                await emit("tool_started", {"operation": "search_hotels_for_arrival"})
                found = await self.adapter.search({**query, "products": ["hotel"], "departure_date": check_in}, turn.now.isoformat())
                s.options_version += 1
                merged = {"flights": [c for c in s.transport_options if c["product_type"] == "avia"],
                          "trains": [c for c in s.transport_options if c["product_type"] == "rail"],
                          "buses": [c for c in s.transport_options if c["product_type"] == "bus"], "hotels": found.get("hotels", [])}
                groups = component_options_from_search(merged, query, s.options_version)
                s.transport_options, s.hotel_options = groups["transport_options"], groups["hotel_options"]
                s.options = options_from_search(merged, query, s.options_version, s.pins)
                s.component_errors = {**{k: v for k, v in s.component_errors.items() if k != "hotel"},
                                      **dict(found.get("component_errors", {}))}
                if found.get("error"):
                    s.component_errors["hotel"] = found["error"]
                    note += " Подбор проживания не завершился; выбранный транспорт сохранён."
                await emit("tool_finished", {"operation": "search_hotels_for_arrival", "count": len(s.hotel_options)})
        option = option_from_components(selected, query, s.options_version)
        if option["complete"] and option["eligibility"] != "satisfied":
            reasons = [c["explanation"] for c in option["checks"] if c.get("hard") and c["status"] != "satisfied"]
            return self._out("partial", "Этот выбор не сочетается с остальной поездкой: " + " ".join(reasons), warnings=reasons)
        plan = self._make_plan(s, option)
        s.last_question = None
        if plan["missing_products"]:
            missing = "проживание" if plan["missing_products"] == ["hotel"] else "транспорт"
            message = f"Сохранил: {card['title']}. Выбрано на {rub(plan['total_minor'])}. Теперь выберите {missing}." + note
        else:
            message = f"Я собрал поездку. Итого за выбранные варианты: {rub(plan['total_minor'])}. Можно изменить выбор." + note
        return self._out("plan_ready", message, plan_diff=plan.get("diff", []), warnings=plan.get("warnings", []))

    @staticmethod
    def _valid_component(card, request):
        kind, query = card["product_type"], card.get("query", {})
        if not component_matches_request(card, request):
            return False
        single = {**request, "products": [kind]}
        if kind == "hotel" and any(p in request.get("products", []) for p in ("avia", "rail", "bus")):
            single["departure_date"] = query.get("departure_date")
        return all(not c.get("hard") or c["status"] == "satisfied" for c in check_components([card], single))

    async def _action(self, s, turn, d, emit):
        intent = d.get("intent")
        if intent == "concert_intake":
            s.last_question = d["question"]
            return self._out("needs_input", d["question"], questions=[d["question"]])
        if intent == "select_event":
            if turn.input == "select_event" and turn.payload.get("event_choices_version") != s.event_choices_version:
                return self._out("error", "Я уже обновил список концертов. Выберите событие из текущего списка.", error={"code": "stale_events", "retryable": False})
            event_id = turn.payload.get("event_id") if turn.input == "select_event" else d.get("event_id")
            event = next((item for item in s.event_choices if item["event_id"] == event_id), None)
            if event is None:
                return self._out("needs_input", "Я не смог определить концерт. Выберите его карточку или укажите номер из списка.")
            # The first selected event must not erase an explicit trip window.
            # Later event changes keep the existing reset-and-reconfirm flow.
            changed = bool(s.selected_event and s.selected_event["event_id"] != event_id)
            s.selected_event = deepcopy(event)
            s.concert_intake.mode = "trip"
            s.concert_intake.event_name = event.get("artist") or event["title"]
            s.concert_intake.period = s.concert_intake.period or event["date"]
            s.concert_intake.locations = s.concert_intake.locations or event["city"]
            from .interpretation import event_destination
            patch = {"destination": event_destination(event), "event_date": event["date"], "event_start_at": event.get("start_at")}
            return await self._search(s, turn, {"intent": "replan", "patch": patch, "reset_dates": changed}, emit)
        if intent == "chat":
            from .response_policy import stops_planning
            from .concert_intake import cancels_intake
            text = str(turn.payload.get("text") or "")
            if stops_planning(text) or cancels_intake(text):
                s.pending_action = None
                s.last_question = None
            return self._out("plan_ready" if s.current_plan else "needs_input",
                             d.get("reply_text") or "Я здесь.", response_kind="chat")
        if intent in ("select", "select_component", "checkout", "confirm", "reject", "undo") and d.get("patch"):
            return await self._search(s, turn, {**d, "intent": "replan"}, emit)
        if intent == "select_component":
            return await self._select_component(s, turn, d, emit)
        if intent == "remove_component":
            kind = turn.payload.get("product_type")
            if kind not in ("avia", "rail", "bus", "hotel"):
                return self._out("needs_input", "Укажите, какой выбранный компонент убрать.")
            selected = [c for c in s.selected_components if c["product_type"] != kind]
            if selected:
                self._make_plan(s, option_from_components(selected, s.request.model_dump(mode="json"), s.options_version))
            else:
                self._invalidate(s, clear_components=False)
                s.selected_components = []
            return self._out("plan_ready" if selected else "options_ready", "Убрал выбранный компонент. Остальные части поездки сохранены.")
        if intent == "select":
            if not s.options:
                return self._out("needs_input", "Сначала задайте параметры поездки и получите варианты.")
            if turn.input == "select_option":
                if turn.payload.get("options_version") != s.options_version:
                    return self._out("error", "Эта выдача уже устарела.", error={"code": "stale_options", "retryable": False})
                option = next((o for o in s.options if o["option_id"] == turn.payload.get("option_id")), None)
            else:
                index = d.get("option_index")
                option = s.options[index - 1] if isinstance(index, int) and 1 <= index <= len(s.options) else None
            if not option:
                return self._out("needs_input", "Не удалось однозначно определить вариант. Укажите номер из текущей выдачи.")
            if option.get("options_version") != s.options_version or not all(self._valid_component(c, s.request.model_dump(mode="json")) for c in option["components"]):
                return self._out("error", "Этот вариант относится к прежним условиям поездки. Обновите поиск.", error={"code": "stale_options", "retryable": False})
            if option["eligibility"] != "satisfied":
                return self._out("partial", "Этот вариант не подтверждает все обязательные условия. Измените требование явно, прежде чем оформлять.", warnings=option["warnings"])
            plan = self._make_plan(s, option)
            return self._out("plan_ready", f"Я сохранил выбор: {plan['title']}. Итого: {rub(plan['total_minor'])}.", plan_diff=plan["diff"], warnings=plan["warnings"])
        if intent == "checkout":
            if self.checkout_mode == "disabled":
                if s.current_plan and not s.current_plan.get("missing_products"):
                    return self._out("plan_ready", "Я сохранил план поездки. Выбранные варианты и стоимость — в карточке поездки.")
                if not (s.transport_options or s.hotel_options or s.options):
                    question = next_trip_question(s.request.model_dump(mode="json")) or "Подобрать варианты по вашим пожеланиям?"
                    s.last_question = question
                    return self._out("needs_input", "Я могу подобрать и сохранить план; покупка и бронирование здесь недоступны. " + question, questions=[question])
                return self._out("needs_input", "Сначала выберите варианты для поездки.")
            if not s.current_plan:
                return self._out("needs_input", "Сначала выберите один из актуальных вариантов.")
            if s.current_plan.get("missing_products"):
                return self._out("needs_input", "Сначала выберите недостающие компоненты поездки.")
            await self._skill(s, "finalize-trip", emit)
            refreshed = await self._refresh(s, turn, emit)
            if refreshed:
                return refreshed
            return await self._prepare(s, turn, emit)
        if intent == "confirm":
            await self._skill(s, "finalize-trip", emit)
            supplied = turn.payload if turn.input == "confirm_action" else (s.pending_action or {})
            prior = self.repo.order(s.user_id, s.session_id, supplied.get("action_id", ""))
            if prior:
                if all(supplied.get(k) == prior.get(k) for k in ("action_id", "plan_id", "plan_version", "payload_hash")):
                    return self._out("completed_demo", "Этот демонстрационный заказ уже создан; возвращаю прежний номер. Реальной покупки нет.", demo_order=prior)
                return self._out("error", "Параметры подтверждения не совпадают.", error={"code": "stale_confirmation", "retryable": False})
            pending = s.pending_action
            fields = ("action_id", "plan_id", "plan_version", "payload_hash")
            if not pending or not all(supplied.get(k) == pending.get(k) for k in fields):
                return self._out("error", "Подтверждение не соответствует актуальному составу поездки.", error={"code": "stale_confirmation", "retryable": False})
            if not s.current_plan or pending["payload_hash"] != plan_hash(s.current_plan):
                s.pending_action = None
                return self._out("error", "Состав поездки изменился. Нужно новое подтверждение.", error={"code": "stale_confirmation", "retryable": False})
            if turn.now >= datetime.fromisoformat(pending["expires_at"]):
                s.pending_action = None
                return self._out("needs_input", "Срок этого подтверждения истёк. Напишите «Оформить», чтобы проверить состав снова.")
            refreshed = await self._refresh(s, turn, emit)
            if refreshed:
                return refreshed
            if s.pending_action is None:
                return await self._prepare(s, turn, emit)
            order = {**{k: pending[k] for k in fields}, "local_id": "DEMO-" + uuid4().hex[:12].upper(), "mode": "demo", "status": "simulated_confirmed", "created_at": turn.now.isoformat(), "total_minor": s.current_plan["total_minor"], "currency": "RUB", "components": deepcopy(s.current_plan["components"]), "data_mode": self.data_mode}
            s.demo_order, s.pending_action = order, None
            return self._out("completed_demo", f"Я создал демонстрационный заказ {order['local_id']}. Реальные билеты и номера я не бронировал; деньги не списывались.", demo_order=order, warnings=s.current_plan.get("warnings", []))
        if intent == "reject":
            if not s.pending_action:
                return self._out("plan_ready" if s.current_plan else "needs_input", "Сейчас нет действия, ожидающего подтверждения.")
            s.pending_action = None
            return self._out("plan_ready" if s.current_plan else "needs_input", "Я отменил подтверждение и сохранил поездку. Можно изменить условия.")
        if intent == "undo":
            s.pending_action = None
            if not s.plan_history:
                return self._out("needs_input", "Предыдущей выбранной версии ещё нет.")
            previous = deepcopy(s.plan_history[-1])
            refresh = await self.adapter.refresh(previous["components"], previous["request"], turn.now.isoformat())
            if refresh["status"] not in ("unchanged", "changed"):
                return self._out("partial", "Предыдущий состав сейчас нельзя проверить; откат не выполнен.")
            if not refresh.get("components") or not all(self._valid_component(c, previous["request"]) for c in refresh["components"]):
                return self._out("partial", "Предыдущая версия больше не подтверждает обязательные условия.")
            previous["components"] = refresh["components"]
            previous["total_minor"] = sum(c["price_minor"] for c in refresh["components"])
            previous["checks"] = check_components(previous["components"], previous["request"])
            refresh_plan_explanation(previous)
            complete = option_from_components(previous["components"], previous["request"])
            if not all(self._valid_component(c, previous["request"]) for c in previous["components"]) or (complete["complete"] and any(c.get("hard") and c["status"] != "satisfied" for c in previous["checks"])):
                return self._out("partial", "Предыдущая версия больше не подтверждает обязательные условия.")
            previous.update(complete)
            plan = self._make_plan(s, previous, previous["request"])
            s.request = TripRequest.model_validate(previous["request"])
            s.options = []
            s.transport_options = []
            s.hotel_options = []
            s.component_errors = {}
            s.search_diagnostics = None
            s.options_version += 1
            return self._out("plan_ready", "Я восстановил предыдущую поездку и отменил прежнее подтверждение.", plan_diff=plan["diff"])
        if intent == "question":
            if not (s.current_plan or s.options or s.transport_options or s.hotel_options or s.search_diagnostics):
                question = next_trip_question(s.request.model_dump(mode="json")) or "Подобрать варианты по вашим пожеланиям?"
                s.last_question = question
                return self._out("needs_input", "Пока нет найденных вариантов для сравнения условий. " + question, questions=[question])
            answer = self._answer_question(s, d.get("question_text") or turn.payload.get("text", ""))
            if answer:
                available = any(item.get("eligibility") == "satisfied" for item in s.options + s.transport_options + s.hotel_options)
                status = "plan_ready" if s.current_plan else "options_ready" if available else "partial" if s.search_diagnostics else "needs_input"
                return self._out(status, answer)
            if s.current_plan:
                plan = s.current_plan
                details = describe_components(plan)
                return self._out("plan_ready", "По выбранным тарифам:\n" + "\n".join(details), warnings=plan.get("warnings", []))
            if s.options:
                lines = []
                for index, option in enumerate(s.options, 1):
                    lines.append(f"Вариант {index} — {option['title']}: {rub(option['total_minor'])}.")
                    lines.extend(describe_components(option))
                return self._out("options_ready", "По проверенным предложениям:\n" + "\n".join(lines)
                                 + "\nСравнение по сохранённым тарифам; актуальность за пределами этих данных не проверялась.")
            from .domain.search_diagnostics import legacy_search_diagnostics, format_search_diagnostics
            diagnostic = s.search_diagnostics or legacy_search_diagnostics(s.search_coverage, s.request.model_dump(mode="json"))
            if diagnostic:
                s.search_diagnostics = diagnostic
                explained = format_search_diagnostics(diagnostic, followup=True, data_mode=self.data_mode)
                s.last_question = explained["question"]
                return self._out("partial", explained["message"],
                                 questions=[s.last_question] if s.last_question else [],
                                 error=diagnostic.get("error"))
            return self._out("needs_input", "Поездка сохранена. Могу сравнить варианты после поиска; актуальные сведения о визах и погоде без источника не подтверждаю.")
        return self._out("needs_input", "Эта возможность пока не реализована. Я могу подобрать самолёт, поезд, автобус и проживание, выбрать их отдельно или изменить условия.")

    async def _refresh(self, s, turn, emit):
        await emit("tool_started", {"operation": "refresh", "data_mode": self.data_mode})
        result = await self.adapter.refresh(s.current_plan["components"], s.current_plan["request"], turn.now.isoformat())
        await emit("tool_finished", {"operation": "refresh", "status": result["status"]})
        if result["status"] not in ("unchanged", "changed"):
            s.pending_action = None
            s.current_plan["verification_status"] = "unverified"
            message = ("Выбранный тариф не удалось подтвердить. Обновим варианты?"
                       if (result.get("error") or {}).get("code") == "stale_offer"
                       else "Не удалось проверить цену и наличие выбранной поездки. Обновим варианты?")
            return self._out("partial", message, questions=["Обновим варианты?"], warnings=result.get("warnings", []), error=result.get("error"))
        components = result.get("components") or []
        previous_kinds = sorted(c["product_type"] for c in s.current_plan["components"])
        if (sorted(c.get("product_type", "") for c in components) != previous_kinds
                or not all(self._valid_component(c, s.current_plan["request"]) for c in components)):
            s.pending_action = None
            s.current_plan["verification_status"] = "unverified"
            return self._out("partial", "Обновлённые данные не подтверждают выбранный состав поездки. Требуется новый поиск.",
                             error={"code": "invalid_offer", "retryable": True})
        old = s.current_plan
        updated = deepcopy(old)
        updated.pop("verification_status", None)
        updated["components"] = result["components"]
        updated["total_minor"] = sum(c["price_minor"] for c in updated["components"])
        updated["checks"] = check_components(updated["components"], updated["request"])
        refresh_plan_explanation(updated)
        if any(c.get("hard") and c["status"] != "satisfied" for c in updated["checks"]):
            s.pending_action = None
            s.current_plan["verification_status"] = "unverified"
            return self._out("partial", "Обязательные условия выбранного состава больше не подтверждены. Требуется новый выбор.", warnings=[c["explanation"] for c in updated["checks"] if c["status"] != "satisfied"])
        if fingerprint(stable_components(old["components"])) != fingerprint(stable_components(updated["components"])):
            self._make_plan(s, updated, updated["request"])
            return await self._prepare(s, turn, emit, "Цена или условия изменились. Проверьте новый состав. ")
        s.current_plan = updated
        s.selected_components = deepcopy(updated["components"])
        return None

    async def _prepare(self, s, turn, emit, prefix=""):
        plan = s.current_plan
        departure_day = datetime.fromisoformat(str(plan["request"]["departure_date"])).strftime("%d.%m.%Y")
        return_day = datetime.fromisoformat(str(plan["request"]["return_date"])).strftime("%d.%m.%Y")
        s.pending_action = {"action_id": str(uuid4()), "plan_id": plan["plan_id"], "plan_version": plan["version"], "payload_hash": plan_hash(plan), "created_at": turn.now.isoformat(), "expires_at": (turn.now + timedelta(minutes=15)).isoformat(), "mode": "demo", "total_minor": plan["total_minor"]}
        await emit("confirmation_required", {"action_id": s.pending_action["action_id"], "plan_version": plan["version"]})
        return self._out("needs_confirmation", prefix + f"Я подготовил демонстрационный заказ на {departure_day} — {return_day}, взрослых: {plan['request']['adults']}, детей: {len(plan['request']['children_ages'])}.\n" + "\n".join(describe_components(plan)) + f"\nИтого за выбранные варианты: {rub(plan['total_minor'])}. Трансферы и прочие расходы я ещё не оценил. Реальной брони и оплаты не будет. Создать этот демонстрационный заказ? Подтверждение действительно 15 минут.", plan_diff=plan.get("diff", []), warnings=plan.get("warnings", []))


"""Conversation context and fail-soft model-generated next-message suggestions.

No external model or event listings are queried. A scripted gateway reproduces
the reported concert conversation and exposes exactly what the model receives.
"""
import asyncio
from copy import deepcopy
import json
import sqlite3
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from travel_agent.domain.models import TurnInput
from travel_agent.interpretation import RuleBasedGateway
from travel_agent.service import AgentService
from travel_agent.web import create_app

NOW = "2026-10-27T12:00:00+03:00"
CONCERT = "хочу на концерт моргена"
FIRST_QUESTION = "На какой концерт Моргенштерна хотите поехать?"
FLEXIBLE = "я один город и дата любые"
ORIGIN_QUESTION = "Из какого города поедете?"
ORIGIN_ANSWER = "Из Москвы"


class TranscriptGateway(RuleBasedGateway):
    name = "scripted-model"

    def __init__(self, suggestion_failure=None):
        self.interpret_contexts = []
        self.suggest_contexts = []
        self.suggestion_failure = suggestion_failure

    async def interpret(self, text, request, now, timezone, skills=None):
        self.interpret_contexts.append({"text": text, "request": deepcopy(request)})
        if text == CONCERT:
            patch, question = {}, FIRST_QUESTION
        elif text == FLEXIBLE:
            patch, question = {"adults": 1}, ORIGIN_QUESTION
        else:
            patch, question = {"origin": "Москва"}, "Какой бюджет выделим на дорогу и проживание?"
        return {"intent": "plan", "patch": patch, "question": question,
                "option_index": None, "keep_hotel": False, "warnings": []}

    async def suggest(self, context):
        self.suggest_contexts.append(deepcopy(context))
        if isinstance(self.suggestion_failure, BaseException):
            raise self.suggestion_failure
        if self.suggestion_failure == "malformed":
            return [{"id": "broken", "label": [], "text": None}]
        if "из какого города" in str(context.get("latest_assistant_question") or "").casefold():
            return [
                {"id": "origin-moscow", "label": "Из Москвы", "text": "Поеду из Москвы."},
                {"id": "origin-petersburg", "label": "Из Петербурга", "text": "Поеду из Санкт-Петербурга."},
            ]
        return [{"id": "concert-flexible", "label": "Город и дата любые", "text": "Я еду один, город и дата концерта любые."}]


def request(text, version=0, *, user="dialogue-user", session="concert"):
    return TurnInput(request_id=str(uuid4()), user_id=user, session_id=session,
                     expected_state_version=version, input="message", payload={"text": text},
                     now=NOW, timezone="Europe/Moscow")


def message_pairs(messages):
    return [(message["role"], message["text"]) for message in messages]


def output_suggestions(result):
    return result.model_dump()["suggestions"]


async def test_concert_grouped_questions_preserve_context_without_unrelated_suggestions(tmp_path):
    gateway = TranscriptGateway()
    service = AgentService(db_path=tmp_path / "concert.sqlite", gateway=gateway)
    try:
        first = await service.run_turn(request(CONCERT))
        second = await service.run_turn(request(FLEXIBLE, first.state_version))
        assert first.status == second.status == "needs_input"
        assert first.questions == [first.message] and second.questions == [second.message]
        assert "какой период" in first.message and "какие города" in first.message
        assert "из какого города" in second.message and "бюджет" in second.message
        assert "какой период" not in second.message and "какие города" not in second.message
        assert len(gateway.interpret_contexts) == 2
        # A grouped intake must not offer a one-field answer as if it completed
        # the whole question. Ordinary single-field suggestions are tested below.
        assert gateway.suggest_contexts == []
        assert output_suggestions(first) == output_suggestions(second) == []
        context = gateway.interpret_contexts[-1]["request"]["_dialogue"]
        assert context["last_question"] == first.message
        history = message_pairs(context["history"])
        assert ("user", CONCERT) in history
        assert ("assistant", first.message) in history
        state = await service.get_session("dialogue-user", "concert")
        assert ("user", FLEXIBLE) in message_pairs(state.dialogue)
        assert state.last_question == second.message
        assert state.request.adults == 1
        assert state.request.origin is None
        assert state.request.destination is None
        assert state.request.departure_date is None
        assert state.concert_intake.mode == "trip"
        assert state.concert_intake.event_name.casefold() == "моргена"
        assert state.concert_intake.period == "любые будущие даты"
        assert state.concert_intake.locations == "любые города и страны"
        assert state.pending_action is None
        assert state.demo_order is None
    finally:
        service.close()


async def test_single_field_question_still_reaches_suggestion_model(tmp_path):
    gateway = TranscriptGateway()
    service = AgentService(db_path=tmp_path / "single-question.sqlite", gateway=gateway)
    try:
        result = await service.run_turn(request(FLEXIBLE))
        assert result.message == ORIGIN_QUESTION
        assert len(gateway.suggest_contexts) == 1
        context = gateway.suggest_contexts[0]
        assert context["latest_assistant_question"] == result.message
        assert context["latest_assistant_message"] == result.message
        assert context["current_user_message"] == FLEXIBLE
        assert ("user", FLEXIBLE) in message_pairs(context["recent_dialogue"])
        hints = output_suggestions(result)
        assert [hint["id"] for hint in hints] == ["origin-moscow", "origin-petersburg"]
        assert all("…" not in hint["text"] and "..." not in hint["text"] for hint in hints)
    finally:
        service.close()


async def test_restart_preserves_flexible_preference_utterance_in_interpretation_history(tmp_path):
    db = tmp_path / "restart.sqlite"
    gateway = TranscriptGateway()
    service = AgentService(db_path=db, gateway=gateway)
    try:
        first = await service.run_turn(request(CONCERT))
        second = await service.run_turn(request(FLEXIBLE, first.state_version))
        before = await service.get_session("dialogue-user", "concert")
        assert message_pairs(before.dialogue) == [
            ("user", CONCERT), ("assistant", first.message),
            ("user", FLEXIBLE), ("assistant", second.message),
        ]
    finally:
        service.close()
    restarted_gateway = TranscriptGateway()
    restarted = AgentService(db_path=db, gateway=restarted_gateway)
    try:
        restored = await restarted.get_session("dialogue-user", "concert")
        assert restored.dialogue == before.dialogue
        assert restored.concert_intake == before.concert_intake
        answer = await restarted.run_turn(request(ORIGIN_ANSWER, second.state_version))
        context = restarted_gateway.interpret_contexts[0]["request"]["_dialogue"]
        assert context["history"] == before.dialogue
        assert context["last_question"] == second.message
        assert restarted_gateway.interpret_contexts[0]["request"]["_concert_intake"] == before.concert_intake.model_dump(mode="json")
        after = await restarted.get_session("dialogue-user", "concert")
        assert ("user", FLEXIBLE) in message_pairs(after.dialogue)
        assert message_pairs(after.dialogue)[-2:] == [("user", ORIGIN_ANSWER), ("assistant", answer.message)]
        assert after.request.origin == "Москва"
        assert after.request.adults == 1
        assert after.concert_intake == before.concert_intake
    finally:
        restarted.close()


@pytest.mark.parametrize("failure", [RuntimeError("do-not-leak-suggestion-debug"), asyncio.TimeoutError(), "malformed"])
async def test_suggestion_error_timeout_or_bad_output_cannot_undo_main_turn(tmp_path, failure):
    gateway = TranscriptGateway(suggestion_failure=failure)
    service = AgentService(db_path=tmp_path / "failure.sqlite", gateway=gateway)
    try:
        result = await service.run_turn(request(FLEXIBLE))
        assert result.status == "needs_input", result.model_dump()
        assert result.message == ORIGIN_QUESTION
        assert result.error is None
        assert result.state_version == 1
        assert len(gateway.suggest_contexts) == 1
        assert "do-not-leak" not in result.model_dump_json()
        state = await service.get_session("dialogue-user", "concert")
        assert state.request.adults == 1
        assert state.request.origin is None
        assert state.state_version == 1
        assert message_pairs(state.dialogue) == [("user", FLEXIBLE), ("assistant", ORIGIN_QUESTION)]
    finally:
        service.close()


async def test_exact_repeat_does_not_regenerate_model_suggestions_or_duplicate_dialogue(tmp_path):
    gateway = TranscriptGateway()
    service = AgentService(db_path=tmp_path / "repeat.sqlite", gateway=gateway)
    try:
        body = request(FLEXIBLE)
        first = await service.run_turn(body)
        prior = await service.get_session("dialogue-user", "concert")
        again = await service.run_turn(body)
        after = await service.get_session("dialogue-user", "concert")
        assert first.model_dump() == again.model_dump()
        assert after.dialogue == prior.dialogue
        assert len(gateway.interpret_contexts) == len(gateway.suggest_contexts) == 1
    finally:
        service.close()


def post_and_wait(client, text, version=0):
    from http_helpers import bootstrap
    token = bootstrap(client).json()["csrf_token"]
    body = {"session_id": "legacy-concert", "request_id": str(uuid4()),
            "expected_state_version": version, "input": "message", "payload": {"text": text}}
    response = client.post("/api/turns", json=body, headers={"X-Travel-Token": token})
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            assert job["status"] == "completed", job
            return job["result"]
        time.sleep(0.015)
    pytest.fail("Local scripted dialogue job did not complete")


def test_legacy_web_history_seeds_next_turn_without_eager_mutation(tmp_path):
    db = tmp_path / "legacy.sqlite"
    gateway = TranscriptGateway()
    with TestClient(create_app(db_path=db, model_backend="rules", gateway=gateway), base_url="http://127.0.0.1") as client:
        first = post_and_wait(client, CONCERT)
        second = post_and_wait(client, FLEXIBLE, first["state_version"])
        cookies = dict(client.cookies)
        user_id = client.get("/api/sessions/legacy-concert").json()["session"]["user_id"]
    with sqlite3.connect(db) as connection:
        row = connection.execute("SELECT body FROM sessions WHERE user_id=? AND session_id='legacy-concert'", (user_id,)).fetchone()
        old_state = json.loads(row[0])
        old_state.pop("dialogue", None)
        connection.execute("UPDATE sessions SET body=? WHERE user_id=? AND session_id='legacy-concert'", (json.dumps(old_state), user_id))
    restarted_gateway = TranscriptGateway()
    with TestClient(create_app(db_path=db, model_backend="rules", gateway=restarted_gateway), base_url="http://127.0.0.1") as client:
        for name, value in cookies.items():
            client.cookies.set(name, value, domain="127.0.0.1", path="/")
        restored = client.get("/api/sessions/legacy-concert").json()
        assert FLEXIBLE in [message["text"] for message in restored["messages"]]
        with sqlite3.connect(db) as connection:
            saved = json.loads(connection.execute("SELECT body FROM sessions WHERE user_id=? AND session_id='legacy-concert'", (user_id,)).fetchone()[0])
            assert "dialogue" not in saved  # GET is presentation-only; no migration side effect.
        result = post_and_wait(client, ORIGIN_ANSWER, second["state_version"])
        history = restarted_gateway.interpret_contexts[0]["request"]["_dialogue"]["history"]
        assert message_pairs(history) == [
            ("user", CONCERT), ("assistant", first["message"]),
            ("user", FLEXIBLE), ("assistant", second["message"]),
        ]
        state = client.get("/api/sessions/legacy-concert").json()["session"]
        assert state["state_version"] == result["state_version"]
        assert ("user", FLEXIBLE) in message_pairs(state["dialogue"])
        assert message_pairs(state["dialogue"])[-2:] == [("user", ORIGIN_ANSWER), ("assistant", result["message"])]


def test_dialogue_bound_preserves_recent_messages_without_mutating_source():
    from travel_agent.dialogue import bound_dialogue
    messages = [{"role": "user" if index % 2 == 0 else "assistant", "text": f"message-{index}:" + "x" * 4500} for index in range(40)]
    original = deepcopy(messages)
    bounded = bound_dialogue(messages)
    assert messages == original
    assert 0 < len(bounded) <= 24
    assert sum(len(message["text"]) for message in bounded) <= 16000
    assert all(len(message["text"]) <= 4000 for message in bounded)
    assert bounded[-1]["text"].startswith("message-39:")


async def test_slow_suggestion_model_does_not_consume_main_turn_deadline(tmp_path):
    class SlowSuggestGateway(RuleBasedGateway):
        name = "slow-suggestion-model"

        def __init__(self):
            self.suggestion_calls = 0
            self.cancelled = False

        async def suggest(self, context):
            self.suggestion_calls += 1
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return []

    gateway = SlowSuggestGateway()
    service = AgentService(db_path=tmp_path / "bounded-time.sqlite", gateway=gateway, turn_timeout=0.4)
    body = request("Из Москвы в Ереван с 24 ноября по 1 декабря 2026, один взрослый, общий бюджет 80 тысяч рублей, багаж 23 кг и завтрак")
    try:
        started = time.monotonic()
        result = await service.run_turn(body)
        elapsed = time.monotonic() - started
        assert result.status == "options_ready", result.model_dump()
        assert result.error is None
        assert elapsed < 1.5
        assert gateway.suggestion_calls <= 1
        if gateway.suggestion_calls:
            assert gateway.cancelled
        state = await service.get_session("dialogue-user", "concert")
        assert state.state_version == 1
        repeat = await service.run_turn(body)
        assert repeat.model_dump() == result.model_dump()
        count = service.repo.conn.execute("SELECT COUNT(*) FROM turns WHERE user_id='dialogue-user' AND session_id='concert'").fetchone()[0]
        assert count == 1
    finally:
        service.close()

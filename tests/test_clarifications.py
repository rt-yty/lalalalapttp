"""One clarification topic at a time without losing volunteered trip details."""
from copy import deepcopy
import re
from uuid import uuid4

import pytest

from travel_agent.domain.clarifications import (
    next_trip_question, normalize_clarification, normalize_session_question,
)
from travel_agent.domain.models import TurnInput
from travel_agent.service import AgentService

NOW = "2026-10-27T21:30:00+03:00"
ROUTE = {"origin": "Москва", "destination": "Хайфа"}
DATES = {"departure_date": "2026-10-28", "return_date": "2026-10-31"}
COMPOUND = "Какие даты вылета и возвращения выберем для концерта в Хайфе 28 октября 2026 года и сколько взрослых поедет?"
DEPARTURE = "Когда хотите вылететь?"


@pytest.mark.parametrize("trip_request, expected, excluded", [
    ({}, r"куда|направлен|город", r"взросл|дат|когда|откуда"),
    ({"destination": "Хайфа"}, r"откуда|из какого города|отправлен", r"взросл|дат|когда"),
    (ROUTE, r"когда.*вылет|дат.*вылет", r"взросл|возвращ"),
    ({**ROUTE, "departure_date": "2026-10-28"}, r"вернут|возврат|возвращ", r"взросл|вылет"),
    ({**ROUTE, **DATES}, r"взросл", r"дат|вылет|возвращ"),
    ({**ROUTE, **DATES, "adults": 2, "budget_minor": 10000000}, r"на всех|на человека|на одного", r"дат|вылет|возвращ"),
])
def test_missing_parameters_are_requested_one_at_a_time(trip_request, expected, excluded):
    question = next_trip_question(deepcopy(trip_request))
    assert question and re.search(expected, question, re.I), question
    assert not re.search(excluded, question, re.I), question
    assert question.count("?") == 1


def test_complete_request_does_not_get_an_extra_budget_question():
    assert next_trip_question({**ROUTE, **DATES, "adults": 1}) is None


def test_screenshot_question_is_reduced_to_departure_only():
    assert normalize_clarification(COMPOUND, ROUTE) == DEPARTURE


def test_known_dates_are_not_requested_again_from_a_compound_question():
    question = normalize_clarification(COMPOUND, {**ROUTE, **DATES})
    assert "взросл" in question
    assert not re.search(r"вылет|возвращ|дат", question, re.I)


@pytest.mark.parametrize("question", [
    "Из какого города поедете?",
    "Вы имеете в виду 1 или 8 ноября?",
    "Подбор поездов пока не реализован в прототипе. Хотите явно изменить запрос на авиаперелет?",
    "Прототип поддерживает одну точку назначения. Какой город выбрать для этой поездки?",
    "Сколько лет каждому ребёнку на дату поездки?",
])
def test_single_topic_and_blocking_questions_keep_their_meaning(question):
    assert normalize_clarification(question, ROUTE) == question


def test_child_ages_take_priority_over_other_compound_clarifications():
    question = normalize_clarification(
        "Когда вылетаете, сколько взрослых поедет и сколько лет каждому ребёнку?", ROUTE)
    assert re.search(r"возраст|сколько лет", question, re.I)
    assert not re.search(r"вылет|взросл", question, re.I)


def test_old_session_question_and_last_assistant_message_are_normalized_together():
    state = {"request": deepcopy(ROUTE), "last_question": COMPOUND, "dialogue": [
        {"role": "assistant", "text": "На какой концерт поедем?"},
        {"role": "user", "text": "Выбираю концерт в Хайфе."},
        {"role": "assistant", "text": COMPOUND},
    ]}
    before = deepcopy(state)
    normalized = normalize_session_question(state)
    assert state == before
    assert normalized["last_question"] == DEPARTURE
    assert normalized["dialogue"][-1]["text"] == DEPARTURE
    assert normalized["dialogue"][:-1] == before["dialogue"][:-1]
    assert normalized["request"] == before["request"]


class ScriptedGateway:
    name = "clarification-test"

    def __init__(self, decisions, suggestions=None):
        self.decisions = iter(decisions)
        self.suggestions = suggestions or []
        self.suggestion_contexts = []
        self.interpret_contexts = []

    async def interpret(self, text, request, now, timezone, skills=None):
        self.interpret_contexts.append(deepcopy(request))
        return deepcopy(next(self.decisions))

    async def suggest(self, context):
        self.suggestion_contexts.append(deepcopy(context))
        return deepcopy(self.suggestions)


class RecordingAdapter:
    def __init__(self):
        self.calls = []

    async def search(self, request, now):
        self.calls.append(deepcopy(request))
        return {"flights": [], "hotels": [], "coverage": [], "warnings": []}


def turn(text, version=0):
    return TurnInput(request_id=str(uuid4()), user_id="one-question-test", session_id="trip",
                     expected_state_version=version, input="message", payload={"text": text},
                     now=NOW, timezone="Europe/Moscow")


async def test_ordinary_trip_aligns_hints_and_keeps_a_multi_parameter_answer(tmp_path):
    gateway = ScriptedGateway([
        {"intent": "plan", "patch": deepcopy(ROUTE), "question": COMPOUND},
        {"intent": "plan", "patch": {**DATES, "adults": 2}, "question": None},
    ], suggestions=[
        {"id": "party", "label": "Еду один", "text": "Еду один, один взрослый."},
        {"id": "departure", "label": "28 октября", "text": "Вылетаю 28 октября 2026 года."},
    ])
    adapter = RecordingAdapter()
    service = AgentService(db_path=tmp_path / "screenshot.sqlite", gateway=gateway, adapter=adapter)
    try:
        first = await service.run_turn(turn("Полетим из Москвы в Хайфу."))
        assert first.status == "needs_input"
        assert first.message == DEPARTURE
        assert first.questions == [DEPARTURE]
        assert not adapter.calls
        context = gateway.suggestion_contexts[-1]
        assert context["latest_assistant_question"] == DEPARTURE
        assert context["latest_assistant_message"] == DEPARTURE
        assert first.suggestions
        assert all("взросл" not in item.text and "один" not in item.text for item in first.suggestions)
        await service.run_turn(turn("Вылетаем 28 октября, возвращаемся 31-го, нас двое взрослых.", first.state_version))
        assert len(adapter.calls) == 1
        query = adapter.calls[0]
        assert all(query[key] == value for key, value in {**ROUTE, **DATES, "adults": 2}.items())
        assert gateway.interpret_contexts[-1]["_dialogue"]["last_question"] == DEPARTURE
        state = await service.get_session("one-question-test", "trip")
        assert state.request.adults == 2
        assert str(state.request.departure_date) == DATES["departure_date"]
        assert str(state.request.return_date) == DATES["return_date"]
    finally:
        service.close()


async def test_fallback_waits_for_each_required_parameter_before_searching(tmp_path):
    patches = [{"destination": "Хайфа"}, {"origin": "Москва"},
               {"departure_date": "2026-10-28"}, {"return_date": "2026-10-31"}, {"adults": 1}]
    gateway = ScriptedGateway([{"intent": "plan", "patch": patch} for patch in patches])
    adapter = RecordingAdapter()
    service = AgentService(db_path=tmp_path / "progression.sqlite", gateway=gateway, adapter=adapter)
    try:
        version = 0
        for patch, expected in zip(patches[:-1], [r"откуда|из какого города|отправлен", r"вылет", r"вернут|возврат|возвращ", r"взросл"]):
            result = await service.run_turn(turn(str(patch), version))
            version = result.state_version
            assert result.status == "needs_input"
            assert len(result.questions) == 1
            assert re.search(expected, result.questions[0], re.I), result.message
            assert result.questions[0].count("?") == 1
            assert not adapter.calls
        await service.run_turn(turn("Один взрослый", version))
        assert len(adapter.calls) == 1
    finally:
        service.close()


async def test_unsupported_transport_question_still_blocks_search(tmp_path):
    question = "Подбор поездов пока не реализован в прототипе. Хотите явно изменить запрос на авиаперелет?"
    gateway = ScriptedGateway([{"intent": "plan", "patch": {**ROUTE, **DATES, "adults": 1}, "question": question}])
    adapter = RecordingAdapter()
    service = AgentService(db_path=tmp_path / "rail.sqlite", gateway=gateway, adapter=adapter)
    try:
        result = await service.run_turn(turn("Хочу ехать поездом"))
        assert result.status == "needs_input"
        assert result.message == question
        assert not adapter.calls
    finally:
        service.close()


def test_legacy_http_question_matches_next_interpretation_without_rewriting_receipts(tmp_path):
    import json
    import sqlite3
    import time
    from fastapi.testclient import TestClient
    from travel_agent.web import create_app

    database = tmp_path / "legacy-http.sqlite"
    session_id = "legacy-single-question"
    adapter = RecordingAdapter()

    def post(client, text, version):
        body = {"session_id": session_id, "request_id": str(uuid4()),
                "expected_state_version": version, "input": "message", "payload": {"text": text}}
        from http_helpers import bootstrap
        token = bootstrap(client).json()["csrf_token"]
        response = client.post("/api/turns", json=body, headers={"X-Travel-Token": token})
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] != "running":
                assert job["status"] == "completed", job
                return job_id, body["request_id"], job["result"]
            time.sleep(0.015)
        pytest.fail("Local clarification job did not finish")

    initial_gateway = ScriptedGateway([{"intent": "plan", "patch": deepcopy(ROUTE), "question": COMPOUND}])
    with TestClient(create_app(db_path=database, model_backend="rules", gateway=initial_gateway,
                              adapter=adapter), base_url="http://127.0.0.1") as client:
        job_id, request_id, first = post(client, "На концерт в Хайфу из Москвы", 0)
        cookies = dict(client.cookies)
        user_id = client.get(f"/api/sessions/{session_id}").json()["session"]["user_id"]

    # Restore an actual old-format compound response, as in the screenshot.
    legacy = deepcopy(first)
    legacy["message"] = COMPOUND
    legacy["questions"] = [COMPOUND]
    legacy["suggestions"] = [{"id": "old-party", "label": "Еду один", "text": "Еду один, один взрослый."}]
    legacy["suggestions_version"] = 3
    legacy_json = json.dumps(legacy, ensure_ascii=False)
    with sqlite3.connect(database) as connection:
        state = json.loads(connection.execute(
            "SELECT body FROM sessions WHERE user_id=? AND session_id=?", (user_id, session_id)).fetchone()[0])
        state["last_question"] = COMPOUND
        state["dialogue"][-1]["text"] = COMPOUND
        legacy_state_json = json.dumps(state, ensure_ascii=False)
        connection.execute("UPDATE sessions SET body=? WHERE user_id=? AND session_id=?", (legacy_state_json, user_id, session_id))
        connection.execute("UPDATE web_jobs SET result=? WHERE job_id=?", (legacy_json, job_id))
        connection.execute("UPDATE turns SET result=? WHERE session_id=? AND request_id=?", (legacy_json, session_id, request_id))

    restarted_gateway = ScriptedGateway([{"intent": "plan", "patch": {"departure_date": "2026-10-28"}}])
    with TestClient(create_app(db_path=database, model_backend="rules", gateway=restarted_gateway,
                              adapter=adapter), base_url="http://127.0.0.1") as client:
        for name, value in cookies.items():
            client.cookies.set(name, value, domain="127.0.0.1", path="/")
        restored = client.get(f"/api/sessions/{session_id}").json()
        assert restored["result"]["message"] == DEPARTURE
        assert restored["result"]["questions"] == [DEPARTURE]
        assert restored["messages"][-1]["text"] == DEPARTURE
        assert restored["session"]["last_question"] == DEPARTURE
        assert restored["session"]["dialogue"][-1]["text"] == DEPARTURE
        assert all(item["id"] != "old-party" and "взросл" not in item["text"] for item in restored["result"]["suggestions"])
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT body FROM sessions WHERE session_id=?", (session_id,)).fetchone()[0] == legacy_state_json
            assert connection.execute("SELECT result FROM web_jobs WHERE job_id=?", (job_id,)).fetchone()[0] == legacy_json
            assert connection.execute("SELECT result FROM turns WHERE session_id=? AND request_id=?", (session_id, request_id)).fetchone()[0] == legacy_json
        post(client, "28 октября", first["state_version"])
        context = restarted_gateway.interpret_contexts[0]["_dialogue"]
        assert context["last_question"] == DEPARTURE
        assert context["history"][-1]["text"] == DEPARTURE
        assert not adapter.calls
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT result FROM web_jobs WHERE job_id=?", (job_id,)).fetchone()[0] == legacy_json
            assert connection.execute("SELECT result FROM turns WHERE session_id=? AND request_id=?", (session_id, request_id)).fetchone()[0] == legacy_json

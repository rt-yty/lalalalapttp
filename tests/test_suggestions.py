"""Contextual next-message suggestions stay separate from travel decisions.

These scenarios exercise the deterministic rules backend and recorded fixture.
Model-driven suggestion context and failure handling are covered in test_dialogue.py.
The web case verifies that persisted suggestions survive reload and restart.
"""
from contextlib import contextmanager
import re
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from travel_agent.domain.models import TurnInput
from travel_agent.interpretation import RuleBasedGateway
from travel_agent.service import AgentService
from travel_agent.web import create_app

NOW = "2026-10-27T12:00:00+03:00"
TRIP = (
    "Из Москвы в Ереван с 24 ноября по 1 декабря 2026, один взрослый, "
    "общий бюджет 80 тысяч рублей на перелёт и отель. Багаж 23 кг и завтрак."
)
ACTION_TEXT = re.compile(r"\b(?:подтверждаю|подтвердить|оформи(?:ть)?|купить|покупаю|заброниров\w*)\b", re.I)


class CountingGateway(RuleBasedGateway):
    def __init__(self):
        self.calls = 0

    async def interpret(self, *args, **kwargs):
        self.calls += 1
        return await super().interpret(*args, **kwargs)


def turn(version=0, text=TRIP, *, kind="message", payload=None):
    return TurnInput(
        request_id=str(uuid4()), user_id="suggestion-user", session_id="trip",
        expected_state_version=version, input=kind,
        payload={"text": text} if payload is None else payload,
        now=NOW, timezone="Europe/Moscow",
    )


@contextmanager
def service_at(path, gateway=None):
    service = AgentService(db_path=path, data_mode="replay", checkout_mode="demo",
                           gateway=gateway or RuleBasedGateway())
    try:
        yield service
    finally:
        service.close()


def suggestions(result, *, required=True):
    values = result.model_dump()["suggestions"] if hasattr(result, "model_dump") else result["suggestions"]
    assert isinstance(values, list)
    assert len(values) <= 3
    if required:
        assert values
    assert len({item["id"] for item in values}) == len(values)
    for item in values:
        assert set(item) == {"id", "label", "text"}
        assert all(isinstance(item[key], str) and item[key].strip() for key in item)
    return values


async def choose(service, result):
    option = result.options[0]
    return await service.run_turn(turn(result.state_version, kind="select_option", payload={
        "option_id": option["option_id"], "options_version": option["options_version"],
    }))


async def test_rules_suggestions_run_after_search_without_extra_model_call_or_plan_mutation(tmp_path):
    gateway = CountingGateway()
    events = []
    with service_at(tmp_path / "suggest.sqlite", gateway) as service:
        result = await service.run_turn(turn(), on_event=events.append)
        assert result.status == "options_ready", result.model_dump()
        suggestions(result)
        assert gateway.calls == 1
        stages = [event["payload"]["stage"] for event in events if event["type"] == "stage_changed"]
        assert stages.count("suggestions") == 1
        assert stages.index("suggestions") > stages.index("search")
        state = await service.get_session("suggestion-user", "trip")
        assert state.request.budget_minor == 8_000_000
        assert state.request.constraints["baggage_kg"] == 23
        assert state.request.constraints["breakfast"] is True
        assert state.current_plan is None
        assert state.pending_action is None
        assert state.demo_order is None


async def test_selected_plan_receives_hints_without_automatic_checkout(tmp_path):
    gateway = CountingGateway()
    with service_at(tmp_path / "selected.sqlite", gateway) as service:
        initial = await service.run_turn(turn())
        request_before = (await service.get_session("suggestion-user", "trip")).request
        selected = await choose(service, initial)
        assert selected.status == "plan_ready", selected.model_dump()
        suggestions(selected)
        assert gateway.calls == 1  # The deterministic rules backend needs no suggestion model call.
        assert selected.current_plan["components"] == initial.options[0]["components"]
        state = await service.get_session("suggestion-user", "trip")
        assert state.request == request_before
        assert state.current_plan == selected.current_plan
        assert state.pending_action is None
        assert state.demo_order is None


async def test_exact_duplicate_preserves_suggestions_without_rerunning_graph(tmp_path):
    gateway = CountingGateway()
    with service_at(tmp_path / "duplicate.sqlite", gateway) as service:
        body = turn()
        first_events, repeat_events = [], []
        first = await service.run_turn(body, on_event=first_events.append)
        again = await service.run_turn(body, on_event=repeat_events.append)
        assert suggestions(first) == suggestions(again)
        assert first.model_dump() == again.model_dump()
        assert gateway.calls == 1
        assert repeat_events == []
        assert sum(e["type"] == "stage_changed" and e["payload"].get("stage") == "suggestions" for e in first_events) == 1


async def test_no_match_hints_do_not_relax_saved_budget_or_baggage(tmp_path):
    with service_at(tmp_path / "budget.sqlite") as service:
        initial = await service.run_turn(turn())
        before = await service.get_session("suggestion-user", "trip")
        no_match = await service.run_turn(turn(initial.state_version, "Бюджет теперь 50 тысяч рублей на всех"))
        assert no_match.status == "partial", no_match.model_dump()
        assert no_match.options == []
        suggestions(no_match, required=False)
        after = await service.get_session("suggestion-user", "trip")
        assert after.request.budget_minor == 5_000_000
        assert after.request.constraints == before.request.constraints
        assert after.request.constraints["baggage_kg"] == 23
        assert after.request.constraints["breakfast"] is True
        assert after.current_plan is None
        assert after.pending_action is None


async def test_missing_party_hints_help_answer_the_actual_clarification(tmp_path):
    with service_at(tmp_path / "clarify.sqlite") as service:
        result = await service.run_turn(turn(text="Из Москвы в Ереван с 24 ноября по 1 декабря 2026"))
        assert result.status == "needs_input", result.model_dump()
        hints = suggestions(result)
        assert any(re.search(r"взрос|еду один|еду одна|поеду один|поеду одна|вдво|двое", hint["text"], re.I) for hint in hints), hints
        state = await service.get_session("suggestion-user", "trip")
        assert state.request.adults is None
        assert str(state.request.departure_date) == "2026-11-24"
        assert state.options == []


async def test_pending_and_completed_hints_cannot_confirm_or_purchase(tmp_path):
    with service_at(tmp_path / "confirmation.sqlite") as service:
        initial = await service.run_turn(turn())
        chosen = await choose(service, initial)
        pending = await service.run_turn(turn(chosen.state_version, "Оформить"))
        assert pending.status == "needs_confirmation", pending.model_dump()
        for hint in suggestions(pending, required=False):
            assert not ACTION_TEXT.search(hint["text"]), hint
        action = {key: pending.pending_action[key] for key in ("action_id", "plan_id", "plan_version", "payload_hash")}
        completed = await service.run_turn(turn(pending.state_version, kind="confirm_action", payload=action))
        assert completed.status == "completed_demo", completed.model_dump()
        for hint in suggestions(completed, required=False):
            assert not ACTION_TEXT.search(hint["text"]), hint
        state = await service.get_session("suggestion-user", "trip")
        assert state.demo_order["local_id"] == completed.demo_order["local_id"]
        assert state.pending_action is None


def test_web_restores_same_suggestions_after_reload_and_restart(tmp_path):
    db = tmp_path / "web-suggestions.sqlite"
    session_id = str(uuid4())
    body = {"session_id": session_id, "request_id": str(uuid4()), "expected_state_version": 0,
            "input": "message", "payload": {"text": TRIP}}
    with TestClient(create_app(db_path=db, model_backend="rules", data_mode="replay"), base_url="http://127.0.0.1") as client:
        from http_helpers import bootstrap
        token = bootstrap(client).json()["csrf_token"]
        response = client.post("/api/turns", json=body, headers={"X-Travel-Token": token})
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] != "running":
                break
            time.sleep(0.015)
        else:
            pytest.fail("Suggestion job did not finish")
        assert job["status"] == "completed", job
        original_hints = suggestions(job["result"])
        cookies = dict(client.cookies)
        view = client.get(f"/api/sessions/{session_id}").json()
        assert suggestions(view["result"]) == original_hints
    with TestClient(create_app(db_path=db, model_backend="rules", data_mode="replay"), base_url="http://127.0.0.1") as client:
        for name, value in cookies.items():
            client.cookies.set(name, value, domain="127.0.0.1", path="/")
        view = client.get(f"/api/sessions/{session_id}").json()
        assert suggestions(view["result"]) == original_hints
        assert view["session"]["request"]["budget_minor"] == 8_000_000
        assert view["session"]["request"]["constraints"]["baggage_kg"] == 23


def web_body(session_id=None, *, version=0, text=TRIP):
    return {"session_id": session_id or str(uuid4()), "request_id": str(uuid4()),
            "expected_state_version": version, "input": "message", "payload": {"text": text}}


def finish_web_turn(client, body):
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
            return job["result"]
        time.sleep(0.015)
    pytest.fail("Suggestion job did not finish")


def test_legacy_history_gets_contextual_suggestions_without_rewriting_receipts(tmp_path):
    import json
    import sqlite3
    db = tmp_path / "legacy.sqlite"
    body = web_body()
    with TestClient(create_app(db_path=db, model_backend="rules"), base_url="http://127.0.0.1") as client:
        initial = finish_web_turn(client, body)
        expected = suggestions(initial)
        cookies = dict(client.cookies)
    # Simulate pre-feature history, which did not have the suggestions field.
    legacy = {key: value for key, value in initial.items() if key != "suggestions"}
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE web_jobs SET result=? WHERE session_id=?", (json.dumps(legacy), body["session_id"]))
        connection.execute("UPDATE turns SET result=? WHERE session_id=?", (json.dumps(legacy), body["session_id"]))
    with TestClient(create_app(db_path=db, model_backend="rules"), base_url="http://127.0.0.1") as client:
        for name, value in cookies.items():
            client.cookies.set(name, value, domain="127.0.0.1", path="/")
        restored = client.get(f"/api/sessions/{body['session_id']}").json()
        assert suggestions(restored["result"]) == expected
        assert restored["session"]["state_version"] == initial["state_version"]
    with sqlite3.connect(db) as connection:
        for table in ("web_jobs", "turns"):
            saved = json.loads(connection.execute(f"SELECT result FROM {table} WHERE session_id=?", (body["session_id"],)).fetchone()[0])
            assert "suggestions" not in saved
            assert saved == legacy


def test_stale_saved_suggestions_are_suppressed_on_session_restore(tmp_path):
    import json
    import sqlite3
    db = tmp_path / "stale-suggestions.sqlite"
    body = web_body()
    with TestClient(create_app(db_path=db, model_backend="rules"), base_url="http://127.0.0.1") as client:
        initial = finish_web_turn(client, body)
        suggestions(initial)
        cookies = dict(client.cookies)
    outdated = dict(initial, state_version=initial["state_version"] - 1)
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE web_jobs SET result=? WHERE session_id=?", (json.dumps(outdated), body["session_id"]))
    with TestClient(create_app(db_path=db, model_backend="rules"), base_url="http://127.0.0.1") as client:
        for name, value in cookies.items():
            client.cookies.set(name, value, domain="127.0.0.1", path="/")
        restored = client.get(f"/api/sessions/{body['session_id']}").json()
        assert restored["result"]["suggestions"] == []
        assert restored["result"]["state_version"] == restored["session"]["state_version"] == initial["state_version"]
        assert restored["result"]["options"] == initial["options"]


def test_error_response_does_not_restore_previous_action_suggestions(tmp_path):
    db = tmp_path / "error-suggestions.sqlite"
    body = web_body()
    with TestClient(create_app(db_path=db, model_backend="rules"), base_url="http://127.0.0.1") as client:
        initial = finish_web_turn(client, body)
        suggestions(initial)
        failed = finish_web_turn(client, web_body(body["session_id"], version=0, text="Бюджет 50 тысяч на всех"))
        assert failed["status"] == "error"
        assert failed["error"]["code"] == "state_conflict"
        assert failed["suggestions"] == []
        restored = client.get(f"/api/sessions/{body['session_id']}").json()
        assert restored["result"]["suggestions"] == []
        assert restored["result"]["options"] == initial["options"]
        assert restored["session"]["request"]["budget_minor"] == 8_000_000

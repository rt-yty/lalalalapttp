"""Adversarial HTTP sequences against a temporary instance of the real service.

No external model or travel provider is used. These tests check the boundary
that remains important even when interpretation is probabilistic.
"""
import asyncio
from copy import deepcopy
import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from travel_agent.interpretation import RuleBasedGateway
from travel_agent.web import create_app
from test_web import TRIP, client_at, submit, turn_body, wait_job
from http_helpers import bootstrap


INVALID = [
    (field, value)
    for field, values in {
        "session_id": ["", "../private", "a" * 101, None, 123, "\u0000"],
        "request_id": ["", "../private", "a" * 101, None, 123, "\n"],
        "expected_state_version": [-1, True, "0", 1.5, None],
        "input": ["", "book", "__proto__", None, 1],
        "payload": [None, [], "text", 7, {}],
    }.items()
    for value in values
]


@pytest.mark.parametrize("field,value", INVALID)
def test_invalid_envelopes_do_not_start_jobs_or_change_saved_trip(tmp_path, field, value):
    with client_at(tmp_path / "invalid.sqlite") as client:
        initial = turn_body(session="existing")
        submit(client, initial)
        before = client.get("/api/sessions/existing").json()
        body = turn_body(session="existing", version=1)
        body[field] = value
        response = client.post("/api/turns", json=body)
        assert response.status_code == 422, response.text
        assert client.get("/api/sessions/existing").json() == before


@pytest.mark.parametrize("text", ["", " ", "\n\t", "x" * 12001, None, 123, [], {}, True])
def test_invalid_messages_have_no_domain_side_effect(tmp_path, text):
    with client_at(tmp_path / "message.sqlite") as client:
        response = client.post("/api/turns", json=turn_body(session="empty", text=text))
        assert response.status_code == 422, response.text
        view = client.get("/api/sessions/empty").json()
        assert view["session"] is None and view["messages"] == [] and view["active_job_id"] is None


@pytest.mark.parametrize("payload,kind", [
    ({"text": "broken \ud800"}, "message"),
    ({"component_id": "\udfff", "options_version": 1}, "select_component"),
    ({"option_id": "\ud800", "options_version": 1}, "select_option"),
])
def test_invalid_unicode_is_rejected_before_sqlite_or_job_creation(tmp_path, payload, kind):
    with client_at(tmp_path / "unicode.sqlite") as client:
        body = turn_body(session="unicode", kind=kind, payload=payload)
        response = client.post("/api/turns", content=json.dumps(body, ensure_ascii=True),
                               headers={"Content-Type": "application/json"})
        assert response.status_code == 422, response.text
        assert client.get("/api/sessions/unicode").json()["messages"] == []


def test_unicode_and_markup_are_plain_text_and_survive_restart(tmp_path):
    path = tmp_path / "text.sqlite"
    text = 'Привет 👩🏽‍🚀 你好 <script>globalThis.audit=true</script> & "quotes"\nМосква'
    body = turn_body(session="text", text=text)
    with client_at(path) as client:
        result = submit(client, body)["result"]
        assert result["status"] != "error"
        view = client.get("/api/sessions/text").json()
        assert view["messages"][0]["text"] == text
        assert view["session"]["current_plan"] is None
    with client_at(path) as client:
        assert client.get("/api/sessions/text").json()["messages"] == view["messages"]


def test_concurrent_http_queue_deduplicates_and_isolates_sessions(tmp_path):
    release = threading.Event()

    class GateGateway(RuleBasedGateway):
        def __init__(self):
            self.calls = 0

        async def interpret(self, *args, **kwargs):
            self.calls += 1
            while not release.is_set():
                await asyncio.sleep(.005)
            return await super().interpret(*args, **kwargs)

    gateway = GateGateway()
    app = create_app(db_path=tmp_path / "queue.sqlite", model_backend="rules", data_mode="replay", gateway=gateway)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        bootstrap(client)
        bodies = [turn_body(session=f"parallel-{i}") for i in range(9)]
        jobs = []
        try:
            for body in bodies[:8]:
                response = client.post("/api/turns", json=body)
                assert response.status_code == 202
                jobs.append(response.json()["job_id"])
            repeat = client.post("/api/turns", json=deepcopy(bodies[0]))
            assert repeat.status_code == 202 and repeat.json()["job_id"] == jobs[0]
            collision = client.post("/api/turns", json=turn_body(session="parallel-0", text="Еду вдвоём"))
            assert collision.status_code == 409
            crowded = client.post("/api/turns", json=bodies[8])
            assert crowded.status_code == 429
            for i, job_id in enumerate(jobs):
                view = client.get(f"/api/sessions/parallel-{i}").json()
                assert view["active_job_id"] == job_id and len(view["messages"]) == 1
        finally:
            release.set()
        for job_id in jobs:
            result = wait_job(client, job_id)["result"]
            assert result["status"] == "options_ready" and result["state_version"] == 1
        assert gateway.calls == 8
        assert submit(client, bodies[8])["result"]["state_version"] == 1


def test_component_choices_restore_after_error_and_exact_retry(tmp_path):
    path = tmp_path / "component.sqlite"
    with client_at(path) as client:
        body = turn_body(session="components")
        first = submit(client, body)["result"]
        flight = first["transport_options"][0]
        selection = turn_body(session="components", version=1, kind="select_component",
            payload={"component_id": flight["component_id"], "options_version": flight["options_version"]})
        selected = submit(client, selection)["result"]
        assert selected["current_plan"]["missing_products"] == ["hotel"]
        stale = submit(client, turn_body(session="components", version=0, text="Бюджет 1 рубль"))["result"]
        assert stale["error"]["code"] == "state_conflict"
        assert submit(client, deepcopy(selection))["result"] == selected
        view = client.get("/api/sessions/components").json()
        assert view["session"]["selected_components"] == selected["selected_components"]
        assert view["session"]["request"]["budget_minor"] == 8_000_000
    with client_at(path) as client:
        restored = client.get("/api/sessions/components").json()
        assert restored["session"] == view["session"]
        assert restored["result"]["selected_components"] == selected["selected_components"]
        assert restored["result"]["hotel_options"] == selected["hotel_options"]


@pytest.mark.parametrize("first_text", [TRIP, "Из Москвы в Ереван, один взрослый"])
def test_chat_response_survives_reload_without_reviving_trip_suggestions(tmp_path, first_text):
    class ChatGateway(RuleBasedGateway):
        async def interpret(self, text, *args, **kwargs):
            if text == "17 × 23?":
                return {"intent": "chat", "patch": {}, "reply_text": "17 × 23 = 391."}
            return await super().interpret(text, *args, **kwargs)

    path = tmp_path / "chat.sqlite"
    app = create_app(db_path=path, model_backend="rules", data_mode="replay", gateway=ChatGateway())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        bootstrap(client)
        initial = submit(client, turn_body(session="chat", text=first_text))["result"]
        before = client.get("/api/sessions/chat").json()["session"]
        result = submit(client, turn_body(session="chat", version=initial["state_version"], text="17 × 23?"))["result"]
        assert result["response_kind"] == "chat" and result["message"] == "17 × 23 = 391."
        assert result["suggestions"] == [] and result["questions"] == []
        after = client.get("/api/sessions/chat").json()["session"]
        cookies = dict(client.cookies)
        for key in ("request", "last_question", "current_plan", "pending_action", "transport_options", "hotel_options"):
            assert after[key] == before[key]
    # Old cached chips are presentation data, not an instruction to revive a
    # trip flow after ordinary conversation. Rebuild without rewriting receipt.
    with sqlite3.connect(path) as db:
        job_id, serialized = db.execute("SELECT job_id,result FROM web_jobs ORDER BY rowid DESC LIMIT 1").fetchone()
        legacy = json.loads(serialized)
        legacy["suggestions_version"] = 5
        legacy["suggestions"] = [{"id": "old", "label": "Выбрать отель", "text": "Выбираю отель 99."}]
        db.execute("UPDATE web_jobs SET result=? WHERE job_id=?", (json.dumps(legacy), job_id))
    with client_at(path, cookies=cookies) as client:
        restored = client.get("/api/sessions/chat").json()
        assert restored["result"]["response_kind"] == "chat"
        assert restored["result"]["message"] == "17 × 23 = 391."
        assert restored["result"]["suggestions"] == []
        assert restored["result"]["suggestions_version"] == 6
        assert restored["messages"][-1]["text"] == "17 × 23 = 391."
        assert client.get(f"/api/jobs/{job_id}").json()["result"]["suggestions_version"] == 5

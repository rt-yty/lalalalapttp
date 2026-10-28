"""HTTP boundary checks: a real core scenario, recovery and local-only access.

These use the deterministic gateway and the recorded provider fixture; no model
or external provider is contacted.
"""
from contextlib import contextmanager
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from travel_agent.web import create_app
from http_helpers import bootstrap

TRIP = (
    "Из Москвы в Ереван с 24 ноября по 1 декабря 2026, один взрослый, "
    "общий бюджет 80 тысяч рублей на перелёт и отель. Багаж 23 кг и завтрак."
)


_browser_cookies = {}


@contextmanager
def client_at(db_path, *, cookies=None):
    """Reopen the same test browser, including its persistent visitor cookie."""
    app = create_app(db_path=db_path, model_backend="rules", data_mode="replay")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for name, value in (cookies or _browser_cookies.get(str(db_path), {})).items():
            client.cookies.set(name, value, domain="127.0.0.1", path="/")
        bootstrap(client)
        try:
            yield client
        finally:
            _browser_cookies[str(db_path)] = dict(client.cookies)


@pytest.fixture
def client(tmp_path):
    with client_at(tmp_path / "web.sqlite") as client:
        yield client


def turn_body(session=None, version=0, text=TRIP, *, kind="message", payload=None,
              request_id=None):
    return {
        "session_id": session or str(uuid4()),
        "request_id": request_id or str(uuid4()),
        "expected_state_version": version,
        "input": kind,
        "payload": payload if payload is not None else {"text": text},
    }


def wait_job(client, job_id):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] != "running":
            assert job["status"] == "completed", job
            assert job.get("result") is not None, job
            return job
        time.sleep(0.015)
    pytest.fail("Local replay job did not complete within 8 seconds")


def submit(client, body):
    response = client.post("/api/turns", json=body)
    assert response.status_code == 202, response.text
    return wait_job(client, response.json()["job_id"])


def test_web_full_trip_to_one_demo_order(client):
    body = turn_body()
    first = submit(client, body)
    result = first["result"]
    assert result["status"] == "options_ready", result
    assert result["state_version"] == 1
    assert first["events"]
    sequences = [e["sequence"] for e in first["events"]]
    assert sequences == sorted(set(sequences))
    option = next(o for o in result["options"] if o["total_minor"] == 6_792_000)
    selected = submit(client, turn_body(body["session_id"], result["state_version"],
        kind="select_option", payload={"option_id": option["option_id"],
                                       "options_version": option["options_version"]}))["result"]
    assert selected["status"] == "plan_ready", selected
    pending = submit(client, turn_body(body["session_id"], selected["state_version"],
                                     text="Оформить"))["result"]
    assert pending["status"] == "needs_confirmation", pending
    action = {key: pending["pending_action"][key] for key in
              ("action_id", "plan_id", "plan_version", "payload_hash")}
    done = submit(client, turn_body(body["session_id"], pending["state_version"],
                                  kind="confirm_action", payload=action))["result"]
    assert done["status"] == "completed_demo", done
    assert done["demo_order"]["total_minor"] == 6_792_000
    assert done["demo_order"]["mode"] == "demo"
    assert "не бронировал" in done["message"]
    again = submit(client, turn_body(body["session_id"], done["state_version"],
                                   kind="confirm_action", payload=action))["result"]
    assert again["demo_order"]["local_id"] == done["demo_order"]["local_id"]
    restored = client.get(f"/api/sessions/{body['session_id']}").json()
    assert restored["session"]["demo_order"]["local_id"] == done["demo_order"]["local_id"]
    assert restored["session"]["pending_action"] is None
    assert restored["messages"][0]["text"] == TRIP
    assert restored["result"]["status"] == "completed_demo"


def test_repeated_http_submission_reuses_job(client):
    body = turn_body()
    first = client.post("/api/turns", json=body)
    again = client.post("/api/turns", json=body)
    assert first.status_code == again.status_code == 202
    assert first.json()["job_id"] == again.json()["job_id"]
    done = wait_job(client, first.json()["job_id"])
    completed_again = client.post("/api/turns", json=body)
    assert completed_again.status_code == 202
    assert completed_again.json()["job_id"] == first.json()["job_id"]
    restored = client.get(f"/api/sessions/{body['session_id']}").json()
    assert restored["session"]["state_version"] == done["result"]["state_version"] == 1
    assert len([m for m in restored["messages"] if m["role"] == "user"]) == 1


def test_same_request_id_cannot_change_body(client):
    body = turn_body()
    submit(client, body)
    conflicting = dict(body, payload={"text": "Бюджет теперь 20 тысяч на всех"})
    response = client.post("/api/turns", json=conflicting)
    assert response.status_code == 409, response.text


def test_session_history_and_idempotency_survive_restart(tmp_path):
    db = tmp_path / "restart.sqlite"
    body = turn_body()
    with client_at(db) as client:
        done = submit(client, body)
        original = client.get(f"/api/sessions/{body['session_id']}").json()
    with client_at(db) as client:
        restored = client.get(f"/api/sessions/{body['session_id']}").json()
        assert restored["session"] == original["session"]
        assert restored["messages"] == original["messages"]
        assert restored["result"] == original["result"]
        repeated = submit(client, body)
        assert repeated["result"] == done["result"]
        assert repeated["result"]["state_version"] == 1
        after_repeat = client.get(f"/api/sessions/{body['session_id']}").json()
        assert after_repeat["messages"] == original["messages"]


def test_stale_version_cannot_overwrite_trip(client):
    body = turn_body()
    submit(client, body)
    result = submit(client, turn_body(body["session_id"], 0,
                                    text="Бюджет теперь 20 тысяч на всех"))["result"]
    assert result["status"] == "error"
    assert result["error"]["code"] == "state_conflict"
    restored = client.get(f"/api/sessions/{body['session_id']}").json()
    assert restored["session"]["state_version"] == 1
    assert restored["session"]["request"]["budget_minor"] == 8_000_000
    assert restored["result"]["options"] == restored["session"]["options"]
    assert restored["result"]["options"]


@pytest.mark.parametrize("token", [None, "invalid-token"])
def test_mutation_requires_bootstrap_token(client, token):
    client.headers.pop("X-Travel-Token", None)
    headers = {"X-Travel-Token": token} if token else {}
    response = client.post("/api/turns", json=turn_body(), headers=headers)
    assert response.status_code == 403, response.text


def test_cross_origin_write_is_rejected_even_with_token(client):
    response = client.post("/api/turns", json=turn_body(),
                           headers={"Origin": "https://unrelated.example"})
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("field,value", [("user_id", "other-user"),
                                        ("now", "2000-01-01T00:00:00Z"),
                                        ("timezone", "UTC")])
def test_browser_cannot_override_server_identity_or_clock(client, field, value):
    response = client.post("/api/turns", json=turn_body() | {field: value})
    assert response.status_code == 422, response.text


def test_malformed_action_does_not_start_work(client):
    response = client.post("/api/turns", json=turn_body(kind="register_checkout_passengers"))
    assert response.status_code == 422, response.text


def test_unstarted_session_is_empty(client):
    response = client.get(f"/api/sessions/{uuid4()}")
    assert response.status_code == 200
    assert response.json()["session"] is None
    assert response.json()["messages"] == []


def test_unknown_job_is_not_a_success(client):
    response = client.get(f"/api/jobs/{uuid4()}")
    assert response.status_code == 404


def test_non_local_host_is_rejected(client):
    response = client.get("/api/bootstrap", headers={"Host": "attacker.example"})
    assert response.status_code in (400, 403), response.text


def test_raw_project_archives_are_not_web_assets(client):
    response = client.get("/exports/tutu-yerevan-2026-10-26/avia_selected_checkout.json")
    assert response.status_code == 404


def test_restart_recovers_domain_commit_before_job_receipt(tmp_path):
    """A crash between core commit and HTTP-job finalization must not repeat it."""
    import sqlite3
    db = tmp_path / "crash-gap.sqlite"
    body = turn_body()
    with client_at(db) as client:
        done = submit(client, body)
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE web_jobs SET status='running',result=NULL WHERE session_id=? AND request_id=?",
                           (body["session_id"], body["request_id"]))
    with client_at(db) as client:
        recovered = submit(client, body)
        assert recovered["result"] == done["result"]
        restored = client.get(f"/api/sessions/{body['session_id']}").json()
        assert restored["session"]["state_version"] == 1
        assert restored["active_job_id"] is None


def test_reload_can_find_active_job_then_restart_marks_uncommitted_job_failed(tmp_path):
    import asyncio
    from travel_agent.interpretation import RuleBasedGateway

    class SlowGateway(RuleBasedGateway):
        async def interpret(self, *args, **kwargs):
            await asyncio.sleep(3600)
            return await super().interpret(*args, **kwargs)

    db = tmp_path / "interrupted.sqlite"
    body = turn_body()
    app = create_app(db_path=db, model_backend="rules", data_mode="replay", gateway=SlowGateway())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = bootstrap(client).json()["csrf_token"]
        response = client.post("/api/turns", json=body, headers={"X-Travel-Token": token})
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        view = client.get(f"/api/sessions/{body['session_id']}").json()
        assert view["active_job_id"] == job_id
        assert view["session"] is None
        cookies = dict(client.cookies)
    with client_at(db, cookies=cookies) as client:
        status = client.get(f"/api/jobs/{job_id}").json()
        assert status["status"] == "failed"
        assert status["error"]
        view = client.get(f"/api/sessions/{body['session_id']}").json()
        assert view["active_job_id"] is None
        assert view["session"] is None
        retried = submit(client, turn_body(body["session_id"]))
        assert retried["result"]["status"] == "options_ready"
        assert retried["result"]["state_version"] == 1


def test_selected_plan_is_restored_after_stale_request_error(client):
    body = turn_body()
    options = submit(client, body)["result"]
    option = next(o for o in options["options"] if o["eligibility"] == "satisfied")
    selected = submit(client, turn_body(body["session_id"], options["state_version"],
        kind="select_option", payload={"option_id": option["option_id"],
                                       "options_version": option["options_version"]}))["result"]
    assert selected["status"] == "plan_ready"
    stale = submit(client, turn_body(body["session_id"], 0, text="Бюджет 20 тысяч на всех"))["result"]
    assert stale["error"]["code"] == "state_conflict"
    restored = client.get(f"/api/sessions/{body['session_id']}").json()
    assert restored["session"]["current_plan"] == selected["current_plan"]
    assert restored["result"]["current_plan"] == restored["session"]["current_plan"]
    assert restored["result"]["state_version"] == selected["state_version"]

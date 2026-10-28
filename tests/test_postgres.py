"""Integration tests against a real Postgres server in a disposable schema."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from travel_agent.domain.models import TurnInput
from travel_agent.storage.factory import create_repository
from travel_agent.web import create_app
from test_web import submit, turn_body
from test_backend_security import bootstrap


@pytest.fixture
def postgres_url():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is needed for real PostgreSQL integration")
    import psycopg
    schema = "test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["options"] = "-csearch_path=" + schema
    scoped = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    try:
        yield scoped
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema)))


def test_postgres_http_booking_restart_profile_and_guest_isolation(postgres_url):
    body = turn_body(session="booking")
    with TestClient(create_app(postgres_url, model_backend="rules"), base_url="http://127.0.0.1") as client:
        bootstrap(client)
        assert client.get("/api/health").json() == {"status": "ok", "storage": "postgres"}
        found = submit(client, body)["result"]
        for card in (found["transport_options"][0], found["hotel_options"][0]):
            found = submit(client, turn_body(session="booking", version=found["state_version"], kind="select_component", payload={"component_id": card["component_id"], "options_version": card["options_version"]}))["result"]
        proposal = submit(client, turn_body(session="booking", version=found["state_version"], text="Оформить"))["result"]
        assert proposal["status"] == "needs_confirmation"
        action = {key: proposal["pending_action"][key] for key in ("action_id", "plan_id", "plan_version", "payload_hash")}
        confirmation = turn_body(session="booking", version=proposal["state_version"], kind="confirm_action", payload=action)
        booked = submit(client, confirmation)["result"]
        assert booked["status"] == "completed_demo"
        assert submit(client, confirmation)["result"] == booked
        client.patch("/api/profile", json={"origin": "Москва", "preferences": ["Без пересадок"]})
        cookies = dict(client.cookies)
        original = client.get("/api/sessions/booking").json()
    with TestClient(create_app(postgres_url, model_backend="rules"), base_url="http://127.0.0.1") as client:
        client.cookies.update(cookies)
        bootstrap(client)
        assert client.get("/api/sessions/booking").json() == original
        assert client.get("/api/profile").json()["origin"] == "Москва"
        assert client.get("/api/sessions").json()["sessions"][0]["session_id"] == "booking"
        assert submit(client, confirmation)["result"] == booked
        client.cookies.clear()
        bootstrap(client)
        assert client.get("/api/sessions/booking").json()["session"] is None
        assert client.get("/api/profile").json()["origin"] == ""


def test_postgres_concurrent_first_commits_and_idempotency(postgres_url):
    first, second = create_repository(postgres_url), create_repository(postgres_url)
    now = datetime.fromisoformat("2026-10-28T10:00:00+03:00")
    turns = [TurnInput(user_id="guest", session_id="race", request_id=str(i), expected_state_version=0, input="message", payload={"text": "Привет"}, now=now) for i in range(2)]
    state = {"state_version": 1}
    result = {"status": "needs_input", "message": "Привет"}
    try:
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(repo.commit, turn, "digest", state, result) for repo, turn in zip((first, second), turns)]
            outcomes = [future.result() for future in futures]
        assert sorted(outcome[0] for outcome in outcomes) == ["committed", "state_conflict"]
        winner = turns[next(i for i, outcome in enumerate(outcomes) if outcome[0] == "committed")]
        assert first.commit(winner, "digest", state, result) == ("repeat", result)
        assert first.commit(winner, "other", state, result)[0] == "request_id_conflict"
        assert first.session("guest", "race") == state
        assert first.session("other", "race") is None
    finally:
        first.close()
        second.close()


def test_postgres_domain_commit_is_recovered_after_job_receipt_loss(postgres_url):
    with TestClient(create_app(postgres_url, model_backend="rules"), base_url="http://127.0.0.1") as client:
        bootstrap(client)
        body = turn_body(session="recovery")
        completed = submit(client, body)
        cookies = dict(client.cookies)
    repo = create_repository(postgres_url)
    repo.conn.execute("UPDATE web_jobs SET status='running',result=NULL WHERE job_id=?", (completed["job_id"],))
    repo.close()
    with TestClient(create_app(postgres_url, model_backend="rules"), base_url="http://127.0.0.1") as client:
        client.cookies.update(cookies)
        bootstrap(client)
        assert submit(client, body)["result"] == completed["result"]
        assert client.get("/api/sessions/recovery").json()["session"]["state_version"] == 1


def test_postgres_order_failure_rolls_back_state_and_receipt(postgres_url):
    repo = create_repository(postgres_url)
    order = {"action_id": "action", "local_id": "already-used"}
    repo.conn.execute("INSERT INTO demo_orders VALUES(?,?,?,?,?)", ("someone", "previous", "action", "already-used", json.dumps(order)))
    turn = TurnInput(user_id="guest", session_id="atomic", request_id="attempt", expected_state_version=0, input="message", payload={"text": "Оформить"}, now="2026-10-28T10:00:00+03:00")
    try:
        with pytest.raises(RuntimeError):
            repo.commit(turn, "digest", {"state_version": 1}, {"status": "completed_demo", "demo_order": order})
        assert repo.session("guest", "atomic") is None
        assert repo.turn("guest", "atomic", "attempt") is None
        assert repo.order("someone", "previous", "action") == order
    finally:
        repo.close()


def test_postgres_second_server_cannot_reconcile_active_first_server(postgres_url):
    with TestClient(create_app(postgres_url, model_backend="rules"), base_url="http://127.0.0.1") as first:
        bootstrap(first)
        with pytest.raises(RuntimeError, match="уже запущен сервер"):
            with TestClient(create_app(postgres_url, model_backend="rules"), base_url="http://127.0.0.1"):
                pytest.fail("Second worker must not acquire the same database")
        assert first.get("/api/health").json()["status"] == "ok"

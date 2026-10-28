"""Retry terminal HTTP job failures without duplicating a committed action."""
import asyncio
import threading
import time

import pytest

from test_web import client_at, turn_body, wait_job


def await_failed(client, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = client.get(f"/api/jobs/{job_id}").json()
        if status["status"] == "failed":
            return status
        time.sleep(.01)
    pytest.fail("The injected HTTP job failure did not become terminal")


def test_failed_job_retry_reuses_original_input_and_recovers(tmp_path):
    with client_at(tmp_path / "retry.sqlite") as client:
        original = client.app.state.service.run_turn
        calls = []

        async def flaky(turn, **kwargs):
            calls.append(turn.model_dump(mode="json"))
            if len(calls) == 1:
                raise RuntimeError("temporary executor failure")
            return await original(turn, **kwargs)

        client.app.state.service.run_turn = flaky
        body = turn_body(session="retry")
        job_id = client.post("/api/turns", json=body).json()["job_id"]
        await_failed(client, job_id)
        retried = client.post("/api/turns", json=body)
        assert retried.status_code == 202 and retried.json()["job_id"] == job_id
        done = wait_job(client, job_id)
        assert done["result"]["status"] == "options_ready"
        assert calls[0] == calls[1]  # Includes server timestamp/idempotency input.
        messages = client.get("/api/sessions/retry").json()["messages"]
        assert len([message for message in messages if message["role"] == "user"]) == 1


def test_failed_job_retry_reconciles_committed_receipt_without_execution(tmp_path):
    with client_at(tmp_path / "committed.sqlite") as client:
        original = client.app.state.service.run_turn
        calls, saved = [], []

        async def commit_then_fail(turn, **kwargs):
            calls.append(turn.request_id)
            result = await original(turn, **kwargs)
            saved.append(result.model_dump(mode="json"))
            raise RuntimeError("failure after domain commit")

        client.app.state.service.run_turn = commit_then_fail
        body = turn_body(session="committed")
        job_id = client.post("/api/turns", json=body).json()["job_id"]
        await_failed(client, job_id)
        assert client.post("/api/turns", json=body).json()["job_id"] == job_id
        assert wait_job(client, job_id)["result"] == saved[0]
        assert calls == [body["request_id"]]


def test_failed_job_retry_does_not_compete_with_an_active_session_job(tmp_path):
    release = threading.Event()
    with client_at(tmp_path / "active.sqlite") as client:
        original = client.app.state.service.run_turn
        calls = []

        async def gated(turn, **kwargs):
            calls.append(turn.request_id)
            if len(calls) == 1:
                raise RuntimeError("temporary executor failure")
            while not release.is_set():
                await asyncio.sleep(.01)
            return await original(turn, **kwargs)

        client.app.state.service.run_turn = gated
        body = turn_body(session="active")
        job_id = client.post("/api/turns", json=body).json()["job_id"]
        await_failed(client, job_id)
        next_body = turn_body(session="active", text="Привет")
        next_job = client.post("/api/turns", json=next_body).json()["job_id"]
        try:
            response = client.post("/api/turns", json=body)
            assert response.status_code == 409
            assert client.get(f"/api/jobs/{job_id}").json()["status"] == "failed"
        finally:
            release.set()
        wait_job(client, next_job)
        assert len(calls) == 2

"""Web research, credential lifetime and replay-to-live continuation without network."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import os
import sqlite3
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from travel_agent.adapters.search_yandex import YandexSearchAdapter
from travel_agent.domain.models import TurnInput
from travel_agent.interpretation import RuleBasedGateway
from travel_agent.service import AgentService
from travel_agent.web import create_app

NOW = "2026-10-27T12:00:00+03:00"
CONCERT = "Покажи афишу концертов Моргена, город и дата любые"
TRIP = "Из Москвы в Ереван с 24 ноября по 1 декабря 2026, один взрослый, общий бюджет 80 тысяч рублей на перелёт и отель. Багаж 23 кг и завтрак."
SOURCE = {"id": "yandex-test", "title": "Тестовая афиша", "url": "https://example.org/concert", "snippet": "Тестовый фрагмент афиши; даты и наличие билетов не подтверждены."}


class FakeSearch:
    def __init__(self, configured=True):
        self.configured = configured
        self.calls = []

    async def search(self, query, now):
        self.calls.append(query)
        return {"provider": "yandex", "query": query, "results": [deepcopy(SOURCE)] if self.configured else [], "fetched_at": now,
                "error": None if self.configured else {"code": "search_unconfigured", "message": "Не подключено", "retryable": False}}


class NoTravelCalls:
    def __init__(self):
        self.calls = 0

    async def search(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("Research must not start travel search without a chosen route")


class RecordingGateway(RuleBasedGateway):
    def __init__(self):
        self.inputs = []

    async def interpret(self, text, request, *args, **kwargs):
        self.inputs.append({"text": text, "request": deepcopy(request)})
        return await super().interpret(text, request, *args, **kwargs)


def core_turn(version=0, text=CONCERT):
    return TurnInput(request_id=str(uuid4()), user_id="research-user", session_id="concert",
                     expected_state_version=version, input="message", payload={"text": text}, now=NOW)


@contextmanager
def core_at(path, search=None, gateway=None):
    travel = NoTravelCalls()
    service = AgentService(db_path=path, gateway=gateway or RecordingGateway(), adapter=travel,
                           data_mode="live", web_search_adapter=search or FakeSearch())
    try:
        yield service, travel
    finally:
        service.close()


_browser_cookies = {}


@contextmanager
def web_at(path, *, mode="replay", search=None, gateway=None, travel=None):
    app = create_app(db_path=path, model_backend="rules", data_mode=mode,
                     gateway=gateway or RecordingGateway(), adapter=travel,
                     web_search_adapter=search or FakeSearch(), allow_local_configuration=True)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for name, value in _browser_cookies.get(str(path), {}).items():
            client.cookies.set(name, value, domain="127.0.0.1", path="/")
        from http_helpers import bootstrap
        bootstrap(client)
        try:
            yield client
        finally:
            _browser_cookies[str(path)] = dict(client.cookies)


def body(session="concert", version=0, text=CONCERT, *, kind="message", payload=None):
    return {"session_id": session, "request_id": str(uuid4()), "expected_state_version": version,
            "input": kind, "payload": {"text": text} if payload is None else payload}


def submit(client, request):
    response = client.post("/api/turns", json=request)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            assert job["status"] == "completed", job
            return job
        time.sleep(0.01)
    pytest.fail("Local search flow did not finish")


async def test_informational_concert_research_needs_no_trip_details_and_never_books(tmp_path):
    search, events = FakeSearch(), []
    with core_at(tmp_path / "concert.sqlite", search=search) as (service, travel):
        result = await service.run_turn(core_turn(), on_event=events.append)
        assert result.status == "research_ready", result.model_dump()
        assert len(search.calls) == 1 and "морген" in search.calls[0].casefold()
        assert result.web_search["results"] == [SOURCE]
        assert result.evidence == [SOURCE["url"]]
        assert "Не удалось составить надёжный ответ" in result.message
        assert SOURCE["snippet"] not in result.message and "Ниже источники" not in result.message
        assert result.options == [] and result.pending_action is None and result.demo_order is None
        state = await service.get_session("research-user", "concert")
        assert state.request.origin is None and state.request.destination is None
        assert state.request.departure_date is None and state.request.return_date is None
        assert state.last_web_search == result.web_search
        assert travel.calls == 0
        stages = [e["payload"]["stage"] for e in events if e["type"] == "stage_changed"]
        assert "web_search" in stages and "search" not in stages
        assert any(e["type"] == "tool_finished" and e["payload"].get("operation") == "web_search" for e in events)


async def test_uncited_abstention_keeps_rejected_dates_out_of_response_and_saved_dialogue(tmp_path):
    rejected_source = {**SOURCE, "snippet": "Архив: Рига, 15 ноября 2025. Берлин, 31 октября — год не указан."}

    class PastAndUnknownSearch(FakeSearch):
        async def search(self, query, now):
            result = await super().search(query, now)
            result["results"] = [deepcopy(rejected_source)]
            return result

    class AbstainingGateway(RecordingGateway):
        async def research(self, context):
            return {"message": "Подходящих будущих дат с подтверждённым годом найти не удалось. Уточнить период или поискать другое событие?", "source_ids": []}

    with core_at(tmp_path / "abstention.sqlite", search=PastAndUnknownSearch(), gateway=AbstainingGateway()) as (service, travel):
        result = await service.run_turn(core_turn())
        assert result.status == "research_ready"
        assert "Не удалось составить надёжный ответ" in result.message
        for rejected in ("Рига", "Берлин", "2025", "31 октября", "Ниже источники"):
            assert rejected not in result.message
        assert result.web_search["results"] == [rejected_source]
        assert result.evidence == [rejected_source["url"]]
        state = await service.get_session("research-user", "concert")
        assert state.dialogue[-1]["text"] == result.message
        assert state.request.destination is None and state.request.event_date is None
        assert travel.calls == 0


async def test_exact_repeat_is_free_and_explicit_retry_reuses_query_without_interpreter(tmp_path):
    search, gateway = FakeSearch(), RecordingGateway()
    with core_at(tmp_path / "repeat.sqlite", search=search, gateway=gateway) as (service, _):
        request = core_turn()
        first = await service.run_turn(request)
        repeated = await service.run_turn(request)
        assert repeated.model_dump() == first.model_dump()
        assert len(search.calls) == len(gateway.inputs) == 1
        retry = await service.run_turn(core_turn(first.state_version, "Повтори поиск в интернете"))
        assert retry.status == "research_ready"
        assert search.calls == [first.web_search["query"], first.web_search["query"]]
        assert len(gateway.inputs) == 1
        assert retry.state_version == first.state_version + 1


async def test_unconfigured_search_keeps_query_until_connection_becomes_available(tmp_path):
    search, gateway = FakeSearch(configured=False), RecordingGateway()
    with core_at(tmp_path / "missing-key.sqlite", search=search, gateway=gateway) as (service, travel):
        failed = await service.run_turn(core_turn())
        assert failed.status == "partial" and failed.error["code"] == "search_unconfigured"
        state = await service.get_session("research-user", "concert")
        assert state.last_web_search["query"] == search.calls[0]
        assert state.last_web_search["results"] == []
        search.configured = True
        success = await service.run_turn(core_turn(failed.state_version, "Повтори поиск в интернете"))
        assert success.status == "research_ready" and success.web_search["query"] == failed.web_search["query"]
        assert len(gateway.inputs) == 1 and travel.calls == 0


def test_web_sources_survive_reload_restart_and_repeated_http_request(tmp_path):
    database = tmp_path / "sources.sqlite"
    request, search = body(), FakeSearch()
    with web_at(database, search=search, travel=NoTravelCalls()) as client:
        completed = submit(client, request)
        original = client.get("/api/sessions/concert").json()
        assert original["result"]["web_search"]["results"] == [SOURCE]
        assert original["messages"][-1]["web_search"] == completed["result"]["web_search"]
        assert client.get("/api/sessions/concert").json() == original
    restarted_search = FakeSearch()
    with web_at(database, search=restarted_search, travel=NoTravelCalls()) as client:
        restored = client.get("/api/sessions/concert").json()
        assert restored == original
        assert restored["session"]["last_web_search"]["results"] == [SOURCE]
        repeated = submit(client, request)
        assert repeated["job_id"] == completed["job_id"]
        assert repeated["result"] == completed["result"]
        assert restarted_search.calls == []


def test_search_configuration_is_csrf_protected_memory_only_and_never_in_model_context(tmp_path, monkeypatch):
    monkeypatch.delenv("YANDEX_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_FOLDER_ID", raising=False)
    secret, folder = "test-secret-api-key-xyz", "private-folder-xyz"
    database, gateway = tmp_path / "config.sqlite", RecordingGateway()
    original_environment = dict(os.environ)
    search_calls = []

    async def local_search(self, query, now):
        assert self.configured
        search_calls.append(query)
        return await FakeSearch().search(query, now)

    monkeypatch.setattr(YandexSearchAdapter, "search", local_search)
    with web_at(database, search=FakeSearch(False), gateway=gateway, travel=NoTravelCalls()) as client:
        payload = {"api_key": secret, "folder_id": folder}
        token = client.headers.pop("X-Travel-Token")
        assert client.post("/api/search/config", json=payload).status_code == 403
        client.headers["X-Travel-Token"] = "wrong-token"
        assert client.post("/api/search/config", json=payload).status_code == 403
        assert client.get("/api/bootstrap").json()["web_search"]["configured"] is False
        client.headers["X-Travel-Token"] = token
        configured = client.post("/api/search/config", json=payload)
        assert configured.status_code == 200 and configured.json() == {"provider": "yandex", "configured": True}
        bootstrap = client.get("/api/bootstrap").json()
        assert bootstrap["web_search"] == {"provider": "yandex", "configured": True}
        assert search_calls == []  # Connecting stores credentials; it does not make a paid search.
        done = submit(client, body())
        assert done["result"]["status"] == "research_ready"
        exposed = json.dumps([bootstrap, configured.json(), done, client.get("/api/sessions/concert").json(), gateway.inputs])
        assert secret not in exposed and folder not in exposed
        assert os.environ == original_environment
    for path in (database, database.with_name(database.name + ".checkpoints")):
        with sqlite3.connect(path) as connection:
            dump = "\n".join(connection.iterdump())
        assert secret not in dump and folder not in dump
    # A fresh process/app must not recover a key from saved conversations.
    app = create_app(db_path=database, model_backend="rules", gateway=RecordingGateway(), adapter=NoTravelCalls())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/bootstrap").json()["web_search"]["configured"] is False


@pytest.mark.parametrize("source_stage", ["pending", "completed"])
def test_continue_replay_into_live_copies_request_and_dialogue_without_old_actions(tmp_path, source_stage):
    database = tmp_path / f"continue-{source_stage}.sqlite"
    with web_at(database) as client:
        first = submit(client, body("old-trip", text=TRIP))["result"]
        assert first["status"] == "options_ready"
        option = first["options"][0]
        selected = submit(client, body("old-trip", first["state_version"], kind="select_option", payload={"option_id": option["option_id"], "options_version": option["options_version"]}))["result"]
        pending = submit(client, body("old-trip", selected["state_version"], "Оформить"))["result"]
        assert pending["pending_action"]
        if source_stage == "completed":
            confirmation = {key: pending["pending_action"][key] for key in ("action_id", "plan_id", "plan_version", "payload_hash")}
            completed = submit(client, body("old-trip", pending["state_version"], kind="confirm_action", payload=confirmation))["result"]
            assert completed["demo_order"]
        original = client.get("/api/sessions/old-trip").json()
        assert original["session"]["current_plan"] and original["session"]["options"]
    travel, search = NoTravelCalls(), FakeSearch()
    with web_at(database, mode="live", search=search, travel=travel) as client:
        token = client.headers.pop("X-Travel-Token")
        assert client.post("/api/sessions/old-trip/continue").status_code == 403
        client.headers["X-Travel-Token"] = token
        response = client.post("/api/sessions/old-trip/continue")
        assert response.status_code == 200
        migrated_id = response.json()["session_id"]
        assert migrated_id != "old-trip" and response.json()["data_mode"] == "live"
        assert client.post("/api/sessions/old-trip/continue").json() == response.json()
        continued = client.get(f"/api/sessions/{migrated_id}").json()
        fresh = continued["session"]
        assert fresh["state_version"] == 0 and fresh["data_mode"] == "live"
        assert fresh["request"] == original["session"]["request"]
        assert fresh["dialogue"] == original["session"]["dialogue"]
        assert fresh["continued_messages"] and continued["messages"][0]["text"] == TRIP
        assert fresh["options"] == [] and fresh["plan_history"] == [] and fresh["pins"] == {}
        assert fresh["current_plan"] is None and fresh["pending_action"] is None and fresh["demo_order"] is None
        assert fresh["options_version"] == fresh["plan_revision"] == 0
        assert client.get("/api/sessions/old-trip").json() == original
        assert client.post(f"/api/sessions/{migrated_id}/continue").json()["session_id"] == migrated_id
        assert travel.calls == 0 and search.calls == []
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2


def test_unknown_continue_does_not_create_an_empty_trip(tmp_path):
    database = tmp_path / "missing-trip.sqlite"
    with web_at(database, mode="live", travel=NoTravelCalls()) as client:
        response = client.post("/api/sessions/unknown/continue")
        assert response.status_code == 404
        assert client.get("/api/sessions/unknown").json()["session"] is None


def test_yandex_config_accepts_adapter_limits_and_trims_folder(tmp_path):
    previous = FakeSearch()
    with web_at(tmp_path / "config-limits.sqlite", search=previous) as client:
        response = client.post("/api/search/config", json={"api_key": "k" * 4096, "folder_id": "  " + "f" * 50 + " \n"})
        assert response.status_code == 200, response.text
        current = client.app.state.service.web_search_adapter
        assert current is not previous and current.configured
        assert current._folder_id == "f" * 50
        assert len(current._api_key) == 4096
        assert previous.calls == []


@pytest.mark.parametrize("payload", [
    {"api_key": "secret-overlong-" + "x" * 4096, "folder_id": "valid-folder"},
    {"api_key": "secret-valid-key", "folder_id": "secret-folder-" + "x" * 50},
    {"api_key": "secret-valid-key", "folder_id": "secret invalid folder!"},
    {"api_key": {"secret-object": "secret-nested-key"}, "folder_id": "valid-folder"},
    {"api_key": "secret-valid-key", "folder_id": "valid-folder", "secret-extra-field": "secret-extra-value"},
    {"api_key": "Bearer secret-unsupported-token", "folder_id": "valid-folder"},
    {"api_key": "secret\r\nX-Header: secret-injection", "folder_id": "valid-folder"},
    {"api_key": "секретный-ключ", "folder_id": "valid-folder"},
    {"api_key": "          ", "folder_id": "valid-folder"},
])
def test_invalid_yandex_input_is_redacted_and_preserves_existing_connection(tmp_path, payload):
    previous = FakeSearch()
    with web_at(tmp_path / "invalid-config.sqlite", search=previous) as client:
        response = client.post("/api/search/config", json=payload)
        assert response.status_code == 422, response.text
        assert isinstance(response.json()["detail"], str)
        assert "secret" not in response.text and "секретный" not in response.text
        assert "input" not in response.json()
        assert client.app.state.service.web_search_adapter is previous
        assert client.get("/api/bootstrap").json()["web_search"]["configured"] is True
        # A rejected replacement must leave the previous search path usable.
        result = submit(client, body())["result"]
        assert result["status"] == "research_ready" and len(previous.calls) == 1


def test_malformed_yandex_json_is_redacted_without_changing_other_validation(tmp_path):
    previous = FakeSearch()
    with web_at(tmp_path / "malformed-config.sqlite", search=previous) as client:
        response = client.post("/api/search/config", content='{"api_key":"secret-malformed-key", "folder_id":', headers={"Content-Type": "application/json"})
        assert response.status_code == 422
        assert "secret-malformed-key" not in response.text and isinstance(response.json()["detail"], str)
        assert client.app.state.service.web_search_adapter is previous
        # Ordinary API validation still has the standard FastAPI detail shape.
        ordinary = client.post("/api/turns", json={})
        assert ordinary.status_code == 422
        assert isinstance(ordinary.json()["detail"], list)
        assert any(item["type"] == "missing" for item in ordinary.json()["detail"])

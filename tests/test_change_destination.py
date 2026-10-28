"""A chosen different concert asks for new travel dates without losing the party."""
from copy import deepcopy
import json
import sqlite3
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from travel_agent.domain.models import TurnInput
from travel_agent.domain.search_diagnostics import build_search_diagnostics, normalize_search_session
from travel_agent.service import AgentService
from travel_agent.web import create_app

NOW = "2026-10-27T21:30:00+03:00"
REQUEST = {"origin": "Москва", "destination": "Хайфа", "departure_date": "2026-10-28", "return_date": "2026-10-29",
           "adults": 2, "children_ages": [8], "budget_minor": 10_000_000, "budget_scope": "trip", "constraints": {"breakfast": True}}
OLD_QUESTION = "Повторить поиск с теми же условиями?"
OLD_MESSAGE = "Туту не вернул авиапредложений. По отелям: получено предложений отелей: 20. Проверка ограничена: подробности проверены только у части отелей.\n" + OLD_QUESTION
NEW_QUESTION = "Рассмотрим другой город?"


class Gateway:
    name = "scripted-destination"

    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.contexts = []

    async def interpret(self, text, request, now, timezone, skills=None):
        self.contexts.append(deepcopy(request))
        return deepcopy(next(self.decisions))

    async def suggest(self, context):
        return []


class Travel:
    def __init__(self):
        self.calls = []

    async def search(self, request, now):
        self.calls.append(deepcopy(request))
        return {"flights": [], "hotels": [], "coverage": [
            {"product": "avia", "query": deepcopy(request), "count": 0, "complete": True},
            {"product": "hotel", "query": deepcopy(request), "count": 0, "complete": True},
        ], "warnings": []}


class EmptyWebSearch:
    configured = True

    def __init__(self):
        self.calls = []

    async def search(self, query, now):
        self.calls.append(query)
        return {"provider": "yandex", "query": query, "results": [], "fetched_at": now, "error": None}


def turn(text, version=0):
    return TurnInput(request_id=str(uuid4()), user_id="change-city", session_id="trip",
                     expected_state_version=version, input="message", payload={"text": text}, now=NOW)


def assert_preserved_party_and_preferences(request):
    for key in ("origin", "adults", "children_ages", "budget_minor", "budget_scope", "constraints"):
        assert request[key] == REQUEST[key], (key, request)


async def test_changed_destination_clears_dates_and_waits_until_both_are_confirmed(tmp_path):
    gateway = Gateway([
        {"intent": "plan", "patch": REQUEST},
        {"intent": "replan", "patch": {"destination": "Вильнюс"}, "reset_dates": True},
        {"intent": "replan", "patch": {"departure_date": "2026-10-31"}},
        {"intent": "replan", "patch": {"return_date": "2026-11-02"}},
    ])
    travel = Travel()
    service = AgentService(db_path=tmp_path / "concert.sqlite", gateway=gateway, adapter=travel)
    try:
        first = await service.run_turn(turn("В Хайфу с 28 по 29 октября, двое взрослых и ребёнок 8 лет"))
        assert len(travel.calls) == 1
        travel.calls.clear()
        changed = await service.run_turn(turn("Меняю направление на Вильнюс; даты поездки выберем заново", first.state_version))
        assert changed.status == "needs_input" and changed.questions == ["Когда хотите вылететь?"]
        saved = (await service.get_session("change-city", "trip")).request.model_dump(mode="json")
        assert saved["destination"] == "Вильнюс"
        assert saved["departure_date"] is None and saved["return_date"] is None
        assert_preserved_party_and_preferences(saved)
        assert not travel.calls
        outbound = await service.run_turn(turn("Вылет 31 октября", changed.state_version))
        assert outbound.questions == ["Когда хотите вернуться?"] and not travel.calls
        await service.run_turn(turn("Возвращение 2 ноября", outbound.state_version))
        assert len(travel.calls) == 1
        assert travel.calls[0]["departure_date"] == "2026-10-31"
        assert travel.calls[0]["return_date"] == "2026-11-02"
        assert travel.calls[0]["destination"] == "Вильнюс"
        assert_preserved_party_and_preferences(travel.calls[0])
    finally:
        service.close()


@pytest.mark.parametrize("reset,dates", [(True, {"departure_date": "2026-11-01", "return_date": "2026-11-04"}), (False, {})])
async def test_explicit_new_dates_override_reset_but_ordinary_city_change_keeps_dates(tmp_path, reset, dates):
    gateway = Gateway([{"intent": "plan", "patch": REQUEST},
                       {"intent": "replan", "patch": {"destination": "Братислава", **dates}, "reset_dates": reset}])
    travel = Travel()
    service = AgentService(db_path=tmp_path / "fresh-dates.sqlite", gateway=gateway, adapter=travel)
    try:
        first = await service.run_turn(turn("Первый город"))
        await service.run_turn(turn("Меняем город и сохраняем явно согласованные даты", first.state_version))
        assert len(travel.calls) == 2
        query = travel.calls[-1]
        assert query["destination"] == "Братислава"
        assert query["departure_date"] == dates.get("departure_date", REQUEST["departure_date"])
        assert query["return_date"] == dates.get("return_date", REQUEST["return_date"])
        assert_preserved_party_and_preferences(query)
    finally:
        service.close()


async def test_web_research_reset_applies_fresh_patch_after_clearing_old_dates(tmp_path):
    gateway = Gateway([{"intent": "plan", "patch": REQUEST},
                       {"intent": "web_search", "web_query": "Моргенштерн концерт Вильнюс 2026", "reset_dates": True,
                        "patch": {"destination": "Вильнюс", "departure_date": "2026-10-31"}}])
    travel, web = Travel(), EmptyWebSearch()
    service = AgentService(db_path=tmp_path / "web-reset.sqlite", gateway=gateway, adapter=travel, web_search_adapter=web)
    try:
        first = await service.run_turn(turn("Первая поездка"))
        result = await service.run_turn(turn("Проверь концерты Моргенштерна в Вильнюсе, вылетаю 31 октября. Нужна только афиша.", first.state_version))
        assert result.status == "needs_input"
        assert len(travel.calls) == 1 and len(web.calls) == 1
        saved = (await service.get_session("change-city", "trip")).request.model_dump(mode="json")
        assert saved["departure_date"] == "2026-10-31" and saved["return_date"] is None
        assert saved["destination"] == "Вильнюс"
        assert_preserved_party_and_preferences(saved)
    finally:
        service.close()


def test_legacy_diagnostic_context_is_shortened_on_a_copy():
    diag = build_search_diagnostics({"flights": [], "hotels": [], "coverage": []}, REQUEST, NOW)
    state = {"request": deepcopy(REQUEST), "search_diagnostics": diag, "last_question": OLD_QUESTION,
             "dialogue": [{"role": "user", "text": "На концерт"}, {"role": "assistant", "text": OLD_MESSAGE}]}
    before = deepcopy(state)
    normalized = normalize_search_session(state)
    assert state == before
    assert normalized["request"] == REQUEST and normalized["search_diagnostics"] == diag
    assert normalized["last_question"] == NEW_QUESTION
    assert normalized["dialogue"][-1]["text"].endswith(NEW_QUESTION)
    assert "По отелям" not in normalized["dialogue"][-1]["text"]
    assert "Повторить" not in normalized["dialogue"][-1]["text"]
    assert normalized["dialogue"][:-1] == state["dialogue"][:-1]


def test_legacy_http_reply_is_compact_without_changing_receipts_and_next_context_matches(tmp_path):
    database = tmp_path / "legacy-results.sqlite"
    session_id = "legacy-empty-search"
    travel = Travel()

    def post(client, version, text):
        body = {"session_id": session_id, "request_id": str(uuid4()), "expected_state_version": version,
                "input": "message", "payload": {"text": text}}
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
        pytest.fail("Local test job did not finish")

    gateway = Gateway([{"intent": "plan", "patch": REQUEST}])
    with TestClient(create_app(db_path=database, model_backend="rules", gateway=gateway, adapter=travel),
                    base_url="http://127.0.0.1") as client:
        job_id, request_id, first = post(client, 0, "Первая поездка")
        cookies = dict(client.cookies)
    old = deepcopy(first)
    old.update(message=OLD_MESSAGE, questions=[OLD_QUESTION], suggestions_version=3,
               suggestions=[{"id": "old-retry", "label": "Повторить поиск", "text": "Повтори поиск с теми же условиями."}])
    old_json = json.dumps(old, ensure_ascii=False)
    with sqlite3.connect(database) as connection:
        state = json.loads(connection.execute("SELECT body FROM sessions WHERE session_id=?", (session_id,)).fetchone()[0])
        state["last_question"] = OLD_QUESTION
        state["dialogue"][-1]["text"] = OLD_MESSAGE
        state_json = json.dumps(state, ensure_ascii=False)
        connection.execute("UPDATE sessions SET body=? WHERE session_id=?", (state_json, session_id))
        connection.execute("UPDATE web_jobs SET result=? WHERE job_id=?", (old_json, job_id))
        connection.execute("UPDATE turns SET result=? WHERE session_id=? AND request_id=?", (old_json, session_id, request_id))

    gateway = Gateway([{"intent": "replan", "patch": {"destination": "Вильнюс"}, "reset_dates": True}])
    with TestClient(create_app(db_path=database, model_backend="rules", gateway=gateway, adapter=travel),
                    base_url="http://127.0.0.1") as client:
        for name, value in cookies.items():
            client.cookies.set(name, value, domain="127.0.0.1", path="/")
        view = client.get(f"/api/sessions/{session_id}").json()
        assert view["result"]["questions"] == [NEW_QUESTION]
        assert view["messages"][-1]["text"] == view["result"]["message"]
        assert view["session"]["last_question"] == NEW_QUESTION
        assert view["session"]["dialogue"][-1]["text"] == view["result"]["message"]
        assert "По отелям" not in view["result"]["message"]
        assert "Повторить" not in view["result"]["message"]
        assert all("повтор" not in item["text"].lower() for item in view["result"]["suggestions"])
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT body FROM sessions WHERE session_id=?", (session_id,)).fetchone()[0] == state_json
            assert connection.execute("SELECT result FROM web_jobs WHERE job_id=?", (job_id,)).fetchone()[0] == old_json
            assert connection.execute("SELECT result FROM turns WHERE session_id=? AND request_id=?", (session_id, request_id)).fetchone()[0] == old_json
        post(client, first["state_version"], "Выбираю другой концерт")
        context = gateway.contexts[0]["_dialogue"]
        assert context["last_question"] == NEW_QUESTION
        assert context["history"][-1]["text"] == view["result"]["message"]
        assert len(travel.calls) == 1
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT result FROM web_jobs WHERE job_id=?", (job_id,)).fetchone()[0] == old_json
            assert connection.execute("SELECT result FROM turns WHERE session_id=? AND request_id=?", (session_id, request_id)).fetchone()[0] == old_json

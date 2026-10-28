"""Explain a missing trip component from saved search facts, without another search."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from uuid import uuid4

import pytest

from travel_agent.adapters.normalize import search_key
from travel_agent.adapters.replay import ReplayAdapter
from travel_agent.domain.models import TripRequest, TurnInput
from travel_agent.interpretation import RuleBasedGateway
from travel_agent.service import AgentService

NOW = "2026-10-27T12:00:00+03:00"
USER, SESSION = "search-explanation-test", "haifa-trip"
FULL_QUERY = "Из Москвы в Хайфу с 28 по 31 октября 2026, один взрослый, бюджет 80 тысяч на всех, багаж 23 кг и завтрак."
WHY = "Почему не получилось подобрать поездку?"
NEW_DATES = "Меняем даты на 1–4 ноября 2026. Сначала уточним время вылета."
REQUEST = TripRequest(origin="Москва", destination="Хайфа", departure_date="2026-10-28", return_date="2026-10-31",
                      adults=1, budget_minor=8_000_000, budget_scope="trip",
                      constraints={"baggage_kg": 23, "breakfast": True}).model_dump(mode="json")
LEGACY_MESSAGE = "Подходящих вариантов среди проверенных предложений не нашлось."


class ScriptedRuleBasedGateway(RuleBasedGateway):
    async def interpret(self, text, current_request, now, timezone, skills=None):
        if text == FULL_QUERY:
            return {"intent": "plan", "patch": deepcopy(REQUEST)}
        if text == WHY:
            return {"intent": "question", "patch": {}, "question": text}
        if text == NEW_DATES:
            return {"intent": "replan", "patch": {"departure_date": "2026-11-01", "return_date": "2026-11-04"},
                    "question": "Уточните желаемое время вылета?"}
        return await super().interpret(text, current_request, now, timezone, skills=skills)


class FakeHaifaAdapter:
    def __init__(self, *, error=None):
        self.calls = []
        self.error = error

    async def search(self, request, now):
        self.calls.append(deepcopy(request))
        if self.error:
            return {"flights": [], "hotels": [], "coverage": [], "warnings": [], "error": deepcopy(self.error)}
        query = search_key(request)
        fixtures = ReplayAdapter().fixture["result"]["hotels"]
        hotels = [deepcopy(fixtures[index % 3]) for index in range(4)]
        for index, hotel in enumerate(hotels):
            hotel.update(title=f"Проверочный отель Хайфы {index % 3 + 1}", query=deepcopy(query), fetched_at=now, data_origin="synthetic")
            hotel["provider_refs"] = {"hotel_id": f"mock-hotel-{index % 3}", "reference_key": f"mock-rate-{index}"}
        return {
            "flights": [], "hotels": hotels, "error": None,
            "warnings": ["Подробные тарифы проверены только у первых трёх отелей выдачи."],
            "coverage": [
                {"provider": "tutu", "product": "avia", "query": deepcopy(query), "pages": [1], "count": 0,
                 "total": 0, "total_exact": True, "complete": True, "stop_reason": "end_of_results"},
                {"provider": "tutu", "product": "hotel", "query": deepcopy(query), "pages": [1, 2], "count": 20,
                 "total": None, "total_exact": False, "complete": False, "stop_reason": "detail_limit", "detailed_hotels": 3},
            ],
        }


def make_turn(text=FULL_QUERY, version=0):
    return TurnInput(request_id=str(uuid4()), user_id=USER, session_id=SESSION, expected_state_version=version,
                     input="message", payload={"text": text}, now=NOW, timezone="Europe/Moscow")


def service_at(path, adapter):
    return AgentService(db_path=path, data_mode="live", checkout_mode="demo", gateway=ScriptedRuleBasedGateway(), adapter=adapter)


def assert_missing_flights_explanation(result):
    text = result.message.lower()
    assert "москв" in text and "хайф" in text, result.message
    assert all(detail not in text for detail in ("по отелям", "авиавыдач", "проверка ограничена", "повторить поиск"))
    assert "28" in text and "31" in text and "2026" in text, result.message
    assert any(word in text for word in ("рейс", "перел", "авиа")), result.message
    assert any(word in text for word in ("не дал", "не вернул", "не наш", "не найден", "отсутств", "0 ави", "нет ави", "нет рей", "нет перел")), result.message
    assert not re.search(r"\b4\s+отел", text), "Four room rates must not be described as four distinct hotels"
    assert not result.options and result.current_plan is None and result.pending_action is None
    assert result.search_diagnostics["code"] == "missing_flights"
    assert result.search_diagnostics["query"] == search_key(REQUEST)


def erase_diagnostics_for_legacy(service, *, stale_coverage=False):
    body = service.repo.session(USER, SESSION)
    body.pop("search_diagnostics", None)
    body["dialogue"] = [{"role": "user", "text": FULL_QUERY}, {"role": "assistant", "text": LEGACY_MESSAGE}]
    if stale_coverage:
        for coverage in body["search_coverage"]:
            coverage["query"]["departure_date"] = "2026-10-15"
            coverage["query"]["return_date"] = "2026-10-18"
    service.repo.conn.execute("UPDATE sessions SET body=? WHERE user_id=? AND session_id=?",
                             (json.dumps(body, ensure_ascii=False), USER, SESSION))


async def test_empty_flights_are_explained_on_search_and_why_without_changing_requirements(tmp_path):
    adapter = FakeHaifaAdapter()
    service = service_at(tmp_path / "explanation.sqlite", adapter)
    try:
        first = await service.run_turn(make_turn())
        assert first.status == "partial"
        assert_missing_flights_explanation(first)
        assert first.search_diagnostics["counts"] == {"flights": 0, "hotel_rates": 4, "hotels": 3}
        assert first.search_diagnostics["products"]["hotel"]["raw_count"] == 20
        assert first.search_diagnostics["products"]["hotel"]["detailed_hotels"] == 3
        assert first.search_diagnostics["products"]["hotel"]["complete"] is False
        original = await service.get_session(USER, SESSION)
        why = await service.run_turn(make_turn(WHY, first.state_version))
        assert why.status == "partial"
        assert_missing_flights_explanation(why)
        assert why.search_diagnostics == first.search_diagnostics
        after = await service.get_session(USER, SESSION)
        assert after.request.model_dump(mode="json") == original.request.model_dump(mode="json") == REQUEST
        assert len(adapter.calls) == 1
        for suggestion in why.suggestions:
            text = suggestion.text.lower()
            assert "почему" not in text and "какое из моих условий мешает" not in text
            assert all(date in {"2026-10-28", "2026-10-31"} for date in re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", text))
    finally:
        service.close()


async def test_diagnostics_survive_restart_and_why_uses_saved_evidence(tmp_path):
    path = tmp_path / "restart.sqlite"
    first_adapter = FakeHaifaAdapter()
    service = service_at(path, first_adapter)
    try:
        first = await service.run_turn(make_turn())
        saved = await service.get_session(USER, SESSION)
        assert saved.search_diagnostics == first.search_diagnostics
    finally:
        service.close()
    fresh_adapter = FakeHaifaAdapter()
    reopened = service_at(path, fresh_adapter)
    try:
        restored = await reopened.get_session(USER, SESSION)
        assert restored.search_diagnostics == saved.search_diagnostics
        why = await reopened.run_turn(make_turn(WHY, first.state_version))
        assert_missing_flights_explanation(why)
        assert why.search_diagnostics == saved.search_diagnostics
        assert (await reopened.get_session(USER, SESSION)).request == saved.request
        assert not fresh_adapter.calls
    finally:
        reopened.close()


async def test_legacy_session_derives_reason_from_matching_coverage_without_search(tmp_path):
    adapter = FakeHaifaAdapter()
    service = service_at(tmp_path / "legacy.sqlite", adapter)
    try:
        first = await service.run_turn(make_turn())
        erase_diagnostics_for_legacy(service)
        assert (await service.get_session(USER, SESSION)).search_diagnostics is None
        why = await service.run_turn(make_turn(WHY, first.state_version))
        assert_missing_flights_explanation(why)
        assert why.message != LEGACY_MESSAGE
        assert why.search_diagnostics["products"]["avia"]["raw_count"] == 0
        assert why.search_diagnostics["products"]["hotel"]["raw_count"] == 20
        assert why.search_diagnostics["counts"]["hotel_rates"] is None
        assert (await service.get_session(USER, SESSION)).request.model_dump(mode="json") == REQUEST
        assert len(adapter.calls) == 1
    finally:
        service.close()


async def test_legacy_coverage_for_other_dates_cannot_explain_current_trip(tmp_path):
    adapter = FakeHaifaAdapter()
    service = service_at(tmp_path / "stale-legacy.sqlite", adapter)
    try:
        first = await service.run_turn(make_turn())
        erase_diagnostics_for_legacy(service, stale_coverage=True)
        why = await service.run_turn(make_turn(WHY, first.state_version))
        assert why.search_diagnostics is None
        assert not why.message.startswith(LEGACY_MESSAGE), "An old empty-search answer must not be reused for a different query"
        assert not why.options
        assert (await service.get_session(USER, SESSION)).request.model_dump(mode="json") == REQUEST
        assert len(adapter.calls) == 1
    finally:
        service.close()


@pytest.mark.parametrize("error", [
    {"code": "timeout", "message": "Туту не ответил за отведённое время.", "retryable": True, "provider": "tutu"},
    {"code": "unavailable", "message": "Не удалось подключиться к Туту.", "retryable": True, "provider": "tutu"},
])
async def test_provider_failure_is_not_described_as_proven_absence_of_offers(tmp_path, error):
    adapter = FakeHaifaAdapter(error=error)
    service = service_at(tmp_path / "failed-search.sqlite", adapter)
    try:
        first = await service.run_turn(make_turn())
        why = await service.run_turn(make_turn(WHY, first.state_version))
        for result in (first, why):
            assert result.status == "partial"
            assert result.error["code"] == error["code"]
            assert result.search_diagnostics["error"]["code"] == error["code"]
            assert result.search_diagnostics["code"] not in {"missing_flights", "missing_hotels", "missing_both"}
            assert not result.message.startswith(LEGACY_MESSAGE)
            assert "не найдено рейсов" not in result.message.lower()
            assert not result.options
        assert (await service.get_session(USER, SESSION)).request.model_dump(mode="json") == REQUEST
        assert len(adapter.calls) == 1
    finally:
        service.close()


async def test_request_change_before_clarification_clears_old_search_evidence(tmp_path):
    adapter = FakeHaifaAdapter()
    service = service_at(tmp_path / "changed-request.sqlite", adapter)
    try:
        first = await service.run_turn(make_turn())
        assert first.search_diagnostics
        changed = await service.run_turn(make_turn(NEW_DATES, first.state_version))
        assert changed.status == "needs_input"
        assert changed.search_diagnostics is None and changed.search_coverage == []
        snapshot = await service.get_session(USER, SESSION)
        assert snapshot.search_diagnostics is None and snapshot.search_coverage == []
        expected = REQUEST | {"departure_date": "2026-11-01", "return_date": "2026-11-04"}
        assert snapshot.request.model_dump(mode="json") == expected
        why = await service.run_turn(make_turn(WHY, changed.state_version))
        assert why.search_diagnostics is None and why.search_coverage == []
        assert "2026-10-28" not in why.message and "2026-10-31" not in why.message
        assert not why.message.startswith(LEGACY_MESSAGE)
        assert (await service.get_session(USER, SESSION)).request.model_dump(mode="json") == expected
        assert len(adapter.calls) == 1
    finally:
        service.close()

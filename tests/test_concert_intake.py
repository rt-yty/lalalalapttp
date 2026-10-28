"""Concert-trip intake is explicit, durable and distinct from reading an agenda."""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from travel_agent.concert_intake import ConcertIntakeState, filter_event_preferences, missing_preferences, update_intake
from travel_agent.domain.models import SessionSnapshot, TripRequest, TurnInput
from travel_agent.event_research import validate_events
from travel_agent.interpretation import Interpretation, RuleBasedGateway
from travel_agent.service import AgentService

NOW = datetime.fromisoformat("2026-10-28T12:00:00+03:00")
GENERIC = "Хочу поехать на концерт. Помоги сравнить ближайшие даты и города."
COMPLETE = {"origin": "Москва", "adults": 2, "budget_minor": 15000000, "budget_scope": "trip"}


def turn(text, version=0):
    return TurnInput(request_id=f"turn-{version}", user_id="u", session_id="intake", expected_state_version=version,
                     input="message", payload={"text": text}, now=NOW)


def recording_service(tmp_path, gateway=None):
    search = SimpleNamespace(configured=True, search=AsyncMock(return_value={"query": "concert", "results": [],
        "error": {"code": "search_unavailable", "message": "Unavailable", "retryable": True}}))
    travel = SimpleNamespace(search=AsyncMock(side_effect=AssertionError("Unresolved event cannot trigger inventory")))
    return AgentService(db_path=tmp_path/"intake.sqlite", gateway=gateway or RuleBasedGateway(),
                        adapter=travel, web_search_adapter=search), search, travel


async def test_generic_shortcut_and_partial_answers_are_durable_and_never_search_early(tmp_path):
    class Gateway(RuleBasedGateway):
        def __init__(self):
            self.contexts = []
        async def interpret(self, text, request, *args, **kwargs):
            self.contexts.append(deepcopy(request))
            if "Моргенштерн" in text:
                return {"intent": "web_search", "patch": {}, "web_query": "Моргенштерн концерты",
                        "concert_intake": {"event_name": "Моргенштерн", "period": "ноябрь 2026", "locations": "любые страны"}}
            if "Из Москвы" in text:
                return {"intent": "plan", "patch": COMPLETE}
            return {"intent": "web_search", "patch": {}, "web_query": "концерты ближайшие"}
    gateway = Gateway()
    service, search, travel = recording_service(tmp_path, gateway)
    try:
        first = await service.run_turn(turn(GENERIC))
        assert first.status == "needs_input" and not first.event_choices and not first.suggestions
        assert "на чей концерт" in first.message and "период" in first.message and "города" in first.message
        state = await service.get_session("u", "intake")
        assert state.last_question == first.message and state.concert_intake.event_name is None
        search.search.assert_not_called()
        second = await service.run_turn(turn("Моргенштерн. Ноябрь 2026, любые страны.", first.state_version))
        assert "из какого города" in second.message and "взрослых" in second.message and "бюджет" in second.message
        assert "на чей концерт" not in second.message and not second.suggestions
        search.search.assert_not_called()
        third = await service.run_turn(turn("Из Москвы, двое взрослых, бюджет 150 тысяч рублей на двоих.", second.state_version))
        assert third.status == "partial" and search.search.await_count == 1
        assert gateway.contexts[-1]["_concert_intake"]["event_name"] == "Моргенштерн"
        assert (await service.get_session("u", "intake")).request.origin == "Москва"
        travel.search.assert_not_called()
    finally:
        service.close()


@pytest.mark.parametrize("text", ["Хочу на концерт в ноябре", "Хочу поехать на концерт.", GENERIC])
def test_missing_artist_does_not_become_month_or_generic_title(text):
    state = update_intake(ConcertIntakeState(), text, {"intent": "web_search"}, COMPLETE)
    assert state.mode == "trip" and state.event_name is None and not state.open_to_suggestions
    assert missing_preferences(state, COMPLETE)[0][0] == "event"


@pytest.mark.parametrize("text", ["Я пока не знаю", "Еду один", "Нас двое", "Мне всё равно", "Не знаю", "Спасибо", "Без ограничений"])
def test_short_non_artist_answers_cannot_complete_known_other_preferences(text):
    previous = ConcertIntakeState(mode="trip", period="любые даты", locations="любые страны")
    state = update_intake(previous, text, {"intent": "replan"}, COMPLETE)
    assert state.event_name is None and missing_preferences(state, COMPLETE)[0][0] == "event"


def test_open_artist_choice_requires_explicit_permission():
    previous = ConcertIntakeState(mode="trip", period="любые даты", locations="любые страны")
    denied = update_intake(previous, "Помоги сравнить ближайшие даты и города", {"concert_intake": {"open_to_suggestions": True}}, COMPLETE)
    assert not denied.open_to_suggestions
    allowed = update_intake(previous, "Исполнитель не важен, подбери концерт сам", {}, COMPLETE)
    assert allowed.open_to_suggestions and missing_preferences(allowed, COMPLETE) == []


async def test_information_only_does_not_require_passengers_or_budget(tmp_path):
    service, search, travel = recording_service(tmp_path)
    try:
        result = await service.run_turn(turn("Где концерты Моргенштерна? Только афиша, поездку пока не планирую."))
        assert result.status == "partial" and search.search.await_count == 1
        assert (await service.get_session("u", "intake")).concert_intake.mode == "information"
        travel.search.assert_not_called()
    finally:
        service.close()


async def test_unrelated_message_preserves_intake_without_search(tmp_path):
    service, search, _ = recording_service(tmp_path)
    try:
        first = await service.run_turn(turn(GENERIC))
        before = (await service.get_session("u", "intake")).concert_intake
        second = await service.run_turn(turn("Напиши Python-код игры в шахматы", first.state_version))
        assert second.response_kind == "chat" and (await service.get_session("u", "intake")).concert_intake == before
        search.search.assert_not_called()
    finally:
        service.close()


def test_chat_contract_rejects_hidden_intake_mutation():
    with pytest.raises(ValidationError):
        Interpretation(intent="chat", reply_text="Привет", concert_intake={"mode": "trip"})


def test_child_age_is_required_after_other_conditions_are_known():
    state = update_intake(ConcertIntakeState(mode="trip", event_name="Aurora", period="ноябрь 2026", locations="любые страны"),
                          "Двое взрослых и ребёнок", {}, COMPLETE)
    assert missing_preferences(state, COMPLETE) == [("children", "сколько лет каждому ребёнку")]
    state = update_intake(state, "Ребёнку 7 лет", {"concert_intake": {"children_pending": False}}, {**COMPLETE, "children_ages": [7]})
    assert missing_preferences(state, {**COMPLETE, "children_ages": [7]}) == []


def test_explicit_budget_removal_and_new_artist_are_applied_to_real_state(tmp_path):
    service, _, _ = recording_service(tmp_path)
    state = SessionSnapshot(user_id="u", session_id="intake", data_mode="replay", checkout_mode="demo",
        request=TripRequest(**COMPLETE), concert_intake=ConcertIntakeState(mode="trip", event_name="Aurora", period="ноябрь 2026", locations="любые города"))
    try:
        result = service._collect_concert_preferences(state, "Бюджет без ограничений", {"intent": "replan", "patch": {}})
        assert result["patch"]["budget_minor"] is None and result["patch"]["budget_scope"] is None
        state.selected_event = {"artist": "Aurora", "city": "Прага", "date": "2026-11-03"}
        state.event_choices = [state.selected_event]
        result = service._collect_concert_preferences(state, "Хочу на концерт Nova Signal", {"intent": "web_search", "patch": {},
            "concert_intake": {"mode": "trip", "event_name": "Nova Signal"}})
        assert state.selected_event is None and not state.event_choices
        assert result["intent"] == "web_search" and "Nova Signal" in result["web_query"]
    finally:
        service.close()


def test_month_and_country_preferences_are_enforced_after_extraction():
    events = [{"date": "2026-10-31", "city": "Москва", "country": "Россия"},
              {"date": "2026-11-02", "city": "Вильнюс", "country": "Литва"},
              {"date": "2026-11-03", "city": "Казань", "country": "Россия"},
              {"date": "2026-12-02", "city": "Москва", "country": "Россия"}]
    assert filter_event_preferences(events, {"period": "Ноябрь 2026", "locations": "только Россия"}, NOW) == [events[2]]


def test_fresh_research_date_patch_is_validated_after_old_dates_are_reset(tmp_path):
    service, _, _ = recording_service(tmp_path)
    state = SessionSnapshot(user_id="u", session_id="intake", data_mode="replay", checkout_mode="demo",
        request=TripRequest(departure_date="2026-11-01", return_date="2026-11-03"))
    try:
        result = service._collect_concert_preferences(state, "Где концерты Aurora?", {
            "intent": "web_search", "reset_dates": True, "patch": {"departure_date": "2026-11-09"},
            "concert_intake": {"mode": "information", "event_name": "Aurora"}})
        assert result["reset_dates"] is True and result["patch"] == {"departure_date": "2026-11-09"}
    finally:
        service.close()


@pytest.mark.parametrize("artist,expected", [("Aurora Waves", "Aurora Waves"), ("Неизвестный", None), ("Исполнитель не указан", None)])
def test_generic_event_title_uses_grounded_artist_or_is_rejected(artist, expected):
    source = {"id": "s", "url": "https://events.example", "title": artist, "page_text": "3 ноября 2026 Москва — Palace"}
    raw = {"events": [{"title": "Концерт", "artist": artist, "city": "Москва", "country": "Россия", "date": "2026-11-03",
                        "start_at": None, "venue": "Palace", "source_ids": ["s"], "evidence": "3 ноября 2026 Москва — Palace"}]}
    actual = validate_events(raw, [source], NOW)
    assert (actual[0]["title"] if actual else None) == expected

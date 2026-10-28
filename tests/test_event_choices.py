"""Concert research must yield grounded, selectable future event alternatives."""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from travel_agent.domain.models import SessionSnapshot, TripRequest, TurnInput
from travel_agent.event_research import event_message, validate_events
from travel_agent.service import AgentService
from travel_agent.interpretation import RuleBasedGateway

NOW = datetime.fromisoformat("2026-10-28T12:00:00+03:00")
PAGE = "Aurora Waves tour 2026. 31 октября 2026 — Прага — Palace. 2 ноября 2026 — Вена — Hall. 5 ноября 2026 — Берлин — Stage. 28 октября 2026 — Хайфа — Arena."
SOURCE = {"id": "tour", "url": "https://events.example/tour", "title": "Aurora Waves tour 2026", "snippet": PAGE, "page_text": PAGE, "page_status": "read"}

def raw_event(city="Прага", day="2026-10-31", evidence="31 октября 2026 — Прага — Palace"):
    return {"title": "Aurora Waves", "artist": "Aurora Waves", "city": city, "country": None, "date": day,
            "start_at": None, "venue": None, "source_ids": ["tour"], "evidence": evidence}

RAW = [raw_event(), raw_event("Вена", "2026-11-02", "2 ноября 2026 — Вена — Hall"),
       raw_event("Берлин", "2026-11-05", "5 ноября 2026 — Берлин — Stage")]

def test_every_grounded_future_date_is_kept_and_deduplicated():
    events = validate_events({"events": [*RAW, RAW[0]]}, [SOURCE], NOW)
    assert len(events) == 3 and [event["city"] for event in events] == ["Прага", "Вена", "Берлин"]
    assert all(event["source_urls"] == [SOURCE["url"]] for event in events)
    assert "несколько" in event_message(events)

def test_single_result_does_not_ask_which_of_multiple_and_has_search_alternative():
    message = event_message(validate_events({"events": RAW[:1]}, [SOURCE], NOW))
    assert "одну" in message and "продолжить поиск" in message
    assert "Какой концерт" not in message

@pytest.mark.parametrize("changed", [
    {"date": "2027-10-31"}, {"source_ids": ["invented"]}, {"evidence": "Я придумал другое событие"},
    {"date": "2026-02-30"}, {"date": "2025-10-31"}, {"evidence": "31 октября 2025 — Прага — Palace"},
])
def test_unverified_or_past_event_is_rejected(changed):
    assert validate_events({"events": [{**RAW[0], **changed}]}, [SOURCE], NOW) == []

def test_today_is_not_the_default_trip_but_can_be_requested_explicitly():
    event = raw_event("Хайфа", "2026-10-28", "28 октября 2026 — Хайфа — Arena")
    assert validate_events({"events": [event]}, [SOURCE], NOW) == []
    assert len(validate_events({"events": [event]}, [SOURCE], NOW, [{"role": "user", "text": "Я в Хайфе, хочу сегодня на концерт"}])) == 1

def test_timezone_is_not_guessed_from_city():
    event = {**RAW[0], "start_at": "2026-10-31T20:00:00+01:00"}
    assert validate_events({"events": [event]}, [SOURCE], NOW)[0]["start_at"] is None
    source = {**SOURCE, "page_text": PAGE + " Начало 20:00 UTC+1."}
    assert validate_events({"events": [event]}, [source], NOW)[0]["start_at"] == event["start_at"]

def test_abbreviated_calendar_dates_use_explicit_page_year():
    source = {**SOURCE, "page_text": "Тур 2026. Сб 31 Окт Прага, Чехия Palace 20:00"}
    event = {**RAW[0], "evidence": "Сб 31 Окт Прага, Чехия Palace 20:00"}
    assert len(validate_events({"events": [event]}, [source], NOW)) == 1

def test_numeric_calendar_date_inherits_unambiguous_tour_header():
    source = {**SOURCE, "page_text": "WORLD TOUR 2026 PART 2\n31/10\n20:00\nПРАГА\nPalace"}
    event = {**RAW[0], "evidence": "31/10\n20:00\nПРАГА\nPalace"}
    assert len(validate_events({"events": [event]}, [source], NOW)) == 1

@pytest.mark.parametrize("body", [
    "WORLD TOUR 2027 31/10 ПРАГА Palace. Copyright 2026",
    "31/10 ПРАГА Palace. Copyright 2026",
    "WORLD TOUR 2026/2027 31/10 ПРАГА Palace",
    "WORLD TOUR 2026. Новый тур 2027 31/10 ПРАГА Palace",
])
def test_footer_or_mismatched_schedule_year_cannot_date_an_event(body):
    source = {"id": "tour", "url": SOURCE["url"], "title": "Concert page", "snippet": "", "page_text": body}
    event = {**RAW[0], "evidence": "31/10 ПРАГА Palace"}
    assert validate_events({"events": [event]}, [source], NOW) == []

@pytest.mark.parametrize("quote", ["31 октября 2027 — Прага Palace. Copyright 2026", "31/10/2027 — Прага Palace. Copyright 2026", "31/10 ПРАГА Palace. Copyright 2026"])
def test_including_footer_in_quote_cannot_manufacture_schedule_year(quote):
    source = {"id": "tour", "url": SOURCE["url"], "title": "Schedule", "snippet": "", "page_text": quote}
    assert validate_events({"events": [{**RAW[0], "evidence": quote}]}, [source], NOW) == []

class Gateway(RuleBasedGateway):
    async def research_plan(self, context):
        return {"queries": [], "source_ids": ["tour"]}
    async def research_events(self, context):
        return {"events": deepcopy(RAW)}

def make_turn(text="Где концерты Aurora Waves?", version=0, kind="message", payload=None, key="start"):
    return TurnInput(request_id=key, session_id="events", user_id="audit", expected_state_version=version,
                     input=kind, payload=payload or {"text": text}, now=NOW)

@pytest.mark.parametrize("text_selection", [False, True])
async def test_event_selection_persists_date_and_city_and_stale_choice_is_rejected(tmp_path, text_selection):
    search = SimpleNamespace(configured=True, search=AsyncMock(return_value={"query": "Aurora Waves концерты", "results": [SOURCE], "error": None}))
    reader = SimpleNamespace(read=AsyncMock(return_value={"status": "read", "text": PAGE}))
    travel = SimpleNamespace(search=AsyncMock(side_effect=AssertionError("Travel dates not chosen yet")))
    service = AgentService(db_path=tmp_path/"events.sqlite", gateway=Gateway(), adapter=travel, web_search_adapter=search, web_reader=reader)
    try:
        first = await service.run_turn(make_turn())
        assert first.status == "research_ready" and len(first.event_choices) == 3
        assert all(s.text.startswith("Выбираю концерт") for s in first.suggestions)
        chosen = first.event_choices[1]
        second = await service.run_turn(make_turn("Выбираю концерт 2.", first.state_version,
            kind="message" if text_selection else "select_event", key="choose",
            payload=None if text_selection else {"event_id": chosen["event_id"], "event_choices_version": first.event_choices_version}))
        assert second.status == "needs_input"
        state = await service.get_session("audit", "events")
        assert state.selected_event["event_id"] == chosen["event_id"]
        assert state.request.destination == "Вена" and str(state.request.event_date) == "2026-11-02"
        assert state.request.departure_date is None and state.request.return_date is None
        stale = await service.run_turn(make_turn(version=second.state_version, kind="select_event", key="stale",
            payload={"event_id": chosen["event_id"], "event_choices_version": 0}))
        assert stale.error["code"] == "stale_events"
        assert stale.selected_event["event_id"] == chosen["event_id"]
        travel.search.assert_not_called()
    finally:
        service.close()

@pytest.mark.parametrize("text", ["Другие?", "А есть ещё?", "Покажи другие концерты", "Предложи другие варианты"])
async def test_asking_for_other_events_searches_again_and_does_not_relabel_old_choices(tmp_path, text):
    search = SimpleNamespace(configured=True, search=AsyncMock(return_value={"query": "Aurora Waves концерты", "results": [SOURCE], "error": None}))
    reader = SimpleNamespace(read=AsyncMock(return_value={"status": "read", "text": PAGE}))
    service = AgentService(db_path=tmp_path/"other.sqlite", gateway=Gateway(), web_search_adapter=search, web_reader=reader)
    try:
        first = await service.run_turn(make_turn())
        before = search.search.await_count
        second = await service.run_turn(make_turn(text, first.state_version, key="other"))
        assert search.search.await_count > before
        assert "не подтвердил другие" in second.message
        assert second.event_choices == first.event_choices
        assert second.event_choices_version == first.event_choices_version
    finally:
        service.close()

def test_later_travel_question_does_not_extract_concerts_again():
    from travel_agent.event_research import event_query
    assert not event_query("Визовые требования Франция", [
        {"role": "user", "text": "Хочу на концерт"}, {"role": "user", "text": "Нужна ли виза?"}])

async def test_new_artist_failure_cannot_leave_previous_artist_selectable(tmp_path):
    search = SimpleNamespace(configured=True, search=AsyncMock(return_value={"query": "Aurora Waves концерты", "results": [SOURCE], "error": None}))
    reader = SimpleNamespace(read=AsyncMock(return_value={"status": "read", "text": PAGE}))
    service = AgentService(db_path=tmp_path/"new-artist.sqlite", gateway=Gateway(), web_search_adapter=search, web_reader=reader)
    try:
        first = await service.run_turn(make_turn())
        assert first.event_choices
        selected = await service.run_turn(make_turn(version=first.state_version, kind="select_event", key="select-before-new-artist",
            payload={"event_id": first.event_choices[0]["event_id"], "event_choices_version": first.event_choices_version}))
        assert selected.selected_event
        search.search.return_value = {"query": "Nova Signal концерты", "results": [], "error": {"code": "search_timeout", "message": "Время ожидания истекло", "retryable": True}}
        second = await service.run_turn(make_turn("Где концерты Nova Signal? Нужна только афиша.", selected.state_version, key="new-artist"))
        assert second.status == "partial" and not second.event_choices
        assert second.selected_event is None
        assert second.event_choices_version > first.event_choices_version
        stored = await service.get_session("audit", "events")
        assert not stored.event_choices and stored.request.event_date is None and stored.request.destination is None
    finally:
        service.close()


async def ignore_event(*args):
    pass


async def test_first_event_keeps_valid_explicit_trip_dates_and_changed_event_resets(tmp_path):
    travel = SimpleNamespace(search=AsyncMock(return_value={"flights": [], "hotels": [], "trains": [], "buses": []}))
    service = AgentService(db_path=tmp_path/"explicit-dates.sqlite", gateway=Gateway(), adapter=travel)
    events = validate_events({"events": RAW}, [SOURCE], NOW)
    state = SessionSnapshot(user_id="audit", session_id="events", data_mode="replay", checkout_mode="demo",
        event_choices=events, event_choices_version=1,
        request=TripRequest(origin="Москва", departure_date="2026-10-30", return_date="2026-11-06", adults=1, budget_minor=15000000, budget_scope="trip"))
    try:
        chosen = make_turn(kind="select_event", payload={"event_id": events[0]["event_id"], "event_choices_version": 1})
        await service._action(state, chosen, {"intent": "select_event"}, ignore_event)
        assert str(state.request.departure_date) == "2026-10-30" and str(state.request.return_date) == "2026-11-06"
        assert state.selected_event["city"] == "Прага"
        assert travel.search.await_count == 1
        changed = make_turn(kind="select_event", payload={"event_id": events[1]["event_id"], "event_choices_version": 1})
        response = await service._action(state, changed, {"intent": "select_event"}, ignore_event)
        assert response["status"] == "needs_input" and state.selected_event["city"] == "Вена"
        assert state.request.departure_date is state.request.return_date is None
        assert travel.search.await_count == 1
    finally:
        service.close()


@pytest.mark.parametrize("products", [["hotel"], ["avia", "hotel"], ["bus"], ["rail"]])
@pytest.mark.parametrize("departure,returning", [("2026-11-01", "2026-11-03"), ("2026-11-13", "2026-11-15")])
async def test_trip_outside_known_event_asks_before_any_external_search(tmp_path, products, departure, returning):
    travel = SimpleNamespace(search=AsyncMock(side_effect=AssertionError("No lookup for contradictory event dates")))
    service = AgentService(db_path=tmp_path/"outside.sqlite", gateway=Gateway(), adapter=travel)
    state = SessionSnapshot(user_id="audit", session_id="events", data_mode="replay", checkout_mode="demo",
        request=TripRequest(origin="Москва", destination="Сеул", departure_date=departure, return_date=returning,
                            event_date="2026-11-11", adults=1, products=products))
    try:
        response = await service._search(state, make_turn(), {"intent": "replan", "patch": {}}, ignore_event)
        assert response["status"] == "needs_input" and "11.11.2026" in response["message"]
        assert response["message"].count("?") == 1
        travel.search.assert_not_called()
    finally:
        service.close()


@pytest.mark.parametrize("reply,search_count", [("Да, останусь в городе после выезда", 1), ("Нет", 0)])
async def test_hotel_checkout_on_event_day_needs_explicit_travel_confirmation(tmp_path, reply, search_count):
    class HotelGateway(Gateway):
        async def interpret(self, *args, **kwargs):
            return {"intent": "plan", "patch": {"destination": "Сеул", "departure_date": "2026-11-09",
                "return_date": "2026-11-11", "event_date": "2026-11-11", "adults": 1, "products": ["hotel"]}}
    travel = SimpleNamespace(search=AsyncMock(return_value={"hotels": [], "flights": []}))
    service = AgentService(db_path=tmp_path/"checkout-day.sqlite", gateway=HotelGateway(), adapter=travel)
    try:
        first = await service.run_turn(make_turn("Нужен отель к концерту Aurora Waves, бюджет без ограничений"))
        assert "Останетесь ли" in first.message and first.status == "needs_input"
        travel.search.assert_not_called()
        second = await service.run_turn(make_turn(reply, first.state_version, key="checkout-answer"))
        assert travel.search.await_count == search_count
        stored = await service.get_session("audit", "events")
        if search_count:
            assert stored.request.event_checkout_ack
        else:
            assert stored.request.return_date is None and not stored.request.event_checkout_ack
            assert "выезд" in second.message.casefold()
    finally:
        service.close()


@pytest.mark.parametrize("checkout,ack,expected", [("2026-11-10", False, "violated"),
    ("2026-11-11", False, "unknown"), ("2026-11-11", True, "satisfied"), ("2026-11-12", False, "satisfied")])
def test_hotel_event_timing_is_a_hard_plan_check(checkout, ack, expected):
    from travel_agent.domain.planning import check_components, event_checkout_key
    request = TripRequest(destination="Сеул", departure_date="2026-11-09", return_date=checkout,
                          event_date="2026-11-11", adults=1, products=["hotel"]).model_dump(mode="json")
    if ack:
        request["event_checkout_ack"] = event_checkout_key(request)
    hotel = {"product_type": "hotel", "query": request, "conditions": {"details_verified": True},
             "availability": "reported_available", "price_minor": 10000, "currency": "RUB"}
    check = next(item for item in check_components([hotel], request) if item["constraint_id"] == "event_timing")
    assert check["status"] == expected and check["hard"] is True
    if ack:
        request["destination"] = "Токио"
        stale = next(item for item in check_components([hotel], request) if item["constraint_id"] == "event_timing")
        assert stale["status"] == "unknown"


def test_selected_event_country_answers_ambiguous_city_guard_without_reasking():
    from travel_agent.interpretation import apply_city_guard, event_destination
    event = {"city": "Александрия", "country": "Египет"}
    decision = apply_city_guard("Из Москвы", {"destination": "Александрия", "_selected_event": event},
                               {"intent": "replan", "patch": {"origin": "Москва"}})
    assert decision["patch"] == {"origin": "Москва", "destination": "Александрия, Египет"}
    assert decision["question"] is None
    for city in ("Вильнюс", "Братислава", "Кёльн", "Мюнхен", "Токио", "Сеул"):
        assert event_destination({"city": city, "country": "Страна"}) == city


async def test_event_selection_searches_ambiguous_city_with_confirmed_country(tmp_path):
    travel = SimpleNamespace(search=AsyncMock(return_value={"hotels": [], "flights": []}))
    service = AgentService(db_path=tmp_path/"country.sqlite", gateway=Gateway(), adapter=travel)
    event = {**validate_events({"events": RAW}, [SOURCE], NOW)[0], "city": "Александрия", "country": "Египет"}
    state = SessionSnapshot(user_id="audit", session_id="events", data_mode="replay", checkout_mode="demo",
        event_choices=[event], event_choices_version=1,
        request=TripRequest(origin="Москва", departure_date="2026-10-30", return_date="2026-11-03", adults=1, budget_minor=15000000, budget_scope="trip"))
    try:
        await service._action(state, make_turn(kind="select_event", payload={"event_id": event["event_id"], "event_choices_version": 1}),
                              {"intent": "select_event"}, ignore_event)
        assert travel.search.call_args.args[0]["destination"] == "Александрия, Египет"
        assert state.request.destination == "Александрия, Египет" and state.selected_event == event
    finally:
        service.close()

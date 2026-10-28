"""Independent regressions for informational detours in an assembled trip."""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_component_flow import ComponentAdapter, REQUEST, ScriptedGateway, assembled, turn
from travel_agent.event_research import event_advice_query, event_query
from travel_agent.research import answer_from_search
from travel_agent.service import AgentService
from travel_agent.web_research import enrich_search

NOW = datetime.fromisoformat("2026-10-28T12:00:00+03:00")
SOURCE = {"id": "facts", "url": "https://example.org/travel-info", "title": "Справка для путешественника",
          "snippet": "Участники фестиваля пишут пожелания на бумаге. На концерт разрешена повседневная одежда.",
          "page_status": "read", "page_text": "Участники фестиваля пишут пожелания на бумаге."}


@pytest.mark.parametrize("question", [
    "Какие традиции у японского фестиваля Танабата?",
    "Как принято одеваться на концерт в Японии?",
    "Что взять на концерт?",
    "Где находится концертный зал в Токио?",
    "Как добраться до фестиваля?",
    "Можно ли фотографировать на концерте?",
    "What to wear to a concert in Japan?",
])
async def test_event_advice_answers_the_question_instead_of_extracting_an_agenda(question):
    assert event_advice_query(question)
    model = SimpleNamespace(
        research=AsyncMock(return_value={"message": "Я нашёл справку по вашему вопросу.", "source_ids": ["facts"]}),
        research_events=AsyncMock(return_value={"events": []}),
    )
    answer = await answer_from_search(model, {"query": question, "results": [SOURCE]},
                                      [{"role": "user", "text": question}], NOW)
    assert answer["message"] == "Я нашёл справку по вашему вопросу."
    assert "event_choices" not in answer
    model.research.assert_awaited_once()
    model.research_events.assert_not_awaited()


@pytest.mark.parametrize("query", [
    "Где концерты Imagine Dragons?", "Афиша фестивалей Японии в ноябре 2026",
    "Расписание концертов Aurora Waves", "Хочу поехать на концерт",
])
def test_actual_event_agendas_remain_event_queries(query):
    assert event_query(query)


async def test_advice_does_not_trigger_an_automatic_extra_tour_search():
    question = "Какие традиции у фестиваля Танабата?"
    gateway = SimpleNamespace(research_plan=AsyncMock(return_value={"queries": [], "source_ids": ["facts"]}))
    search = SimpleNamespace(search=AsyncMock(side_effect=AssertionError("Unrequested tour lookup")))
    reader = SimpleNamespace(read=AsyncMock(return_value={"status": "read", "text": SOURCE["page_text"]}))
    import asyncio
    result = await enrich_search(gateway, search, reader, {"query": question, "results": [SOURCE]},
                                 [{"role": "user", "text": question}], NOW,
                                 deadline=asyncio.get_running_loop().time() + 120, emit=AsyncMock())
    assert result["research"]["queries"] == [question]
    search.search.assert_not_awaited()


@pytest.mark.parametrize("question,query,model_patch", [
    ("Можно ли сходить в музеи Казани с бюджетом до 5000 рублей?", "Казань музеи стоимость входа", {}),
    ("Бывают экскурсии в Казани до 5000 рублей?", "Казань экскурсии цены", {}),
    ("Нужна ли виза россиянам в Японию?", "Япония визовые правила россиянам", {"destination": "Япония"}),
    ("Как принято одеваться на концерт в Японии?", "Япония концерт этикет одежда", {}),
    ("Какие традиции у японского фестиваля Танабата?", "Традиции фестиваля Танабата", {}),
])
async def test_information_preserves_selected_trip_and_event(question, query, model_patch):
    gateway = ScriptedGateway({**deepcopy(REQUEST), "event_date": "2026-11-12"}, {
        question: {"intent": "web_search", "web_query": query, "patch": model_patch},
    })
    gateway.research = AsyncMock(return_value={"message": "Я нашёл справку по вашему вопросу.", "source_ids": ["facts"]})
    gateway.research_events = AsyncMock(return_value={"events": []})
    search = SimpleNamespace(configured=True, search=AsyncMock(return_value={
        "query": query, "results": [SOURCE], "error": None,
    }))
    service = AgentService(db_path=":memory:", data_mode="live", checkout_mode="disabled",
                           gateway=gateway, adapter=ComponentAdapter(), web_search_adapter=search)
    try:
        await assembled(service)
        before = await service.get_session("component-user", "trip")
        event = {"event_id": "chosen-event", "title": "Aurora Waves", "artist": "Aurora Waves",
                 "city": REQUEST["destination"], "country": "Россия", "date": "2026-11-12",
                 "start_at": None, "venue": "Hall", "source_ids": ["facts"], "source_urls": [SOURCE["url"]]}
        # Seed the previously selected, evidence-bound event in the isolated DB.
        before.selected_event = deepcopy(event)
        before.event_choices = [deepcopy(event)]
        before.event_choices_version = 1
        service.repo.conn.execute("UPDATE sessions SET body=? WHERE user_id=? AND session_id=?",
                                  (before.model_dump_json(), "component-user", "trip"))
        response = await service.run_turn(turn(before.state_version, question))
        after = await service.get_session("component-user", "trip")
        assert response.status == "research_ready", response.message
        assert response.message == "Я нашёл справку по вашему вопросу."
        for field in ("request", "current_plan", "selected_components", "selected_event", "event_choices",
                      "event_choices_version", "options_version", "transport_options", "hotel_options"):
            assert getattr(after, field) == getattr(before, field), field
        gateway.research_events.assert_not_awaited()
        assert len(service.adapter.calls) == 1
    finally:
        service.close()


def venue_fixture():
    raw = {"title": "Nova Signal", "artist": "Nova Signal", "city": "Прага", "country": "Чехия",
           "date": "2026-11-12", "start_at": None, "venue": "Old Monument", "source_ids": ["catalog"],
           "evidence": "12 November 2026 — Prague — Old Monument"}
    catalog = {"id": "catalog", "url": "https://shazam.com/artist/nova/events", "title": "Nova Signal tour",
               "snippet": raw["evidence"], "page_status": "read", "page_text": raw["evidence"]}
    official = {"id": "arena", "url": "https://newarena.example/events/nova", "title": "Nova Signal | New Arena",
                "snippet": "Nova Signal, 12 November 2026. New Arena, Prague.", "page_status": "read",
                "page_text": "Nova Signal plays New Arena in Prague on 12 November 2026."}
    return raw, catalog, official


def test_aggregator_venue_conflict_prefers_linked_arena_page_without_city_hardcoding():
    from travel_agent.event_research import validate_events
    raw, catalog, official = venue_fixture()
    event = validate_events({"events": [raw]}, [catalog, official], NOW)[0]
    assert event["venue"] == "New Arena"
    assert event["venue_status"] == "primary"
    assert event["venue_source_ids"] == ["arena"]
    assert event["date"] == raw["date"] and event["city"] == raw["city"]
    assert set(event["source_ids"]) == {"catalog", "arena"}


def test_single_aggregator_cannot_turn_a_landmark_into_a_confirmed_venue():
    from travel_agent.event_research import event_message, validate_events
    raw, catalog, _ = venue_fixture()
    events = validate_events({"events": [raw]}, [catalog], NOW)
    assert events[0]["venue"] is None and events[0]["venue_status"] == "unverified"
    assert "Old Monument" not in event_message(events)
    assert "площадка уточняется" in event_message(events)
    assert events[0]["evidence_hash"] and events[0]["evidence_source_id"] == "catalog"


@pytest.mark.parametrize("change", [
    {"url": "https://unrelated-news.example/events/nova"},
    {"title": "Another Band | New Arena", "snippet": "Another Band, 12 November 2026, New Arena, Prague.",
     "page_text": "Another Band plays New Arena on 12 November 2026."},
    {"snippet": "Nova Signal, 12 November 2027. New Arena, Prague.",
     "page_text": "Nova Signal plays New Arena on 12 November 2027."},
])
def test_unknown_domain_wrong_artist_or_wrong_year_does_not_override_venue(change):
    from travel_agent.event_research import validate_events
    raw, catalog, official = venue_fixture()
    event = validate_events({"events": [raw]}, [catalog, {**official, **change}], NOW)[0]
    assert event["venue"] is None


def test_conflicting_primary_venue_pages_remain_explicitly_unknown():
    from travel_agent.event_research import validate_events
    raw, catalog, official = venue_fixture()
    other = {"id": "other", "url": "https://otherhall.example/nova", "title": "Nova Signal | Other Hall",
             "snippet": "Nova Signal, 12 November 2026. Other Hall, Prague."}
    event = validate_events({"events": [raw]}, [catalog, official, other], NOW)[0]
    assert event["venue"] is None and event["venue_status"] == "conflicting"


def test_two_pages_from_the_same_event_feed_are_not_independent_corroboration():
    from travel_agent.event_research import validate_events
    raw, catalog, _ = venue_fixture()
    apple = {**catalog, "id": "apple", "url": "https://music.apple.com/concert/nova"}
    event = validate_events({"events": [raw]}, [catalog, apple], NOW)[0]
    assert event["venue"] is None


def test_preserved_event_trace_has_no_more_than_25_quoted_words_per_source():
    from travel_agent.event_research import validate_events
    raw, catalog, _ = venue_fixture()
    events = [{**raw, "city": f"City {i}", "venue": None,
               "evidence": f"12 November 2026 — City {i} — Nova Signal performance"} for i in range(12)]
    catalog = {**catalog, "snippet": "", "page_text": ". ".join(event["evidence"] for event in events)}
    saved = validate_events({"events": events}, [catalog], NOW)
    assert len(saved) == 12
    assert sum(len(event["evidence_excerpt"].split()) for event in saved) <= 25
    assert all(len(event["evidence_hash"]) == 64 for event in saved)

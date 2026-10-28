"""Negative findings from the real Luna audit, with deterministic reproduction."""
from copy import deepcopy

import pytest

from travel_agent.domain.models import SessionSnapshot
from travel_agent.graph.suggestions import _answers_question, _calendar_reply_valid, _question_kind, build_suggestions, suggest_next_messages


def snapshot(**request):
    return SessionSnapshot(user_id="synthetic", session_id="audit", data_mode="replay", checkout_mode="disabled",
                           request={"products": ["avia", "hotel"], "destination": "Ереван", **request})


@pytest.mark.parametrize("kind,text,trip_request,valid", [
    ("departure_date", "Вылетаю 26 ноября 2026 года.", {"event_date": "2026-11-25"}, False),
    ("departure_date", "Вылетаю 25 ноября 2026 года.", {"event_date": "2026-11-25"}, True),
    ("departure_date", "Вылетаю 24 ноября 2026 года.", {"event_date": "2026-11-25"}, True),
    ("departure_date", "Вылетаю 32 ноября 2026 года.", {}, False),
    ("departure_date", "Вылетаю 27 октября 2026 года.", {}, False),
    ("departure_date", "Вылетаю на следующий день после концерта.", {"event_date": "2026-11-25"}, False),
    ("departure_date", "Вылетаю накануне концерта.", {"event_date": "2026-11-25"}, True),
    ("return_date", "Вернусь 24 ноября 2026 года.", {"departure_date": "2026-11-24"}, False),
    ("return_date", "Вернусь 23 ноября 2026 года.", {"departure_date": "2026-11-24"}, False),
    ("return_date", "Вернусь 25 ноября 2026 года.", {"departure_date": "2026-11-24"}, True),
    ("return_date", "Вернусь 25 ноября 2026 года.", {"event_date": "2026-11-26"}, False),
    ("return_date", "Вернусь 2 января.", {"departure_date": "2026-12-31", "event_date": "2027-01-01"}, False),
    ("return_date", "Вернусь 2 января 2027 года.", {"departure_date": "2026-12-31", "event_date": "2027-01-01"}, True),
    ("dates", "С 24 по 30 ноября 2026 года.", {"event_date": "2026-11-25"}, True),
    ("dates", "С 26 по 30 ноября 2026 года.", {"event_date": "2026-11-25"}, False),
    ("dates", "С 30 по 24 ноября 2026 года.", {}, False),
])
def test_calendar_drafts_respect_known_bounds_without_guessing_year(kind, text, trip_request, valid):
    assert _calendar_reply_valid(text.casefold(), kind, snapshot(**trip_request), "2026-10-28T12:00:00+03:00") is valid


@pytest.mark.parametrize("marker", ["decision", "response"])
async def test_chat_suppresses_old_travel_question_without_model_or_state_mutation(marker):
    saved = snapshot()
    saved.last_question = "Из какого города поедете?"
    state = {"session": saved.model_dump(mode="json"), "result": {"status": "needs_input", "message": "Дважды два — четыре."}}
    if marker == "decision":
        state["decision"] = {"intent": "chat", "reply_text": "Дважды два — четыре."}
    else:
        state["result"]["response_kind"] = "chat"
    before = deepcopy(state)
    class Gateway:
        async def suggest(self, context):
            pytest.fail("Chat must not call the suggestion model")
    assert (await suggest_next_messages(state, gateway=Gateway()))["result"]["suggestions"] == []
    assert state == before
    assert build_suggestions(saved, {**state["result"], "response_kind": "chat"}) == []


async def test_empty_event_search_does_not_invent_new_artist_genre_or_city():
    saved = snapshot()
    saved.last_web_search = {"query": "концерты Синтетической группы", "results": []}
    state = {"session": saved.model_dump(mode="json"), "result": {"status": "needs_input", "message": "Ничего не нашлось.", "questions": ["Уточните название исполнителя или запрос."]}}
    class Gateway:
        async def suggest(self, context):
            pytest.fail("An empty event search should not guess user preferences")
    assert (await suggest_next_messages(state, gateway=Gateway()))["result"]["suggestions"] == []
    assert build_suggestions(saved, state["result"]) == []
    # An older empty search must not suppress a later concrete travel question.
    later = {"status": "needs_input", "questions": ["Из какого города поедете?"]}
    assert build_suggestions(saved, later)


async def test_filtered_impossible_date_never_returns_as_fallback():
    saved = snapshot(event_date="2026-11-25")
    state = {"session": saved.model_dump(mode="json"), "result": {"status": "needs_input", "message": "Когда вылетать?", "questions": ["Когда хотите вылететь?"]},
             "turn": {"input": "message", "payload": {"text": "На концерт."}, "now": "2026-10-28T12:00:00+03:00"}}
    class Gateway:
        async def suggest(self, context):
            return [{"id": "late", "label": "26 ноября", "text": "Вылетаю 26 ноября 2026 года."},
                    {"id": "same-day", "label": "25 ноября", "text": "Вылетаю 25 ноября 2026 года."}]
    result = await suggest_next_messages(state, gateway=Gateway())
    assert [item["text"] for item in result["result"]["suggestions"]] == ["Вылетаю 25 ноября 2026 года."]


def test_hotel_checkout_is_return_not_departure():
    assert _question_kind("На какую дату планируете выезд из отеля?") == "return_date"
    assert _answers_question("Планируем выехать из отеля 25 ноября 2026 года.", "return_date")
    assert not _answers_question("Вылетаю 25 ноября 2026 года.", "return_date")


def budget_selection_state():
    saved = snapshot(origin="Москва", departure_date="2026-11-24", return_date="2026-12-01", adults=1,
                     budget_minor=8_000_000, budget_scope="trip")
    query = saved.request.model_dump(mode="json")
    def component(kind, index, price):
        return {"component_id": f"synthetic-{kind}-{index}", "options_version": 1, "product_type": kind,
                "title": f"Тестовый {'рейс' if kind == 'avia' else 'отель'} {index}", "price_minor": price, "currency": "RUB",
                "query": deepcopy(query), "conditions": {"details_verified": True}, "eligibility": "satisfied", "availability": "reported_available",
                "departure_at": "2026-11-24T08:00:00+03:00", "arrival_at": "2026-11-24T12:00:00+04:00",
                "return_departure_at": "2026-12-01T15:00:00+04:00", "return_arrival_at": "2026-12-01T17:00:00+03:00"}
    saved.transport_options = [component("avia", 1, 3_521_800)]
    saved.selected_components = deepcopy(saved.transport_options)
    saved.hotel_options = [component("hotel", i, price) for i, price in enumerate((3_270_200, 3_822_500, 5_469_100), 1)]
    return {"session": saved.model_dump(mode="json"), "result": {"status": "plan_ready", "message": "Перелёт выбран; выберите отель."},
            "turn": {"input": "message", "payload": {"text": "Выбираю перелёт 1."}, "now": "2026-10-28T12:00:00+03:00"}}


async def test_known_over_budget_completion_is_not_suggested_and_numbers_are_stable():
    state = budget_selection_state()
    before = deepcopy(state)
    class Gateway:
        async def suggest(self, context):
            assert [card["selectable_with_current"] for card in context["hotel_options"]] == [True, True, False]
            return [{"id": str(i), "label": f"Отель {i}", "text": f"Выбираю отель {i}: Тестовый отель {i}."} for i in (3, 2, 1)]
    result = await suggest_next_messages(state, gateway=Gateway())
    assert [item["text"] for item in result["result"]["suggestions"]] == ["Выбираю отель 2: Тестовый отель 2.", "Выбираю отель 1: Тестовый отель 1."]
    assert state == before
    # Fallback also skips incompatible first cards without renumbering them.
    state["session"]["hotel_options"][0]["price_minor"] = 5_469_100
    fallback = build_suggestions(SessionSnapshot.model_validate(state["session"]), state["result"])
    assert fallback[0]["text"] == "Выбираю отель 2: Тестовый отель 2."


@pytest.mark.parametrize("change", ["party", "dates", "event"])
async def test_known_incompatible_completion_is_not_offered(change):
    state = budget_selection_state()
    if change == "party":
        state["session"]["hotel_options"][0]["query"]["adults"] = 2
    elif change == "dates":
        state["session"]["hotel_options"][0]["query"]["departure_date"] = "2026-11-25"
    else:
        state["session"]["request"]["event_date"] = "2026-11-23"
    class Gateway:
        async def suggest(self, context):
            assert context["hotel_options"][0]["selectable_with_current"] is False
            return [{"id": "hotel1", "label": "Отель 1", "text": "Выбираю отель 1: Тестовый отель 1."}]
    result = await suggest_next_messages(state, gateway=Gateway())
    assert "Выбираю отель 1: Тестовый отель 1." not in [s["text"] for s in result["result"]["suggestions"]]


async def test_one_way_search_is_not_offered_as_supported_action():
    state = budget_selection_state()
    class Gateway:
        async def suggest(self, context):
            return [{"id": "one-way", "label": "В одну сторону", "text": "Покажи перелёт в один конец без обратного билета."}]
    result = await suggest_next_messages(state, gateway=Gateway())
    assert not any("в один конец" in item["text"] for item in result["result"]["suggestions"])


async def test_foreign_currency_clarification_has_no_invented_ruble_budget():
    saved = snapshot()
    saved.dialogue = [{"role": "user", "text": "Мой бюджет $500 на всех."}]
    state = {"session": saved.model_dump(mode="json"),
             "result": {"status": "needs_input", "message": "Какой бюджет в рублях на всю поездку?", "questions": ["Какой бюджет в рублях на всю поездку?"]},
             "turn": {"input": "message", "payload": {"text": "Мой бюджет $500 на всех."}}}
    class Gateway:
        async def suggest(self, context):
            pytest.fail("Do not invent a conversion or replacement budget")
    assert (await suggest_next_messages(state, gateway=Gateway()))["result"]["suggestions"] == []
    assert build_suggestions(saved, state["result"]) == []


def test_month_clarification_is_not_departure_date_or_origin():
    assert _question_kind("Уточните, пожалуйста, месяц вылета: первого числа какого месяца?") == "month"
    assert _question_kind("В каком месяце вылетаете?") == "month"
    assert _answers_question("В ноябре 2026 года.", "month")
    assert _answers_question("Первого мая 2027 года.", "month")
    assert not _answers_question("Дата выезда гибкая.", "month")
    assert not _answers_question("Первого числа.", "month")


async def test_month_question_filters_nonanswers_without_changing_state():
    saved = snapshot()
    question = "Уточните, пожалуйста, месяц вылета: первого числа какого месяца?"
    saved.last_question = question
    state = {"session": saved.model_dump(mode="json"), "result": {"status": "needs_input", "message": question, "questions": [question]},
             "turn": {"input": "message", "payload": {"text": "Первого."}, "now": "2026-10-28T12:00:00+03:00"}}
    before = deepcopy(state)
    class Gateway:
        async def suggest(self, context):
            assert context["question_kind"] == "month"
            texts = ["Дата выезда гибкая.", "Первого числа.", "На семь ночей.", "В ноябре 2026 года."]
            return [{"id": str(i), "label": text, "text": text} for i, text in enumerate(texts)]
    result = await suggest_next_messages(state, gateway=Gateway())
    assert [item["text"] for item in result["result"]["suggestions"]] == ["В ноябре 2026 года."]
    assert state == before


async def test_month_question_has_no_flexible_date_fallback():
    saved = snapshot()
    question = "Уточните, пожалуйста, месяц вылета: первого числа какого месяца?"
    result = {"status": "needs_input", "message": question, "questions": [question]}
    before = saved.model_dump(mode="json")
    assert build_suggestions(saved, result, "Первого.") == []
    state = {"session": before, "result": result}
    assert (await suggest_next_messages(state, gateway=None))["result"]["suggestions"] == []
    assert saved.model_dump(mode="json") == before

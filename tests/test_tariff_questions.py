"""Questions select relevant saved facts; comparisons include alternatives."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from travel_agent.service import AgentService
from test_component_flow import ComponentAdapter, REQUEST, ScriptedGateway, turn, select


def card(kind, label, **conditions):
    return {"product_type": kind, "title": label, "price_minor": 123400,
            "provider_refs": {"reference_key": label}, "conditions": conditions,
            "departure_at": "2026-11-10T08:00:00+03:00", "arrival_at": "2026-11-10T12:00:00+03:00",
            "return_departure_at": "2026-11-17T12:00:00+03:00", "return_arrival_at": "2026-11-17T16:00:00+03:00"}


def state(*, selection=("Flight A", "Hotel A")):
    transports = [card("avia", "Flight A", baggage_kg=23, refundable=True),
                  card("avia", "Flight B", baggage_kg=0, refundable=False),
                  card("rail", "Train A", seat_category="COMPARTMENT", refundable=True),
                  card("rail", "Train B", seat_category="RESERVED_SEAT", refundable=False)]
    hotels = [card("hotel", "Hotel A", breakfast=True, free_cancellation=True, refundable=True),
              card("hotel", "Hotel B", breakfast=False, free_cancellation=False, refundable=False)]
    return SimpleNamespace(selected_components=[deepcopy(c) for c in transports + hotels if c["title"] in selection],
                           current_plan=None, transport_options=transports, hotel_options=hotels)


@pytest.mark.parametrize("question,included,excluded", [
    ("Сравни отели", ["Hotel A", "Hotel B"], ["Flight A", "Train A"]),
    ("Сравни перелёты", ["Flight A", "Flight B"], ["Hotel A", "Train A"]),
    ("Сравни поезда", ["Train A", "Train B"], ["Flight A", "Hotel A"]),
    ("Сравни транспорт", ["Flight A", "Flight B", "Train A", "Train B"], ["Hotel A", "Hotel B"]),
])
def test_comparisons_use_relevant_alternatives_even_after_full_selection(question, included, excluded):
    snapshot = state()
    before = deepcopy(snapshot)
    answer = AgentService._answer_question(snapshot, question)
    assert all(label in answer for label in included)
    assert all(label not in answer for label in excluded)
    assert "1 234 ₽" in answer
    assert snapshot == before


def test_hotel_comparison_after_only_flight_selected_uses_hotels():
    answer = AgentService._answer_question(state(selection=("Flight A",)), "Сравнить отели")
    assert "Hotel A" in answer and "Hotel B" in answer
    assert "Flight A" not in answer and "Багаж" not in answer

@pytest.mark.parametrize("question", [
    "Что уже выбрано и какие расходы пока не входят в сумму?",
    "Что я выбрал?", "Какой состав поездки?", "Что входит в стоимость поездки?",
    "Какая итоговая сумма?", "Сколько всего получилось?", "Какие расходы пока не учтены?",
])
def test_trip_summary_lists_only_selection_and_distinguishes_unpriced_extras(question):
    snapshot = state()
    original = deepcopy(snapshot)
    answer = AgentService._answer_question(snapshot, question)
    assert "Flight A" in answer and "Hotel A" in answer and "2 468 ₽" in answer
    assert "Hotel B" not in answer and "Flight B" not in answer
    assert "Трансферы" in answer and "ещё не оценил" in answer
    assert snapshot == original

def test_trip_summary_does_not_claim_candidates_are_selected():
    answer = AgentService._answer_question(state(selection=()), "Что уже выбрано?")
    assert "пока не сохранил" in answer
    assert "Hotel A" not in answer and "₽" not in answer


def test_baggage_question_uses_selected_flight_without_unselected_fares():
    answer = AgentService._answer_question(state(), "Багаж включён?")
    assert "Flight A" in answer and "23 кг" in answer
    assert all(label not in answer for label in ("Flight B", "Hotel A", "Train A"))
    assert "₽" not in answer and "Завтрак" not in answer


def test_breakfast_question_uses_selected_hotel():
    answer = AgentService._answer_question(state(), "Завтрак включён?")
    assert "Hotel A" in answer and "Завтрак включён: да" in answer
    assert "Hotel B" not in answer and "Flight A" not in answer


def test_breakfast_question_after_flight_selection_uses_hotel_cards():
    answer = AgentService._answer_question(state(selection=("Flight A",)), "Завтрак включён?")
    assert "Hotel A" in answer and "Hotel B" in answer
    assert "Завтрак включён: да" in answer and "Завтрак включён: нет" in answer
    assert "Flight A" not in answer and "Багаж" not in answer


@pytest.mark.parametrize("question", ["Есть ли бассейн в отеле?", "Какое питание в самолёте?", "Какой багаж в поезде?"])
def test_unknown_fact_is_not_replaced_with_irrelevant_tariff_details(question):
    answer = AgentService._answer_question(state(), question)
    assert "нет подтверждённых сведений" in answer
    assert "₽" not in answer and "23 кг" not in answer and "Завтрак" not in answer


def test_no_hotel_candidates_does_not_turn_hotel_comparison_into_flight_answer():
    snapshot = state(selection=("Flight A",))
    snapshot.hotel_options = []
    answer = AgentService._answer_question(snapshot, "Сравни отели")
    assert "вариантов отелей пока нет" in answer and "Flight A" not in answer


def test_comparison_is_bounded_to_eight_short_lines_and_deduplicates_selection():
    snapshot = state(selection=())
    snapshot.hotel_options = [card("hotel", f"Hotel {i}\nLong title", breakfast=True) for i in range(12)]
    snapshot.selected_components = [deepcopy(snapshot.hotel_options[0])]
    answer = AgentService._answer_question(snapshot, "Сравни отели")
    assert len(answer.splitlines()) == 8
    assert answer.count("Hotel 0 ") == 1 and "Hotel 8 " not in answer
    assert all(len(line) <= 400 for line in answer.splitlines())


async def test_public_hotel_comparison_after_flight_selection_preserves_state_and_does_not_search(tmp_path):
    adapter = ComponentAdapter()
    gateway = ScriptedGateway(REQUEST, {"compare-hotels": {"intent": "question", "patch": {}, "question_text": "Сравни отели"}})
    service = AgentService(db_path=tmp_path / "questions.sqlite", data_mode="live", checkout_mode="disabled",
                           gateway=gateway, adapter=adapter)
    try:
        found = await service.run_turn(turn())
        chosen = await select(service, found, found.transport_options[0])
        before = await service.get_session("component-user", "trip")
        searches = len(adapter.calls)
        result = await service.run_turn(turn(chosen.state_version, "compare-hotels"))
        assert "Hotel h1" in result.message and "Hotel h2" in result.message
        assert "Synthetic Airlines" not in result.message
        assert len(adapter.calls) == searches
        after = await service.get_session("component-user", "trip")
        assert after.selected_components == before.selected_components
        assert after.current_plan == before.current_plan
        assert after.request == before.request
        assert after.hotel_options == before.hotel_options
    finally:
        service.close()

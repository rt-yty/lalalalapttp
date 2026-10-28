"""Product choices and event context stay distinct from trip requirements."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from travel_agent.domain.clarifications import next_trip_question, normalize_clarification
from travel_agent.interpretation import Interpretation, RequestPatch, RuleBasedGateway


NOW = "2026-10-28T12:00:00+03:00"
TRIP = {
    "origin": "Москва", "destination": "Казань", "departure_date": "2026-11-20",
    "return_date": "2026-11-24", "adults": 1, "products": ["avia", "hotel"],
    "constraints": {"baggage_kg": 23, "breakfast": True},
}


async def interpret(text, request=None):
    return await RuleBasedGateway().interpret(text, deepcopy(request or {}), NOW, "Europe/Moscow")


@pytest.mark.parametrize("text, products", [
    ("Найди только отель в Казани", ["hotel"]),
    ("Нужен только поезд из Москвы в Казань", ["rail"]),
    ("Ехать обязательно поездом", ["rail", "hotel"]),
    ("Самолёт или поезд, без отеля", ["avia", "rail"]),
    ("Отель не нужен", ["avia"]),
])
async def test_explicit_products_do_not_turn_rail_into_a_flight(text, products):
    decision = await interpret(text, TRIP)
    assert decision["patch"]["products"] == products
    assert decision["intent"] == "replan"
    assert not decision["question"]


async def test_new_hotel_search_only_asks_for_stay_details():
    decision = await interpret("Найди отель в Казани с 20 ноября по 24 ноября 2026, один взрослый")
    request = decision["patch"]
    assert request["products"] == ["hotel"]
    assert "origin" not in request
    assert next_trip_question(request) is None
    assert "заезд" in next_trip_question({"products": ["hotel"], "destination": "Казань"})
    assert "выезд" in next_trip_question({"products": ["hotel"], "destination": "Казань", "departure_date": "2026-11-20"})


def test_train_and_combined_transport_questions_do_not_ask_about_flying():
    for products in (["rail"], ["avia", "rail"]):
        question = next_trip_question({"products": products, "origin": "Москва", "destination": "Казань"})
        assert "отправиться" in question
        assert "вылет" not in question


def test_compound_hotel_clarification_omits_departure_city():
    question = normalize_clarification("Из какого города и когда хотите приехать?", {"products": ["hotel"], "destination": "Казань"})
    assert "заезд" in question
    assert "из какого города" not in question


@pytest.mark.parametrize("text, kind, index", [
    ("Выбираю перелёт 1: утренний рейс", "avia", 1),
    ("Выбираю поезд 2: экспресс", "rail", 2),
    ("Выбираю отель 3: в центре", "hotel", 3),
    ("Беру второй отель", "hotel", 2),
    ("Первый поезд", "rail", 1),
    ("Выбираю гостиницу номер 2", "hotel", 2),
])
async def test_component_selection_is_typed_and_keeps_group_numbering(text, kind, index):
    decision = await interpret(text, TRIP)
    assert (decision["intent"], decision["component_type"], decision["option_index"]) == ("select_component", kind, index)
    assert decision["patch"] == {}


async def test_old_package_selection_remains_supported():
    decision = await interpret("Выбираю вариант 2", TRIP)
    assert decision["intent"] == "select" and decision["option_index"] == 2
    assert decision["component_type"] is None


@pytest.mark.parametrize("text, retained", [
    ("Подбери другой отель, остальное оставь", "keep_transport"),
    ("Подбери другой перелёт, остальное оставь", "keep_hotel"),
    ("Отель оставь, перелёт подешевле", "keep_hotel"),
    ("Поезд оставь, отель подешевле", "keep_transport"),
])
async def test_component_replacement_keeps_the_other_selection(text, retained):
    decision = await interpret(text, TRIP)
    assert decision[retained] is True
    assert decision["intent"] == "replan"


async def test_factual_question_and_date_change_both_survive_without_new_hotel_condition():
    decision = await interpret("Завтрак включён? И давай на день позже", TRIP)
    assert decision["intent"] == "replan"
    assert decision["question_text"] == "Завтрак включён?"
    assert decision["patch"]["departure_date"] == "2026-11-21"
    assert decision["patch"]["return_date"] == "2026-11-25"
    assert "constraints" not in decision["patch"]


async def test_pure_factual_question_does_not_add_constraints():
    decision = await interpret("А багаж включён?", TRIP)
    assert decision["intent"] == "question"
    assert decision["question_text"] == "А багаж включён?"
    assert decision["patch"] == {}


async def test_soft_preferences_do_not_remove_hard_constraints():
    original = deepcopy(TRIP)
    decision = await interpret("Подбери дешевле и побыстрее, желательно днём; желательно рейтинг от 8.5", TRIP)
    assert decision["patch"]["constraints"] == {
        "prefer_cheaper": True, "prefer_shorter": True, "prefer_daytime": True, "prefer_hotel_rating": 8.5,
    }
    assert TRIP == original


async def test_selected_event_date_is_not_the_departure_date():
    decision = await interpret("Выбираю концерт в Казани 24 ноября 2026", TRIP)
    assert decision["intent"] == "replan"
    assert decision["patch"]["destination"] == "Казань"
    assert decision["patch"]["event_date"] == "2026-11-24"
    assert "departure_date" not in decision["patch"] and "return_date" not in decision["patch"]
    assert "event_start_at" not in decision["patch"]
    assert decision["reset_dates"] is True


async def test_event_year_is_not_invented_by_offline_parser():
    decision = await interpret("Выбираю концерт в Казани 24 ноября", TRIP)
    assert "event_date" not in decision["patch"]
    assert "departure_date" not in decision["patch"]


async def test_explicit_event_time_is_retained_with_its_timezone():
    decision = await interpret("Выбираю концерт в Казани 2026-11-24T20:00:00+03:00", TRIP)
    assert decision["patch"]["event_date"] == "2026-11-24"
    assert decision["patch"]["event_start_at"] == "2026-11-24T20:00:00+03:00"


@pytest.mark.parametrize("change", [
    {"products": []}, {"products": ["avia", "avia"]}, {"products": ["tram"]},
    {"event_date": "2026-02-30"}, {"event_start_at": "2026-11-24T20:00:00"},
])
def test_invalid_product_or_event_patch_is_rejected(change):
    with pytest.raises(ValidationError):
        RequestPatch.model_validate(change)


def test_event_patch_serializes_as_plain_json_values():
    result = Interpretation(patch=RequestPatch(event_date="2026-11-24", event_start_at="2026-11-24T20:00:00+03:00")).as_result()
    assert result["patch"] == {"event_date": "2026-11-24", "event_start_at": "2026-11-24T20:00:00+03:00"}

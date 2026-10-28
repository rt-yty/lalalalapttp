"""Suggestions respect independent components and the exact question being answered."""
from copy import deepcopy
from uuid import uuid4

import pytest

from travel_agent.domain.models import SessionSnapshot, TurnInput
from travel_agent.graph.suggestions import build_suggestions, suggest_next_messages
from travel_agent.interpretation import RuleBasedGateway
from travel_agent.service import AgentService


def card(product, number=1, title=None):
    return {"component_id": f"{product}-{number}", "options_version": 2,
            "product_type": product, "title": title or f"{product} choice {number}",
            "price_minor": number * 100000, "currency": "RUB", "conditions": {}}


def state(products=None, *, question=None, transport=None, hotels=None, selected=None):
    request = {"products": products or ["avia", "hotel"], "origin": "Москва", "destination": "Ереван"}
    snapshot = SessionSnapshot(user_id="suggestions", session_id="components", data_mode="replay", checkout_mode="disabled",
                               request=request, transport_options=transport or [], hotel_options=hotels or [],
                               selected_components=selected or [])
    result = {"status": "needs_input" if question else "options_ready", "message": question or "Выберите подходящие варианты.",
              "questions": [question] if question else []}
    return {"session": snapshot.model_dump(mode="json"), "result": result,
            "turn": {"input": "message", "payload": {"text": "Продолжим подбор."}, "now": "2026-10-28T12:00:00+03:00"}}


async def generated(saved, texts):
    class Gateway:
        async def suggest(self, context):
            return [{"id": str(index), "label": text[:72], "text": text} for index, text in enumerate(texts)]
    result = await suggest_next_messages(saved, gateway=Gateway())
    return [item["text"] for item in result["result"]["suggestions"]]


@pytest.mark.parametrize("question,good,bad", [
    ("Когда хотите вылететь?", "Вылетаю 30 октября 2026 года.", "Возвращаюсь 30 октября 2026 года."),
    ("Когда хотите вернуться?", "Возвращаюсь 3 ноября 2026 года.", "Вылетаю 3 ноября 2026 года."),
    ("Когда хотите вернуться?", "Вернусь на следующий день после концерта.", "Нас 2 взрослых."),
    ("Когда хотите вылететь?", "Вылетаю 30 октября 2026 года.", "На семь ночей."),
    ("Когда хотите вернуться?", "Возвращаюсь 3 ноября 2026 года.", "Вылетаю 30 октября и возвращаюсь 3 ноября."),
    ("На сколько ночей ищем отель?", "На семь ночей.", "Заеду 30 октября 2026 года."),
    ("Когда хотите вылететь?", "Вылетаю 30 октября 2026 года.", "Вылетаю 30 октября, нас двое взрослых."),
    ("Когда хотите вылететь?", "Вылетаю 30 октября 2026 года.", "Вылетаю 30 октября, нужен багаж 23 кг."),
    ("Из какого города поедете?", "Поеду из Москвы.", "Из Москвы 30 октября."),
    ("Из какого города поедете?", "Поеду из Москвы.", "Из Москвы в Ереван."),
    ("Сколько взрослых поедет?", "Нас двое взрослых.", "Нас двое взрослых, бюджет 80 тысяч рублей."),
    ("Сколько лет ребёнку?", "Ребёнку 8 лет.", "Нас 2 взрослых."),
    ("Бюджет на всех или на человека?", "Бюджет на всех участников.", "Бюджет 100 тысяч рублей на всех."),
])
async def test_suggestion_answers_only_the_requested_parameter(question, good, bad):
    saved = state(question=question)
    before = deepcopy(saved)
    assert await generated(saved, [bad, good]) == [good]
    assert saved == before


def test_independent_choices_use_per_product_numbers():
    saved = state(["avia", "rail", "hotel"], transport=[card("avia"), card("rail")], hotels=[card("hotel")])
    hints = build_suggestions(SessionSnapshot.model_validate(saved["session"]), saved["result"])
    assert [item["text"] for item in hints] == [
        "Выбираю перелёт 1: avia choice 1.", "Выбираю поезд 1: rail choice 1.", "Выбираю отель 1: hotel choice 1.",
    ]


def test_selected_transport_prompts_for_the_remaining_hotel():
    flight, hotel = card("avia"), card("hotel")
    saved = state(transport=[flight], hotels=[hotel], selected=[flight])
    hints = build_suggestions(SessionSnapshot.model_validate(saved["session"]), saved["result"])
    assert hints[0]["text"] == "Выбираю отель 1: hotel choice 1."
    assert not any("Выбираю перелёт" in hint["text"] for hint in hints)


def test_selected_train_is_a_transport_choice_not_a_missing_flight():
    train, hotel = card("rail"), card("hotel")
    saved = state(["avia", "rail", "hotel"], transport=[card("avia"), train], hotels=[hotel], selected=[train])
    hints = build_suggestions(SessionSnapshot.model_validate(saved["session"]), saved["result"])
    assert hints[0]["text"] == "Выбираю отель 1: hotel choice 1."
    assert not any("Выбираю перелёт" in hint["text"] for hint in hints)


@pytest.mark.parametrize("products,allowed,forbidden", [
    (["avia"], "Выбираю перелёт 1: avia choice 1.", "Выбираю отель 1: hotel choice 1."),
    (["hotel"], "Выбираю отель 1: hotel choice 1.", "Выбираю перелёт 1: avia choice 1."),
    (["rail"], "Выбираю поезд 1: rail choice 1.", "Добавим авиабилет."),
])
async def test_request_products_exclude_unrequested_parts_even_when_old_cards_remain(products, allowed, forbidden):
    saved = state(products, transport=[card("avia"), card("rail")], hotels=[card("hotel")])
    assert await generated(saved, [forbidden, allowed]) == [allowed]


async def test_component_selection_must_match_the_current_card():
    saved = state(["hotel"], hotels=[card("hotel", title="Отель у парка")])
    assert await generated(saved, ["Выбираю отель 2: Отель у парка.", "Выбираю отель 1: Несуществующий отель.",
                                   "Выбираю отель 1: Отель у парка."]) == ["Выбираю отель 1: Отель у парка."]


async def test_dated_offer_selection_does_not_count_as_moving_the_concert():
    flight = card("avia", title="Москва — Ереван, 30 октября, без багажа")
    saved = state(["avia"], transport=[flight])
    saved["session"]["request"].update(departure_date="2026-10-30", event_date="2026-10-31")
    text = "Выбираю перелёт 1: Москва — Ереван, 30 октября, без багажа."
    assert await generated(saved, [text]) == [text]


async def test_separate_search_and_component_replacement_are_supported():
    saved = state(transport=[card("avia")], hotels=[card("hotel")], selected=[card("avia"), card("hotel")])
    text = "Подбери другой отель, остальное оставь."
    assert await generated(saved, [text]) == [text]
    assert await generated(saved, ["Поищи отель отдельно."]) == ["Поищи отель отдельно."]


async def test_event_ticket_availability_and_new_filters_are_not_suggested():
    saved = state(transport=[card("avia")])
    good = "Сравни найденные варианты дороги по времени и стоимости."
    assert await generated(saved, ["Проверь наличие билетов на концерт.", "Добавь завтрак и багаж 23 кг.", good]) == [good]


async def test_concert_context_does_not_suggest_vague_ticket_availability():
    saved = state()
    saved["session"]["dialogue"] = [{"role": "user", "text": "Хочу на концерт."}]
    assert "Проверим наличие билетов?" not in await generated(saved, ["Проверим наличие билетов?"])


async def test_removal_is_offered_only_when_user_discusses_removing_a_component():
    saved = state(transport=[card("avia")], hotels=[card("hotel")], selected=[card("avia"), card("hotel")])
    assert "Убери отель из поездки." not in await generated(saved, ["Убери отель из поездки."])
    saved["turn"]["payload"]["text"] = "Хочу убрать ненужную часть поездки."
    assert await generated(saved, ["Убери отель из поездки."]) == ["Убери отель из поездки."]


async def test_model_context_contains_separate_cards_and_current_capabilities():
    saved = state(["rail", "hotel"], transport=[card("rail")], hotels=[card("hotel")], selected=[card("rail")])
    class Gateway:
        async def suggest(self, context):
            assert context["request"]["products"] == ["rail", "hotel"]
            assert context["transport_options"][0]["product_type"] == "rail"
            assert context["hotel_options"][0]["number"] == 1
            assert context["selected_components"][0]["product_type"] == "rail"
            assert "rail_search" in context["available_capabilities"]
            assert "avia_search" not in context["available_capabilities"]
            assert "separate_flight_or_hotel_search" not in context["unavailable_capabilities"]
            return []
    await suggest_next_messages(saved, gateway=Gateway())


async def test_bounded_model_context_keeps_train_alternatives_after_many_flights():
    saved = state(["avia", "rail"], transport=[card("avia", number) for number in range(1, 12)] + [card("rail")])
    class Gateway:
        async def suggest(self, context):
            assert len(context["transport_options"]) == 4
            train = next(item for item in context["transport_options"] if item["product_type"] == "rail")
            assert train["number"] == 1
            return []
    await suggest_next_messages(saved, gateway=Gateway())


async def test_sending_generated_component_choices_assembles_trip_then_answers_tariff_question(tmp_path):
    service = AgentService(db_path=tmp_path / "choices.sqlite", gateway=RuleBasedGateway(), data_mode="replay")
    def turn(text, version=0):
        return TurnInput(request_id=str(uuid4()), user_id="suggestion-path", session_id="trip",
                         expected_state_version=version, input="message", payload={"text": text},
                         now="2026-10-28T12:00:00+03:00")
    try:
        first = await service.run_turn(turn("Из Москвы в Ереван с 24 ноября по 1 декабря 2026, один взрослый, общий бюджет 80 тысяч рублей на перелёт и отель. Багаж 23 кг и завтрак."))
        initial_request = (await service.get_session("suggestion-path", "trip")).request
        flight_choice = next(hint.text for hint in first.suggestions if hint.text.startswith("Выбираю перелёт"))
        second = await service.run_turn(turn(flight_choice, first.state_version))
        assert second.status == "plan_ready"
        assert [item["product_type"] for item in second.selected_components] == ["avia"]
        hotel_choice = next(hint.text for hint in second.suggestions if hint.text.startswith("Выбираю отель"))
        third = await service.run_turn(turn(hotel_choice, second.state_version))
        assert third.status == "plan_ready"
        assert {item["product_type"] for item in third.selected_components} == {"avia", "hotel"}
        assert third.current_plan["missing_products"] == []
        assert third.pending_action is None and third.demo_order is None
        baggage_question = next(hint.text for hint in third.suggestions if "багаж" in hint.text.lower())
        fourth = await service.run_turn(turn(baggage_question, third.state_version))
        assert "23 кг" in fourth.message
        assert fourth.selected_components == third.selected_components
        assert (await service.get_session("suggestion-path", "trip")).request == initial_request
    finally:
        service.close()

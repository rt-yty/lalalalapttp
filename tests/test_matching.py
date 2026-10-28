"""Only fully matching trips belong in the main recommendation list."""
from copy import deepcopy
from uuid import uuid4

from travel_agent.adapters.replay import ReplayAdapter
from travel_agent.domain.models import TurnInput
from travel_agent.interpretation import RuleBasedGateway
from travel_agent.service import AgentService

NOW = "2026-10-27T12:00:00+03:00"
DEMO_QUERY = (
    "Из Москвы в Ереван с 24 ноября по 1 декабря 2026, один взрослый, "
    "общий бюджет 80 тысяч рублей на перелёт и отель. Нужен багаж 23 кг и завтрак."
)


def turn(text, version=0):
    return TurnInput(
        request_id=str(uuid4()), user_id="matching-test", session_id="trip",
        expected_state_version=version, input="message", payload={"text": text},
        now=NOW, timezone="Europe/Moscow",
    )


def service_at(path, adapter=None):
    return AgentService(db_path=path, data_mode="replay", checkout_mode="demo",
                        gateway=RuleBasedGateway(), adapter=adapter)


async def test_demo_lists_only_two_fully_matching_hotels(tmp_path):
    service = service_at(tmp_path / "matching.sqlite")
    try:
        result = await service.run_turn(turn(DEMO_QUERY))
        assert result.status == "options_ready", result.model_dump()
        assert len(result.options) == 2
        assert {o["total_minor"] for o in result.options} == {6_792_000, 7_344_300}
        for option in result.options:
            assert option["eligibility"] == "satisfied"
            assert all(check["status"] == "satisfied" for check in option["checks"] if check["hard"])
            flight = next(c for c in option["components"] if c["product_type"] == "avia")
            hotel = next(c for c in option["components"] if c["product_type"] == "hotel")
            assert flight["conditions"]["baggage_kg"] >= 23
            assert hotel["conditions"]["breakfast"] is True
            assert option["total_minor"] <= 8_000_000
        saved = await service.get_session("matching-test", "trip")
        assert saved.options == result.options
    finally:
        service.close()


async def test_too_small_budget_returns_no_cards_and_keeps_hard_constraints(tmp_path):
    service = service_at(tmp_path / "budget.sqlite")
    try:
        initial = await service.run_turn(turn(DEMO_QUERY))
        original = await service.get_session("matching-test", "trip")
        result = await service.run_turn(turn("Бюджет теперь 50 тысяч рублей на всех", initial.state_version))
        assert result.status == "partial", result.model_dump()
        assert result.options == []
        assert result.current_plan is None
        explanation = " ".join([result.message, *result.warnings]).lower()
        assert "бюджет" in explanation or "лимит" in explanation
        assert "50" in explanation or "стоимост" in explanation or "цен" in explanation
        saved = await service.get_session("matching-test", "trip")
        assert saved.options == []
        assert saved.request.budget_minor == 5_000_000
        assert saved.request.constraints == original.request.constraints
        assert saved.request.constraints["baggage_kg"] == 23
        assert saved.request.constraints["breakfast"] is True
        assert saved.request.origin == original.request.origin
        assert saved.request.destination == original.request.destination
    finally:
        service.close()


async def test_pet_friendly_hotel_does_not_hide_unknown_airline_pet_policy(tmp_path):
    fixture = deepcopy(ReplayAdapter().fixture)
    for hotel in fixture["result"]["hotels"]:
        hotel["conditions"]["pets"] = True
        hotel["data_origin"] = "synthetic"
    for flight in fixture["result"]["flights"]:
        flight["conditions"]["pets"] = None
        flight["data_origin"] = "synthetic"
    fixture["result"]["warnings"] = ["Синтетическая проверка: отели разрешают питомцев, правила авиаперевозки неизвестны."]
    service = service_at(tmp_path / "pets.sqlite", ReplayAdapter(fixture=fixture))
    try:
        result = await service.run_turn(turn(DEMO_QUERY + " Еду с собакой."))
        assert result.status == "partial", result.model_dump()
        assert result.options == []
        assert result.current_plan is None
        assert result.pending_action is None
        explanation = " ".join([result.message, *result.warnings]).lower()
        assert "питом" in explanation or "перевоз" in explanation
        assert any(word in explanation for word in ("неизвест", "не подтверж", "нет подтверж", "провер"))
        saved = await service.get_session("matching-test", "trip")
        assert saved.options == []
        assert saved.request.pets is True
        assert saved.request.constraints["baggage_kg"] == 23
    finally:
        service.close()

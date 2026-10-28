"""Suggested questions must have a useful supported reply, not a dead end."""
from uuid import uuid4
from travel_agent.domain.models import TurnInput
from travel_agent.interpretation import RuleBasedGateway
from travel_agent.service import AgentService


def turn(version, text):
    return TurnInput(request_id=str(uuid4()), user_id="followup", session_id="trip",
                     expected_state_version=version, input="message", payload={"text":text},
                     now="2026-10-27T17:15:00+03:00")


async def test_offer_comparison_preserves_options_and_explains_known_conditions(tmp_path):
    service = AgentService(db_path=tmp_path / "compare.sqlite", gateway=RuleBasedGateway())
    try:
        first = await service.run_turn(turn(0, "Из Москвы в Ереван с 24 ноября по 1 декабря 2026, один взрослый, общий бюджет 80 тысяч рублей. Багаж 23 кг и завтрак."))
        result = await service.run_turn(turn(first.state_version, "Сравни условия отмены у предложенных вариантов."))
        assert result.status == "options_ready"
        assert result.options == first.options
        assert result.transport_options == first.transport_options
        assert result.hotel_options == first.hotel_options
        assert result.current_plan is None
        assert all(o["title"] in result.message for o in result.transport_options + result.hotel_options)
        assert "отмен" in result.message.lower() or "возврат" in result.message.lower()
    finally:
        service.close()


async def test_blocker_question_explains_budget_without_offering_the_same_question_again(tmp_path):
    service = AgentService(db_path=tmp_path / "blocked.sqlite", gateway=RuleBasedGateway())
    try:
        first = await service.run_turn(turn(0, "Из Москвы в Ереван с 24 ноября по 1 декабря 2026, один взрослый, общий бюджет 50 тысяч рублей. Багаж 23 кг и завтрак."))
        assert first.status == "partial"
        before = await service.get_session("followup", "trip")
        result = await service.run_turn(turn(first.state_version, "Какое из моих условий мешает подобрать поездку?"))
        assert result.status == "partial"
        assert "бюджет" in result.message.lower() or "лимит" in result.message.lower()
        for answer in (first, result):
            assert all("Какое из моих условий" not in hint.text and "причин" not in hint.label.lower()
                       for hint in answer.suggestions)
        saved = await service.get_session("followup", "trip")
        assert saved.request == before.request
        assert saved.request.budget_minor == 5000000
        assert saved.options == []
        assert saved.request.constraints["baggage_kg"] == 23
    finally:
        service.close()

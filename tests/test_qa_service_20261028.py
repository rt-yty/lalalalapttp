from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_component_flow import ComponentAdapter, REQUEST, ScriptedGateway, turn
from travel_agent.service import AgentService


@pytest.mark.parametrize('interval', ['с 15 по 20 ноября 2026', '15 - 20 ноября 2026'])
@pytest.mark.parametrize('amount', ['−5000', '-5000', '−50\u202f000'])
async def test_negative_budget_keeps_independently_valid_trip_fields_and_continues(tmp_path, amount, interval):
    prompt = f'Нужен только отель в Казани {interval}, двое взрослых, бюджет {amount} рублей.'
    correction = 'Исправляю: 50 тысяч рублей за всё проживание.'
    gateway = ScriptedGateway(REQUEST, {correction: {'intent': 'replan', 'patch': {'budget_minor': 5000000, 'budget_scope': 'trip'}}})
    service = AgentService(db_path=tmp_path/'negative.sqlite', gateway=gateway, adapter=ComponentAdapter(), checkout_mode='disabled')
    try:
        first = await service.run_turn(turn(text=prompt))
        snapshot = await service.get_session('component-user', 'trip')
        assert first.status == 'needs_input' and 'больше нуля' in first.message
        assert snapshot.request.destination == 'Казань'
        assert snapshot.request.products == ['hotel']
        assert str(snapshot.request.departure_date) == '2026-11-15'
        assert str(snapshot.request.return_date) == '2026-11-20'
        assert snapshot.request.adults == 2
        assert snapshot.request.budget_minor is None
        result = await service.run_turn(turn(first.state_version, correction))
        assert result.status == 'options_ready', result.message
        assert result.hotel_options and not result.transport_options
    finally:
        service.close()


@pytest.mark.parametrize('stop', ['Отмени поездку', 'Отложим планирование', 'Не хочу путешествовать.'])
async def test_stopping_planning_revokes_pending_confirmation_but_keeps_plan(tmp_path, stop):
    from test_component_flow import assembled
    service = AgentService(db_path=tmp_path/'stopped.sqlite', gateway=ScriptedGateway(REQUEST),
                           adapter=ComponentAdapter(), checkout_mode='demo')
    try:
        chosen = await assembled(service)
        checkout = await service.run_turn(turn(chosen.state_version, 'Оформить'))
        assert checkout.status == 'needs_confirmation'
        stopped = await service.run_turn(turn(checkout.state_version, stop))
        saved = await service.get_session('component-user', 'trip')
        assert saved.current_plan is not None
        assert saved.pending_action is None
        assert saved.last_question is None
        later = await service.run_turn(turn(stopped.state_version, 'Да'))
        assert later.status != 'completed_demo' and later.demo_order is None
    finally:
        service.close()


@pytest.mark.parametrize('question', ['Какие отели ты уже подобрал?', 'Покажи найденные отели', 'Напомни, какие варианты ты нашёл'])
async def test_recall_found_hotels_lists_saved_cards_without_new_search(tmp_path, question):
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path/'recall.sqlite', checkout_mode='disabled', adapter=adapter,
                           gateway=ScriptedGateway({**REQUEST, 'products': ['hotel']},
                               {question: {'intent': 'question', 'patch': {}, 'question_text': question}}))
    try:
        first = await service.run_turn(turn())
        before = await service.get_session('component-user', 'trip')
        result = await service.run_turn(turn(first.state_version, question))
        assert all(card['title'] in result.message for card in first.hotel_options)
        assert '₽' in result.message
        assert 'нет подтверждённых сведений' not in result.message
        after = await service.get_session('component-user', 'trip')
        assert after.request == before.request and after.hotel_options == before.hotel_options
        assert len(adapter.calls) == 1
    finally:
        service.close()


@pytest.mark.parametrize("kind", ["rail", "bus"])
def test_transport_condition_messages_do_not_expose_internal_constraint_keys(kind):
    from travel_agent.domain.planning import check_components
    checks = check_components([{"product_type": kind, "price_minor": 100000, "availability": "reported_available", "conditions": {"details_verified": True}}],
                              {**REQUEST, "products": [kind], "pets": True, "constraints": {"refundable": True}})
    relevant = [c for c in checks if c["constraint_id"].startswith(kind + ".")]
    assert relevant and any(c["constraint_id"].endswith(".pets") for c in relevant)
    assert all(kind + "." not in c["explanation"] and "reported_available" not in c["explanation"] for c in relevant)
    assert any("Перевозка питомца" in c["explanation"] for c in relevant)

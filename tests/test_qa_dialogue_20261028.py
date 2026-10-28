"""Independent traveler QA: grounded budgets, negations and concert periods."""
from copy import deepcopy
from datetime import datetime

import pytest

from travel_agent.concert_intake import ConcertIntakeState, filter_event_preferences, update_intake
from travel_agent.interpretation import apply_request_scope_guard, explicit_budget, nightly_budget


@pytest.mark.parametrize('separator', [' ', '\u00a0', '\u202f', '\t'])
@pytest.mark.parametrize('amount', ['50', '-50'])
def test_budget_accepts_human_number_grouping_without_losing_thousands(separator, amount):
    assert explicit_budget(f'Бюджет {amount}{separator}000 рублей на всех') == (int(amount) * 100000, 'trip')
    assert nightly_budget(f'{amount}{separator}000 рублей за ночь') == (int(amount) * 100000, 'trip')


@pytest.mark.parametrize('text,key', [
    ('Мне не нужен тихий номер', 'quiet_room'),
    ('Не нужен доступный вход для коляски', 'wheelchair_accessible'),
    ('Не хочу номер вдали от лифта', 'away_from_elevator'),
    ('Доступный вход больше не обязателен', 'wheelchair_accessible'),
    ('Номер вдали от лифта не обязателен', 'away_from_elevator'),
])
def test_explicit_removal_is_not_reintroduced_as_a_mandatory_condition(text, key):
    original = {'intent': 'replan', 'patch': {'constraints': {key: False}}}
    before = deepcopy(original)
    result = apply_request_scope_guard(text, {'destination': 'Казань'}, original)
    assert result['patch']['constraints'][key] is False
    assert original == before


@pytest.mark.parametrize('text,key', [
    ('Хочу отель, желательно доступный вход для коляски', 'wheelchair_accessible'),
    ('Хочу отель, по возможности доступный вход без ступенек', 'wheelchair_accessible'),
    ('Хочу отель, желательно вдали от лифта', 'away_from_elevator'),
    ('Нужен отель, желательно тихий номер', 'quiet_room'),
])
def test_soft_preferences_do_not_become_mandatory_filters(text, key):
    result = apply_request_scope_guard(text, {}, {'intent': 'plan', 'patch': {}})
    assert result['patch'].get('constraints', {}).get(key) is not True


def test_independent_hotel_conditions_do_not_share_requirement_or_negation():
    result = apply_request_scope_guard('Нужен тихий номер; доступный вход не обязателен.', {}, {'intent': 'plan', 'patch': {}})
    assert result['patch']['constraints'] == {'quiet_room': True, 'wheelchair_accessible': False}


@pytest.mark.parametrize('text,preferences', [
    ('Не хочу концерт в Москве, лучше в Казани', {'locations': 'Казань'}),
    ('Не хочу на концерт без детей', {'children_pending': True}),
])
def test_concert_correction_does_not_cancel_goal_or_replace_known_artist(text, preferences):
    before = ConcertIntakeState(mode='trip', event_name='Muse', locations='Москва', period='ноябрь 2026')
    result = update_intake(before, text, {'concert_intake': {'mode': 'trip', **preferences}}, {})
    assert result.mode == 'trip'
    assert result.event_name == 'Muse'
    for key, value in preferences.items():
        assert getattr(result, key) == value
    assert before.event_name == 'Muse' and before.locations == 'Москва'


@pytest.mark.parametrize('text', ['Не хочу концерт', 'Не нужен концерт.', 'Без концерта', 'Отменяю концерт'])
def test_unqualified_concert_cancellation_is_still_respected(text):
    result = update_intake(ConcertIntakeState(mode='trip', event_name='Muse'), text, {}, {})
    assert result.mode == 'inactive'


@pytest.mark.parametrize('period,expected', [
    ('ноябрь и декабрь 2026', ['2026-11-15', '2026-12-15']),
    ('ноябрь или январь', ['2026-11-15', '2027-01-15']),
    ('с ноября по январь', ['2026-11-15', '2026-12-15', '2027-01-15']),
    ('декабрь 2026 — январь 2027', ['2026-12-15', '2027-01-15']),
    ('ноябрь 2026', ['2026-11-15']),
    ('декабрь 2026 — январь', ['2026-12-15', '2027-01-15']),
    ('декабрь — январь 2027', ['2026-12-15', '2027-01-15']),
])
def test_all_explicit_concert_months_are_considered(period, expected):
    events = [{'date': d, 'city': 'Москва', 'country': 'Россия'} for d in
              ['2026-10-15', '2026-11-15', '2026-12-15', '2027-01-15', '2027-02-15']]
    result = filter_event_preferences(events, {'period': period, 'locations': 'Россия'}, datetime.fromisoformat('2026-10-28T12:00:00+03:00'))
    assert [event['date'] for event in result] == expected


async def test_service_preserves_grouped_budget_over_model_answer(tmp_path):
    from test_component_flow import ComponentAdapter, REQUEST, ScriptedGateway, turn
    from travel_agent.service import AgentService
    text = 'Бюджет 150\u202f000 рублей на всех'
    gateway = ScriptedGateway(REQUEST, {text: {'intent': 'replan', 'patch': {'budget_minor': 15000000, 'budget_scope': 'trip'}}})
    service = AgentService(db_path=tmp_path/'grouped-budget.sqlite', gateway=gateway, adapter=ComponentAdapter(), checkout_mode='disabled')
    try:
        first = await service.run_turn(turn())
        await service.run_turn(turn(first.state_version, text))
        saved = await service.get_session('component-user', 'trip')
        assert saved.request.budget_minor == 15000000
        assert saved.request.budget_scope == 'trip'
    finally:
        service.close()


@pytest.mark.parametrize('text,expected', [
    ('Желательно тихий номер, нужен доступный вход', {'wheelchair_accessible': True}),
    ('Нужен тихий номер, желательно доступный вход', {'quiet_room': True}),
    ('Мне не нужен тихий номер, нужен доступный вход', {'quiet_room': False, 'wheelchair_accessible': True}),
])
def test_requirement_qualifiers_stay_with_their_own_feature(text, expected):
    result = apply_request_scope_guard(text, {}, {'intent': 'plan', 'patch': {}})
    assert result['patch'].get('constraints', {}) == expected


@pytest.mark.parametrize('text', [
    'Можно ли сходить в музеи Казани с бюджетом до 5000 рублей?',
    'Бывают экскурсии в Казани до 5000 рублей?',
    'Какая виза нужна в Японию?',
    'Расскажи про обычаи Японии',
    'Как принято одеваться на концерт в Японии?',
    'Какие традиции фестиваля Танабата?',
    'Сколько нужно денег на еду в Казани?',
    'Какая погода сейчас в Казани?',
])
def test_information_lookup_cannot_smuggle_a_route_or_budget_edit(text):
    from travel_agent.interpretation import apply_capability_guard
    decision = {'intent': 'web_search', 'web_query': text, 'patch': {'destination': 'Казань', 'budget_minor': 500000},
                'reset_dates': True, 'concert_intake': {'mode': 'trip'}}
    result = apply_capability_guard(text, {}, decision)
    assert result['read_only_research'] is True
    assert result['patch'] == {} and result['concert_intake'] is None and result['reset_dates'] is False
    assert result['web_query'] == text


@pytest.mark.parametrize('text', [
    'Какая погода в Казани? Поменяй направление поездки на Казань.',
    'Поменяй бюджет на 50 тыс и узнай про визу',
    'Расскажи про Японию и подбери отель в Токио',
    'Нужен отель в Казани. Какая там погода?',
    'Где концерты Imagine Dragons?',
    'Когда будет концерт Muse?',
])
def test_mixed_booking_commands_and_event_schedules_keep_their_routing(text):
    from travel_agent.interpretation import apply_capability_guard
    decision = {'intent': 'web_search', 'web_query': text, 'patch': {'destination': 'Казань'}}
    result = apply_capability_guard(text, {}, decision)
    assert result == decision


@pytest.mark.parametrize('text', [
    'Из Москвы хочу в Казань и Сочи с 15 по 20 ноября 2026, двое взрослых.',
    'Хочу посетить два города из Москвы: Казань, затем в Сочи с 15 по 20 ноября 2026, двое взрослых.',
])
def test_multicity_clarification_retains_unrelated_explicit_dates_and_party(text):
    from travel_agent.interpretation import apply_capability_guard
    decision = {'intent': 'plan', 'patch': {'origin': 'Москва'}, 'question': 'Пока могу подобрать поездку в один город. Какой город выбрать?'}
    result = apply_capability_guard(text, {}, decision, now='2026-10-28T12:00:00+03:00')
    assert result['patch'] == {'origin': 'Москва', 'departure_date': '2026-11-15', 'return_date': '2026-11-20', 'adults': 2}
    assert result['question']


@pytest.mark.parametrize('text', ['Отменим пока поездку.', 'Давай отложим поездку', 'Отмени планирование поездки', 'Останови подбор'])
def test_stop_trip_planning_is_a_chat_acknowledgement(text):
    from travel_agent.response_policy import policy_decision
    result = policy_decision(text, {'destination': 'Казань'})
    assert result['intent'] == 'chat' and result['patch'] == {}
    assert 'остановлю подбор' in result['reply_text']


@pytest.mark.parametrize('text', ['Отмени выбор отеля', 'Отмени бронь', 'Отмени поездку в Казань, хочу в Сочи',
                                'Просто поболтаем о Японии, но сначала найди билеты в Токио'])
def test_stop_policy_does_not_swallow_specific_cancellations_or_mixed_requests(text):
    from travel_agent.response_policy import stops_planning, policy_decision
    assert stops_planning(text) is False
    assert policy_decision(text, {}) is None


@pytest.mark.parametrize('text', [
    'Проверь погоду в Казани, отель нужен там с 15 по 20 ноября',
    'Какая погода в Казани, хочу перенести поездку туда?',
    'Проверь прогноз, билеты мне нужны в Казань на 15 ноября',
])
def test_mixed_request_keeps_reversed_service_word_order_and_infinitives(text):
    from travel_agent.interpretation import apply_capability_guard
    decision = {'intent': 'web_search', 'web_query': 'Погода в Казани', 'patch': {'destination': 'Казань'}}
    assert apply_capability_guard(text, {}, decision) == decision


@pytest.mark.parametrize('text,key', [
    ('Желательно тихий номер', 'quiet_room'),
    ('По возможности доступный вход без ступенек', 'wheelchair_accessible'),
    ('Желательно вдали от лифта', 'away_from_elevator'),
])
@pytest.mark.parametrize('existing', [False, True])
def test_explicit_soft_preference_overrides_model_hard_condition(text, key, existing):
    request = {'constraints': {key: True}} if existing else {}
    decision = {'intent': 'replan', 'patch': {'constraints': {key: True}}}
    result = apply_request_scope_guard(text, request, decision)
    assert result['patch']['constraints'][key] is False


@pytest.mark.parametrize('count', [10, 20])
def test_multicity_recovery_does_not_insert_an_unsupported_adult_count(count):
    from travel_agent.interpretation import apply_capability_guard
    text = f'Из Москвы хочу в Казань и Сочи с 15 по 20 ноября 2026, {count} взрослых'
    decision = {'intent': 'plan', 'patch': {}, 'question': 'Какой один город выбрать?'}
    result = apply_capability_guard(text, {}, decision, now='2026-10-28T12:00:00+03:00')
    assert 'adults' not in result['patch']
    assert result['patch']['departure_date'] == '2026-11-15'
    assert result['patch']['return_date'] == '2026-11-20'


@pytest.mark.parametrize('text,expected', [
    ('30000 рублей за всё', (3000000, 'trip')),
    ('30 тысяч рублей за всё проживание', (3000000, 'trip')),
    ('Исправляю: 50 тысяч рублей за всё проживание.', (5000000, 'trip')),
    ('50\u202f000 ₽ на всех', (5000000, 'trip')),
    ('30000 рублей на человека', (3000000, 'per_person')),
    ('−30000 рублей за всё', (-3000000, 'trip')),
])
def test_volunteered_scoped_ruble_limit_does_not_need_budget_prefix(text, expected):
    assert explicit_budget(text) == expected


@pytest.mark.parametrize('text', [
    '30000 за всё', '30000 рублей', '30000 евро за всё',
    '5000 рублей за ночь',
    'Посчитай: билеты 25000 рублей за всё и отель 30000 рублей за всё проживание',
    'Отель стоит 30000 рублей за всё проживание',
    'Я заплатил 30000 рублей за всё',
    '30000 рублей за всё проживание — это дорого?',
])
def test_volunteered_budget_fallback_does_not_guess_currency_scope_or_reprice_a_quote(text):
    assert explicit_budget(text) is None


async def test_invalid_date_correction_keeps_volunteered_budget_even_if_model_omits_it(tmp_path):
    from test_component_flow import ComponentAdapter, ScriptedGateway, turn
    from travel_agent.service import AgentService
    first_text = 'Нужен отель в Казани с 31 ноября по 5 декабря 2026, один взрослый, 30000 рублей за всё.'
    second_text = 'Заезд 30 ноября, выезд 5 декабря 2026.'
    request = {'destination': 'Казань', 'return_date': '2026-12-05', 'adults': 1, 'products': ['hotel']}
    gateway = ScriptedGateway({}, {
        first_text: {'intent': 'plan', 'patch': request},
        second_text: {'intent': 'replan', 'patch': {'departure_date': '2026-11-30', 'return_date': '2026-12-05'}},
    })
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path/'calendar-budget.sqlite', gateway=gateway, adapter=adapter)
    try:
        first = await service.run_turn(turn(text=first_text))
        saved = await service.get_session('component-user', 'trip')
        assert first.status == 'needs_input' and saved.request.budget_minor == 3000000
        assert saved.request.budget_scope == 'trip' and not adapter.calls
        second = await service.run_turn(turn(first.state_version, second_text))
        saved = await service.get_session('component-user', 'trip')
        assert second.status == 'options_ready'
        assert saved.request.budget_minor == 3000000 and saved.request.budget_scope == 'trip'
        assert adapter.calls[-1]['budget_minor'] == 3000000
    finally:
        service.close()


@pytest.mark.parametrize('products', [['hotel'], ['avia'], ['rail']])
def test_date_party_budget_reply_cannot_add_unrequested_products(products):
    request = {'destination': 'Казань', 'products': products}
    decision = {'intent': 'replan', 'patch': {'products': ['avia', 'hotel'], 'adults': 2}}
    result = apply_request_scope_guard('С 15 по 20 ноября 2026, двое взрослых, 50 тысяч рублей за всё.', request, decision)
    assert result['patch']['products'] == products
    assert result['patch']['adults'] == 2


@pytest.mark.parametrize('text', ['И билеты тоже', 'Добавь дорогу', 'Нужен весь комплект', 'Добавь отель'])
def test_explicit_additional_service_is_not_suppressed(text):
    request = {'destination': 'Казань', 'products': ['hotel']}
    decision = {'intent': 'replan', 'patch': {'products': ['avia', 'hotel']}}
    assert apply_request_scope_guard(text, request, decision) == decision


@pytest.mark.parametrize('text,question', [('Да', 'Добавить перелёт?'), ('Оба', 'Нужен перелёт или отель?'), ('Второй вариант', 'Поезд или автобус выбрать?')])
def test_short_answer_to_product_choice_may_change_scope(text, question):
    request = {'destination': 'Казань', 'products': ['hotel'], '_dialogue': {'last_question': question}}
    decision = {'intent': 'replan', 'patch': {'products': ['avia', 'hotel']}}
    assert apply_request_scope_guard(text, request, decision) == decision


@pytest.mark.parametrize('products,model_products,question', [
    (['hotel'], ['avia', 'hotel'], 'Какой город отправления?'),
    (['hotel'], ['hotel'], 'Какой город отправления?'),
    (['avia'], ['avia', 'hotel'], 'Нужен ли завтрак в отеле?'),
])
async def test_single_service_intake_survives_followup_without_irrelevant_questions(tmp_path, products, model_products, question):
    from test_component_flow import ComponentAdapter, ScriptedGateway, turn
    from travel_agent.service import AgentService
    text = 'С 15 по 20 ноября 2026, двое взрослых, 50 тысяч рублей за всё.'
    request = {'destination': 'Казань', 'products': products}
    if products != ['hotel']:
        request['origin'] = 'Москва'
    gateway = ScriptedGateway(request, {text: {'intent': 'replan', 'patch': {'products': model_products,
        'departure_date': '2026-11-15', 'return_date': '2026-11-20', 'adults': 2, 'budget_minor': 5000000,
        'budget_scope': 'trip'}, 'question': question}})
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path/'single-service.sqlite', gateway=gateway, adapter=adapter)
    try:
        first = await service.run_turn(turn())
        result = await service.run_turn(turn(first.state_version, text))
        saved = await service.get_session('component-user', 'trip')
        assert saved.request.products == products
        assert result.status == 'options_ready' and not result.questions
        assert len(adapter.calls) == 1 and adapter.calls[0]['products'] == products
        assert bool(result.hotel_options) is (products == ['hotel'])
        assert bool(result.transport_options) is (products != ['hotel'])
    finally:
        service.close()


@pytest.mark.parametrize('adults,children,pending,needs_scope', [
    (1, [], False, False),
    (2, [], False, True),
    (1, [7], False, True),
    (1, [], True, True),
])
def test_concert_budget_scope_is_only_ambiguous_for_multiple_or_unresolved_travellers(adults, children, pending, needs_scope):
    from travel_agent.concert_intake import missing_preferences
    state = ConcertIntakeState(mode='trip', event_name='Muse', period='ноябрь 2026', locations='Казань', children_pending=pending)
    request = {'origin': 'Москва', 'adults': adults, 'children_ages': children, 'budget_minor': 5000000}
    fields = [key for key, _ in missing_preferences(state, request)]
    assert ('budget_scope' in fields) is needs_scope
    assert ('children' in fields) is pending


@pytest.mark.parametrize('text', ['change-8dd16d64-4767-40c9-9fa6-322cd47b49eb', 'Поменяем состав услуг'])
def test_scope_fallback_does_not_override_semantic_decisions_for_unrecognized_messages(text):
    request = {'origin': 'Москва', 'destination': 'Казань', 'products': ['hotel']}
    decision = {'intent': 'replan', 'patch': {'products': ['avia', 'hotel']}}
    assert apply_request_scope_guard(text, request, decision) == decision

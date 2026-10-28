"""Independent audit regressions: known tariff facts and scoped negations."""
from copy import deepcopy

import pytest

from test_component_flow import ComponentAdapter, REQUEST, ScriptedGateway, turn, select
from test_tariff_questions import state
from travel_agent.response_policy import policy_decision
from travel_agent.service import AgentService


def detailed_state():
    snapshot = state()
    hotel = next(c for c in snapshot.selected_components if c['product_type'] == 'hotel')
    hotel['conditions'].update(check_in_time='14:00', check_out_time='12:00',
                               meal='Всё включено', pets=False, rating=8.7, stars=4)
    hotel['query'] = {'departure_date': '2026-11-10', 'return_date': '2026-11-17'}
    return snapshot


@pytest.mark.parametrize('question,expected,excluded', [
    ('Во сколько заселение в отель?', ['14:00'], ['12:00', 'Flight A', '₽']),
    ('Когда выезд из отеля?', ['12:00', '17.11.2026'], ['14:00', 'Flight A', '₽']),
    ('Когда заезд?', ['10.11.2026', '14:00'], ['Flight A', '₽']),
    ('Какие даты проживания?', ['10.11.2026', '17.11.2026'], ['Flight A', '₽']),
    ('Какое питание в отеле?', ['Всё включено'], ['Flight A', '₽']),
    ('Можно с собакой в выбранный отель?', ['питомцем: нет'], ['Flight A', '₽']),
    ('Какой рейтинг у отеля?', ['8.7'], ['Flight A', '₽']),
    ('Сколько звёзд у отеля?', ['4'], ['Flight A', '₽']),
])
def test_tariff_answers_use_already_confirmed_specific_facts(question, expected, excluded):
    snapshot = detailed_state()
    before = deepcopy(snapshot)
    answer = AgentService._answer_question(snapshot, question)
    assert all(text in answer for text in expected), answer
    assert all(text not in answer for text in excluded), answer
    assert 'Hotel A' in answer and 'Hotel B' not in answer
    assert snapshot == before
    assert len(answer) < 500


@pytest.mark.parametrize('question', [
    'Во сколько заселение в отель?', 'Когда выезд из отеля?',
    'Можно с собакой в отель?', 'Какой рейтинг у отеля?', 'Сколько звёзд у отеля?',
])
def test_missing_tariff_facts_are_explicitly_unknown(question):
    answer = AgentService._answer_question(state(), question)
    assert 'не указан' in answer, answer
    assert '14:00' not in answer and 'питомцем: да' not in answer


def test_meal_question_does_not_infer_full_board_from_breakfast():
    answer = AgentService._answer_question(state(), 'Какое питание в отеле?')
    assert 'Питание: не указано' in answer
    assert 'Завтрак включён: да' in answer
    assert 'Всё включено' not in answer


@pytest.mark.parametrize('text,patch', [
    ('Не хочу путешествовать один, нас будет двое взрослых.', {'adults': 2}),
    ('Не хочу путешествовать на автобусе, только поезд.', {'products': ['rail']}),
    ('Не хочу путешествовать без собаки, она едет со мной.', {'pets': True}),
])
async def test_rejecting_a_trip_condition_is_not_rejecting_the_trip(tmp_path, text, patch):
    assert policy_decision(text, REQUEST) is None
    gateway = ScriptedGateway(REQUEST, {text: {'intent': 'replan', 'patch': patch}})
    service = AgentService(db_path=tmp_path/'negation.sqlite', data_mode='live', checkout_mode='disabled',
                           gateway=gateway, adapter=ComponentAdapter())
    try:
        first = await service.run_turn(turn())
        result = await service.run_turn(turn(first.state_version, text))
        saved = await service.get_session('component-user', 'trip')
        assert result.response_kind == 'trip'
        assert 'остановлю' not in result.message
        for key,value in patch.items():
            assert getattr(saved.request, key) == value
    finally:
        service.close()


@pytest.mark.parametrize('text', ['Не хочу путешествовать.', 'Я не хочу путешествовать!',
    'Вообще не хочу путешествовать, просто болтаю.', 'Просто поболтаем?'])
def test_unqualified_refusal_still_stops_planning(text):
    decision = policy_decision(text, REQUEST)
    assert decision['intent'] == 'chat' and decision['patch'] == {}
    assert 'остановлю' in decision['reply_text']


async def test_public_known_hotel_facts_preserve_selected_plan_without_search(tmp_path):
    class DetailedAdapter(ComponentAdapter):
        async def search(self, request, now):
            result = await super().search(request, now)
            for hotel in result['hotels']:
                hotel['conditions'].update(check_in_time='14:00', check_out_time='12:00',
                                           meal='Всё включено', pets=False, rating=8.7, stars=4)
            return result
    questions = ['Во сколько заселение в отель?', 'Какое питание в отеле?',
                 'Можно с собакой в отель?', 'Какой рейтинг у отеля?']
    gateway = ScriptedGateway(REQUEST, {q: {'intent': 'question', 'patch': {}, 'question_text': q} for q in questions})
    adapter = DetailedAdapter()
    service = AgentService(db_path=tmp_path/'facts.sqlite', data_mode='live', checkout_mode='disabled',
                           gateway=gateway, adapter=adapter)
    try:
        result = await service.run_turn(turn())
        result = await select(service, result, result.hotel_options[0])
        before = await service.get_session('component-user', 'trip')
        searches = len(adapter.calls)
        for question, fact in zip(questions, ['14:00', 'Всё включено', 'питомцем: нет', '8.7']):
            result = await service.run_turn(turn(result.state_version, question))
            assert fact in result.message, result.message
            after = await service.get_session('component-user', 'trip')
            assert after.request == before.request
            assert after.selected_components == before.selected_components
            assert after.current_plan == before.current_plan
        assert len(adapter.calls) == searches
    finally:
        service.close()


@pytest.mark.parametrize('phrase', ['в одну сторону', 'в один конец', 'без обратного билета',
                                  'one-way', 'one way', 'только туда'])
async def test_unsupported_one_way_synonyms_cannot_become_generic_city_intake(tmp_path, phrase):
    text = 'Нужен билет из Москвы в Казань ' + phrase + ' 15 ноября 2026, один взрослый.'
    gateway = ScriptedGateway(REQUEST, {text: {'intent': 'plan', 'patch': {},
        'question_text': 'Могу подобрать только туда и обратно. Подойдёт?'}})
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path/'oneway.sqlite', data_mode='live', checkout_mode='disabled',
                           gateway=gateway, adapter=adapter)
    try:
        result = await service.run_turn(turn(text=text))
        assert result.status == 'needs_input'
        assert 'только туда и обратно' in result.message
        assert 'Куда хотите поехать' not in result.message and 'Ереван' not in result.message
        assert not adapter.calls and not result.transport_options
        assert result.questions == [result.message]
    finally:
        service.close()


@pytest.mark.parametrize('text', ['Нужен не в одну сторону, а туда и обратно.',
    'Не хочу билет one-way, нужен туда и обратно.', 'Объясни выражение «в одну сторону».'])
def test_one_way_negations_and_quoted_discussion_are_not_blocked(text):
    from travel_agent.interpretation import apply_capability_guard
    decision = {'intent': 'question', 'patch': {}, 'question_text': text}
    assert apply_capability_guard(text, REQUEST, decision) == decision


@pytest.mark.parametrize('departure,returning', [('2026-11-20', '2026-11-15'),
                                               ('2026-11-20', '2026-11-20')])
async def test_invalid_date_order_keeps_given_details_and_asks_one_specific_correction(tmp_path, departure, returning):
    supplied = {**REQUEST, 'departure_date': departure, 'return_date': returning, 'adults': 2}
    adapter = ComponentAdapter()
    gateway = ScriptedGateway(supplied, {'25 ноября': {'intent': 'replan', 'patch': {'return_date': '2026-11-25'}}})
    service = AgentService(db_path=tmp_path/'order.sqlite', data_mode='live', checkout_mode='disabled',
                           gateway=gateway, adapter=adapter)
    try:
        first = await service.run_turn(turn())
        assert first.status == 'needs_input' and first.error is None, first.message
        assert 'позже' in first.message and '20.11.2026' in first.message
        assert len(first.questions) == 1 and first.message.count('?') == 1
        assert not adapter.calls
        saved = await service.get_session('component-user', 'trip')
        assert saved.request.destination == REQUEST['destination'] and saved.request.adults == 2
        assert str(saved.request.departure_date) == departure and saved.request.return_date is None
        second = await service.run_turn(turn(first.state_version, '25 ноября'))
        assert second.status == 'options_ready', second.message
        assert adapter.calls[0]['return_date'] == '2026-11-25'
        assert adapter.calls[0]['adults'] == 2
    finally:
        service.close()


async def test_invalid_date_change_removes_old_prices_until_correction(tmp_path):
    adapter = ComponentAdapter()
    gateway = ScriptedGateway(REQUEST, {'wrong': {'intent': 'replan', 'patch': {'return_date': '2026-11-09'}}})
    service = AgentService(db_path=tmp_path/'change-order.sqlite', data_mode='live', checkout_mode='disabled',
                           gateway=gateway, adapter=adapter)
    try:
        first = await service.run_turn(turn())
        first = await select(service, first, first.transport_options[0])
        result = await service.run_turn(turn(first.state_version, 'wrong'))
        assert result.status == 'needs_input' and result.error is None
        assert not result.transport_options and not result.hotel_options
        assert not result.selected_components and result.current_plan is None
        assert len(adapter.calls) == 1
    finally:
        service.close()


@pytest.mark.parametrize('text,kind', [
    ('Нужны авиабилеты Москва — Сочи с 15 по 20 ноября 2026, двое взрослых.', 'avia'),
    ('Нужен поезд из Москвы в Казань с 15 по 20 ноября 2026, один взрослый.', 'rail'),
    ('Найди автобус из Москвы в Казань с 15 по 20 ноября 2026, один взрослый.', 'bus'),
])
def test_explicit_transport_service_does_not_add_unsolicited_hotel(text, kind):
    from travel_agent.interpretation import apply_capability_guard
    decision = {'intent': 'plan', 'patch': {'products': [kind, 'hotel'], 'destination': 'Сочи'}}
    guarded = apply_capability_guard(text, {}, decision)
    assert guarded['patch']['products'] == [kind]


@pytest.mark.parametrize('text', ['Нужны авиабилеты и отель в Сочи.', 'Хочу полететь в Сочи.'])
def test_normal_trip_or_explicit_hotel_keeps_lodging(text):
    from travel_agent.interpretation import apply_capability_guard
    decision = {'intent': 'plan', 'patch': {'products': ['avia', 'hotel'], 'destination': 'Сочи'}}
    assert apply_capability_guard(text, {}, decision)['patch']['products'] == ['avia', 'hotel']


def test_flight_replacement_in_existing_trip_does_not_remove_hotel():
    from travel_agent.interpretation import apply_capability_guard
    decision = {'intent': 'replan', 'patch': {}}
    assert apply_capability_guard('Нужны авиабилеты подешевле.', REQUEST, decision) == decision


@pytest.mark.parametrize('kind', ['rail', 'bus'])
def test_unsupported_children_question_never_suggests_another_unsupported_transport(kind):
    from travel_agent.interpretation import apply_capability_guard
    decision = {'intent': 'plan', 'patch': {'products': [kind], 'children_ages': [8]},
                'question': 'Сейчас подбор поездов доступен только для взрослых без детей. Рассмотреть автобус или самолёт?'}
    guarded = apply_capability_guard('Нужен поезд с ребёнком 8 лет.', {}, decision)
    assert 'автобус или' not in guarded['question']
    assert 'перелёт' in guarded['question']
    assert guarded['patch']['children_ages'] == [8]


@pytest.mark.parametrize('text', ['Москва — Казань, ноль взрослых, один ребёнок 7 лет.',
                                 'Нужен отель в Сочи, 0 взрослых и ребёнок 8 лет.'])
async def test_zero_adults_gets_specific_clarification_before_model(tmp_path, text):
    gateway = ScriptedGateway(REQUEST)
    from unittest.mock import AsyncMock
    gateway.interpret = AsyncMock(side_effect=AssertionError('Invalid party must be caught before model'))
    service = AgentService(db_path=tmp_path/'zero-adults.sqlite', gateway=gateway, checkout_mode='disabled')
    try:
        result = await service.run_turn(turn(text=text))
        assert result.status == 'needs_input' and result.error is None
        assert 'без взрослых' in result.message
        gateway.interpret.assert_not_called()
    finally:
        service.close()


@pytest.mark.parametrize('text,nightly,scope,total', [
    ('Нужен отель в Казани с 15 по 20 ноября 2026, двое взрослых, до 8000 рублей за ночь.', 800000, 'trip', 4000000),
    ('Нужен отель в Казани с 15 по 20 ноября 2026, двое взрослых, до 8 тысяч за ночь.', 800000, 'trip', 4000000),
    ('Нужен отель в Казани с 15 по 20 ноября 2026, двое взрослых, до 8000 рублей за ночь на человека.', 800000, 'per_person', 4000000),
])
async def test_nightly_budget_is_for_nights_not_guests_and_recalculates_dates(tmp_path, text, nightly, scope, total):
    initial = {**REQUEST, 'products': ['hotel'], 'departure_date': '2026-11-15',
               'return_date': '2026-11-20', 'adults': 2, 'budget_minor': 800000, 'budget_scope': 'per_person'}
    gateway = ScriptedGateway(REQUEST, {text: {'intent': 'plan', 'patch': initial},
        'На два дня дольше': {'intent': 'replan', 'patch': {'return_date': '2026-11-22'}},
        'Общий бюджет теперь 60000 рублей на всё проживание': {'intent': 'replan', 'patch': {'budget_minor': 6000000, 'budget_scope': 'trip'}}})
    service = AgentService(db_path=tmp_path/'nightly.sqlite', gateway=gateway, adapter=ComponentAdapter(), checkout_mode='disabled')
    try:
        result = await service.run_turn(turn(text=text))
        saved = await service.get_session('component-user', 'trip')
        assert saved.request.nightly_budget_minor == nightly
        assert saved.request.budget_minor == total and saved.request.budget_scope == scope
        result = await service.run_turn(turn(result.state_version, 'На два дня дольше'))
        saved = await service.get_session('component-user', 'trip')
        assert saved.request.budget_minor == nightly * 7
        result = await service.run_turn(turn(result.state_version, 'Общий бюджет теперь 60000 рублей на всё проживание'))
        saved = await service.get_session('component-user', 'trip')
        assert saved.request.budget_minor == 6000000 and saved.request.nightly_budget_minor is None
    finally:
        service.close()


async def test_zero_budget_clarifies_amount_without_losing_route(tmp_path):
    gateway = ScriptedGateway({**REQUEST, 'budget_minor': 0})
    service = AgentService(db_path=tmp_path/'zero-budget.sqlite', gateway=gateway, adapter=ComponentAdapter(), checkout_mode='disabled')
    try:
        result = await service.run_turn(turn())
        assert result.status == 'needs_input' and result.error is None, result.message
        assert 'больше нуля' in result.message
        saved = await service.get_session('component-user', 'trip')
        assert saved.request.destination == REQUEST['destination']
        assert saved.request.budget_minor is None
    finally:
        service.close()


@pytest.mark.parametrize('text', ['Подбери круиз по Волге на двоих в мае 2027.', 'Нужен круиз по Средиземному морю.'])
def test_cruise_does_not_silently_become_a_bus_trip(text):
    from travel_agent.interpretation import apply_capability_guard
    result = apply_capability_guard(text, {}, {'intent': 'plan', 'patch': {'destination': 'Волга', 'products': ['bus', 'hotel']}})
    assert result['patch'] == {} and 'Круизы я пока не подбираю' in result['question']


@pytest.mark.parametrize('text,departure,returning,bad', [
    ('Нужен отель в Казани с 29 февраля по 3 марта 2027, один взрослый.', None, '2027-03-03', '29.02.2027'),
    ('Нужен отель в Казани с 31 ноября по 5 декабря 2026, один взрослый.', None, '2026-12-05', '31.11.2026'),
    ('Нужен отель в Казани с 25 ноября по 31 ноября 2026, один взрослый.', '2026-11-25', None, '31.11.2026'),
])
async def test_invalid_calendar_day_names_error_and_preserves_independent_date(tmp_path, text, departure, returning, bad):
    patch = {**REQUEST, 'products': ['hotel'], 'departure_date': departure, 'return_date': returning}
    gateway = ScriptedGateway(REQUEST, {text: {'intent': 'plan', 'patch': patch}})
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path/'calendar.sqlite', gateway=gateway, adapter=adapter, checkout_mode='disabled')
    try:
        result = await service.run_turn(turn(text=text))
        assert result.status == 'needs_input' and result.error is None
        assert bad in result.message and 'не существует' in result.message
        assert result.message.count('?') == 1 and not adapter.calls
        saved = await service.get_session('component-user', 'trip')
        assert (saved.request.departure_date.isoformat() if saved.request.departure_date else None) == departure
        assert (saved.request.return_date.isoformat() if saved.request.return_date else None) == returning
    finally:
        service.close()


async def test_child_age_precedes_missing_adult_count_and_survives_another_answer(tmp_path):
    text = 'В Казань с 15 по 20 ноября 2026, со мной ребёнок.'
    patch = {**REQUEST, 'adults': None, 'children_ages': []}
    # Model accidentally omits the child, as in live L031.
    patch.pop('children_ages')
    gateway = ScriptedGateway(REQUEST, {text: {'intent': 'plan', 'patch': patch, 'question': 'Сколько взрослых поедет?'},
        'Двое взрослых': {'intent': 'replan', 'patch': {'adults': 2}},
        'Ребёнку 8 лет': {'intent': 'replan', 'patch': {'children_ages': [8]}}})
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path/'child.sqlite', gateway=gateway, adapter=adapter, checkout_mode='disabled')
    try:
        result = await service.run_turn(turn(text=text))
        assert 'Сколько лет' in result.message and not adapter.calls
        result = await service.run_turn(turn(result.state_version, 'Двое взрослых'))
        assert 'Сколько лет' in result.message and not adapter.calls
        result = await service.run_turn(turn(result.state_version, 'Ребёнку 8 лет'))
        assert adapter.calls[0]['children_ages'] == [8] and adapter.calls[0]['adults'] == 2
        assert not (await service.get_session('component-user', 'trip')).children_pending
    finally:
        service.close()


@pytest.mark.parametrize('question', ['Уточните область бюджета', 'Бюджет на всех или на человека?',
                                    'Указанный бюджет — на всех или на одного человека?'])
async def test_no_budget_scope_question_when_no_amount_was_given(tmp_path, question):
    initial = {**REQUEST, 'budget_minor': None, 'budget_scope': None}
    gateway = ScriptedGateway(REQUEST, {'start': {'intent': 'plan', 'patch': initial, 'question': question}})
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path/'scope.sqlite', gateway=gateway, adapter=adapter, checkout_mode='disabled')
    try:
        result = await service.run_turn(turn())
        assert result.status == 'options_ready' and adapter.calls, result.message
        assert not result.questions
    finally:
        service.close()


def test_legacy_event_artist_is_in_persisted_http_selection_and_model_history(tmp_path):
    import json
    from fastapi.testclient import TestClient
    from travel_agent.web import create_app
    from test_web import submit, turn_body
    from http_helpers import bootstrap
    app = create_app(tmp_path/'event-history.sqlite', model_backend='rules', checkout_mode='disabled')
    with TestClient(app, base_url='http://127.0.0.1') as client:
        bootstrap(client)
        first = submit(client, turn_body(session='event', text='Привет'))['result']
        state = client.get('/api/sessions/event').json()['session']
        state['event_choices'] = [{'event_id': 'legacy-event', 'title': 'Концерт', 'artist': 'Алекс Лим',
                                  'city': 'Калуга', 'date': '2026-11-24', 'country': 'Россия'}]
        state['event_choices_version'] = 1
        import sqlite3
        with sqlite3.connect(tmp_path/'event-history.sqlite') as conn:
            conn.execute('UPDATE sessions SET body=? WHERE user_id=? AND session_id=?',
                         (json.dumps(state), state['user_id'], 'event'))
        submit(client, turn_body(session='event', version=first['state_version'], kind='select_event',
                                payload={'event_id': 'legacy-event', 'event_choices_version': 1}))
        restored = client.get('/api/sessions/event').json()
        expected = 'Выбираю концерт: Калуга, 24.11.2026, Алекс Лим'
        assert expected in [m['text'] for m in restored['messages'] if m['role'] == 'user']
        assert expected in [m['text'] for m in restored['session']['dialogue'] if m['role'] == 'user']


@pytest.mark.parametrize('origin,destination,products,search', [
    ('Москва', 'Москва', ['avia'], False), ('Москвы', 'Москва', ['rail'], False),
    ('москва', 'Москва', ['bus'], False), ('Москва', 'Москва', ['hotel'], True),
])
async def test_identical_endpoints_require_correction_only_for_transport(tmp_path, origin, destination, products, search):
    request = {**REQUEST, 'origin': origin, 'destination': destination, 'products': products}
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path/'same-city.sqlite', gateway=ScriptedGateway(request), adapter=adapter, checkout_mode='disabled')
    try:
        result = await service.run_turn(turn())
        assert bool(adapter.calls) == search
        if not search:
            assert result.status == 'needs_input' and 'совпадают' in result.message
            assert len(result.questions) == 1
    finally:
        service.close()


@pytest.mark.parametrize('code', ['model_timeout', 'model_failed', 'model_invalid_response'])
async def test_model_failure_does_not_claim_unsaved_request_was_saved(tmp_path, code):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from travel_agent.adapters.model_codex import ModelGatewayError
    gateway = SimpleNamespace(interpret=AsyncMock(side_effect=ModelGatewayError(code, 'Private diagnostic')))
    service = AgentService(db_path=tmp_path/'failed-model.sqlite', gateway=gateway, checkout_mode='disabled')
    try:
        result = await service.run_turn(turn(text='Из Москвы в Париж на неделю.'))
        assert result.status == 'error' and result.state_version == 0
        assert 'сохранил' not in result.message and 'Private diagnostic' not in result.message
        assert service.repo.session('component-user', 'trip') is None
    finally:
        service.close()


async def test_calendar_clarification_has_no_unrelated_tariff_preface(tmp_path):
    text = 'Нужен отель в Казани с 31 ноября по 5 декабря 2026, один взрослый.'
    gateway = ScriptedGateway(REQUEST, {text: {'intent': 'plan', 'patch': {
        'destination': 'Казань', 'products': ['hotel'], 'return_date': '2026-12-05', 'adults': 1},
        'question_text': 'Когда заезд?', 'question': 'Когда планируете заезд?'}})
    service = AgentService(db_path=tmp_path/'calendar-preface.sqlite', gateway=gateway, adapter=ComponentAdapter(), checkout_mode='disabled')
    try:
        result = await service.run_turn(turn(text=text))
        assert result.message.startswith('Даты 31.11.2026 не существует')
        assert 'Сохранённых вариантов' not in result.message
    finally:
        service.close()


@pytest.mark.parametrize('text', ['Москва — Казань, туда и обратно 15 ноября 2026.',
                                 'Москва — Казань, туда и обратно в один день 15 ноября 2026.'])
async def test_same_day_limitation_survives_model_dropping_return_date(tmp_path, text):
    gateway = ScriptedGateway(REQUEST, {text: {'intent': 'plan', 'patch': {'origin': 'Москва', 'destination': 'Казань',
        'departure_date': '2026-11-15'}, 'question': 'Сколько взрослых едет?'}})
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path/'same-day.sqlite', gateway=gateway, adapter=adapter, checkout_mode='disabled')
    try:
        result = await service.run_turn(turn(text=text))
        assert result.status == 'needs_input' and 'следующего дня' in result.message
        assert 'взросл' not in result.message and not adapter.calls
    finally:
        service.close()


def test_multiday_roundtrip_is_not_mistaken_for_same_day():
    from travel_agent.domain.input_validation import same_day_trip_requested
    assert not same_day_trip_requested('Москва — Казань, туда и обратно с 15 по 20 ноября 2026.')
    assert not same_day_trip_requested('Москва — Казань, туда и обратно 15 ноября, назад через неделю.')


@pytest.mark.parametrize('phrase', ['по 40 тысяч рублей на человека', 'до 40 тыс. рублей на человека',
                                   'бюджет 40 000 рублей на человека'])
async def test_person_budget_is_grounded_before_multiplying_guest_count(tmp_path, phrase):
    text = 'Москва — Казань с 15 по 20 ноября 2026, двое взрослых, ' + phrase + '.'
    bad_model = {**REQUEST, 'adults': 2, 'budget_minor': 8000000, 'budget_scope': 'per_person'}
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path/'per-person.sqlite', gateway=ScriptedGateway(REQUEST,
        {text: {'intent': 'plan', 'patch': bad_model}}), adapter=adapter, checkout_mode='disabled')
    try:
        result = await service.run_turn(turn(text=text))
        saved = await service.get_session('component-user', 'trip')
        assert saved.request.budget_minor == 4000000
        assert saved.request.budget_scope == 'per_person'
        from travel_agent.domain.planning import check_components
        checks = check_components([*result.transport_options[:1], *result.hotel_options[:1]], saved.request.model_dump(mode='json'))
        budget = next(check for check in checks if check['constraint_id'] == 'budget_known_components')
        assert budget['required'] == 8000000
    finally:
        service.close()


@pytest.mark.parametrize('text,expected', [
    ('До 20 ноября хочу вернуться, бюджет 60 тысяч рублей на всех.', (6000000, 'trip')),
    ('Билеты 25 тысяч, отель 30 тысяч, общий бюджет 60 тысяч.', (6000000, 'trip')),
    ('Увеличь бюджет на 10000 рублей.', None),
    ('Москва — Казань, 15–20 ноября, двое взрослых.', None),
])
def test_budget_grounding_does_not_read_dates_or_deltas_as_absolute_amount(text, expected):
    from travel_agent.interpretation import explicit_budget
    assert explicit_budget(text) == expected


@pytest.mark.parametrize('requirement,keys', [
    ('Обязательно доступный вход без ступенек для человека на коляске.', ['wheelchair_accessible']),
    ('Обязательно тихий номер подальше от лифта.', ['quiet_room', 'away_from_elevator']),
])
async def test_mandatory_unverified_hotel_requirement_survives_model_omission_and_followup(tmp_path, requirement, keys):
    text = 'Нужен только отель в Казани с 15 по 20 ноября 2026, двое взрослых. ' + requirement
    request = {**REQUEST, 'products': ['hotel'], 'adults': 2}
    gateway = ScriptedGateway(REQUEST, {text: {'intent': 'plan', 'patch': request},
        'Ищи подешевле': {'intent': 'replan', 'patch': {'constraints': {'prefer_cheaper': True}}}})
    service = AgentService(db_path=tmp_path/'required.sqlite', gateway=gateway, adapter=ComponentAdapter(), checkout_mode='disabled')
    try:
        result = await service.run_turn(turn(text=text))
        for _ in range(2):
            saved = await service.get_session('component-user', 'trip')
            assert all(saved.request.constraints.get(key) is True for key in keys)
            assert result.status == 'partial'
            assert not result.options and not result.hotel_options and not result.selected_components
            assert 'нет подтверждённых данных' in result.message or 'не подтвердил' in result.message
            assert not any(key in result.message for key in keys)
            result = await service.run_turn(turn(result.state_version, 'Ищи подешевле'))
    finally:
        service.close()


def test_model_schema_can_preserve_the_three_mandatory_hotel_conditions():
    from travel_agent.adapters.model_codex import _constraint_schema
    import jsonschema
    from copy import deepcopy
    values = {key: None for key in _constraint_schema['properties']}
    values.update(wheelchair_accessible=True, quiet_room=True, away_from_elevator=True)
    jsonschema.validate(values, _constraint_schema)


def test_soft_quiet_preference_does_not_become_hard_and_explicit_removal_is_respected():
    from travel_agent.interpretation import apply_request_scope_guard
    result = apply_request_scope_guard('Нужен отель, желательно тихий номер.', {}, {'intent': 'plan', 'patch': {}})
    assert not result['patch'].get('constraints', {}).get('quiet_room')
    removed = apply_request_scope_guard('Тихий номер не обязателен.', REQUEST, {'intent': 'replan', 'patch': {}})
    assert removed['patch']['constraints']['quiet_room'] is False


async def test_already_mandatory_accessibility_does_not_need_duplicate_permission(tmp_path):
    text = 'Нужен только отель в Казани с 15 по 20 ноября 2026, двое взрослых. Обязательно доступный вход без ступенек.'
    gateway = ScriptedGateway(REQUEST, {text: {'intent': 'plan', 'patch': {**REQUEST, 'products': ['hotel']},
        'question': 'Доступный вход — обязательное условие. Продолжить подбор с этим требованием?'}})
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path/'access.sqlite', gateway=gateway, adapter=adapter, checkout_mode='disabled')
    try:
        result = await service.run_turn(turn(text=text))
        assert adapter.calls and result.status == 'partial'
        assert 'Продолжить подбор' not in result.message and not result.hotel_options
    finally:
        service.close()


def test_unconfirmed_mandatory_room_details_do_not_ask_to_drop_them():
    from travel_agent.domain.search_diagnostics import format_search_diagnostics
    result = format_search_diagnostics({'code': 'conditions_unmet', 'requested_products': ['hotel'],
        'reasons': ['Тихий номер: нет подтверждённых данных; требуется да',
                    'Номер вдали от лифта: нет подтверждённых данных; требуется да']})
    assert 'не подтвердил обязательные условия' in result['message']
    assert 'тихий номер' in result['message'] and 'вдали от лифта' in result['message']
    assert result['question'] is None and 'Какое условие готовы изменить' not in result['message']


def test_direct_question_about_unconfirmed_room_details_is_specific():
    answer = AgentService._answer_question(detailed_state(), 'Подтверждено ли, что номер тихий и не рядом с лифтом?')
    assert 'Тихий номер: не указано' in answer and 'Номер вдали от лифта: не указано' in answer
    assert '₽' not in answer and 'Завтрак' not in answer


async def test_bus_pet_limitation_does_not_invent_a_child_party(tmp_path):
    text = 'Нужен автобус из Москвы в Казань с собакой.'
    gateway = ScriptedGateway(REQUEST, {text: {'intent': 'plan', 'patch': {'origin': 'Москва', 'destination': 'Казань',
        'products': ['bus'], 'pets': True},
        'question': 'Автобусы доступны только для взрослых без детей. Для поездки с собакой рассмотреть другой транспорт?'}})
    service = AgentService(db_path=tmp_path/'bus-pet.sqlite', gateway=gateway, adapter=ComponentAdapter(), checkout_mode='disabled')
    try:
        result = await service.run_turn(turn(text=text))
        assert result.status == 'needs_input' and 'питомца в автобусе' in result.message
        assert 'детей' not in result.message and 'взросл' not in result.message
        saved = await service.get_session('component-user', 'trip')
        assert saved.request.pets and saved.request.products == ['bus']
    finally:
        service.close()


@pytest.mark.parametrize('budget', [5000000, None])
@pytest.mark.parametrize('question', ['Какой у вас лимит по бюджету?', 'Какой у вас бюджет?', 'Какой бюджет на поездку?'])
async def test_complete_request_does_not_reask_known_or_optional_budget(tmp_path, budget, question):
    request = {**REQUEST, 'budget_minor': budget, 'budget_scope': 'trip' if budget else None}
    gateway = ScriptedGateway(REQUEST, {'start': {'intent': 'plan', 'patch': request, 'question': question}})
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path/'known-budget.sqlite', gateway=gateway, adapter=adapter, checkout_mode='disabled')
    try:
        result = await service.run_turn(turn())
        assert result.status == 'options_ready' and adapter.calls and not result.questions
    finally:
        service.close()


def test_negative_budget_sign_is_not_swallowed_as_punctuation():
    from travel_agent.interpretation import explicit_budget, nightly_budget
    assert explicit_budget('Бюджет -5000 рублей') == (-500000, None)
    assert explicit_budget('Бюджет: 5000 рублей') == (500000, None)
    assert explicit_budget('Бюджет — 5000 рублей') == (500000, None)
    assert nightly_budget('Бюджет -5000 рублей за ночь') == (-500000, 'trip')


async def test_negative_budget_is_clarified_before_model_and_not_made_positive(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    gateway = SimpleNamespace(interpret=AsyncMock(side_effect=AssertionError('No model required for an explicit invalid amount')))
    service = AgentService(db_path=tmp_path/'negative-budget.sqlite', gateway=gateway, checkout_mode='disabled')
    try:
        result = await service.run_turn(turn(text='Бюджет -5000 рублей'))
        assert result.status == 'needs_input' and result.error is None
        assert 'больше нуля' in result.message
        assert (await service.get_session('component-user', 'trip')).request.budget_minor is None
        gateway.interpret.assert_not_called()
    finally:
        service.close()


async def test_ticket_only_scope_survives_oneway_clarification_and_acceptance(tmp_path):
    first_text = 'Нужен билет из Москвы в Казань в одну сторону 15 ноября 2026, один взрослый, до 20 тысяч.'
    yes = 'Да, подойдёт туда и обратно.'
    back = 'Обратно 20 ноября 2026.'
    gateway = ScriptedGateway(REQUEST, {
        first_text: {'intent': 'plan', 'patch': {}, 'question': 'Туда и обратно подойдёт?'},
        yes: {'intent': 'plan', 'patch': {'origin': 'Москва', 'destination': 'Казань', 'departure_date': '2026-11-15',
            'adults': 1, 'budget_minor': 2000000, 'budget_scope': None, 'products': ['avia', 'hotel']},
              'question': 'Когда планируете вернуться?'},
        back: {'intent': 'replan', 'patch': {'return_date': '2026-11-20'}},
    })
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path/'ticket-scope.sqlite', gateway=gateway, adapter=adapter, checkout_mode='disabled')
    try:
        first = await service.run_turn(turn(text=first_text))
        assert 'только туда и обратно' in first.message
        second = await service.run_turn(turn(first.state_version, yes))
        assert (await service.get_session('component-user', 'trip')).request.products == ['avia']
        third = await service.run_turn(turn(second.state_version, back))
        assert adapter.calls[0]['products'] == ['avia'] and not third.hotel_options
        assert third.status == 'options_ready'
    finally:
        service.close()


def test_historical_ticket_scope_does_not_remove_existing_plan_hotel_or_new_explicit_hotel():
    from travel_agent.interpretation import apply_request_scope_guard
    context = {'_dialogue': {'last_question': 'Сейчас могу подобрать транспорт только туда и обратно. Такой вариант подойдёт?',
        'history': [{'role': 'user', 'text': 'Нужен билет в одну сторону.'}]}}
    decision = {'intent': 'replan', 'patch': {'products': ['avia', 'hotel']}}
    assert apply_request_scope_guard('Да, и нужен отель.', context, decision)['patch']['products'] == ['avia', 'hotel']
    assert apply_request_scope_guard('Да, подойдёт.', {**REQUEST, **context}, decision)['patch']['products'] == ['avia', 'hotel']


async def test_zero_adults_preserves_route_dates_and_known_child_age_for_correction(tmp_path):
    text = 'Москва — Казань, 15–20 ноября 2026, ноль взрослых, один ребёнок 7 лет.'
    gateway = ScriptedGateway(REQUEST, {'Поедет один взрослый.': {'intent': 'replan', 'patch': {'adults': 1}}})
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path/'correct-adults.sqlite', gateway=gateway, adapter=adapter, checkout_mode='disabled')
    try:
        result = await service.run_turn(turn(text=text))
        saved = await service.get_session('component-user', 'trip')
        assert saved.request.origin == 'Москва' and saved.request.destination == 'Казань'
        assert str(saved.request.departure_date) == '2026-11-15' and str(saved.request.return_date) == '2026-11-20'
        assert saved.request.children_ages == [7] and saved.request.adults is None
        result = await service.run_turn(turn(result.state_version, 'Поедет один взрослый.'))
        assert result.status == 'options_ready' and adapter.calls[0]['children_ages'] == [7]
        assert 'Сколько лет' not in result.message
    finally:
        service.close()


def test_service_availability_word_does_not_mean_steps_or_wheelchair_access():
    from types import SimpleNamespace
    empty = SimpleNamespace(selected_components=[], current_plan=None, transport_options=[], hotel_options=[])
    answer = AgentService._answer_question(empty, 'Доступен ли поезд для одного взрослого и ребёнка?')
    assert 'коляск' not in answer and 'вход' not in answer
    actual = AgentService._answer_question(empty, 'Доступен ли вход без ступенек для человека на коляске?')
    assert 'коляск' in actual


@pytest.mark.parametrize('currency', ['долларов', 'евро', 'USD', 'EUR'])
async def test_foreign_budget_guard_does_not_save_same_number_as_rubles(tmp_path, currency):
    text = 'Нужен отель в Казани, бюджет 1000 ' + currency + '.'
    gateway = ScriptedGateway(REQUEST, {text: {'intent': 'plan', 'patch': {}, 'question': 'Какую сумму в рублях использовать?'}})
    service = AgentService(db_path=tmp_path/'currency.sqlite', gateway=gateway, checkout_mode='disabled')
    try:
        result = await service.run_turn(turn(text=text))
        assert result.status == 'needs_input' and 'рублях' in result.message
        assert (await service.get_session('component-user', 'trip')).request.budget_minor is None
    finally:
        service.close()


@pytest.mark.parametrize('question', ['Какого числа вы хотите заехать в отель?', 'В какой день планируете заселиться?',
                                    'Какого числа вы хотите выехать из отеля?'])
async def test_known_hotel_dates_are_not_reasked_with_natural_date_synonyms(tmp_path, question):
    request = {**REQUEST, 'products': ['hotel']}
    gateway = ScriptedGateway(REQUEST, {'start': {'intent': 'plan', 'patch': request, 'question': question}})
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path/'date-synonyms.sqlite', gateway=gateway, adapter=adapter, checkout_mode='disabled')
    try:
        result = await service.run_turn(turn())
        assert adapter.calls and result.status == 'options_ready', result.message
        assert not result.questions
    finally:
        service.close()


def test_known_dates_do_not_suppress_a_question_with_real_alternatives():
    from travel_agent.domain.clarifications import answered_slot_question
    assert not answered_slot_question('Какого числа хотите заехать: 15 или 16 ноября?', REQUEST)


@pytest.mark.parametrize('adults,children,expected_search', [(1, [], True), (1, [7], False), (2, [], False)])
async def test_budget_scope_is_not_a_question_for_one_known_traveller(tmp_path, adults, children, expected_search):
    request = {**REQUEST, 'adults': adults, 'children_ages': children, 'budget_scope': None}
    gateway = ScriptedGateway(REQUEST, {'start': {'intent': 'plan', 'patch': request,
        'question': 'Указанный бюджет — на всех или на одного человека?'}})
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path/'one-person-budget.sqlite', gateway=gateway, adapter=adapter, checkout_mode='disabled')
    try:
        result = await service.run_turn(turn())
        assert bool(adapter.calls) == expected_search
        if expected_search:
            assert result.status == 'options_ready' and not result.questions
            assert (await service.get_session('component-user', 'trip')).request.budget_scope == 'trip'
        else:
            assert result.status == 'needs_input' and 'на всех' in result.message
    finally:
        service.close()


@pytest.mark.parametrize('question', ['Ты проверил, что номера тихие и не рядом с лифтом?',
                                    'Подтверждены ли тихие номера подальше от лифта?'])
def test_answer_covers_both_quiet_rooms_and_distance_from_elevator(question):
    from types import SimpleNamespace
    snapshot = SimpleNamespace(selected_components=[], current_plan=None, transport_options=[], hotel_options=[])
    answer = AgentService._answer_question(snapshot, question)
    assert 'тихий номер' in answer and 'вдали от лифта' in answer
    assert answer.startswith('Я пока не подтвердил') and len(answer) < 180


@pytest.mark.parametrize('complete', [False, True])
async def test_flexible_calendar_choice_is_announced_before_next_step(tmp_path, complete):
    text = 'Хочу в Сочи в ноябре, даты гибкие, дней на пять.'
    request = {**REQUEST, 'destination': 'Сочи', 'departure_date': '2026-11-01', 'return_date': '2026-11-06'}
    if not complete:
        request.update(origin=None, adults=None, budget_minor=None, budget_scope=None)
    gateway = ScriptedGateway(REQUEST, {text: {'intent': 'plan', 'patch': request}})
    service = AgentService(db_path=tmp_path/'flexible-dates.sqlite', gateway=gateway,
                           adapter=ComponentAdapter(), checkout_mode='disabled')
    try:
        result = await service.run_turn(turn(text=text))
        assert result.message.startswith('Для начала беру даты 01.11.2026–06.11.2026. ')
        assert result.status == ('options_ready' if complete else 'needs_input')
        if not complete:
            assert result.questions == ['Из какого города поедете?']
        assert (await service.get_session('component-user', 'trip')).dialogue[-1]['text'] == result.message
    finally:
        service.close()


@pytest.mark.parametrize('invented_dates', [{}, {'departure_date': '2026-11-01', 'return_date': '2026-11-06'}])
async def test_english_indefinite_weekend_keeps_route_and_asks_which_weekend(tmp_path, invented_dates):
    text = 'I want to visit Kazan from Moscow for a weekend.'
    gateway = ScriptedGateway(REQUEST, {text: {'intent': 'plan', 'patch': {'origin': 'Москва',
        'destination': 'Казань', **invented_dates}, 'question': 'Когда хотите вылететь?'}})
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path/'weekend.sqlite', gateway=gateway, adapter=adapter, checkout_mode='disabled')
    try:
        result = await service.run_turn(turn(text=text))
        assert result.message == 'Какие выходные выбрать: ближайшие или другие?'
        saved = (await service.get_session('component-user', 'trip')).request
        assert saved.origin == 'Москва' and saved.destination == 'Казань'
        assert saved.departure_date is None and saved.return_date is None and not adapter.calls
    finally:
        service.close()


@pytest.mark.parametrize('text', ['I want to visit Kazan for a weekend, November 14–15, 2026.',
                                 'I want to visit Kazan for the nearest weekend.'])
def test_weekend_guard_does_not_replace_a_known_period(text):
    from travel_agent.interpretation import apply_request_scope_guard
    decision = {'intent': 'plan', 'patch': {'destination': 'Казань', 'departure_date': '2026-11-14',
                                          'return_date': '2026-11-15'}}
    assert apply_request_scope_guard(text, {}, decision) == decision


@pytest.mark.parametrize('existing', [False, True])
async def test_pure_weather_question_cannot_mutate_trip_from_model_patch(tmp_path, existing):
    from travel_agent.domain.models import TripRequest
    text = 'Какая погода сейчас в Казани?'
    gateway = ScriptedGateway(REQUEST, {text: {'intent': 'web_search', 'web_query': 'Погода в Казани сейчас',
        'patch': {**REQUEST, 'destination': 'Казань', 'adults': 3, 'budget_minor': 1000000}, 'reset_dates': True}})
    class Search:
        async def search(self, query, now):
            return {'provider': 'yandex', 'query': query, 'fetched_at': now, 'results': [], 'error': None}
    adapter = ComponentAdapter()
    service = AgentService(db_path=tmp_path/'weather-read-only.sqlite', gateway=gateway, adapter=adapter,
                           web_search_adapter=Search(), checkout_mode='disabled')
    try:
        version = 0
        before = None
        if existing:
            initial = await service.run_turn(turn())
            selected = await select(service, initial, initial.transport_options[0])
            version = selected.state_version
            before = await service.get_session('component-user', 'trip')
        travel_calls = len(adapter.calls)
        result = await service.run_turn(turn(version, text))
        after = await service.get_session('component-user', 'trip')
        assert result.error is None and result.web_search['query'] == 'Погода в Казани сейчас'
        assert after.request == (before.request if before else TripRequest())
        if before:
            for field in ('current_plan', 'plan_history', 'transport_options', 'hotel_options',
                          'selected_components', 'pending_action', 'search_diagnostics'):
                assert getattr(after, field) == getattr(before, field), field
        assert len(adapter.calls) == travel_calls
    finally:
        service.close()


@pytest.mark.parametrize('text', ['Поедем в Казань, и узнай, какая там погода сейчас.',
                                 'Нужен отель в Казани. Какая там погода?',
                                 'Какая погода в Казани? Поменяй направление поездки на Казань.'])
async def test_mixed_weather_and_explicit_trip_change_keeps_requested_patch(tmp_path, text):
    gateway = ScriptedGateway(REQUEST, {text: {'intent': 'web_search', 'web_query': 'Погода в Казани сейчас',
                                           'patch': {'destination': 'Казань'}}})
    class Search:
        async def search(self, query, now):
            return {'provider': 'yandex', 'query': query, 'fetched_at': now, 'results': [], 'error': None}
    service = AgentService(db_path=tmp_path/'weather-mixed.sqlite', gateway=gateway,
                           web_search_adapter=Search(), checkout_mode='disabled')
    try:
        result = await service.run_turn(turn(text=text))
        assert result.error is None
        assert (await service.get_session('component-user', 'trip')).request.destination == 'Казань'
    finally:
        service.close()

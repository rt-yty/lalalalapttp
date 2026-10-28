"""Regressions for useful, send-ready next-message proposals."""
from copy import deepcopy

import pytest

from travel_agent.domain.models import SessionSnapshot
from travel_agent.graph.suggestions import build_suggestions, suggest_next_messages


def session(**kwargs):
    return SessionSnapshot(user_id="u", session_id="s", data_mode="replay", checkout_mode="demo", **kwargs)


def concert_state():
    saved = session(request={"adults": 1}).model_dump(mode="json")
    saved["dialogue"] = [
        {"role": "user", "text": "хочу на концерт моргена"},
        {"role": "assistant", "text": "Какие города и даты подходят, откуда поедете и сколько взрослых?"},
    ]
    return {"session": saved, "turn": {"input": "message", "payload": {"text": "я один город и дата любые"}, "now": "2026-10-27T12:00:00+03:00"},
            "result": {"status": "needs_input", "message": "Из какого города поедете?", "questions": ["Из какого города поедете?"]}}


def test_origin_reply_chips_do_not_enumerate_other_missing_fields():
    state = concert_state()
    hints = build_suggestions(SessionSnapshot.model_validate(state["session"]), state["result"], state["turn"]["payload"]["text"])
    assert [hint["label"] for hint in hints] == ["Из Москвы", "Из Петербурга", "Из Казани"]
    assert all("…" not in hint["text"] and "дат" not in hint["text"].lower() for hint in hints)
    assert all("из " in hint["text"].lower() for hint in hints)


async def test_model_receives_flexible_preferences_latest_question_and_current_user():
    class Gateway:
        context = None
        async def suggest(self, context):
            self.context = context
            return [{"id": "from-msk", "label": "Из Москвы", "text": "Поеду из Москвы."}]
    gateway = Gateway()
    state = concert_state()
    before = deepcopy(state)
    result = await suggest_next_messages(state, gateway=gateway)
    assert result["result"]["suggestions"][0]["text"] == "Поеду из Москвы."
    assert gateway.context["latest_assistant_question"] == "Из какого города поедете?"
    assert gateway.context["current_user_message"] == "я один город и дата любые"
    assert "хочу на концерт моргена" in str(gateway.context["recent_dialogue"])
    assert "город и дата любые" in str(gateway.context["recent_dialogue"])
    assert gateway.context["request"]["adults"] == 1
    assert state == before


@pytest.mark.parametrize("reply", [
    [{"id": "dates", "label": "Указать даты", "text": "Даты поездки: с … по …"}],
    [{"id": "destination", "label": "В Ереван", "text": "Хочу поехать в Ереван."}],
    [{"id": "purchase", "label": "Подтвердить", "text": "Подтверждаю покупку."}],
    {"unexpected": "format"},
])
async def test_templates_irrelevant_replies_and_actions_fall_back_to_actual_question(reply):
    class Gateway:
        async def suggest(self, context):
            return reply
    result = await suggest_next_messages(concert_state(), gateway=Gateway())
    assert [item["label"] for item in result["result"]["suggestions"]] == ["Из Москвы", "Из Петербурга", "Из Казани"]


async def test_duplicate_model_chips_are_removed():
    class Gateway:
        async def suggest(self, context):
            return [{"id": "a", "label": " Из Москвы ", "text": " Поеду из Москвы. "},
                    {"id": "b", "label": "Из Москвы", "text": "Поеду из Москвы."},
                    {"id": "a", "label": "Из Казани", "text": "Поеду из Казани."}]
    result = await suggest_next_messages(concert_state(), gateway=Gateway())
    assert result["result"]["suggestions"] == [{"id": "a", "label": "Из Москвы", "text": "Поеду из Москвы."}]


async def test_suggestion_failure_leaves_successful_answer_and_state_intact():
    class Gateway:
        async def suggest(self, context):
            raise RuntimeError("provider unavailable")
    state = concert_state()
    before = deepcopy(state)
    result = await suggest_next_messages(state, gateway=Gateway())
    assert result["result"]["message"] == state["result"]["message"]
    assert len(result["result"]["suggestions"]) == 3
    assert state == before


async def test_zero_remaining_timeout_skips_optional_model():
    class Gateway:
        async def suggest(self, context):
            pytest.fail("Optional model cannot run after the main turn's budget")
    result = await suggest_next_messages(concert_state(), gateway=Gateway(), timeout=0)
    assert len(result["result"]["suggestions"]) == 3


def test_flexible_concert_dates_never_invent_event_schedule():
    state = concert_state()
    saved = SessionSnapshot.model_validate(state["session"])
    result = {"status": "needs_input", "message": "Какие даты поездки подходят?"}
    hints = build_suggestions(saved, result, "хочу на концерт, даты любые")
    assert hints == []  # No event lookup capability and flexibility was already stated.


async def test_context_does_not_include_action_tokens_or_unknown_constraint_fields():
    class Gateway:
        async def suggest(self, context):
            rendered = str(context)
            assert "SECRET_ACTION" not in rendered
            assert "SECRET_KEY" not in rendered
            assert "payload_hash" not in rendered
            assert len(context["request"]["origin"]) <= 120
            return []
    state = concert_state()
    state["session"]["request"]["origin"] = "Москва" * 1000
    state["session"]["request"]["constraints"] = {"api_key": "SECRET_KEY", "breakfast": True}
    state["session"]["pending_action"] = {"action_id": "SECRET_ACTION", "payload_hash": "SECRET_HASH"}
    result = await suggest_next_messages(state, gateway=Gateway())
    assert result["result"]["suggestions"] == []


@pytest.mark.parametrize("action", ["Забронировать номер", "Подтверди заказ", "Оформить поездку", "Да"])
async def test_booking_and_confirmation_variants_never_become_suggestions(action):
    class Gateway:
        async def suggest(self, context):
            return [{"id": "action", "label": action, "text": action}]
    state = concert_state()
    state["result"] = {"status": "needs_confirmation", "message": "Проверьте выбранный состав."}
    result = await suggest_next_messages(state, gateway=Gateway())
    assert all(item["text"] != action for item in result["result"]["suggestions"])


def test_flexible_concert_city_is_not_replaced_by_arbitrary_destination():
    state = concert_state()
    hints = build_suggestions(SessionSnapshot.model_validate(state["session"]),
                              {"status": "needs_input", "message": "Какой город вам подходит?"},
                              "город и дата любые")
    assert hints == []


def test_missing_fixture_does_not_suggest_unsupported_coverage_question():
    hints = build_suggestions(session(), {"status": "partial", "error": {"code": "fixture_missing"}, "message": "Нет записи."})
    assert hints == []


def diagnosed_state(code='missing_flights', *, retryable=True):
    saved = session(request={'origin': 'Москва', 'destination': 'Хайфа', 'departure_date': '2026-10-28',
                             'return_date': '2026-10-31', 'adults': 1})
    state = {'session': saved.model_dump(mode='json'),
             'turn': {'input': 'message', 'payload': {'text': 'Подбери поездку на выбранный концерт.'}},
             'result': {'status': 'partial', 'message': 'Поставщик не вернул перелётов для выбранного маршрута.',
                        'search_diagnostics': {'code': code, 'counts': {'flights': 0, 'hotel_rates': 3, 'hotels': 2},
                                               'error': {'code': 'provider_timeout', 'retryable': retryable}}}}
    state['session']['dialogue'] = [{'role': 'user', 'text': 'Хочу на концерт 28 октября в Хайфе, еду один.'}]
    return state


@pytest.mark.parametrize('code,retryable,expected_count', [
    ('provider_error', False, 0), ('provider_error', True, 1),
    ('missing_flights', True, 1), ('missing_hotels', True, 1),
    ('conditions_unmet', True, 0), ('no_priced_pairs', True, 1),
])
def test_diagnosed_empty_search_offers_another_city_and_only_retries_transient_errors(code, retryable, expected_count):
    state = diagnosed_state(code, retryable=retryable)
    saved = SessionSnapshot.model_validate(state['session'])
    before = saved.model_dump()
    hints = build_suggestions(saved, state['result'], state['turn']['payload']['text'])
    assert len(hints) == expected_count
    if code == 'provider_error':
        assert all(hint['text'] == 'Повтори поиск с теми же условиями поездки.' for hint in hints)
    else:
        assert all(hint['text'] == 'Давай посмотрим концерты в других городах.' for hint in hints)
    assert saved.model_dump() == before


def test_an_already_requested_retry_does_not_offer_another_blind_retry():
    state = diagnosed_state()
    hints = build_suggestions(SessionSnapshot.model_validate(state['session']), state['result'],
                              'Попробуй ещё раз с теми же условиями.')
    assert [hint['id'] for hint in hints] == ['other-concert-city']


@pytest.mark.parametrize('bad_reply', [
    'Почему не нашлись варианты?',
    'Узнать причину отсутствия перелётов.',
    'Что именно мешает подобрать поездку?',
    'Какое условие мешает?',
    'Какое из моих условий мешает подобрать поездку?',
    'Сдвинем даты поездки.',
    'Прилетим на день раньше.',
    'Прилетим 27 октября.',
    'Полетим через Тель-Авив.',
    'Поехали в Казань.',
    'Давай в Тель-Авив.',
    'Поищи соседний аэропорт.',
    'Увеличим бюджет до 100 тысяч рублей.',
    'Можно с пересадкой.',
])
async def test_model_cannot_turn_an_explained_missing_flight_into_a_loop_or_unverified_workaround(bad_reply):
    class Gateway:
        async def suggest(self, context):
            return [{'id': 'bad', 'label': bad_reply, 'text': bad_reply},
                    {'id': 'retry', 'label': 'Повторить с теми же условиями',
                     'text': 'Повтори поиск с теми же условиями поездки.'}]
    state = diagnosed_state()
    before = deepcopy(state)
    response = await suggest_next_messages(state, gateway=Gateway())
    assert [hint['id'] for hint in response['result']['suggestions']] == ['other-concert-city']
    assert state == before


async def test_nonretryable_provider_error_cannot_gain_model_retry_chip():
    class Gateway:
        async def suggest(self, context):
            return [{'id': 'retry', 'label': 'Попробовать ещё раз', 'text': 'Повтори поиск с теми же условиями.'}]
    response = await suggest_next_messages(diagnosed_state('provider_error', retryable=False), gateway=Gateway())
    assert response['result']['suggestions'] == []


async def test_fixed_concert_dates_remain_fixed_for_a_different_diagnosed_blocker():
    class Gateway:
        async def suggest(self, context):
            return [{'id': 'move', 'label': 'Прилететь на день раньше', 'text': 'Давайте прилетим на день раньше.'}]
    response = await suggest_next_messages(diagnosed_state('conditions_unmet'), gateway=Gateway())
    assert all(hint['id'] != 'move' for hint in response['result']['suggestions'])


async def test_explicit_trip_date_flexibility_is_not_overridden_by_suggestion_guard():
    class Gateway:
        async def suggest(self, context):
            return [{'id': 'move', 'label': 'Прилететь на день раньше', 'text': 'Давайте прилетим на день раньше.'}]
    state = diagnosed_state('conditions_unmet')
    state['turn']['payload']['text'] = 'Даты поездки гибкие, главное успеть на концерт.'
    response = await suggest_next_messages(state, gateway=Gateway())
    assert [hint['id'] for hint in response['result']['suggestions']] == ['move']


async def test_diagnostics_context_is_bounded_and_drops_unrelated_private_fields():
    state = diagnosed_state('provider_error')
    diagnostics = state['result']['search_diagnostics']
    diagnostics.update(query={'origin': 'Москва' * 100, 'api_key': 'PRIVATE_QUERY_VALUE'},
                       reasons=['Причина ' * 100] * 10, api_key='PRIVATE_TOP_VALUE',
                       products={'avia': {'complete': False, 'pages_read': 1, 'private': 'PRIVATE_PRODUCT_VALUE'}})
    diagnostics['error']['private'] = 'PRIVATE_ERROR_VALUE'

    class Gateway:
        async def suggest(self, context):
            current = context['search_diagnostics']
            assert current['code'] == 'provider_error'
            assert current['counts']['flights'] == 0
            assert current['error']['retryable'] is True
            assert len(current['query']['origin']) <= 120
            assert len(current['reasons']) == 3 and max(map(len, current['reasons'])) <= 240
            assert 'PRIVATE_' not in str(context)
            assert 'alternate_airports' in context['unavailable_capabilities']
            return []
    before = deepcopy(state)
    response = await suggest_next_messages(state, gateway=Gateway())
    assert response['result']['suggestions'] == []
    assert state == before


async def test_diagnostics_in_saved_session_also_prevent_repeated_reason_suggestions():
    state = diagnosed_state('provider_error', retryable=False)
    state['session']['search_diagnostics'] = state['result'].pop('search_diagnostics')

    class Gateway:
        async def suggest(self, context):
            assert context['search_diagnostics']['code'] == 'provider_error'
            return [{'id': 'reason', 'label': 'Узнать причину', 'text': 'Почему поиск не завершился?'}]
    response = await suggest_next_messages(state, gateway=Gateway())
    assert response['result']['suggestions'] == []


async def test_explicit_connection_permission_requires_matching_diagnosed_constraint():
    class Gateway:
        async def suggest(self, context):
            return [{'id': 'connections', 'label': 'Можно с пересадкой', 'text': 'Можно с пересадкой, остальные условия сохрани.'}]
    state = diagnosed_state('conditions_unmet')
    state['session']['request']['constraints']['direct'] = True
    state['result']['search_diagnostics']['reasons'] = ['Требуется прямой рейс, найденный тариф предусматривает пересадку.']
    before = deepcopy(state)
    response = await suggest_next_messages(state, gateway=Gateway())
    assert [hint['id'] for hint in response['result']['suggestions']] == ['connections']
    assert state == before


@pytest.mark.parametrize("code", ["missing_flights", "missing_hotels", "missing_both", "no_usable_fares", "no_priced_pairs", "legacy_coverage", "conditions_unmet"])
async def test_valid_empty_result_rejects_model_retry_even_on_first_empty_search(code):
    class Gateway:
        async def suggest(self, context):
            return [{"id": "retry", "label": "Повторить", "text": "Повтори поиск с теми же условиями."}]
    response = await suggest_next_messages(diagnosed_state(code), gateway=Gateway())
    assert all("повтор" not in hint["text"].lower() for hint in response["result"]["suggestions"])


async def test_other_concert_from_saved_dialogue_can_change_city_and_event_date():
    class Gateway:
        async def suggest(self, context):
            return [{"id": "vilnius", "label": "Вильнюс · 31 октября", "text": "Выбираю концерт в Вильнюсе 31 октября."}]
    state = diagnosed_state()
    state["session"]["dialogue"].insert(0, {"role": "assistant", "text": "Ближайшие концерты:\n31 октября 2026 — Вильнюс.\n2 ноября 2026 — Братислава."})
    before = deepcopy(state)
    response = await suggest_next_messages(state, gateway=Gateway())
    assert [hint["id"] for hint in response["result"]["suggestions"]] == ["vilnius"]
    assert state == before


async def test_city_and_date_must_belong_to_the_same_found_event():
    class Gateway:
        async def suggest(self, context):
            return [{"id": "wrong-date", "label": "Братислава · 31 октября", "text": "Выбираю концерт в Братиславе 31 октября."}]
    state = diagnosed_state()
    state["session"]["dialogue"].insert(0, {"role": "assistant", "text": "31 октября 2026 — Вильнюс.\n2 ноября 2026 — Братислава."})
    response = await suggest_next_messages(state, gateway=Gateway())
    assert [hint["id"] for hint in response["result"]["suggestions"]] == ["other-concert-city"]


async def test_known_city_does_not_make_claimed_flight_availability_valid():
    class Gateway:
        async def suggest(self, context):
            return [{"id": "claim", "label": "Вильнюс", "text": "В Вильнюсе есть билеты, поехали туда."}]
    state = diagnosed_state()
    state["session"]["dialogue"].insert(0, {"role": "assistant", "text": "31 октября 2026 — Вильнюс."})
    response = await suggest_next_messages(state, gateway=Gateway())
    assert [hint["id"] for hint in response["result"]["suggestions"]] == ["other-concert-city"]


def test_nonconcert_empty_search_has_other_city_fallback():
    state = diagnosed_state()
    state["session"]["dialogue"] = []
    state["turn"]["payload"]["text"] = "Подбери поездку."
    hints = build_suggestions(SessionSnapshot.model_validate(state["session"]), state["result"], state["turn"]["payload"]["text"])
    assert [hint["text"] for hint in hints] == ["Давай подберём другой город для поездки."]

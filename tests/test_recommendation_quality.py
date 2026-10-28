"""Independent recommendation quality regressions at the public algorithm boundary."""
from datetime import datetime, timedelta, timezone

import pytest

from travel_agent.recommendations import build_recommendations, extract_signals


NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


def signal(topic, context=None, age=0):
    return {"topic_id": topic, "context": context or {},
            "occurred_at": (NOW - timedelta(days=age)).isoformat()}


def feed(rows, popular=None, dismissed=None):
    return build_recommendations(rows, popular or [], {}, NOW, dismissed)


def test_source_city_is_not_an_interest_and_destination_is():
    state = {"request": {"origin": "Москва", "destination": "Япония"}}
    rows = extract_signals(state, "Хочу из Москвы в Японию")
    assert "destination:japan" in {row["topic_id"] for row in rows}
    assert "destination:moscow" not in {row["topic_id"] for row in rows}


def test_rejected_destination_is_not_an_interest():
    state = {"request": {"destination": "Южная Корея"}}
    rows = extract_signals(state, "Не хочу в Японию, лучше в Корею")
    assert "destination:japan" not in {row["topic_id"] for row in rows if not row["context"].get("rejected")}
    assert any(row["topic_id"] == "destination:japan" and row["context"].get("rejected") for row in rows)
    assert "destination:korea" in {row["topic_id"] for row in rows}


@pytest.mark.parametrize("message", ["Спасибо!", "Привет", "Как тебя зовут?", "Сколько будет два плюс два?"])
def test_stale_trip_and_artist_do_not_turn_chat_into_interest(message):
    state = {"request": {"destination": "Япония", "budget_minor": 15000000},
             "concert_intake": {"mode": "trip", "event_name": "Imagine Dragons"},
             "selected_event": {"artist": "Imagine Dragons"}}
    assert extract_signals(state, message) == []


def test_assistant_suggestions_and_unselected_events_do_not_create_interests():
    state = {"request": {},
             "dialogue": [{"role": "assistant", "text": "Предлагаю Японию или Coldplay"}],
             "event_choices": [{"artist": "Coldplay"}, {"artist": "Imagine Dragons"}],
             "last_web_search": {"query": "Япония концерт Imagine Dragons"}}
    assert extract_signals(state, "Хочу в путешествие, пока не знаю куда") == []


def test_japan_lookalike_retains_budget_as_search_cap_and_not_price():
    context = {"budget_minor": 15000000, "budget_scope": "per_person", "adults": 2,
               "origin": "Москва", "products": ["avia", "hotel"], "duration_days": 7}
    items = feed([signal("destination:japan", context)])["personalized"]
    korea = next(item for item in items if item["id"] == "destination:korea")
    assert korea["budget_minor"] == 15000000
    assert "человек" in korea["prompt"].lower()
    assert "150" in korea["prompt"]
    assert "Япони" in korea["reason"]
    assert "стоимость поездки" not in korea["description"].lower()


def test_unknown_budget_scope_is_not_guessed():
    items = feed([signal("destination:japan", {"budget_minor": 9000000})])["personalized"]
    assert items
    assert any(word in items[0]["prompt"].lower() for word in ("уточни", "уточнить", "уточним"))


def test_hotel_only_recommendations_preserve_product_scope():
    items = feed([signal("destination:japan", {"budget_minor": 9000000,
                 "budget_scope": "trip", "products": ["hotel"], "adults": 2})])["personalized"]
    assert items
    for item in items:
        assert "отел" in item["prompt"].lower()
        assert "перелёт" not in item["prompt"].lower()
        assert "перелет" not in item["prompt"].lower()


def test_old_exact_dates_are_not_copied_to_new_trip():
    state = {"request": {"destination": "Япония", "departure_date": "2025-03-04",
                          "return_date": "2025-03-11", "budget_minor": 15000000}}
    extracted = extract_signals(state, "Хочу снова в Японию")
    items = feed([{**row, "occurred_at": NOW.isoformat()} for row in extracted])["personalized"]
    assert items
    assert all("2025-03-04" not in item["prompt"] and "2025-03-11" not in item["prompt"] for item in items)


def test_known_artist_produces_other_artist_research_not_sold_concert():
    rows = extract_signals({"concert_intake": {"mode": "trip", "event_name": "Imagine Dragons"}},
                           "Хочу поехать на концерт Imagine Dragons")
    assert "artist:imagine-dragons" in {row["topic_id"] for row in rows}
    items = feed([{**row, "occurred_at": NOW.isoformat()} for row in rows])["personalized"]
    artists = [item for item in items if item["kind"] == "artist"]
    assert artists and any(item["id"] != "artist:imagine-dragons" for item in artists)
    assert all(any(word in item["prompt"].lower() for word in ("проверь", "проверить", "найди", "поищи")) for item in artists)
    assert all("билеты в наличии" not in item["description"].lower() for item in artists)


def test_expired_interests_do_not_personalize_forever():
    result = feed([signal("destination:japan", age=100)])
    assert result["personalized"] == []


def test_duplicates_and_dismissal_do_not_fill_home_with_same_card():
    rows = [signal("destination:japan"), signal("destination:japan"), signal("artist:imagine-dragons")]
    first = feed(rows)
    ids = [item["id"] for item in first["personalized"] + first["popular"]]
    assert len(ids) == len(set(ids))
    assert first["personalized"]
    dismissed = first["personalized"][0]["id"]
    second = feed(rows, dismissed={dismissed})
    assert all(item["id"] != dismissed for item in second["personalized"] + second["popular"])


def test_cold_start_is_honestly_labeled_and_never_leaks_personal_context():
    result = feed([])
    assert result["personalized"] == []
    assert result["popular"]
    assert result["popular_source"] == "starter"
    assert all(item["source"] == "starter" for item in result["popular"])
    assert all(item["budget_minor"] is None for item in result["popular"])


def test_ordinary_weekend_is_not_the_weeknd_artist():
    rows = extract_signals({"request": {}}, "Хочу уикенд у моря")
    assert "artist:the-weeknd" not in {row["topic_id"] for row in rows}
    assert "theme:weekend" in {row["topic_id"] for row in rows}


def test_negation_after_destination_is_not_positive_interest():
    rows = extract_signals({"request": {"destination": "Южная Корея"}},
                           "Японию не предлагай, лучше Корею")
    assert "destination:japan" not in {row["topic_id"] for row in rows if not row["context"].get("rejected")}
    assert any(row["topic_id"] == "destination:japan" and row["context"].get("rejected") for row in rows)
    assert "destination:korea" in {row["topic_id"] for row in rows}


def test_north_korea_is_not_a_south_korea_interest():
    rows = extract_signals({"request": {"destination": "Северная Корея"}},
                           "Хочу в Северную Корею")
    assert "destination:korea" not in {row["topic_id"] for row in rows}


def test_latest_source_budget_wins_even_if_input_order_is_oldest_first():
    rows = [signal("destination:japan", {"budget_minor": 15000000, "budget_scope": "trip"}, age=30),
            signal("destination:japan", {"budget_minor": 9000000, "budget_scope": "trip"}, age=1)]
    items = feed(rows)["personalized"]
    assert items
    assert all(item["budget_minor"] == 9000000 for item in items)


@pytest.mark.parametrize("message", [
    "Не хочу больше на концерт, бюджет оставим прежним",
    "Отменяю поездку в Японию",
    "Не хочу в Японию, бюджет оставим прежним",
    "Отмена, бюджет пока прежний",
])
def test_cancellation_with_budget_words_does_not_reinforce_inherited_interests(message):
    state = {"request": {"destination": "Япония", "budget_minor": 10000000},
             "concert_intake": {"mode": "trip", "event_name": "Imagine Dragons"}}
    assert not [row for row in extract_signals(state, message) if not row["context"].get("rejected")]


@pytest.mark.parametrize(("origin", "phrase", "topic"), [
    ("Казань", "из Казани", "destination:kazan"),
    ("Париж", "из Парижа", "destination:france"),
])
def test_catalogued_departure_city_does_not_become_destination_interest(origin, phrase, topic):
    rows = extract_signals({"request": {"origin": origin, "destination": "Япония"}},
                           f"Хочу {phrase} в Японию")
    ids = {row["topic_id"] for row in rows}
    assert "destination:japan" in ids
    assert topic not in ids


def test_duration_carried_as_nights_without_reusing_dates():
    rows = extract_signals({"request": {"destination": "Япония", "departure_date": "2026-10-01",
                                         "return_date": "2026-10-08"}}, "Хочу в Японию")
    japan = next(row for row in rows if row["topic_id"] == "destination:japan")
    assert japan["context"]["nights"] == 7
    assert "departure_date" not in japan["context"]
    assert "return_date" not in japan["context"]
    items = feed([{**japan, "occurred_at": NOW.isoformat()}])["personalized"]
    assert all("7 ночей" in item["prompt"] for item in items)


def test_nightly_budget_retains_per_person_scope_and_is_not_total_trip_budget():
    rows = extract_signals({"request": {"destination": "Япония", "nightly_budget_minor": 600000,
                                         "budget_minor": 4200000, "budget_scope": "per_person",
                                         "products": ["hotel"], "adults": 2}}, "Хочу отель в Японии")
    japan = next(row for row in rows if row["topic_id"] == "destination:japan")
    assert japan["context"]["nightly_budget_minor"] == 600000
    items = feed([{**japan, "occurred_at": NOW.isoformat()}])["personalized"]
    assert items
    for item in items:
        assert item["budget_minor"] is None
        assert "6 000 ₽ за ночь" in item["prompt"]
        assert "на человека" in item["prompt"]
        assert "42 000" not in item["prompt"]


def test_personal_budget_does_not_leak_into_community_cards():
    result = feed([signal("destination:japan", {"budget_minor": 15000000, "budget_scope": "trip"})],
                  [{"topic_id": "destination:turkey", "users": 5, "score": 5.0}])
    assert result["personalized"] and result["popular_source"] == "community"
    assert result["popular"]
    for item in result["popular"]:
        assert item["budget_minor"] is None
        assert "150 000" not in item["prompt"]


def positive_ids(rows):
    return {row['topic_id'] for row in rows if not row['context'].get('rejected')}


@pytest.mark.parametrize('text', [
    'Я живу в Казани, хочу в Японию',
    'Город вылета Казань, направление Япония',
    'Казань — город отправления, хочу в Японию',
    'Вылет: Казань, хочу в Японию',
])
def test_residence_and_labeled_origin_are_not_destination_interest(text):
    rows = extract_signals({'request': {'origin': 'Казань', 'destination': 'Япония'}}, text)
    assert positive_ids(rows) == {'destination:japan'}


@pytest.mark.parametrize('text', [
    'Не хочу в Японию а лучше в Корею',
    'Вместо Японии хочу в Корею',
    'Япония отпадает, рассмотрим Корею',
    'Хочу Корею вместо Японии',
])
def test_rejection_and_positive_alternative_survive_same_sentence(text):
    rows = extract_signals({}, text)
    assert positive_ids(rows) == {'destination:korea'}
    assert {'topic_id': 'destination:japan', 'context': {'rejected': True}} in rows


@pytest.mark.parametrize(('text', 'rejected_topic'), [
    ('Без концертов, хочу в Японию', 'theme:concerts'),
    ('Выходные без моря, лучше горы', 'theme:sea'),
    ('Без любых концертов', 'theme:concerts'),
    ('Японию мне не предлагай', 'destination:japan'),
])
def test_explicit_negative_topics_are_signed_and_never_positive(text, rejected_topic):
    rows = extract_signals({}, text)
    assert rejected_topic not in positive_ids(rows)
    assert {'topic_id': rejected_topic, 'context': {'rejected': True}} in rows


def test_never_been_somewhere_is_not_a_refusal():
    rows = extract_signals({}, 'Я ещё не был в Японии, хочу туда поехать')
    assert positive_ids(rows) == {'destination:japan'}
    assert not any(row['context'].get('rejected') for row in rows)


@pytest.mark.parametrize('text', ['Без пересадок в Японию', 'Хочу без самолёта в Японию'])
def test_service_exclusion_does_not_reject_the_destination(text):
    rows = extract_signals({}, text)
    assert 'destination:japan' in positive_ids(rows)


def test_new_unknown_artist_does_not_inherit_old_artist_destination_or_cap():
    state = {'request': {'destination': 'Япония', 'budget_minor': 15000000, 'budget_scope': 'trip'},
             'concert_intake': {'mode': 'trip', 'event_name': 'Imagine Dragons'}}
    rows = extract_signals(state, 'Хочу на концерт Taylor Swift, бюджет 50000 рублей')
    assert positive_ids(rows) == {'theme:concerts'}
    assert rows[0]['context']['budget_minor'] == 5000000


def test_unrelated_destination_question_does_not_inherit_old_trip_conditions():
    state = {'request': {'destination': 'Япония', 'budget_minor': 15000000,
                          'adults': 4, 'products': ['rail', 'hotel'], 'origin': 'Москва'}}
    rows = extract_signals(state, 'Что интересного в Корее?')
    assert positive_ids(rows) == {'destination:korea'}
    assert rows[0]['context'] == {'origin': 'Москва'}


def test_explicit_current_cap_overrides_old_snapshot_cap():
    rows = extract_signals({'request': {'destination': 'Япония', 'budget_minor': 15000000,
                                       'budget_scope': 'trip'}}, 'Бюджет теперь 90000 рублей на всех')
    assert rows[0]['context']['budget_minor'] == 9000000


@pytest.mark.parametrize(('phrase', 'value', 'unit'), [
    ('на семь ночей', 7, 'nights'), ('на 7 ночей', 7, 'nights'), ('на неделю', 7, 'duration_days'),
    ('на две недели', 14, 'duration_days'), ('на 10 дней', 10, 'duration_days'), ('Длительность: 7 ночей', 7, 'nights'),
])
def test_duration_is_retained_without_any_calendar_dates(phrase, value, unit):
    rows = extract_signals({'request': {'destination': 'Япония'}}, f'Хочу в Японию {phrase}')
    japan = next(row for row in rows if row['topic_id'] == 'destination:japan')
    assert japan['context'][unit] == value
    other_unit = 'duration_days' if unit == 'nights' else 'nights'
    assert japan['context'][other_unit] is None
    result = feed([{**japan, 'occurred_at': NOW.isoformat()}])
    label = 'ночей' if unit == 'nights' else 'дней'
    assert all(f'{value} {label}' in item['prompt'] for item in result['personalized'])


def test_explicit_unknown_duration_blocks_old_nights_carry():
    rows = extract_signals({'request': {'destination': 'Япония'}}, 'Бюджет прежний, длительность пока не знаю')
    assert rows[0]['context']['nights'] is None


@pytest.mark.parametrize('context', [
    {'budget_minor': 'oops'}, {'products': ['hotel', {}]}, {'constraints': 'x'},
    {'children_ages': '12'}, {'nights': 'bad'}, {'budget_scope': []},
    {'adults': -5, 'nightly_budget_minor': -30}, [], 'corrupt', None,
])
def test_legacy_bad_context_does_not_crash_or_print_invalid_values(context):
    row = signal('destination:japan')
    row['context'] = context
    result = feed([row])
    assert result['personalized']
    prompt = result['personalized'][0]['prompt']
    assert all(part not in prompt for part in ('oops', 'bad', '1, 2 лет', '-5', '-30', 'corrupt'))


def test_latest_refusal_removes_source_and_suppresses_target_from_other_sources():
    rows = [signal('destination:japan', age=10), signal('destination:korea', {'rejected': True}, age=1)]
    result = feed(rows, [{'topic_id': 'destination:korea', 'users': 5, 'score': 5}])
    assert result['personalized']
    assert 'destination:korea' not in {item['id'] for item in result['personalized'] + result['popular'] + result['starter']}
    assert all('Корея' not in item['reason'] for item in result['personalized'])


def test_new_explicit_positive_interest_resets_earlier_refusal():
    result = feed([signal('destination:korea', {'rejected': True}, age=10), signal('destination:korea', age=1)])
    assert result['personalized']
    assert any('Корея' in item['reason'] for item in result['personalized'])


def test_concert_refusal_suppresses_old_artist_recommendations():
    result = feed([signal('artist:imagine-dragons', age=10), signal('theme:concerts', {'rejected': True}, age=1)])
    assert not any(item['kind'] == 'artist' for item in result['personalized'] + result['popular'])
    assert 'theme:concerts' not in {item['id'] for item in result['popular']}


def test_positive_artist_after_general_concert_refusal_reenables_music():
    result = feed([signal('theme:concerts', {'rejected': True}, age=10), signal('artist:coldplay', age=1)])
    assert any(item['kind'] == 'artist' for item in result['personalized'])


def test_current_default_origin_wins_over_historical_trip_origin():
    result = build_recommendations([signal('destination:japan', {'origin': 'Москва'})], [], {'origin': 'Казань'}, NOW)
    assert result['personalized']
    assert all('из Казани' in item['prompt'] and 'Москв' not in item['prompt'] for item in result['personalized'])


def test_hometown_is_not_recommended_as_a_new_destination():
    result = build_recommendations([signal('theme:weekend', {'origin': 'Казань'})], [], {}, NOW)
    assert 'destination:kazan' not in {item['id'] for item in result['personalized']}


def test_sparse_community_gets_separately_labeled_starters():
    result = feed([], [{'topic_id': 'destination:france', 'users': 3, 'score': 3}])
    assert result['popular_source'] == 'community'
    assert len(result['popular']) == 1 and result['popular'][0]['source'] == 'popular'
    assert len(result['starter']) == 5
    assert all(item['source'] == 'starter' for item in result['starter'])
    assert len({item['id'] for item in result['popular'] + result['starter']}) == 6


def test_sparse_starters_respect_dismissed_and_rejected_topics():
    result = feed([signal('theme:sea', {'rejected': True})],
                  [{'topic_id': 'destination:france', 'users': 3, 'score': 3}], dismissed={'theme:weekend'})
    assert {'theme:sea', 'theme:weekend'}.isdisjoint({item['id'] for item in result['starter']})


def test_corrupt_popularity_rows_do_not_crash_the_feed():
    result = feed([], [{'topic_id': 'destination:france', 'users': 'bad', 'score': 3},
                      {'topic_id': 'destination:korea', 'users': 4, 'score': 'NaN'}, None])
    assert result['popular_source'] == 'starter'


def test_future_dated_signal_does_not_pretend_to_be_current_interest():
    result = feed([signal('destination:japan', age=-365)])
    assert result['personalized'] == []


@pytest.mark.parametrize('products', [['hotel'], ['avia'], ['rail'], ['bus'],
                                      ['avia', 'hotel'], ['rail', 'hotel'], ['bus', 'hotel'],
                                      ['avia', 'rail'], ['avia', 'rail', 'hotel']])
async def test_generated_draft_roundtrips_products_party_origin_and_budget(products):
    from travel_agent.interpretation import RuleBasedGateway
    context = {'products': products, 'adults': 2, 'children_ages': [5, 7],
               'origin': 'Москва', 'budget_minor': 12000000, 'budget_scope': 'trip'}
    result = feed([signal('theme:weekend', context)])
    card = next(item for item in result['personalized'] if item['id'] == 'destination:kazan')
    parsed = await RuleBasedGateway().interpret(card['prompt'], {}, NOW.isoformat(), 'Europe/Moscow')
    patch = parsed['patch']
    assert patch.get('products', ['avia', 'hotel']) == products
    assert patch['origin'] == 'Москва'
    assert patch['adults'] == 2
    assert patch['children_ages'] == [5, 7]
    assert patch['budget_minor'] == 12000000
    assert patch['budget_scope'] == 'trip'


def test_explicit_days_do_not_become_confirmed_hotel_nights():
    rows = extract_signals({'request': {'destination': 'Япония'}}, 'Хочу в Японию на 7 дней')
    result = feed([{**row, 'occurred_at': NOW.isoformat()} for row in rows])
    assert result['personalized']
    for item in result['personalized']:
        assert '7 дней' in item['prompt']
        assert '7 ночей' not in item['prompt']
        assert 'число ночей уточни' in item['prompt']


@pytest.mark.parametrize('text', [
    'Мне не нужен перелёт в Японию, нужен только отель',
    'Не хочу пересадок на пути в Японию',
    'Не хочу ехать поездом в Японию, хочу самолётом',
])
def test_declining_a_service_does_not_reject_the_destination(text):
    rows = extract_signals({'request': {'destination': 'Япония'}}, text)
    assert 'destination:japan' in positive_ids(rows)
    assert not any(row['topic_id'] == 'destination:japan' and row['context'].get('rejected') for row in rows)


def test_short_departure_city_reply_updates_existing_trip_instead_of_creating_new_interest():
    rows = extract_signals({'request': {'destination': 'Япония', 'origin': 'Казань'}}, 'Казань')
    assert positive_ids(rows) == {'destination:japan'}
    assert rows[0]['context']['origin'] == 'Казань'


def test_short_new_destination_is_retained_when_snapshot_confirms_destination():
    rows = extract_signals({'request': {'destination': 'Казань', 'origin': 'Москва'}}, 'Казань')
    assert positive_ids(rows) == {'destination:kazan'}


@pytest.mark.parametrize(('scope', 'words'), [('trip', 'на всех'), ('per_person', 'на человека')])
async def test_nightly_draft_roundtrips_explicit_rate_scope(scope, words):
    from travel_agent.interpretation import RuleBasedGateway, nightly_budget
    result = feed([signal('theme:weekend', {'products': ['hotel'], 'nightly_budget_minor': 800000,
                                           'budget_scope': scope, 'adults': 2})])
    card = next(item for item in result['personalized'] if item['id'] == 'destination:kazan')
    assert f'8 000 ₽ за ночь {words}.' in card['prompt']
    assert nightly_budget(card['prompt']) == (800000, scope)
    parsed = await RuleBasedGateway().interpret(card['prompt'], {}, NOW.isoformat(), 'Europe/Moscow')
    assert parsed['patch']['budget_scope'] == scope


def test_duration_only_refinement_updates_current_interest():
    rows = extract_signals({'request': {'destination': 'Япония'}}, 'Поеду на три ночи')
    assert positive_ids(rows) == {'destination:japan'}
    assert rows[0]['context']['nights'] == 3
    assert rows[0]['context']['duration_days'] is None


def test_unknown_duration_only_refinement_resets_both_units():
    rows = extract_signals({'request': {'destination': 'Япония'}}, 'Длительность пока не знаю')
    assert positive_ids(rows) == {'destination:japan'}
    assert rows[0]['context']['nights'] is None
    assert rows[0]['context']['duration_days'] is None

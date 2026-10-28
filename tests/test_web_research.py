"""Bounded event-page research; every provider, model and reader is a local fake."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
import json
import sqlite3
from uuid import uuid4

import pytest
from travel_agent.domain.models import TurnInput
from travel_agent.interpretation import RuleBasedGateway
from travel_agent.service import AgentService
from travel_agent.web_research import enrich_search, public_search

NOW = datetime.fromisoformat('2026-10-27T12:00:00+03:00')
QUERY = 'Моргенштерн концерты'
REFINE = 'Моргенштерн официальный тур 2026 даты'
PAGE = 'PAGE_ONLY_MARKER_42. WORLD TOUR 2026 PART 2. 28 октября 2026. Хайфа. ROMEMA. Начало 20:30.'
DIALOGUE = [{'role': 'user', 'text': 'Где концерты Моргена? Нужна только афиша, город и дата любые'}]


def source(i, url=None):
    return {'id': f'yandex-{i}', 'title': 'Афиша артиста', 'url': url or f'https://events.example/{i}',
            'snippet': 'Страница тура. Полные даты в афише.'}


def result(query=QUERY, sources=None):
    return {'provider': 'yandex', 'query': query, 'results': deepcopy([source('initial')] if sources is None else sources),
            'fetched_at': NOW.isoformat(), 'error': None}


class Planner:
    def __init__(self, plan=None, failure=None):
        self.plan = plan or {'queries': [REFINE], 'source_ids': ['yandex-initial']}
        self.contexts, self.failure = [], failure

    async def research_plan(self, context):
        self.contexts.append(deepcopy(context))
        if self.failure is not None:
            raise self.failure
        return deepcopy(self.plan)


class Search:
    def __init__(self, responses=None, failure=None):
        self.responses = responses or {REFINE: [source('tour')]}
        self.calls, self.failure = [], failure

    async def search(self, query, now):
        self.calls.append((query, now))
        if self.failure is not None:
            raise self.failure
        return result(query, self.responses.get(query, [source('initial')]))


class Reader:
    def __init__(self, failure=None):
        self.calls, self.failure = [], failure

    async def read(self, url):
        self.calls.append(url)
        if self.failure is not None:
            raise self.failure
        return {'status': 'read', 'text': PAGE, 'final_url': url + '?redirect=1'}


async def enrich(gateway, search=None, reader=None, original=None, remaining=120):
    events = []

    async def emit(kind, payload):
        events.append({'type': kind, 'payload': deepcopy(payload)})

    enriched = await enrich_search(gateway, search or Search(), reader or Reader(), original or result(), DIALOGUE, NOW,
                                   deadline=asyncio.get_running_loop().time() + remaining, emit=emit)
    return enriched, events


async def test_missing_snippet_dates_trigger_refinement_and_page_year_evidence_without_input_mutation():
    original = result()
    snapshot = deepcopy(original)
    gateway, search, reader = Planner(), Search(), Reader()
    enriched, events = await enrich(gateway, search, reader, original)
    assert original == snapshot
    assert gateway.contexts[0]['recent_dialogue'] == DIALOGUE
    assert gateway.contexts[0]['now'] == NOW.isoformat()
    assert '2026' not in original['results'][0]['snippet']
    assert search.calls == [(REFINE, NOW.isoformat())]
    assert set(reader.calls) == {source('initial')['url'], source('tour')['url']}
    assert all('28 октября 2026' in item['page_text'] and item['page_status'] == 'read' for item in enriched['results'])
    assert enriched['research']['pages_read'] == enriched['research']['pages_attempted'] == 2
    assert {event['payload']['operation'] for event in events} == {'web_search_refine', 'web_read'}


async def test_model_invented_source_ids_and_url_queries_never_become_fetch_targets():
    search, reader = Search(), Reader()
    plan = {'queries': ['https://untrusted.example/invented-event'],
            'source_ids': ['not-in-yandex', 'https://untrusted.example/invented-event']}
    enriched, _ = await enrich(Planner(plan), search, reader)
    assert [query for query, _ in search.calls] == [QUERY + ' все даты города тур 2026']
    assert not any('untrusted.example' in query for query, _ in search.calls)
    assert reader.calls == [source('initial')['url']]
    assert [item['id'] for item in enriched['results']] == ['yandex-initial']


@pytest.mark.parametrize('queries,expected', [
    ([f'  {QUERY.upper()}  ', REFINE], [REFINE]),
    ([REFINE, REFINE.upper()], [REFINE]),
    (['афиша один', 'афиша два', 'афиша три'], [QUERY + ' все даты города тур 2026']),
])
async def test_query_duplicates_and_over_limit_plans_do_not_issue_extra_searches(queries, expected):
    search = Search()
    await enrich(Planner({'queries': queries, 'source_ids': []}), search)
    assert [query for query, _ in search.calls] == expected


async def test_event_merge_keeps_sixteen_distinct_sources_and_reads_six_known_urls():
    first = [source('initial'), source('duplicate-url', source('initial')['url'])] + [source(f'first-{i}') for i in range(10)]
    second = [source('first-0'), source('same-url', source('first-1')['url'])] + [source(f'second-{i}') for i in range(10)]
    search, reader = Search({'уточнение один': first, 'уточнение два': second}), Reader()
    plan = {'queries': ['уточнение один', 'уточнение два'], 'source_ids': ['yandex-initial']}
    enriched, _ = await enrich(Planner(plan), search, reader)
    sources = enriched['results']
    assert len(search.calls) == 2
    assert len(sources) == len({s['id'] for s in sources}) == len({s['url'] for s in sources}) == 16
    assert len(reader.calls) == len(set(reader.calls)) == 6
    assert set(reader.calls) <= {s['url'] for s in sources}
    assert any(s['id'].startswith('yandex-first-') for s in sources)
    assert any(s['id'].startswith('yandex-second-') for s in sources)


async def test_duplicate_urls_in_initial_batch_are_not_read_twice():
    original = result(sources=[source('one'), source('same-page', source('one')['url'])])
    reader = Reader()
    enriched, _ = await enrich(Planner({'queries': [], 'source_ids': []}), reader=reader, original=original)
    assert len(reader.calls) == len(set(reader.calls))
    assert len({s['url'] for s in enriched['results']}) == len(enriched['results'])


async def test_full_calendar_is_read_before_duplicate_archive_pages_consume_budget():
    calendar = {**source('calendar', 'https://organizer.example/tour'),
                'title': 'WORLD TOUR 2026 PART 2', 'snippet': 'Full tour dates and venues'}
    initial = [source('official', 'https://artist.example/tour'), calendar]
    archives = [{**source(f'archive-{index}', f'https://archive.example/event/{index}'),
                 'title': 'Concert history 2026', 'snippet': 'Past concerts and notifications'}
                for index in range(8)]
    extra = [source('future', 'https://venue.example/calendar')]
    reader = Reader()
    enriched, _ = await enrich(Planner({'queries': [REFINE], 'source_ids': ['yandex-official']}),
        Search({REFINE: [*archives, *extra]}), reader, result(sources=initial))
    assert calendar['url'] in reader.calls[:3]
    assert extra[0]['url'] in reader.calls[:4]
    assert sum('archive.example' in url for url in reader.calls) <= 3
    assert enriched['research']['pages_attempted'] == 6


@pytest.mark.parametrize('stage', ['planner', 'search', 'reader'])
async def test_optional_failures_preserve_initial_evidence_and_hide_exception_detail(stage):
    failure = RuntimeError('PRIVATE_EXCEPTION_DETAIL_83')
    gateway = Planner(failure=failure if stage == 'planner' else None)
    search = Search(failure=failure if stage == 'search' else None)
    reader = Reader(failure=failure if stage == 'reader' else None)
    original = result()
    enriched, events = await enrich(gateway, search, reader, original)
    assert {key: enriched['results'][0][key] for key in source('initial')} == source('initial')
    assert original == result()
    assert 'PRIVATE_EXCEPTION_DETAIL_83' not in json.dumps([enriched, events])
    assert enriched['provider'] == 'yandex'
    if stage == 'reader':
        assert all(s['page_status'] == 'unavailable' for s in enriched['results'])


@pytest.mark.parametrize('remaining', [-1, 0, 25])
async def test_expired_deadline_and_summary_reserve_prevent_optional_calls(remaining):
    gateway, search, reader = Planner(), Search(), Reader()
    enriched, events = await enrich(gateway, search, reader, remaining=remaining)
    assert gateway.contexts == search.calls == reader.calls == events == []
    assert enriched['results'] == result()['results']
    assert enriched['research']['pages_attempted'] == 0


async def test_no_planner_retains_single_search_path_without_reading_pages():
    search, reader, original = Search(), Reader(), result()
    enriched, events = await enrich(RuleBasedGateway(), search, reader, original)
    assert enriched == original and enriched is not original
    assert search.calls == reader.calls == events == []


@pytest.mark.parametrize('stage', ['planner', 'search', 'reader'])
async def test_cancellation_propagates_from_every_optional_operation(stage):
    cancel = asyncio.CancelledError()
    gateway = Planner(failure=cancel if stage == 'planner' else None)
    search = Search(failure=cancel if stage == 'search' else None)
    reader = Reader(failure=cancel if stage == 'reader' else None)
    with pytest.raises(asyncio.CancelledError):
        await enrich(gateway, search, reader)


def test_public_search_strips_page_text_redirects_and_extra_attributes_without_mutation():
    original = result(sources=[{**source('one'), 'page_status': 'read', 'page_text': PAGE,
                               'final_url': 'https://events.example/redirect', 'headers': {'private': 'value'}}])
    original['research'] = {'queries': [QUERY], 'pages_read': 1, 'pages_attempted': 1}
    snapshot = deepcopy(original)
    public = public_search(original)
    assert public['results'] == [{**source('one'), 'page_status': 'read'}]
    assert public['research'] == original['research']
    assert original == snapshot
    assert PAGE not in json.dumps(public)


class ResearchGateway(RuleBasedGateway):
    def __init__(self):
        self.plans, self.answers = [], []

    async def research_plan(self, context):
        self.plans.append(deepcopy(context))
        return {'queries': [REFINE], 'source_ids': ['yandex-initial']}

    async def research(self, context):
        self.answers.append(deepcopy(context))
        assert any(PAGE in s.get('page_text', '') for s in context['search_results'])
        return {'message': 'По афише: 28 октября 2026 — Хайфа, ROMEMA. Рассмотрим этот концерт?', 'source_ids': ['yandex-tour']}


class NoTravel:
    async def search(self, *args, **kwargs):
        raise AssertionError('Research must not start a travel search')


async def test_service_keeps_pages_only_in_summary_context_and_repeat_is_idempotent(tmp_path):
    database = tmp_path / 'research.sqlite'
    gateway, search, reader = ResearchGateway(), Search(), Reader()
    service = AgentService(db_path=database, data_mode='live', gateway=gateway, adapter=NoTravel(),
                           web_search_adapter=search, web_reader=reader)
    turn = TurnInput(request_id=str(uuid4()), user_id='reader-user', session_id='reader-trip',
                     expected_state_version=0, input='message', payload={'text': DIALOGUE[0]['text']}, now=NOW)
    events = []
    try:
        answer = await service.run_turn(turn, on_event=events.append)
        assert answer.status == 'research_ready', answer.model_dump()
        assert '28 октября 2026' in answer.message and 'афишу ещё нужно сверить' not in answer.message
        assert answer.options == [] and answer.current_plan is None and answer.pending_action is None
        assert len(gateway.plans) == len(gateway.answers) == 1
        assert len(search.calls) == len(reader.calls) == 2
        assert answer.evidence == [source('tour')['url']]
        session = await service.get_session(turn.user_id, turn.session_id)
        assert session.request.destination is None and session.request.departure_date is None
        assert session.last_web_search == answer.web_search
        exposed = json.dumps([answer.model_dump(mode='json'), session.model_dump(mode='json'), events])
        assert 'PAGE_ONLY_MARKER_42' not in exposed and 'page_text' not in exposed and 'final_url' not in exposed
        assert all(s['page_status'] == 'read' for s in answer.web_search['results'])
        repeated = await service.run_turn(turn)
        assert repeated.model_dump() == answer.model_dump()
        assert len(gateway.plans) == len(gateway.answers) == 1
        assert len(search.calls) == len(reader.calls) == 2
    finally:
        service.close()
    for path in (database, database.with_name(database.name + '.checkpoints')):
        with sqlite3.connect(path) as connection:
            dump = '\n'.join(connection.iterdump())
        assert 'PAGE_ONLY_MARKER_42' not in dump and 'page_text' not in dump and 'final_url' not in dump


async def test_empty_initial_search_can_refine_and_read_a_new_source():
    gateway, search, reader = Planner(), Search(), Reader()
    original = result(sources=[])
    enriched, events = await enrich(gateway, search, reader, original)
    assert original == result(sources=[])
    assert gateway.contexts[0]['search_results'] == []
    assert search.calls == [(REFINE, NOW.isoformat())]
    assert reader.calls == [source('tour')['url']]
    assert len(enriched['results']) == 1
    assert enriched['results'][0]['id'] == 'yandex-tour'
    assert enriched['results'][0]['page_status'] == 'read'
    assert '28 октября 2026' in enriched['results'][0]['page_text']
    assert enriched['research']['pages_read'] == 1
    assert {event['payload']['operation'] for event in events} == {'web_search_refine', 'web_read'}


class EmptyFirstSearch(Search):
    async def search(self, query, now):
        if not self.calls:
            self.calls.append((query, now))
            return result(query, sources=[])
        return await super().search(query, now)


async def test_service_refines_empty_first_search_before_asking_user_for_more_details(tmp_path):
    gateway, search, reader = ResearchGateway(), EmptyFirstSearch(), Reader()
    service = AgentService(db_path=tmp_path / 'empty-first.sqlite', data_mode='live', gateway=gateway,
                           adapter=NoTravel(), web_search_adapter=search, web_reader=reader)
    turn = TurnInput(request_id=str(uuid4()), user_id='reader-user', session_id='empty-first-trip',
                     expected_state_version=0, input='message', payload={'text': DIALOGUE[0]['text']}, now=NOW)
    try:
        answer = await service.run_turn(turn)
        assert answer.status == 'research_ready', answer.model_dump()
        assert '28 октября 2026' in answer.message
        assert len(gateway.plans) == len(gateway.answers) == 1
        assert gateway.plans[0]['search_results'] == []
        assert len(search.calls) == 2 and search.calls[1] == (REFINE, NOW.isoformat())
        assert reader.calls == [source('tour')['url']]
        assert answer.evidence == [source('tour')['url']]
        assert answer.options == [] and answer.current_plan is None and answer.pending_action is None
        assert answer.web_search['results'] == [{**source('tour'), 'page_status': 'read'}]
        session = await service.get_session(turn.user_id, turn.session_id)
        assert session.last_web_search == answer.web_search
        assert session.request.destination is None and session.request.departure_date is None
    finally:
        service.close()


async def test_read_first_plan_reconsiders_old_and_unavailable_pages_before_refining():
    old, unavailable = source('old-tour'), source('unavailable')
    future_sources = [source(f'future-{i}') for i in range(3)]
    original = result(sources=[old, unavailable])
    snapshot = deepcopy(original)

    class ReadThenPlan:
        def __init__(self):
            self.contexts = []

        async def research_plan(self, context):
            self.contexts.append(deepcopy(context))
            if len(self.contexts) == 1:
                assert all('page_text' not in item for item in context['search_results'])
                return {'queries': [], 'source_ids': [old['id'], unavailable['id']]}
            assert len(self.contexts) == 2
            evidence = {item['id']: item for item in context['search_results']}
            assert evidence[old['id']]['page_status'] == 'read'
            assert '12 ноября 2019' in evidence[old['id']]['page_text']
            assert evidence[unavailable['id']]['page_status'] == 'unavailable'
            assert 'page_text' not in evidence[unavailable['id']]
            return {'queries': [REFINE], 'source_ids': []}

    class EventReader(Reader):
        async def read(self, url):
            self.calls.append(url)
            if url == old['url']:
                return {'status': 'read', 'text': 'Архив тура: 12 ноября 2019 — концерт в Москве.', 'final_url': url}
            if url == unavailable['url']:
                return {'status': 'unavailable'}
            return {'status': 'read', 'text': PAGE, 'final_url': url}

    gateway, search, reader = ReadThenPlan(), Search({REFINE: future_sources}), EventReader()
    enriched, events = await enrich(gateway, search, reader, original)
    assert original == snapshot
    assert len(gateway.contexts) == 2
    assert search.calls == [(REFINE, NOW.isoformat())]
    assert reader.calls[:2] == [old['url'], unavailable['url']]
    assert set(reader.calls[2:]) == {item['url'] for item in future_sources}
    assert len(reader.calls) == len(set(reader.calls)) == enriched['research']['pages_attempted'] == 5
    assert enriched['research']['pages_read'] == 4
    sources = {item['id']: item for item in enriched['results']}
    assert '28 октября 2026' in sources[future_sources[0]['id']]['page_text']
    assert sources[future_sources[1]['id']]['page_status'] == 'read'
    assert sources[future_sources[2]['id']]['page_status'] == 'read'
    refine_start = next(i for i, event in enumerate(events)
                        if event['type'] == 'tool_started' and event['payload']['operation'] == 'web_search_refine')
    assert len([event for event in events[:refine_start]
                if event['type'] == 'tool_finished' and event['payload']['operation'] == 'web_read']) == 2

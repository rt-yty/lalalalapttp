"""Persistence, privacy, popularity and HTTP boundaries for home recommendations."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from travel_agent.auth import AuthStore
from travel_agent.home_recommendations import RecommendationStore
from travel_agent.storage.factory import create_repository
from travel_agent.web import create_app
from http_helpers import bootstrap
from test_web import submit, turn_body
from test_postgres import postgres_url

NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


def prepare(repo):
    AuthStore(repo)
    postgres = getattr(repo, 'dialect', 'sqlite') == 'postgres'
    sequence = 'sequence BIGSERIAL UNIQUE,' if postgres else ''
    repo.conn.execute(f'''CREATE TABLE IF NOT EXISTS web_jobs (
        {sequence}job_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
        request_id TEXT NOT NULL, turn_json TEXT NOT NULL, result TEXT,
        created_at TEXT NOT NULL, status TEXT NOT NULL)''')
    return RecommendationStore(repo)


@pytest.fixture
def repo(tmp_path):
    repo = create_repository(tmp_path / 'recommendations.sqlite')
    prepare(repo)
    yield repo
    repo.close()


def seed(repo, user='one', request='r1', *, when=NOW, text='Japan', kind='message',
         status='needs_input', response_kind='trip', version=1, state_version=None,
         receipt=True, account=True):
    if account:
        repo.conn.execute('INSERT OR IGNORE INTO accounts VALUES(?,?,?,?,?)', (user, user+'@example.com', 'unused', '', 0))
    session = user + '-trip'
    job = user + '-' + request
    state = {'state_version': state_version if state_version is not None else version,
             'request': {'destination': 'Япония', 'budget_minor': 18000000, 'origin': 'PrivateOrigin'}}
    result = {'state_version': version, 'status': status, 'response_kind': response_kind}
    turn = {'input': kind, 'payload': {'text': text}}
    repo.conn.execute('INSERT INTO sessions VALUES(?,?,?,?) ON CONFLICT(user_id,session_id) DO UPDATE SET version=excluded.version,body=excluded.body',
                      (user, session, state['state_version'], json.dumps(state)))
    if receipt:
        repo.conn.execute('INSERT OR IGNORE INTO turns VALUES(?,?,?,?,?)', (user, session, request, 'digest', json.dumps(result)))
    repo.conn.execute('INSERT INTO web_jobs(job_id,user_id,session_id,request_id,turn_json,result,created_at,status) VALUES(?,?,?,?,?,?,?,?)',
                      (job, user, session, request, json.dumps(turn), json.dumps(result), when.isoformat(), 'completed'))
    return repo.conn.execute('SELECT * FROM web_jobs WHERE job_id=?', (job,)).fetchone()


def capture_engine(monkeypatch):
    calls = []
    def extract(state, text):
        calls.append((state, text))
        return [{'topic_id': 'japan', 'context': {'budget_minor': state.get('request', {}).get('budget_minor'), 'private': text}}]
    def build(signals, popular, profile, now, dismissed=None):
        return {'signals': signals, 'popular': popular, 'profile': profile, 'dismissed': sorted(dismissed or [])}
    monkeypatch.setattr('travel_agent.home_recommendations.extract_signals', extract)
    monkeypatch.setattr('travel_agent.home_recommendations.build_recommendations', build)
    return calls


def test_signals_need_a_successful_committed_message_and_are_idempotent(repo, monkeypatch):
    calls = capture_engine(monkeypatch)
    store = RecommendationStore(repo)
    row = seed(repo)
    store.record_job(row, now=NOW)
    store.record_job(row, now=NOW)
    for request, kwargs in [('error', {'status': 'error'}), ('chat', {'response_kind': 'chat'}),
                            ('action', {'kind': 'select_component'}), ('receipt', {'receipt': False}),
                            ('guest', {'user': 'guest', 'account': False})]:
        store.record_job(seed(repo, request=request, **kwargs), now=NOW)
    assert len(calls) == 2  # Chat is inspected for refusals; its positive match is discarded.
    assert repo.conn.execute('SELECT COUNT(*) FROM recommendation_signals').fetchone()[0] == 1
    assert store.feed('other', {}, now=NOW)['signals'] == []


def test_backfill_never_reuses_newer_trip_context_for_older_query(repo, monkeypatch):
    calls = capture_engine(monkeypatch)
    seed(repo, request='old', version=1)
    seed(repo, request='latest', version=2)
    store = RecommendationStore(repo)
    store.backfill(now=NOW)
    by_text = calls
    assert len(by_text) == 2
    assert sorted(bool(state.get('request')) for state, _ in by_text) == [False, True]
    store.backfill(now=NOW)
    assert len(calls) == 2


def test_community_counts_unique_accounts_and_never_contains_private_context(repo, monkeypatch):
    capture_engine(monkeypatch)
    store = RecommendationStore(repo)
    for index in range(5):
        store.record_job(seed(repo, request='repeat'+str(index), text='secret-one'), now=NOW)
    store.record_job(seed(repo, user='two', text='secret-two'), now=NOW)
    assert store.feed('viewer', {}, now=NOW)['popular'] == []
    store.record_job(seed(repo, user='three', when=NOW-timedelta(days=45), text='secret-three'), now=NOW)
    feed = store.feed('viewer', {}, now=NOW)
    assert feed['popular'] == [{'topic_id': 'japan', 'users': 3, 'score': 2.4}]
    assert feed['signals'] == []
    assert 'secret' not in json.dumps(feed) and 'PrivateOrigin' not in json.dumps(feed)


def test_retention_and_dismissals_are_owned_and_survive_reopen(tmp_path, monkeypatch):
    capture_engine(monkeypatch)
    path = tmp_path/'durable.sqlite'
    repo = create_repository(path)
    store = prepare(repo)
    store.record_job(seed(repo), now=NOW)
    store.record_job(seed(repo, user='expired', when=NOW-timedelta(days=91)), now=NOW)
    store.dismiss('one', 'destination:korea', now=NOW)
    store.dismiss('one', 'destination:korea', now=NOW)
    repo.close()
    repo = create_repository(path)
    try:
        store = RecommendationStore(repo)
        assert store.feed('one', {}, now=NOW)['dismissed'] == ['destination:korea']
        assert store.feed('other', {}, now=NOW)['dismissed'] == []
        assert store.feed('expired', {}, now=NOW)['signals'] == []
        assert store.feed('one', {}, now=NOW+timedelta(days=91))['signals'] == []
        assert repo.conn.execute('SELECT COUNT(*) FROM recommendation_signals').fetchone()[0] == 0
    finally:
        repo.close()


def test_http_feed_and_dismissal_require_account_csrf_and_preserve_isolation(tmp_path):
    path = tmp_path/'http.sqlite'
    with TestClient(create_app(path, model_backend='rules'), base_url='http://127.0.0.1') as client:
        bootstrap(client, account=False)
        assert client.get('/api/recommendations').status_code == 401
        assert client.post('/api/recommendations/korea/dismiss').status_code == 401
        bootstrap(client)
        first = client.get('/api/recommendations')
        assert first.status_code == 200, first.text
        feed = first.json()
        assert feed['personalized'] == [] and feed['popular_source'] == 'starter'
        item = feed['popular'][0]
        token = client.headers.pop('X-Travel-Token')
        assert client.post('/api/recommendations/'+item['id']+'/dismiss').status_code == 403
        client.headers['X-Travel-Token'] = token
        assert client.post('/api/recommendations/'+item['id']+'/dismiss').json() == {'ok': True}
        after = client.get('/api/recommendations').json()
        assert item['id'] not in {card['id'] for card in after['popular']+after['personalized']}
        cookies = dict(client.cookies)
        client.cookies.clear()
        bootstrap(client)
        assert item['id'] in {card['id'] for card in client.get('/api/recommendations').json()['popular']}
    with TestClient(create_app(path, model_backend='rules'), base_url='http://127.0.0.1') as client:
        client.cookies.update(cookies)
        bootstrap(client)
        assert item['id'] not in {card['id'] for card in client.get('/api/recommendations').json()['popular']}


def test_completed_http_job_records_signals_and_retry_does_not_duplicate(tmp_path):
    with TestClient(create_app(tmp_path/'flow.sqlite', model_backend='rules'), base_url='http://127.0.0.1') as client:
        bootstrap(client)
        body = turn_body()
        submit(client, body)
        count = client.portal.call(lambda: client.app.state.service.repo.conn.execute('SELECT COUNT(*) FROM recommendation_signals').fetchone()[0])
        assert count > 0
        submit(client, body)
        assert client.portal.call(lambda: client.app.state.service.repo.conn.execute('SELECT COUNT(*) FROM recommendation_signals').fetchone()[0]) == count
        response = client.get('/api/recommendations')
        assert response.status_code == 200 and response.json()['history_used']


def test_postgres_recommendation_storage_ranking_and_isolation(postgres_url, monkeypatch):
    capture_engine(monkeypatch)
    repo = create_repository(postgres_url)
    try:
        store = prepare(repo)
        for user in ['first', 'second', 'third']:
            store.record_job(seed(repo, user=user), now=NOW)
        store.dismiss('first', 'destination:korea', now=NOW)
        feed = store.feed('first', {}, now=NOW)
        assert feed['popular'] == [{'topic_id': 'japan', 'users': 3, 'score': 3.0}]
        assert feed['dismissed'] == ['destination:korea']
        assert store.feed('viewer', {}, now=NOW)['signals'] == []
    finally:
        repo.close()

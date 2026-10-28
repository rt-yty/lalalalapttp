"""Account security and guest migration through the real HTTP boundary."""
import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from travel_agent.auth import AUTH_COOKIE, SESSION_SECONDS, verify_password
from travel_agent.web import COOKIE_NAME, create_app
from http_helpers import bootstrap as initialize_browser
from test_web import submit, turn_body
from test_postgres import postgres_url
from travel_agent.domain.models import TurnInput
from travel_agent.storage.sqlite import fingerprint

PASSWORD = "A real password 2026!"


def bootstrap(client):
    return initialize_browser(client, account=False)


def legacy_submit(client, body):
    """Seed data from the pre-account release without permitting guest HTTP work."""
    guest = client.cookies[COOKIE_NAME].split('.')[0]
    turn = TurnInput(**body, user_id=guest, now=datetime.now(timezone.utc).isoformat(), timezone='Europe/Moscow')
    result = client.portal.call(client.app.state.service.run_turn, turn)
    job_id = uuid4().hex
    client.portal.call(lambda: client.app.state.service.repo.conn.execute(
        'INSERT INTO web_jobs(job_id,session_id,request_id,body_hash,turn_json,user_text,status,result,error,events,created_at,user_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (job_id,turn.session_id,turn.request_id,fingerprint(body),turn.model_dump_json(),body['payload'].get('text','Выбираю вариант'),'completed',result.model_dump_json(),None,'[]',turn.now.isoformat(),guest)))
    return {'job_id':job_id,'result':result.model_dump(mode='json')}


def legacy_profile(client, value):
    guest = client.cookies[COOKIE_NAME].split('.')[0]
    client.portal.call(lambda: client.app.state.service.repo.conn.execute(
        'INSERT INTO profiles VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET body=excluded.body', (guest,json.dumps(value))))
    return guest


def auth(client, path, body, *, status=200):
    response = client.post('/api/auth/' + path, json=body)
    assert response.status_code == status, response.text
    if 'csrf_token' in response.json():
        client.headers['X-Travel-Token'] = response.json()['csrf_token']
    return response


def browser(client, cookies=None):
    client.cookies.clear()
    for name, value in (cookies or {}).items():
        client.cookies.set(name, value, domain='127.0.0.1', path='/')
    return bootstrap(client)


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path/'auth.sqlite', model_backend='rules'), base_url='http://127.0.0.1') as client:
        bootstrap(client)
        yield client


def test_signup_migrates_guest_trip_profile_and_revokes_guest_cookie(client):
    body = turn_body(session='guest-trip')
    done = legacy_submit(client, body)
    legacy_profile(client, {'origin':'Москва','preferences':['Тихий номер']})
    guest_cookies = dict(client.cookies)
    response = auth(client, 'signup', {'email':'Alex@Example.com','password':PASSWORD,'name':'Алексей'}, status=201)
    value = response.json()
    assert value['user']['email'] == 'alex@example.com' and value['user']['email_verified'] is False
    assert value['guest_migration']['session_id_map'] == {'guest-trip':'guest-trip'}
    assert value['guest_migration']['moved_sessions'] == 1
    assert client.get('/api/auth/me').json()['user'] == value['user']
    assert client.get('/api/profile').json()['preferences'] == ['Тихий номер']
    state = client.get('/api/sessions/guest-trip').json()['session']
    assert state['user_id'] == value['user']['id'] and state['state_version'] == 1
    repeated = submit(client, body)
    assert repeated['job_id'] == done['job_id'] and repeated['result'] == done['result']
    account_cookies = dict(client.cookies)
    client.cookies.clear()
    for name, cookie in guest_cookies.items():
        client.cookies.set(name,cookie,domain='127.0.0.1',path='/')
    assert client.get('/api/sessions/guest-trip').status_code == 401
    bootstrap(client)
    assert client.get('/api/sessions/guest-trip').status_code == 401
    browser(client, account_cookies)
    assert client.get('/api/sessions/guest-trip').json()['session']['user_id'] == value['user']['id']


def test_password_is_hashed_tokens_are_opaque_and_logout_invalidates(client):
    signed = auth(client,'signup',{'email':'account@example.com','password':PASSWORD},status=201)
    token = client.cookies[AUTH_COOKIE]
    assert len(token) == 64
    cookies = dict(client.cookies)
    row = client.portal.call(lambda: client.app.state.auth.account_by_email('account@example.com'))
    assert row['password_hash'].startswith('scrypt$131072$8$1$')
    assert PASSWORD not in row['password_hash']
    assert verify_password(PASSWORD,row['password_hash'])
    # Read via the running application's loop, preserving SQLite thread safety.
    saved = client.portal.call(lambda: client.app.state.service.repo.conn.execute('SELECT token_hash FROM auth_sessions').fetchone()[0])
    assert saved == hashlib.sha256(token.encode()).hexdigest() and saved != token
    old_csrf = signed.json()['csrf_token']
    auth(client,'logout',{})
    assert client.get('/api/auth/me').json()['user'] is None
    assert client.post('/api/profile',json={}).status_code == 401
    client.headers['X-Travel-Token'] = old_csrf
    assert client.patch('/api/profile',json={'origin':'Казань'}).status_code == 401
    client.cookies.clear()
    for name,value in cookies.items():
        client.cookies.set(name,value,domain='127.0.0.1',path='/')
    assert client.get('/api/sessions').status_code == 401
    bootstrap(client)
    assert client.get('/api/auth/me').json()['user'] is None


def test_second_device_login_gets_same_account_and_no_other_accounts(client):
    first = auth(client,'signup',{'email':'one@example.com','password':PASSWORD},status=201).json()['user']
    submit(client,turn_body(session='private'))
    cookies_one = dict(client.cookies)
    browser(client)
    wrong = client.post('/api/auth/login',json={'email':'one@example.com','password':'Wrong password long'})
    unknown = client.post('/api/auth/login',json={'email':'missing@example.com','password':'Wrong password long'})
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json()
    assert client.get('/api/sessions/private').status_code == 401
    logged = auth(client,'login',{'email':'ONE@example.com','password':PASSWORD}).json()['user']
    assert logged == first
    assert client.get('/api/sessions/private').json()['session']
    browser(client,cookies_one)
    assert client.get('/api/auth/me').json()['user'] == first
    browser(client)
    auth(client,'signup',{'email':'two@example.com','password':PASSWORD},status=201)
    assert client.get('/api/sessions/private').json()['session'] is None


def test_expiry_and_duplicate_registration(client):
    auth(client,'signup',{'email':'expiry@example.com','password':PASSWORD},status=201)
    browser(client)
    duplicate = client.post('/api/auth/signup',json={'email':'EXPIRY@example.com','password':PASSWORD})
    assert duplicate.status_code == 409
    auth(client,'login',{'email':'expiry@example.com','password':PASSWORD})
    client.app.state.auth.clock = lambda: time.time() + SESSION_SECONDS + 1
    assert client.get('/api/sessions').status_code == 401
    bootstrap(client)
    assert client.get('/api/auth/me').json()['user'] is None


def test_csrf_fixation_and_cross_account_tokens_fail(client):
    guest_csrf = client.headers['X-Travel-Token']
    client.headers.pop('X-Travel-Token')
    assert client.post('/api/auth/signup',json={'email':'csrf@example.com','password':PASSWORD}).status_code == 403
    client.headers['X-Travel-Token'] = guest_csrf
    assert client.post('/api/auth/signup',json={'email':'csrf@example.com','password':PASSWORD},headers={'Origin':'https://attacker.example'}).status_code == 403
    response = auth(client,'signup',{'email':'csrf@example.com','password':PASSWORD},status=201)
    assert response.json()['csrf_token'] != guest_csrf
    client.headers['X-Travel-Token'] = guest_csrf
    assert client.patch('/api/profile',json={'origin':'Казань'}).status_code == 403
    old_auth_csrf = response.json()['csrf_token']
    browser(client)
    auth(client,'signup',{'email':'other-csrf@example.com','password':PASSWORD},status=201)
    client.headers['X-Travel-Token'] = old_auth_csrf
    assert client.post('/api/auth/logout',json={}).status_code == 403


def test_rate_limit_persists_and_validation_never_echoes_secrets(client):
    secret = 'secret-password-in-invalid-body'
    for payload in [{'email':'bad','password':secret},{'email':'ok@example.com','password':secret,'secret-field':secret},{'email':'ok@example.com','password':'short'}]:
        response = client.post('/api/auth/signup',json=payload)
        assert response.status_code == 422 and secret not in response.text and 'secret-field' not in response.text
    for i in range(10):
        response = client.post('/api/auth/login',json={'email':'blocked@example.com','password':PASSWORD})
        assert response.status_code == 401
    blocked = client.post('/api/auth/login',json={'email':'blocked@example.com','password':PASSWORD})
    assert blocked.status_code == 429 and int(blocked.headers['retry-after']) > 0
    browser(client)
    assert client.post('/api/auth/login',json={'email':'blocked@example.com','password':PASSWORD}).status_code == 429


def test_migration_remaps_colliding_sessions_and_preserves_both_profiles(client):
    account = auth(client,'signup',{'email':'collision@example.com','password':PASSWORD},status=201).json()['user']
    first = submit(client,turn_body(session='same-id'))
    account_preferences = [f'Пожелание {i}' for i in range(10)]
    client.patch('/api/profile',json={'origin':'Москва','preferences':account_preferences})
    auth(client,'logout',{})
    second_body = turn_body(session='same-id',text='Привет')
    second = legacy_submit(client,second_body)
    guest_id = legacy_profile(client,{'origin':'Казань','preferences':['Гостевое пожелание']})
    response = auth(client,'login',{'email':'collision@example.com','password':PASSWORD}).json()
    target = response['guest_migration']['session_id_map']['same-id']
    assert target != 'same-id' and response['guest_migration']['profile_preferences_not_imported'] == 1
    assert client.get('/api/sessions/same-id').json()['result'] == first['result']
    moved = client.get('/api/sessions/'+target).json()
    assert moved['session']['user_id'] == account['id']
    assert moved['messages'][0]['text'] == 'Привет'
    repeated = submit(client,second_body|{'session_id':target})
    assert repeated['job_id'] == second['job_id']
    assert client.get('/api/profile').json() == {'origin':'Москва','preferences':account_preferences}
    archived = client.portal.call(lambda: client.app.state.service.repo.conn.execute('SELECT body FROM profiles WHERE user_id=?',(guest_id,)).fetchone()[0])
    assert json.loads(archived)['preferences'] == ['Гостевое пожелание']


def test_active_guest_work_blocks_signup_without_partial_account_creation(tmp_path):
    with TestClient(create_app(tmp_path/'active.sqlite',model_backend='rules'),base_url='http://127.0.0.1') as client:
        bootstrap(client)
        previous = legacy_submit(client,turn_body())
        client.portal.call(lambda: client.app.state.service.repo.conn.execute(
            "UPDATE web_jobs SET status='running' WHERE job_id=?", (previous['job_id'],)))
        response = client.post('/api/auth/signup',json={'email':'active@example.com','password':PASSWORD})
        assert response.status_code == 409
        assert client.portal.call(lambda: client.app.state.auth.account_by_email('active@example.com')) is None
        assert client.get('/api/auth/me').json()['user'] is None


def test_account_and_profile_survive_server_restart(tmp_path):
    path=tmp_path/'restart.sqlite'
    with TestClient(create_app(path,model_backend='rules'),base_url='http://127.0.0.1') as client:
        bootstrap(client)
        account=auth(client,'signup',{'email':'restart@example.com','password':PASSWORD},status=201).json()['user']
        client.patch('/api/profile',json={'origin':'Томск'})
        cookies=dict(client.cookies)
    with TestClient(create_app(path,model_backend='rules'),base_url='http://127.0.0.1') as client:
        browser(client,cookies)
        assert client.get('/api/auth/me').json()['user']==account
        assert client.get('/api/profile').json()['origin']=='Томск'


def test_https_auth_cookies_are_secure_httponly_and_maps_csp(tmp_path):
    with TestClient(create_app(tmp_path/'secure.sqlite',model_backend='rules'),base_url='https://127.0.0.1') as client:
        bootstrap(client)
        response=auth(client,'signup',{'email':'secure@example.com','password':PASSWORD},status=201)
        cookies=response.headers.get_list('set-cookie')
        assert any(value.startswith('__Host-travel_account=') for value in cookies)
        assert all('Secure' in value and 'HttpOnly' in value and 'SameSite=lax' in value for value in cookies)
        assert 'frame-src https://www.google.com https://maps.google.com' in response.headers['content-security-policy']


def test_migrated_pending_confirmation_keeps_action_integrity(client):
    found = legacy_submit(client, turn_body(session='pending'))['result']
    for card in (found['transport_options'][0], found['hotel_options'][0]):
        found = legacy_submit(client,turn_body(session='pending',version=found['state_version'],kind='select_component',payload={'component_id':card['component_id'],'options_version':card['options_version']}))['result']
    pending = legacy_submit(client,turn_body(session='pending',version=found['state_version'],text='Оформить'))['result']
    assert pending['pending_action']
    auth(client,'signup',{'email':'pending@example.com','password':PASSWORD},status=201)
    payload={key:pending['pending_action'][key] for key in ('action_id','plan_id','plan_version','payload_hash')}
    body=turn_body(session='pending',version=pending['state_version'],kind='confirm_action',payload=payload)
    completed=submit(client,body)
    assert completed['result']['status']=='completed_demo'
    assert submit(client,body)['result']['demo_order']==completed['result']['demo_order']


def test_postgres_account_migration_restart_and_revoked_token(postgres_url):
    with TestClient(create_app(postgres_url,model_backend='rules'),base_url='http://127.0.0.1') as client:
        bootstrap(client)
        body=turn_body(session='postgres-account')
        previous=legacy_submit(client,body)
        account=auth(client,'signup',{'email':'postgres@example.com','password':PASSWORD},status=201).json()['user']
        cookies=dict(client.cookies)
        assert submit(client,body)['job_id']==previous['job_id']
    with TestClient(create_app(postgres_url,model_backend='rules'),base_url='http://127.0.0.1') as client:
        browser(client,cookies)
        assert client.get('/api/auth/me').json()['user']==account
        assert client.get('/api/sessions/postgres-account').json()['session']['user_id']==account['id']
        auth(client,'logout',{})
    with TestClient(create_app(postgres_url,model_backend='rules'),base_url='http://127.0.0.1') as client:
        browser(client,cookies)
        assert client.get('/api/auth/me').json()['user'] is None
        assert client.get('/api/sessions/postgres-account').status_code == 401
        auth(client,'login',{'email':'postgres@example.com','password':PASSWORD})
        assert client.get('/api/sessions/postgres-account').json()['session']['user_id']==account['id']


def test_password_hashing_is_bounded_and_does_not_block_http(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from travel_agent.auth import hash_password

    release, two_started = threading.Event(), threading.Event()
    lock, counts = threading.Lock(), {'active':0,'maximum':0}

    def blocking_hash(password):
        with lock:
            counts['active'] += 1
            counts['maximum'] = max(counts['maximum'], counts['active'])
            if counts['active'] == 2:
                two_started.set()
        try:
            assert release.wait(5)
            return hash_password(password)
        finally:
            with lock:
                counts['active'] -= 1

    monkeypatch.setattr('travel_agent.web.hash_password', blocking_hash)
    headers = {'Cookie':'; '.join(name+'='+value for name,value in client.cookies.items()),'X-Travel-Token':client.headers['X-Travel-Token']}
    with ThreadPoolExecutor(3) as pool:
        futures=[pool.submit(client.post,'/api/auth/signup',json={'email':f'parallel{i}@example.com','password':PASSWORD},headers=headers) for i in range(3)]
        try:
            assert two_started.wait(3)
            started=time.monotonic()
            assert client.get('/api/health').status_code==200
            assert time.monotonic()-started < .5
            assert counts['maximum']==2
        finally:
            release.set()
        assert all(future.result().status_code==201 for future in futures)
        assert counts['maximum']==2


def test_all_business_routes_require_account_before_work_and_preserve_legacy_data(client, monkeypatch):
    from fastapi.routing import APIRoute

    old = legacy_submit(client, turn_body(session='archived-guest'))
    guest = legacy_profile(client, {'origin':'Москва','preferences':['Тихий номер']})
    original_cookies = dict(client.cookies)
    counts = lambda: client.portal.call(lambda: {
        table: client.app.state.service.repo.conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
        for table in ('sessions','web_jobs','turns','profiles','events','demo_orders','accounts','auth_sessions','auth_rate_limits')})
    before = counts()
    calls = []

    async def forbidden_work(*args, **kwargs):
        calls.append('run_turn')
        raise AssertionError('An anonymous request reached the agent')

    monkeypatch.setattr(client.app.state.service, 'run_turn', forbidden_work)
    public = {'/api/bootstrap','/api/health','/api/auth/signup','/api/auth/login','/api/auth/logout','/api/auth/me'}
    routes = [(method,route.path.replace('{session_id}','archived-guest').replace('{job_id}',old['job_id']))
              for route in client.app.routes if isinstance(route,APIRoute) and route.path not in public
              for method in route.methods]
    routes += [('GET','/api/jobs/'+old['job_id']+'/events'),('GET','/api/events'),
               ('POST','/api/turn'),('POST','/api/search'),('POST','/api/sessions'),
               ('POST','/api/turns/'),('POST','/api/%74urns'),('GET','/api/sessions?user_id='+guest)]
    assert len(routes) >= 14
    for has_cookie in (False, True):
        client.cookies.clear()
        if has_cookie:
            client.cookies.update(original_cookies)
            assert bootstrap(client).json()['auth_required'] is True
        for method,path in routes:
            response = client.request(method,path,content=b'not even valid JSON',headers={'Content-Type':'application/json'})
            assert response.status_code == 401,(method,path,response.status_code,response.text)
            assert response.json() == {'detail':'Войдите в аккаунт, чтобы продолжить.'}
    assert counts() == before and calls == []
    assert client.get('/api/health').status_code == 200
    assert client.get('/').status_code == 200
    assert client.get('/api/bootstrap').json()['user'] is None
    archived = client.portal.call(lambda: client.app.state.service.repo.session(guest,'archived-guest'))
    assert archived['state_version'] == 1


def test_unauthenticated_voice_and_turn_body_are_not_consumed(client):
    def expensive_upload():
        raise AssertionError('Anonymous upload must not be read')
        yield b''

    for path in ('/api/voice/transcribe','/api/turns'):
        response=client.post(path,content=expensive_upload(),headers={'Content-Type':'application/octet-stream'})
        assert response.status_code==401


def test_logged_out_account_cannot_read_any_trip_surface_or_start_work(client):
    auth(client,'signup',{'email':'gate@example.com','password':PASSWORD},status=201)
    body=turn_body(session='account-trip')
    job=submit(client,body)
    client.patch('/api/profile',json={'origin':'Москва'})
    old_cookies=dict(client.cookies)
    auth(client,'logout',{})
    for cookies in (dict(client.cookies),old_cookies):
        client.cookies.clear()
        for name,value in cookies.items():
            client.cookies.set(name,value,domain='127.0.0.1',path='/')
        for path in ('/api/sessions','/api/sessions/account-trip','/api/profile','/api/jobs/'+job['job_id']):
            assert client.get(path).status_code==401
        assert client.post('/api/turns',json=turn_body()).status_code==401
        assert client.patch('/api/profile',json={'origin':'Казань'}).status_code==401
    bootstrap(client)
    auth(client,'login',{'email':'gate@example.com','password':PASSWORD})
    assert client.get('/api/profile').json()['origin']=='Москва'
    assert client.get('/api/sessions/account-trip').json()['session']['state_version']==1


def test_account_continuation_preserves_concert_intake(tmp_path):
    path=tmp_path/'concert-continuation.sqlite'
    intake={'mode':'trip','event_name':'Coldplay','period':'ноябрь 2026',
            'locations':'Сеул или Токио','budget_flexible':False,
            'open_to_suggestions':False,'children_pending':True}
    with TestClient(create_app(path,model_backend='rules',data_mode='replay'),base_url='http://127.0.0.1') as client:
        bootstrap(client)
        user=auth(client,'signup',{'email':'continue@example.com','password':PASSWORD},status=201).json()['user']
        submit(client,turn_body(session='concert'))
        def seed_intake():
            repo=client.app.state.service.repo
            state=repo.session(user['id'],'concert')
            state['concert_intake']=intake
            repo.conn.execute('UPDATE sessions SET body=? WHERE user_id=? AND session_id=?',
                              (json.dumps(state),user['id'],'concert'))
        client.portal.call(seed_intake)
        cookies=dict(client.cookies)
    with TestClient(create_app(path,model_backend='rules',data_mode='live'),base_url='http://127.0.0.1') as client:
        browser(client,cookies)
        response=client.post('/api/sessions/concert/continue')
        assert response.status_code==200,response.text
        fresh=client.get('/api/sessions/'+response.json()['session_id']).json()['session']
        assert fresh['concert_intake']==intake

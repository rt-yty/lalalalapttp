"""Registered accounts -> real turns -> real home feed, including PostgreSQL.

No recommendation, model decision or provider answer is stubbed here. The rules
model uses incomplete requests, so the real application asks for missing details
without contacting a travel provider. All accounts live in a disposable database.
"""
from copy import deepcopy
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from travel_agent.web import create_app
from http_helpers import bootstrap
from test_web import submit, turn_body
from test_postgres import postgres_url

PASSWORD = 'Recommendation journey test 2026!'
JAPAN = 'Хочу в Японию на семь ночей. Бюджет 180 тысяч рублей на всех, два взрослых. Нужен только отель.'


@pytest.fixture(params=['sqlite', 'postgres'])
def journey_database(request, tmp_path):
    return tmp_path / 'journey.sqlite' if request.param == 'sqlite' else request.getfixturevalue('postgres_url')


def register(client, name):
    client.cookies.clear()
    bootstrap(client, account=False)
    email = f'recommendation-{name}-{uuid4().hex}@example.com'
    response = client.post('/api/auth/signup', json={'email':email,'password':PASSWORD,'name':name})
    assert response.status_code == 201, response.text
    client.headers['X-Travel-Token'] = response.json()['csrf_token']
    return {'email':email, 'id':response.json()['user']['id'], 'cookies':[deepcopy(cookie) for cookie in client.cookies.jar]}


def enter(client, account):
    client.cookies.clear()
    for cookie in account['cookies']:
        client.cookies.jar.set_cookie(deepcopy(cookie))
    value = bootstrap(client, account=False).json()
    assert value['user']['id'] == account['id']


def feed(client):
    response = client.get('/api/recommendations')
    assert response.status_code == 200, response.text
    assert response.headers.get('cache-control') == 'no-store'
    return response.json()


def ids(value):
    return {card['id'] for card in value['personalized'] + value['popular'] + value.get('starter', [])}


def query(client, text, *, session=None, version=0):
    body = turn_body(session=session,version=version,text=text)
    result = submit(client,body)['result']
    assert result['status'] != 'error', result
    return body,result


def test_registered_journey_budget_music_popularity_privacy_dismiss_and_restart(journey_database):
    with TestClient(create_app(journey_database,model_backend='rules'),base_url='http://127.0.0.1') as client:
        first = register(client, 'first')
        initial = feed(client)
        assert initial['personalized'] == [] and initial['popular_source'] == 'starter'
        assert initial['popular'] and all(card['source']=='starter' for card in initial['popular'])
        assert client.patch('/api/profile',json={'origin':'Казань','preferences':[]}).status_code == 200
        japan_body,japan_result = query(client,JAPAN,session='japan-trip')
        personal = feed(client)
        korea = next(card for card in personal['personalized'] if card['id']=='destination:korea')
        assert korea['budget_minor'] == 18000000
        assert 'на всех' in korea['budget_label']
        assert 'из Казани' in korea['prompt'] and '2 взрослых' in korea['prompt']
        assert '7 ночей' in korea['prompt']
        assert 'отель' in korea['prompt'] and 'перелёт' not in korea['prompt']
        assert 'Япони' in korea['reason']
        count = client.portal.call(lambda:client.app.state.service.repo.conn.execute('SELECT COUNT(*) FROM recommendation_signals').fetchone()[0])
        submit(client,japan_body)
        assert client.portal.call(lambda:client.app.state.service.repo.conn.execute('SELECT COUNT(*) FROM recommendation_signals').fetchone()[0]) == count

        _,updated = query(client,'Бюджет теперь 90 тысяч рублей на всех.',session='japan-trip',version=japan_result['state_version'])
        after_budget = feed(client)
        korea = next(card for card in after_budget['personalized'] if card['id']=='destination:korea')
        assert korea['budget_minor']==9000000 and '180 000' not in korea['prompt']
        assert '7 ночей' in korea['prompt'], 'A budget follow-up must not discard known trip duration.'
        query(client,'Хочу поехать на концерт Imagine Dragons.',session='music-trip')
        own_feed = feed(client)
        assert any(card['kind']=='artist' for card in own_feed['personalized'])
        assert any(card['kind']=='destination' for card in own_feed['personalized'])
        assert own_feed['popular_source']=='starter', 'One account cannot create community popularity.'

        second = register(client,'second')
        assert feed(client)['personalized']==[]
        second_body,second_result=query(client,JAPAN,session='second-japan')
        assert feed(client)['popular_source']=='starter'
        third=register(client,'third')
        query(client,JAPAN.replace('180','120'),session='third-japan')
        assert any(card['id']=='destination:japan' for card in feed(client)['popular'])
        viewer=register(client,'viewer')
        public=feed(client)
        assert public['personalized']==[] and public['popular_source']=='community'
        assert any(card['id']=='destination:japan' and card['source']=='popular' for card in public['popular'])
        assert all(card['budget_minor'] is None and card.get('budget_label') is None for card in public['popular'])
        encoded=json.dumps(public,ensure_ascii=False)
        assert first['id'] not in encoded and first['email'] not in encoded
        assert 'Казань' not in encoded and '180 000' not in encoded and '90 000' not in encoded
        # A second device sees the same account but no other account's trips.
        assert client.get('/api/sessions/japan-trip').json()['session'] is None
        enter(client,first)
        assert any(card['kind']=='artist' for card in feed(client)['personalized'])
        target='destination:korea'
        token=client.headers.pop('X-Travel-Token')
        assert client.post('/api/recommendations/'+target+'/dismiss').status_code==403
        client.headers['X-Travel-Token']=token
        assert client.post('/api/recommendations/'+target+'/dismiss').status_code==200
        assert target not in ids(feed(client))
        # Hiding a suggestion is local to the account, not a global removal.
        enter(client,second)
        assert target in ids(feed(client))
        query(client,'Не хочу в Японию, передумал.',session=second_body['session_id'],version=second_result['state_version'])
        enter(client,viewer)
        assert not any(card['id']=='destination:japan' for card in feed(client)['popular']), 'A current refusal must withdraw that account\'s popularity vote.'
        enter(client,first)
        logout=client.post('/api/auth/logout',json={})
        assert logout.status_code==200
        client.headers['X-Travel-Token']=logout.json()['csrf_token']
        assert client.get('/api/recommendations').status_code==401
        login=client.post('/api/auth/login',json={'email':first['email'],'password':PASSWORD})
        assert login.status_code==200,login.text
        client.headers['X-Travel-Token']=login.json()['csrf_token']
        assert target not in ids(feed(client))
        assert any(card['kind']=='artist' for card in feed(client)['personalized'])
        saved_cookies=[deepcopy(cookie) for cookie in client.cookies.jar]

    with TestClient(create_app(journey_database,model_backend='rules'),base_url='http://127.0.0.1') as client:
        for cookie in saved_cookies:
            client.cookies.jar.set_cookie(deepcopy(cookie))
        assert bootstrap(client,account=False).json()['user']['id']==first['id']
        restored=feed(client)
        assert target not in ids(restored)
        assert any(card['kind']=='artist' for card in restored['personalized'])
        assert any(card['budget_minor']==9000000 for card in restored['personalized'])
        assert restored['popular_source']=='starter'


def test_real_job_error_stale_version_and_chat_cannot_add_or_replace_interests(journey_database):
    with TestClient(create_app(journey_database,model_backend='rules'),base_url='http://127.0.0.1') as client:
        register(client,'isolation')
        body,result=query(client,JAPAN,session='stable')
        original=feed(client)['personalized']
        # Revision conflicts occur through the actual HTTP guard; no fake job.
        response=client.post('/api/turns',json=turn_body(session='stable',version=0,text='Хочу в Италию'))
        assert response.status_code in {202,409}
        if response.status_code==202:
            from test_web import wait_job
            assert wait_job(client,response.json()['job_id'])['result']['status']=='error'
        assert feed(client)['personalized']==original
        _,chat=query(client,'Спасибо!',session='stable',version=result['state_version'])
        assert chat['response_kind']=='chat'
        assert feed(client)['personalized']==original
        conflict=deepcopy(body)
        conflict['payload']['text']='Хочу во Францию'
        assert client.post('/api/turns',json=conflict).status_code==409
        assert feed(client)['personalized']==original
        assert client.post('/api/recommendations/invalid%20id/dismiss').status_code==422

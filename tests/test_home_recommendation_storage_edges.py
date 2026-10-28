"""Regression cases found by testing stored histories, not just fresh feeds."""
from datetime import timedelta
import json

import pytest

from travel_agent.home_recommendations import RecommendationStore
from travel_agent.storage.factory import create_repository
from test_home_recommendations import NOW, prepare, seed
from test_postgres import postgres_url


@pytest.fixture(params=['sqlite','postgres'])
def edge_repo(request,tmp_path):
    path=tmp_path/'edges.sqlite' if request.param=='sqlite' else request.getfixturevalue('postgres_url')
    repo=create_repository(path)
    prepare(repo)
    yield repo
    repo.close()


def test_many_repeated_searches_do_not_evict_another_interest(edge_repo):
    repo=edge_repo
    store=RecommendationStore(repo)
    store.record_job(seed(repo,request='music',text='Хочу на концерт Imagine Dragons',when=NOW-timedelta(days=1)),now=NOW)
    for index in range(505):
        store.record_job(seed(repo,request=f'japan-{index}',text='Хочу в Японию'),now=NOW)
    value=store.feed('one',{},now=NOW)
    assert any(card['kind']=='artist' for card in value['personalized'])
    assert any(card['kind']=='destination' for card in value['personalized'])


def test_uncommitted_legacy_jobs_cannot_starve_backfill(edge_repo):
    repo=edge_repo
    seed(repo,request='valid',when=NOW-timedelta(days=1))
    for index in range(305):
        seed(repo,request=f'uncommitted-{index}',receipt=False)
    value=RecommendationStore(repo).feed('one',{},now=NOW)
    assert value['history_used']
    assert any(card['id']=='destination:korea' for card in value['personalized'])


def test_malformed_legacy_json_is_skipped_once_and_valid_history_still_loads(edge_repo):
    repo=edge_repo
    seed(repo,request='good',when=NOW-timedelta(days=1))
    cases=[('turn_json','[]'),('turn_json','null'),('turn_json','{"input":"message","payload":null}'),
           ('result','[]'),('result','null'),('turn_json','{broken'),('result','{broken')]
    for index,(column,value) in enumerate(cases):
        row=seed(repo,request=f'bad-{index}')
        repo.conn.execute(f'UPDATE web_jobs SET {column}=? WHERE job_id=?',(value,row['job_id']))
    store=RecommendationStore(repo)
    value=store.feed('one',{},now=NOW)
    assert any(card['id']=='destination:korea' for card in value['personalized'])
    count=repo.conn.execute('SELECT COUNT(*) FROM recommendation_processed_jobs').fetchone()[0]
    assert count==len(cases)+1
    store.feed('one',{},now=NOW)
    assert repo.conn.execute('SELECT COUNT(*) FROM recommendation_processed_jobs').fetchone()[0]==count


def test_extractor_upgrade_rebuilds_wrong_signals_and_keeps_dismissals(edge_repo):
    repo=edge_repo
    row=seed(repo,text='Хочу уикенд у моря')
    repo.conn.execute('INSERT INTO recommendation_signals(user_id,session_id,request_id,topic_id,occurred_at,context) VALUES(?,?,?,?,?,?)',
                      ('one',row['session_id'],row['request_id'],'artist:the-weeknd',NOW.isoformat(),'{}'))
    repo.conn.execute('INSERT INTO recommendation_processed_jobs VALUES(?,?,?)',('one',row['job_id'],NOW.isoformat()))
    store=RecommendationStore(repo)
    store.dismiss('one','destination:sochi',now=NOW)
    repo.conn.execute("UPDATE recommendation_metadata SET value='1' WHERE name='extractor_version'")
    store=RecommendationStore(repo)
    value=store.feed('one',{},now=NOW)
    assert all(card['kind']!='artist' for card in value['personalized'])
    assert all(card['id']!='destination:sochi' for card in value['personalized']+value['popular'])
    assert repo.conn.execute('SELECT COUNT(*) FROM web_jobs').fetchone()[0]==1
    assert repo.conn.execute('SELECT COUNT(*) FROM turns').fetchone()[0]==1


def test_latest_refusal_with_equal_timestamp_withdraws_vote_and_positive_restores_it(edge_repo):
    repo=edge_repo
    store=RecommendationStore(repo)
    for user in ('one','two','three'):
        store.record_job(seed(repo,user=user,request='zpositive',text='Хочу в Японию'),now=NOW)
    assert any(card['id']=='destination:japan' for card in store.feed('viewer',{},now=NOW)['popular'])
    # The later request sorts before the old one lexically, while event order is later.
    store.record_job(seed(repo,request='arefusal',text='Японию не предлагай, передумал'),now=NOW)
    assert not any(card['id']=='destination:japan' for card in store.feed('viewer',{},now=NOW)['popular'])
    assert not store.feed('one',{},now=NOW)['personalized']
    store.record_job(seed(repo,request='positive-again',text='Хочу в Японию'),now=NOW)
    assert any(card['id']=='destination:japan' for card in store.feed('viewer',{},now=NOW)['popular'])
    assert any(card['id']=='destination:korea' for card in store.feed('one',{},now=NOW)['personalized'])


def test_refusal_classified_as_chat_still_withdraws_interest(edge_repo):
    repo=edge_repo
    store=RecommendationStore(repo)
    store.record_job(seed(repo,request='positive',text='Хочу в Японию',when=NOW-timedelta(hours=1)),now=NOW)
    store.record_job(seed(repo,request='negative',text='Японию не предлагай',response_kind='chat'),now=NOW)
    assert not store.feed('one',{},now=NOW)['personalized']


def test_late_backfill_duration_is_recovered_without_crossing_refusal(edge_repo):
    repo=edge_repo
    store=RecommendationStore(repo)
    initial=seed(repo,request='initial',text='Хочу в Японию на семь ночей',version=1,when=NOW-timedelta(days=2))
    latest=seed(repo,request='latest',text='Бюджет теперь 90 тысяч на всех',version=3)
    # Simulate the initial message falling outside the first bounded batch.
    store.record_job(latest,now=NOW)
    store.record_job(initial,now=NOW)
    value=store.feed('one',{},now=NOW)
    assert '7 ночей' in value['personalized'][0]['prompt']
    rejection=seed(repo,request='middle-refusal',text='Не хочу в Японию',version=2,when=NOW-timedelta(days=1))
    store.record_job(rejection,now=NOW)
    value=store.feed('one',{},now=NOW)
    assert value['personalized']
    assert all('7 ночей' not in card['prompt'] for card in value['personalized'])


def test_duration_keeps_units_through_refinement_and_explicit_reset(edge_repo):
    repo=edge_repo
    store=RecommendationStore(repo)
    store.record_job(seed(repo,request='days',text='Хочу в Японию на неделю',when=NOW-timedelta(hours=3)),now=NOW)
    store.record_job(seed(repo,request='budget',text='Бюджет теперь 90 тысяч на всех',when=NOW-timedelta(hours=2)),now=NOW)
    prompt=store.feed('one',{},now=NOW)['personalized'][0]['prompt']
    assert '7 дней' in prompt and '7 ночей' not in prompt
    store.record_job(seed(repo,request='nights',text='Поеду на три ночи',when=NOW-timedelta(hours=1)),now=NOW)
    prompt=store.feed('one',{},now=NOW)['personalized'][0]['prompt']
    assert '3 ночей' in prompt and '7 дней' not in prompt
    store.record_job(seed(repo,request='unknown',text='Длительность пока не знаю'),now=NOW)
    prompt=store.feed('one',{},now=NOW)['personalized'][0]['prompt']
    assert '3 ночей' not in prompt and '7 дней' not in prompt

import sqlite3,threading
import pytest
from app import store
from app.errors import DomainError

@pytest.fixture
def kept(state,tmp_path,monkeypatch):
    monkeypatch.setenv('DB_PATH',str(tmp_path/'r.db'))
    store.save(state['run_id'],state)
    return state['run_id']

def test_first_signature_adjudicates(kept):
    r=store.review(kept,1,'通过','复核员甲','RETEST-A-1')
    assert r['review_version']==2 and r['decision']=='通过'
    assert r['review_result']=={'outcome':'当前','duplicate':False,'reviewer':'复核员甲','decision':'通过','material':'RETEST-A-1'}
    rows=store.ledger(kept)
    assert len(rows)==1 and rows[0]['outcome']=='当前' and rows[0]['reviewer']=='复核员甲' and rows[0]['material']=='RETEST-A-1' and rows[0]['base_version']==1
    assert store.get(kept)['adjudication']['material']=='RETEST-A-1'

def test_conflict_kept_in_ledger_not_current(kept):
    store.review(kept,1,'通过','复核员甲','RETEST-A-1')
    with pytest.raises(DomainError) as e:store.review(kept,1,'退回','复核员乙','RETEST-B-2')
    assert e.value.status==409
    run=store.get(kept)
    # 当前签发结果不变：样品结论与批次判级只认这一版
    assert run['decision']=='通过' and run['review_version']==2
    rows=store.ledger(kept)
    # 两份记录都有效：原始签字与复检材料不能丢失
    assert [(r['reviewer'],r['decision'],r['material'],r['outcome']) for r in rows]==[('复核员甲','通过','RETEST-A-1','当前'),('复核员乙','退回','RETEST-B-2','留痕')]

def test_conflict_on_current_version_also_recorded(kept):
    store.review(kept,1,'通过','复核员甲','RETEST-A-1')
    with pytest.raises(DomainError) as e:store.review(kept,2,'退回','复核员乙','RETEST-B-2')
    assert e.value.status==409 and e.value.code=='CONFLICT'
    assert store.get(kept)['decision']=='通过' and store.get(kept)['review_version']==2
    assert [r['outcome'] for r in store.ledger(kept)]==['当前','留痕']

def test_same_opinion_retry_adds_no_version(kept):
    store.review(kept,1,'通过','复核员甲','RETEST-A-1')
    again=store.review(kept,1,'通过','复核员甲','RETEST-A-1')
    assert again['review_version']==2 and again['review_result']['duplicate'] is True and again['review_result']['outcome']=='当前'
    assert len(store.ledger(kept))==1

def test_same_material_different_opinion_is_new_record(kept):
    store.review(kept,1,'通过','复核员甲','RETEST-A-1')
    with pytest.raises(DomainError):store.review(kept,1,'退回','复核员甲','RETEST-A-1')
    assert len(store.ledger(kept))==2

def test_simultaneous_submission_keeps_single_current(kept):
    results={}
    def sign(name,decision,material):
        try:results[name]=('ok',store.review(kept,1,decision,name,material)['review_version'])
        except DomainError as e:results[name]=('conflict',e.status)
    a=threading.Thread(target=sign,args=('复核员甲','通过','RETEST-A-1'))
    b=threading.Thread(target=sign,args=('复核员乙','退回','RETEST-B-2'))
    a.start();b.start();a.join();b.join()
    assert sorted(results.values())==[('conflict',409),('ok',2)]
    run=store.get(kept);assert run['review_version']==2
    rows=store.ledger(kept)
    assert len(rows)==2 and sum(r['outcome']=='当前' for r in rows)==1 and sum(r['outcome']=='留痕' for r in rows)==1
    assert run['decision']==next(r['decision'] for r in rows if r['outcome']=='当前')

def test_interrupted_review_keeps_old_version(kept):
    with store.connection() as db:
        db.execute("CREATE TRIGGER break_sign BEFORE INSERT ON signatures BEGIN SELECT RAISE(ABORT,'interrupted'); END")
    with pytest.raises(sqlite3.IntegrityError):store.review(kept,1,'通过','复核员甲','RETEST-A-1')
    run=store.get(kept)
    assert run['review_version']==1 and run['decision']=='待复核' and run['adjudication'] is None
    assert store.ledger(kept)==[]

def test_export_recognizes_only_current_version(client):
    run_id=client.post('/api/runs',json={}).get_json()['run_id']
    r=client.post('/api/runs/'+run_id+'/review',json={'version':1,'decision':'通过','reviewer':'复核员甲','material':'RETEST-A-1'})
    assert r.status_code==200
    assert client.post('/api/runs/'+run_id+'/review',json={'version':1,'decision':'退回','reviewer':'复核员乙','material':'RETEST-B-2'}).status_code==409
    text=client.get('/api/archive/'+run_id+'/export.csv').data.decode('utf-8-sig')
    assert '裁决' in text and '复核员甲' in text and 'RETEST-A-1' in text
    assert '复核员乙' not in text and 'RETEST-B-2' not in text

def test_review_api_accepts_signature_and_material(client):
    run_id=client.post('/api/runs',json={}).get_json()['run_id']
    r=client.post('/api/runs/'+run_id+'/review',json={'version':1,'decision':'通过','reviewer':'复核员甲','material':'RETEST-A-1'})
    assert r.status_code==200 and r.get_json()['review_result']['outcome']=='当前'
    rows=client.get('/api/runs/'+run_id+'/signatures').get_json()
    assert len(rows)==1 and rows[0]['material']=='RETEST-A-1'

def test_review_api_retry_same_opinion(client):
    run_id=client.post('/api/runs',json={}).get_json()['run_id']
    body={'version':1,'decision':'通过','reviewer':'复核员甲','material':'RETEST-A-1'}
    client.post('/api/runs/'+run_id+'/review',json=body)
    r=client.post('/api/runs/'+run_id+'/review',json=body)
    assert r.status_code==200 and r.get_json()['review_result']['duplicate'] is True
    assert len(client.get('/api/runs/'+run_id+'/signatures').get_json())==1

def test_workbench_shows_signature_ledger(client):
    run_id=client.post('/api/runs',json={}).get_json()['run_id']
    client.post('/api/runs/'+run_id+'/review',json={'version':1,'decision':'通过','reviewer':'复核员甲','material':'RETEST-A-1'})
    client.post('/api/runs/'+run_id+'/review',json={'version':1,'decision':'退回','reviewer':'复核员乙','material':'RETEST-B-2'})
    page=client.get('/m7_archive').data.decode()
    assert '签字账' in page and '复核员甲' in page and '复核员乙' in page and 'RETEST-B-2' in page and '留痕' in page and '当前' in page

def test_business_and_grades_untouched_by_recorded_conflict(kept):
    before=store.get(kept)
    store.review(kept,1,'通过','复核员甲','RETEST-A-1')
    with pytest.raises(DomainError):store.review(kept,1,'退回','复核员乙','RETEST-B-2')
    after=store.get(kept)
    assert after['business']==before['business'] and after['reconcile']['grades']==before['reconcile']['grades'] and after['calculations']==before['calculations']

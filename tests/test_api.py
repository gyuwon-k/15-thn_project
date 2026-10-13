"""Persistence/API integration for confirmation, metadata and revision guards."""
import copy
import pytest
from fastapi.testclient import TestClient
from backend import domain, store
from backend.main import app
from test_workflow import fixture_state, no_collision


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(store,'DATA_ROOT',tmp_path)
    store.init()
    s=fixture_state()
    store.save(s)
    return TestClient(app),s


def test_confirm_idempotent_and_confirmed_cannot_become_editable(setup):
    client,s=setup
    s['validation']=domain.validate(s,no_collision)
    store.save(s)
    base=f"/api/projects/{s['project']['id']}/actions/"
    for _ in range(2):
        response=client.post(base+'confirm',json={'reviewer':'합성검토자'})
        assert response.status_code==200,response.text
    assert len(store.snapshots(s['project']['id'],True))==1
    for action,body in [('validate',{}),('generate',{}),('save_plan',{'revision':1,'ordered_operation_ids':[]}),('update_operation',{'id':'op-a','title':'mutated'})]:
        assert client.post(base+action,json=body).status_code==409
    assert store.get(s['project']['id'])['plan']['status']=='confirmed'
    assert client.post(base+'new_draft',json={'reason':'시험 개정'}).status_code==200
    assert len(store.snapshots(s['project']['id'],True))==1


def test_metadata_change_requires_revalidation_and_preserves_strings(setup):
    client,s=setup
    base=f"/api/projects/{s['project']['id']}"
    csv='id,company_part_no,display_name\npart-a,000123,한글 부품\n'
    response=client.post(base+'/metadata',files={'file':('정보.csv',csv.encode('utf-8'),'text/csv')})
    assert response.status_code==200,response.text
    assert response.json()['parts'][0]['company_part_no']=='000123'
    assert response.json()['operations'][0]['part_no']=='000123'
    revision=response.json()['plan']['revision']
    conflict=client.post(base+'/actions/save_plan',json={'revision':revision-1,'ordered_operation_ids':[]})
    assert conflict.status_code==409
    response=client.post(base+'/actions/update_part',json={'id':'part-a','company_part_no':''})
    assert response.status_code==200
    assert any(f['check_type']=='MISSING_PART_METADATA' for f in domain.structural_findings(store.get(s['project']['id'])))


def test_target_change_does_not_inherit_old_torque(setup):
    client,s=setup
    s['operations'][0]['torque']={'min':1,'max':2,'unit':'Nm','source_ref':'test'}
    store.save(s)
    response=client.post(f"/api/projects/{s['project']['id']}/actions/update_operation",json={'id':'op-a','target_unit_ids':['b']})
    assert response.status_code==200
    changed=response.json()['operations'][0]
    assert changed['part_no']=='000-b'
    assert changed['torque'] is None
    assert changed['review_status']=='review_required'
    assert changed['warning']['status']=='missing'


def test_api_rejects_remote_origin_and_host(setup):
    client,_=setup
    assert client.get('/api/health',headers={'origin':'https://outside.example'}).status_code==403
    assert client.get('/api/health',headers={'host':'outside.example'}).status_code==403


def test_units_duplicate_membership_is_rejected(setup):
    client,s=setup
    units=copy.deepcopy(s['units'])
    units[1]['member_instance_ids']=units[0]['member_instance_ids'][:]
    response=client.post(f"/api/projects/{s['project']['id']}/actions/save_units",json={'units':units})
    assert response.status_code==422


def test_pending_worker_cannot_overwrite_new_confirmation(setup):
    from backend.main import commit_result
    client,s=setup
    s['validation']=domain.validate(s,no_collision)
    store.save(s)
    initial=copy.deepcopy(s)
    candidate=copy.deepcopy(s)
    candidate['plan']['ordered_operation_ids'].reverse()
    base=f"/api/projects/{s['project']['id']}/actions/"
    assert client.post(base+'confirm',json={'reviewer':'합성 시험 검토자'}).status_code==200
    with pytest.raises(domain.DomainError,match='계산 도중'):
        commit_result(s['project']['id'],initial,candidate,'generate')
    assert store.get(s['project']['id'])['plan']['status']=='confirmed'


def test_incomplete_torque_source_cannot_be_confirmed(setup):
    _,s=setup
    s['operations'][0].update(kind='fasten',torque={'min':2,'max':1,'unit':'','source_ref':''})
    validation=domain.validate(s,no_collision)
    assert validation['status']=='review_required'
    assert any(f['check_type']=='INVALID_TORQUE' for f in validation['findings'])

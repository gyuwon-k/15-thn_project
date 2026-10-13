"""Major feature acceptance: constraints, state search, complete plans, immutable revisions."""
import copy
import pytest
from backend import domain, store


def fixture_state():
    state = domain.new_project(dict(name='합성 기능 시험',vehicle_model='TEST',item_name='Synthetic',product_no='0001-한글'))
    state['project']['status'] = 'ready'
    for key in ['a','b','c']:
        state['parts'].append(dict(id='part-'+key,company_part_no='000-'+key,display_name=key))
        state['instances'].append(dict(id='cad-'+key))
        state['units'].append(dict(id=key,name=key,part_definition_id='part-'+key,mapping_status='confirmed',member_instance_ids=['cad-'+key]))
        state['operations'].append(dict(id='op-'+key,title=key,kind='place',target_unit_ids=[key],direction='+Z',duration_s=1,
                                         part_no='000-'+key,part_name=key,review_status='reviewed',warning=dict(status='explicit_none',text='')))
    state['plan']['ordered_operation_ids'] = [o['id'] for o in state['operations']]
    state['baseline'] = list(state['plan']['ordered_operation_ids'])
    return state


def no_collision(op, assembled):
    return [domain.make_finding('MOTION','synthetic clear',op['id'],'passed')]


def test_AT17_AT19_constraints_and_reference_errors():
    s = fixture_state()
    s['rules'] = [dict(id='r',before_operation_id='op-b',after_operation_id='op-a',strength='hard')]
    assert any(f['check_type']=='PRECEDENCE_VIOLATION' for f in domain.structural_findings(s))
    generated = domain.generate(s)
    assert generated['ordered_operation_ids'].index('op-b') < generated['ordered_operation_ids'].index('op-a')
    s['rules'].append(dict(id='cycle',before_operation_id='op-a',after_operation_id='op-b'))
    with pytest.raises(domain.DomainError, match='순환'):
        domain.generate(s)
    s['rules'] = [dict(id='bad',before_operation_id='missing',after_operation_id='op-a')]
    with pytest.raises(domain.DomainError, match='없는 작업'):
        domain.generate(s)


def test_AT20_state_search_backtracks_when_baseline_blocks_path():
    s = fixture_state()
    def path(op, installed):
        return [domain.make_finding('MOTION','blocked by A',op['id'],'collision' if op['id']=='op-b' and 'a' in installed else 'passed')]
    proposal = domain.generate(s, path)
    assert proposal['geometry_checked']
    assert proposal['ordered_operation_ids'].index('op-b') < proposal['ordered_operation_ids'].index('op-a')


def test_AT23_AT25_full_manual_and_instance_count_not_enough():
    s = fixture_state()
    s['plan']['ordered_operation_ids'] = []
    assert sum(f['check_type']=='MISSING_OPERATION' for f in domain.structural_findings(s)) == 3
    s['plan']['ordered_operation_ids'] = ['op-a','op-b','op-c']
    s['operations'][1]['target_unit_ids'] = ['a']
    findings = domain.structural_findings(s)
    assert any(f['check_type']=='DUPLICATE_INSTALL' for f in findings)
    assert any(f['check_type']=='MISSING_UNIT' and f['unit_id']=='b' for f in findings)


def test_AT15_AT26_AT27_stale_validation_and_immutable_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(store,'DATA_ROOT',tmp_path)
    store.init()
    s = fixture_state()
    s['validation'] = domain.validate(s,no_collision)
    assert s['validation']['status'] == 'validated'
    snap = domain.make_snapshot(s,'테스트 검토자','합성 시험 데이터')
    store.save(s)
    store.put_snapshot(snap,True,{'reason':'test'})
    original = store.snapshot(snap['id'])
    s['operations'][0]['title'] = '다음 개정의 새 문구'
    assert store.snapshot(snap['id'])['operations'][0]['title'] == original['operations'][0]['title']
    with pytest.raises(domain.DomainError, match='현재 개정'):
        domain.make_snapshot(s,'reviewer')
    s['validation'] = domain.validate(s,lambda o,a:[domain.make_finding('MOTION','collision',o['id'],'collision')])
    with pytest.raises(domain.DomainError, match='미해결'):
        domain.make_snapshot(s,'reviewer')


def test_AT17_connection_does_not_insert_again_AT28_timeline():
    s = fixture_state()
    connection = dict(id='connect',title='연결',kind='mate_connector',target_unit_ids=['a'],duration_s=2,direction='+Z',review_status='reviewed',warning={'status':'explicit_none'})
    s['operations'].append(connection)
    s['plan']['ordered_operation_ids'].append('connect')
    assert not any(f['check_type']=='DUPLICATE_INSTALL' for f in domain.structural_findings(s))
    timeline = domain.build_timeline(s)
    assert timeline['duration_s']==5
    assert timeline['segments'][-1]['start_s']==3


def test_AT46_restarted_job_reports_interruption(tmp_path, monkeypatch):
    monkeypatch.setattr(store,'DATA_ROOT',tmp_path)
    store.init()
    s = fixture_state()
    store.save(s)
    store.put_job(dict(id='job',project_id=s['project']['id'],kind='render',status='running',created_at=store.now()))
    store.init()
    assert store.jobs(s['project']['id'])[0]['error']=='INTERRUPTED'
    assert store.get(s['project']['id'])['project']['product_no']=='0001-한글'

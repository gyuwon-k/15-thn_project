import copy
import pytest
from backend import domain


def minimal():
    s=domain.new_project({'name':'Independent input'})
    s['units']=[{'id':'one','member_instance_ids':['source-one']},{'id':'two','member_instance_ids':['source-two']}]
    s['operations']=[{'id':'install-one','kind':'place','direction':'+Z','target_unit_ids':['one'],'duration_s':2},
        {'id':'install-two','kind':'place','direction':'+Z','target_unit_ids':['two'],'duration_s':2}]
    s['plan']['ordered_operation_ids']=[o['id'] for o in s['operations']]
    return s


def test_reference_order_does_not_rank_generated_sequence():
    state=minimal();state['baseline']=list(reversed(state['plan']['ordered_operation_ids']))
    a=domain.generate(state)['ordered_operation_ids']
    state['baseline'].reverse()
    assert domain.generate(state)['ordered_operation_ids']==a


def test_exact_move_reports_rule_without_mutation():
    s=minimal();s['rules']=[{'id':'r','before_operation_id':'install-one','after_operation_id':'install-two','strength':'hard'}]
    before=copy.deepcopy(s)
    result=domain.preview_move(s,'install-two',0)
    assert not result['allowed'] and result['violations'][0]['id']=='r' and s==before
    assert result['geometry_status']=='not_checked'


def test_tool_identity_counts_socket_change_and_first_preparation_separately():
    s=minimal();s['tools']=[{'id':'drive1','assembly_id':'driver+10','type':'powered','change_time_s':5},
        {'id':'drive2','assembly_id':'driver+12','type':'powered','change_time_s':8}]
    s['operations'][0]['tool_id']='drive1';s['operations'][1]['tool_id']='drive2'
    s['operations'].insert(1,{'id':'inspect','kind':'inspect','duration_s':1})
    s['plan']['ordered_operation_ids'].insert(1,'inspect')
    m=domain.plan_metrics(s)
    assert m['tool_switch_count']==1 and m['tool_change_time_s']==8 and m['initial_preparation_s']==5


def test_temporary_and_final_fastening_retain_single_occurrence():
    op={'kind':'fasten','fastening_stage':'final'}
    assert not domain.installs(op)
    op['fastening_stage']='temporary';assert domain.installs(op)


def test_invalid_operation_rejects_unknown_target_and_nonfinite_torque():
    s=minimal();op=copy.deepcopy(s['operations'][0]);op['target_unit_ids']=['missing']
    with pytest.raises(domain.DomainError,match='대상'):domain.validate_operation(s,op)
    op=s['operations'][0]|{'torque':{'min':float('nan')}}
    with pytest.raises(domain.DomainError,match='토크'):domain.validate_operation(s,op)


def test_asap_path_current_after_field_metadata_but_stale_after_install_reorder():
    s=minimal();s['plan'].update(asap_run_id='run',asap_order_fingerprint=domain.fingerprint(s['plan']['ordered_operation_ids']))
    for op in s['operations']:
        u=op['target_unit_ids'][0]
        op['asap_motion']={'verified':True,'unit_id':u,'original_direction':'+Z','original_members':[{'occurrence_id':'source-'+u}]}
    domain.refresh_asap_validity(s);assert not s['plan']['asap_path_stale']
    s['operations'][0]['title']='Reviewed title';domain.refresh_asap_validity(s)
    assert not s['plan']['asap_path_stale']
    s['operations'][1]['kind']='prepare';domain.refresh_asap_validity(s)
    assert s['plan']['asap_path_stale']
    s['operations'][1]['kind']='place';domain.refresh_asap_validity(s)
    assert not s['plan']['asap_path_stale']
    s['plan']['ordered_operation_ids'].reverse();domain.refresh_asap_validity(s)
    assert s['plan']['asap_path_stale']
    with pytest.raises(domain.DomainError,match='ASAP'):domain.make_snapshot(s,draft=True)

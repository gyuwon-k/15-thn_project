import unittest
from backend.process_rules import compile_process_rules,process_eligibility,height_ordered_fasteners


def fixture():
    names=[('base','housing',0),('plate','plate',1),('high','fastener',3),('equal','fastener',3.04),('low','fastener',2),('relay','relay',4)]
    units=[{'id':name,'name':name,'role':role,'fixed':name=='base','occurrence_ids':['o_'+name],'bbox':{'center':[0,0,height],'min':[0,0,height-.1],'max':[1,1,height+.1],'size':[1,1,.2]}} for name,role,height in names]
    return {'units':units,'occurrences':[{'id':'o_'+name,'representation_uncertainty_mm':.005} for name,_,_ in names]}


def rules():
    return {'process_policy':{'require_fixing_before_next_component':True,'required_unit_ids':['plate']},
        'process_groups':[{'id':'plate-fixing','target_unit_id':'plate','fastener_unit_ids':['high','equal','low'],'ordered_fastener_groups':[['high','equal'],['low']],
        'status':'confirmed','basis':'test mounting drawing','fixation_mode':'separate_fasteners'}]}


class ProcessRulesTests(unittest.TestCase):
    def test_component_must_finish_fixing_before_next_component(self):
        model=fixture();compiled=compile_process_rules(model,rules(),{'base'},{'plate','high','equal','low','relay'})
        allowed,reasons=process_eligibility({'high','equal','low','relay'},{'base','plate'},compiled)
        self.assertEqual(allowed,{'high','equal'})
        self.assertEqual(process_eligibility({'low','relay'},{'base','plate','high','equal'},compiled)[0],{'low'})
        self.assertEqual(reasons['relay']['code'],'PROCESS_FIXING_REQUIRED')
        self.assertIn(('high','low'),compiled['edges']);self.assertIn(('equal','low'),compiled['edges'])
        allowed,_=process_eligibility({'relay'},{'base','plate','high','equal','low'},compiled)
        self.assertEqual(allowed,{'relay'})
    def test_missing_required_assignment_blocks_placement(self):
        value=rules();value['process_groups']=[]
        compiled=compile_process_rules(fixture(),value,{'base'},{'plate','relay'})
        allowed,reasons=process_eligibility({'plate','relay'},{'base'},compiled)
        self.assertNotIn('plate',allowed);self.assertIn('relay',allowed)
        self.assertEqual(reasons['plate']['code'],'PROCESS_FASTENER_ASSOCIATION_REVIEW_REQUIRED')
    def test_fastener_cannot_be_assigned_to_two_targets(self):
        value=rules();value['process_groups'].append(dict(value['process_groups'][0],id='relay-fixing',target_unit_id='relay'))
        compiled=compile_process_rules(fixture(),value,{'base'},{'plate','relay','high','equal','low'})
        self.assertEqual(compiled['errors'][0]['code'],'FASTENER_ASSIGNED_TO_MULTIPLE_COMPONENTS')
    def test_higher_first_equal_height_is_unordered_batch(self):
        result=height_ordered_fasteners(fixture(),['low','equal','high'])
        self.assertEqual(set(result[0]),{'high','equal'});self.assertEqual(result[1],['low'])
    def test_self_fixed_requires_explicit_basis(self):
        value={'process_policy':{'require_fixing_before_next_component':True,'required_unit_ids':['plate']},'process_groups':[
            {'target_unit_id':'plate','status':'confirmed','fixation_mode':'self_fixed','basis':'integrated snap latch','fastener_unit_ids':[]}]}
        compiled=compile_process_rules(fixture(),value,{'base'},{'plate','relay'})
        allowed,_=process_eligibility({'plate','relay'},{'base'},compiled)
        self.assertIn('plate',allowed);self.assertFalse(compiled['reviews'])
    def test_disabled_policy_does_not_change_old_sequence(self):
        value=rules();value['process_policy']['require_fixing_before_next_component']=False
        compiled=compile_process_rules(fixture(),value,{'base'},{'plate','relay','high','equal','low'})
        self.assertFalse(compiled['edges'])
        self.assertEqual(process_eligibility({'relay'},{'base','plate'},compiled)[0],{'relay'})
    def test_unassigned_terminal_fastener_cannot_be_placed_before_component(self):
        value=rules();value['process_groups'][0]['fastener_unit_ids']=['high','equal'];value['process_groups'][0]['ordered_fastener_groups']=[['high','equal']]
        compiled=compile_process_rules(fixture(),value,{'base'},{'plate','relay','high','equal','low'})
        allowed,reasons=process_eligibility({'plate','low'},{'base'},compiled)
        self.assertEqual(allowed,{'plate'})
        self.assertEqual(reasons['low']['code'],'PROCESS_FASTENER_ASSOCIATION_REVIEW_REQUIRED')
    def test_editable_assumption_is_not_recorded_as_physical_confirmation(self):
        value={'process_policy':{'require_fixing_before_next_component':True,'required_unit_ids':['plate']},'process_groups':[
            {'target_unit_id':'plate','status':'assumed','assumption':True,'requires_field_confirmation':True,'fixation_mode':'cover_seated','retaining_cover_unit_id':'relay','basis':'user requested planning assumption','fastener_unit_ids':[]}]}
        compiled=compile_process_rules(fixture(),value,{'base'},{'plate','relay'})
        self.assertIn('plate',process_eligibility({'plate','relay'},{'base'},compiled)[0])
        self.assertEqual(compiled['groups']['plate']['status'],'assumed')
        self.assertTrue(compiled['groups']['plate']['requires_field_confirmation'])
        self.assertNotIn('confirmed',compiled['groups']['plate'])
        self.assertIn(('plate','relay'),compiled['edges'])
    def test_cover_seated_requires_valid_subsequent_retainer(self):
        value=rules();value['process_groups']=[{'target_unit_id':'plate','status':'confirmed','basis':'retained by subsequent cover','fixation_mode':'cover_seated'}]
        compiled=compile_process_rules(fixture(),value,{'base'},{'plate','relay'})
        self.assertEqual(compiled['errors'][0]['code'],'INVALID_RETAINING_COVER')
        value['process_groups'][0]['retaining_cover_unit_id']='base'
        compiled=compile_process_rules(fixture(),value,{'base'},{'plate','relay'})
        self.assertEqual(compiled['errors'][0]['code'],'INVALID_RETAINING_COVER')


if __name__=='__main__':unittest.main()

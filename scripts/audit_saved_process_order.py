"""Audit every saved card's process order, independently of executable motion."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.planning import _normalise
from backend.process_rules import process_eligibility,height_ordered_fasteners


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    raw=json.loads(args.model.read_text(encoding='utf-8'));model=raw.get('result',raw)
    saved=json.loads(args.plan.read_text(encoding='utf-8'));plan=saved['plan'];rules=saved['rules']
    units,initial,targets,pred,errors,process=_normalise(model,rules)
    installed=set(initial);violations=list(errors);cards=[]
    positions={s['unit_id']:index for index,s in enumerate(plan['steps'])}
    for index,step in enumerate(plan['steps'],1):
        uid=step['unit_id'];allowed,reasons=process_eligibility(targets-installed,installed,process)
        missing=sorted(pred[uid]-installed)
        if missing:violations.append({'index':index,'code':'PRECEDENCE_VIOLATION','missing':missing})
        if uid not in allowed:violations.append({'index':index,**reasons.get(uid,{'code':'PROCESS_INELIGIBLE'})})
        cards.append({'index':index,'unit_id':uid,'name':step['name'],'status':step['status'],'executable':step['executable'],
            'fixes_component_id':process['fastener_owners'].get(uid),'predecessors_satisfied':not missing,'process_eligible':uid in allowed})
        # This is an abstract ordering audit, never installation evidence.
        installed.add(uid)
    groups=[]
    for target,group in process['groups'].items():
        batches=height_ordered_fasteners(model,group['fastener_unit_ids'])
        valid=all(max(positions[x] for x in earlier)<min(positions[x] for x in later) for earlier,later in zip(batches,batches[1:]))
        if not valid:violations.append({'code':'HEIGHT_BATCH_ORDER_VIOLATION','target_unit_id':target})
        groups.append({'target_unit_id':target,'target_name':units[target]['name'],'status':group['status'],
            'fastener_count':len(group['fastener_unit_ids']),'same_height_unordered_batches':batches,'higher_batch_before_lower':valid,
            'requires_field_confirmation':group.get('requires_field_confirmation',False)})
    result={'model_fingerprint':model['fingerprint'],'scope':'Abstract card order only. Unverified placements are not execution or physical fixing evidence.',
        'card_count':len(cards),'coverage_complete':set(positions)==targets and len(cards)==len(targets),'initial_unit_names':[units[x]['name'] for x in initial],
        'status':'PASS' if not violations else 'FAIL','violations':violations,'groups':groups,'cards':cards}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('status','card_count','coverage_complete','violations')},ensure_ascii=True))


if __name__=='__main__':main()

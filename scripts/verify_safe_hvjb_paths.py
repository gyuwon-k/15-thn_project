"""Check real saved CAD against its actual fixed housing; no exclusions."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.planning import GeometrySession,_action,_default_distance,_directions_for_unit,generate_plan,validate_plan


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seconds',type=float,default=300)
    parser.add_argument('--unit',action='append')
    parser.add_argument('--installed-unit',action='append',help='Additional physically installed unit ID or unique name for entry diagnostics.')
    parser.add_argument('--direction',action='append')
    parser.add_argument('--generate',action='store_true')
    parser.add_argument('--probe-only',action='store_true')
    parser.add_argument('--process-profile',action='store_true')
    parser.add_argument('--validate',action='store_true')
    args=parser.parse_args()
    data=json.loads(args.model.read_text(encoding='utf-8'));model=data.get('result',data)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    def progress(event):print(json.dumps(event,ensure_ascii=True),flush=True)
    if args.generate:
        rules={'directions':['+X','-X','+Y','-Y','+Z','-Z'],'max_seconds':args.seconds,'validation_seconds':args.seconds,'beam_width':3}
        if args.process_profile:
            from backend.process_rules import recommend_process_rules
            proposal=recommend_process_rules(model)
            rules.update({key:proposal[key] for key in ('process_policy','process_groups','precedence')})
            args.output.with_name(args.output.stem+'-profile.json').write_text(json.dumps(proposal,ensure_ascii=False,indent=2),encoding='utf-8')
        plan=generate_plan(model,rules,progress=progress)
        result={'model_fingerprint':model['fingerprint'],'rules':rules,'plan':plan}
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        if args.validate:
            result['validation']=validate_plan(model,plan,rules,progress=progress)
            args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        assert plan['coverage']['complete']
        prefix=plan['playback']['executable_prefix_count']
        assert all(s['executable'] and s['path']['verified'] for s in plan['steps'][:prefix])
        assert all(not s['executable'] and s['path'] is None for s in plan['steps'][prefix:])
        print(json.dumps({'status':plan['status'],'playback':plan.get('playback'),'steps':len(plan['steps']),'search':plan['search']},ensure_ascii=True),flush=True)
        return
    units={u['id']:u for u in model['units']};fixed={u['id'] for u in units.values() if u.get('fixed')}
    for requested in args.installed_unit or []:
        matches=[u['id'] for u in units.values() if requested in (u['id'],u['name'])]
        if len(matches)!=1:raise ValueError('Installed unit must uniquely identify a physical unit: '+requested)
        fixed.add(matches[0])
    requested_units=args.unit or ['BUS-KD300','BUS-KD000','BUS-KD100','BUS-KD200','PLT-KD000']
    selected=[u for u in units.values() if u['name'] in requested_units or u['id'] in requested_units]
    evidence={'fixed_units':list(fixed),'model_fingerprint':model['fingerprint'],'tests':[],'whole_product_verified':False}
    with GeometrySession(model,timeout=20) as engine:
        for unit in selected:
            if args.probe_only:
                started=time.monotonic();directions=_directions_for_unit(unit,model,['+X','-X','+Y','-Y','+Z','-Z'],engine,fixed,started+args.seconds)
                row={'name':unit['name'],'ranked_directions':directions,'elapsed_seconds':time.monotonic()-started,'certification':False}
                evidence['tests'].append(row);progress(row);args.output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8');continue
            for direction in (args.direction or ['-Z','+X']):
                started=time.monotonic()
                result=_action(unit,fixed,units,engine,direction,_default_distance(model),started+args.seconds)
                row={'unit_id':unit['id'],'name':unit['name'],'direction':direction,'elapsed_seconds':time.monotonic()-started,**result}
                evidence['tests'].append(row);progress(row)
                args.output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()

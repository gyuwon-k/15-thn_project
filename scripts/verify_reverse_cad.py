"""Read an existing job input without mutating it; compare on isolated work files."""
import argparse,json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.jobs import path_values,atomic_json
from backend.planning import generate_plan,validate_plan
from backend.planning_settings import compatible_rules

def main():
    parser=argparse.ArgumentParser();parser.add_argument('request',type=Path);parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--seconds',type=float,default=1800);parser.add_argument('--compare',action='store_true');args=parser.parse_args()
    request=json.loads(args.request.read_text(encoding='utf-8'))
    project=path_values(request['payload'],Path(request['data_dir']))['project']
    args.out.mkdir(parents=True,exist_ok=True);report={'project_name':project.get('name'),'cases':[]}
    for method in (['forward','disassembly_reverse'] if args.compare else ['disassembly_reverse']):
        rules={**compatible_rules(project['rules'])[0],'max_seconds':args.seconds,'validation_seconds':args.seconds,'generation_method':method}
        previous=[None,0]
        published=[None,0.]
        def progress(event):
            key=(event.get('stage'),event.get('current'))
            tick=time.monotonic()
            if event.get('stage')!=published[0] or tick-published[1]>=.25:
                atomic_json(args.out/'progress.json',{'method':method,**event});published[:]=[event.get('stage'),tick]
            if key!=previous[0] and time.monotonic()-previous[1]>8:
                print(json.dumps({'method':method,**event},ensure_ascii=False),flush=True);previous[:]=[key,time.monotonic()]
        started=time.monotonic();plan=generate_plan(project['model'],rules,progress=progress)
        atomic_json(args.out/(method+'-plan.json'),plan)
        validation=validate_plan(project['model'],plan,rules,progress=progress) if plan.get('replace_plan',bool(plan.get('steps'))) else {'status':'NOT_RUN'}
        atomic_json(args.out/(method+'-validation.json'),validation)
        row={'method':method,'unit_count':len(project['model']['units']),'steps':len(plan['steps']),
            'generation_status':plan['status'],'validation_status':validation['status'],'search':plan.get('search'),
            'placement_status':plan.get('placement_audit',{}).get('status'),'removal_count':plan.get('disassembly',{}).get('removed_count'),
            'total_seconds':round(time.monotonic()-started,3),'playback':validation.get('playback'),'support':plan.get('support'), 'rules':rules}
        report['cases'].append(row);atomic_json(args.out/'report.json',report);print(json.dumps({k:v for k,v in row.items() if k not in ("support","rules")},ensure_ascii=False),flush=True)

if __name__=='__main__':main()

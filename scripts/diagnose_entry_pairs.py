import argparse,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.planning import GeometrySession,make_path,_default_distance


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--model',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--unit',default='PLT-KD000');parser.add_argument('--direction',default='-Z');parser.add_argument('--seconds',type=float,default=40);args=parser.parse_args()
    raw=json.loads(args.model.read_text(encoding='utf-8'));model=raw.get('result',raw);rows={o['id']:o for o in model['occurrences']}
    moving=next(u for u in model['units'] if u['name']==args.unit);fixed=[u for u in model['units'] if u.get('fixed')]
    report={'model_fingerprint':model['fingerprint'],'unit':moving['id'],'direction':args.direction,'scope':'Per-member diagnostic; no members are omitted from the real assembly unit.','pairs':[]}
    with GeometrySession(model,timeout=args.seconds) as engine:
        for a in moving['occurrence_ids']:
            for obstacle in fixed:
                for b in obstacle['occurrence_ids']:
                    started=time.monotonic();verdict=engine.path_pair(a,b,make_path(args.direction,_default_distance(model)),started+args.seconds)
                    record={'a':a,'a_name':rows[a]['name'],'b':b,'b_name':rows[b]['name'],'elapsed_seconds':time.monotonic()-started,**verdict}
                    report['pairs'].append(record);print(json.dumps(record,ensure_ascii=True),flush=True)
                    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()

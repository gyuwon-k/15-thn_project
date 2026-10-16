"""Regress every real fastener affected by the former 16-partition limit."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.geometry import repair_occurrences,REPAIR_VERSION

def main():
 p=argparse.ArgumentParser();p.add_argument('--cache',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 document=json.loads(next(a.cache.glob('3048*/manifest.json')).read_text(encoding='utf-8'))
 document['instances']=[r for r in document['instances'] if r['name'].startswith(('M5X20.','M6X20.'))]
 assert len(document['instances'])==31
 result=repair_occurrences(document,a.cache,lambda row:print(json.dumps(row,ensure_ascii=True),flush=True))
 rows=[{'id':r['id'],'name':r['name'],'exact_valid':r.get('exact_method_valid'),'collision_ready':r['collision_ready'],'repair':r['repair']} for r in result['instances']]
 summary={'repair_version':REPAIR_VERSION,'total':len(rows),'passed':sum(r['exact_valid'] is True for r in rows),'occurrences':rows}
 a.output.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps({k:v for k,v in summary.items() if k!='occurrences'}),flush=True)
 assert summary['passed']==31 and all(r['collision_ready'] for r in rows)

if __name__=='__main__':main()

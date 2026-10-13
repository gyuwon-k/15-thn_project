"""A single rigid body's native preprocessing runs outside the search process."""
from pathlib import Path
import argparse,json,sys,locale,faulthandler,os,ctypes
parser=argparse.ArgumentParser();parser.add_argument('--request',required=True);parser.add_argument('--output',required=True)
args=parser.parse_args();root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
faulthandler.enable();locale.setlocale(locale.LC_ALL,'C')
if os.name=='nt':ctypes.windll.kernel32.SetErrorMode(0x0001|0x0002)
request=json.loads(Path(args.request).read_text(encoding='utf-8'))
from engine.bridge import prepare_engine_path,native_guard_provenance,write_json,emit
try:
    prepare_engine_path(Path(request['engine_root']))
    import redmax_py
    native_guard_provenance(Path(request['engine_root']),redmax_py.__file__)
    from engine.solid_sdf import prepare_caches
    def progress(stage,message,**fields):emit(stage,message,unit_part=request['part'],**fields)
    result=prepare_caches(request['assembly_dir'],request['asset_folder'],request['parameters'],
        deadline=request['deadline'],audit_path=Path(request['audit_path']),emit=progress,
        expected_revision=request['expected_revision'],expected_source_sha256=request['source_sha256'],
        scale=request['scale'],native_provenance=request['native_provenance'],
        verified_cache_reuse=request.get('verified_cache_reuse'))
    write_json(args.output,{'ok':True,'report':result})
except Exception as exc:
    import traceback
    traceback.print_exc()
    write_json(args.output,{'ok':False,'reason_code':getattr(exc,'reason_code','NATIVE_PREFLIGHT_ERROR'),'message':str(exc)})
    sys.exit(1)

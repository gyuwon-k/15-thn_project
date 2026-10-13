"""Bounded diagnostic, never publishes an assembly order or waives a collision."""
import importlib.util, json, sys, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('hvjb_bridge',ROOT/'asap_service/engine/bridge.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
old=ROOT.parent/'asap-unit-test/data/sessions/cf6717a8601e4deb9fa7a9e5cdb25bac/runs/088f431e4d5b49b089d132169dadd0dc'
result=b.read_json(old/'result.json')
stage=Path(result['provenance']['native_ascii_staging'])
engine=ROOT/'dist/PILOT-Studio/_internal/runtime/asap/ASAP'
b.prepare_engine_path(engine)
import redmax_py
import numpy as np
adapter=b.PhysicsParameterAdapter(result['settings']['physics_profile'],b.read_json(old/'coordinate_map.json'))
adapter.install();adapter.bind_solid_caches(stage, sorted(result['provenance']['original_solid_sdf']['bodies']),result['provenance']['original_solid_sdf'])
adapter.install_timing()
from plan_sequence.physics_planner import MultiPartPathPlanner
from plan_sequence.feasibility_check import get_R3_actions
from plan_sequence.stable_pose import get_stable_poses,get_combined_mesh
parts=sorted(adapter.report['original_solid_cache_contract'] and result['provenance']['original_solid_sdf']['bodies'])
poses=get_stable_poses(get_combined_mesh(stage,parts),max_num=2)
pose=poses[0]
report={'diagnostic_only':True,'direction_wall_time_s':3,'pose':pose.tolist(),'units':len(parts),'candidates':[]}
try:
    for moving in sys.argv[1:] or ['00047','00003']:
        t=time.monotonic()
        planner=MultiPartPathPlanner(str(engine/'assets'),str(stage),[p for p in parts if p!=moving],moving,pose=pose,save_sdf=True)
        constructor=time.monotonic()-t
        planner.max_time=3
        directions=[]
        for action in get_R3_actions():
            t=time.monotonic();success,path=planner.check_success(action,return_path=True)
            positions=np.asarray(path)[:,:3]
            directions.append({'direction':action.tolist(),'success':bool(success),'elapsed_s':time.monotonic()-t,'samples':len(path),
                'net_translation_engine':(positions[-1]-positions[0]).tolist(),'maximum_translation_engine':float(np.max(np.linalg.norm(positions-positions[0],axis=1)))})
        report['candidates'].append({'moving_part':moving,'constructor_s':constructor,'directions':directions})
        b.write_json(ROOT/'evidence/hvjb-final/physics-probe.json',report)
finally:
    adapter.close()

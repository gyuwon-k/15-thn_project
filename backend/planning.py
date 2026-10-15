"""Independent rigid translation planning with bounded native geometry work.

All numerical verdicts are relative to the declared tolerance. A sampled path
never becomes PASS: intervals need a Lipschitz clearance certificate or a
validated complete planar-face sweep. Display visibility has no physical role.
"""
from __future__ import annotations
import hashlib
import json
import math
import multiprocessing as mp
import queue
import time
from pathlib import Path

ALGORITHM_VERSION="pilot-sequence-2.0.2"
AXES={"+X":[1,0,0],"-X":[-1,0,0],"+Y":[0,1,0],"-Y":[0,-1,0],"+Z":[0,0,1],"-Z":[0,0,-1]}
LINEAR_TOL=1e-5
VOLUME_TOL=1e-5


def _json_hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()


def _bbox_union(boxes):
    boxes=list(boxes)
    if not boxes:return None
    low=[min(b["min"][i] for b in boxes) for i in range(3)]
    high=[max(b["max"][i] for b in boxes) for i in range(3)]
    return {"min":low,"max":high,"size":[b-a for a,b in zip(low,high)],"center":[(a+b)/2 for a,b in zip(low,high)]}


def _offset_box(box,offset):
    return {"min":[box["min"][i]+offset[i] for i in range(3)],"max":[box["max"][i]+offset[i] for i in range(3)]}


def _separated(a,b):
    return any(a["max"][i] < b["min"][i]-LINEAR_TOL or b["max"][i] < a["min"][i]-LINEAR_TOL for i in range(3))


def _distance_bounds(a,b):
    return math.sqrt(sum(max(0,a["min"][i]-b["max"][i],b["min"][i]-a["max"][i])**2 for i in range(3)))


def _direction(value):
    vec=AXES.get(value) if isinstance(value,str) else value
    if not isinstance(vec,(list,tuple)) or len(vec)!=3:raise ValueError("INVALID_ENTRY_DIRECTION")
    vec=[float(v) for v in vec]
    length=math.sqrt(sum(v*v for v in vec))
    if not all(math.isfinite(v) for v in vec) or length<1e-12:raise ValueError("INVALID_ENTRY_DIRECTION")
    return [v/length for v in vec]


def make_path(direction,distance_mm):
    vec=_direction(direction);length=float(distance_mm)
    if not math.isfinite(length) or length<=0:raise ValueError("INVALID_ENTRY_DISTANCE")
    return {"kind":"translation","start_translation_mm":[-v*length for v in vec],"end_translation_mm":[0,0,0],"verified":False}


def _native_worker(requests,results,occurrences):
    """One replaceable process: timeout terminates blocked OCCT operations."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape,TopoDS_Compound,TopoDS
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common,BRepAlgoAPI_Cut
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopLoc import TopLoc_Location
    from OCP.gp import gp_Trsf,gp_Vec,gp_Pnt
    from OCP.TopAbs import TopAbs_FACE,TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Plane,GeomAbs_Cylinder,GeomAbs_Cone
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism,BRepPrimAPI_MakeBox
    from OCP.BRepBndLib import BRepBndLib
    from OCP.Bnd import Bnd_Box
    try:from OCP.collections import List_TopoDS_Shape as ShapeList
    except ImportError:from OCP.TopTools import TopTools_ListOfShape as ShapeList
    shapes={};masked_shapes={};rows={o["id"]:o for o in occurrences};builder=BRep_Builder()
    def shape(ident):
        if ident not in shapes:
            s=TopoDS_Shape()
            if not BRepTools.Read_s(s,rows[ident]["brep_path"],builder) or s.IsNull():raise ValueError("BREP_READ_FAILED")
            shapes[ident]=s
        return shapes[ident]
    def moved(s,offset):
        tr=gp_Trsf();tr.SetTranslation(gp_Vec(*offset))
        return s.Moved(TopLoc_Location(tr))
    def bounds(s):
        box=Bnd_Box();BRepBndLib.AddOptimal_s(s,box,False,False)
        if box.IsVoid():return None
        low,high=box.CornerMin(),box.CornerMax()
        return {"min":[low.X(),low.Y(),low.Z()],"max":[high.X(),high.Y(),high.Z()]}
    def boundary_sweep_role(face,surface,direction):
        """Prove planar or monotone analytic surface projection before sweep.

        For a cylinder/cone, normal dot travel depends only on the angular
        parameter A*cos(u)+B*sin(u)+C. A strict constant sign over the full
        trimmed parameter interval prevents a folded swept solid. A cylinder
        parallel to its axis sweeps only a zero-volume surface.
        """
        kind=surface.GetType()
        if kind==GeomAbs_Plane:
            return "tangent" if abs(gp_Vec(surface.Plane().Axis().Direction()).Dot(direction))<1e-12 else "solid"
        if kind not in (GeomAbs_Cylinder,GeomAbs_Cone):return "unsupported"
        obj=surface.Cylinder() if kind==GeomAbs_Cylinder else surface.Cone()
        axis=obj.Position();x=gp_Vec(axis.XDirection());y=gp_Vec(axis.YDirection());z=gp_Vec(axis.Direction())
        angle=0. if kind==GeomAbs_Cylinder else obj.SemiAngle()
        a=math.cos(angle)*x.Dot(direction);b=math.cos(angle)*y.Dot(direction);c=-math.sin(angle)*z.Dot(direction)
        if kind==GeomAbs_Cylinder and math.hypot(a,b)<1e-12:return "tangent"
        umin,umax,vmin,vmax=BRepTools.UVBounds_s(face)
        if kind==GeomAbs_Cone:
            radii=[obj.RefRadius()+v*math.sin(angle) for v in (vmin,vmax)]
            if min(radii)<-LINEAR_TOL and max(radii)>LINEAR_TOL:return "unsupported"
        points=[umin,umax];phase=math.atan2(b,a)
        for k in range(math.floor((umin-phase)/math.pi)-1,math.ceil((umax-phase)/math.pi)+2):
            critical=phase+k*math.pi
            if umin<critical<umax:points.append(critical)
        values=[a*math.cos(u)+b*math.sin(u)+c for u in points]
        # Zero at a trim edge is allowed, but interior sign changes are not.
        if min(values)>=-1e-12 and max(values)>1e-9:return "solid"
        if max(values)<=1e-12 and min(values)<-1e-9:return "solid"
        return "unsupported"
    def common(a,b):
        op=BRepAlgoAPI_Common();args=ShapeList();ts=ShapeList();args.Append(a);ts.Append(b)
        op.SetArguments(args);op.SetTools(ts);op.SetNonDestructive(True);op.SetRunParallel(True);op.SetFuzzyValue(LINEAR_TOL);op.Build()
        if not op.IsDone():raise ValueError("BOOLEAN_FAILED")
        result=op.Shape()
        if not result.IsNull() and not BRepCheck_Analyzer(result).IsValid():raise ValueError("INVALID_BOOLEAN_RESULT")
        p=GProp_GProps()
        if not result.IsNull():BRepGProp.VolumeProperties_s(result,p)
        volume=abs(p.Mass())
        if not math.isfinite(volume):raise ValueError("NONFINITE_VOLUME")
        bounds=None
        if volume>VOLUME_TOL:
            box=Bnd_Box();BRepBndLib.AddOptimal_s(result,box,False,False)
            if not box.IsVoid():
                low,high=box.CornerMin(),box.CornerMax()
                bounds={"min":[low.X(),low.Y(),low.Z()],"max":[high.X(),high.Y(),high.Z()]}
        return volume,bounds
    def classify(a,b,allow_empty_obstacle=False,need_distance=False):
        ab,bb=bounds(a),bounds(b)
        if ab is None:raise ValueError("EMPTY_MOVING_GEOMETRY")
        if bb is None:
            if allow_empty_obstacle:return {"status":"PASS","distance_mm":1e100,"common_volume_mm3":0,"method":"empty_permitted_region_remainder"}
            raise ValueError("EMPTY_OBSTACLE_GEOMETRY")
        if _separated(ab,bb):return {"status":"PASS","distance_mm":_distance_bounds(ab,bb),"common_volume_mm3":0,"method":"exact_bounds_clearance"}
        gap=None
        if need_distance:
            distance=BRepExtrema_DistShapeShape()
            distance.LoadS1(a);distance.LoadS2(b)
            distance.SetMultiThread(True)
            distance.Perform()
            if not distance.IsDone():raise ValueError("DISTANCE_FAILED")
            gap=float(distance.Value())
            if not math.isfinite(gap):raise ValueError("NONFINITE_DISTANCE")
            # DistShapeShape returns zero for complete solid containment too.
            if gap>LINEAR_TOL:return {"status":"PASS","distance_mm":gap,"common_volume_mm3":0}
        volume,intersection_bounds=common(a,b)
        return {"status":"COLLISION" if volume>VOLUME_TOL else "PASS","distance_mm":gap,"common_volume_mm3":volume,"intersection_bbox":intersection_bounds,"method":"exact_boolean_volume"}
    while True:
        request=requests.get()
        if request is None:return
        ident,operation,payload=request
        try:
            a=shape(payload["a"]);b=shape(payload["b"])
            masks=payload.get("masks",[])
            if masks:
                key=_json_hash([payload["b"],masks])
                if key not in masked_shapes:
                    current=b
                    for mask in masks:
                        region=mask["region_bbox"]
                        box=BRepPrimAPI_MakeBox(gp_Pnt(*region["min"]),gp_Pnt(*region["max"])).Shape()
                        cut=BRepAlgoAPI_Cut(current,box);cut.Build()
                        if not cut.IsDone() or not BRepCheck_Analyzer(cut.Shape()).IsValid():raise ValueError("CONTACT_REGION_CUT_FAILED")
                        current=cut.Shape()
                    masked_shapes[key]=current
                b=masked_shapes[key]
            if operation=="pair":
                result=classify(moved(a,payload.get("offset",[0,0,0])),b,bool(masks),payload.get("need_distance",True))
            elif operation=="sweep":
                # Exact occupied sweep for planar closed BREP boundary faces.
                # A swept volume = start solid union all its swept boundary
                # faces. Test each volume independently (union, not odd/even).
                offset=payload["offset"];end=payload.get("end_offset",[0,0,0]);vector=gp_Vec(*[end[i]-offset[i] for i in range(3)]);start=moved(a,offset)
                result=classify(start,b,bool(masks))
                if result["status"]=="COLLISION":
                    result["reason"]="START_PENETRATION"
                else:
                    exp=TopExp_Explorer(start,TopAbs_FACE);faces=[];unsupported=False
                    while exp.More():
                        face=TopoDS.Face(exp.Current()) if hasattr(TopoDS,"Face") else TopoDS.Face_s(exp.Current())
                        surface=BRepAdaptor_Surface(face)
                        sweep_role=boundary_sweep_role(face,surface,vector.Normalized())
                        if sweep_role=="unsupported":unsupported=True;break
                        if sweep_role=="solid":faces.append(face)
                        exp.Next()
                    if unsupported or len(faces)>128:result={"status":"UNSUPPORTED","reason":"NONMONOTONE_OR_LARGE_ANALYTIC_SWEEP"}
                    else:
                        count=0
                        for face in faces:
                            prism=BRepPrimAPI_MakePrism(face,vector,True,True)
                            if not prism.IsDone() or not BRepCheck_Analyzer(prism.Shape()).IsValid():raise ValueError("SWEEP_INVALID")
                            verdict=classify(prism.Shape(),b,bool(masks));count+=1
                            if verdict["status"]=="COLLISION":result={**verdict,"reason":"SWEPT_VOLUME_PENETRATION"};break
                        else:result={"status":"PASS","method":"exact_monotone_analytic_boundary_sweep","faces_checked":count,"linear_tolerance_mm":LINEAR_TOL,"volume_tolerance_mm3":VOLUME_TOL}
            else:raise ValueError("UNKNOWN_NATIVE_OPERATION")
            if masks:result["contact_policy_ids"]=[p["id"] for p in masks]
            results.put((ident,result))
        except BaseException as exc:results.put((ident,{"status":"UNKNOWN","reason":"NATIVE_ERROR","detail":f"{type(exc).__name__}: {exc}"}))


class GeometrySession:
    def __init__(self,model,timeout=8.0,cancel=None,contact_policies=None):
        self.model=model;self.rows={o["id"]:o for o in model["occurrences"]};self.timeout=timeout;self.cancel=cancel
        self.context=mp.get_context("spawn");self.process=None;self.counter=0;self.cache={};self.policies={}
        units={u["id"]:u for u in model["units"]}
        for policy in contact_policies or []:
            for a in units[policy["unit_a"]]["occurrence_ids"]:
                for b in units[policy["unit_b"]]["occurrence_ids"]:
                    self.policies.setdefault(frozenset((a,b)),[]).append(policy)
    def _start(self):
        self.requests=self.context.Queue();self.results=self.context.Queue()
        self.process=self.context.Process(target=_native_worker,args=(self.requests,self.results,list(self.rows.values())),daemon=True);self.process.start()
    def close(self):
        if self.process:
            if self.process.is_alive():self.process.terminate()
            self.process.join(timeout=2);self.process=None
        for name in ("requests","results"):
            channel=getattr(self,name,None)
            if channel is not None:
                try:channel.cancel_join_thread();channel.close()
                except (ValueError,OSError):pass
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
    def request(self,operation,payload,deadline):
        if time.monotonic()>=deadline:return {"status":"UNKNOWN","reason":"TIME_BUDGET"}
        if self.cancel and self.cancel():raise InterruptedError("PLANNING_CANCELLED")
        key=_json_hash([operation,payload])
        if key in self.cache:return self.cache[key]
        if self.process is None or not self.process.is_alive():self.close();self._start()
        self.counter+=1;ident=self.counter;self.requests.put((ident,operation,payload))
        stop=min(deadline,time.monotonic()+self.timeout)
        while time.monotonic()<stop:
            if self.cancel and self.cancel():self.close();raise InterruptedError("PLANNING_CANCELLED")
            try:
                received,result=self.results.get(timeout=min(0.2,max(.001,stop-time.monotonic())))
                if received!=ident:continue
                if result.get("reason") not in ("TIME_BUDGET","NATIVE_TIMEOUT"):self.cache[key]=result
                return result
            except queue.Empty:
                if not self.process.is_alive():self.close();return {"status":"UNKNOWN","reason":"NATIVE_PROCESS_FAILED"}
        self.close();return {"status":"UNKNOWN","reason":"NATIVE_TIMEOUT","timeout_seconds":self.timeout}
    def pair(self,a,b,deadline,offset=None,masks=None,need_distance=False):
        x,y=self.rows[a],self.rows[b];offset=offset or [0,0,0]
        if _separated(_offset_box(x["bbox"],offset),y["bbox"]):return {"status":"PASS","method":"disjoint_exact_bounds","distance_mm":_distance_bounds(_offset_box(x["bbox"],offset),y["bbox"])}
        if not x.get("collision_ready",x.get("valid",False)) or not y.get("collision_ready",y.get("valid",False)):
            return {"status":"UNKNOWN","reason":"INVALID_ANALYSIS_REPRESENTATION","occurrence_ids":[a,b]}
        # A None mask selects static final-placement policy; [] is explicit no
        # exemption for travel outside the approved terminal interval.
        if masks is None:masks=self.policies.get(frozenset((a,b)),[])
        return self.request("pair",{"a":a,"b":b,"offset":offset,"masks":masks,"need_distance":need_distance},deadline)
    def path_pair(self,a,b,path,deadline):
        start=path["start_translation_mm"]
        length=math.sqrt(sum(v*v for v in start))
        policies=self.policies.get(frozenset((a,b)),[])
        boundaries=sorted({0.,1.,*(max(0.,1.-p["terminal_distance_mm"]/length) for p in policies)})
        segments=[]
        for lo,hi in zip(boundaries,boundaries[1:]):
            masks=[p for p in policies if (1-lo)*length<=p["terminal_distance_mm"]+1e-9]
            result=self._path_segment(a,b,path,deadline,lo,hi,masks)
            segments.append({"interval":[lo,hi],"status":result["status"],"contact_policy_ids":[p["id"] for p in masks]})
            if result["status"]!="PASS":return {**result,"policy_segments":segments}
        return {"status":"PASS","method":"certified_segment_union","policy_segments":segments}
    def _path_segment(self,a,b,path,deadline,begin,end,masks):
        total_start=path["start_translation_mm"];start=[v*(1-begin) for v in total_start];end_offset=[v*(1-end) for v in total_start];box=self.rows[a]["bbox"];ob=self.rows[b]["bbox"]
        sweep=_bbox_union([_offset_box(box,end_offset),_offset_box(box,start)])
        if _separated(sweep,ob):return {"status":"PASS","method":"disjoint_swept_bounds"}
        if not self.rows[a].get("collision_ready",self.rows[a].get("valid",False)) or not self.rows[b].get("collision_ready",self.rows[b].get("valid",False)):
            return {"status":"UNKNOWN","reason":"INVALID_ANALYSIS_REPRESENTATION","occurrence_ids":[a,b]}
        final=self.pair(a,b,deadline,end_offset,masks)
        if final["status"] in ("COLLISION","UNKNOWN"):return {**final,"path_fraction":1.0,"reason":"FINAL_PENETRATION" if final["status"]=="COLLISION" else final.get("reason")}
        swept=self.request("sweep",{"a":a,"b":b,"offset":start,"end_offset":end_offset,"masks":masks},deadline)
        if swept["status"]!="UNSUPPORTED":return swept
        length=math.sqrt(sum(v*v for v in total_start));intervals=[(begin,end)];checked=[];unresolved=[]
        # A shape-set distance is 1-Lipschitz under translation. At a midpoint,
        # gap greater than the maximum interval movement certifies all times.
        while intervals:
            lo,hi=intervals.pop();mid=(lo+hi)/2
            sub_bounds=_bbox_union([_offset_box(box,[v*(1-lo) for v in total_start]),_offset_box(box,[v*(1-hi) for v in total_start])])
            if _separated(sub_bounds,ob):checked.append([lo,hi]);continue
            verdict=self.pair(a,b,deadline,[v*(1-mid) for v in total_start],masks,need_distance=True)
            if verdict["status"]=="COLLISION":return {**verdict,"path_fraction":mid,"checked_segments":checked}
            if verdict["status"]=="UNKNOWN":return {**verdict,"checked_segments":checked,"unresolved_segments":[[lo,hi]]+intervals}
            gap=verdict.get("distance_mm")
            if gap is None or not math.isfinite(float(gap)):
                return {"status":"UNKNOWN","reason":"CLEARANCE_VALUE_UNAVAILABLE","checked_segments":checked,"unresolved_segments":[[lo,hi]]+intervals}
            if gap>length*(hi-lo)/2+LINEAR_TOL:
                checked.append([lo,hi]);continue
            if length*(hi-lo)<0.002 or len(checked)+len(intervals)>1024:
                unresolved.append([lo,hi]);continue
            intervals.extend([(mid,hi),(lo,mid)])
        return {"status":"UNKNOWN" if unresolved else "PASS","reason":"CONTACT_INTERVAL_UNRESOLVED" if unresolved else None,"method":"conservative_clearance_intervals","checked_segments":checked,"unresolved_segments":unresolved}


def _normalise(model,rules):
    rules=rules or {};units={u["id"]:u for u in model["units"]};errors=[]
    if model.get("quality",{}).get("inventory_complete") is False:
        errors.append({"code":"MODEL_INVENTORY_INCOMPLETE","message":"Missing transferred source members must be resolved before a product plan can be certified."})
    occurrence_ids={o["id"] for o in model["occurrences"]}
    if len(units)!=len(model["units"]) or len(occurrence_ids)!=len(model["occurrences"]):
        errors.append({"code":"DUPLICATE_GEOMETRY_ID","message":"Unit and occurrence identifiers must be unique."})
    membership=[o for u in units.values() for o in u["occurrence_ids"]]
    if len(membership)!=len(set(membership)) or set(membership)!=occurrence_ids:errors.append({"code":"INVALID_UNIT_COVERAGE","message":"Every physical occurrence must belong to exactly one assembly unit."})
    fixed=set(rules.get("fixed_unit_ids",[u["id"] for u in units.values() if u.get("fixed")]))
    environment=set(rules.get("initial_obstacle_ids",[]))
    selected=set(rules.get("selected_scope",list(units)))
    if selected!=set(units) and not rules.get("environment_confirmed",False):
        errors.append({"code":"PARTIAL_SCOPE_ENVIRONMENT_UNCONFIRMED","message":"Explicitly confirm the installed obstacles for a selected scope; absent from the plan does not imply absent from the workspace."})
    if (fixed|environment|selected)-set(units):errors.append({"code":"UNKNOWN_UNIT_REFERENCE","unit_ids":sorted((fixed|environment|selected)-set(units))})
    installed=fixed|environment
    targets=selected-installed
    pred={u:set() for u in targets}
    for edge in rules.get("precedence",[]):
        a,b=(edge.get("before"),edge.get("after")) if isinstance(edge,dict) else edge
        if a not in units or b not in units:errors.append({"code":"UNKNOWN_PREDECESSOR","before":a,"after":b});continue
        if b in pred:pred[b].add(a)
        if b in installed and a not in installed:errors.append({"code":"INITIAL_STATE_RULE_CONFLICT","before":a,"after":b})
    checked=set(installed);pending=set(targets)
    while pending:
        eligible=[u for u in pending if pred[u]<=checked]
        if not eligible:errors.append({"code":"CYCLIC_OR_MISSING_PREREQUISITE","unit_ids":sorted(pending)});break
        checked.update(eligible);pending.difference_update(eligible)
    for policy in rules.get("contact_policies",[]):
        try:
            valid=(policy["unit_a"] in units and policy["unit_b"] in units and policy["unit_a"]!=policy["unit_b"]
                and bool(policy["id"]) and bool(policy["approved_by"]) and bool(policy["approval_reference"])
                and math.isfinite(float(policy["terminal_distance_mm"])) and float(policy["terminal_distance_mm"])>0
                and len(policy["region_bbox"]["min"])==3 and len(policy["region_bbox"]["max"])==3
                and all(math.isfinite(float(v)) for side in ("min","max") for v in policy["region_bbox"][side])
                and all(policy["region_bbox"]["max"][i]>policy["region_bbox"]["min"][i] for i in range(3)))
        except (KeyError,TypeError,IndexError,ValueError):valid=False
        if not valid:errors.append({"code":"INVALID_CONTACT_POLICY","message":"Provide approved_by, approval_reference, pair IDs, region_bbox in world mm, positive terminal_distance_mm and policy ID."})
    return units,installed,targets,pred,errors


def _audit(model,units,initial,targets,engine,deadline,progress=None):
    relevant=sorted(initial|targets);pairs=[];pass_count=0;potential=0;unresolved=0;work=[]
    for index,a in enumerate(relevant):
        for b in relevant[index+1:]:
            if _separated(units[a]["bbox"],units[b]["bbox"]):pass_count+=1;continue
            potential+=1
            for oa in units[a]["occurrence_ids"]:
                for ob in units[b]["occurrence_ids"]:
                    if _separated(engine.rows[oa]["bbox"],engine.rows[ob]["bbox"]):continue
                    complexity=engine.rows[oa].get("face_count",1)*engine.rows[ob].get("face_count",1)
                    work.append((not(a in initial and b in initial),complexity,a,b,oa,ob))
    # A complex early housing pair cannot starve hundreds of inexpensive
    # independent pairs. Initial installed pairs retain first priority.
    work.sort()
    for index,(_,_,a,b,oa,ob) in enumerate(work):
        verdict=engine.pair(oa,ob,deadline)
        if verdict["status"] not in ("PASS","CONTACT"):
            pairs.append({"a":a,"b":b,"a_name":units[a]["name"],"b_name":units[b]["name"],"occurrence_a":oa,"occurrence_b":ob,
                "initial_pair":a in initial and b in initial,**verdict})
        if verdict["status"]=="UNKNOWN":unresolved+=1
        if progress and (index%10==0 or index+1==len(work)):progress({"stage":"placement_audit","current":index+1,"total":len(work),"message":units[a]["name"]+" / "+units[b]["name"]})
    status="COLLISION" if any(p["status"]=="COLLISION" for p in pairs) else ("UNKNOWN" if pairs else "PASS")
    proposals=[]
    for p in pairs:
        if p["status"]=="COLLISION" and p.get("intersection_bbox"):
            proposals.append({"id":"proposal_"+_json_hash([p["occurrence_a"],p["occurrence_b"]])[:12],"unit_a":p["a"],"unit_b":p["b"],
                "region_bbox":p["intersection_bbox"],"terminal_distance_mm":None,"approved_by":None,"approval_reference":None,
                "basis":"Measured final BREP intersection bounding region; intent and allowed travel remain unapproved.","requires_engineering_review":True,"measured_overlap_mm3":p["common_volume_mm3"]})
    return {"status":status,"pairs":pairs,"contact_policy_proposals":proposals,"broadphase_disjoint_pairs":pass_count,"potential_unit_pairs":potential,"potential_occurrence_pairs":len(work),"unknown_pair_count":unresolved,
        "scope":"initial installed pairs and final placement across all selected units","contact_policy":"approved pair-specific spatial regions only; no unapproved overlap ignored","applied_contact_policy_ids":sorted({p["id"] for policies in engine.policies.values() for p in policies}),"linear_tolerance_mm":LINEAR_TOL,"volume_tolerance_mm3":VOLUME_TOL}


def _action(unit,installed,units,engine,direction,distance,deadline):
    path=make_path(direction,distance);evidence=[];status="PASS"
    if not unit.get("collision_ready",True):return {"status":"UNKNOWN","reason":"INVALID_ANALYSIS_REPRESENTATION","path":path,"pairs":[]}
    # Starting outside the complete selected model envelope is conservative.
    world=_bbox_union([u["bbox"] for u in units.values()])
    if not _separated(_offset_box(unit["bbox"],path["start_translation_mm"]),world):
        return {"status":"UNKNOWN","reason":"START_NOT_OUTSIDE_PRODUCT","path":path,"pairs":[]}
    for obstacle in sorted(installed):
        if obstacle==unit["id"]:continue
        for a in unit["occurrence_ids"]:
            for b in units[obstacle]["occurrence_ids"]:
                verdict=engine.path_pair(a,b,path,deadline)
                if verdict["status"]!="PASS":
                    evidence.append({"moving_occurrence_id":a,"obstacle_occurrence_id":b,"obstacle_unit_id":obstacle,**verdict})
                    if verdict["status"]=="COLLISION":status="COLLISION"
                    elif status!="COLLISION":status="UNKNOWN"
                    if status=="COLLISION":return {"status":status,"path":path,"pairs":evidence,"reason":verdict.get("reason")}
        if time.monotonic()>=deadline:return {"status":"UNKNOWN","reason":"TIME_BUDGET","path":path,"pairs":evidence}
    path["verified"]=status=="PASS"
    return {"status":status,"path":path,"pairs":evidence}


def _priority(unit):
    role={"housing":0,"plate":1,"component":2,"connector":3,"relay":4,"busbar":5,"fastener":6,"seal":7,"cover":8}.get(unit.get("role"),3)
    return role,unit["bbox"]["center"][2],unit["name"],unit["id"]


def _default_distance(model):return max(model["bbox"]["size"])*1.35+25.0


def _directions_for_unit(unit,model,defaults):
    """Rank entry axes; this recommendation is never a collision certificate."""
    if unit.get("role") not in ("connector","component"):
        return list(defaults)
    world=model["bbox"];box=unit["bbox"]
    def cost(direction):
        vector=_direction(direction)
        if sum(abs(v)>1e-10 for v in vector)!=1:return 1e9
        axis=max(range(3),key=lambda i:abs(vector[i]))
        # Entry +axis comes from the negative external side.
        escape=box["max"][axis]-world["min"][axis] if vector[axis]>0 else world["max"][axis]-box["min"][axis]
        return escape
    return sorted(defaults,key=cost)


def _ranking_score(unit,installed_fraction,record):
    """Pairwise-2.0.0 point-score tie-break, applied only after geometry PASS."""
    if not isinstance(record,dict) or not record.get("active") or record.get("feature_version")!="pairwise-2.0.0":return 0.
    try:
        coeff=[float(v) for v in record["coefficients"]];scale=[float(v) for v in record["scale"]]
        if len(coeff)!=10 or len(scale)!=10 or not all(math.isfinite(v) for v in coeff+scale) or min(scale)<=0:return 0.
        values=[*[math.log1p(max(0,float(v))) for v in unit["bbox"]["size"]],unit["bbox"]["center"][2]/1000.,float(unit.get("role") in ("fastener","bolt","nut"))]
        expanded=values+[v*installed_fraction for v in values]
        score=sum(c*x/s for c,x,s in zip(coeff,expanded,scale))
        return score if math.isfinite(score) else 0.
    except (TypeError,ValueError,KeyError,OverflowError):return 0.


def _geometry_review_resolved(model,rules,units,initial,targets):
    if model.get("quality",{}).get("matching_ready") is False:return False
    relevant={o for ident in initial|targets for o in units[ident]["occurrence_ids"]}
    disagreements=set(model.get("quality",{}).get("exact_method_disagreement_ids",[]))&relevant
    if not disagreements:return True
    review=rules.get("geometry_review",{})
    return (review.get("model_fingerprint")==model.get("fingerprint") and review.get("acceptance_policy")=="standard_occt_preserved" and bool(review.get("approved_by"))
        and bool(review.get("approval_reference")) and set(review.get("occurrence_ids",[]))>=disagreements)


def _step(unit,index,direction,distance,verdict):
    return {"id":"step_"+unit["id"],"unit_id":unit["id"],"name":unit["name"],"index":index,"direction":direction,"distance_mm":distance,
        "path":verdict.get("path",make_path(direction,distance)),"status":verdict["status"],"diagnostics":verdict.get("pairs",[]),"reason":verdict.get("reason"),
        "torque":None,"caution":"","checked":False}


def generate_plan(model,rules=None,cache_dir=None,progress=None,cancel=None):
    rules=rules or {};started=time.monotonic();budget=max(1.,float(rules.get("max_seconds",90)));deadline=started+budget
    units,initial,targets,pred,errors=_normalise(model,rules)
    directions=rules.get("directions",["-Z","+Z","-Y","+Y","-X","+X"])
    distance=float(rules.get("distance_mm") or _default_distance(model));attempts=0;beam_width=max(1,min(8,int(rules.get("beam_width",3))))
    audit={"status":"UNKNOWN","pairs":[],"reason":"RULE_ERRORS"};steps=[];complete_verified=False;termination="completed";attempt_trace=[];decision_traces=[]
    geometry_reviewed=_geometry_review_resolved(model,rules,units,initial,targets) if not errors else False
    if errors:
        return {"id":"plan_"+_json_hash([model["fingerprint"],rules])[:16],"status":"BLOCKED","steps":[],"diagnostics":errors,"coverage":{"complete":False,"expected":len(targets),"actual":0},"algorithm_version":ALGORITHM_VERSION}
    with GeometrySession(model,timeout=float(rules.get("native_timeout_seconds",5)),cancel=cancel,contact_policies=rules.get("contact_policies")) as engine:
        audit=_audit(model,units,initial,targets,engine,started+budget*.35,progress)
        # Each frontier contains separate state copies; no mutation leaks.
        frontier=[(0.,frozenset(initial),[])];complete=[];best=frontier[0]
        search_deadline=started+budget*.8
        while frontier and time.monotonic()<search_deadline:
            children=[]
            for cost,installed,current in frontier:
                state_candidates=[]
                pending=targets-set(installed)
                if not pending:complete.append((cost,installed,current));continue
                eligible=sorted((u for u in pending if pred[u]<=set(installed)),key=lambda u:_priority(units[u]))
                actions=[];passed_units=set()
                for uid in eligible:
                    per_unit=rules.get("entry_conditions",{}).get(uid,{})
                    dirs=[per_unit["direction"]] if per_unit.get("direction") else _directions_for_unit(units[uid],model,directions)
                    length=float(per_unit.get("distance_mm",distance))
                    actions.extend((direction_index,uid,direction,length) for direction_index,direction in enumerate(dirs))
                actions.sort(key=lambda a:(a[0],_priority(units[a[1]])))
                # Try each eligible unit's best direction before spending the
                # budget on five alternatives of the first complex component.
                for _,uid,direction,length in actions:
                    if time.monotonic()>=search_deadline:break
                    if uid in passed_units:continue
                    action_deadline=min(search_deadline,time.monotonic()+float(rules.get("candidate_seconds",4.0)))
                    action_started=time.monotonic()
                    verdict=_action(units[uid],installed,units,engine,direction,length,action_deadline);attempts+=1
                    attempt_trace.append({"unit_id":uid,"unit_name":units[uid]["name"],"installed_unit_ids":sorted(installed),"direction":direction,"distance_mm":length,"status":verdict["status"],"reason":verdict.get("reason"),"pairs":verdict.get("pairs",[]),"elapsed_seconds":round(time.monotonic()-action_started,3)})
                    if verdict["status"]=="PASS":
                        step=_step(units[uid],len(current)+1,direction,length,verdict)
                        step["ranking_score"]=_ranking_score(units[uid],len(installed)/max(1,len(units)),rules.get("ranking_model"))
                        change=10 if current and current[-1]["direction"]!=direction else 0
                        score=cost+length+change
                        state_candidates.append({"unit_id":uid,"direction":direction,"features":{"distance_mm":length,"direction_change":bool(change),"role":units[uid].get("role"),"center":units[uid]["bbox"]["center"]}})
                        child=(score,installed|{uid},current+[step]);children.append(child);passed_units.add(uid)
                        if len(child[2])>len(best[2]):best=child
                        if targets<=set(child[1]):complete.append(child)
                    if progress:progress({"stage":"sequence_search","current":len(best[2]),"total":len(targets),"attempts":attempts,"message":units[uid]["name"]})
                if state_candidates:decision_traces.append({"installed_ids":sorted(installed),"feasible_unit_ids":sorted({c["unit_id"] for c in state_candidates}),"candidates":state_candidates,"selected_unit_id":None,"label_status":"unlabelled_verified_candidates"})
            if complete:break
            unique={}
            for candidate in sorted(children,key=lambda v:(-len(v[2]),v[0],-v[2][-1].get("ranking_score",0))):
                key=(candidate[1],candidate[2][-1]["direction"] if candidate[2] else None)
                unique.setdefault(key,candidate)
            frontier=list(unique.values())[:beam_width]
        if complete:
            steps=min(complete,key=lambda c:(c[0],-sum(s.get("ranking_score",0) for s in c[2])))[2];complete_verified=audit["status"]=="PASS" and geometry_reviewed
        else:
            termination="budget_reached" if time.monotonic()>=search_deadline else "no_verified_continuation"
            # Preserve verified prefix, then produce an explicitly unverified
            # complete rule-respecting draft for human inspection/revision.
            _,installed,steps=best;installed=set(installed);steps=list(steps)
            pending=targets-installed
            while pending:
                eligible=sorted((u for u in pending if pred[u]<=installed),key=lambda u:_priority(units[u]))
                if not eligible:break
                uid=eligible[0];per_unit=rules.get("entry_conditions",{}).get(uid,{})
                direction=per_unit.get("direction",_directions_for_unit(units[uid],model,directions)[0]);length=float(per_unit.get("distance_mm",distance))
                verdict={"status":"UNKNOWN","reason":"REVIEW_DRAFT_PATH_NOT_VERIFIED","path":make_path(direction,length)}
                steps.append(_step(units[uid],len(steps)+1,direction,length,verdict));installed.add(uid);pending.remove(uid)
    selected_installed=set(initial)
    for step in steps:
        for trace in decision_traces:
            if set(trace["installed_ids"])==selected_installed and step["unit_id"] in trace["feasible_unit_ids"]:
                trace["selected_unit_id"]=step["unit_id"];trace["label_status"]="recommendation_only_requires_expert_preference_review"
        selected_installed.add(step["unit_id"])
    status="PASS" if complete_verified else "UNKNOWN"
    diagnostics=list(errors)
    if not geometry_reviewed:
        diagnostics.append({"code":"MODEL_MAPPING_REVIEW_REQUIRED","message":"Resolve ambiguous source-to-unit matches before confirming."} if model.get("quality",{}).get("matching_ready") is False else {"code":"GEOMETRY_EXACT_METHOD_REVIEW_REQUIRED","message":"Preserved repairs pass standard OCCT but disagree with exact-method checks. Confirming requires an explicit scoped representation review, not just a display checkbox."})
    if audit["status"]!="PASS":diagnostics.append({"code":"FINAL_PLACEMENT_REQUIRES_REVIEW","message":"Final geometry contains penetration or unresolved pairs; reordering alone cannot repair final fit.","collision_count":sum(p["status"]=="COLLISION" for p in audit.get("pairs",[])),"unknown_count":sum(p["status"]=="UNKNOWN" for p in audit.get("pairs",[]))})
    if any(s["status"]!="PASS" for s in steps):diagnostics.append({"code":"DRAFT_REQUIRES_PATH_VALIDATION","message":"A complete draft is available; unverified steps cannot be confirmed or exported as an approved instruction."})
    return {"id":"plan_"+_json_hash([model["fingerprint"],rules,steps])[:16],"status":status,"kind":"verified_recommendation" if status=="PASS" else "review_draft",
        "steps":steps,"initial_unit_ids":sorted(initial),"diagnostics":diagnostics,"placement_audit":audit,"coverage":{"complete":{s["unit_id"] for s in steps}==targets,"expected":len(targets),"actual":len(steps),"initial_count":len(initial),"complete_product_scope":initial|targets==set(units),"selected_unit_ids":sorted(initial|targets)},
        "algorithm_version":ALGORITHM_VERSION,"dependency_hash":_json_hash([model["fingerprint"],rules]),"attempt_trace":attempt_trace,"decision_trace":decision_traces,"search":{"attempts":attempts,"beam_width":beam_width,"termination":termination,"optimality":"not_globally_proven","elapsed_seconds":round(time.monotonic()-started,3)},
        "scope":"fixed orientation, single rigid unit, straight translation; tool access, deformation and fixture stability untested"}


def validate_plan(model,plan,rules=None,cache_dir=None,progress=None,cancel=None):
    rules=rules or {};started=time.monotonic();budget=max(1.,float(rules.get("validation_seconds",rules.get("max_seconds",90))));deadline=started+budget
    units,initial,targets,pred,errors=_normalise(model,rules);steps=plan.get("steps",[])
    ids=[s.get("unit_id") for s in steps]
    if len(ids)!=len(set(ids)) or set(ids)!=targets:errors.append({"code":"INVALID_PLAN_COVERAGE","missing":sorted(targets-set(ids)),"unexpected":sorted(set(ids)-targets),"duplicate":len(ids)!=len(set(ids))})
    installed=set(initial);validated=[]
    if errors:return {"status":"BLOCKED","diagnostics":errors,"steps":[],"coverage":{"complete":False},"dependency_hash":_json_hash([model["fingerprint"],rules,steps])}
    with GeometrySession(model,timeout=float(rules.get("native_timeout_seconds",5)),cancel=cancel,contact_policies=rules.get("contact_policies")) as engine:
        audit=_audit(model,units,initial,targets,engine,started+budget*.35,progress)
        for index,step in enumerate(steps):
            uid=step["unit_id"]
            if not pred[uid]<=installed:verdict={"status":"COLLISION","reason":"PRECEDENCE_VIOLATION","missing_predecessors":sorted(pred[uid]-installed)}
            else:
                try:
                    expected=make_path(step["direction"],step["distance_mm"])
                    supplied=step.get("path",{})
                    mismatch=any(supplied.get(key)!=expected[key] for key in ("kind","start_translation_mm","end_translation_mm"))
                    if mismatch:
                        # Explicit rebuild means manual direction/distance edits
                        # cannot replay the old path or retain its PASS marker.
                        errors.append({"code":"PATH_REBUILT_FROM_CURRENT_ENTRY","severity":"info","step_id":step["id"]})
                    verdict=_action(units[uid],installed,units,engine,step["direction"],step["distance_mm"],deadline)
                except (ValueError,TypeError,KeyError) as exc:verdict={"status":"UNKNOWN","reason":str(exc)}
            updated={**step,"status":verdict["status"],"path":verdict.get("path",{}),"diagnostics":verdict.get("pairs",[]),"reason":verdict.get("reason")};validated.append(updated);installed.add(uid)
            if progress:progress({"stage":"plan_validation","current":index+1,"total":len(steps),"message":step["name"],"status":verdict["status"]})
    reviewed=_geometry_review_resolved(model,rules,units,initial,targets)
    if not reviewed:errors.append({"code":"MODEL_MAPPING_REVIEW_REQUIRED","message":"Resolve ambiguous source-to-unit matches before confirming."} if model.get("quality",{}).get("matching_ready") is False else {"code":"GEOMETRY_EXACT_METHOD_REVIEW_REQUIRED","message":"Standard-valid preserved representation remains subject to explicit scope review."})
    status="PASS" if reviewed and audit["status"]=="PASS" and all(s["status"]=="PASS" for s in validated) else ("BLOCKED" if audit["status"]=="COLLISION" or any(s["status"]=="COLLISION" for s in validated) else "UNKNOWN")
    return {"status":status,"steps":validated,"diagnostics":errors,"placement_audit":audit,"coverage":{"complete":True,"expected":len(targets),"actual":len(steps)},
        "algorithm_version":ALGORITHM_VERSION,"dependency_hash":_json_hash([model["fingerprint"],rules,steps]),"elapsed_seconds":round(time.monotonic()-started,3)}

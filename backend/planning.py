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
from backend.process_rules import compile_process_rules,process_eligibility,process_summary
from backend.planning_settings import DEFAULT_ANALYSIS_SECONDS, DEFAULT_CANDIDATE_SECONDS, DEFAULT_NATIVE_TIMEOUT_SECONDS

ALGORITHM_VERSION="pilot-sequence-2.4.0"
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
    from OCP.gp import gp_Trsf,gp_Vec,gp_Pnt,gp_Lin,gp_Dir
    from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
    from OCP.TopAbs import TopAbs_FACE,TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Plane,GeomAbs_Cylinder,GeomAbs_Cone
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism,BRepPrimAPI_MakeBox
    from OCP.BRepBndLib import BRepBndLib
    from OCP.Bnd import Bnd_Box
    try:from OCP.collections import List_TopoDS_Shape as ShapeList
    except ImportError:from OCP.TopTools import TopTools_ListOfShape as ShapeList
    shapes={};masked_shapes={};clipped_shapes={};reference_shapes={};ray_intersectors={};rows={o["id"]:o for o in occurrences};builder=BRep_Builder()
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
    def common(a,b,return_shape=False):
        op=BRepAlgoAPI_Common();args=ShapeList();ts=ShapeList();args.Append(a);ts.Append(b)
        op.SetArguments(args);op.SetTools(ts);op.SetNonDestructive(True);op.SetRunParallel(True);op.SetFuzzyValue(LINEAR_TOL);op.Build()
        if not op.IsDone():raise ValueError("BOOLEAN_FAILED")
        result=op.Shape()
        if not result.IsNull() and not BRepCheck_Analyzer(result).IsValid():raise ValueError("INVALID_BOOLEAN_RESULT")
        # Swept boundary compounds may contain opposite face orientations.
        # Summing signed mass for the compound could cancel real overlap.
        # Every closed solid therefore contributes its absolute volume.
        volume=0.;solid_count=0
        if not result.IsNull():
            solids=TopExp_Explorer(result,TopAbs_SOLID)
            while solids.More():
                p=GProp_GProps();BRepGProp.VolumeProperties_s(solids.Current(),p)
                volume+=abs(p.Mass());solid_count+=1;solids.Next()
        if not math.isfinite(volume):raise ValueError("NONFINITE_VOLUME")
        bounds=None
        if volume>VOLUME_TOL:
            box=Bnd_Box();BRepBndLib.AddOptimal_s(result,box,False,False)
            if not box.IsVoid():
                low,high=box.CornerMin(),box.CornerMax()
                bounds={"min":[low.X(),low.Y(),low.Z()],"max":[high.X(),high.Y(),high.Z()]}
        return (volume,bounds,result) if return_shape else (volume,bounds)
    def classify(a,b,allow_empty_obstacle=False,need_distance=False,minimum_clearance=0.):
        ab,bb=bounds(a),bounds(b)
        if ab is None:raise ValueError("EMPTY_MOVING_GEOMETRY")
        if bb is None:
            if allow_empty_obstacle:return {"status":"PASS","distance_mm":1e100,"common_volume_mm3":0,"method":"empty_permitted_region_remainder"}
            raise ValueError("EMPTY_OBSTACLE_GEOMETRY")
        if _separated(ab,bb) and _distance_bounds(ab,bb)>minimum_clearance:return {"status":"PASS","distance_mm":_distance_bounds(ab,bb),"common_volume_mm3":0,"method":"exact_bounds_clearance"}
        gap=None
        if need_distance or minimum_clearance>0:
            distance=BRepExtrema_DistShapeShape()
            distance.LoadS1(a);distance.LoadS2(b)
            distance.SetMultiThread(True)
            distance.Perform()
            if not distance.IsDone():raise ValueError("DISTANCE_FAILED")
            gap=float(distance.Value())
            if not math.isfinite(gap):raise ValueError("NONFINITE_DISTANCE")
            # DistShapeShape returns zero for complete solid containment too.
            if gap>max(LINEAR_TOL,minimum_clearance):return {"status":"PASS","distance_mm":gap,"common_volume_mm3":0,"required_clearance_mm":minimum_clearance}
        volume,intersection_bounds=common(a,b)
        if volume<=VOLUME_TOL and minimum_clearance>0 and (gap is None or gap<=minimum_clearance):
            return {"status":"UNKNOWN","reason":"REPRESENTATION_CLEARANCE_UNRESOLVED","distance_mm":gap,"required_clearance_mm":minimum_clearance,"common_volume_mm3":volume}
        return {"status":"COLLISION" if volume>VOLUME_TOL else "PASS","distance_mm":gap,"common_volume_mm3":volume,"intersection_bbox":intersection_bounds,"method":"exact_boolean_volume"}
    while True:
        request=requests.get()
        if request is None:return
        ident,operation,payload=request
        try:
            a=shape(payload["a"]);b=shape(payload["b"])
            if operation=='support':
                from .support import native_support_pair
                result=native_support_pair(a,b,common=common,moved=moved,probe_mm=payload.get('probe_mm',.01))
                results.put((ident,result));continue
            baseline_volume=0.
            if payload.get('assembled_reference'):
                # The input final assembly is the accepted nominal reference.
                # Remove only B intersect A(final), never a bounding box or the
                # entire pair. All newly occupied material remains an obstacle.
                reference_ids=tuple(sorted(payload.get('reference_occurrence_ids') or [payload['a']]))
                reference_key=(reference_ids,payload['b'])
                if reference_key not in reference_shapes:
                    remainder=b
                    for member in reference_ids:
                        # Sequential exact cuts equal subtraction of the whole
                        # moving unit's final union, without a costly fuse.
                        if bounds(remainder) is None:break
                        final_shape=shape(member)
                        if _separated(rows[member]['bbox'],rows[payload['b']]['bbox']):continue
                        overlap,_=common(final_shape,remainder)
                        if overlap>VOLUME_TOL:
                            cut=BRepAlgoAPI_Cut(remainder,final_shape);cut.SetNonDestructive(True);cut.SetFuzzyValue(LINEAR_TOL);cut.Build()
                            if not cut.IsDone() or (not cut.Shape().IsNull() and not BRepCheck_Analyzer(cut.Shape()).IsValid()):raise ValueError('ASSEMBLED_REFERENCE_CUT_FAILED')
                            remainder=cut.Shape();baseline_volume+=overlap
                    reference_shapes[reference_key]=(remainder,baseline_volume)
                b,baseline_volume=reference_shapes[reference_key]
            masks=payload.get("masks",[]);minimum_clearance=payload.get("minimum_clearance_mm",0.)
            allow_empty=bool(masks) or bool(payload.get('assembled_reference'))
            if masks:
                key=_json_hash([payload["a"] if payload.get('assembled_reference') else None,payload["b"],masks])
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
            if operation=="entry_probe":
                if payload["b"] not in ray_intersectors:
                    intersector=IntCurvesFace_ShapeIntersector();intersector.Load(b,LINEAR_TOL)
                    ray_intersectors[payload["b"]]=intersector
                intersector=ray_intersectors[payload["b"]];rank=[];box=payload["unit_bbox"]
                for direction in payload["directions"]:
                    entry=_direction(direction);outward=[-v for v in entry];axis=max(range(3),key=lambda i:abs(entry[i]));other=[i for i in range(3) if i!=axis]
                    points=[]
                    for p,q in ((.5,.5),(.25,.25),(.25,.75),(.75,.25),(.75,.75)):
                        point=[(box["min"][i]+box["max"][i])/2 for i in range(3)]
                        point[axis]=box["min"][axis] if entry[axis]>0 else box["max"][axis]
                        point[other[0]]=box["min"][other[0]]+p*(box["max"][other[0]]-box["min"][other[0]])
                        point[other[1]]=box["min"][other[1]]+q*(box["max"][other[1]]-box["min"][other[1]])
                        points.append(point)
                    blocked=0;intersections=0
                    for point in points:
                        intersector.Perform(gp_Lin(gp_Pnt(*point),gp_Dir(*outward)),LINEAR_TOL,payload["distance_mm"])
                        if not intersector.IsDone():raise ValueError("ENTRY_PROBE_FAILED")
                        hits=intersector.NbPnt();blocked+=bool(hits);intersections+=hits
                    rank.append({"direction":direction,"blocked_rays":blocked,"surface_intersections":intersections,"rays":len(points)})
                result={"status":"HINT","method":"exact_cad_ray_ranking_only_not_path_certificate","rank":rank}
            elif operation=="envelope":
                # This solid contains every position of the moving shape.
                # Empty exact intersection therefore certifies a whole path;
                # an intersection is only an inconclusive broad-phase result.
                region=payload["region_bbox"]
                envelope=BRepPrimAPI_MakeBox(gp_Pnt(*region["min"]),gp_Pnt(*region["max"])).Shape()
                result=classify(envelope,b,allow_empty,minimum_clearance=minimum_clearance)
                if result["status"]=="PASS":result["method"]="exact_conservative_translation_envelope"
                elif result["status"]=="COLLISION":
                    if minimum_clearance==0:
                        # Every potential collision lies inside this envelope.
                        # Retaining only the exact intersecting obstacle region
                        # greatly reduces moulded housings before face sweeps.
                        key=_json_hash([payload['a'] if payload.get('assembled_reference') else None,payload['b'],region,masks])
                        _,_,clipped=common(envelope,b,True);clipped_shapes[key]=clipped
                        result={"status":"UNSUPPORTED","reason":"ENVELOPE_OVERLAP_IS_NOT_SHAPE_COLLISION","clipped_obstacle_key":key}
                    else:result={"status":"UNSUPPORTED","reason":"ENVELOPE_OVERLAP_IS_NOT_SHAPE_COLLISION"}
            elif operation=="pair":
                result=classify(moved(a,payload.get("offset",[0,0,0])),b,allow_empty,payload.get("need_distance",True),minimum_clearance)
            elif operation=="sweep":
                clip_key=payload.get('clipped_obstacle_key')
                if clip_key and clip_key in clipped_shapes:b=clipped_shapes[clip_key]
                # Exact occupied sweep for planar closed BREP boundary faces.
                # A swept volume = start solid union all its swept boundary
                # faces. Test each volume independently (union, not odd/even).
                offset=payload["offset"];end=payload.get("end_offset",[0,0,0]);vector=gp_Vec(*[end[i]-offset[i] for i in range(3)]);start=moved(a,offset)
                result=classify(start,b,allow_empty,minimum_clearance=minimum_clearance)
                if result["status"]=="COLLISION":
                    result["reason"]="START_PENETRATION"
                elif result["status"]!="PASS":
                    result["path_fraction"]=0.0
                else:
                    exp=TopExp_Explorer(start,TopAbs_FACE);faces=[];unsupported=0;face_count=0
                    while exp.More():
                        face=TopoDS.Face(exp.Current()) if hasattr(TopoDS,"Face") else TopoDS.Face_s(exp.Current())
                        surface=BRepAdaptor_Surface(face)
                        sweep_role=boundary_sweep_role(face,surface,vector.Normalized())
                        if sweep_role=="unsupported":unsupported+=1
                        if sweep_role!="tangent":faces.append((face,sweep_role))
                        face_count+=1
                        exp.Next()
                    if len(faces)>2048:result={"status":"UNSUPPORTED","reason":"LARGE_BOUNDARY_SWEEP"}
                    else:
                        compound=TopoDS_Compound();builder.MakeCompound(compound)
                        for face,role in faces:
                            if role=="unsupported":
                                # A non-monotone/BSpline face gets a containing
                                # swept box, never an unsafe guessed prism.
                                region=bounds(face)
                                delta=[vector.X(),vector.Y(),vector.Z()]
                                region=_bbox_union([region,_offset_box(region,delta)])
                                low=[v-LINEAR_TOL for v in region["min"]];high=[v+LINEAR_TOL for v in region["max"]]
                                solid=BRepPrimAPI_MakeBox(gp_Pnt(*low),gp_Pnt(*high)).Shape()
                            else:
                                prism=BRepPrimAPI_MakePrism(face,vector,True,True)
                                if not prism.IsDone() or not BRepCheck_Analyzer(prism.Shape()).IsValid():raise ValueError("SWEEP_INVALID")
                                solid=prism.Shape()
                            builder.Add(compound,solid)
                        verdict=classify(compound,b,allow_empty,minimum_clearance=minimum_clearance) if faces else {"status":"PASS"}
                        if verdict["status"]=="PASS":
                            result={"status":"PASS","method":"certified_boundary_envelope_union" if unsupported else "exact_monotone_analytic_boundary_sweep","faces_checked":face_count,"conservative_face_envelopes":unsupported,"linear_tolerance_mm":LINEAR_TOL,"volume_tolerance_mm3":VOLUME_TOL,"required_clearance_mm":minimum_clearance,"distance_mm":verdict.get("distance_mm")}
                        elif unsupported:
                            result={"status":"UNSUPPORTED","reason":"CONSERVATIVE_FACE_ENVELOPE_OVERLAP"}
                        else:result={**verdict,"reason":"SWEPT_VOLUME_PENETRATION" if verdict["status"]=="COLLISION" else verdict.get("reason")}
            else:raise ValueError("UNKNOWN_NATIVE_OPERATION")
            if masks:result["contact_policy_ids"]=[p["id"] for p in masks]
            if payload.get('assembled_reference'):
                result['reference_mode']='assembled_reference'
                result['reference_overlap_mm3']=baseline_volume
                result['reference_scope']='exact_final_moving_unit_only'
            results.put((ident,result))
        except BaseException as exc:results.put((ident,{"status":"UNKNOWN","reason":"NATIVE_ERROR","detail":f"{type(exc).__name__}: {exc}"}))


class GeometrySession:
    def __init__(self,model,timeout=8.0,cancel=None,contact_policies=None,collision_policy=None):
        self.model=model;self.rows={o["id"]:o for o in model["occurrences"]};self.timeout=timeout;self.cancel=cancel
        self.collision_policy=collision_policy or {};self.verification_mode=self.collision_policy.get('mode','nominal_interference')
        self.reference_mode=self.collision_policy.get('reference_mode','strict_geometry')
        if self.verification_mode not in ('nominal_interference','uncertainty_clearance'):raise ValueError('INVALID_COLLISION_POLICY')
        self.context=mp.get_context("spawn");self.process=None;self.counter=0;self.cache={};self.path_cache={};self.policies={}
        units={u["id"]:u for u in model["units"]}
        self.member_groups={o:sorted(unit['occurrence_ids']) for unit in units.values() for o in unit['occurrence_ids']}
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
    def clearance(self,a,b):
        try:values=[float(self.rows[ident].get("representation_uncertainty_mm",0.)) for ident in (a,b)]
        except (TypeError,ValueError):return math.inf
        if not all(math.isfinite(v) and v>=0 for v in values):return math.inf
        total=sum(values)
        requested=float(self.collision_policy.get('minimum_clearance_mm',0.))
        if not math.isfinite(requested) or requested<0:return math.inf
        return max(requested,total+LINEAR_TOL if total>0 else 0.) if self.verification_mode=='uncertainty_clearance' else requested
    def pair(self,a,b,deadline,offset=None,masks=None,need_distance=False,assembled_reference=False):
        x,y=self.rows[a],self.rows[b];offset=offset or [0,0,0];clearance=self.clearance(a,b)
        if not math.isfinite(clearance):return {"status":"UNKNOWN","reason":"INVALID_REPRESENTATION_UNCERTAINTY"}
        if _separated(_offset_box(x["bbox"],offset),y["bbox"]) and _distance_bounds(_offset_box(x["bbox"],offset),y["bbox"])>clearance:return {"status":"PASS","method":"disjoint_exact_bounds","distance_mm":_distance_bounds(_offset_box(x["bbox"],offset),y["bbox"]),"required_clearance_mm":clearance}
        if not x.get("collision_ready",x.get("valid",False)) or not y.get("collision_ready",y.get("valid",False)):
            return {"status":"UNKNOWN","reason":"INVALID_ANALYSIS_REPRESENTATION","occurrence_ids":[a,b]}
        # A None mask selects static final-placement policy; [] is explicit no
        # exemption for travel outside the approved terminal interval.
        if masks is None:masks=self.policies.get(frozenset((a,b)),[])
        return self.request("pair",{"a":a,"b":b,"offset":offset,"masks":masks,"need_distance":need_distance,"minimum_clearance_mm":clearance,"assembled_reference":assembled_reference,'reference_occurrence_ids':self.member_groups.get(a,[a])},deadline)
    def path_pair(self,a,b,path,deadline):
        start=path["start_translation_mm"]
        length=math.sqrt(sum(v*v for v in start))
        policies=self.policies.get(frozenset((a,b)),[])
        available=max(0.,deadline-time.monotonic());key=_json_hash([a,b,path,policies,self.clearance(a,b)])
        cached=self.path_cache.get(key)
        if cached and (cached["result"]["status"] in ("PASS","COLLISION") or available<=cached["available_seconds"]*1.05):return cached["result"]
        boundaries=sorted({0.,1.,*(max(0.,1.-p["terminal_distance_mm"]/length) for p in policies)})
        segments=[]
        for lo,hi in zip(boundaries,boundaries[1:]):
            masks=[p for p in policies if (1-lo)*length<=p["terminal_distance_mm"]+1e-9]
            result=self._path_segment(a,b,path,deadline,lo,hi,masks)
            segments.append({"interval":[lo,hi],"status":result["status"],"method":result.get("method"),"contact_policy_ids":[p["id"] for p in masks],
                'reference_mode':self.reference_mode,'reference_overlap_mm3':result.get('reference_overlap_mm3',0.)})
            if result["status"]!="PASS":
                verdict={**result,"policy_segments":segments}
                self.path_cache[key]={"result":verdict,"available_seconds":available}
                return verdict
        verdict={"status":"PASS","method":"certified_segment_union","policy_segments":segments,
            "certification_scope":"nominal_cad_interference" if self.verification_mode=='nominal_interference' else 'representation_uncertainty_clearance',
            "manufacturing_tolerance_certified":False,"manufacturing_clearance_certified":False,"representation_precision_limited":True,"representation_clearance_required":self.verification_mode=='uncertainty_clearance'}
        self.path_cache[key]={"result":verdict,"available_seconds":available}
        return verdict
    def _path_segment(self,a,b,path,deadline,begin,end,masks):
        total_start=path["start_translation_mm"];start=[v*(1-begin) for v in total_start];end_offset=[v*(1-end) for v in total_start];box=self.rows[a]["bbox"];ob=self.rows[b]["bbox"];clearance=self.clearance(a,b)
        if not math.isfinite(clearance):return {"status":"UNKNOWN","reason":"INVALID_REPRESENTATION_UNCERTAINTY"}
        sweep=_bbox_union([_offset_box(box,end_offset),_offset_box(box,start)])
        if _separated(sweep,ob) and _distance_bounds(sweep,ob)>clearance:return {"status":"PASS","method":"disjoint_swept_bounds","required_clearance_mm":clearance}
        if not self.rows[a].get("collision_ready",self.rows[a].get("valid",False)) or not self.rows[b].get("collision_ready",self.rows[b].get("valid",False)):
            return {"status":"UNKNOWN","reason":"INVALID_ANALYSIS_REPRESENTATION","occurrence_ids":[a,b]}
        # A small axis-aligned box against a complex obstacle is often much
        # cheaper than dozens of curved-face sweeps. It is a sufficient clear
        # certificate only: a box hit never rejects the actual CAD path.
        reference_ids=self.member_groups.get(a,[a])
        reference=self.reference_mode=='assembled_reference' and any(not _separated(self.rows[member]['bbox'],ob) for member in reference_ids)
        envelope=self.request("envelope",{"a":a,"b":b,"region_bbox":sweep,"masks":masks,"minimum_clearance_mm":clearance,'assembled_reference':reference,'reference_occurrence_ids':reference_ids},deadline)
        if envelope["status"]=="PASS":return envelope
        final=self.pair(a,b,deadline,end_offset,masks,assembled_reference=reference)
        if final["status"] in ("COLLISION","UNKNOWN"):return {**final,"path_fraction":end,"reason":"FINAL_PENETRATION" if final["status"]=="COLLISION" and end==1.0 else ("TRANSIT_PENETRATION" if final["status"]=="COLLISION" else final.get("reason")),"collision_phase":"final_placement" if end==1.0 else "transit"}
        swept=self.request("sweep",{"a":a,"b":b,"offset":start,"end_offset":end_offset,"masks":masks,"minimum_clearance_mm":clearance,"clipped_obstacle_key":envelope.get('clipped_obstacle_key'),'assembled_reference':reference,'reference_occurrence_ids':reference_ids},deadline)
        if swept["status"]!="UNSUPPORTED":return swept
        length=math.sqrt(sum(v*v for v in total_start));intervals=[(begin,end)];checked=[];unresolved=[]
        # A shape-set distance is 1-Lipschitz under translation. At a midpoint,
        # gap greater than the maximum interval movement certifies all times.
        while intervals:
            lo,hi=intervals.pop();mid=(lo+hi)/2
            sub_bounds=_bbox_union([_offset_box(box,[v*(1-lo) for v in total_start]),_offset_box(box,[v*(1-hi) for v in total_start])])
            if _separated(sub_bounds,ob) and _distance_bounds(sub_bounds,ob)>clearance:checked.append([lo,hi]);continue
            verdict=self.pair(a,b,deadline,[v*(1-mid) for v in total_start],masks,need_distance=True,assembled_reference=reference)
            if verdict["status"]=="COLLISION":return {**verdict,"path_fraction":mid,"checked_segments":checked}
            if verdict["status"]=="UNKNOWN":return {**verdict,"checked_segments":checked,"unresolved_segments":[[lo,hi]]+intervals}
            gap=verdict.get("distance_mm")
            if gap is None or not math.isfinite(float(gap)):
                return {"status":"UNKNOWN","reason":"CLEARANCE_VALUE_UNAVAILABLE","checked_segments":checked,"unresolved_segments":[[lo,hi]]+intervals}
            if gap>length*(hi-lo)/2+max(LINEAR_TOL,clearance):
                checked.append([lo,hi]);continue
            if length*(hi-lo)<0.002 or len(checked)+len(intervals)>1024:
                unresolved.append([lo,hi]);continue
            intervals.extend([(mid,hi),(lo,mid)])
        return {"status":"UNKNOWN" if unresolved else "PASS","reason":"CONTACT_INTERVAL_UNRESOLVED" if unresolved else None,"method":"conservative_clearance_intervals","checked_segments":checked,"unresolved_segments":unresolved}


def _normalise(model,rules):
    rules=rules or {};units={u["id"]:u for u in model["units"]};errors=[]
    from .planning_settings import settings_errors
    errors.extend(settings_errors(model,rules))
    if model.get("quality",{}).get("inventory_complete") is False:
        errors.append({"code":"MODEL_INVENTORY_INCOMPLETE","message":"Missing transferred source members must be resolved before a product plan can be certified."})
    occurrence_ids={o["id"] for o in model["occurrences"]}
    collision_policy=rules.get('collision_policy') or {}
    if collision_policy.get('mode','nominal_interference') not in ('nominal_interference','uncertainty_clearance'):
        errors.append({'code':'INVALID_COLLISION_POLICY'})
    try:
        clearance=float(collision_policy.get('minimum_clearance_mm',0.))
        if not math.isfinite(clearance) or clearance<0:raise ValueError()
    except (ValueError,TypeError):errors.append({'code':'INVALID_MINIMUM_CLEARANCE'})
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
    if isinstance(rules.get('support_fixtures',[]),list) and any(isinstance(item,dict) and item.get('unit_id') not in selected|installed for item in rules.get('support_fixtures',[])):
        errors.append({'code':'INVALID_SUPPORT_CONFIGURATION','message':'Support fixtures must belong to the selected or initial product scope.'})
    process=compile_process_rules(model,rules,installed,targets);errors.extend(process['errors'])
    pred={u:set() for u in targets}
    for edge in list(rules.get("precedence",[]))+process['edges']:
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
    return units,installed,targets,pred,errors,process


def _contact_proposals(pairs):
    """Unapproved local fit review proposals from measured final intersections."""
    proposals={}
    for pair in pairs:
        if pair.get('status')!='COLLISION' or not pair.get('intersection_bbox'):continue
        key=tuple(sorted((pair['occurrence_a'],pair['occurrence_b'])))
        proposals[key]={"id":"proposal_"+_json_hash(key)[:12],"unit_a":pair['a'],"unit_b":pair['b'],
            "region_bbox":pair['intersection_bbox'],"terminal_distance_mm":None,"approved_by":None,"approval_reference":None,
            "basis":"최종 BREP 형상의 실제 겹침 영역입니다. 나사 체결·압입 등 의도된 접촉인지 확인되지 않았으며 허용 구간과 근거 검토가 필요합니다.",
            "requires_engineering_review":True,"measured_overlap_mm3":pair['common_volume_mm3'],
            "occurrence_a":pair['occurrence_a'],"occurrence_b":pair['occurrence_b'],"applied":False}
    return list(proposals.values())


def _path_final_overlaps(unit_id,pairs):
    return [{**pair,'a':unit_id,'b':pair['obstacle_unit_id'],'occurrence_a':pair['moving_occurrence_id'],'occurrence_b':pair['obstacle_occurrence_id']}
        for pair in pairs if pair.get('collision_phase')=='final_placement' and pair.get('status')=='COLLISION']


def _audit(model,units,initial,targets,engine,deadline,progress=None):
    invalid=[uid for uid in sorted(initial|targets) if not units[uid].get('collision_ready',True)
        or any(not engine.rows[o].get('collision_ready',engine.rows[o].get('valid',False)) for o in units[uid]['occurrence_ids'])]
    if invalid:
        return {'status':'UNKNOWN','pairs':[],'contact_policy_proposals':[],
            'reason':'INVALID_ANALYSIS_REPRESENTATION','unit_ids':invalid,'static_fit_certified':False}
    if engine.reference_mode=='assembled_reference':
        return {'status':'PASS','pairs':[],'contact_policy_proposals':[],
            'scope':'accepted assembled reference; new penetration checked continuously along every movement',
            'reference_mode':'assembled_reference','assumed_valid_final_assembly':True,
            'static_fit_certified':False,'reference_normalization':'exact obstacle minus final moving shape; no pair-wide exemption'}
    relevant=sorted(initial|targets);pairs=[];pass_count=0;potential=0;unresolved=0;work=[]
    for index,a in enumerate(relevant):
        for b in relevant[index+1:]:
            unit_clearance=max((engine.clearance(oa,ob) for oa in units[a]["occurrence_ids"] for ob in units[b]["occurrence_ids"]),default=0.)
            if _separated(units[a]["bbox"],units[b]["bbox"]) and _distance_bounds(units[a]["bbox"],units[b]["bbox"])>unit_clearance:pass_count+=1;continue
            potential+=1
            for oa in units[a]["occurrence_ids"]:
                for ob in units[b]["occurrence_ids"]:
                    if _separated(engine.rows[oa]["bbox"],engine.rows[ob]["bbox"]) and _distance_bounds(engine.rows[oa]["bbox"],engine.rows[ob]["bbox"])>engine.clearance(oa,ob):continue
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
    return {"status":status,"pairs":pairs,"contact_policy_proposals":_contact_proposals(pairs),"broadphase_disjoint_pairs":pass_count,"potential_unit_pairs":potential,"potential_occurrence_pairs":len(work),"unknown_pair_count":unresolved,
        "scope":"initial installed pairs and final placement across all selected units","contact_policy":"approved pair-specific spatial regions only; no unapproved overlap ignored","applied_contact_policy_ids":sorted({p["id"] for policies in engine.policies.values() for p in policies}),"linear_tolerance_mm":LINEAR_TOL,"volume_tolerance_mm3":VOLUME_TOL}


def _action(unit,installed,units,engine,direction,distance,deadline):
    path=make_path(direction,distance);evidence=[];certificates=[];status="PASS"
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
                if verdict["status"]=="PASS":
                    certificates.append({"moving_occurrence_id":a,"obstacle_occurrence_id":b,"obstacle_unit_id":obstacle,
                        "method":verdict.get("method"),"policy_segments":verdict.get("policy_segments",[])})
                if verdict["status"]!="PASS":
                    if verdict["status"]=="COLLISION" and not verdict.get("collision_phase"):
                        verdict={**verdict,"collision_phase":"transit","reason":verdict.get("reason") or "TRANSIT_PENETRATION"}
                    evidence.append({"moving_occurrence_id":a,"obstacle_occurrence_id":b,"obstacle_unit_id":obstacle,**verdict})
                    if verdict["status"]=="COLLISION":status="COLLISION"
                    elif status!="COLLISION":status="UNKNOWN"
                    if status=="COLLISION":return {"status":status,"path":path,"pairs":evidence,"reason":verdict.get("reason")}
        if time.monotonic()>=deadline:return {"status":"UNKNOWN","reason":"TIME_BUDGET","path":path,"pairs":evidence}
    path["verified"]=status=="PASS";path["executable"]=status=="PASS"
    path['certification_scope']='nominal_cad_interference' if engine.verification_mode=='nominal_interference' else 'representation_uncertainty_clearance'
    path['manufacturing_tolerance_certified']=False
    path['manufacturing_clearance_certified']=False;path['representation_precision_limited']=True
    return {"status":status,"path":path,"pairs":evidence,"certificates":certificates,"reason":next((p.get('reason') for p in evidence if p.get('reason')),None)}


def _prepare_support(model,rules,initial,targets,engine,deadline,process,pred,progress=None):
    from .support import prepare_support,infer_support_process
    support=prepare_support(model,rules,initial,targets,engine,deadline,process=process)
    support['_progress']=progress
    if progress:progress({'stage':'support_inference','current':0,'total':len(targets),'message':'실제 형상에서 지지·체결 관계 분석 중'})
    if not support['errors']:
        for before,after in infer_support_process(support):
            if after in pred:pred[after].add(before)
            if after in initial and before not in initial:
                support['errors'].append({'code':'INITIAL_STATE_RULE_CONFLICT','before':before,'after':after})
        reached=set(initial);pending=set(targets)
        while pending:
            eligible={u for u in pending if pred[u]<=reached}
            if not eligible:
                support['errors'].append({'code':'CYCLIC_OR_MISSING_PREREQUISITE','unit_ids':sorted(pending)});break
            reached|=eligible;pending-=eligible
    return support


def _priority(unit):
    role={"housing":0,"plate":1,"component":2,"connector":3,"relay":4,"busbar":5,"fastener":6,"seal":7,"cover":8}.get(unit.get("role"),3)
    return role,unit["bbox"]["center"][2],unit["name"],unit["id"]


def _default_distance(model):return max(model["bbox"]["size"])*1.35+25.0


def _directions_for_unit(unit,model,defaults,engine=None,initial=None,deadline=None):
    """Rank entry axes; this recommendation is never a collision certificate."""
    if engine is not None and initial and deadline is not None and time.monotonic()<deadline:
        obstacles=[u for u in model["units"] if u["id"] in initial and u.get("role")=="housing"]
        scores={_json_hash(d):[0,0] for d in defaults};usable=False
        for obstacle in obstacles:
            for occurrence in obstacle["occurrence_ids"]:
                hint=engine.request("entry_probe",{"a":unit["occurrence_ids"][0],"b":occurrence,"unit_bbox":unit["bbox"],"directions":list(defaults),"distance_mm":_default_distance(model)},min(deadline,time.monotonic()+4.))
                if hint.get("status")=="HINT":
                    usable=True
                    for row in hint["rank"]:
                        score=scores[_json_hash(row["direction"])];score[0]+=row["blocked_rays"];score[1]+=row["surface_intersections"]
        if usable:return sorted(defaults,key=lambda d:tuple(scores[_json_hash(d)]))
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
    # A reviewer name cannot replace the automatic representation proof.
    # Minor metadata discrepancies are resolved during measured calibration.
    return False


def _step(unit,index,direction,distance,verdict):
    executable=verdict["status"]=="PASS" and verdict.get("path",{}).get("verified") is True
    return {"id":"step_"+unit["id"],"unit_id":unit["id"],"name":unit["name"],"index":index,"direction":direction,"distance_mm":distance,
        "path":{**verdict["path"],"executable":True} if executable else None,"status":verdict["status"],"diagnostics":verdict.get("pairs",[]),"reason":verdict.get("reason"),
        "executable":executable,"planning_state":"READY" if executable else ("BLOCKED" if verdict["status"]=="COLLISION" else "UNPLANNED"),
        "proposed_entry":{"direction":direction,"distance_mm":distance},"certificates":verdict.get("certificates",[]),
        "torque":None,"caution":"","checked":False}


def _playback(steps):
    prefix=0
    for step in steps:
        if not step.get("executable") or not step.get("path",{}).get("verified"):break
        prefix+=1
    return {"policy":"verified_prefix_only","executable_prefix_count":prefix,"total_count":len(steps),
        "blocked_count":sum(s["status"]=="COLLISION" for s in steps),"unplanned_count":len(steps)-prefix}


def _precedence_suggestions(attempts,initial):
    """Only a demonstrated newly installed blocker justifies a suggestion.

    This is a route-specific suggestion for review, not an automatically
    applied manufacturing rule or proof that all alternative routes fail.
    """
    passed={};suggestions={}
    for attempt in attempts:
        route=(attempt["unit_id"],_json_hash(attempt["direction"]),attempt["distance_mm"])
        installed=frozenset(attempt["installed_unit_ids"])
        if attempt["status"]=="PASS":passed.setdefault(route,set()).add(installed)
        if attempt["status"]!="COLLISION":continue
        for pair in attempt.get("pairs",[]):
            blocker=pair.get("obstacle_unit_id")
            if pair.get("collision_phase")!="transit" or blocker in initial or blocker not in installed:continue
            if installed-{blocker} not in passed.get(route,set()):continue
            key=(attempt["unit_id"],blocker)
            suggestions[key]={"before":key[0],"after":key[1],"kind":"route_specific_blocker","applied":False,
                "direction":attempt["direction"],"distance_mm":attempt["distance_mm"],"basis":"Same complete entry passed before this blocker was installed and collides after it was installed. Alternative entries may remain possible."}
    return list(suggestions.values())


def generate_plan(model,rules=None,cache_dir=None,progress=None,cancel=None):
    rules=rules or {}
    if rules.get('generation_method','disassembly_reverse')=='forward':
        return generate_forward_plan(model,rules,cache_dir,progress,cancel)
    from .disassembly import generate_reverse_plan
    return generate_reverse_plan(model,rules,cache_dir,progress,cancel)


def generate_forward_plan(model,rules=None,cache_dir=None,progress=None,cancel=None):
    rules=rules or {};started=time.monotonic();budget=max(1.,float(rules.get("max_seconds",DEFAULT_ANALYSIS_SECONDS)));deadline=started+budget
    units,initial,targets,pred,errors,process=_normalise(model,rules)
    directions=rules.get("directions",["-Z","+Z","-Y","+Y","-X","+X"])
    distance=float(rules.get("distance_mm") or _default_distance(model));attempts=0;beam_width=int(rules.get("beam_width",4))
    audit={"status":"UNKNOWN","pairs":[],"reason":"RULE_ERRORS"};steps=[];complete_verified=False;termination="completed";attempt_trace=[];decision_traces=[]
    geometry_reviewed=_geometry_review_resolved(model,rules,units,initial,targets) if not errors else False
    if errors:
        return {"id":"plan_"+_json_hash([model["fingerprint"],rules])[:16],"status":"BLOCKED","steps":[],"diagnostics":errors,"coverage":{"complete":False,"expected":len(targets),"actual":0},"algorithm_version":ALGORITHM_VERSION}
    with GeometrySession(model,timeout=float(rules.get("native_timeout_seconds",DEFAULT_NATIVE_TIMEOUT_SECONDS)),cancel=cancel,contact_policies=rules.get("contact_policies"),collision_policy=rules.get('collision_policy')) as engine:
        audit=_audit(model,units,initial,targets,engine,started+budget*.15,progress)
        from .support import prepare_support,support_state,support_summary
        support=_prepare_support(model,rules,initial,targets,engine,deadline,process,pred,progress)
        # Each frontier contains separate state copies; no mutation leaks.
        frontier=[(0.,frozenset(initial),[])];complete=[];best=frontier[0]
        search_deadline=started+budget*.95
        while frontier and time.monotonic()<search_deadline:
            children=[]
            for cost,installed,current in frontier:
                state_candidates=[]
                pending=targets-set(installed)
                if not pending:complete.append((cost,installed,current));continue
                process_allowed,_=process_eligibility(pending,installed,process)
                eligible=sorted((u for u in pending&process_allowed if pred[u]<=set(installed)),key=lambda u:_priority(units[u]))
                actions=[];passed_units=set();final_blocked_units=set()
                for uid in eligible:
                    per_unit=rules.get("entry_conditions",{}).get(uid,{})
                    dirs=[per_unit["direction"]] if per_unit.get("direction") else _directions_for_unit(units[uid],model,directions,engine,initial,search_deadline)
                    length=float(per_unit.get("distance_mm",distance))
                    actions.extend((direction_index,uid,direction,length) for direction_index,direction in enumerate(dirs))
                actions.sort(key=lambda a:(a[0],_priority(units[a[1]])))
                # Try each eligible unit's best direction before spending the
                # budget on five alternatives of the first complex component.
                for _,uid,direction,length in actions:
                    if time.monotonic()>=search_deadline:break
                    if uid in passed_units or uid in final_blocked_units:continue
                    seating=support_state(set(installed)|{uid},support)
                    if seating['status']!='PASS':
                        attempt_trace.append({'unit_id':uid,'unit_name':units[uid]['name'],'installed_unit_ids':sorted(installed),'direction':direction,
                            'distance_mm':length,'status':'UNKNOWN' if seating['status']=='UNKNOWN' else 'UNSUPPORTED','reason':seating['reason'],'pairs':[], 'support_diagnostic':seating})
                        continue
                    # Each constituent has its own complete path proof. A
                    # compound unit must not receive a single-solid budget.
                    candidate_budget=float(rules.get("candidate_seconds",DEFAULT_CANDIDATE_SECONDS))
                    action_deadline=min(search_deadline,time.monotonic()+candidate_budget)
                    action_started=time.monotonic()
                    verdict=_action(units[uid],installed,units,engine,direction,length,action_deadline);attempts+=1
                    attempt_trace.append({"unit_id":uid,"unit_name":units[uid]["name"],"installed_unit_ids":sorted(installed),"direction":direction,"distance_mm":length,"status":verdict["status"],"reason":verdict.get("reason"),"pairs":verdict.get("pairs",[]),"elapsed_seconds":round(time.monotonic()-action_started,3)})
                    # No entry direction can remove the same fixed final
                    # intersection. Keep the measured blocker for review.
                    if _path_final_overlaps(uid,verdict.get('pairs',[])):final_blocked_units.add(uid)
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
                    # Advancing a bounded set of verified branches produces a
                    # useful safe prefix. Exhaustively trying every remaining
                    # part before extending any branch used the entire budget
                    # on first-step candidates in complex real assemblies.
                    if len(passed_units)>=max(2,beam_width):break
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
                process_allowed,process_reasons=process_eligibility(pending,installed,process)
                eligible=sorted((u for u in pending&process_allowed if pred[u]<=installed),key=lambda u:_priority(units[u]))
                if not eligible:eligible=sorted((u for u in pending if pred[u]<=installed),key=lambda u:_priority(units[u]))
                if not eligible:break
                uid=eligible[0];per_unit=rules.get("entry_conditions",{}).get(uid,{})
                direction=per_unit.get("direction",_directions_for_unit(units[uid],model,directions)[0]);length=float(per_unit.get("distance_mm",distance))
                rejects=[t for t in attempt_trace if t["unit_id"]==uid and t["status"]!="PASS"]
                overlaps=[p for p in audit.get("pairs",[]) if uid in (p["a"],p["b"]) and p["status"]=="COLLISION"]
                overlaps.extend(p for attempt in rejects for p in _path_final_overlaps(uid,attempt.get('pairs',[])))
                allowed=[per_unit["direction"]] if per_unit.get("direction") else _directions_for_unit(units[uid],model,directions)
                tested={_json_hash(t["direction"]):t for t in rejects if set(t["installed_unit_ids"])==set(best[1])}
                blocked=bool(allowed) and all(tested.get(_json_hash(d),{}).get("status")=="COLLISION" for d in allowed)
                verdict={"status":"COLLISION" if overlaps or blocked else "UNKNOWN","reason":"FINAL_PLACEMENT_OVERLAP" if overlaps else ("ALL_ALLOWED_ENTRIES_COLLIDE" if blocked else "NO_VERIFIED_ENTRY_PATH"),"pairs":overlaps or [p for t in tested.values() for p in t.get("pairs",[])]}
                step=_step(units[uid],len(steps)+1,direction,length,verdict)
                if uid in process_reasons:
                    step['process_diagnostic']=process_reasons[uid];step['reason']=process_reasons[uid]['code']
                step["rejected_candidate_summary"]=[{"direction":r["direction"],"status":r["status"],"reason":r.get("reason"),"obstacle_unit_ids":sorted({p.get("obstacle_unit_id") for p in r.get("pairs",[]) if p.get("obstacle_unit_id")})} for r in rejects]
                steps.append(step);installed.add(uid);pending.remove(uid)
    path_final_pairs=[pair for attempt in attempt_trace for pair in _path_final_overlaps(attempt['unit_id'],attempt.get('pairs',[]))]
    audit['contact_policy_proposals']=_contact_proposals(audit.get('pairs',[])+path_final_pairs)
    selected_installed=set(initial)
    for step in steps:
        for trace in decision_traces:
            if set(trace["installed_ids"])==selected_installed and step["unit_id"] in trace["feasible_unit_ids"]:
                trace["selected_unit_id"]=step["unit_id"];trace["label_status"]="recommendation_only_requires_expert_preference_review"
        selected_installed.add(step["unit_id"])
    status="PASS" if complete_verified else "UNKNOWN"
    diagnostics=list(errors)+process['reviews']
    if rules.get('collision_policy',{}).get('mode','nominal_interference')=='nominal_interference':
        diagnostics.append({'code':'NOMINAL_CAD_INTERFERENCE_SCOPE','severity':'info','message':'Continuous nominal CAD geometry is checked for positive volume penetration. Zero-volume contact is allowed. Source representation uncertainty and manufacturing tolerance clearance are not certified.'})
    if not geometry_reviewed:
        diagnostics.append({"code":"MODEL_MAPPING_REVIEW_REQUIRED","message":"Resolve ambiguous source-to-unit matches before confirming."} if model.get("quality",{}).get("matching_ready") is False else {"code":"GEOMETRY_EXACT_METHOD_REVIEW_REQUIRED","message":"Automatic representation calibration did not resolve the exact check. Keep the result unresolved; reviewer metadata cannot certify geometry."})
    if audit["status"]!="PASS":diagnostics.append({"code":"FINAL_PLACEMENT_REQUIRES_REVIEW","message":"Final geometry contains penetration or unresolved pairs; reordering alone cannot repair final fit.","collision_count":sum(p["status"]=="COLLISION" for p in audit.get("pairs",[])),"unknown_count":sum(p["status"]=="UNKNOWN" for p in audit.get("pairs",[]))})
    if any(s["status"]!="PASS" for s in steps):diagnostics.append({"code":"DRAFT_REQUIRES_PATH_VALIDATION","message":"The complete work inventory is retained. Only the contiguous certified prefix has executable movement; unresolved steps have no path."})
    return {"id":"plan_"+_json_hash([model["fingerprint"],rules,steps])[:16],"status":status,"kind":"verified_recommendation" if status=="PASS" else "review_draft",
        "steps":steps,"initial_unit_ids":sorted(initial),"diagnostics":diagnostics,"placement_audit":audit,"process":process_summary(process),'support':support_summary(support),"coverage":{"complete":{s["unit_id"] for s in steps}==targets,"expected":len(targets),"actual":len(steps),"initial_count":len(initial),"complete_product_scope":initial|targets==set(units),"selected_unit_ids":sorted(initial|targets)},
        "algorithm_version":ALGORITHM_VERSION,"dependency_hash":_json_hash([model["fingerprint"],rules]),"attempt_trace":attempt_trace,"decision_trace":decision_traces,"playback":_playback(steps),"precedence_suggestions":_precedence_suggestions(attempt_trace,initial),"search":{"attempts":attempts,"beam_width":beam_width,"termination":termination,"optimality":"not_globally_proven","elapsed_seconds":round(time.monotonic()-started,3)},
        "verification_mode":rules.get('collision_policy',{}).get('mode','nominal_interference'),"manufacturing_tolerance_certified":False,"manufacturing_clearance_certified":False,"representation_precision_limited":True,
        "scope":"fixed orientation, single rigid unit, straight translation; tool access, deformation, manufacturing tolerance and fixture stability untested"}


def validate_plan(model,plan,rules=None,cache_dir=None,progress=None,cancel=None):
    rules=rules or {};started=time.monotonic();budget=max(1.,float(rules.get("validation_seconds",rules.get("max_seconds",DEFAULT_ANALYSIS_SECONDS))));deadline=started+budget
    units,initial,targets,pred,errors,process=_normalise(model,rules);steps=plan.get("steps",[])
    ids=[s.get("unit_id") for s in steps]
    if len(ids)!=len(set(ids)) or set(ids)!=targets:errors.append({"code":"INVALID_PLAN_COVERAGE","missing":sorted(targets-set(ids)),"unexpected":sorted(set(ids)-targets),"duplicate":len(ids)!=len(set(ids))})
    installed=set(initial);validated=[];prefix_ready=True;path_final_pairs=[]
    if errors:return {"status":"BLOCKED","diagnostics":errors,"steps":[],"coverage":{"complete":False},"dependency_hash":_json_hash([model["fingerprint"],rules,steps])}
    with GeometrySession(model,timeout=float(rules.get("native_timeout_seconds",DEFAULT_NATIVE_TIMEOUT_SECONDS)),cancel=cancel,contact_policies=rules.get("contact_policies"),collision_policy=rules.get('collision_policy')) as engine:
        audit=_audit(model,units,initial,targets,engine,started+budget*.15,progress)
        from .support import prepare_support,support_state,support_summary
        support=_prepare_support(model,rules,initial,targets,engine,deadline,process,pred,progress)
        if support['errors']:
            return {'status':'BLOCKED','diagnostics':support['errors'],'steps':[], 'support':support_summary(support),'coverage':{'complete':True}}
        for index,step in enumerate(steps):
            uid=step["unit_id"]
            process_allowed,process_reasons=process_eligibility(targets-installed,installed,process)
            if uid not in process_allowed:
                diagnostic=process_reasons[uid]
                verdict={"status":"UNKNOWN" if 'REVIEW' in diagnostic['code'] else "COLLISION","reason":diagnostic['code'],"process_diagnostic":diagnostic}
            elif not pred[uid]<=installed:verdict={"status":"COLLISION","reason":"PRECEDENCE_VIOLATION","missing_predecessors":sorted(pred[uid]-installed)}
            else:
                try:
                    from .planning_settings import entry_settings
                    allowed,_,_=entry_settings(rules,uid)
                    if step['direction'] not in allowed:raise ValueError('DIRECTION_CONSTRAINT_CONFLICT')
                    expected=make_path(step["direction"],step["distance_mm"])
                    supplied=step.get("path") or {}
                    mismatch=any(supplied.get(key)!=expected[key] for key in ("kind","start_translation_mm","end_translation_mm"))
                    if mismatch:
                        # Explicit rebuild means manual direction/distance edits
                        # cannot replay the old path or retain its PASS marker.
                        errors.append({"code":"PATH_REBUILT_FROM_CURRENT_ENTRY","severity":"info","step_id":step["id"]})
                    verdict=_action(units[uid],installed,units,engine,step["direction"],step["distance_mm"],deadline)
                    if verdict['status']=='PASS':
                        seating=support_state(installed|{uid},support)
                        if seating['status']!='PASS':
                            verdict={**verdict,'status':'UNKNOWN' if seating['status']=='UNKNOWN' else 'COLLISION',
                                'reason':seating['reason'],'support_diagnostic':seating}
                        else:verdict['support_evidence']=seating
                except (ValueError,TypeError,KeyError) as exc:verdict={"status":"UNKNOWN","reason":str(exc)}
            executable=prefix_ready and verdict["status"]=="PASS" and bool(verdict.get("path",{}).get("verified"))
            state="READY" if executable else ("BLOCKED" if verdict["status"]=="COLLISION" else ("WAITING_PREDECESSOR" if verdict["status"]=="PASS" else "UNPLANNED"))
            updated={**step,"status":verdict["status"],"path":{**verdict["path"],"executable":True} if executable else None,"diagnostics":verdict.get("pairs",[]),"reason":verdict.get("reason"),
                "executable":executable,"planning_state":state,"proposed_entry":{"direction":step.get("direction"),"distance_mm":step.get("distance_mm")},"certificates":verdict.get("certificates",[])}
            if verdict.get('process_diagnostic'):updated['process_diagnostic']=verdict['process_diagnostic']
            if verdict.get('support_diagnostic'):updated['support_diagnostic']=verdict['support_diagnostic']
            if verdict.get('support_evidence'):updated['support_evidence']=verdict['support_evidence']
            path_final_pairs.extend(_path_final_overlaps(uid,verdict.get('pairs',[])))
            validated.append(updated);installed.add(uid);prefix_ready=executable
            if progress:progress({"stage":"plan_validation","current":index+1,"total":len(steps),"message":step["name"],"status":verdict["status"]})
    audit['contact_policy_proposals']=_contact_proposals(audit.get('pairs',[])+path_final_pairs)
    reviewed=_geometry_review_resolved(model,rules,units,initial,targets)
    if not reviewed:errors.append({"code":"MODEL_MAPPING_REVIEW_REQUIRED","message":"Resolve ambiguous source-to-unit matches before confirming."} if model.get("quality",{}).get("matching_ready") is False else {"code":"GEOMETRY_EXACT_METHOD_REVIEW_REQUIRED","message":"The current representation has not passed automatic geometry checks."})
    status="PASS" if reviewed and audit["status"]=="PASS" and all(s["status"]=="PASS" for s in validated) else ("BLOCKED" if audit["status"]=="COLLISION" or any(s["status"]=="COLLISION" for s in validated) else "UNKNOWN")
    return {"status":status,"steps":validated,"diagnostics":errors+process['reviews'],"placement_audit":audit,"process":process_summary(process),'support':support_summary(support),"playback":_playback(validated),"coverage":{"complete":True,"expected":len(targets),"actual":len(steps)},
        "algorithm_version":ALGORITHM_VERSION,"verification_mode":rules.get('collision_policy',{}).get('mode','nominal_interference'),"manufacturing_tolerance_certified":False,"manufacturing_clearance_certified":False,"representation_precision_limited":True,
        "dependency_hash":_json_hash([model["fingerprint"],rules,steps]),"elapsed_seconds":round(time.monotonic()-started,3)}

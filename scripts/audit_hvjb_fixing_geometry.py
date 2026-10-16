"""Read real BREP cylinder axes for fixing candidates; never auto-approve."""
import argparse,json,math,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from OCP.BRep import BRep_Builder
from OCP.BRepTools import BRepTools
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepGProp import BRepGProp
from OCP.BRepBndLib import BRepBndLib
from OCP.Bnd import Bnd_Box
from OCP.GProp import GProp_GProps
from OCP.GeomAbs import GeomAbs_Cylinder
from OCP.TopAbs import TopAbs_FACE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS_Shape,TopoDS


def cylinders(path):
    shape=TopoDS_Shape();builder=BRep_Builder()
    if not BRepTools.Read_s(shape,str(path),builder):raise RuntimeError('BREP_READ_FAILED')
    out=[];exp=TopExp_Explorer(shape,TopAbs_FACE)
    while exp.More():
        face=TopoDS.Face_s(exp.Current()) if hasattr(TopoDS,'Face_s') else TopoDS.Face(exp.Current())
        surface=BRepAdaptor_Surface(face)
        if surface.GetType()==GeomAbs_Cylinder:
            cylinder=surface.Cylinder();axis=cylinder.Axis();point=axis.Location();direction=axis.Direction()
            bounds=Bnd_Box();BRepBndLib.AddOptimal_s(face,bounds,False,False)
            low,high=bounds.CornerMin(),bounds.CornerMax()
            props=GProp_GProps();BRepGProp.SurfaceProperties_s(face,props)
            out.append({'origin':[point.X(),point.Y(),point.Z()],'axis':[direction.X(),direction.Y(),direction.Z()],
                'radius_mm':cylinder.Radius(),'area_mm2':props.Mass(),'orientation':str(face.Orientation()),
                'bbox':{'min':[low.X(),low.Y(),low.Z()],'max':[high.X(),high.Y(),high.Z()]}})
        exp.Next()
    return out


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--model',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    raw=json.loads(args.model.read_text(encoding='utf-8'));model=raw.get('result',raw);rows={o['id']:o for o in model['occurrences']}
    targets=[u for u in model['units'] if u['name'].startswith(('PLT-','RLY-','MBD-'))]
    fixtures=[u for u in model['units'] if u.get('role')=='fastener']
    target_cylinders={}
    for target in targets:
        target_cylinders[target['id']]=[dict(c,occurrence_id=ident) for ident in target['occurrence_ids'] for c in cylinders(rows[ident]['brep_path'])]
    result={'model_fingerprint':model['fingerprint'],'policy':'Measured axes and spatial interfaces are candidates only. Fastener purpose and screen-relative upper/lower order require review.','targets':targets,'associations':[]}
    for fixture in fixtures:
        faces=[c for ident in fixture['occurrence_ids'] for c in cylinders(rows[ident]['brep_path'])]
        if not faces:continue
        primary=max(faces,key=lambda c:c['area_mm2']);direction=primary['axis'];origin=primary['origin']
        axis=max(range(3),key=lambda i:abs(direction[i]));transverse=[i for i in range(3) if i!=axis]
        if abs(direction[axis])<.999:continue
        matches=[]
        for target in targets:
            for c in target_cylinders[target['id']]:
                alignment=abs(sum(a*b for a,b in zip(direction,c['axis'])))
                if alignment<.9999:continue
                radial=math.sqrt(sum((origin[i]-c['origin'][i])**2 for i in transverse))
                if radial>.05:continue
                gap=max(0.,fixture['bbox']['min'][axis]-c['bbox']['max'][axis],c['bbox']['min'][axis]-fixture['bbox']['max'][axis])
                if gap>1.:continue
                relative=(sum(fixture['bbox'][s][2] for s in ('min','max'))/2-target['bbox']['min'][2])/max(.001,target['bbox']['size'][2])
                matches.append({'target_unit_id':target['id'],'target_name':target['name'],'axis_offset_mm':radial,'axial_gap_mm':gap,
                    'candidate_surface':c,'height_fraction_in_target':relative,
                    'region_hint':'lower_mounting_region' if relative<.25 else ('upper_terminal_or_fixing_region_review' if relative>.65 else 'middle_region_review')})
        result['associations'].append({'fastener_unit_id':fixture['id'],'name':fixture['name'],'center':fixture['bbox']['center'],'axis':direction,'axis_origin':origin,'matches':matches,'status':'review'})
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'fasteners':len(result['associations']),'matched':sum(bool(r['matches']) for r in result['associations']),'rows':[{'name':r['name'],'id':r['fastener_unit_id'],'center':r['center'],'targets':sorted({m['target_name']+':'+m['region_hint'] for m in r['matches']})} for r in result['associations'] if r['matches']]},ensure_ascii=True,indent=2))


if __name__=='__main__':main()

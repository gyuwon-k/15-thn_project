"""Explicit placement/fixing groups; geometric proximity never approves a group."""
from __future__ import annotations

FASTENER_ROLES={"fastener","bolt","nut","washer","screw"}


def compile_process_rules(model,rules,initial,targets):
    units={u['id']:u for u in model['units']}
    policy=rules.get('process_policy') or {}
    enabled=bool(policy.get('require_fixing_before_next_component'))
    default_required=[g.get('target_unit_id') for g in rules.get('process_groups',[]) if isinstance(g,dict) and g.get('target_unit_id')]
    required=set(policy.get('required_unit_ids',policy.get('required_target_unit_ids',default_required))) if enabled else set()
    groups={};owners={};edges=[];errors=[];reviews=[]
    if required-set(units):errors.append({'code':'PROCESS_UNKNOWN_REQUIRED_UNIT','unit_ids':sorted(required-set(units))})
    for raw in rules.get('process_groups',[]):
        if not isinstance(raw,dict):errors.append({'code':'INVALID_PROCESS_GROUP'});continue
        target=raw.get('target_unit_id');fasteners=list(raw.get('fastener_unit_ids') or [])
        mode=raw.get('fixation_mode','separate_fasteners')
        if target not in units or target in groups or len(fasteners)!=len(set(fasteners)) or target in fasteners or set(fasteners)-set(units):
            errors.append({'code':'INVALID_PROCESS_GROUP_MEMBERS','target_unit_id':target});continue
        if mode not in {'separate_fasteners','self_fixed','not_required','cover_seated'} or (mode!='separate_fasteners' and fasteners):
            errors.append({'code':'INVALID_FIXATION_MODE','target_unit_id':target});continue
        retainer=raw.get('retaining_cover_unit_id')
        if mode=='cover_seated':
            if retainer not in units or retainer==target or retainer in initial:
                errors.append({'code':'INVALID_RETAINING_COVER','target_unit_id':target,'retaining_cover_unit_id':retainer});continue
            edges.append((target,retainer))
        ordered=raw.get('ordered_fastener_groups') or ([fasteners] if fasteners else [])
        flat=[ident for batch in ordered for ident in batch]
        if len(flat)!=len(set(flat)) or set(flat)!=set(fasteners) or any(not batch for batch in ordered):
            errors.append({'code':'INVALID_ORDERED_FASTENER_GROUPS','target_unit_id':target});continue
        if set(fasteners)&set(owners):
            errors.append({'code':'FASTENER_ASSIGNED_TO_MULTIPLE_COMPONENTS','unit_ids':sorted(set(fasteners)&set(owners))});continue
        confirmed=raw.get('status') in {'confirmed','verified_geometry','user_confirmed','assumed'} and bool(raw.get('basis'))
        if raw.get('status')=='assumed' and (not raw.get('assumption') or not raw.get('requires_field_confirmation')):confirmed=False
        if mode=='separate_fasteners' and not fasteners:confirmed=False
        group={**raw,'target_unit_id':target,'fastener_unit_ids':fasteners,'ordered_fastener_groups':ordered,'fixation_mode':mode,'eligible_for_scheduling':confirmed}
        groups[target]=group
        for fastener in fasteners:owners[fastener]=target;edges.append((target,fastener))
        for earlier,later in zip(ordered,ordered[1:]):
            edges.extend((a,b) for a in earlier for b in later)
        if not confirmed:reviews.append({'code':'PROCESS_FASTENER_ASSOCIATION_REVIEW_REQUIRED','unit_id':target,'message':'Confirm the actual fixing hardware and its order; proximity alone is insufficient.'})
    for target in sorted(required-set(groups)):
        reviews.append({'code':'PROCESS_FASTENER_ASSOCIATION_MISSING','unit_id':target,'message':'Declare associated fasteners or explicitly confirm self-fixed/no-separate-fastener treatment.'})
    unassigned={u for u in targets if u in units and units[u].get('role') in FASTENER_ROLES and u not in owners}
    if enabled and unassigned:reviews.append({'code':'PROCESS_UNASSIGNED_FASTENERS','unit_ids':sorted(unassigned),'message':'Assign fixing and terminal hardware to its actual component before scheduling it.'})
    return {'enabled':enabled,'required_unit_ids':required,'groups':groups,'fastener_owners':owners,'unassigned_fastener_ids':unassigned,'edges':edges if enabled else [],'errors':errors,'reviews':reviews if enabled else []}


def process_eligibility(pending,installed,compiled):
    """Return permitted next IDs and review/blocking reasons for excluded IDs."""
    pending=set(pending);installed=set(installed);reasons={}
    if not compiled['enabled']:return pending,reasons
    active=[g for target,g in compiled['groups'].items() if target in installed and set(g['fastener_unit_ids'])-installed]
    if active:
        # More than one active group can occur only in an imported initial
        # state/manual proposal. Finish those groups before placing anything.
        allowed=set()
        for group in active:
            if not group['eligible_for_scheduling']:continue
            # Keep the height/batch rule inside the eligibility contract too:
            # inferred groups can be added after precedence normalisation.
            for batch in group['ordered_fastener_groups']:
                missing=set(batch)-installed
                if missing:
                    allowed.update(missing & pending);break
        for ident in pending-allowed:
            reasons[ident]={'code':'PROCESS_FIXING_REQUIRED','active_component_ids':[g['target_unit_id'] for g in active],
                'missing_fastener_ids':sorted({f for g in active for f in g['fastener_unit_ids'] if f not in installed})}
        return allowed,reasons
    allowed=set()
    for ident in pending:
        group=compiled['groups'].get(ident)
        owner=compiled['fastener_owners'].get(ident)
        if ident in compiled['unassigned_fastener_ids']:
            reasons[ident]={'code':'PROCESS_FASTENER_ASSOCIATION_REVIEW_REQUIRED','unit_id':ident}
        elif owner and owner not in installed:
            reasons[ident]={'code':'PROCESS_COMPONENT_NOT_PLACED','target_unit_id':owner}
        elif (ident in compiled['required_unit_ids'] and not group) or (group and not group['eligible_for_scheduling']):
            reasons[ident]={'code':'PROCESS_FASTENER_ASSOCIATION_REVIEW_REQUIRED','unit_id':ident}
        else:allowed.add(ident)
    return allowed,reasons


def process_summary(compiled):
    return {'enabled':compiled['enabled'],'required_unit_ids':sorted(compiled['required_unit_ids']),
        'groups':list(compiled['groups'].values()),'review_items':compiled['reviews'],
        'policy':'Place a component, complete its eligible fixing groups in order, then place another component. Assumed retention remains subject to field confirmation.'}


def _cylinder_faces(path):
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.BRepAdaptor import BRepAdaptor_Surface,BRepAdaptor_Curve
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepBndLib import BRepBndLib
    from OCP.Bnd import Bnd_Box
    from OCP.GProp import GProp_GProps
    from OCP.GeomAbs import GeomAbs_Cylinder,GeomAbs_Circle
    from OCP.TopAbs import TopAbs_FACE,TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS_Shape,TopoDS
    shape=TopoDS_Shape();builder=BRep_Builder()
    if not BRepTools.Read_s(shape,str(path),builder):raise ValueError('BREP_READ_FAILED')
    out=[];exp=TopExp_Explorer(shape,TopAbs_FACE)
    while exp.More():
        face=TopoDS.Face_s(exp.Current()) if hasattr(TopoDS,'Face_s') else TopoDS.Face(exp.Current());surface=BRepAdaptor_Surface(face)
        if surface.GetType()==GeomAbs_Cylinder:
            cylinder=surface.Cylinder();axis=cylinder.Axis();point=axis.Location();direction=axis.Direction()
            box=Bnd_Box();BRepBndLib.AddOptimal_s(face,box,False,False);low,high=box.CornerMin(),box.CornerMax()
            props=GProp_GProps();BRepGProp.SurfaceProperties_s(face,props)
            out.append({'origin':[point.X(),point.Y(),point.Z()],'axis':[direction.X(),direction.Y(),direction.Z()],
                'radius_mm':cylinder.Radius(),'area_mm2':props.Mass(),'orientation':str(face.Orientation()),
                'bbox':{'min':[low.X(),low.Y(),low.Z()],'max':[high.X(),high.Y(),high.Z()]}})
        exp.Next()
    exp=TopExp_Explorer(shape,TopAbs_EDGE)
    while exp.More():
        edge=TopoDS.Edge_s(exp.Current()) if hasattr(TopoDS,'Edge_s') else TopoDS.Edge(exp.Current());curve=BRepAdaptor_Curve(edge)
        if curve.GetType()==GeomAbs_Circle:
            circle=curve.Circle();axis=circle.Axis();point=axis.Location();direction=axis.Direction()
            box=Bnd_Box();BRepBndLib.AddOptimal_s(edge,box,False,False);low,high=box.CornerMin(),box.CornerMax()
            out.append({'origin':[point.X(),point.Y(),point.Z()],'axis':[direction.X(),direction.Y(),direction.Z()],
                'radius_mm':circle.Radius(),'area_mm2':0.,'orientation':'circular_edge','evidence_kind':'circular_edge',
                'bbox':{'min':[low.X(),low.Y(),low.Z()],'max':[high.X(),high.Y(),high.Z()]}})
        exp.Next()
    return out


def fastener_association_evidence(model):
    """Measure coaxial cylindrical interfaces; this cannot infer bolt purpose."""
    import math
    rows={o['id']:o for o in model['occurrences']};units=model['units'];errors=[]
    targets=[u for u in units if u.get('role') not in FASTENER_ROLES]
    fasteners=[u for u in units if u.get('role') in FASTENER_ROLES];surfaces={}
    for unit in units:
        try:
            surfaces[unit['id']]=[dict(c,occurrence_id=ident) for ident in unit['occurrence_ids'] for c in _cylinder_faces(rows[ident]['brep_path'])]
            if unit.get('role')=='busbar':
                # A slotted hole has two equal-radius semicylinders whose
                # axes straddle the fastener centre. Requiring the paired
                # geometry avoids treating an arbitrary nearby edge as a hole.
                cylinders=[c for c in surfaces[unit['id']] if c.get('evidence_kind')!='circular_edge']
                slots=[]
                for index,a in enumerate(cylinders):
                    for b in cylinders[index+1:]:
                        if a['occurrence_id']!=b['occurrence_id'] or abs(a['radius_mm']-b['radius_mm'])>.001:continue
                        if abs(sum(x*y for x,y in zip(a['axis'],b['axis'])))<.9999:continue
                        delta=[b['origin'][i]-a['origin'][i] for i in range(3)];axial=sum(delta[i]*a['axis'][i] for i in range(3))
                        separation=math.sqrt(max(0.,sum(v*v for v in delta)-axial*axial))
                        if abs(axial)>.01 or not .01<separation<=2*a['radius_mm']:continue
                        slots.append({**a,'origin':[(x+y)/2 for x,y in zip(a['origin'],b['origin'])],'evidence_kind':'paired_slot_semicylinders',
                            'paired_origins':[a['origin'],b['origin']],'slot_axis_separation_mm':separation,
                            'bbox':{'min':[min(a['bbox']['min'][i],b['bbox']['min'][i]) for i in range(3)],'max':[max(a['bbox']['max'][i],b['bbox']['max'][i]) for i in range(3)]}})
                surfaces[unit['id']].extend(slots)
        except Exception as exc:surfaces[unit['id']]=[];errors.append({'unit_id':unit['id'],'reason':type(exc).__name__+': '+str(exc)})
    associations=[]
    for fastener in fasteners:
        faces=surfaces[fastener['id']]
        if not faces:
            associations.append({'fastener_unit_id':fastener['id'],'name':fastener['name'],'center':fastener['bbox']['center'],'matches':[],'status':'review'});continue
        primary=max(faces,key=lambda c:c['area_mm2']);direction=primary['axis'];origin=primary['origin'];matches=[]
        for target in targets:
            for cylinder in surfaces[target['id']]:
                alignment=abs(sum(a*b for a,b in zip(direction,cylinder['axis'])))
                if alignment<.9999:continue
                delta=[cylinder['origin'][i]-origin[i] for i in range(3)]
                axial=sum(delta[i]*direction[i] for i in range(3))
                radial=math.sqrt(max(0.,sum(v*v for v in delta)-axial*axial))
                if radial>.05:continue
                # Axis-aligned projection of the actual face and fastener
                # bounds supplies a conservative axial-nearness filter.
                def interval(box):
                    low=sum(direction[i]*(box['min'][i] if direction[i]>=0 else box['max'][i]) for i in range(3))
                    high=sum(direction[i]*(box['max'][i] if direction[i]>=0 else box['min'][i]) for i in range(3))
                    return low,high
                f0,f1=interval(fastener['bbox']);c0,c1=interval(cylinder['bbox']);gap=max(0.,f0-c1,c0-f1)
                if gap>1.:continue
                relative=(fastener['bbox']['center'][2]-target['bbox']['min'][2])/max(.001,target['bbox']['size'][2])
                matches.append({'target_unit_id':target['id'],'target_name':target['name'],'axis_offset_mm':radial,'axial_gap_mm':gap,'candidate_surface':cylinder,
                    'height_fraction_in_target':relative,'region_hint':'lower_mounting_region' if relative<.25 else ('upper_terminal_or_fixing_region_review' if relative>.65 else 'middle_region_review')})
        associations.append({'fastener_unit_id':fastener['id'],'name':fastener['name'],'center':fastener['bbox']['center'],'axis':direction,'axis_origin':origin,'matches':matches,'status':'review'})
    return {'method':'Measured BREP cylinder axes, circular edges, paired slot semicylinders and axial intervals; geometric candidates alone do not approve semantic fixing purpose.','associations':associations,'errors':errors}


def height_ordered_fasteners(model,identifiers):
    """Higher world-Z centres first; effectively equal heights share a batch."""
    units={u['id']:u for u in model['units']};rows={o['id']:o for o in model['occurrences']}
    def uncertainty(ident):
        values=[rows[o].get('representation_uncertainty_mm') for o in units[ident]['occurrence_ids']]
        return max([float(v) for v in values if isinstance(v,(int,float))]+[0.])
    sorted_ids=sorted(identifiers,key=lambda ident:(-units[ident]['bbox']['center'][2],ident));batches=[]
    for ident in sorted_ids:
        if batches:
            anchor=batches[-1][0];tolerance=max(.1,uncertainty(ident)+uncertainty(anchor))
            if abs(units[anchor]['bbox']['center'][2]-units[ident]['bbox']['center'][2])<=tolerance:
                batches[-1].append(ident);continue
        batches.append([ident])
    return batches


def recommend_process_rules(model):
    """Read-only candidate profile, scoped to the current model fingerprint."""
    units=model['units'];fixed={u['id'] for u in units if u.get('fixed')}
    targets=[u for u in units if u['id'] not in fixed and u.get('role') not in FASTENER_ROLES]
    evidence=fastener_association_evidence(model);groups=[];precedence=[]
    known_hvjb=model.get('source',{}).get('sha256')=='3048cab7cc1dd38b1a39af1084f645c34fe91514aa88ee97e2144273b68bc2fe'
    plate=next((u for u in targets if u['name']=='PLT-KD000'),None)
    housing=next((u for u in units if u['name']=='MBD-KD000'),None)
    relays=[u for u in targets if u['name']=='RLY-KET400']
    cover=next((u for u in targets if u['name']=='COV-KD000'),None)
    if known_hvjb and plate and housing:
        precedence.append({'before':housing['id'],'after':plate['id'],'basis':'User requested housing, plate, relay process order.'})
        precedence.extend({'before':plate['id'],'after':relay['id'],'basis':'User requested plate before relay.'} for relay in relays)
    for target in targets:
        candidate=[];excluded=[]
        if known_hvjb:
            for row in evidence['associations']:
                matches=[m for m in row['matches'] if m['target_unit_id']==target['id']]
                all_targets={m['target_unit_id'] for m in row['matches']}
                if target['name']=='PLT-KD000' and housing and housing['id'] in all_targets and row['name']=='BLT-WASM620R' and matches:candidate.append(row['fastener_unit_id'])
                elif target['name']=='RLY-KET400' and plate and plate['id'] in all_targets and row['name']=='NUT-FLGM5A' and any(m['region_hint']=='lower_mounting_region' for m in matches):candidate.append(row['fastener_unit_id'])
                elif target['name']=='RLY-KET400' and matches and any(m['region_hint']=='upper_terminal_or_fixing_region_review' for m in matches):excluded.append(row['fastener_unit_id'])
                elif target['name'] in {'LV CONT','HVSC 2800','BLK-KD000','COV-KD000'} and housing and housing['id'] in all_targets and matches:candidate.append(row['fastener_unit_id'])
                elif target.get('role')=='busbar' and matches:
                    supports=[u for u in units if u['id'] in all_targets and u['id']!=target['id'] and (u['name'] in {'RLY-KET400','BLK-KD000','HVSC 2800'})]
                    if supports:
                        candidate.append(row['fastener_unit_id'])
                        precedence.extend({'before':support['id'],'after':target['id'],'basis':'Measured terminal fastener interface joins this busbar to the supporting component.'} for support in supports)
        expected={'PLT-KD000':4,'RLY-KET400':2,'LV CONT':2,'HVSC 2800':4,'BLK-KD000':2,'COV-KD000':10,'BUS-KD000':3,'BUS-KD100':3,'BUS-KD200':3,'BUS-KD300':2}
        proven=known_hvjb and target['name'] in expected and len(candidate)==expected[target['name']]
        groups.append({'id':'fix_'+target['id'],'target_unit_id':target['id'],'target_name':target['name'],'fastener_unit_ids':candidate,
            'ordered_fastener_groups':height_ordered_fasteners(model,candidate),'fixation_mode':'separate_fasteners','status':'verified_geometry' if proven else 'review',
            'basis':'대상 부품과 지지 부품의 실제 BREP 체결 축·원형 또는 슬롯 홀을 대조하고 제공 제품의 공정 의도를 적용한 대응입니다. 체결 유지력이나 토크를 확인한 것은 아닙니다.' if proven else '체결품 대응 또는 별도 체결품이 없는 고정 방식의 검토가 필요합니다.',
            'excluded_upper_terminal_candidates':excluded,'position_order_status':'higher_world_z_first_equal_height_unordered',
            'height_order_basis':'User requested higher physical positions first; centres within 0.1 mm or combined representation uncertainty share a batch.'})
        if known_hvjb and target['name'] in {'AIR-BENT0A','SEL-KD000'}:
            group=groups[-1];air=target['name']=='AIR-BENT0A'
            group.update(status='assumed',fixation_mode='self_fixed' if air else 'cover_seated',assumption=True,requires_field_confirmation=True,
                basis='공정 설계 가정: 제공 모델에 별도 체결품이 없는 벤트는 부품 자체 고정 구조로 유지되는 것으로 취급합니다. 실제 고정 방식과 유지력은 현장 확인이 필요하며 이 가정은 수정할 수 있습니다.' if air else '공정 설계 가정: 실은 홈에 안착한 상태를 유지하다가 후속 커버 조립으로 고정되는 것으로 취급합니다. 커버 전 안착 유지와 실제 고정 상태는 현장 확인이 필요하며 이 가정은 수정할 수 있습니다.')
            if not air:
                group['retaining_cover_unit_id']=cover['id'] if cover else None
                if not cover:group['status']='review'
    if known_hvjb and plate and relays:
        # The supplied process starts with the plate then both fixed relays.
        # Later connector/busbar/cover placements follow that explicit intent.
        for target in targets:
            if target['id']!=plate['id'] and target not in relays:
                precedence.extend({'before':relay['id'],'after':target['id'],'basis':'User requested mainbody, plate, then relay before later components.'} for relay in relays)
        if cover:
            precedence.extend({'before':u['id'],'after':cover['id'],'basis':'The enclosing cover follows the inner and interface components.'} for u in targets if u['id']!=cover['id'])
    unique={(p['before'],p['after']):p for p in precedence};precedence=list(unique.values())
    verified=sum(g['status']=='verified_geometry' for g in groups);assumed=sum(g['status']=='assumed' for g in groups);unresolved=[g['target_name'] for g in groups if g['status']=='review']
    return {'model_fingerprint':model['fingerprint'],'status':'review','profile':'provided_hvjb_user_intent' if known_hvjb else 'generic_geometry_candidates',
        'process_policy':{'require_fixing_before_next_component':True,'required_unit_ids':[u['id'] for u in targets]},'process_groups':groups,'precedence':precedence,
        'evidence':evidence,'scope':f'실제 BREP 체결 인터페이스와 제공 제품의 공정 의도를 대조한 체결 그룹 {verified}개, 수정 가능한 고정 가정 {assumed}개입니다. 가정은 물리적 고정 확인을 뜻하지 않으며, 체결 토크·고정 유지력·공구 접근성은 인증하지 않습니다.',
        'unresolved_questions':(['체결 또는 자체 고정 방식을 확인할 대상: '+', '.join(unresolved)] if unresolved else []),
        'field_confirmation_items':[{'target_unit_id':g['target_unit_id'],'target_name':g['target_name'],'basis':g['basis']} for g in groups if g.get('requires_field_confirmation')]}

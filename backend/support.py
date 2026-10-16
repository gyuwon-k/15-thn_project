"""Grounded assembly support, independent of collision-free travel.

World -Z is gravity. Exact CAD contact and a small downward displacement
establish a bearing relation; bounds only discard impossible contacts. This
is a seating/attachment test, not a strength, friction, or tipping simulation.
"""
from __future__ import annotations

import math
import time

PROBE_MM = .01
CONTACT_MM = .01
VOLUME_TOL = 1e-5
RADIAL_FIT_CLEARANCE_MM = .5
_CYLINDER_CACHE = {}


def _cylinders(shape):
    """Trimmed, oriented actual cylinder faces, cached in the native worker."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepTools import BRepTools
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.GeomAbs import GeomAbs_Cylinder
    key = hash(shape)
    cached = _CYLINDER_CACHE.get(key)
    if cached and cached[0].IsSame(shape):
        return cached[1]
    rows = []; exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current()) if hasattr(TopoDS,'Face_s') else TopoDS.Face(exp.Current())
        surface = BRepAdaptor_Surface(face)
        if surface.GetType() == GeomAbs_Cylinder:
            cylinder = surface.Cylinder(); axis = cylinder.Axis(); origin = axis.Location(); direction = axis.Direction()
            vector = [direction.X(), direction.Y(), direction.Z()]; point = [origin.X(), origin.Y(), origin.Z()]
            u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
            props = GProp_GProps(); BRepGProp.SurfaceProperties_s(face, props)
            expected = 2*math.pi*cylinder.Radius()*abs(v1-v0)
            full = abs((u1-u0)-2*math.pi) <= 1e-5 and abs(props.Mass()-expected) <= max(1e-4,expected*1e-6)
            rows.append({'origin': point, 'axis': vector, 'radius_mm': cylinder.Radius(),
                         'span': [v0, v1], 'full_cylinder': full, 'female': face.Orientation() == TopAbs_REVERSED})
        exp.Next()
    if len(_CYLINDER_CACHE) > 2048:
        _CYLINDER_CACHE.clear()
    _CYLINDER_CACHE[key] = (shape, rows)
    return rows


def _coaxial(a, b):
    alignment = sum(x*y for x,y in zip(a['axis'],b['axis']))
    if abs(alignment) < .999999:
        return None
    delta = [b['origin'][i]-a['origin'][i] for i in range(3)]
    axial = sum(delta[i]*a['axis'][i] for i in range(3))
    radial = math.sqrt(max(0.,sum(v*v for v in delta)-axial*axial))
    if radial > CONTACT_MM:
        return None
    b_span = sorted(axial+alignment*v for v in b['span'])
    overlap = min(a['span'][1],b_span[1])-max(a['span'][0],b_span[0])
    return radial, overlap, b_span


def _head_direction(shaft, cylinders):
    for head in cylinders:
        if head['female'] or not head['full_cylinder'] or head['radius_mm'] < shaft['radius_mm']*1.25:
            continue
        pair = _coaxial(shaft,head)
        if pair is None:
            continue
        _, overlap, span = pair
        if overlap > CONTACT_MM or span[1]-span[0] < .25:
            continue
        if abs(span[1]-shaft['span'][0]) <= CONTACT_MM:
            return list(shaft['axis'])
        if abs(span[0]-shaft['span'][1]) <= CONTACT_MM:
            return [-v for v in shaft['axis']]
    return None


def _fits(a, b):
    matches = []
    for side, male_rows, female_rows in (('a',_cylinders(a),_cylinders(b)),('b',_cylinders(b),_cylinders(a))):
        for male in male_rows:
            if male['female'] or not male['full_cylinder']:
                continue
            for female in female_rows:
                if not female['female'] or not female['full_cylinder']:
                    continue
                clearance = female['radius_mm']-male['radius_mm']
                if not -1e-5 <= clearance <= RADIAL_FIT_CLEARANCE_MM+1e-5:
                    continue
                pair = _coaxial(male,female)
                if pair is None or pair[1] < min(.5,male['radius_mm']*.2):
                    continue
                matches.append({'male_side':side, 'axis':male['axis'], 'radial_clearance_mm':max(0.,clearance),
                                'axis_offset_mm':pair[0], 'axial_overlap_mm':pair[1],
                                'gravity_radially_constrained':abs(male['axis'][2]) <= .01,
                                'head_insertion_direction':_head_direction(male,male_rows)})
    return matches


def native_support_pair(a, b, common, moved, probe_mm=PROBE_MM):
    """Run in GeometrySession's replaceable worker, on original final solids."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    distance = BRepExtrema_DistShapeShape()
    distance.LoadS1(a); distance.LoadS2(b); distance.SetMultiThread(True); distance.Perform()
    if not distance.IsDone():
        raise ValueError('SUPPORT_DISTANCE_FAILED')
    gap = float(distance.Value())
    if not math.isfinite(gap):
        raise ValueError('SUPPORT_NONFINITE_DISTANCE')
    result = {'status': 'PASS', 'method': 'exact_downward_bearing_probe',
              'contact': gap <= CONTACT_MM, 'distance_mm': gap,
              'probe_mm': probe_mm, 'a_supported_by_b': False, 'b_supported_by_a': False}
    fits = _fits(a,b)
    result['coaxial_fits'] = fits
    if any(fit['gravity_radially_constrained'] for fit in fits):
        # A full cylindrical bore captures a horizontal shaft radially even
        # with normal CAD fit clearance. Axial retention is a separate matter.
        result.update(a_supported_by_b=True,b_supported_by_a=True,radially_constrained=True)
    if gap > CONTACT_MM:
        return result
    baseline, _ = common(a, b)
    a_down, _ = common(moved(a, [0., 0., -probe_mm]), b)
    b_down, _ = common(a, moved(b, [0., 0., -probe_mm]))
    # Increasing overlap means actual material resists downward displacement.
    # Existing nominal fit overlap is the reference, never a new support proof.
    result.update(baseline_volume_mm3=baseline,
                  a_downward_added_volume_mm3=max(0., a_down-baseline),
                  b_downward_added_volume_mm3=max(0., b_down-baseline),
                  a_supported_by_b=result['a_supported_by_b'] or a_down-baseline > VOLUME_TOL,
                  b_supported_by_a=result['b_supported_by_a'] or b_down-baseline > VOLUME_TOL)
    for fit in fits:
        direction = fit.get('head_insertion_direction')
        if not direction:
            continue
        offset = [v*probe_mm for v in direction]
        volume, _ = common(moved(a,offset),b) if fit['male_side']=='a' else common(a,moved(b,offset))
        if volume-baseline > VOLUME_TOL:
            result[fit['male_side']+'_head_seated_on_'+('b' if fit['male_side']=='a' else 'a')] = True
    return result


def _bounds_gap(a, b):
    return math.sqrt(sum(max(0., a['min'][i]-b['max'][i], b['min'][i]-a['max'][i])**2
                         for i in range(3)))


def prepare_support(model, rules, initial, targets, engine, deadline, process=None):
    """Prepare lazy exact relations; no all-pairs OCC pass before search."""
    units = {u['id']: u for u in model['units']}
    rows = {o['id']: o for o in model['occurrences']}
    selected = set(initial) | set(targets)
    fixtures = set(); errors = []
    for item in rules.get('support_fixtures', []):
        if not isinstance(item, dict) or item.get('unit_id') not in selected or not str(item.get('basis') or '').strip():
            errors.append({'code': 'INVALID_SUPPORT_FIXTURE', 'message': 'A fixture needs a selected unit and its physical holding basis.'})
        else:
            fixtures.add(item['unit_id'])
    # A generic STEP may not identify a housing/root. In that case use the
    # product's lowest exact-CAD extent as a declared workbench assumption.
    # A lowest extremum proves contact with this infinite plane, unlike the
    # overlap of two finite part boxes, which can never prove part contact.
    workbench = None; floor_roots = set()
    if not initial and not fixtures and selected:
        floor = min(units[uid]['bbox']['min'][2] for uid in selected)
        floor_roots = {uid for uid in selected if units[uid].get('collision_ready') is True
                       and all(rows[o].get('collision_ready') is True for o in units[uid]['occurrence_ids'])
                       and abs(units[uid]['bbox']['min'][2]-floor) <= CONTACT_MM}
        workbench = {'mode': 'assumed_horizontal_workbench', 'height_mm': floor,
                     'resting_unit_ids': sorted(floor_roots),
                     'basis': 'No initial fixture was supplied. Units at the lowest validated CAD extent rest on an assumed horizontal workbench; they enter the state only when assembled.'}
    nearby = {uid: set() for uid in selected}
    for index, a in enumerate(sorted(selected)):
        for b in sorted(selected)[index+1:]:
            if _bounds_gap(units[a]['bbox'], units[b]['bbox']) <= CONTACT_MM:
                nearby[a].add(b); nearby[b].add(a)
    return {'enabled': True, 'gravity_direction': '-Z', 'roots': set(initial) | fixtures | floor_roots,
            'fixture_unit_ids': fixtures, 'initial_unit_ids': set(initial), 'selected': selected,
            'workbench_assumption': workbench,
            'errors': errors, 'process': process or {'enabled': False, 'groups': {}, 'fastener_owners': {}},
            '_units': units, '_rows': rows, '_engine': engine, '_deadline': deadline,
            '_nearby': nearby, '_pairs': {}, '_state_cache': {}, '_probe_count': 0}


def _pair(a, b, compiled):
    key = tuple(sorted((a, b)))
    if key in compiled['_pairs']:
        result = compiled['_pairs'][key]
    elif b not in compiled['_nearby'].get(a, set()):
        result = {'status': 'PASS', 'supports': set(), 'head_seats':set(), 'contact': False, 'coaxial_fit':False, 'evidence': [], 'method': 'separated_bounds'}
        compiled['_pairs'][key] = result
    else:
        first, second = key
        result = {'status': 'PASS', 'supports': set(), 'head_seats':set(), 'contact': False, 'coaxial_fit':False, 'evidence': []}
        for oa in compiled['_units'][first]['occurrence_ids']:
            for ob in compiled['_units'][second]['occurrence_ids']:
                if _bounds_gap(compiled['_rows'][oa]['bbox'], compiled['_rows'][ob]['bbox']) > CONTACT_MM:
                    continue
                if time.monotonic() >= compiled['_deadline']:
                    verdict = {'status': 'UNKNOWN', 'reason': 'SUPPORT_BUDGET_REACHED'}
                else:
                    verdict = compiled['_engine'].request('support', {'a': oa, 'b': ob, 'probe_mm': PROBE_MM}, compiled['_deadline'])
                    compiled['_probe_count'] += 1
                result['evidence'].append({'occurrence_a': oa, 'occurrence_b': ob, **verdict})
                if verdict.get('status') != 'PASS':
                    result['status'] = 'UNKNOWN'
                    continue
                result['contact'] |= bool(verdict.get('contact'))
                result['coaxial_fit'] |= bool(verdict.get('coaxial_fits'))
                if verdict.get('a_supported_by_b'):
                    result['supports'].add((second, first))
                if verdict.get('b_supported_by_a'):
                    result['supports'].add((first, second))
                if verdict.get('a_head_seated_on_b'):
                    result['head_seats'].add((first,second))
                if verdict.get('b_head_seated_on_a'):
                    result['head_seats'].add((second,first))
        compiled['_pairs'][key] = result
    return result


def infer_support_process(compiled):
    """Add only unambiguous actual shaft/head fixing relations, before search."""
    from .process_rules import FASTENER_ROLES, height_ordered_fasteners
    process = compiled['process']; candidates = {}; existing = set(process.get('fastener_owners', {}))
    for uid, unit in sorted(compiled['_units'].items()):
        if uid not in compiled['selected'] or uid in existing or unit.get('role') not in FASTENER_ROLES:
            continue
        if compiled.get('_progress'):
            compiled['_progress']({'stage':'support_inference','message':'체결품의 지지 대상 확인 중 · '+unit.get('name',uid)})
        owners = []
        for other in sorted(compiled['_nearby'].get(uid, set())):
            if compiled['_units'][other].get('role') in FASTENER_ROLES:
                continue
            relation = _pair(uid,other,compiled)
            if (uid,other) in relation['head_seats']:
                owners.append(other)
        if len(owners) == 1 and owners[0] not in compiled['roots']:
            candidates.setdefault(owners[0],[]).append(uid)
    new_groups = []
    for target, fasteners in candidates.items():
        # Respect a user's declared treatment, including explicit no-fastener
        # and self-retention modes, rather than silently rewriting that rule.
        if target in process.get('groups', {}):
            continue
        ordered = height_ordered_fasteners({'units':list(compiled['_units'].values()),
                                            'occurrences':list(compiled['_rows'].values())},fasteners)
        new_groups.append({'id':'geometry_fix_'+target, 'target_unit_id':target,
                           'fastener_unit_ids':fasteners,'ordered_fastener_groups':ordered,
                           'fixation_mode':'separate_fasteners','status':'verified_geometry',
                           'eligible_for_scheduling':True,'inferred':True,
                           'basis':'Exact coaxial full-bore shaft fit and larger concentric head seated against this unique component; fastening strength/torque is not certified.'})
    if new_groups and not process.get('enabled'):
        process['enabled'] = True
        # Inference only imposes the groups it can prove. Unidentified hardware
        # retains the geometry/support checks, not a new manual-review gate.
        process['unassigned_fastener_ids'] = set()
    added_edges = []
    for group in new_groups:
        target = group['target_unit_id']; process['groups'][target] = group
        process['required_unit_ids'].add(target)
        for fastener in group['fastener_unit_ids']:
            process['fastener_owners'][fastener] = target
            process['unassigned_fastener_ids'].discard(fastener)
            added_edges.append((target,fastener))
        for earlier,later in zip(group['ordered_fastener_groups'],group['ordered_fastener_groups'][1:]):
            added_edges.extend((a,b) for a in earlier for b in later)
    process['edges'].extend(added_edges)
    compiled['_state_cache'].clear()
    compiled['inferred_process_groups'] = new_groups
    return added_edges


def _anchor(target, group, grounded, compiled):
    """A declared fixing group must bridge the target to a grounded body."""
    fasteners = set(group.get('fastener_unit_ids') or [])
    for fastener in sorted(fasteners):
        owner_relation = _pair(fastener,target,compiled)
        if not (owner_relation['contact'] or owner_relation['coaxial_fit']):
            continue
        for anchor in sorted((compiled['_nearby'].get(fastener, set()) & grounded) - fasteners - {target}):
            relation = _pair(fastener, anchor, compiled)
            if relation['contact'] or (anchor,fastener) in relation['supports']:
                # Eligible process groups already identify the owner's actual
                # fixing hardware; this check proves its grounded anchor too.
                return anchor
    return None


def support_state(installed, compiled):
    """Every installed unit must reach a declared root through real support."""
    installed = frozenset(installed)
    if installed in compiled['_state_cache']:
        return compiled['_state_cache'][installed]
    if compiled['errors']:
        return {'status': 'UNKNOWN', 'reason': 'INVALID_SUPPORT_CONFIGURATION',
                'unsupported_unit_ids': sorted(installed), 'diagnostics': compiled['errors']}
    grounded = set(installed) & compiled['roots']; temporary = set(); evidence = {}
    process = compiled['process']; groups = process.get('groups', {})
    while True:
        added = False
        for uid in sorted(set(installed)-grounded):
            owner = process.get('fastener_owners', {}).get(uid) if process.get('enabled') else None
            if owner in grounded and groups.get(owner, {}).get('eligible_for_scheduling'):
                owner_relation = _pair(uid,owner,compiled)
                if owner_relation['contact'] or owner_relation['coaxial_fit']:
                    grounded.add(uid); evidence[uid] = {'kind': 'declared_fixing_hardware', 'support_unit_id': owner}; added = True
                    continue
            for base in sorted(compiled['_nearby'].get(uid, set()) & grounded):
                relation = _pair(uid, base, compiled)
                if (base, uid) in relation['supports']:
                    grounded.add(uid); evidence[uid] = {'kind': 'exact_bearing', 'support_unit_id': base}; added = True
                    break
            if uid in grounded:
                continue
            group = groups.get(uid) if process.get('enabled') else None
            if not group or not group.get('eligible_for_scheduling'):
                continue
            if group.get('fixation_mode') == 'self_fixed':
                anchor = next((base for base in sorted(compiled['_nearby'].get(uid, set()) & grounded)
                               if _pair(uid, base, compiled)['contact']), None)
                if anchor:
                    grounded.add(uid); evidence[uid] = {'kind': 'declared_self_retention', 'support_unit_id': anchor}; added = True
            elif group.get('fixation_mode') == 'separate_fasteners':
                anchor = _anchor(uid, group, grounded, compiled)
                if anchor:
                    missing = set(group['fastener_unit_ids'])-installed
                    grounded.add(uid); added = True
                    if missing:
                        temporary.add(uid)
                    evidence[uid] = {'kind': 'temporary_hold_until_immediate_fixing' if missing else 'declared_mechanical_attachment',
                                     'support_unit_id': anchor, 'missing_fastener_ids': sorted(missing)}
        if not added:
            break
    unsupported = set(installed)-grounded
    # An unresolved exact pair is not evidence of either support or absence.
    unknown = any(result['status'] != 'PASS' and set(key) & unsupported
                  for key, result in compiled['_pairs'].items())
    result = {'status': ('UNKNOWN' if unknown else 'UNSUPPORTED') if unsupported else 'PASS',
              'reason': ('SUPPORT_GEOMETRY_UNRESOLVED' if unknown else 'UNSUPPORTED_COMPONENT') if unsupported else None,
              'unsupported_unit_ids': sorted(unsupported), 'grounded_unit_ids': sorted(grounded),
              'temporary_supported_unit_ids': sorted(temporary), 'support_evidence': evidence}
    compiled['_state_cache'][installed] = result
    return result


def support_eligibility(pending, installed, compiled):
    allowed = set(); reasons = {}
    for uid in sorted(pending):
        result = support_state(set(installed) | {uid}, compiled)
        if result['status'] == 'PASS':
            allowed.add(uid)
        else:
            reasons[uid] = {'code': result['reason'], 'unit_id': uid, **result}
    return allowed, reasons


def support_removal(uid, occupied, compiled):
    """Removing a supporting body must not leave remaining bodies floating."""
    before = support_state(occupied, compiled)
    if before['status'] != 'PASS':
        return {**before, 'code': before['reason'], 'removed_unit_id': uid, 'phase': 'before_removal'}
    unfastening = support_unfastening(uid, occupied, compiled)
    if unfastening['status'] != 'PASS':
        return unfastening
    after = support_state(set(occupied)-{uid}, compiled)
    if after['status'] != 'PASS':
        return {**after, 'code': 'REMOVAL_LEAVES_UNSUPPORTED_COMPONENTS', 'removed_unit_id': uid, 'phase': 'after_removal'}
    return {**after, 'code': None, 'removed_unit_id': uid}


def support_unfastening(uid, occupied, compiled):
    """Do not open a fixing group while its owner still supports other parts.

    Assembly places an owner and completes its hardware before another part.
    The reverse therefore must remove those later supported parts before it
    starts unfastening the owner. Checking only one removed bolt would miss
    this commitment and enter a state with no legal continuation.
    """
    process = compiled['process']; occupied = set(occupied)
    owner = process.get('fastener_owners', {}).get(uid) if process.get('enabled') else None
    group = process.get('groups', {}).get(owner)
    if owner not in occupied or owner in compiled['roots'] or not group or not group.get('eligible_for_scheduling'):
        return {'status':'PASS','code':None}
    after_group = occupied - set(group['fastener_unit_ids']) - {owner}
    remaining = support_state(after_group, compiled)
    if remaining['status'] == 'PASS':
        return {'status':'PASS','code':None}
    return {**remaining, 'code':'REMOVAL_FIXING_DEPENDENTS',
            'removed_unit_id':uid, 'target_unit_id':owner,
            'dependent_unit_ids':remaining['unsupported_unit_ids'],
            'phase':'before_unfastening_group'}


def support_summary(compiled):
    relations = []
    for key, result in compiled['_pairs'].items():
        if result['supports'] or result['head_seats'] or result['status'] != 'PASS':
            relations.append({'unit_ids': list(key), 'status': result['status'],
                              'support_edges': [{'support_unit_id': a, 'supported_unit_id': b}
                                                for a, b in sorted(result['supports'])],
                              'head_seats': [{'fastener_unit_id':a,'owner_unit_id':b} for a,b in sorted(result['head_seats'])],
                              'evidence': result['evidence']})
    return {'enabled': True, 'gravity_direction': '-Z', 'root_unit_ids': sorted(compiled['roots']),
            'fixture_unit_ids': sorted(compiled['fixture_unit_ids']), 'contact_tolerance_mm': CONTACT_MM,
            'radial_fit_clearance_mm':RADIAL_FIT_CLEARANCE_MM,
            'inferred_process_groups':compiled.get('inferred_process_groups',[]),
            'workbench_assumption': compiled['workbench_assumption'],
            'probe_distance_mm': PROBE_MM, 'native_probe_count': compiled['_probe_count'],
            'relations': relations, 'diagnostics': compiled['errors'],
            'scope': 'Exact CAD seating and declared mechanical attachment to initial fixtures; excludes tipping, friction, material strength and fixture design.'}

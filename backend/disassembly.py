"""Complete-product removal search; only complete solutions become assembly plans."""
from __future__ import annotations
import time
from . import planning as p
from .planning_settings import entry_settings, DEFAULT_ANALYSIS_SECONDS, DEFAULT_CANDIDATE_SECONDS, DEFAULT_NATIVE_TIMEOUT_SECONDS
from .process_rules import process_eligibility, process_summary
from .support import prepare_support, support_removal, support_summary


def extraction_distance(model, unit, direction, rules):
    entry = rules.get('entry_conditions', {}).get(unit['id'], {})
    axis = p.AXES[direction]; i = next(i for i,v in enumerate(axis) if v)
    world = p._bbox_union(u['bbox'] for u in model['units'])
    margin = float(rules.get('extraction_margin_mm', 5))
    required = max(0., unit['bbox']['max'][i]-world['min'][i] if axis[i]>0 else world['max'][i]-unit['bbox']['min'][i]) + margin
    value = entry.get('distance_mm', rules.get('distance_mm'))
    return float(required if value is None else value), required, 'user' if value is not None else 'axis_envelope_plus_margin'


def reverse_path(assembly_path):
    return {**assembly_path, 'start_translation_mm': list(assembly_path['end_translation_mm']),
            'end_translation_mm': list(assembly_path['start_translation_mm'])}


def _rank(state):
    return state['preference_misses'], state['cost'], tuple((s['unit_id'],s['direction']) for s in reversed(state['steps']))


def generate_reverse_plan(model, rules, cache_dir=None, progress=None, cancel=None):
    started=time.monotonic()
    units, initial, targets, pred, errors, process=p._normalise(model,rules)
    deadline=started+(float(rules.get('max_seconds',DEFAULT_ANALYSIS_SECONDS)) if not errors else 0)
    attempts=[]; expanded=0; cache_hits=0; termination='no_verified_continuation'
    audit={'status':'UNKNOWN','pairs':[],'reason':'RULE_ERRORS'}; support=None
    base={'remaining':frozenset(targets),'steps':[], 'cost':0.,'preference_misses':0,'last_direction':None}
    best=base; complete=[]; width=int(rules.get('beam_width',4)) if not errors else 4
    geometry_reviewed=p._geometry_review_resolved(model,rules,units,initial,targets) if not errors else False
    if not errors:
        action_cache={};route_passes={};route_blockers={}
        with p.GeometrySession(model,timeout=float(rules.get('native_timeout_seconds',DEFAULT_NATIVE_TIMEOUT_SECONDS)),cancel=cancel,
                contact_policies=rules.get('contact_policies'),collision_policy=rules.get('collision_policy')) as engine:
            audit=p._audit(model,units,initial,targets,engine,deadline,progress)
            # An invalid analysis representation already prevents any valid
            # route. Do not spend the whole budget probing its fixing faces.
            support=(p._prepare_support(model,rules,initial,targets,engine,deadline,process,pred,progress)
                if audit['status']=='PASS' else prepare_support(model,rules,initial,targets,engine,deadline,process=process))
            successors={u:set() for u in units}
            for after,before_ids in pred.items():
                for before in before_ids:successors[before].add(after)
            if support['errors']:
                errors.extend(support['errors']);audit={**audit,'status':'UNKNOWN','reason':'SUPPORT_RULE_ERRORS'}
            if audit['status']!='PASS': termination='final_placement_collision' if audit['status']=='COLLISION' else 'geometry_unresolved'
            elif not targets: complete=[base];termination='completed'
            else:
                frontier=[base]
                while frontier and time.monotonic()<deadline and not complete:
                    children=[]
                    for state in frontier:
                        if cancel and cancel(): raise InterruptedError('PLANNING_CANCELLED')
                        occupied=set(state['remaining'])|initial; expanded+=1
                        # Exterior proximity is a hint only. Every remaining target is eligible for inspection.
                        def priority(uid):
                            box=units[uid]['bbox']; world=model['bbox']
                            surface=min(min(abs(box['min'][i]-world['min'][i]),abs(world['max'][i]-box['max'][i])) for i in range(3))
                            return surface, uid
                        eligible=sorted((u for u in state['remaining'] if not successors[u]&occupied),key=priority)
                        feasible_units=0
                        for uid in eligible:
                            after=occupied-{uid}
                            permitted,reasons=process_eligibility(targets-after,after,process)
                            if uid not in permitted:
                                attempts.append({'unit_id':uid,'occupied_unit_ids':sorted(occupied),'status':'UNKNOWN',
                                    'reason':reasons.get(uid,{}).get('code','PROCESS_FIXING_REQUIRED'),'direction':None,'pairs':[]})
                                continue
                            if progress:progress({'stage':'support_check','current':len(best['steps']),'total':len(targets),'message':'남은 부품의 지지·고정 확인 중 · '+units[uid]['name']})
                            seating=support_removal(uid,occupied,support)
                            if seating['status']!='PASS':
                                attempts.append({'unit_id':uid,'occupied_unit_ids':sorted(occupied),'status':'UNKNOWN' if seating['status']=='UNKNOWN' else 'UNSUPPORTED',
                                    'reason':seating['code'],'direction':None,'pairs':[], 'support_diagnostic':seating})
                                continue
                            allowed, preferred, source=entry_settings(rules,uid)
                            # Preferences order inspection, but do not stop after the first passing direction.
                            allowed.sort(key=lambda d:(d!=preferred,d))
                            passed=[]
                            for direction in allowed:
                                if time.monotonic()>=deadline:break
                                if cancel and cancel():raise InterruptedError('PLANNING_CANCELLED')
                                length,required,distance_source=extraction_distance(model,units[uid],direction,rules)
                                key=(uid,frozenset(after),direction,length)
                                route=(uid,direction,length);basis=None
                                tick=time.monotonic();cached=key in action_cache
                                if cached:verdict=action_cache[key];basis='same_obstacle_set'
                                else:
                                    # In a fixed CAD/policy session, removing obstacles cannot create a
                                    # collision, and a proven colliding pair remains blocked while that
                                    # obstacle remains. UNKNOWN never supplies either certificate.
                                    reusable=next((v for obstacles,v in route_passes.get(route,[]) if after<=obstacles),None)
                                    blocker=next((v for obstacle,v in route_blockers.get(route,[]) if obstacle in after),None)
                                    if reusable is not None:verdict=reusable;cached=True;basis='subset_of_verified_obstacles'
                                    elif blocker is not None:verdict=blocker;cached=True;basis='still_occupied_proven_blocker'
                                    else:
                                        verdict=p._action(units[uid],after,units,engine,direction,length,min(deadline,tick+float(rules.get('candidate_seconds',DEFAULT_CANDIDATE_SECONDS))))
                                        if verdict['status']=='PASS':route_passes.setdefault(route,[]).append((frozenset(after),verdict))
                                        elif verdict['status']=='COLLISION':
                                            for pair in verdict.get('pairs',[]):
                                                if pair.get('status')=='COLLISION' and pair.get('obstacle_unit_id') in after:
                                                    route_blockers.setdefault(route,[]).append((pair['obstacle_unit_id'],verdict))
                                    if verdict['status'] in ('PASS','COLLISION'):action_cache[key]=verdict
                                if cached:cache_hits+=1
                                attempts.append({'unit_id':uid,'unit_name':units[uid]['name'],'occupied_unit_ids':sorted(occupied),
                                    'obstacle_unit_ids':sorted(after),'direction':direction,'direction_source':source,'distance_mm':length,
                                    'required_extraction_distance_mm':required,'distance_source':distance_source,
                                    'status':verdict['status'],'reason':verdict.get('reason'),'pairs':verdict.get('pairs',[]),
                                    'cache_hit':cached,'cache_basis':basis,'elapsed_seconds':round(time.monotonic()-tick,4)})
                                if verdict['status']=='PASS':
                                    step=p._step(units[uid],len(state['steps'])+1,direction,length,verdict)
                                    step.update(direction_source=source,preferred_direction=preferred,removal_path=reverse_path(step['path']))
                                    step['support_evidence']=seating
                                    passed.append(step)
                                if progress:progress({'stage':'disassembly_search','current':len(best['steps']),'total':len(targets),'attempts':len(attempts),'message':'분해 경로 탐색 중 · '+units[uid]['name']})
                            passed.sort(key=lambda s:(bool(preferred and s['direction']!=preferred),s['distance_mm']+(float(rules.get('direction_change_cost',10)) if state['last_direction'] and s['direction']!=state['last_direction'] else 0),s['direction']))
                            for step in passed[:int(rules.get('direction_candidate_count',2))]:
                                changed=bool(state['last_direction'] and step['direction']!=state['last_direction'])
                                child={'remaining':state['remaining']-{uid},'steps':state['steps']+[step],
                                    'preference_misses':state['preference_misses']+int(bool(preferred and step['direction']!=preferred)),
                                    'cost':state['cost']+step['distance_mm']+changed*float(rules.get('direction_change_cost',10)), 'last_direction':step['direction']}
                                children.append(child)
                                if len(child['steps'])>len(best['steps']) or (len(child['steps'])==len(best['steps']) and _rank(child)<_rank(best)):best=child
                                if not child['remaining']:complete.append(child)
                            if passed:feasible_units+=1
                            # Bounded candidate expansion; remaining units are retried at the next frontier.
                            if feasible_units>=max(2,width):break
                        if time.monotonic()>=deadline:break
                    unique={}
                    for child in sorted(children,key=_rank):unique.setdefault((child['remaining'],child['last_direction']),child)
                    frontier=list(unique.values())[:width]
                if complete:termination='completed'
                elif time.monotonic()>=deadline:termination='budget_reached'
                elif any(a['status']=='UNKNOWN' for a in attempts):termination='geometry_unresolved'
    else:termination='input_error'
    selected=min(complete,key=_rank) if complete else best
    removal=selected['steps']
    latest={}
    for row in attempts:
        if row['unit_id'] in selected['remaining'] and set(row.get('occupied_unit_ids',[]))==set(selected['remaining'])|initial:
            latest[(row['unit_id'],row.get('direction'))]=row
    blocked_candidates=[{k:v for k,v in row.items() if k not in ('occupied_unit_ids','obstacle_unit_ids')} for row in latest.values() if row['status']!='PASS']
    steps=[{**step,'index':i+1} for i,step in enumerate(reversed(removal))] if complete else []
    diagnostics=errors+list(process['reviews'])
    if audit['status']!='PASS' and not errors:diagnostics.append({'code':'FINAL_PLACEMENT_REQUIRES_REVIEW','status':audit['status']})
    if not geometry_reviewed and not errors:diagnostics.append({'code':'MODEL_MAPPING_REVIEW_REQUIRED' if model.get('quality',{}).get('matching_ready') is False else 'GEOMETRY_EXACT_METHOD_REVIEW_REQUIRED'})
    if not complete:diagnostics.append({'code':'DISASSEMBLY_INCOMPLETE','termination':termination,'removed':len(removal),'total':len(targets)})
    status='PASS' if complete and geometry_reviewed else ('BLOCKED' if errors or audit['status']=='COLLISION' else 'UNKNOWN')
    return {'id':'plan_'+p._json_hash([model['fingerprint'],rules,steps])[:16], 'status':status,
        'kind':'assembly_candidate' if complete else 'incomplete_disassembly','generation_method':'disassembly_reverse',
        'replace_plan':bool(complete),'steps':steps,'initial_unit_ids':sorted(initial),'diagnostics':diagnostics,
        'placement_audit':audit,'process':process_summary(process),
        'support':support_summary(support) if support else None,
        'coverage':{'complete':bool(complete),'expected':len(targets),'actual':len(steps),'initial_count':len(initial),
            'complete_product_scope':initial|targets==set(units),'selected_unit_ids':sorted(initial|targets)},
        'disassembly':{'trace_kind':'removal_v1','removed_count':len(removal),'target_count':len(targets),
            'remaining_unit_ids':sorted(selected['remaining']),'removal_steps':removal,'persistent_obstacle_ids':sorted(initial),
            'blocked_candidates':blocked_candidates},
        'attempt_trace':attempts,'decision_trace':[],'learning_eligibility':{'eligible':False,'reason':'assembly_state_alternatives_not_recorded'},
        'algorithm_version':p.ALGORITHM_VERSION,'dependency_hash':p._json_hash([model['fingerprint'],rules]),
        'playback':p._playback(steps),'verification_mode':rules.get('collision_policy',{}).get('mode','nominal_interference'),
        'manufacturing_tolerance_certified':False,'manufacturing_clearance_certified':False,'representation_precision_limited':True,
        'search':{'attempts':len(attempts),'expanded_states':expanded,'cache_hits':cache_hits,'beam_width':width,
            'termination':termination,'optimality':'not_globally_proven','preference_misses':selected['preference_misses'],
            'cost':selected['cost'],'direction_change_cost':rules.get('direction_change_cost',10),
            'extraction_margin_mm':rules.get('extraction_margin_mm',5),'elapsed_seconds':round(time.monotonic()-started,3)}}

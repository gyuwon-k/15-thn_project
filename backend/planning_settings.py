"""Shared direction and search settings contract, independent of native CAD."""
import copy
import math

AXES = ("+X", "-X", "+Y", "-Y", "+Z", "-Z")
DEFAULT_ANALYSIS_SECONDS = 1800
DEFAULT_CANDIDATE_SECONDS = 120
DEFAULT_NATIVE_TIMEOUT_SECONDS = 120
TIMEOUT_POLICY_VERSION = 2


def analysis_job_timeout(rules, kind):
    """Allow both analysis stages and result publication before the supervisor stops a job."""
    generation = float(rules.get('max_seconds', DEFAULT_ANALYSIS_SECONDS)) if kind == 'generate' else 0
    validation = float(rules.get('validation_seconds', rules.get('max_seconds', DEFAULT_ANALYSIS_SECONDS)))
    return max(120, generation + validation + 120)


def settings_errors(model, rules):
    errors = []
    ids = {u['id'] for u in model.get('units', [])}
    directions = rules.get('directions', list(AXES))
    def fail(code, **detail): errors.append({'code': code, **detail})
    def number(key, default, low, high, integer=False):
        value = rules.get(key, default)
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high or (integer and int(value) != value):
            fail('INVALID_SEARCH_SETTING', field=key, minimum=low, maximum=high)
    if rules.get('generation_method', 'disassembly_reverse') not in ('disassembly_reverse', 'forward'):
        fail('INVALID_GENERATION_METHOD')
    policy = rules.get('collision_policy') or {}
    if isinstance(policy, dict) and policy.get('reference_mode', 'assembled_reference') not in ('assembled_reference', 'strict_geometry'):
        fail('INVALID_COLLISION_REFERENCE_MODE')
    fixtures=rules.get('support_fixtures',[])
    if not isinstance(fixtures,list) or any(not isinstance(item,dict) or item.get('unit_id') not in ids or not str(item.get('basis') or '').strip() for item in fixtures):
        fail('INVALID_SUPPORT_CONFIGURATION')
    if not isinstance(directions, list) or not directions or any(d not in AXES for d in directions):
        fail('INVALID_DIRECTION', field='directions'); directions = []
    for key, default, low, high, integer in (
        ('beam_width', 4, 1, 8, True), ('max_seconds', DEFAULT_ANALYSIS_SECONDS, 1, 7200, False),
        ('validation_seconds', DEFAULT_ANALYSIS_SECONDS, 1, 7200, False), ('candidate_seconds', DEFAULT_CANDIDATE_SECONDS, .1, 120, False),
        ('native_timeout_seconds', DEFAULT_NATIVE_TIMEOUT_SECONDS, .1, 120, False), ('direction_candidate_count', 2, 1, 2, True),
        ('extraction_margin_mm', 5, .0001, 10000, False), ('direction_change_cost', 10, 0, 1000000, False)):
        number(key, default, low, high, integer)
    if rules.get('distance_mm') is not None and (type(rules['distance_mm']) not in (int,float) or not math.isfinite(rules['distance_mm']) or rules['distance_mm'] <= 0):
        fail('INVALID_ENTRY_DISTANCE', field='distance_mm')
    entries = rules.get('entry_conditions', {})
    if not isinstance(entries, dict):
        fail('INVALID_ENTRY_CONDITIONS'); return errors
    for ident, entry in entries.items():
        if ident not in ids or not isinstance(entry, dict):
            fail('INVALID_ENTRY_CONDITIONS', unit_id=ident); continue
        allowed = entry.get('allowed_directions', [entry['direction']] if entry.get('direction') else directions)
        if not isinstance(allowed, list) or not allowed or any(d not in directions for d in allowed):
            fail('DIRECTION_CONSTRAINT_CONFLICT', unit_id=ident); continue
        if entry.get('direction') and entry['direction'] not in allowed:
            fail('DIRECTION_CONSTRAINT_CONFLICT', unit_id=ident)
        if entry.get('preferred_direction') and entry['preferred_direction'] not in allowed:
            fail('PREFERRED_DIRECTION_NOT_ALLOWED', unit_id=ident)
        if 'distance_mm' in entry and (type(entry['distance_mm']) not in (int,float) or not math.isfinite(entry['distance_mm']) or entry['distance_mm'] <= 0):
            fail('INVALID_ENTRY_DISTANCE', unit_id=ident)
    return errors


def entry_settings(rules, unit_id):
    entry = rules.get('entry_conditions', {}).get(unit_id, {})
    allowed = entry.get('allowed_directions', [entry['direction']] if entry.get('direction') else rules.get('directions', list(AXES)))
    source = 'user_allowed_list' if 'allowed_directions' in entry else ('legacy_single_direction' if entry.get('direction') else 'project_default')
    return list(dict.fromkeys(allowed)), entry.get('preferred_direction'), source


def compatible_rules(rules):
    """Upgrade obsolete defaults on a copy; never mutate an archived snapshot."""
    result = copy.deepcopy(rules)
    notes = []
    policy = result.setdefault('collision_policy', {'mode':'nominal_interference'})
    if isinstance(policy, dict):
        policy.setdefault('reference_mode', 'assembled_reference')
    width = result.get('beam_width', 4)
    if type(width) in (int,float) and math.isfinite(width) and 8 < width <= 32:
        result['beam_width'] = 8
        notes.append({'code':'LEGACY_BEAM_WIDTH_CONVERTED', 'previous':width, 'current':8,
                      'message':'기존 탐색 폭을 새 허용 범위에 맞춰 8로 변환했습니다. 재생성·재검증이 필요합니다.'})
    if result.get('timeout_policy_version') != TIMEOUT_POLICY_VERSION:
        changed = {}
        for key, previous_default, current_default in (
                ('max_seconds', 300, DEFAULT_ANALYSIS_SECONDS),
                ('validation_seconds', 300, DEFAULT_ANALYSIS_SECONDS),
                ('candidate_seconds', 4, DEFAULT_CANDIDATE_SECONDS),
                ('native_timeout_seconds', 20, DEFAULT_NATIVE_TIMEOUT_SECONDS)):
            previous = result.get(key)
            if key not in result or (type(previous) in (int, float) and previous == previous_default):
                result[key] = current_default
                changed[key] = {'previous': previous, 'current': current_default}
        result['timeout_policy_version'] = TIMEOUT_POLICY_VERSION
        if changed:
            notes.append({'code':'LEGACY_ANALYSIS_TIMEOUT_EXTENDED', 'changes':changed,
                          'message':'기존 기본 제한 시간을 확장했습니다. 순서 생성·전체 재검증은 각각 30분, 후보·형상 연산은 각각 120초를 기본으로 사용합니다. 직접 설정한 다른 시간은 유지됩니다.'})
    return result, notes

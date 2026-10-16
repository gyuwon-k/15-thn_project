"""Reassess failed analysis representations without repeating STEP registration.

Only copied occurrence records and derived BREP files can change. Display CAD,
placements, assembly units, matching decisions and process rules are retained.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path
from .domain import DomainError


_REPAIR_DIAGNOSTICS = {'REPRESENTATION_REPAIR_ACCEPTED', 'REPRESENTATION_REPAIR_BLOCKED',
                       'EXACT_REPRESENTATION_REVIEW_REQUIRED'}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _source_changed():
    return DomainError('ANALYSIS_SOURCE_HASH_MISMATCH',
        '보관된 원본 형상이 변경되어 기존 배치·매칭을 사용할 수 없습니다. STEP을 다시 등록해 주세요.')


def _recorded_source_hashes(row):
    return [value for value in (row.get('source_brep_sha256'),
            (row.get('repair') or {}).get('source_sha256')) if value]


def _checked_source_sha(row):
    actual = _file_sha(row.get('source_brep_path') or row['brep_path'])
    if any(recorded != actual for recorded in _recorded_source_hashes(row)):
        raise _source_changed()
    return actual


def _failed(row):
    return row.get('collision_ready') is False or row.get('exact_method_valid') is False


def _transient(row):
    return (row.get('repair') or {}).get('reason') == 'NATIVE_REPAIR_TIMEOUT_OR_FAILURE'


def _refresh_quality(model):
    occurrences = {row['id']: row for row in model.get('occurrences', [])}
    for unit in model.get('units', []):
        unit['collision_ready'] = bool(unit.get('occurrence_ids')) and all(
            occurrences.get(ident, {}).get('collision_ready', False) for ident in unit['occurrence_ids'])
    quality = model.setdefault('quality', {})
    bad = [ident for ident, row in occurrences.items() if not row.get('collision_ready')]
    exact = [ident for ident, row in occurrences.items() if row.get('exact_method_valid') is not True]
    quality.update(collision_ready=not bad, analysis_ready=not bad and not exact,
                   invalid_occurrence_ids=bad, exact_method_disagreement_ids=exact,
                   automatically_repaired_occurrence_ids=[ident for ident, row in occurrences.items()
                       if (row.get('repair') or {}).get('status') == 'accepted'])
    by_unit = {unit['id']: unit for unit in model.get('units', [])}
    for result in quality.get('unit_results', []):
        if result.get('unit_id') in by_unit:
            result['collision_ready'] = by_unit[result['unit_id']]['collision_ready']
    quality['status'] = 'PASS' if (quality.get('inventory_complete') and quality.get('display_ready')
        and quality.get('matching_ready') and not bad and not exact
        and not any(row.get('exact_method_unverified_ids') for row in quality.get('part_geometry', []))) else 'LIMITED'


def refresh_analysis_model(model, cache_dir, *, deadline, native_timeout_seconds=120,
                           progress=None, cancel=None, repair=None, policy_version=None, clock=None):
    """Return (new model or None, report), without mutating the input on any exit.

    Completed assessments are reused for the same policy. A native timeout can
    be retried, but identical evidence derives the same fingerprint rather than
    invalidating a project repeatedly. ``deadline`` belongs to the caller's
    existing generation/validation budget, not an additional repair allowance.
    """
    clock = clock or time.monotonic
    started = clock()
    report = {'attempted_occurrence_ids': [], 'pending_occurrence_ids': [], 'elapsed_seconds': 0.0,
              'changed': False, 'budget_exhausted': False}
    failed = [row for row in model.get('occurrences', []) if _failed(row)]
    if not failed:
        return None, report
    # Check before the policy comparison, including upgrades from older policy
    # versions. A changed source invalidates the retained scene/bounds/mapping;
    # it cannot be adopted as an ordinary analysis-representation repair.
    source_hashes = {row['id']: _checked_source_sha(row) for row in failed}
    if policy_version is None or repair is None:
        from .geometry import REPAIR_VERSION, repair_occurrences
        policy_version = policy_version or REPAIR_VERSION
        repair = repair or repair_occurrences
    report['policy_version'] = policy_version
    def current(row):
        assessed = row.get('analysis_policy_version')
        if not assessed and model.get('analysis_policy_version') == policy_version:
            assessed = (row.get('repair') or {}).get('version')
        if assessed != policy_version or _transient(row):
            return False
        # Legacy rows without a source hash need one initial reassessment.
        if not _recorded_source_hashes(row):
            return False
        analysis_sha = row.get('analysis_brep_sha256') or (row.get('repair') or {}).get('analysis_brep_sha256')
        try:
            return not analysis_sha or _file_sha(row['brep_path']) == analysis_sha
        except OSError:
            # A missing derived artifact can be recreated from a verified source.
            return False
    selected = [row['id'] for row in failed if not current(row)]
    if not selected:
        return None, report
    updated = copy.deepcopy(model)
    rows = {row['id']: row for row in updated['occurrences']}
    for index, ident in enumerate(selected):
        if cancel and cancel():
            raise InterruptedError('ANALYSIS_REFRESH_CANCELLED')
        remaining = deadline-clock()
        if remaining <= 0:
            report['pending_occurrence_ids'] = selected[index:]
            report['budget_exhausted'] = True
            break
        row = rows[ident]
        # Retry from the immutable source, never accumulate geometry changes by
        # recalibrating a previously derived candidate as a new source.
        source = row.get('source_brep_path') or row['brep_path']
        source_sha = _checked_source_sha(row)
        if source_sha != source_hashes[ident]:
            raise _source_changed()
        row['brep_path'] = source
        assembly = {'instances': [row], 'diagnostics': []}
        if progress:
            progress({'stage': 'analysis_refresh', 'current': index+1, 'total': len(selected),
                      'message': '기존 형상 분석 기준 갱신 중 · '+str(row.get('name') or ident)})
        repair(assembly, Path(cache_dir)/'analysis-refresh', progress=None, cancel=cancel,
               timeout_seconds=min(120., float(native_timeout_seconds), remaining))
        if cancel and cancel():
            raise InterruptedError('ANALYSIS_REFRESH_CANCELLED')
        repaired = assembly['instances'][0]
        # An ordinary OCCT pass alone must never promote an exact-check failure.
        if repaired.get('exact_method_valid') is not True:
            repaired['collision_ready'] = False
        repaired['analysis_attempt_policy_version'] = policy_version
        if not _transient(repaired):
            repaired['analysis_policy_version'] = policy_version
        else:
            repaired.pop('analysis_policy_version', None)
        if _file_sha(source) != source_sha:
            raise _source_changed()
        repaired['source_brep_sha256'] = source_sha
        repaired['analysis_brep_sha256'] = _file_sha(repaired['brep_path'])
        updated['diagnostics'] = [item for item in updated.get('diagnostics', [])
            if not (item.get('instance_id') == ident and item.get('code') in _REPAIR_DIAGNOSTICS)] + assembly['diagnostics']
        report['attempted_occurrence_ids'].append(ident)
    report['elapsed_seconds'] = max(0., clock()-started)
    report['budget_exhausted'] |= clock() >= deadline
    if not report['attempted_occurrence_ids']:
        return None, report
    _refresh_quality(updated)
    updated['analysis_policy_version'] = policy_version
    base = model.get('analysis_base_fingerprint') or model['fingerprint']
    updated['analysis_base_fingerprint'] = base
    evidence = [{key: row.get(key) for key in ('id', 'analysis_attempt_policy_version',
                    'source_brep_sha256', 'analysis_brep_sha256', 'collision_ready', 'exact_method_valid', 'analysis_scope',
                    'representation_uncertainty_mm', 'collision_clearance_guard_required')}
                | {'result': {key: (row.get('repair') or {}).get(key)
                     for key in ('status', 'method', 'reason', 'standard_valid', 'exact_valid')}}
                for row in sorted(updated['occurrences'], key=lambda item: item['id'])
                if row.get('analysis_attempt_policy_version')]
    updated['fingerprint'] = _digest({'base_fingerprint': base, 'analysis_policy_version': policy_version,
                                      'analysis_evidence': evidence})
    report['changed'] = updated['fingerprint'] != model['fingerprint']
    updated['analysis_refresh'] = copy.deepcopy(report)
    return (updated if report['changed'] else None), report


def incomplete_refresh_plan(model, report):
    """No guessed motion or inherited PASS when reassessment uses the budget."""
    return {'status': 'UNKNOWN', 'kind': 'incomplete_disassembly', 'replace_plan': False,
            'generation_method': 'disassembly_reverse', 'steps': [],
            'disassembly': {'removed_count': 0, 'target_count': len(model.get('units', [])),
                            'remaining_unit_ids': [unit['id'] for unit in model.get('units', [])]},
            'search': {'termination': 'budget_reached', 'elapsed_seconds': 0, 'attempts': 0},
            'diagnostics': [{'code': 'ANALYSIS_REFRESH_BUDGET_REACHED',
                             'message': '형상 재평가에 분석 시간이 사용되어 순서 탐색을 완료하지 못했습니다.'}],
            'analysis_refresh': copy.deepcopy(report)}

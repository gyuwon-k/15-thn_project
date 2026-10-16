"""Exercise a saved STEP analysis input in an isolated worker directory.

This reads the saved request/CAD, never publishes to the live project database.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.jobs import atomic_json, path_values
from backend.planning_settings import compatible_rules, analysis_job_timeout
from backend.release import load_manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('request', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--backend', type=Path)
    parser.add_argument('--seconds', type=float)
    parser.add_argument('--kind', choices=('generate', 'validate'), default='generate')
    args = parser.parse_args()
    original_bytes = args.request.read_bytes()
    saved = json.loads(original_bytes)
    project = path_values(saved['payload'], Path(saved['data_dir']).resolve())['project']
    before = copy.deepcopy(project['model'])
    project['rules'] = compatible_rules(project['rules'])[0]
    if args.seconds is not None:
        project['rules'].update(max_seconds=args.seconds, validation_seconds=args.seconds)
    folder = args.out.resolve()
    folder.mkdir(parents=True, exist_ok=False)
    release_dir = args.backend.resolve().parent.parent if args.backend else ROOT
    manifest = load_manifest(release_dir)
    request = {'kind': args.kind, 'release': manifest, 'data_dir': str(folder), 'folder': str(folder),
               'payload': {'project': project, 'ranking_model': None, 'cache_dir': str(folder / 'analysis'),
                           'timeout_seconds': analysis_job_timeout(project['rules'], args.kind)}}
    atomic_json(folder / 'request.json', request)
    env = dict(os.environ, PILOT_BUILD_ID=manifest['build_id'], PILOT_DATA_DIR=str(folder), PYTHONUTF8='1')
    if args.backend:
        command = [str(args.backend.resolve()), '--worker', str(folder / 'request.json')]
        env.update(PILOT_RELEASE_ROOT=str(release_dir), PILOT_RESOURCE_DIR=str(args.backend.resolve().parent / '_internal'))
    else:
        command = [sys.executable, '-X', 'utf8', '-m', 'backend.worker', str(folder / 'request.json')]
        for key in ('PILOT_RELEASE_ROOT', 'PILOT_RESOURCE_DIR'):
            env.pop(key, None)
    tick = time.monotonic()
    with (folder / 'worker.log').open('wb') as log:
        completed = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
            timeout=request['payload']['timeout_seconds'] + 30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    envelope = json.loads((folder / 'result.json').read_text(encoding='utf-8'))
    if completed.returncode or 'error' in envelope:
        raise RuntimeError(envelope.get('error', 'WORKER_FAILED'))
    result = envelope['result']
    after = result.get('analysis_model') or before
    plan, validation = result.get('plan', {'status': 'NOT_REQUESTED'}), result['validation']
    source_keys = ('id', 'name', 'bbox', 'transform_world')
    preserved = [{key: row.get(key) for key in source_keys} for row in before['occurrences']] == [
                {key: row.get(key) for key in source_keys} for row in after['occurrences']]
    unit_keys = ('id', 'name', 'occurrence_ids', 'bbox', 'matching', 'role')
    units_preserved = [{key: row.get(key) for key in unit_keys} for row in before['units']] == [
                       {key: row.get(key) for key in unit_keys} for row in after['units']]
    report = {'build_id': manifest['build_id'], 'kind': args.kind, 'source_request_sha256': hashlib.sha256(original_bytes).hexdigest(),
        'source_request_unchanged': args.request.read_bytes() == original_bytes,
        'unit_count': len(before['units']), 'occurrence_count': len(before['occurrences']),
        'placements_and_unit_definitions_preserved': preserved and units_preserved,
        'before_invalid': before.get('quality', {}).get('invalid_occurrence_ids', []),
        'after_invalid': after.get('quality', {}).get('invalid_occurrence_ids', []),
        'after_exact_disagreements': after.get('quality', {}).get('exact_method_disagreement_ids', []),
        'analysis_ready': after.get('quality', {}).get('analysis_ready'),
        'analysis_policy_version': after.get('analysis_policy_version'),
        'analysis_refresh': result.get('analysis_refresh'),
        'matching_ready': after.get('quality', {}).get('matching_ready'),
        'generation_status': plan['status'], 'validation_status': validation['status'],
        'steps': len(plan.get('steps', [])), 'search': plan.get('search'),
        'diagnostics': plan.get('diagnostics'), 'playback': validation.get('playback'),
        'wall_seconds': round(time.monotonic() - tick, 3)}
    assert report['source_request_unchanged'] and report['placements_and_unit_definitions_preserved']
    atomic_json(folder / 'report.json', report)
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()

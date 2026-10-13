"""Real HVJB, via final application's public HTTP APIs, no Assembly Flow input."""
from pathlib import Path
import argparse, json, time, tempfile
import httpx

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'evidence/hvjb-final'
SOURCE = ROOT.parent / '프로젝트_제공_파일/3D DATA/STP/KD1_HVJB_91958-KD000.stp'
EVIDENCE.mkdir(parents=True, exist_ok=True)

def persist(name, data):
    (EVIDENCE / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8781')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args()
    client = httpx.Client(base_url=args.url, timeout=180)
    def call(method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()
    def wait(job_id, timeout):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            job = call('GET', '/api/asap/jobs/' + job_id)
            marker = (job['status'], job.get('stage'), int(job.get('elapsed_s', 0) / 30))
            if marker != last:
                print(json.dumps({key: job.get(key) for key in ('id', 'type', 'status', 'stage', 'elapsed_s', 'error')}, ensure_ascii=False), flush=True)
                last = marker
            if job['status'] in ('success', 'failed', 'canceled', 'interrupted'):
                persist(job_id + '-job.json', job)
                return job
            time.sleep(1)
        raise TimeoutError(job_id)
    state_path = EVIDENCE / 'state.json'
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding='utf-8'))
    else:
        config = call('GET', '/api/asap/config')
        persist('config.json', config)
        project = call('POST', '/api/projects', json={'name': '실제 HVJB 전체 순서 검증', 'input_mode': 'assembly_only'})
        with SOURCE.open('rb') as stream:
            session = call('POST', '/api/asap/sessions', data={'input_mode': 'assembly_only'}, files={'file': (SOURCE.name, stream)})
        state = {'project_id': project['project']['id'], 'session_id': session['id'], 'parse_job': session['job_id'], 'source_path': str(SOURCE), 'api_url': args.url}
        persist('state.json', state)
        call('POST', '/api/projects/' + state['project_id'] + '/asap-link', json={'session_id': state['session_id']})
    sid = state['session_id']
    base = '/api/asap/sessions/' + sid
    parsed = wait(state['parse_job'], 1800)
    if parsed['status'] != 'success':
        raise RuntimeError(parsed)
    session = call('GET', base)
    revision = session['unit_state']['revision']
    if 'convert_job' not in state:
        call('POST', base + '/units/confirm', json={'expected_revision': revision})
        conversion = call('POST', base + '/convert', json={'penetration_max_faces_per_unit': 0})
        state.update(convert_job=conversion['job_id'], unit_revision=revision)
        persist('state.json', state)
    converted = wait(state['convert_job'], 1800)
    if converted['status'] != 'success':
        raise RuntimeError(converted)
    persist('session.json', call('GET', base))
    if args.prepare_only:
        print('HVJB registration and conversion completed', flush=True)
        return
    config = call('GET', '/api/asap/config')
    if not config['engine']['workspace_root'].isascii():
        config['engine']['workspace_root'] = str(Path(tempfile.gettempdir()) / 'PILOT-HVJB-Final-20261013/physics')
        config = call('PUT', '/api/asap/config', json={'engine': config['engine']})
    persist('config.json', config)
    run = call('POST', base + '/runs', json={'strategy': 'learn', 'budget': 400, 'timeout_s': args.timeout, 'max_gripper': 3, 'max_pose': 1,
        'expected_unit_revision': revision, 'physics_profile': {'contact_eps_mm': .25, 'sdf_dx_mm': 2., 'sdf_res': 20, 'path_col_th_mm': .05, 'minimum_separation_mm': 5.}})
    state['latest_run'] = run
    persist('state.json', state)
    wait(run['job_id'], args.timeout + 30)
    result = call('GET', '/api/asap/runs/' + run['run_id'])
    persist('baseline-result.json', result)
    print(json.dumps({k: result.get(k) for k in ('sequence_status', 'path_status', 'preprocessing_status', 'reason_code', 'stats')}, ensure_ascii=False), flush=True)

if __name__ == '__main__':
    main()

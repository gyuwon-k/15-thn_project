"""Isolated real HTTP/Chromium tests for the packaged design and reverse planner."""
import argparse,hashlib,json,os,socket,subprocess,sys,time,uuid
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.jobs import JobService, atomic_json

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--backend',type=Path);args=parser.parse_args()
    folder=ROOT/'work/workflow-tests'/('application-'+uuid.uuid4().hex[:8]);folder.mkdir(parents=True)
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    token=uuid.uuid4().hex+uuid.uuid4().hex;url=f'http://127.0.0.1:{port}'
    environment=dict(os.environ,PILOT_PORT=str(port),PILOT_SESSION_TOKEN=token,PILOT_DATA_DIR=str(folder/'data'),PYTHONUTF8='1')
    if args.backend:
        exe=args.backend.resolve();release=exe.parent.parent;resources=exe.parent/'_internal';command=[str(exe),'--server']
        environment.update(PILOT_RELEASE_ROOT=str(release),PILOT_RESOURCE_DIR=str(resources))
    else:
        resources=ROOT/'packaging/resources';environment['PILOT_RESOURCE_DIR']=str(resources);command=[sys.executable,str(ROOT/'launcher.py'),'--server']
    environment['PLAYWRIGHT_BROWSERS_PATH']=str(resources/'browsers')
    log=(folder/'server.log').open('wb');process=subprocess.Popen(command,cwd=ROOT,env=environment,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    client=httpx.Client(base_url=url,headers={'X-Pilot-Token':token},timeout=180)
    report={'folder':str(folder),'checks':[],'javascript_errors':[]}
    def get(p):return client.get('/api'+p).raise_for_status().json()
    def post(p,body):return client.post('/api'+p,json=body).raise_for_status().json()
    def wait(job):
        deadline=time.monotonic()+300
        while time.monotonic()<deadline:
            value=get('/jobs/'+job['id'])
            if value['status']=='succeeded':return value
            if value['status'] in ('failed','canceled','interrupted'):raise AssertionError(value)
            time.sleep(.3)
        raise TimeoutError(job)
    try:
        deadline=time.monotonic()+120
        while True:
            try:report['health']=get('/health');break
            except Exception:
                if process.poll() is not None or time.monotonic()>deadline:raise
                time.sleep(.3)
        subprocess.run([sys.executable,'-X','utf8',str(ROOT/'scripts/verify_application_workflow.py'),'--url',url,'--token',token,'--report',str(folder/'workflow.json')],cwd=ROOT,env=environment,check=True,timeout=800)
        workflow=json.loads((folder/'workflow.json').read_text(encoding='utf-8'));ident=workflow['project_id'];prefix='/projects/'+ident
        project=get(prefix);assert project['plan']['generation_method']=='disassembly_reverse';assert project['validation']['status']=='PASS'
        assert workflow['checks']['confirmation_without_torque_caution_or_review_check']
        assert workflow['checks']['new_project_30_minute_generation_and_validation_defaults']
        assert all(s['direction']==s['proposed_entry']['direction'] for s in project['plan']['steps'])
        blank_steps=json.loads(json.dumps(project['plan']['steps']))
        for step in blank_steps:step.update(reviewed=False,torque=None,torque_status='unset',caution='',caution_reviewed=False)
        project=client.patch('/api'+prefix,json={'expected_revision':project['revision'],'plan':{'steps':blank_steps}}).raise_for_status().json()
        assert project['validation']['status']=='PASS'
        report['checks'].append('Native STEP → reverse generation → full revalidation → confirmation → MP4 → manual edit/revalidation')
        from backend.release import prepare_browser_runtime
        os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(prepare_browser_runtime(resources/'browsers',folder/'data',report['health']['build_id']))
        from playwright.sync_api import sync_playwright
        with sync_playwright() as auto:
            browser=auto.chromium.launch(headless=True)
            context=browser.new_context(viewport={'width':1440,'height':1000},accept_downloads=True)
            page=context.new_page();page.on('pageerror',lambda e:report['javascript_errors'].append(str(e)))
            page.goto(url+'/?project='+ident+'&token='+token,wait_until='networkidle');page.locator('canvas').wait_for()
            page.locator('.sequence-card').first.wait_for();assert page.locator('.sequence-card').count()==3
            assert page.locator('.theme-vectary').count()==1
            assert page.locator('.sidebar').evaluate('(e)=>getComputedStyle(e).width')=='54px'
            page.get_by_role('button',name='순서 확정',exact=True).click()
            dialog=page.get_by_role('dialog',name='조립순서 확정')
            assert dialog.locator('input:required,textarea:required').count()==0
            assert dialog.get_by_role('textbox',name='기록자 (선택)',exact=True).input_value()==''
            page.screenshot(path=str(folder/'confirmation-optional.png'),full_page=True)
            with page.expect_response(lambda response: response.url.endswith(prefix+'/confirm') and response.request.method=='POST') as confirmation_response:
                dialog.get_by_role('button',name='현재 순서 확정',exact=True).click()
            assert confirmation_response.value.status==200
            assert get(prefix)['plan']['status']=='CONFIRMED'
            page.get_by_role('button',name='확정됨',exact=True).wait_for()
            assert get(prefix)['plan']['status']=='CONFIRMED'
            report['checks'].append('Actual UI confirms validated sequence with empty torque/caution, unchecked work review and no recorder')
            page.locator('.sequence-card').first.click();page.get_by_text('허용·선호 조립 방향',exact=True).wait_for()
            page.get_by_role('combobox',name='선호 조립 방향',exact=True).select_option('-Z')
            page.get_by_role('button',name='방향 조건 저장',exact=True).click()
            page.wait_for_function("() => document.querySelector('.saved-state')?.textContent?.includes('저장된')")
            refreshed=get(prefix);entry=refreshed['rules']['entry_conditions'];assert any(e.get('preferred_direction')=='-Z' for e in entry.values())
            assert refreshed['validation'] is None
            report['checks'].append('Unit allowed/preferred directions saved through actual UI and invalidated proof')
            for width in (1440,980,600):
                page.set_viewport_size({'width':width,'height':1000});page.wait_for_timeout(200)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),f'Horizontal page overflow at {width}'
                page.screenshot(path=str(folder/f'design2-{width}.png'),full_page=True)
            report['checks'].append('Design2 layout / 1440, 980, 600 px without page horizontal overflow')
            page.set_viewport_size({'width':1440,'height':1000})
            page.get_by_role('tab',name='조립 규칙',exact=True).click();page.get_by_text('조립순서 생성 설정',exact=True).wait_for()
            project=get(prefix)
            project=client.patch('/api'+prefix,json={'expected_revision':project['revision'],'rules':{'max_seconds':1800,'validation_seconds':1800}}).raise_for_status().json()
            page.reload(wait_until='networkidle')
            page.get_by_role('tab',name='조립 규칙',exact=True).click()
            assert page.locator('[name=max_minutes]').input_value()=='30'
            assert page.locator('[name=validation_minutes]').input_value()=='30'
            assert page.locator('[name=candidate_seconds]').input_value()=='120'
            assert page.locator('[name=native_timeout_seconds]').input_value()=='120'
            page.screenshot(path=str(folder/'analysis-timeouts-30-minutes.png'),full_page=True)
            report['checks'].append('Generation and full revalidation show 30 minutes each; candidate/native limits show 120 seconds')
            page.get_by_role('tab',name='영상 출력',exact=True).click();page.get_by_role('button',name='확정본 영상 생성',exact=True).wait_for()
            page.get_by_role('tab',name='모델 편집',exact=True).click()
            project=get(prefix)
            project=client.patch('/api'+prefix,json={'expected_revision':project['revision'],'rules':{'distance_mm':.01,'max_seconds':15,'validation_seconds':15}}).raise_for_status().json()
            previous_plan=project['plan'];previous_validation=project['validation']
            wait(post(prefix+'/generate',{'expected_revision':project['revision']}));project=get(prefix)
            assert project['plan']==previous_plan and project['validation']==previous_validation
            assert project['generation_report']['kind']=='incomplete_disassembly'
            artifact=project['generation_report']['attempt_trace_artifact'];data=client.get(artifact['url']).raise_for_status().content
            assert hashlib.sha256(data).hexdigest()==artifact['sha256']
            assert len(json.loads(data)['attempts'])==artifact['count']
            page.reload(wait_until='networkidle');page.locator('.generation-report').wait_for()
            with page.expect_download() as download_info:
                page.get_by_role('link',name='이번 탐색의 전체 기록 내려받기',exact=False).click()
            downloaded=folder/'ui-search-trace.json';download_info.value.save_as(str(downloaded))
            assert hashlib.sha256(downloaded.read_bytes()).hexdigest()==artifact['sha256']
            page.locator('.generation-report summary').click()
            directional_index=next(i for i,row in enumerate(project['generation_report']['disassembly']['blocked_candidates']) if row.get('direction'))
            page.locator('.diagnostic-focus').nth(directional_index).click()
            assert page.locator('.direction-caption').count()==1
            page.screenshot(path=str(folder/'incomplete-preserved.png'),full_page=True)
            assert client.post('/api'+prefix+'/confirm',json={'expected_revision':project['revision']}).status_code==422
            report['checks'].extend(['Incomplete removal preserves existing steps, work information and validation','Incomplete trace stored and downloaded with exact SHA/count','Failed candidate highlights moving/blocking units with a static entry arrow; no uncertified animation','Unvalidated plan cannot be confirmed'])
            assert not report['javascript_errors'],report['javascript_errors']
            browser.close()
        report['status']='PASS';atomic_json(folder/'report.json',report);print(json.dumps({'report':str(folder/'report.json'),'status':'PASS'},ensure_ascii=False),flush=True)
    except Exception as exc:
        report['status']='FAILED';report['error']=str(exc)
        raise
    finally:
        client.close()
        if process.poll() is None:JobService._terminate(process)
        process.wait(timeout=30)
        log.close();atomic_json(folder/'report.json',report)

if __name__=='__main__':main()

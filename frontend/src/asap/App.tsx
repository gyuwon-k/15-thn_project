import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertCircle, ArrowLeftRight, Box, Check, CheckCircle2, ChevronDown, ChevronLeft, ChevronRight, Download, FileUp, FolderOpen, GitCompareArrows, Layers3, LoaderCircle, Pause, Play, RotateCw, Search, Settings2, ShieldCheck, SkipBack, SkipForward, Square, Terminal, X } from 'lucide-react';
import AssemblyViewer from './AssemblyViewer';
import UnitEditor from './UnitEditor';
import TemplatePanel from './TemplatePanel';
import DiagnosticPanel from './DiagnosticPanel';
import { api, ApiError, post } from './api';
import { comparisonDifferences } from './comparison';
import { logLines } from './logs';
import { assemblyOrder, canStartPlayback, mapId, playbackBoundary, poseTransition, resultOf, stepsOf } from './playback';
import type { DisplayScene, Job, Json, Run, RunSettings, Session, Unit, ViewerMode } from './types';

const DEFAULT_SETTINGS: RunSettings = { strategy: 'learn', planner: 'dfs', seed: 0, budget: 400, timeout_s: 1800, max_gripper: 3, max_pose: 3, pose_reuse: 0, base_part: null };
const TERMINAL = new Set(['success', 'failed', 'canceled', 'interrupted']);
const STATUS: Record<string, string> = { unit_review_required: '조립 단위 확인 필요', units_confirmed: '조립 단위 확정', unit_editing: '조립 단위 편집 중', ready: '실행 준비 완료', parsed: 'STEP 확인 완료', uploaded: '등록됨', parsing: 'STEP 해석 중', converting: 'OBJ 변환 중', converted: 'OBJ 변환 완료', blocked: '품질 확인 필요', quality_review_required: '품질 확인 필요', template_parsing:'기준 부품 해석 중',templates_ready:'기준 부품 준비',matching:'부품 매칭 중',match_review_required:'매칭 후보 확인 필요', running: '실행 중', queued: '작업 대기', success: '완료', failed: '실패', canceled: '취소됨', interrupted: '중단됨', sequence_found: '순서 생성 완료', ready_to_view: '결과 확인 가능', path_extracting: '경로 추출 중', unresolved: '미해결', unavailable: '경로 없음' };
const label = (status: unknown) => STATUS[String(status)] ?? String(status ?? '미확인');
function short(value: unknown) { return String(value ?? '').slice(0, 10); }
function number(value: unknown, precision = 2) { return typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('ko-KR', { maximumFractionDigits: precision }) : '—'; }
function sourceName(session: Json | null) { return typeof session?.source === 'string' ? session.source : session?.source?.name ?? session?.source?.filename ?? session?.manifest?.source_name ?? session?.source_name ?? session?.id ?? ''; }
function seconds(value: unknown) { return typeof value === 'number' ? value >= 60 ? `${Math.floor(value / 60)}분 ${Math.round(value % 60)}초` : `${value.toFixed(1)}초` : '—'; }
function bboxText(unit: Unit) {
  const bbox = unit.bbox;
  if (!bbox) return '—';
  const size = !Array.isArray(bbox) ? bbox.size ?? (bbox.min && bbox.max ? bbox.max.map((v: number, i: number) => v - bbox.min[i]) : null) : bbox.length === 6 ? bbox.slice(3).map((v: number, i: number) => v - bbox[i]) : null;
  return Array.isArray(size) ? size.map((v: number) => number(v)).join(' × ') + ' mm' : '—';
}
type WorkspaceProps={projectId:string;inputRevision?:number;readOnly?:boolean;onImported:()=>void;};
async function projectRequest(projectId:string,path:string,body?:any){
 const response=await fetch(`/api/projects/${projectId}/${path}`,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
 const data=await response.json();if(!response.ok)throw new ApiError(data.detail||data.message||'프로젝트 연결 실패',data.code||data.reason_code);return data;
}
export default function App({projectId,inputRevision,readOnly=false,onImported}:WorkspaceProps) {
  const [showTemplates,setShowTemplates]=useState(false), [inputMode,setInputMode]=useState('assembly_only');
  const pendingRun=useRef<{settings:RunSettings;sessionId:string;revision:string}|null>(null);
  const [showUnitEditor, setShowUnitEditor] = useState(false);
  const [diagnosticIds,setDiagnosticIds]=useState<string[]>([]), [focusToken,setFocusToken]=useState(0);
  const [sessions, setSessions] = useState<Json[]>([]), [session, setSession] = useState<Session | null>(null);
  const [cad, setCad] = useState<DisplayScene | null>(null), [obj, setObj] = useState<DisplayScene | null>(null);
  const [runCad, setRunCad] = useState<DisplayScene | null>(null), [runObj, setRunObj] = useState<DisplayScene | null>(null);
  const [environment, setEnvironment] = useState<Json | null>(null), [config, setConfig] = useState<Json>({ engine: { transport: 'native', python: '', asap_root: '', weights_path: '', distribution: '' } });
  const [settings, setSettings] = useState<RunSettings>(DEFAULT_SETTINGS), [convertSettings, setConvertSettings] = useState({ deflection_mm: .1, angular_deflection_rad: .2, engine_extent: 10, adaptive_meshing: true, brep_repair_policy: 'verified_topology', multi_solid_policy: 'preserve', union_tolerance_mm: 1e-6 });
  const [job, setJob] = useState<Job | null>(null), [run, setRun] = useState<Run | null>(null), [compare, setCompare] = useState<Run | null>(null);
  const [selected, setSelected] = useState<string | null>(null), [query, setQuery] = useState(''), [leftTab, setLeftTab] = useState<'parts' | 'sequence'>('parts');
  const [mode, setMode] = useState<ViewerMode>('cad'), [isolate, setIsolate] = useState(false), [showFuture, setShowFuture] = useState(false), [showPath, setShowPath] = useState(true);
  const [stepIndex, setStepIndex] = useState(0), [progress, setProgress] = useState(0), [playing, setPlaying] = useState(false), [speed, setSpeed] = useState(1);
  const [showConfig, setShowConfig] = useState(false), [showCompare, setShowCompare] = useState(false), [showAdvanced, setShowAdvanced] = useState(false), [rightTab, setRightTab] = useState<'settings' | 'details' | 'logs'>('settings');
  const [error, setError] = useState<ApiError | null>(null), [busy, setBusy] = useState(false), [checking, setChecking] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null), sessionRef = useRef(session), activeJobRef = useRef(job);
  sessionRef.current = session; activeJobRef.current = job;
  const withAction = useCallback(async (fn: () => Promise<void>) => {
    setError(null); setBusy(true);
    try { await fn(); } catch (err) { setError(err instanceof ApiError ? err : new ApiError(String(err))); }
    finally { setBusy(false); }
  }, []);
  const refreshSessions = useCallback(async () => {
    const response = await api('/sessions'); setSessions(Array.isArray(response) ? response : response.sessions ?? []);
  }, []);
  const loadSession = useCallback(async (id: string, preserveRun = false) => {
    const data = await api<Session>(`/sessions/${id}`); setSession(data);setInputMode(data.input_mode??'assembly_only');
    const responses = await Promise.allSettled([api<DisplayScene>(`/sessions/${id}/scene?mode=cad`), api<DisplayScene>(`/sessions/${id}/scene?mode=obj`)]);
    setCad(responses[0].status === 'fulfilled' ? responses[0].value : null);
    setObj(responses[1].status === 'fulfilled' ? responses[1].value : null);
    const loadedJob = !preserveRun && data.job_id ? await api<Job>(`/jobs/${data.job_id}`).catch(() => null) : null;
    if (!preserveRun) { setJob(loadedJob && !TERMINAL.has(loadedJob.status) ? loadedJob : null); setRun(null); setCompare(null); setMode('cad'); setSelected(null); setStepIndex(0); setProgress(0); setPlaying(false); setIsolate(false); }
  }, [projectId,inputRevision]);
  const loadRun = useCallback(async (id: string, comparison = false) => {
    const data = await api<Run>(`/runs/${id}`);
    const result = resultOf(data);
    if ((!data.steps || data.steps.some(step => !Array.isArray(step.path))) && Array.isArray(result.step_ids)) {
      data.steps = await Promise.all(result.step_ids.map((_id: string, index: number) => api(`/runs/${id}/steps/${index}`)));
    }
    if (comparison) { setCompare(data); return; }
    // Reopening a run must show that run's job logs, never a previous session.
    const savedJob = data.job_id ? await api<Job>(`/jobs/${data.job_id}`).catch(() => null) : null;
    setJob(savedJob);
    const scenes = await Promise.allSettled([api<DisplayScene>(`/runs/${id}/scene?mode=cad`), api<DisplayScene>(`/runs/${id}/scene?mode=obj`)]);
    setRunCad(scenes[0].status === 'fulfilled' ? scenes[0].value : null);
    setRunObj(scenes[1].status === 'fulfilled' ? scenes[1].value : null);
    if (scenes[0].status !== 'fulfilled' && assemblyOrder(data).some(unitId => !sessionRef.current?.units.some(unit => unit.id === unitId))) {
      setError(new ApiError('이 저장 실행은 현재 입력과 다른 부품 단위를 사용합니다. 실행 당시의 장면 파일이 필요합니다.', 'RUN_SCENE_UNAVAILABLE', '원래 실행 입력을 복원하거나 실행 ZIP의 원본 형상을 확인하세요.'));
    }
    setRun(data); setMode('assembly'); setLeftTab('sequence'); setStepIndex(0); setProgress(0); setPlaying(false); setIsolate(false);
  }, []);
  const checkEnvironment = useCallback(async () => {
    setChecking(true);
    try { setEnvironment(await api('/environment')); } catch (err) { setError(err as ApiError); }
    finally { setChecking(false); }
  }, []);
  useEffect(() => {
    let alive = true;
    projectRequest(projectId,'asap-link').then(async link=>{if(alive&&link.session_id){await loadSession(link.session_id);if(link.run_id)await loadRun(link.run_id);}}).catch(e=>{if(alive)setError(e);});
    api('/sessions').then(value => { if (alive) setSessions(Array.isArray(value) ? value : value.sessions ?? []); }).catch(e => { if (alive) setError(e); });
    api('/config').then(value => { if (alive) setConfig(value); }).catch(e => { if (alive) setError(e); });
    api('/environment').then(value => { if (alive) setEnvironment(value); }).catch(e => { if (alive) setError(e); });
    return () => { alive = false; };
  }, []);
  useEffect(() => {
    if (!job?.id || TERMINAL.has(job.status)) return;
    let alive = true, timer = 0;
    const poll = async () => {
      try {
        const data = await api<Job>(`/jobs/${job.id}`);
        if (!alive) return;
        setJob(data);
        if (TERMINAL.has(data.status)) {
          if (sessionRef.current) await loadSession(sessionRef.current.id, true);
          await refreshSessions();
          const runId = data.run_id ?? data.result?.run_id ?? job.run_id ?? (data.type === 'run' ? data.result?.id : null);
          if (runId) await loadRun(runId);
          if (pendingRun.current && data.type==='convert') {
            const next=pendingRun.current;pendingRun.current=null;
            const ready=await api<Session>(`/sessions/${next.sessionId}`);
            if (!data.error && ready.conversion?.ready && ready.unit_state.revision===next.revision) {
              const started=await post(`/sessions/${next.sessionId}/runs`,{...next.settings,expected_unit_revision:next.revision});
              setRun(null);setMode('cad');setJob({id:started.job_id,status:'queued',stage:'자동 네이티브 전처리·ASAP',type:'run',run_id:started.run_id});return;
            }
          }
          if (data.error) {
            const d = typeof data.error === 'string' ? { message: data.error } : data.error;
            setError(new ApiError(d.message ?? '작업이 종료되었습니다.', d.reason_code, d.action));
          }
          return;
        }
      } catch (err) { if (alive) setError(err as ApiError); }
      if (alive) timer = window.setTimeout(poll, 1100);
    };
    timer = window.setTimeout(poll, 500);
    return () => { alive = false; clearTimeout(timer); };
  }, [job?.id, loadRun, loadSession, refreshSessions]);
  const active = !!job && !TERMINAL.has(job.status), locked = active || busy || readOnly;
  const units = session?.units ?? [], selectedUnit = units.find(unit => unit.id === selected);
  const filteredUnits = useMemo(() => units.filter(unit => `${unit.name} ${unit.id} ${unit.definition_id}`.toLowerCase().includes(query.toLowerCase())), [units, query]);
  const result = resultOf(run), order = assemblyOrder(run), planSteps = stepsOf(run);
  const savedSettings = run?.settings ?? result.settings ?? {};
  const playbackMode = mode === 'disassembly' ? 'disassembly' : 'assembly';
  const boundary = useMemo(() => playbackBoundary(run, stepIndex, progress, playbackMode), [run, stepIndex, progress, playbackMode]);
  const poseEvent = useMemo(() => poseTransition(run, stepIndex, playbackMode), [run, stepIndex, playbackMode]);
  const coordinateMap = run?.coordinate_map ?? result.coordinate_map ?? session?.coordinate_map;
  const motionMode = mode === 'assembly' || mode === 'disassembly';
  const maxStep = Math.max(0, order.length - 1);
  const jobLogs = logLines(job?.logs), visibleLogs = jobLogs.length ? jobLogs : logLines(result.logs);
  const canConvert = !!session && units.length > 0 && !!session.unit_state?.confirmed && !locked;
  const conversionReady = session?.conversion?.ready === true || (session?.conversion?.ready == null && session?.conversion?.status === 'ready');
  const engineReady = settings.strategy === 'learn' ? environment?.learn_ready : environment?.heuristic_ready;
  const canRun = !!session?.unit_state?.confirmed && engineReady && !locked;
  const hasResult = order.length > 0;
  const validPlayback = result.path_status === 'success';
  const firstAction = useMemo(() => playbackBoundary(run, 1, 0, 'assembly'), [run]);
  const canPlay = motionMode && canStartPlayback(run, stepIndex, playbackMode);
  const comparisonWarnings = comparisonDifferences(run, compare);
  const sceneCompatible = order.every(id => units.some(unit => unit.id === id));
  const viewerCad = motionMode ? runCad ?? (sceneCompatible ? cad : null) : cad;
  const viewerObj = motionMode ? runObj ?? (sceneCompatible ? obj : null) : obj;
  const nameFor = (id: unknown) => {
    const unitId = mapId(run, id);
    const mapped = Object.values(run?.part_map ?? {}).find((value: any) => value?.unit_id === unitId || value?.id === unitId) as Json | undefined;
    return units.find(unit => unit.id === unitId)?.name ?? mapped?.name ?? mapped?.display_label ?? unitId;
  };
  useEffect(() => {
    if (!playing || !validPlayback || !boundary.hasPath || !motionMode) return;
    let timer = 0, last = performance.now();
    const animate = (now: number) => {
      const delta = Math.min((now - last) / 1000, .2); last = now;
      setProgress(previous => Math.min(1, previous + delta * speed / 3));
      timer = requestAnimationFrame(animate);
    };
    timer = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(timer);
  }, [playing, validPlayback, boundary.hasPath, motionMode, speed]);
  useEffect(() => {
    if (!playing || progress < 1) return;
    if (stepIndex < maxStep) { setStepIndex(index => index + 1); setProgress(0); }
    else setPlaying(false);
  }, [playing, progress, stepIndex, maxStep]);
  useEffect(() => { if (playing && (!boundary.hasPath || !validPlayback)) setPlaying(false); }, [playing, boundary.hasPath, validPlayback]);
  function selectStep(index: number) { setPlaying(false); setStepIndex(Math.max(0, Math.min(maxStep, index))); setProgress(0); }
  function changeMode(next: ViewerMode) { setPlaying(false); setMode(next); setProgress(0); setStepIndex(0); }
  async function upload(file: File) {
    if (!/\.(stp|step)$/i.test(file.name)) { setError(new ApiError('완제품 .stp 또는 .step 파일을 선택하세요.', 'INVALID_EXTENSION')); return; }
    await withAction(async () => {
      const form = new FormData(); form.append('file', file);form.append('input_mode',inputMode);
      const data = await api('/sessions', { method: 'POST', body: form });
      const id = data.id ?? data.session_id;
      await projectRequest(projectId,'asap-link',{session_id:id,expected_input_revision:inputRevision});
      setSession({ id, status: 'parsing', input_mode:inputMode, source: { name: file.name }, units: [], manifest: {}, runs: [] });
      setCad(null); setObj(null); setRun(null); setCompare(null); setSelected(null); setMode('cad'); setLeftTab('parts'); setPlaying(false); setIsolate(false);
      setJob({ id: data.job_id ?? data.parse_job_id, status: 'queued', stage: 'STEP 등록' });
      await refreshSessions();
    });
  }
  async function startRun() {
    if (!session) return;
    await withAction(async () => {
      if (!conversionReady) {
        pendingRun.current={settings:{...settings},sessionId:session.id,revision:session.unit_state.revision};
        let data:Json;try{data=await post(`/sessions/${session.id}/convert`,convertSettings);}catch(e){pendingRun.current=null;throw e;}
        setJob({id:data.job_id,status:'queued',stage:'자동 CAD 전처리',type:'convert'});setRightTab('logs');return;
      }
      const data = await post(`/sessions/${session.id}/runs`, {...settings,expected_unit_revision:session.unit_state.revision});
      setRun(null); setRunCad(null); setRunObj(null); setMode('cad'); setPlaying(false);
      setJob({ id: data.job_id, status: 'queued', stage: '자동 네이티브 전처리·ASAP', run_id: data.id ?? data.run_id, type: 'run' });
      setRightTab('logs');
    });
  }
  function baseOptions() {
    const raw = session?.part_map ?? session?.conversion?.part_map ?? {};
    const map = Array.isArray(raw) ? Object.fromEntries(raw.map(item => [String(item.numeric_id ?? item.engine_id), item])) : raw;
    return Object.entries(map).map(([id, item]) => ({ id, unitId: typeof item === 'string' ? item : String((item as Json).unit_id ?? (item as Json).id), name: typeof item === 'string' ? item : String((item as Json).name ?? (item as Json).display_label ?? (item as Json).original_names?.[0] ?? '') }));
  }
  const baseUnits = baseOptions();
  const jobProgress = typeof job?.progress === 'number' ? Math.min(100, Math.max(0, job.progress <= 1 ? job.progress * 100 : job.progress)) : null;
  const quality = session?.quality_report ?? session?.conversion?.quality_report ?? session?.conversion ?? {};
  return <div className="asap-workspace"><div className="app-shell">
    {showUnitEditor && session && <UnitEditor session={session} scene={cad} locked={locked} onClose={() => setShowUnitEditor(false)} onRefresh={() => loadSession(session.id, true)} onJob={id => { setRun(null); setObj(null); setMode('cad'); setPlaying(false); setSelected(null); setJob({ id, status: 'queued', stage: '조립 단위 편집' }); }} />}
    {showTemplates&&session&&<TemplatePanel session={session} locked={locked} onClose={()=>setShowTemplates(false)} onRefresh={()=>loadSession(session.id,true)} onJob={id=>setJob({id,status:'queued',stage:'기준 부품 해석·매칭'})}/>}
    {readOnly&&<div className="readonly-strip">확정본의 입력·단위·순서는 보존됩니다. 이력에서 새 초안을 만든 후 편집하세요.</div>}<header className="app-header">
      <div className="brand"><div className="brand-symbol"><Layers3 size={21} /></div><div><h1>ASAP <span>PHYSICS WORKBENCH</span></h1><p>원본 보존 · 조립 단위 · 실제 경로</p></div></div>
      <div className="header-actions">
        <button className="button primary" disabled={locked} onClick={() => fileRef.current?.click()}><FileUp size={16} />완제품 STEP 등록</button>
        <button className="button" disabled={!session || !units.length || locked} onClick={() => setShowUnitEditor(true)}><Layers3 size={16} />조립 단위 편집·확정</button>
        <button className="button" disabled={!canConvert} onClick={() => withAction(async () => { const data = await post(`/sessions/${session!.id}/convert`, convertSettings); setJob({ id: data.job_id, status: 'queued', stage: 'OBJ 변환' }); })}><Layers3 size={16} />OBJ 변환·검증</button>
        <button className="button engine" disabled={!canRun} onClick={startRun}><Play size={15} fill="currentColor" />자동 전처리·ASAP 실행</button>
        {active && <button className="button danger" disabled={busy} onClick={() => withAction(async () => { pendingRun.current=null; const data = await post(`/jobs/${job!.id}/cancel`); setJob(data.id ? data : { ...job!, stage: '취소 요청 · 프로세스 종료 대기' }); })}><Square size={13} fill="currentColor" />취소</button>}
        <button className="icon-button header-settings" title="엔진 환경 설정" onClick={() => setShowConfig(true)}><Settings2 size={20} /></button>
      </div>
      <input ref={fileRef} type="file" accept=".stp,.step" className="hidden" onChange={event => { const file = event.target.files?.[0]; if (file) void upload(file); event.target.value = ''; }} />
    </header>
    <div className="input-mode-strip"><label>입력 방식 <select aria-label="입력 방식" value={inputMode} disabled={locked} onChange={e=>{const value=e.target.value;setInputMode(value);if(session)void withAction(async()=>{await api(`/sessions/${session.id}/input-mode`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({input_mode:value})});await loadSession(session.id,true);});}}><option value="assembly_only">완제품만</option><option value="assembly_with_parts">완제품＋하위 부품</option></select></label><span>완제품 STEP 1개: {session?sourceName(session):'상단 STEP 등록에서 선택'}</span>{inputMode==='assembly_with_parts'?<><button className="button" disabled={!session||locked} onClick={()=>setShowTemplates(true)}>하위 부품 STEP 여러 개 등록·매칭</button><span>기준 파일 {session?.templates?.files?.filter((f:Json)=>f.status==='ready').length??0}개</span></>:<span>기준 파일 없이 조립 단위 편집 사용</span>}</div>
    <div className="status-strip">
      <div className="current-file"><Box size={15} /><strong>{session ? sourceName(session) : '완제품 STEP 파일을 등록하세요'}</strong>{session && <span className="status-chip">{label(session.status)}</span>}</div>
      <div className="environment-status"><i className={`dot ${environment?.learn_ready ? 'green' : 'amber'}`} /><span>{environment?.learn_ready ? '사전학습 엔진 준비됨' : environment?.heuristic_ready ? '휴리스틱 준비됨 · 모델 확인 필요' : '엔진 환경 확인 필요'}</span><button onClick={() => setShowConfig(true)}>환경 보기</button></div>
    </div>
    {error && <div className="error-banner" role="alert"><AlertCircle size={18} /><div><strong>{error.message}</strong>{error.reason && <code>{error.reason}</code>}{error.action && <p>{error.action}</p>}</div><button className="icon-button" onClick={() => setError(null)} title="알림 닫기"><X size={16} /></button></div>}
    <main className="workspace">
      <aside className="left-panel">
        <div className="session-switch"><FolderOpen size={16} /><select aria-label="저장 입력 다시 열기" value={session?.id ?? ''} disabled={locked} onChange={event => { if (event.target.value) void withAction(async()=>{await projectRequest(projectId,'asap-link',{session_id:event.target.value,expected_input_revision:inputRevision});await loadSession(event.target.value);}); }}><option value="">저장 입력 다시 열기</option>{sessions.map(item => <option key={item.id} value={item.id}>{sourceName(item)} · {short(item.id)}</option>)}</select></div>
        <div className="panel-tabs"><button className={leftTab === 'parts' ? 'selected' : ''} onClick={() => setLeftTab('parts')}>조립 단위 <span>{units.length}</span></button><button className={leftTab === 'sequence' ? 'selected' : ''} onClick={() => setLeftTab('sequence')}>생성 순서 <span>{order.length || '—'}</span></button></div>
        {leftTab === 'parts' ? <>
          <div className="search"><Search size={15} /><input placeholder="부품명 · 배치 ID 검색" value={query} onChange={event => setQuery(event.target.value)} /><span>{filteredUnits.length}</span></div>
          <div className="part-list">{filteredUnits.map(unit => <button key={unit.id} className={`part-item ${selected === unit.id ? 'selected' : ''}`} onClick={() => { setSelected(unit.id); setRightTab('details'); }}><div className={`part-icon ${unit.valid === false ? 'invalid' : ''}`}><Box size={16} /></div><div><strong>{unit.name || `부품 ${short(unit.id)}`}</strong><small>{short(unit.id)} · {unit.solid_count ?? 1} solid{(unit.solid_count ?? 1) > 1 ? ' · 복합 단위' : ''}</small></div>{unit.valid === false && <AlertCircle size={14} className="text-warning" />}</button>)}{!units.length && <div className="panel-empty"><Box size={25} /><p>등록한 STEP의 각 배치가<br />여기에 표시됩니다.</p></div>}{!!units.length && !filteredUnits.length && <div className="panel-empty">검색 결과가 없습니다.</div>}</div>
          <div className="left-footnote"><ShieldCheck size={15} /><span>동일 파일을 사용한 부품도<br />각 배치 ID로 구분합니다.</span></div>
        </> : <>
          <div className="sequence-heading"><span>{mode === 'disassembly' ? '분해 순서' : '조립 순서'}</span><span>{hasResult ? `${order.length}개 단위` : '생성 결과 없음'}</span></div>
          <div className="part-list sequence-list">{(mode === 'disassembly' ? [...planSteps.map(step => mapId(run, step.moving_part)), order[0]].filter(Boolean) : order).map((id, index) => <button key={`${id}-${index}`} className={`sequence-item ${motionMode && stepIndex === index ? 'selected' : ''}`} onClick={() => { if (!motionMode) setMode('assembly'); selectStep(index); setSelected(id); }}><span className="step-number">{index + 1}</span><div><strong>{nameFor(id)}</strong><small>{short(id)}{mode !== 'disassembly' && index === 0 ? ' · 기준 부품' : mode === 'disassembly' && index === maxStep ? ' · 기준 부품 잔류' : ''}</small></div>{motionMode && stepIndex === index && <ChevronRight size={15} />}</button>)}{!hasResult && <div className="panel-empty"><ArrowLeftRight size={26} /><p>OBJ 검증 후 ASAP를 실행하면<br />실제 생성된 순서를 확인할 수 있습니다.</p></div>}</div>
          {!!hasResult && <div className="left-footnote"><i className="dot green" /><span>기준 부품 + 분해 행동의 역순<br />{planSteps.length}개 행동 · {order.length}개 조립 단위</span></div>}
        </>}
      </aside>
      <section className="center-panel">
        <div className="viewer-header"><div className="mode-tabs">{([['cad', '원본 STEP'], ['obj', 'OBJ 복원'], ['overlay', '겹쳐 보기']] as [ViewerMode, string][]).map(([value, name]) => <button key={value} className={mode === value ? 'selected' : ''} disabled={value !== 'cad' && !obj} onClick={() => changeMode(value)}>{name}</button>)}<div className="tab-separator" /><button disabled={!hasResult} className={motionMode ? 'selected' : ''} onClick={() => changeMode('assembly')}>순서 재생</button></div><label className="check-label"><input type="checkbox" checked={isolate} disabled={!selected} onChange={event => setIsolate(event.target.checked)} />선택 격리</label></div>
        <AssemblyViewer selectedIds={diagnosticIds.includes(selected ?? '') ? diagnosticIds : undefined} focusToken={focusToken} cad={viewerCad} obj={viewerObj} mode={mode} selected={selected} isolate={isolate} showFuture={showFuture} showPath={showPath} boundary={motionMode ? boundary : null} progress={progress} coordinateMap={coordinateMap} onSelect={id => { setSelected(id); setRightTab('details'); }} />
        <div className="scope-notice"><ShieldCheck size={14} /><span>강체 조립의 기계적 순서·경로 테스트</span><i />현장 규칙·공구·토크·유연체 공정 미검증</div>
        <div className={`playback-panel ${!motionMode ? 'inactive' : ''}`}>
          <div className="playback-top"><div className="direction-toggle"><button disabled={!hasResult} className={mode === 'assembly' ? 'selected' : ''} onClick={() => changeMode('assembly')}>조립</button><button disabled={!hasResult} className={mode === 'disassembly' ? 'selected' : ''} onClick={() => changeMode('disassembly')}>분해</button></div><div className="step-description"><strong>{motionMode && hasResult ? `${stepIndex + 1} / ${order.length}` : '— / —'}</strong><span>{motionMode && hasResult ? boundary.moving ? nameFor(boundary.moving) : boundary.label : 'ASAP 결과를 선택하세요'}</span></div><label className="check-label"><input type="checkbox" checked={showPath} onChange={event => setShowPath(event.target.checked)} />경로</label><label className="check-label"><input type="checkbox" checked={showFuture} onChange={event => setShowFuture(event.target.checked)} />미래 부품</label></div>
          <div className="playback-controls"><button className="icon-button" disabled={!motionMode || !hasResult || stepIndex === 0} onClick={() => selectStep(0)} title="처음 단계"><SkipBack size={17} /></button><button className="icon-button" disabled={!motionMode || !hasResult || stepIndex === 0} onClick={() => selectStep(stepIndex - 1)} title="이전 단계"><ChevronLeft size={21} /></button><button className="play-button" disabled={!canPlay} onClick={() => { if (!playing && mode === 'assembly' && stepIndex === 0 && firstAction.hasPath) { setStepIndex(1); setProgress(0); setPlaying(true); } else { if (!playing && progress >= 1) setProgress(0); setPlaying(!playing); } }} title={canPlay ? '실제 경로 재생·정지' : validPlayback ? '이 단계의 실제 경로가 없습니다' : '실제 경로 출력이 완료된 결과에서 재생할 수 있습니다'}>{playing ? <Pause size={18} fill="currentColor" /> : <Play size={18} fill="currentColor" />}</button><button className="icon-button" disabled={!motionMode || !hasResult || stepIndex >= maxStep} onClick={() => selectStep(stepIndex + 1)} title="다음 단계"><ChevronRight size={21} /></button><button className="icon-button" disabled={!motionMode || !hasResult || stepIndex >= maxStep} onClick={() => selectStep(maxStep)} title="마지막 단계"><SkipForward size={17} /></button><input className="path-slider" aria-label="현재 단계 경로 진행률" type="range" min={0} max={1} step={.001} value={progress} disabled={!motionMode || !validPlayback || !boundary.hasPath} onChange={event => { setPlaying(false); setProgress(Number(event.target.value)); }} /><span className="progress-number">{Math.round(progress * 100)}%</span><select aria-label="재생 속도" value={speed} onChange={event => setSpeed(Number(event.target.value))}><option value={.5}>0.5×</option><option value={1}>1×</option><option value={2}>2×</option></select></div>
          <div className="path-caption">{motionMode && hasResult ? !validPlayback ? '경로 출력이 완료되지 않아 연속 재생은 비활성입니다. 단계별 설치 상태를 확인하세요.' : boundary.hasPath ? `${boundary.path.length}개 실제 경로 샘플 · 관찰용 재생 속도` : boundary.moving ? '이 단계는 경로가 없습니다. 설치 상태만 표시합니다.' : boundary.label : '단계 이동은 순서 생성 후, 연속 재생은 실제 경로가 있을 때 사용할 수 있습니다.'}</div>{motionMode && poseEvent.changed && <div className="pose-transition-note"><RotateCw size={12} /><strong>조립체 자세 전환</strong><span title={poseEvent.affected.join(', ')}>설치된 {poseEvent.affected.length}개 부품 전체에 적용</span><span>연속 재배향 경로 미검증 · 상태 전환</span></div>}
        </div>
      </section>
      <aside className="right-panel">
        <div className="panel-tabs"><button className={rightTab === 'settings' ? 'selected' : ''} onClick={() => setRightTab('settings')}>실행·결과</button><button className={rightTab === 'details' ? 'selected' : ''} onClick={() => setRightTab('details')}>부품·품질</button><button className={rightTab === 'logs' ? 'selected' : ''} onClick={() => setRightTab('logs')}>로그{active && <i className="dot amber" />}</button></div>
        <div className="right-content">{rightTab === 'settings' && <>
          <section className="settings-section"><div className="section-label"><span>다음 실행 설정</span><code>dfs</code></div><div className="strategy-select"><button className={settings.strategy === 'learn' ? 'selected' : ''} disabled={locked} onClick={() => setSettings({ ...settings, strategy: 'learn' })}><strong>사전학습 GNN</strong><span>learn · 가중치 동결</span></button><button className={settings.strategy === 'heur-out' ? 'selected' : ''} disabled={locked} onClick={() => setSettings({ ...settings, strategy: 'heur-out' })}><strong>휴리스틱 비교</strong><span>heur-out · 공식 ASAP</span></button></div><div className="form-grid"><NumberField label="평가 예산" value={settings.budget} min={1} max={100000} disabled={locked} onChange={value => setSettings({ ...settings, budget: value })} /><NumberField label="전체 제한시간 (초)" value={settings.timeout_s} min={5} max={86400} disabled={locked} onChange={value => setSettings({ ...settings, timeout_s: value })} /><NumberField label="Seed" value={settings.seed} min={0} max={2147483647} disabled={locked} onChange={value => setSettings({ ...settings, seed: value })} /><NumberField label="Max gripper" value={settings.max_gripper} min={2} max={20} disabled={locked} onChange={value => setSettings({ ...settings, max_gripper: value })} /></div><label className="field full"><span>고정 기준 부품 <small>선택 사항</small></span><select disabled={locked || !conversionReady} value={settings.base_part ?? ''} onChange={event => setSettings({ ...settings, base_part: event.target.value || null })}><option value="">자동 탐색 · 전체 재배향 허용</option>{baseUnits.map(item => <option key={item.id} value={item.id}>{units.find(unit => unit.id === item.unitId)?.name ?? item.name ?? item.unitId} · {item.id}</option>)}</select></label>{settings.base_part && <p className="inline-note">기준 부품 고정 시 전체 조립체 재배향이 제한됩니다.</p>}<button className="advanced-toggle" onClick={() => setShowAdvanced(!showAdvanced)}><ChevronDown size={14} className={showAdvanced ? 'rotate' : ''} />좌표·메시 고급 설정</button>{showAdvanced && <div className="advanced-panel"><div className="form-grid"><NumberField label="Pose 후보 수" value={settings.max_pose} min={1} max={100} disabled={locked} onChange={value => setSettings({ ...settings, max_pose: value })} /><NumberField label="Pose reuse" value={settings.pose_reuse} min={0} max={100} disabled={locked} onChange={value => setSettings({ ...settings, pose_reuse: value })} /><NumberField label="메시 오차 (mm)" value={convertSettings.deflection_mm} min={.001} max={5} step={.01} disabled={locked} onChange={value => setConvertSettings({ ...convertSettings, deflection_mm: value })} /><NumberField label="각도 오차 (rad)" value={convertSettings.angular_deflection_rad} min={.03} max={1} step={.01} disabled={locked} onChange={value => setConvertSettings({ ...convertSettings, angular_deflection_rad: value })} /><NumberField label="엔진 최대 길이" value={convertSettings.engine_extent} min={.1} max={1000} step={.1} disabled={locked} onChange={value => setConvertSettings({ ...convertSettings, engine_extent: value })} /></div><label className="check-label meshing-option"><input type="checkbox" checked={convertSettings.adaptive_meshing} disabled={locked} onChange={event => setConvertSettings({ ...convertSettings, adaptive_meshing: event.target.checked })} />곡면·작은 형상의 적응형 메시 정밀화</label><label className="check-label meshing-option"><input type="checkbox" checked={convertSettings.brep_repair_policy === 'verified_topology'} disabled={locked} onChange={event => setConvertSettings({ ...convertSettings, brep_repair_policy: event.target.checked ? 'verified_topology' : 'preserve' })} />형상 보존 검증을 통과한 STEP 위상 보정 허용</label><label className="field full"><span>복수 솔리드 분석 방식</span><select value={convertSettings.multi_solid_policy} disabled={locked} onChange={event => setConvertSettings({ ...convertSettings, multi_solid_policy: event.target.value })}><option value="preserve">원본 솔리드 유지</option><option value="verified_union">검증된 점유 체적 합집합</option></select></label>{convertSettings.multi_solid_policy === 'verified_union' && <><div className="union-tolerance"><NumberField label="합집합 형상 허용 오차 (mm)" value={convertSettings.union_tolerance_mm} min={1e-8} max={.001} step={1e-6} disabled={locked} onChange={value => setConvertSettings({ ...convertSettings, union_tolerance_mm: value })} /></div><p className="inline-note">겹치는 솔리드를 하나의 강체로 다룰 때 선택하세요. 원본과 원시 OBJ를 보존하고 점유 체적 일치 검증을 통과한 합집합 형상을 분석에 사용합니다. 검증 실패는 실행을 차단합니다.</p></>}<p className="inline-note">메시·크기 설정은 다음 OBJ 변환에 적용됩니다. 모든 부품에 공통 좌표 변환을 사용합니다.</p><PhysicsSettings settings={settings} disabled={locked} onChange={setSettings} />{coordinateMap && <details><summary>좌표·물리 근사 프로파일</summary><pre className="json-preview">{JSON.stringify(coordinateMap, null, 2)}</pre></details>}</div>}<p className="small-note">GNN은 후보 순위를 제안하고 물리 탐색이 순서를 확인합니다. 로봇 팔·그리퍼 접근성은 검사하지 않습니다.</p></section>
          <section className="settings-section"><div className="section-label"><span>저장 실행 결과</span><span>{session?.runs?.length ?? 0}</span></div><select className="run-select" aria-label="저장 결과 열기" disabled={active || busy || !session?.runs?.length} value={run?.id ?? ''} onChange={event => { if (event.target.value) void withAction(() => loadRun(event.target.value)); }}><option value="">실행 결과 선택</option>{session?.runs?.map(item => <option key={item.id} value={item.id}>{item.settings?.strategy ?? item.strategy ?? 'ASAP'} · {label(item.sequence_status ?? item.status)} · {short(item.id)}</option>)}</select>{run ? <><div className="result-status"><StatusLine title="자동 전처리" status={result.preprocessing_status==='passed'?'success':result.preprocessing_status} /><StatusLine title="순서 생성" status={result.sequence_status ?? run.status} /><StatusLine title="실제 경로" status={result.path_status} /></div><div className="saved-run-settings"><strong>저장된 실행 조건</strong><p>Seed {number(savedSettings.seed, 0)} · 예산 {number(savedSettings.budget, 0)} · 제한 {number(savedSettings.timeout_s, 0)}초<br />Max gripper {number(savedSettings.max_gripper, 0)} · Pose 후보 {number(savedSettings.max_pose, 0)}<br /><span title={String(savedSettings.base_part ?? '')}>기준 부품: {'base_part' in savedSettings ? savedSettings.base_part ? `고정 · ${nameFor(savedSettings.base_part)}` : '자동' : '미기록'}</span></p></div><dl className="metric-list"><div><dt>준비 단위 / 원본 솔리드</dt><dd>{result.preprocessing_summary?.prepared_body_count ?? '—'} / {result.preprocessing_summary?.original_solid_count ?? '—'}</dd></div><div><dt>전략</dt><dd>{run.settings?.strategy ?? result.provenance?.strategy ?? '—'}</dd></div><RunMetrics result={result} /><div><dt>종료 이유</dt><dd>{result.reason_code ?? result.stop_reason ?? '—'}</dd></div><div><dt>기준 부품</dt><dd title={String(result.base_part ?? '')}>{result.base_part != null ? nameFor(result.base_part) : '—'}</dd></div></dl><div className="result-actions"><button className="button primary" disabled={locked||result.sequence_status!=='success'} onClick={()=>withAction(async()=>{await projectRequest(projectId,'asap-import',{run_id:run.id,expected_input_revision:inputRevision});onImported();})}><CheckCircle2 size={14}/>공정 초안에 적용</button><a className="button" href={`/api/asap/runs/${run.id}/export`} download><Download size={14} />JSON</a><a className="button" href={`/api/asap/runs/${run.id}/bundle`} download><Download size={14} />실행 ZIP</a><button className="button" onClick={() => setShowCompare(true)}><GitCompareArrows size={14} />비교</button></div><PhysicsRecord result={result} /><details className="provenance"><summary>입력·엔진·가중치 기록</summary><pre className="json-preview">{JSON.stringify({ settings: run.settings, stats: result.stats, provenance: result.provenance, diagnostics: result.diagnostics }, null, 2)}</pre></details></> : <p className="small-note">실행별 순서·경로·조건이 보존됩니다. 엔진이 준비되지 않아도 STEP과 OBJ를 확인할 수 있습니다.</p>}</section>
        </>}
        {rightTab === 'details' && <>
          <section className="settings-section"><div className="section-label"><span>선택 조립 단위</span><Box size={15} /></div>{selectedUnit ? <><h3 className="unit-name">{selectedUnit.name}</h3><dl className="metric-list unit-details"><div><dt>배치 ID</dt><dd className="monospace wrap">{selectedUnit.id}</dd></div><div><dt>정의 ID</dt><dd className="monospace wrap">{selectedUnit.definition_id}</dd></div><div><dt>크기</dt><dd>{bboxText(selectedUnit)}</dd></div><div><dt>체적</dt><dd>{number(selectedUnit.volume_mm3)} mm³</dd></div><div><dt>솔리드 수</dt><dd>{selectedUnit.solid_count}</dd></div><div><dt>원본 STEP 상태</dt><dd>{selectedUnit.valid === false ? '확인 필요' : '유효'}</dd></div>{selectedUnit.parent_id && <div><dt>상위 배치</dt><dd className="monospace wrap">{selectedUnit.parent_id}</dd></div>}</dl><PartGeometryRecord session={session} unit={selectedUnit} />{selectedUnit.solid_count > 1 && <><button className="button full-width" disabled={locked} onClick={() => withAction(async () => { const data = await post(`/sessions/${session!.id}/units/split`, { unit_ids: [selectedUnit.id] }); setRun(null); setObj(null); setMode('cad'); setPlaying(false); setSelected(null); setJob({ id: data.job_id, status: 'queued', stage: '독립 솔리드 분할' }); })}><Layers3 size={15} />독립 솔리드로 분할</button><p className="small-note">각 솔리드를 별도 강체로 분석할 때 선택합니다. 분할하면 OBJ를 다시 변환해야 하며 이전 실행은 원래 입력으로 보존됩니다.</p></>}</> : <p className="small-note">목록 또는 3D에서 부품을 선택하세요.</p>}</section>
          <section className="settings-section"><div className="section-label"><span>변환 품질</span><span>{label(session?.conversion?.status ?? '미변환')}</span></div><dl className="metric-list"><div><dt>CAD 배치 / 분석 단위</dt><dd>{number(session?.manifest?.instances_count ?? session?.manifest?.metrics?.unit_count, 0)} / {units.length}</dd></div><div><dt>OBJ 수</dt><dd>{number(session?.conversion?.obj_count ?? session?.conversion?.mesh_count ?? quality.obj_count ?? quality.mesh_count, 0)}</dd></div><div><dt>원본 단위</dt><dd>{session?.manifest?.unit ?? session?.manifest?.length_unit ?? '—'}</dd></div><div><dt>원본 해시</dt><dd className="monospace wrap" title={session?.manifest?.source_sha256 ?? session?.manifest?.source?.sha256}>{short(session?.manifest?.source_sha256 ?? session?.manifest?.source?.sha256) || '—'}</dd></div></dl><InspectionScope report={quality.initial_penetration} />{session && <DiagnosticPanel session={session} onLocate={ids=>{setDiagnosticIds(ids);setSelected(ids[0]);setMode('cad');setIsolate(true);setPlaying(false);setFocusToken(n=>n+1);}} selectedIds={diagnosticIds.includes(selected ?? '') ? diagnosticIds : selected ? [selected] : []} />}{session?.conversion && <details><summary>변환 진단 전체 보기</summary><pre className="json-preview">{JSON.stringify(quality, null, 2)}</pre></details>}</section>
          {motionMode && hasResult && <section className="settings-section"><div className="section-label"><span>현재 단계</span><span>{stepIndex + 1}</span></div><p className="small-note">설치된 부품 {boundary.installed.size}개 · 지지 대상 {boundary.fixed.size}개</p>{poseEvent.changed && <div className="pose-event-detail"><strong>조립체 자세 전환 · 적용 범위</strong><p>단계 시작에 설치된 모든 부품에 같은 전체 자세를 적용합니다. 연속 재배향 동작을 보간하지 않습니다.</p><details><summary>적용된 {poseEvent.affected.length}개 배치 보기</summary><ul>{poseEvent.affected.map(id => <li key={id}>{nameFor(id)}<code>{id}</code></li>)}</ul></details></div>}{[...boundary.fixed].map(id => <button className="support-item" key={id} onClick={() => setSelected(id)}><i className="dot green" />{nameFor(id)}<small>{short(id)}</small></button>)}</section>}
        </>}
        {rightTab === 'logs' && <><section className="settings-section"><div className="section-label"><span>작업 상태</span><Terminal size={15} /></div>{job ? <><div className="job-status"><i className={`dot ${active ? 'amber' : job.status === 'success' ? 'green' : 'red'}`} /><strong>{label(job.status)}</strong><span>{job.stage}</span></div>{active && <div className={`job-progress ${jobProgress === null ? 'indeterminate' : ''}`}><i style={{ width: jobProgress === null ? '35%' : `${jobProgress}%` }} /></div>}<dl className="metric-list"><div><dt>작업 ID</dt><dd className="monospace">{short(job.id)}</dd></div><div><dt>진행률</dt><dd>{jobProgress === null ? '작업 단계 기준' : `${Math.round(jobProgress)}%`}</dd></div>{job.elapsed_s != null && <div><dt>경과시간</dt><dd>{seconds(job.elapsed_s)}</dd></div>}</dl></> : <p className="small-note">등록·변환·ASAP 실행의 실제 로그가 여기에 표시됩니다.</p>}</section><div className="log-output">{visibleLogs.map((line, index) => <div key={index}>{line}</div>)}{!visibleLogs.length && <span>수집된 로그가 없습니다.</span>}</div></>}
        </div>
      </aside>
    </main>
    <footer className="app-footer"><span><i className="dot green" />로컬 파일 처리</span><span>학습 없음 · 기존 순서 참조 없음 · 실행별 입력·조건 기록</span>{active && <span className="footer-job"><LoaderCircle size={13} className="spin" />{job.stage}</span>}</footer>
    {showConfig && <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) setShowConfig(false); }}><section className="modal config-modal" role="dialog" aria-modal="true" aria-label="ASAP 환경 설정"><div className="modal-header"><div><h2>ASAP 실행 환경</h2><p>CAD/API와 엔진 Python을 분리해 실행합니다.</p></div><button className="icon-button" onClick={() => setShowConfig(false)} title="환경 설정 닫기"><X size={20} /></button></div><div className="modal-content"><div className="transport-tabs"><button className={config.engine?.transport === 'native' ? 'selected' : ''} onClick={() => setConfig({ ...config, engine: { ...config.engine, transport: 'native' } })}>네이티브 Python</button><button className={config.engine?.transport === 'wsl' ? 'selected' : ''} onClick={() => setConfig({ ...config, engine: { ...config.engine, transport: 'wsl' } })}>WSL2 엔진</button></div>{(['python', 'asap_root', 'weights_path', ...(config.engine?.transport === 'wsl' ? ['distribution'] : [])] as string[]).map(key => <label className="field full" key={key}><span>{{ python: 'ASAP Python 실행파일', asap_root: 'ASAP 저장소 경로', weights_path: '사전학습 가중치 경로', distribution: 'WSL 배포판 (비우면 기본 배포판)' }[key]}</span><input value={config.engine?.[key] ?? ''} placeholder={key === 'python' ? config.engine?.transport === 'wsl' ? '/home/user/envs/asap/bin/python' : 'C:\\...\\asap-env\\python.exe' : key === 'asap_root' ? '공식 ASAP checkout 경로' : key === 'weights_path' ? 'pretrained_model.pt' : ''} onChange={event => setConfig({ ...config, engine: { ...config.engine, [key]: event.target.value } })} /></label>)}<p className="inline-note">WSL 모드는 Linux 경로를 입력하세요. 엔진 누락이나 모델 호환 실패 시 원인을 표시하며 자동으로 다른 전략으로 대체하지 않습니다.</p><div className="environment-checks">{environment?.checks?.map((item: Json, index: number) => <div className="environment-check" key={index}>{item.status === 'ok' ? <CheckCircle2 size={17} className="text-success" /> : <AlertCircle size={17} className="text-warning" />}<div><strong>{item.name}</strong><span>{item.message}</span></div></div>)}</div><div className="runtime-info"><span>사용자 데이터: {config.data_dir ?? '서버 데이터 폴더'}</span><span>CAD Python: {config.cad_python ?? '서버 실행 환경'}</span></div></div><div className="modal-footer"><button className="button" disabled={checking || busy} onClick={checkEnvironment}>{checking ? <LoaderCircle size={15} className="spin" /> : <RotateCw size={15} />}현재 설정 환경 확인</button><button className="button primary" disabled={locked} onClick={() => withAction(async () => { setConfig(await api('/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(config) })); await checkEnvironment(); })}><Check size={15} />설정 저장·확인</button></div></section></div>}
    {showCompare && <div className="modal-backdrop"><section className="modal compare-modal" role="dialog" aria-modal="true" aria-label="ASAP 실행 비교"><div className="modal-header"><div><h2>실행 결과 비교</h2><p>같은 입력·좌표·메시·지지 조건에서 전략을 비교하세요.</p></div><button className="icon-button" onClick={() => setShowCompare(false)} title="비교 닫기"><X size={20} /></button></div><div className="modal-content"><label className="field full"><span>비교할 저장 실행</span><select value={compare?.id ?? ''} onChange={event => { if (event.target.value) void withAction(() => loadRun(event.target.value, true)); }}><option value="">실행 선택</option>{session?.runs?.filter(item => item.id !== run?.id).map(item => <option key={item.id} value={item.id}>{item.settings?.strategy ?? item.strategy} · {short(item.id)}</option>)}</select></label><div className="comparison-columns"><RunComparison run={run} units={units} /><RunComparison run={compare} units={units} /></div>{compare && <><div className={comparisonWarnings.length ? 'comparison-warning' : 'comparison-compatible'}><strong>{comparisonWarnings.length ? '조건 차이가 있어 전략만의 성능 비교로 해석할 수 없습니다.' : '입력·전처리·탐색 조건이 일치합니다.'}</strong>{comparisonWarnings.length > 0 && <ul>{comparisonWarnings.map((message, index) => <li key={index}>{message}</li>)}</ul>}</div><p className="small-note">서로 다른 순서도 유효할 수 있습니다. 단일 실행의 시간 차이는 통계적 성능 우위를 보장하지 않습니다.</p></>}</div></section></div>}
  </div></div>;
}
function NumberField({ label: name, value, min, max, step = 1, disabled, onChange }: { label: string; value: number; min: number; max: number; step?: number; disabled?: boolean; onChange: (value: number) => void }) {
  return <label className="field"><span>{name}</span><input type="number" min={min} max={max} step={step} value={value} disabled={disabled} onChange={event => { const next = event.target.valueAsNumber; if (Number.isFinite(next)) onChange(Math.max(min, Math.min(max, next))); }} /></label>;
}
function PhysicsSettings({ settings, disabled, onChange }: { settings: RunSettings; disabled: boolean; onChange: (value: RunSettings) => void }) {
  const profile = settings.physics_profile;
  const change = (field: string, value: number) => onChange({ ...settings, physics_profile: { ...profile!, [field]: value } });
  return <div className="settings-section"><label className="check-label"><input type="checkbox" disabled={disabled} checked={!!profile} onChange={event => onChange({ ...settings, physics_profile: event.target.checked ? { contact_eps_mm: .25, sdf_dx_mm: 1, sdf_res: 20, path_col_th_mm: .05, minimum_separation_mm: 5 } : null })} />물리 기준을 mm로 지정</label>{profile ? <><div className="form-grid"><NumberField label="접촉 거리 (mm)" value={profile.contact_eps_mm} min={.00001} max={10} step={.05} disabled={disabled} onChange={value => change('contact_eps_mm', value)} /><NumberField label="SDF 최대 간격 (mm)" value={profile.sdf_dx_mm} min={.0001} max={20} step={.1} disabled={disabled} onChange={value => change('sdf_dx_mm', value)} /><NumberField label="SDF 축 최소 분할" value={profile.sdf_res} min={4} max={200} disabled={disabled} onChange={value => change('sdf_res', value)} /><NumberField label="경로 접촉 오차 (mm)" value={profile.path_col_th_mm} min={0} max={1} step={.01} disabled={disabled} onChange={value => change('path_col_th_mm', value)} /><NumberField label="최소 분리 거리 (mm)" value={profile.minimum_separation_mm} min={.0001} max={100} step={1} disabled={disabled} onChange={value => change('minimum_separation_mm', value)} /></div><p className="inline-note">공통 CAD 좌표는 유지하고 물리 수치만 엔진 단위로 환산합니다. SDF 간격을 줄이면 메모리와 계산 시간이 증가합니다. 초기 중첩 보정은 공식 ASAP 방식대로 기록됩니다.</p></> : <p className="inline-note">공식 ASAP 기본 물리 수치를 사용합니다. mm 환산값은 실행 기록에서 확인할 수 있습니다.</p>}</div>;
}
function PartGeometryRecord({ session, unit }: { session: Session | null; unit: Unit }) {
  const quality = (session?.quality_report?.units ?? []).find((item: Json) => item.unit_id === unit.id);
  if (!quality) return null;
  return <div><p className="small-note">현재 분석 BREP: {quality.analysis_brep_valid ? '유효' : '확인 필요'} · 메시: {quality.status === 'ready' ? '검증 통과' : '확인 필요'}<br />토폴로지 보정: {quality.topology_repair_audit?.status ?? '—'}</p><details className="provenance"><summary>형상 보존·원본 솔리드 검증</summary><pre className="json-preview">{JSON.stringify({ source_brep_valid: quality.source_brep_valid, analysis_brep_valid: quality.analysis_brep_valid, errors: quality.errors, boundary_edges: quality.boundary_edge_count, nonmanifold_edges: quality.nonmanifold_edge_count, source_volume_mm3: quality.source_cad_volume_mm3, volume_relative_error: quality.volume_relative_error, topology_repair_audit_path: quality.topology_repair_audit_path, internal_occupancy_audit: quality.internal_occupancy_audit, solid_partition_audit: quality.solid_partition_audit }, null, 2)}</pre></details></div>;
}
function PhysicsRecord({ result }: { result: Json }) {
  const physical = result.physics_parameters ?? result.provenance?.physics_parameters;
  return physical ? <details className="provenance"><summary>실제 물리 기준·초기 중첩 기록</summary><pre className="json-preview">{JSON.stringify(physical, null, 2)}</pre></details> : null;
}
function StatusLine({ title, status }: { title: string; status: unknown }) {
  return <div className="status-line"><span>{title}</span><strong className={status === 'success' ? 'text-success' : 'text-warning'}>{status === 'success' ? <CheckCircle2 size={14} /> : <AlertCircle size={14} />}{label(status)}</strong></div>;
}
function RunComparison({ run, units }: { run: Run | null; units: Unit[] }) {
  const result = resultOf(run);
  return <div className="comparison-column">{run ? <><h3>{run.settings?.strategy ?? result.provenance?.strategy ?? 'ASAP'}</h3><small className="monospace">{short(run.id)}</small><StatusLine title="순서" status={result.sequence_status} /><StatusLine title="경로" status={result.path_status} /><dl className="metric-list"><RunMetrics result={result} /></dl><ol>{assemblyOrder(run).map(id => <li key={id}>{units.find(unit => unit.id === id)?.name ?? id}<small>{short(id)}</small></li>)}</ol><details><summary>조건·해시</summary><pre className="json-preview">{JSON.stringify({ settings: run.settings, provenance: result.provenance }, null, 2)}</pre></details></> : <div className="panel-empty"><GitCompareArrows size={25} /><p>비교할 실행을 선택하세요.</p></div>}</div>;
}

function RunMetrics({ result }: { result: Json }) {
  const stats = result.stats ?? {};
  const times = stats.times_by_stage ?? result.times_by_stage ?? {};
  const total = stats.total_wall_time_s ?? stats.total_time_s ?? stats.total_time ?? result.elapsed_s ?? times.total;
  return <>
    <div><dt title="브리지가 기록한 실제 분해 물리 가능성 검사 호출 수입니다.">물리 검사</dt><dd>{number(stats.bridge_physics_evaluations, 0)}</dd></div>
    <div><dt title="ASAP 탐색기 n_eval 집계입니다. 두 부품 말단의 물리 검사는 이 탐색 평가 집계에 포함되지 않을 수 있습니다.">탐색 평가</dt><dd>{number(stats.n_eval ?? stats.evaluation_count ?? result.evaluation_count, 0)}</dd></div>
    <div><dt>총 실행시간</dt><dd>{seconds(total)}</dd></div>
    {typeof times.initialization_s === 'number' && <div><dt>초기화</dt><dd>{seconds(times.initialization_s)}</dd></div>}
    {typeof times.planning_s === 'number' && <div><dt>탐색</dt><dd>{seconds(times.planning_s)}</dd></div>}
    {typeof times.path_export_s === 'number' && <div><dt>경로 출력</dt><dd>{seconds(times.path_export_s)}</dd></div>}
  </>;
}

function InspectionScope({ report }: { report?: Json }) {
  if (!report) return null;
  const partial = report.status !== 'complete';
  return <div className={`inspection-scope ${partial ? 'partial' : ''}`}>
    <strong>{partial ? '초기 침투 검사 일부 미확인' : '초기 침투 검사 범위 완료'}</strong>
    <dl className="metric-list">
      <div><dt>전체 부품 쌍</dt><dd>{number(report.total_pairs, 0)}쌍</dd></div>
      <div><dt>경계 겹침 후보</dt><dd>{number(report.broadphase_candidates, 0)}쌍</dd></div>
      <div><dt>정밀검사 시도</dt><dd>{number(report.checked_pairs, 0)}쌍</dd></div>
      <div><dt>미검사·계산 실패</dt><dd>{number(report.unresolved_pair_count, 0)}쌍</dd></div>
      <div><dt>확인된 초기 침투</dt><dd>{number(report.penetration_pairs?.length, 0)}쌍</dd></div>
    </dl>
    {partial && <p>미검사·실패 부품 쌍의 간섭 여부는 확인되지 않았습니다. 검사 범위를 초과한 항목을 통과로 처리하지 않습니다.</p>}
  </div>;
}

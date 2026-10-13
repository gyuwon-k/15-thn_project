import {useEffect,useRef,useState} from 'react';
import {X} from 'lucide-react';
import {api,post} from './api';
import AssemblyViewer from './AssemblyViewer';
import type {Session,DisplayScene,Json} from './types';

export default function TemplatePanel({session,locked,onJob,onRefresh,onClose}:{session:Session;locked:boolean;onJob:(id:string)=>void;onRefresh:()=>Promise<void>;onClose:()=>void}) {
  const [selected,setSelected]=useState<string[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const [match,setMatch]=useState<Json|null>(null),[chosen,setChosen]=useState<string[]>([]),[candidate,setCandidate]=useState<Json|null>(null);
  const [source,setSource]=useState<DisplayScene|null>(null),[templateScene,setTemplateScene]=useState<DisplayScene|null>(null);
  const [templateId,setTemplateId]=useState(''),[manualUnits,setManualUnits]=useState<string[]>([]),[name,setName]=useState(''),[partNumber,setPartNumber]=useState('');
  const [focusToken,setFocusToken]=useState(0);const [scope,setScope]=useState<string[]>([]),[tolerance,setTolerance]=useState(.5);const fileRef=useRef<HTMLInputElement>(null);
  const templates:Json[]=session.templates?.templates??[],files:Json[]=session.templates?.files??[];
  const disabled=busy||locked, activeTemplate=templates.find(t=>t.id===templateId);
  const matchId=session.templates?.last_match_id;
  useEffect(()=>{api<DisplayScene>(`/sessions/${session.id}/scene?mode=source`).then(setSource).catch(e=>setError(String(e)));},[session.id]);
  useEffect(()=>{if(matchId)api<Json>(`/sessions/${session.id}/matches/${matchId}`).then(setMatch).catch(e=>setError(String(e)));},[matchId,session.id]);
  async function action(fn:()=>Promise<void>){setError('');setBusy(true);try{await fn();}catch(e){setError(String(e));}finally{setBusy(false);}}
  async function viewTemplate(id:string){setTemplateId(id);setCandidate(null);const t=templates.find(t=>t.id===id);setName(t?.name??'');setPartNumber(t?.part_number??'');setScope(t?.scope_ids??[]);setTemplateScene(await api(`/sessions/${session.id}/templates/${id}/scene`));}
  async function upload(files:File[]){await action(async()=>{const form=new FormData();files.forEach(f=>form.append('files',f));const result=await api<Json>(`/sessions/${session.id}/templates`,{method:'POST',body:form});onJob(result.job_id);});}
  const overlay=templateScene ? {...templateScene,meshes:templateScene.meshes.filter(m=>scope.includes(m.id)).map(m=>{
    const matrix=candidate?.template_to_cad_matrix;
    const positions=matrix?m.positions.map((_,i)=>{const row=i%3,base=i-row;return matrix[row*4]*m.positions[base]+matrix[row*4+1]*m.positions[base+1]+matrix[row*4+2]*m.positions[base+2]+matrix[row*4+3];}):m.positions;
    const normals=matrix&&m.normals ? m.normals.map((_,i)=>{const row=i%3,base=i-row;return matrix[row*4]*m.normals![base]+matrix[row*4+1]*m.normals![base+1]+matrix[row*4+2]*m.normals![base+2];}):m.normals;
    return {...m,id:'template_'+m.id,positions,normals,color:'#009caf'};
  })} : null;
  const highlight=candidate ? [...candidate.member_refs.map((m:Json)=>m.occurrence_id),...(overlay?.meshes.map(m=>m.id)??[])] : [];
  return <div className="modal-backdrop"><section className="unit-editor" role="dialog" aria-label="하위 부품 등록·매칭">
    <header className="modal-header"><div><h2>완제품＋하위 부품 · 형상 매칭</h2><p>기준 파일은 조립 단위를 식별합니다. 완제품의 원본 형상과 반복 배치를 유지합니다.</p></div><button className="icon-button" disabled={disabled} onClick={onClose} aria-label="부품 매칭 닫기"><X size={20}/></button></header>
    {error&&<div className="unit-error" role="alert">{error}</div>}
    <div className="unit-summary"><span>완제품: {typeof session.source==='string'?session.source:session.source?.name}</span><span>기준 파일 {files.filter(f=>f.status!=='inactive').length}개</span><span>정상 템플릿 {templates.length}개</span><span>후보 {match?.candidates?.length??0}개</span><strong>매칭 후보는 검토 후 확정</strong></div>
    <div className="unit-editor-body"><aside className="unit-editor-list">
      <button className="button primary" disabled={disabled} onClick={()=>fileRef.current?.click()}>하위 부품 STEP 여러 개 추가</button><input ref={fileRef} aria-label="하위 부품 STEP 파일들" type="file" multiple accept=".stp,.step" className="hidden" onChange={e=>{void upload(Array.from(e.target.files??[]));e.target.value='';}}/>
      <p className="small-note">같은 볼트 파일 하나로 여러 배치를 찾습니다. 기준 파일이 없어도 완제품만 방식으로 진행할 수 있습니다.</p>
      <button className="button" disabled={disabled||!templates.length} onClick={()=>setSelected(templates.map(t=>t.id))}>정상 기준 전체 선택</button><div className="unit-scroll">{files.filter(f=>f.status!=='inactive').map(f=><div className="template-file" key={f.id}><strong>{f.name}</strong><small>{f.status==='ready'?'정상':f.status==='failed'?'파싱 실패':'해석 중'} · {(f.size/1024).toFixed(1)} KiB</small>{f.error&&<p className="small-note">{f.error.message}</p>}{templates.filter(t=>t.file_id===f.id).map(t=><div key={t.id}><label><input type="checkbox" checked={selected.includes(t.id)} onChange={e=>setSelected(old=>e.target.checked?[...old,t.id]:old.filter(x=>x!==t.id))}/>{t.name}</label><small>솔리드 {t.solid_count} · {t.matching_status}</small><button className="button" disabled={disabled} onClick={()=>action(()=>viewTemplate(t.id))}>기준 형상 보기·범위 편집</button></div>)}<button className="button" disabled={disabled} onClick={()=>action(async()=>{await api(`/sessions/${session.id}/templates/${f.id}`,{method:'DELETE'});await onRefresh();})}>활성 입력에서 제거</button></div>)}</div>
      <label className="field"><span>형상 허용 오차 (mm)</span><input type="number" min={.01} max={5} step={.1} value={tolerance} onChange={e=>setTolerance(Math.max(.01,Math.min(5,Number(e.target.value)||.5)))}/></label>
      <button className="button primary" disabled={disabled||!selected.length||!session.units.length} onClick={()=>action(async()=>{const result=await post(`/sessions/${session.id}/matches`,{template_ids:selected,expected_revision:session.unit_state.revision,tolerance_mm:tolerance,timeout_s:120});onJob(result.job_id);})}>선택 기준 부품 매칭</button>
    </aside>
    <div className="unit-editor-view"><div className="unit-view-controls"><span>{candidate?'원본 대상 파랑 · 정합 기준 청록 와이어':'기준 파일 미리보기'}</span></div><AssemblyViewer cad={candidate?source:overlay} obj={candidate?overlay:null} mode={candidate?'overlay':'cad'} selected={null} selectedIds={highlight} focusToken={focusToken} isolate={!!candidate} showFuture={false} showPath={false} boundary={null} progress={0} onSelect={()=>{}}/></div>
    <aside className="unit-editor-actions"><h3>매칭 후보·검토</h3>{match?.timeout_reached&&<p className="small-note">매칭 시간 한도에 도달했습니다. 저장된 후보를 검토하거나 나머지는 수동 대응하세요.</p>}
      {match?.templates?.map((t:Json)=><p className="small-note" key={t.template_id}>{templates.find(x=>x.id===t.template_id)?.name}: 후보 {t.candidate_count}개 {t.reason??''}</p>)}
      <div className="match-candidates">{match?.candidates?.map((c:Json)=><div key={c.id}><label><input type="checkbox" checked={chosen.includes(c.id)} disabled={disabled||match.unit_revision!==session.unit_state.revision} onChange={e=>setChosen(old=>e.target.checked?[...old,c.id]:old.filter(x=>x!==c.id))}/><strong>{c.template_name}</strong></label><small>{c.member_names.join(' + ')} · p95 {c.metrics.p95_surface_distance_mm.toFixed(3)} mm · 중복 후보 {c.conflicts.length}</small><button className="button" onClick={()=>action(async()=>{await viewTemplate(c.template_id);setCandidate(c);setFocusToken(n=>n+1);})}>배치·정합 3D 확인</button></div>)}</div>
      <button className="button primary" disabled={disabled||!chosen.length||match?.unit_revision!==session.unit_state.revision} onClick={()=>action(async()=>{const r=await post(`/sessions/${session.id}/matches/${match!.id}/accept`,{candidate_ids:chosen,expected_revision:session.unit_state.revision});setChosen([]);onJob(r.job_id);})}>선택 후보를 조립 단위로 적용</button>
      {activeTemplate&&<><h3>기준 파일 정보·범위</h3><label className="field full"><span>표시명</span><input value={name} onChange={e=>setName(e.target.value)}/></label><label className="field full"><span>품번</span><input value={partNumber} onChange={e=>setPartNumber(e.target.value)}/></label>{templateScene?.meshes.map(m=><label className="template-scope" key={m.id}><input type="checkbox" checked={scope.includes(m.id)} onChange={e=>setScope(old=>e.target.checked?[...old,m.id]:old.filter(x=>x!==m.id))}/>{m.name}</label>)}<button className="button" disabled={disabled||!scope.length||!name.trim()} onClick={()=>action(async()=>{await api(`/sessions/${session.id}/templates/${templateId}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,part_number:partNumber,scope_ids:scope})});await onRefresh();})}>기준 정보·범위 저장</button>
      <h3>명시적인 수동 대응</h3><p className="small-note">자동 매칭이 모호하면 해당 공급 부품에 속하는 현재 단위를 직접 선택합니다. 형상은 대체하지 않습니다.</p><div className="unit-member-list">{session.units.map(u=><label key={u.id}><input type="checkbox" checked={manualUnits.includes(u.id)} onChange={e=>setManualUnits(old=>e.target.checked?[...old,u.id]:old.filter(x=>x!==u.id))}/>{u.name} · {u.id.slice(-6)}</label>)}</div><button className="button" disabled={disabled||!manualUnits.length} onClick={()=>action(async()=>{const r=await post(`/sessions/${session.id}/matches/manual`,{template_id:templateId,unit_ids:manualUnits,expected_revision:session.unit_state.revision});onJob(r.job_id);})}>선택 단위를 이 기준 부품으로 대응</button></>}
      <p className="small-note">겹치는 후보는 동시에 적용할 수 없습니다. 적용 후 전체 조립 단위를 다시 확정하고 전처리합니다.</p>
    </aside></div><footer className="modal-footer"><button className="button" disabled={disabled} onClick={onClose}>닫기</button></footer>
  </section></div>;
}

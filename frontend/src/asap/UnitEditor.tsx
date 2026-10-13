import { useEffect, useMemo, useState } from 'react';
import { X } from 'lucide-react';
import AssemblyViewer from './AssemblyViewer';
import DiagnosticPanel from './DiagnosticPanel';
import { diagnosticRows } from './diagnostics';
import { api, post } from './api';
import type { DisplayScene, Json, Session } from './types';

export default function UnitEditor({ session, scene, locked, onJob, onRefresh, onClose }: {
  session: Session; scene: DisplayScene | null; locked: boolean;
  onJob: (id: string) => void; onRefresh: () => Promise<void>; onClose: () => void;
}) {
  const [selected, setSelected] = useState<string[]>([]), [query, setQuery] = useState('');
  const [name, setName] = useState(''), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [raw, setRaw] = useState<DisplayScene | null>(null), [sourceMode, setSourceMode] = useState(false);
  const [isolate, setIsolate] = useState(false), [target, setTarget] = useState(''), [members, setMembers] = useState<Json[]>([]);
  const [focusToken,setFocusToken]=useState(0), [issueOnly,setIssueOnly]=useState(false);
  const [actionTab,setActionTab]=useState<'edit'|'diagnostics'>('edit');
  const state = session.unit_state ?? {}, units = session.units, sources: Json[] = session.source_units ?? [];
  const sourceNames = useMemo(() => Object.fromEntries(sources.map(u => [u.id, u.name])), [sources]);
  const selectedUnits = units.filter(u => selected.includes(u.id));
  const selectedRefs = selectedUnits.flatMap(u => u.member_refs ?? []);
  const issueRows=useMemo(()=>diagnosticRows(session),[session]);
  const issueCounts=useMemo(()=>Object.fromEntries(units.map(u=>[u.id,issueRows.filter(r=>r.unitIds.includes(u.id)).length])),[units,issueRows]);
  const filtered = units.filter(u => `${u.name} ${u.id}`.toLowerCase().includes(query.toLowerCase()) && (!issueOnly||issueCounts[u.id]>0));
  const disabled = busy || locked;
  useEffect(() => { setSelected([]); setMembers([]); setName(''); setTarget(''); }, [state.revision]);
  useEffect(() => { let alive = true; api<DisplayScene>(`/sessions/${session.id}/scene?mode=source`).then(s => { if (alive) setRaw(s); }).catch(e => { if (alive) setError(String(e)); }); return () => { alive = false; }; }, [session.id]);
  function choose(id: string, additive = true) {
    setMembers([]);
    setSelected(previous => additive ? previous.includes(id) ? previous.filter(x => x !== id) : [...previous, id] : [id]);
  }
  async function edit(operation: string, extra: Json = {}) {
    setError(''); setBusy(true);
    try {
      const result = await api<Json>(`/sessions/${session.id}/units`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_revision: state.revision, operation, unit_ids: selected, name: name || null, ...extra }) });
      onJob(result.job_id);
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  async function confirm() {
    setError(''); setBusy(true);
    try { await post(`/sessions/${session.id}/units/confirm`, { expected_revision: state.revision }); await onRefresh(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  const highlighted = sourceMode ? [...new Set(selectedRefs.map(m => m.occurrence_id))] : selected;
  return <div className="modal-backdrop"><section className="unit-editor" role="dialog" aria-modal="true" aria-label="조립 단위 편집">
    <header className="modal-header"><div><h2>조립 단위 편집 · v0.2</h2><p>함께 설치하는 구성원을 묶으세요. 같은 이름의 반복 볼트는 각각 유지합니다.</p></div><button className="icon-button" disabled={disabled} onClick={onClose} aria-label="조립 단위 편집 닫기"><X size={20} /></button></header>
    <div className="unit-summary"><span>원본 CAD {sources.length}</span><span>솔리드 {state.coverage?.source_solid_count ?? '—'}</span><span>조립 단위 {units.length}</span><span>미배정 {state.coverage?.unassigned_count ?? '—'} · 중복 {state.coverage?.duplicate_count ?? '—'}</span><strong>{state.confirmed ? '구성 확정됨' : '검토·확정 필요'}</strong></div>
    {error && <div role="alert" className="unit-error">{error}</div>}
    <div className="unit-editor-body">
      <aside className="unit-editor-list"><input aria-label="조립 단위 검색" placeholder="이름 / ID 검색" value={query} onChange={e => setQuery(e.target.value)} />
        <div className="unit-list-actions"><button disabled={disabled} onClick={() => setSelected(filtered.map(u => u.id))}>검색 결과 선택</button><button onClick={() => { setSelected([]); setMembers([]); }}>선택 해제</button></div>
        <label><input type="checkbox" checked={issueOnly} onChange={e=>setIssueOnly(e.target.checked)}/>진단이 있는 단위만</label>
        <div className="unit-scroll">{filtered.map(u => <label key={u.id} className={`unit-row ${selected.includes(u.id) ? 'selected' : ''}`}><input type="checkbox" disabled={disabled} checked={selected.includes(u.id)} onChange={() => choose(u.id)} /><span><strong>{u.name}</strong><small>{u.id} · CAD {u.member_refs?.length ?? 1} · 솔리드 {u.solid_count}</small>{issueCounts[u.id]>0 && <button className="unit-issue-badge" type="button" onClick={e=>{e.preventDefault();e.stopPropagation();setSelected([u.id]);setMembers([]);setActionTab('diagnostics');}}>진단 {issueCounts[u.id]}건 보기</button>}</span></label>)}</div>
      </aside>
      <div className="unit-editor-view"><div className="unit-view-controls"><button className={sourceMode ? '' : 'selected'} onClick={() => setSourceMode(false)}>조립 단위 보기</button><button className={sourceMode ? 'selected' : ''} onClick={() => setSourceMode(true)}>원본 CAD 보기</button><label><input type="checkbox" checked={isolate} onChange={e => setIsolate(e.target.checked)} />선택만 보기</label><span>Ctrl+클릭 다중 선택</span></div>
        <AssemblyViewer cad={sourceMode ? raw : scene} obj={null} mode="cad" selected={null} selectedIds={highlighted} focusToken={focusToken} isolate={isolate} showFuture={false} showPath={false} boundary={null} progress={0}
          onSelect={(id, additive) => { const uid = sourceMode ? units.find(u => u.member_refs?.some((m: Json) => m.occurrence_id === id))?.id : id; if (uid) choose(uid, !!additive); }} />
      </div>
      <aside className="unit-editor-actions"><div className="unit-list-actions"><button onClick={()=>setActionTab('edit')}>단위 편집</button><button onClick={()=>setActionTab('diagnostics')}>검사 내역</button></div><h3>선택 {selected.length}개</h3>{actionTab==='edit' ? <><label className="field full"><span>묶음 / 변경 이름</span><input aria-label="조립 단위 이름" value={name} onChange={e => setName(e.target.value)} maxLength={200} placeholder="예: 버스바 블록" /></label>
        <button className="button primary" disabled={disabled || selected.length < 2} onClick={() => edit('merge')}>선택 단위 묶기</button>
        <button className="button" disabled={disabled || !selected.length} onClick={() => edit('ungroup')}>묶음 해제</button>
        <button className="button" disabled={disabled || selected.length !== 1 || !name.trim()} onClick={() => edit('rename')}>선택 단위 이름 변경</button>
        {selectedUnits.some(u=>u.template_id) && <button className="button" disabled={disabled} onClick={()=>edit('clear_template')}>기준 부품 연결 해제</button>}
        <button className="button" disabled={disabled || selected.length !== 1 || selectedUnits[0]?.solid_count < 2} onClick={() => edit('split')}>독립 솔리드로 분할</button>
        <p className="small-note">묶기 전 선택 형상을 확인하세요. 접촉·겹침만으로 자동 병합하지 않습니다. 내부 형상은 모두 유지됩니다.</p>
        <h3>원본 구성원</h3><div className="unit-member-list">{selectedRefs.map((m, i) => <label key={JSON.stringify(m)}><input type="checkbox" checked={members.some(x => JSON.stringify(x) === JSON.stringify(m))} onChange={e => setMembers(old => e.target.checked ? [...old, m] : old.filter(x => JSON.stringify(x) !== JSON.stringify(m)))} /><span>{sourceNames[m.occurrence_id]}{m.solid_indices ? ` / 솔리드 ${m.solid_indices.map((n: number) => n+1).join(',')}` : ''}<small>{m.occurrence_id}</small></span></label>)}</div>
        <select aria-label="구성원 이동 대상" value={target} onChange={e => setTarget(e.target.value)}><option value="">이동 대상 단위</option>{units.map(u => <option key={u.id} value={u.id}>{u.name} · {u.id.slice(-6)}</option>)}</select>
        <button className="button" disabled={disabled || !members.length || !target} onClick={() => edit('reassign', { member_refs: members, target_unit_id: target })}>구성원 이동</button>
        <button className="button" disabled={disabled || !members.length} onClick={() => edit('detach', { member_refs: members })}>구성원을 별도 단위로 분리</button>
        </> : <DiagnosticPanel selectedOnlyByDefault session={session} selectedIds={selected} onLocate={ids=>{setSelected(ids);setSourceMode(false);setIsolate(true);setMembers([]);setFocusToken(n=>n+1);}} /> }
      </aside>
    </div>
    <footer className="modal-footer"><button className="button" disabled={disabled || !state.undo?.length} onClick={() => edit('undo', { unit_ids: [] })}>되돌리기</button><button className="button" disabled={disabled || !state.redo?.length} onClick={() => edit('redo', { unit_ids: [] })}>다시 실행</button><span className="small-note">편집은 저장됩니다. 변경 후에는 확정과 OBJ 변환이 필요합니다.</span><button className="button primary" disabled={disabled || state.confirmed} onClick={confirm}>전체 조립 단위 확정</button><button className="button" disabled={disabled} onClick={onClose}>닫기</button></footer>
  </section></div>;
}

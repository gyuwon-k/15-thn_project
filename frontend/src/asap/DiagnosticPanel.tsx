import { useMemo, useState } from 'react';
import { diagnosticRows, describeCode } from './diagnostics';
import type { Session } from './types';
export default function DiagnosticPanel({session,onLocate,selectedIds=[],selectedOnlyByDefault=false}:{session:Session;onLocate:(ids:string[])=>void;selectedIds?:string[];selectedOnlyByDefault?:boolean}) {
  const [category,setCategory]=useState('전체'), [query,setQuery]=useState(''), [page,setPage]=useState(0);
  const rows=useMemo(()=>diagnosticRows(session),[session]);
  const quality=session.quality_report ?? session.conversion?.quality ?? {}, p=quality.initial_penetration ?? {};
  const [onlySelected,setOnlySelected]=useState(selectedOnlyByDefault);
  const filtered=rows.filter(r => (category==='전체'||r.category===category) && (!onlySelected||!selectedIds.length||r.unitIds.some(id=>selectedIds.includes(id))) && `${r.title} ${r.code} ${r.names.join(' ')} ${JSON.stringify(r.raw)}`.toLowerCase().includes(query.toLowerCase()));
  const shown=filtered.slice(page*30,page*30+30), maxPage=Math.max(0,Math.ceil(filtered.length/30)-1);
  return <div className="diagnostic-panel"><h3>문제 부품·검사 내역</h3>
    <div className="diagnostic-summary"><span>체적 겹침 {p.penetration_pairs?.length ?? 0}쌍</span><span>미검사 {Object.values(p.unchecked_pair_count_by_reason ?? {}).reduce((a:number,b:any)=>a+Number(b),0) as number}쌍</span><span>계산 실패 {(p.pairs??[]).filter((x:any)=>x.status==='calculation_failed').length}쌍</span><span>OCCT 경고 {session.transfer_report ? session.transfer_report.entry_count-session.transfer_report.failure_count : '기록 없음'}건</span></div>
    {p.unchecked_pair_count_by_reason && <details><summary>미검사 이유별 수량</summary>{Object.entries(p.unchecked_pair_count_by_reason).map(([code,count])=><p key={code}>{describeCode(code)[0]}: {String(count)}쌍</p>)}</details>}
    <select aria-label="진단 분류" value={category} onChange={e=>{setCategory(e.target.value);setPage(0);}}>{['전체','간섭','미검사','계산 실패','변환','OCCT','엔진','이전 실행','안내'].map(c=><option key={c}>{c}</option>)}</select>
    <input aria-label="진단 검색" placeholder="품명 / 코드 / 원문 검색" value={query} onChange={e=>{setQuery(e.target.value);setPage(0);}} />
    {selectedIds.length>0 && <label><input type="checkbox" checked={onlySelected} onChange={e=>{setOnlySelected(e.target.checked);setPage(0);}}/>선택한 단위의 진단만</label>}
    <p className="small-note">{filtered.length}개 내역. 미검사는 간섭 없음이나 계산 실패로 처리하지 않습니다. OCCT 엔티티 경고는 특정 부품을 확인할 근거가 없는 경우 전체 파일 경고로 표시합니다.</p>
    {shown.map(r=><article className={`diagnostic-record diagnostic-${r.category==='간섭'||r.category==='계산 실패'||r.category==='엔진'?'issue':'note'}`} key={r.key}><strong>{r.title}</strong><small>{r.category} · {r.code}</small><p>{r.message}</p>{r.names.length>0 && <p className="diagnostic-part-names">{r.names.join(' ↔ ')}</p>}
      {r.raw.common_volume_mm3 != null && <p>공통 체적: {Number(r.raw.common_volume_mm3).toLocaleString('ko-KR',{maximumFractionDigits:6})} mm³ · 판정 기준: {r.raw.volume_tolerance_mm3} mm³</p>}
      {r.code==='ZERO_AREA_TRIANGLES_REMOVED' && <p>제거 삼각형: {r.raw.preprocessing?.faces_removed ?? '기록 없음'}개 · 구멍 채움: {r.raw.preprocessing?.holes_filled ?? '기록 없음'}개</p>}
      {r.code==='CALCULATION_FAILED' && <p>실패 원문: {r.raw.reason}</p>}
      {r.category==='OCCT' && <p>원문: {r.raw.message}<br/>STEP 엔티티: {(r.raw.entity_numbers??[]).slice(0,5).join(', ')}{r.raw.count>5?' 외 '+(r.raw.count-5)+'건':''}</p>}
      {r.category==='변환' && r.raw.reason && <p>실패 원문: {r.raw.reason}</p>}
      {['엔진','이전 실행'].includes(r.category) && <><p>실행 오류: {r.raw.message}</p>{r.raw.last_part_event && <p>마지막 부품 로그: {r.raw.last_part_name ?? r.raw.last_part_event.part} · {r.raw.last_part_event.stage} · {r.raw.last_part_event.message}<br/>이 부품이 원인이라는 확정 판정은 아닙니다.</p>}</>}
      {r.unitIds.length>0 ? <button className="button" onClick={()=>onLocate(r.unitIds)}>{r.unitIds.length>1?'부품 쌍 3D 확인':'해당 단위 3D 확인'}</button> : <p className="small-note">특정 부품 연결 정보 없음 / 전체 입력에 대한 안내</p>}
      {r.unitIds.length===2 && <div className="diagnostic-pages"><button onClick={()=>onLocate([r.unitIds[0]])}>A만 보기</button><button onClick={()=>onLocate([r.unitIds[1]])}>B만 보기</button></div>}
      <details><summary>수치·설정·원문 자세히</summary><pre className="json-preview">{JSON.stringify(r.raw,null,2)}</pre></details>
    </article>)}
    {!shown.length && <p className="small-note">현재 조건의 진단이 없습니다. 미검사 영역까지 정상이라는 뜻은 아닙니다.</p>}
    {maxPage>0 && <div className="diagnostic-pages"><button disabled={page===0} onClick={()=>setPage(n=>n-1)}>이전</button><span>{page+1} / {maxPage+1}</span><button disabled={page>=maxPage} onClick={()=>setPage(n=>n+1)}>다음</button></div>}
  </div>;
}

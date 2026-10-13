export type Row = Record<string, any>;
export async function api(path:string, options:RequestInit = {}) {
 const headers:HeadersInit = options.body instanceof FormData ? {} : {'Content-Type':'application/json'};
 const r = await fetch('/api'+path,{...options,headers:{...headers,...options.headers}});
 const text = await r.text(); let body:any;
 try {body = text ? JSON.parse(text) : null;} catch {body = {detail:text};}
 if(!r.ok) throw new Error(typeof body?.detail==='string' ? body.detail : JSON.stringify(body?.detail || body?.error || body || `HTTP ${r.status}`));
 return body;
}
export const post = (path:string, body:any={}) => api(path,{method:'POST',body:JSON.stringify(body)});
export const patch = (path:string, body:any) => api(path,{method:'PATCH',body:JSON.stringify(body)});
export function rows(value:any):Row[]{return Array.isArray(value)?value:[];}
export function fmtDate(date:string){return date ? new Date(date).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';}
export function statusLabel(s:string) {return ({partial:'일부 형상 오류',candidate:'대응 후보',passed:'검사 범위 내 통과',contact:'접촉',collision:'간섭',timeout:'시간 초과',created:'생성됨',ready:'준비 완료',draft:'편집 중',confirmed:'확정됨',validating:'검증 중',validated:'검사 범위 내 통과',validated_with_exceptions:'예외 포함 검증',invalid:'수정 필요',review_required:'검토 필요',mapping_required:'대응 검토 필요',importing:'CAD 해석 중',import_failed:'입력 실패',pending:'대기 중',unreviewed:'미검토',reviewed:'검토 완료',succeeded:'완료',failed:'실패',running:'진행 중',queued:'대기 중',canceled:'취소됨',no_history:'확정 이력 없음',insufficient_labels:'학습 라벨 부족',available:'사용 가능',training:'학습 중',training_failed:'학습 실패',pass:'통과',warning:'검토',error:'오류',not_published:'게시 전',published:'게시됨',publish_failed:'게시 실패'} as Row)[s] || s || '미입력';}

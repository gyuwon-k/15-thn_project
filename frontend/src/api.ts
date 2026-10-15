export type Row = Record<string, any>;
declare const __PILOT_VERSION__: string;
declare const __PILOT_BUILD_ID__: string;
export const UI_VERSION = __PILOT_VERSION__;
export const UI_BUILD_ID = __PILOT_BUILD_ID__;
export async function api(path:string,options:RequestInit={}) {
  const supplied=new URLSearchParams(location.search).get('token');
  if(supplied)sessionStorage.setItem('pilot-session',supplied);
  const token=(window as any).pilot?.sessionToken || supplied || sessionStorage.getItem('pilot-session');
  const headers:Record<string,string>={};
  if(!(options.body instanceof FormData)) headers['Content-Type']='application/json';
  if(token) headers['X-Pilot-Token']=token;
  const response=await fetch('/api'+path,{...options,headers:{...headers,...options.headers}});
  const text=await response.text();let body:any;
  try{body=text?JSON.parse(text):null;}catch{body={detail:text};}
  if(!response.ok){
    const detail=body?.detail||body?.error||body;
    throw new Error(typeof detail==='string'?detail:detail?.message||JSON.stringify(detail)||`HTTP ${response.status}`);
  }
  return body;
}
export const post=(path:string,body:unknown={})=>api(path,{method:'POST',body:JSON.stringify(body)});
export const patch=(path:string,body:unknown)=>api(path,{method:'PATCH',body:JSON.stringify(body)});
export function rows(value:any):Row[]{return Array.isArray(value)?value:Array.isArray(value?.items)?value.items:[];}
export function fmtDate(value?:string){return value?new Date(value).toLocaleString('ko-KR',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';}
export function statusLabel(value?:string){const key=(value||'pending').toLowerCase();return ({interrupted:'작업 중단',cancel_requested:'취소 처리 중',limited:'일부 제한',needs_validation:'재검증 대기',empty:'순서 대기',model_ready:'모델 준비됨',collecting:'데이터 수집 중',cad_leaf:'CAD 배치 사용',matched:'파일 대응됨',reviewed:'검토됨',manual:'수동 대응',info:'정보',pass:'범위 내 통과',passed:'범위 내 통과',validated:'범위 내 통과',validated_recommendation:'검증된 추천',unknown:'미판정',unverified:'미검증',unsupported:'미지원',collision:'간섭 발견',fail:'간섭 / 수정 필요',failed:'실패',blocked:'진행 조건 확인',draft:'검토용 초안',changed:'재검증 대기',stale:'재검증 대기',confirmed:'확정본',ready:'준비 완료',analysis_ready:'분석 준비 완료',warning:'확인 필요',partial:'일부 제한',review_required:'검토 필요',pending:'미실행',queued:'대기 중',running:'진행 중',success:'완료',succeeded:'완료',completed:'완료',cancelled:'취소됨',canceled:'취소됨',error:'오류',not_found:'통과 순서 미발견',budget_exhausted:'탐색 시간 도달',insufficient_data:'학습 데이터 부족',not_configured:'설정 필요',connected:'연결됨',disconnected:'연결되지 않음'} as Row)[key]||value||'미실행';}
export function statusTone(value?:string){const key=(value||'').toLowerCase();if(['pass','passed','validated','validated_recommendation','confirmed','success','succeeded','completed','ready','analysis_ready','connected'].includes(key))return 'success';if(['fail','failed','collision','error','blocked','disconnected'].includes(key))return 'danger';if(['limited','needs_validation','unknown','warning','partial','review_required','stale','changed','unverified','unsupported','not_found','budget_exhausted'].includes(key))return 'warning';return 'neutral';}
export function unitMembers(unit?:Row):string[]{return unit?.occurrence_ids||unit?.member_occurrence_ids||unit?.member_instance_ids||[];}
export function stepUnit(step?:Row){return step?.unit_id||step?.target_unit_ids?.[0]||'';}
export function stepName(step?:Row){return step?.name||step?.title||'이름 없는 단계';}
export function diagnosticText(value:any):string {if(typeof value==='string')return value;return value?.message||value?.reason||value?.detail||value?.code||JSON.stringify(value);}
export function diagnosticMessage(value:any):string{
 const code=typeof value==='string'?value:value?.code||value?.reason;
 const labels:Row={FINAL_PLACEMENT_REQUIRES_REVIEW:'최종 배치에 체적 겹침 또는 미판정 부품 쌍이 있습니다. 순서 변경만으로 최종 결합 상태가 해결되지는 않습니다.',GEOMETRY_EXACT_METHOD_REVIEW_REQUIRED:'형상 검사 방식 간 결과가 다릅니다. 원본 대비 보존 결과와 사용 범위를 검토해 주세요.',DRAFT_REQUIRES_PATH_VALIDATION:'전체 순서 초안을 생성했습니다. 미검증 단계의 진입 조건을 확인하고 재검증해야 확정할 수 있습니다.',REVIEW_DRAFT_PATH_NOT_VERIFIED:'검토용 초안의 진입 경로입니다. 현재 경로의 간섭 여부는 아직 통과하지 않았습니다.',CONTACT_INTERVAL_UNRESOLVED:'접촉 구간의 간섭 여부를 판정하지 못했습니다. 접촉 영역이나 형상 상태를 확인하세요.',PRECEDENCE_VIOLATION:'필수 선행 작업이 먼저 설치되지 않았습니다.',INVALID_PLAN_COVERAGE:'조립 단위가 누락되거나 중복되었습니다. 전체 작업 범위를 확인해 주세요.',PATH_REBUILT_FROM_CURRENT_ENTRY:'변경한 진입 방향·거리로 경로를 다시 구성했습니다.',FINAL_PLACEMENT_COLLISION:'최종 위치에서 부품 간 체적 겹침이 발견되었습니다.',NATIVE_TIMEOUT:'형상 연산의 제한 시간이 초과되어 미판정으로 남겼습니다.'};
 return labels[code]||diagnosticText(value);
}

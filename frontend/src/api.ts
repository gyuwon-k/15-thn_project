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
export function statusLabel(value?:string){const key=(value||'pending').toLowerCase();return ({interrupted:'작업 중단',cancel_requested:'취소 처리 중',limited:'일부 제한',needs_validation:'재검증 대기',empty:'순서 대기',model_ready:'모델 준비됨',collecting:'데이터 수집 중',cad_leaf:'CAD 배치 사용',matched:'파일 대응됨',reviewed:'검토됨',manual:'수동 대응',info:'정보',pass:'범위 내 통과',passed:'범위 내 통과',validated:'범위 내 통과',validated_recommendation:'검증된 추천',unknown:'미판정',unverified:'미검증',unsupported:'미지원',collision:'간섭 발견',fail:'간섭 / 수정 필요',failed:'실패',blocked:'진행 조건 확인',draft:'검토용 초안',changed:'재검증 대기',stale:'재검증 대기',confirmed:'확정본',ready:'경로 검증 통과',unplanned:'경로 미계획',waiting_predecessor:'선행 단계 대기',analysis_ready:'분석 준비 완료',warning:'확인 필요',partial:'일부 제한',review_required:'검토 필요',pending:'미실행',queued:'대기 중',running:'진행 중',success:'완료',succeeded:'완료',completed:'완료',cancelled:'취소됨',canceled:'취소됨',error:'오류',not_found:'통과 순서 미발견',budget_exhausted:'탐색 시간 도달',insufficient_data:'학습 데이터 부족',not_configured:'설정 필요',connected:'연결됨',disconnected:'연결되지 않음'} as Row)[key]||value||'미실행';}
export function statusTone(value?:string){const key=(value||'').toLowerCase();if(['pass','passed','validated','validated_recommendation','confirmed','success','succeeded','completed','ready','analysis_ready','connected'].includes(key))return 'success';if(['fail','failed','collision','error','blocked','disconnected'].includes(key))return 'danger';if(['limited','needs_validation','unknown','warning','partial','review_required','stale','changed','unverified','unsupported','not_found','budget_exhausted'].includes(key))return 'warning';return 'neutral';}
export function unitMembers(unit?:Row):string[]{return unit?.occurrence_ids||unit?.member_occurrence_ids||unit?.member_instance_ids||[];}
export function stepUnit(step?:Row){return step?.unit_id||step?.target_unit_ids?.[0]||'';}
export function stepName(step?:Row){return step?.name||step?.title||'이름 없는 단계';}
export function diagnosticText(value:any):string {if(typeof value==='string')return value;return value?.message||value?.reason||value?.detail||value?.code||JSON.stringify(value);}
export function diagnosticMessage(value:any):string{
 const code=typeof value==='string'?value:value?.code||value?.reason;
 const labels:Row={PROCESS_FIXING_REQUIRED:'먼저 배치한 부품의 고정 체결을 완료해야 다음 부품을 배치할 수 있습니다.',PROCESS_COMPONENT_NOT_PLACED:'체결할 대상 부품이 아직 배치되지 않았습니다.',PROCESS_FASTENER_ASSOCIATION_REVIEW_REQUIRED:'체결품의 고정 대상과 체결 순서 확인이 필요합니다.',PROCESS_FASTENER_ASSOCIATION_MISSING:'필수 부품의 고정 방법이 등록되지 않았습니다.',INVALID_REPRESENTATION_UNCERTAINTY:'형상 오차의 상한을 확인하지 못해 간섭 판정을 보류했습니다.',FINAL_PENETRATION:'최종 조립 위치에서 체적 겹침이 발생합니다.',NO_VERIFIED_ENTRY_PATH:'허용 방향에서 검증된 진입 경로를 아직 찾지 못했습니다.',ALL_ALLOWED_ENTRIES_COLLIDE:'검사한 모든 허용 진입 방향에서 간섭이 발생했습니다.',FINAL_PLACEMENT_OVERLAP:'최종 조립 위치에 체적 겹침이 있어 결합 조건을 확인해야 합니다.',TRANSIT_PENETRATION:'이동 중 다른 부품을 관통합니다. 진입 방향이나 선후 순서를 변경해야 합니다.',REPRESENTATION_CLEARANCE_UNRESOLVED:'부품 사이 간격이 CAD 표현 오차보다 작아 무간섭을 확정하지 못했습니다.',TIME_BUDGET:'전체 검사 제한 시간에 도달했습니다.',MODEL_MAPPING_REVIEW_REQUIRED:'부품의 실제 형상 대응을 확인해야 합니다.',TEMPLATE_MEMBER_SET_NOT_PROVEN:'선택한 부품 파일과 구성 배치 전체의 형상 일치를 확인하지 못했습니다.',MANUAL_MAPPING_CHANGED:'부품 대응이 변경되어 실제 형상 재검증이 필요합니다.',EDIT_REQUIRES_VALIDATION:'순서 또는 진입 조건이 변경되었습니다. 재검증 후 경로를 재생할 수 있습니다.',WAITING_PREDECESSOR:'앞 단계의 경로가 확정되지 않아 이 단계의 재생을 보류했습니다.',FINAL_PLACEMENT_REQUIRES_REVIEW:'최종 배치에 체적 겹침 또는 미판정 부품 쌍이 있습니다. 순서 변경만으로 최종 결합 상태가 해결되지는 않습니다.',GEOMETRY_EXACT_METHOD_REVIEW_REQUIRED:'형상 자동 보정·검사가 완료되지 않았습니다. 보류 원인과 원본 형상을 확인해 주세요.',DRAFT_REQUIRES_PATH_VALIDATION:'전체 순서 초안을 생성했습니다. 미검증 단계의 진입 조건을 확인하고 재검증해야 확정할 수 있습니다.',REVIEW_DRAFT_PATH_NOT_VERIFIED:'이 단계의 진입 경로가 아직 검증되지 않아 재생을 보류했습니다.',CONTACT_INTERVAL_UNRESOLVED:'접촉 구간의 간섭 여부를 판정하지 못했습니다. 접촉 영역이나 형상 상태를 확인하세요.',PRECEDENCE_VIOLATION:'필수 선행 작업이 먼저 설치되지 않았습니다.',INVALID_PLAN_COVERAGE:'조립 단위가 누락되거나 중복되었습니다. 전체 작업 범위를 확인해 주세요.',PATH_REBUILT_FROM_CURRENT_ENTRY:'변경한 진입 방향·거리로 경로를 다시 구성했습니다.',FINAL_PLACEMENT_COLLISION:'최종 위치에서 부품 간 체적 겹침이 발견되었습니다.',NATIVE_TIMEOUT:'형상 연산의 제한 시간이 초과되어 미판정으로 남겼습니다.'};
 const additional:Row={REMOVAL_FIXING_DEPENDENTS:'이 고정 볼트를 풀기 전에 해당 부품에 지지·결합된 부품부터 제거해야 합니다.',UNSUPPORTED_COMPONENT:'이 단계에서 부품을 받쳐 주거나 고정할 대상이 없습니다. 지지 부품을 먼저 조립해야 합니다.',REMOVAL_LEAVES_UNSUPPORTED_COMPONENTS:'이 부품을 먼저 분해하면 남은 부품의 지지·고정이 사라집니다.',SUPPORT_GEOMETRY_UNRESOLVED:'부품의 실제 지지 접촉을 판정하지 못했습니다. 형상 검사 기록을 확인하세요.',INVALID_SUPPORT_CONFIGURATION:'지지·고정 조건이 올바르지 않습니다.',SUPPORT_BUDGET_REACHED:'지지·고정 검사 시간이 부족하여 미판정으로 남았습니다.',ASSEMBLED_REFERENCE_CUT_FAILED:'완제품 기준 접촉 영역을 계산하지 못했습니다. 이 이동은 통과 처리하지 않았습니다.',SWEPT_VOLUME_PENETRATION:'이동 경로에서 부품이 차지하는 공간이 이미 배치된 부품과 겹칩니다. 진입 방향과 조립 순서를 확인하세요.',START_NOT_OUTSIDE_PRODUCT:'지정 거리가 짧아 시작 위치가 제품 밖에 도달하지 못했습니다. 검사 기록의 필요 거리를 확인하세요.',DISASSEMBLY_INCOMPLETE:'전체 분해 경로를 찾지 못했습니다. 기존 조립 계획은 보존되며, 확인한 부분 분해 기록은 진단으로만 제공됩니다.',DIRECTION_CONSTRAINT_CONFLICT:'단위별 허용 방향과 프로젝트 방향 또는 선택 단계 방향이 일치하지 않습니다.',PREFERRED_DIRECTION_NOT_ALLOWED:'선호 방향은 허용 방향 중에서 지정해야 합니다.',INVALID_SEARCH_SETTING:'탐색 설정이 허용 범위를 벗어났습니다.',NOMINAL_CAD_INTERFERENCE_SCOPE:'CAD에 기록된 표현 정밀도에서 전체 이동 중 체적 간섭을 검사합니다. 제조 여유는 별도 확인이 필요합니다.',INVALID_ANALYSIS_REPRESENTATION:'형상의 정밀 검사 조건을 충족하지 못해 간섭 판정을 보류했습니다.',MATCH_CANDIDATE_SEARCH_INCOMPLETE:'후보 탐색 시간이 부족해 대응의 유일성을 확인하지 못했습니다.',PROCESS_UNASSIGNED_FASTENERS:'고정 대상이 지정되지 않은 체결품이 있습니다.',PART_IMPORT_FAILED:'부품 STEP을 정상적으로 불러오지 못했습니다. 원본 파일과 변환 진단을 확인해 주세요.'};
 const refresh:Row={ANALYSIS_REFRESH_SOURCE_MODIFIED:'보관된 원본 형상이 변경되어 기존 배치·매칭을 사용할 수 없습니다. STEP을 다시 등록해 주세요.',ANALYSIS_SOURCE_HASH_MISMATCH:'보관된 원본 형상이 변경되어 기존 배치·매칭을 사용할 수 없습니다. STEP을 다시 등록해 주세요.',ANALYSIS_REFRESH_BUDGET_REACHED:'형상 재평가 후 사용할 분석 시간이 부족합니다. 다시 실행하거나 분석 시간을 늘려 주세요.'};
 return labels[code]||additional[code]||refresh[code]||diagnosticText(value);
}

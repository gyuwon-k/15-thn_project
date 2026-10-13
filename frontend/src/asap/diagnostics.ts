import type { Json, Session } from './types';
export interface DiagnosticRow { key: string; category: string; code: string; title: string; message: string; unitIds: string[]; names: string[]; raw: Json; }
const explanations: Record<string, [string, string]> = {
  ZERO_AREA_TRIANGLES_REMOVED: ['면적이 없는 삼각형 정리', '변환 과정에서 면적이 0인 삼각형을 제거한 처리 내역입니다. 이 경고 자체가 검사 실패나 실물 부품 삭제를 뜻하지 않습니다.'],
  INVALID_BREP: ['원본 CAD 형상 유효성 확인 필요', 'OCCT의 형상 구조 검사에서 유효하지 않은 것으로 판정했습니다. 실제 간섭 여부와 다른 진단이며 분석용 형상 보정 결과도 확인해야 합니다.'],
  NON_SOLID_GEOMETRY: ['닫힌 솔리드 없음', '닫힌 체적을 구성하지 않는 형상이므로 체적·점유 검사가 제한됩니다.'],
  ANALYSIS_MESH_FAILED: ['분석 메시 변환 실패', '테셀레이션 또는 메시 준비 과정이 완료되지 않았습니다. 아래 원문과 실패한 작업 단계를 확인하세요.'],
  MESH_NOT_WATERTIGHT: ['메시의 경계가 닫히지 않음', '열린 경계가 있어 체적 내부/외부 판정을 신뢰하기 어렵습니다.'],
  MESH_ORIENTATION_INVALID: ['메시 면 방향 불일치', '면의 안쪽/바깥쪽 방향이 일관되지 않습니다.'],
  MESH_VOLUME_APPROXIMATION: ['메시 체적 근사 오차', '원본 CAD 체적과 삼각 메시 체적 사이에 근사 오차가 있습니다. 체적 차이·허용 기준·최종 메시 준비 상태를 확인하세요.'],
  INITIAL_PENETRATION: ['완제품 상태에서 체적 겹침 확인', '정밀검사에서 두 조립 단위의 공통 체적을 검출했습니다. 압축되는 가스켓·삽입물 등의 설계 의도인지 별도 판단해야 합니다.'],
  INITIAL_PENETRATION_PARTIAL: ['초기 침투 검사 일부 미확인', '미검사와 계산 실패를 포함한 집계입니다. 간섭이 있다는 판정도, 간섭이 없다는 판정도 아닙니다.'],
  RIGID_APPROXIMATION: ['강체 근사의 범위 안내', '각 조립 단위를 변형되지 않는 강체로 취급합니다. 와이어 변형·가스켓 압축·체결력·공구 접근은 검사하지 않습니다. 실패 경고가 아닌 분석 범위 안내입니다.'],
  ORIGINAL_SOLID_OR_SDF_REQUIRED: ['원본 솔리드 합집합 충돌 표현 사용', '원본 내부 솔리드를 함께 보존하고 점유 체적의 합집합으로 처리해야 하는 단위입니다. 입력 준비와 엔진의 실제 SDF 생성 성공은 별도입니다.'],
  EXACT_BREP_REVIEW_REQUIRED: ['정밀 CAD 유효성 검토 필요', '분석용 형상이 일반 검사에는 통과했지만 정밀 검사에서는 미해결 상태입니다. 테스트용 경고 허용과 엄격 검증을 구분하세요.'],
  SDF_CACHE_DOMAIN_MISMATCH: ['충돌 격자 범위·크기 불일치', '격자 최대 좌표의 인덱스가 실제 배열 범위를 벗어나 네이티브 로딩 전에 차단했습니다. 캐시 재생성과 격자 설정을 확인하세요.'],
  UNIT_PREPROCESS_FAILED: ['일부 단위의 자동 전처리 미완료', '필수 부품을 제외하지 않고 해당 단위의 실패를 기록했습니다. 정상 단위의 결과는 보존되며 모든 단위가 준비된 뒤 순서를 탐색합니다.'],
  ADAPTIVE_COARSER_MESH: ['메시 세분화 정밀도 조정', '요청보다 큰 선형 오차의 테셀레이션 대안을 사용했습니다. 원본 대비 오차와 검증 결과를 확인하세요.'],
  ADAPTIVE_COARSER_ANGULAR_MESH: ['메시 각도 정밀도 조정', '변환을 완료하기 위해 요청보다 큰 각도 기준을 사용했습니다. 요청값과 실제 적용값은 상세 원문에 있습니다.'],
  VERIFIED_TOPOLOGY_REPAIR_SOURCE_RETAINED: ['분석용 형상 보정 적용', '원본은 유지하고 형상 보존 검증을 통과한 분석용 형상을 사용했습니다.'],
  STEP_TRANSFER_WARNINGS: ['OCCT STEP 변환 경고', 'STEP을 OCCT 형상으로 읽는 과정의 경고입니다. 메시 변환·간섭 검사·순서 탐색 실패와 구분해야 합니다.'],
  STEP_TRANSFER_FAILURE: ['OCCT STEP 엔티티 변환 실패', 'STEP을 읽는 과정에서 실패로 기록된 엔티티가 있습니다. 원문과 엔티티 번호를 확인하세요.'],
  INVALID_OR_NON_SOLID_BREP: ['형상 조건 때문에 검사하지 못함', '유효하지 않거나 닫힌 솔리드가 없는 형상이 포함되어 공통 체적 계산을 시도하지 않았습니다.'],
  FACE_COMPLEXITY_LIMIT: ['복잡도 제한으로 미검사', '면 수 또는 두 부품의 면 수 곱이 설정 한도를 넘어서 검사를 건너뛰었습니다. 계산 실패나 간섭 검출이 아닙니다.'],
  FACE_COMPLEXITY_UNKNOWN: ['면 복잡도 정보 부족으로 미검사', '신뢰할 수 있는 면 수를 확인할 수 없어 고비용 계산을 시작하지 않았습니다.'],
  MISSING_BOUNDS: ['경계 정보 부족으로 미검사', '한 부품의 경계 크기 정보가 없어 후보 쌍 검사를 수행하지 못했습니다.'],
  TIME_BUDGET_EXHAUSTED: ['시간 예산 종료로 미검사', '정밀검사에 할당한 전체 시간이 끝나 남은 후보 쌍은 계산하지 않았습니다.'],
  PAIR_BUDGET_EXHAUSTED: ['쌍 수 예산 종료로 미검사', '설정한 최대 정밀검사 쌍 수에 도달해 해당 쌍의 계산을 시작하지 않았습니다.'],
  CALCULATION_FAILED: ['공통 체적 계산 실패', 'OCCT BooleanCommon 계산을 시도했으나 완료하지 못했습니다. 아래 원문이 해당 계산의 실패 이유입니다.'],
  ENGINE_CRASH: ['네이티브 엔진 실행 오류', 'ASAP/RedMax 프로세스가 정상 결과를 완료하지 못하고 종료됐습니다. 간섭 검사 경고와 다른 실행 오류입니다. 마지막 로그 부품은 조사 대상이며 원인 부품으로 확정된 것은 아닙니다.'],
};
export function describeCode(code: string): [string, string] { return explanations[code] ?? [code, '상세 원문과 실제 적용값을 확인하세요. 이 항목만으로 원본 CAD 결함이나 간섭을 단정하지 않습니다.']; }
export function diagnosticRows(session: Session): DiagnosticRow[] {
  const quality = session.quality_report ?? session.conversion?.quality ?? {};
  const unitById = new Map(session.units.map(u => [u.id, u]));
  const owners = new Map<string, string[]>();
  for (const u of session.units) for (const m of u.member_refs ?? [{occurrence_id:u.id}]) owners.set(m.occurrence_id, [...(owners.get(m.occurrence_id) ?? []), u.id]);
  const resolve = (ids: string[]) => [...new Set(ids.flatMap(id => unitById.has(id) ? [id] : owners.get(id) ?? []))];
  const rows: DiagnosticRow[] = [], seen = new Set<string>();
  function add(category: string, code: string, raw: Json, ids: string[] = [], override?: string) {
    const key = `${category}:${code}:${ids.join(',')}:${raw.entity_number ?? (category==='OCCT' ? raw.message : '')}`;
    if (seen.has(key)) return; seen.add(key);
    const unitIds = resolve(ids), [title, explanation] = describeCode(code);
    rows.push({key, category, code, title, message: override ?? explanation, unitIds,
      names:unitIds.map(id => unitById.get(id)?.name ?? id), raw});
  }
  for (const d of session.manifest?.diagnostics ?? []) {
    if (d.code === 'MULTI_SOLID_UNIT' || d.code === 'STEP_NAME_ENCODING_RECOVERED' || d.code === 'STEP_TRANSFER_WARNINGS') continue;
    if (d.code === 'INVALID_BREP') {
      const related = resolve([d.unit_id]);
      const repaired = related.some(id => (quality.units ?? []).some((q:Json) => q.unit_id===id && q.analysis_brep_valid && q.topology_repair_audit?.status==='accepted'));
      add('변환',d.code,{...d,analysis_repaired:repaired},[d.unit_id], repaired ? '원본 CAD는 유효성 경고가 있지만 분석용 보정 형상이 검증을 통과했습니다. 원본과 분석용 상태를 구분하세요.' : undefined);
    } else add('변환',d.code,d,d.unit_id ? [d.unit_id] : []);
  }
  for (const u of quality.units ?? []) {
    for (const code of [...(u.errors ?? []), ...(u.warnings ?? [])]) add('변환',code,u,[u.unit_id]);
  }
  for (const d of quality.blocking ?? []) add('변환',d.code,d,d.unit_id ? [d.unit_id] : []);
  for (const d of quality.warnings ?? []) {
    if (['INITIAL_PENETRATION','INITIAL_PENETRATION_PARTIAL'].includes(d.code)) continue;
    add(d.code==='RIGID_APPROXIMATION' ? '안내':'변환',d.code,d,d.unit_id ? [d.unit_id] : []);
  }
  const p = quality.initial_penetration ?? {};
  for (const pair of p.penetration_pairs ?? []) add('간섭','INITIAL_PENETRATION',pair,[pair.a,pair.b]);
  for (const pair of p.unchecked_pairs ?? []) add('미검사',pair.reason_code ?? 'UNRESOLVED',pair,[pair.a,pair.b]);
  for (const pair of p.pairs ?? []) if (pair.status==='calculation_failed') add('계산 실패','CALCULATION_FAILED',pair,[pair.a,pair.b]);
  if (p.unchecked_pair_details_truncated) add('미검사','DETAILS_TRUNCATED',{count:p.unchecked_pair_details_truncated},[], `이 저장 결과에는 미검사 쌍 ${p.unchecked_pair_details_truncated}개의 상세 내역이 생략돼 있습니다. 기록이 없으므로 부품을 추정해 표시하지 않습니다.`);
  const transfer = session.transfer_report;
  if (transfer) {
    const groups = new Map<string, Json[]>();
    for (const e of transfer.entries ?? []) { const key = `${e.kind}:${e.message}`; groups.set(key,[...(groups.get(key)??[]),e]); }
    for (const entries of groups.values()) {
      const e=entries[0]; let translated = '원문 의미를 추가 확인해야 하는 STEP 변환 경고입니다.';
      if (/projection vertex.*curve/i.test(e.message)) translated='꼭짓점을 3차원 곡선에 투영한 결과의 정확도가 좋지 않다는 경고입니다. 곡선·꼭짓점의 일치 또는 허용 오차를 검토해야 하며, 부품 간 간섭 판정이 아닙니다.';
      if (/Edges were intersecting, corrected/i.test(e.message)) translated='STEP을 읽는 과정에서 교차하는 엣지 곡선을 OCCT가 보정했다는 경고입니다. 서로 다른 부품 사이의 간섭을 검출했다는 뜻은 아닙니다. 원본 STEP 파일은 수정하지 않습니다.';
      if (/Surface forced to be periodic/i.test(e.message)) translated='곡면을 주기적인 곡면으로 취급하도록 OCCT가 해석을 보정했다는 경고입니다. 해당 곡면의 형상과 허용 오차를 검토해야 하며, 부품 간 간섭 판정이 아닙니다.';
      add('OCCT',e.kind==='failure' ? 'STEP_TRANSFER_FAILURE':'STEP_TRANSFER_WARNINGS', {message:e.message,kind:e.kind,count:entries.length,entity_numbers:entries.map(x=>x.entity_number)},[],`${translated} ${entries.length}건. STEP 엔티티 번호와 조립 단위 사이의 매핑이 없어 특정 부품을 확정할 수 없습니다.`);
    }
  }
  const current = (session.runs ?? []).find((r:Json) => r.reason_code && r.reason_code!=='SUCCESS' && r.unit_revision===session.unit_state?.revision && r.input_revision===session.conversion?.revision);
  if (current) {
    const context=current.diagnostic_context ?? {};
    add('엔진', current.reason_code, {...context,message:current.message,run_id:current.run_id,reason_code:current.reason_code},context.unit_ids ?? []);
  }
  const previous=(session.runs??[]).find((r:Json)=>r.reason_code && r.reason_code!=='SUCCESS' &&
    (r.unit_revision!==session.unit_state?.revision || r.input_revision!==session.conversion?.revision));
  if (previous) add('이전 실행',previous.reason_code,{...previous.diagnostic_context,message:previous.message,run_id:previous.run_id,
      unit_revision:previous.unit_revision,input_revision:previous.input_revision,current_input:false},previous.unit_revision===session.unit_state?.revision ? previous.diagnostic_context?.unit_ids ?? [] : [],
    `이전 입력의 실행 오류입니다. 현재 변환의 실패로 합산하지 않습니다. ${describeCode(previous.reason_code)[1]}`);
  const priority:Record<string,number>={'엔진':0,'이전 실행':1,'계산 실패':1,'간섭':2,'변환':3,'미검사':4,'OCCT':5,'안내':6};
  return rows.sort((a,b)=>(priority[a.category]??9)-(priority[b.category]??9));
}

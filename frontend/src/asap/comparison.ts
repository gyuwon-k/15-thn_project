import type { Json, Run } from './types';
function stable(value: any): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}`;
  return JSON.stringify(value) ?? 'null';
}
export function comparisonDifferences(first: Run | null, second: Run | null): string[] {
  if (!first || !second) return [];
  const a = first.result ?? first, b = second.result ?? second;
  const pa = a.provenance ?? {}, pb = b.provenance ?? {};
  const sa = first.settings ?? a.settings ?? {}, sb = second.settings ?? b.settings ?? {};
  const differences: string[] = [];
  const revisionA = a.input_revision ?? pa.input_revision, revisionB = b.input_revision ?? pb.input_revision;
  const meshesA = pa.input_files_sha256, meshesB = pb.input_files_sha256;
  if (revisionA && revisionB && revisionA !== revisionB) differences.push('변환 리비전이 다릅니다. 메시 정밀도·형상 처리 방식이 같은 전처리인지 확인하세요.');
  if (meshesA && meshesB && stable(meshesA) !== stable(meshesB)) differences.push('분석 OBJ 파일의 형상 해시가 다릅니다.');
  if (!(revisionA && revisionB) && !(meshesA && meshesB)) differences.push('변환 리비전 또는 메시 해시 기록이 부족해 동일한 분석 입력인지 확인할 수 없습니다.');
  if (stable(first.coordinate_map ?? a.coordinate_map) !== stable(second.coordinate_map ?? b.coordinate_map)) differences.push('좌표·크기·물리 근사 프로파일이 다릅니다.');
  if (stable(sa.physics_profile ?? null) !== stable(sb.physics_profile ?? null)) differences.push('접촉·SDF·분리 거리의 물리 설정이 다릅니다.');
  const fields: [string, string][] = [['planner', '탐색기'], ['seed', 'Seed'], ['budget', '평가 예산'], ['timeout_s', '전체 제한시간'], ['base_part', '고정 기준 부품'], ['max_gripper', 'Max gripper'], ['max_pose', 'Pose 후보 수'], ['pose_reuse', 'Pose reuse']];
  for (const [key, label] of fields) {
    const va = sa[key] ?? null, vb = sb[key] ?? null;
    if (va !== vb) differences.push(`${label} 설정이 다릅니다 (${va ?? '미지정'} ↔ ${vb ?? '미지정'}).`);
  }
  for (const [key, label] of [['asap_commit', 'ASAP 코드 버전'], ['redmax_binary_sha256', 'RedMax 엔진 바이너리']] as const) {
    if (pa[key] && pb[key] && pa[key] !== pb[key]) differences.push(`${label}가 다릅니다.`);
  }
  if (sa.strategy === 'learn' && sb.strategy === 'learn' && pa.weights_sha256 !== pb.weights_sha256) differences.push('사전학습 가중치가 다르거나 가중치 해시 기록이 부족합니다.');
  return differences;
}

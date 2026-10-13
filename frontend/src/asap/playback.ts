import { Matrix4, Quaternion, Vector3 } from 'three';
import type { Boundary, Json, PlanStep, Run } from './types';
export const IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
export const VIEW_MATRIX = new Matrix4().makeRotationX(-Math.PI / 2);
const VIEW_INVERSE = VIEW_MATRIX.clone().invert();
export function fromRowMajor(values?: number[] | null): Matrix4 {
  if (!values || values.length !== 16 || values.some(v => !Number.isFinite(v))) return new Matrix4();
  return new Matrix4().fromArray(values).transpose();
}
export function forwardMatrix(map?: Json): number[] {
  return map?.forward_matrix ?? map?.forward ?? map?.cad_to_engine ?? map?.matrix ?? IDENTITY;
}
export function engineToDisplay(values: number[] | null, coordinateMap?: Json): Matrix4 {
  const c = fromRowMajor(forwardMatrix(coordinateMap));
  return VIEW_MATRIX.clone().multiply(c.clone().invert()).multiply(fromRowMajor(values)).multiply(c).multiply(VIEW_INVERSE);
}
export function interpolatePath(path: number[][], fraction: number): Matrix4 {
  if (!path.length) return new Matrix4();
  const offset = Math.max(0, Math.min(1, fraction)) * (path.length - 1);
  const a = Math.floor(offset), b = Math.min(a + 1, path.length - 1), t = offset - a;
  const pa = new Vector3(), pb = new Vector3(), qa = new Quaternion(), qb = new Quaternion(), sa = new Vector3(), sb = new Vector3();
  fromRowMajor(path[a]).decompose(pa, qa, sa); fromRowMajor(path[b]).decompose(pb, qb, sb);
  return new Matrix4().compose(pa.lerp(pb, t), qa.slerp(qb, t), sa.lerp(sb, t));
}
export function interpolatedDisplay(path: number[][], fraction: number, coordinateMap?: Json): Matrix4 {
  const row = interpolatePath(path, fraction).transpose().toArray();
  return engineToDisplay(row, coordinateMap);
}
export function resultOf(run: Run | null): Json { return run?.result ?? run ?? {}; }
export function partMapping(run: Run | null): Record<string, string> {
  const result = resultOf(run);
  const raw = run?.part_map ?? result.part_map ?? {};
  const out: Record<string, string> = {};
  if (Array.isArray(raw)) raw.forEach(item => { out[String(item.numeric_id ?? item.engine_id)] = String(item.unit_id ?? item.id); });
  else Object.entries(raw).forEach(([key, value]) => { out[key] = typeof value === 'string' ? value : String((value as Json).unit_id ?? (value as Json).id ?? key); });
  return out;
}
export function mapId(run: Run | null, id: unknown): string { const key = String(id ?? ''); return partMapping(run)[key] ?? key; }
export function stepsOf(run: Run | null): PlanStep[] { return (run?.steps ?? resultOf(run).steps ?? []) as PlanStep[]; }
export function assemblyOrder(run: Run | null): string[] {
  const result = resultOf(run);
  const order = result.assembly_order ?? (result.base_part != null ? [result.base_part, ...(result.disassembly ?? result.disassembly_order ?? []).slice().reverse()] : []);
  return order.map((id: any) => mapId(run, id));
}
export function playbackBoundary(run: Run | null, index: number, fraction: number, mode: 'assembly' | 'disassembly'): Boundary {
  const result = resultOf(run), all = assemblyOrder(run), steps = stepsOf(run);
  const empty: Boundary = { moving: null, installed: new Set(), fixed: new Set(), pose: null, path: [], hasPath: false, label: '결과를 선택하세요', step: null };
  if (!all.length) return empty;
  if (mode === 'assembly' && index === 0) {
    return { ...empty, installed: new Set([all[0]]), pose: steps[steps.length - 1]?.pose ?? IDENTITY, label: '기준 부품 배치' };
  }
  // Both modes include an exact start/end stage. Assembly row 0 is the base,
  // disassembly final row is the remaining base. No artificial motion is added.
  if (mode === 'disassembly' && index >= steps.length) {
    return { ...empty, installed: new Set([all[0]]), pose: steps[steps.length - 1]?.pose ?? IDENTITY, label: '분해 완료 · 기준 부품 잔류' };
  }
  const stepIndex = mode === 'assembly' ? steps.length - index : index;
  const step = steps[stepIndex];
  if (!step) return empty;
  const moving = mapId(run, step.moving_part ?? step.moving_unit_id);
  const removedBefore = steps.slice(0, stepIndex).map(s => mapId(run, s.moving_part));
  const before = (step.installed_before ?? all.filter(id => !removedBefore.includes(id))).map(id => mapId(run, id));
  const after = (step.installed_after ?? before.filter(id => id !== moving)).map(id => mapId(run, id));
  const rawPath = Array.isArray(step.path) ? step.path : [];
  const hasPath = rawPath.length > 1 && rawPath.every(frame => Array.isArray(frame) && frame.length === 16 && frame.every(Number.isFinite));
  const path = mode === 'assembly' ? rawPath.slice().reverse() : rawPath;
  // A sequence with no path exposes the exact resulting installation state.
  const atEnd = fraction >= 1 || !hasPath;
  const installed = new Set(mode === 'assembly' ? (atEnd ? before : after) : (atEnd ? after : before));
  return { moving, installed, fixed: new Set((step.fixed_parts ?? step.held_unit_ids ?? []).map(id => mapId(run, id))), pose: step.pose ?? step.assembly_pose ?? IDENTITY, path, hasPath, label: mode === 'assembly' ? '부품 조립' : '부품 분해', step };
}
export function canStartPlayback(run: Run | null, index: number, mode: 'assembly' | 'disassembly'): boolean {
  if (resultOf(run).path_status !== 'success') return false;
  if (playbackBoundary(run, index, 0, mode).hasPath) return true;
  return mode === 'assembly' && index === 0 && playbackBoundary(run, 1, 0, mode).hasPath;
}
/** Reorientation is a boundary-state event, never an invented continuous path. */
export function poseTransition(run: Run | null, index: number, mode: 'assembly' | 'disassembly', tolerance = 1e-6): { changed: boolean; affected: string[]; legacy: boolean } {
  const current = playbackBoundary(run, index, 0, mode);
  const previous = index > 0 ? playbackBoundary(run, index - 1, 1, mode).pose : IDENTITY;
  const valid = (matrix: number[] | null): matrix is number[] => !!matrix && matrix.length === 16 && matrix.every(Number.isFinite);
  const legacy = current.step?.pose_changed === true;
  const changed = legacy || (valid(current.pose) && valid(previous) && current.pose.some((value, offset) => Math.abs(value - previous[offset]) > tolerance));
  return { changed, affected: changed ? [...current.installed] : [], legacy };
}

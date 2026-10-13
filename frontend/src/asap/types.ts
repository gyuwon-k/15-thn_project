export type Json = Record<string, any>;
export interface Unit extends Json { id: string; name: string; definition_id: string; bbox: Json | number[]; volume_mm3: number | null; solid_count: number; valid: boolean; }
export interface SceneMesh { id: string; name?: string; color?: string | number | number[]; positions: number[]; normals?: number[]; indices?: number[]; }
export interface DisplayScene { bbox?: Json | number[]; meshes: SceneMesh[]; }
export interface PlanStep extends Json { index: number; moving_part: string; fixed_parts: string[]; pose: number[]; path: number[][]; installed_before?: string[]; installed_after?: string[]; }
export interface Run extends Json { id?: string; result?: Json; steps?: PlanStep[]; coordinate_map?: Json; part_map?: Json | Json[]; }
export interface Session extends Json { id: string; status: string; source: Json | string; units: Unit[]; manifest: Json; conversion?: Json; runs: Json[]; }
export interface Job extends Json { id: string; status: string; stage?: string; progress?: number | null; logs?: string | any[]; error?: Json | string; }
export interface PhysicsProfile { contact_eps_mm: number; sdf_dx_mm: number; sdf_res: number; path_col_th_mm: number; minimum_separation_mm: number; }
export interface RunSettings { strategy: 'learn' | 'heur-out'; planner: 'dfs'; seed: number; budget: number; timeout_s: number; max_gripper: number; max_pose: number; pose_reuse: number; base_part: string | null; physics_profile?: PhysicsProfile | null; }
export type ViewerMode = 'cad' | 'obj' | 'overlay' | 'assembly' | 'disassembly';
export interface Boundary { moving: string | null; installed: Set<string>; fixed: Set<string>; pose: number[] | null; path: number[][]; hasPath: boolean; label: string; step: PlanStep | null; }

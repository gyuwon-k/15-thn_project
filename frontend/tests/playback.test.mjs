import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { readFileSync, writeFileSync, rmSync } from 'node:fs';
import ts from 'typescript';
import { Matrix4, Vector3 } from 'three';
const compiled = new URL('./.playback.generated.mjs', import.meta.url);
writeFileSync(compiled, ts.transpileModule(readFileSync(new URL('../src/asap/playback.ts', import.meta.url), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext }
}).outputText);
const { IDENTITY, VIEW_MATRIX, engineToDisplay, interpolatePath, playbackBoundary, canStartPlayback, poseTransition, assemblyOrder } = await import(compiled.href);
const compiledComparison = new URL('./.comparison.generated.mjs', import.meta.url);
writeFileSync(compiledComparison, ts.transpileModule(readFileSync(new URL('../src/asap/comparison.ts', import.meta.url), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext }
}).outputText);
const { comparisonDifferences } = await import(compiledComparison.href);
const compiledLogs = new URL('./.logs.generated.mjs', import.meta.url);
writeFileSync(compiledLogs, ts.transpileModule(readFileSync(new URL('../src/asap/logs.ts', import.meta.url), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext }
}).outputText);
const { logLines } = await import(compiledLogs.href);
after(() => { rmSync(compiled, { force: true }); rmSync(compiledComparison, { force: true }); rmSync(compiledLogs, { force: true }); });
const row = matrix => matrix.clone().transpose().toArray();
const move = x => row(new Matrix4().makeTranslation(x, 0, 0));
const run = {
  path_status: 'success',
  part_map: { '00000': { unit_id: 'base' }, '00001': { unit_id: 'bolt_a' }, '00002': { unit_id: 'bolt_b' } },
  base_part: '00000', disassembly: ['00002', '00001'], assembly_order: ['00000', '00001', '00002'],
  steps: [
    { index: 0, moving_part: '00002', fixed_parts: ['00000'], installed_before: ['00000', '00001', '00002'], installed_after: ['00000', '00001'], pose: move(3), path: [move(3), move(7)] },
    { index: 1, moving_part: '00001', fixed_parts: ['00000'], installed_before: ['00000', '00001'], installed_after: ['00000'], pose: move(5), path: [move(5), move(9)] }
  ]
};
test('inverse assembly preserves base and distinct repeated occurrences', () => {
  assert.deepEqual(assemblyOrder(run), ['base', 'bolt_a', 'bolt_b']);
  const base = playbackBoundary(run, 0, 0, 'assembly');
  assert.deepEqual([...base.installed], ['base']);
  const assembly = playbackBoundary(run, 1, 0, 'assembly');
  assert.equal(assembly.moving, 'bolt_a');
  assert.deepEqual([...assembly.installed], ['base']);
  assert.deepEqual(assembly.path, run.steps[1].path.slice().reverse());
  assert.deepEqual([...assembly.fixed], ['base']);
  assert.deepEqual([...playbackBoundary(run, 1, 1, 'assembly').installed], ['base', 'bolt_a']);
  assert.deepEqual([...playbackBoundary(run, 2, 1, 'assembly').installed], ['base', 'bolt_a', 'bolt_b']);
});
test('disassembly final stage retains exactly base', () => {
  assert.deepEqual([...playbackBoundary(run, 0, 1, 'disassembly').installed], ['base', 'bolt_a']);
  assert.deepEqual([...playbackBoundary(run, 2, 1, 'disassembly').installed], ['base']);
});
test('a sequence without actual paths exposes stages without fabricated motion', () => {
  const missing = { ...run, steps: run.steps.map(step => ({ ...step, path: [] })) };
  const stage = playbackBoundary(missing, 1, 0, 'assembly');
  assert.equal(stage.hasPath, false);
  assert.equal(stage.path.length, 0);
  assert.deepEqual([...stage.installed], ['base', 'bolt_a']);
});
test('play from assembly base advances only to a recorded first action; failed paths cannot play', () => {
  assert.equal(canStartPlayback(run, 0, 'assembly'), true);
  assert.equal(canStartPlayback(run, 2, 'disassembly'), false);
  assert.equal(canStartPlayback({ ...run, path_status: 'failed' }, 0, 'assembly'), false);
  assert.equal(canStartPlayback({ ...run, steps: run.steps.map(step => ({ ...step, path: [] })) }, 0, 'assembly'), false);
});
test('engine world delta conjugates shared scale and origin into CAD then display', () => {
  const c = new Matrix4().makeScale(.1, .1, .1).multiply(new Matrix4().makeTranslation(-100, -50, 0));
  const actual = engineToDisplay(move(2), { cad_to_engine: row(c) });
  const point = new Vector3(130, 60, 20).applyMatrix4(VIEW_MATRIX).applyMatrix4(actual);
  const expected = new Vector3(150, 60, 20).applyMatrix4(VIEW_MATRIX);
  assert.ok(point.distanceTo(expected) < 1e-8);
});
test('all installed units use the recorded global pose for the chosen action', () => {
  assert.deepEqual(playbackBoundary(run, 1, .5, 'assembly').pose, move(5));
  assert.deepEqual(playbackBoundary(run, 2, .5, 'assembly').pose, move(3));
});
test('pose transition uses actual stage matrices and the whole installed boundary set', () => {
  assert.deepEqual(poseTransition(run, 0, 'disassembly').affected, ['base', 'bolt_a', 'bolt_b']);
  assert.equal(poseTransition(run, 1, 'disassembly').changed, true);
  assert.deepEqual(poseTransition(run, 1, 'disassembly').affected, ['base', 'bolt_a']);
  assert.equal(poseTransition(run, 2, 'disassembly').changed, false);
  assert.deepEqual(poseTransition(run, 0, 'assembly').affected, ['base']);
  assert.equal(poseTransition(run, 1, 'assembly').changed, false);
  assert.deepEqual(poseTransition(run, 2, 'assembly').affected, ['base', 'bolt_a']);
});
test('tiny pose noise is ignored but legacy explicit events are accepted', () => {
  const quiet = { ...run, steps: run.steps.map(step => ({ ...step, pose: move(5e-8) })) };
  assert.equal(poseTransition(quiet, 0, 'disassembly').changed, false);
  assert.equal(poseTransition(quiet, 1, 'disassembly').changed, false);
  const legacy = { ...quiet, steps: quiet.steps.map((step, index) => ({ ...step, pose_changed: index === 1 })) };
  assert.equal(poseTransition(legacy, 1, 'disassembly').changed, true);
  assert.equal(poseTransition(legacy, 1, 'disassembly').legacy, true);
});
test('path interpolation uses adjacent translation and quaternion slerp', () => {
  const a = IDENTITY;
  const b = row(new Matrix4().makeRotationZ(Math.PI / 2).setPosition(10, 0, 0));
  const middle = interpolatePath([a, b], .5);
  const origin = new Vector3().applyMatrix4(middle);
  assert.ok(Math.abs(origin.x - 5) < 1e-8);
  const axis = new Vector3(1, 0, 0).transformDirection(middle);
  assert.ok(Math.abs(axis.x - Math.SQRT1_2) < 1e-8);
  assert.ok(Math.abs(axis.y - Math.SQRT1_2) < 1e-8);
});
test('changing strategy alone is compatible when preprocessing and constraints match', () => {
  const a = { input_revision: 'same', coordinate_map: { scale: .1 }, settings: { strategy: 'learn', base_part: null, max_gripper: 3, max_pose: 3, pose_reuse: 0 }, provenance: { input_files_sha256: { b: 'b', a: 'a' } } };
  const b = { ...a, settings: { ...a.settings, strategy: 'heur-out' }, provenance: { input_files_sha256: { a: 'a', b: 'b' } } };
  assert.deepEqual(comparisonDifferences(a, b), []);
});
test('changed mesh revision and holding/pose constraints warn before benchmark comparison', () => {
  const a = { input_revision: 'raw', settings: { base_part: null, max_gripper: 3, max_pose: 3, pose_reuse: 0 } };
  const b = { input_revision: 'union', settings: { base_part: '00000', max_gripper: 4, max_pose: 5, pose_reuse: 1 } };
  const warnings = comparisonDifferences(a, b);
  assert.equal(warnings.length, 5);
  assert.ok(warnings.some(message => message.includes('변환 리비전')));
  assert.ok(warnings.some(message => message.includes('고정 기준')));
  assert.ok(warnings.some(message => message.includes('Max gripper')));
});
test('missing input provenance cannot claim an equivalent benchmark', () => {
  assert.ok(comparisonDifferences({ settings: {} }, { settings: {} }).some(message => message.includes('확인할 수 없습니다')));
});
test('physical mm settings distinguish comparisons without object key-order noise', () => {
  const a = { input_revision:'same', settings:{ physics_profile:{contact_eps_mm:.25, sdf_dx_mm:1} } };
  const b = { input_revision:'same', settings:{ physics_profile:{sdf_dx_mm:1, contact_eps_mm:.25} } };
  assert.deepEqual(comparisonDifferences(a,b), []);
  b.settings.physics_profile.contact_eps_mm = .5;
  assert.ok(comparisonDifferences(a,b).some(message => message.includes('접촉·SDF·분리')));
  assert.ok(comparisonDifferences(a,{input_revision:'same',settings:{}}).some(message => message.includes('물리 설정')));
});
test('canonical text logs preserve real line breaks without treating text as an array', () => {
  assert.deepEqual(logLines('STEP 해석\r\nOBJ 변환\nASAP 완료'), ['STEP 해석', 'OBJ 변환', 'ASAP 완료']);
});
test('legacy text and event-array logs remain readable', () => {
  assert.deepEqual(logLines(['첫 줄', { message: '둘째 줄\n셋째 줄' }, { stage: 'run', progress: .5 }]), ['첫 줄', '둘째 줄', '셋째 줄', '{"stage":"run","progress":0.5}']);
});
test('missing or malformed log payloads cannot crash the log panel', () => {
  for (const value of [null, undefined, '', false, 17, { unexpected: true }]) assert.deepEqual(logLines(value), []);
  assert.deepEqual(logLines([null, false, 17]), []);
});


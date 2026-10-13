import assert from 'node:assert/strict';
import test, {after} from 'node:test';
import {readFileSync,writeFileSync,rmSync} from 'node:fs';
import ts from 'typescript';
const compiled=new URL('./.diagnostics.generated.mjs',import.meta.url);
writeFileSync(compiled,ts.transpileModule(readFileSync(new URL('../src/asap/diagnostics.ts',import.meta.url),'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}}).outputText);
const {diagnosticRows}=await import(compiled.href);
after(()=>rmSync(compiled,{force:true}));
const source=()=>({units:[{id:'group',name:'block',member_refs:[{occurrence_id:'insert'}]},{id:'bolt1',name:'same bolt'},{id:'bolt2',name:'same bolt'}],manifest:{diagnostics:[]},unit_state:{revision:'u1'},conversion:{revision:'c1'},quality_report:{units:[],warnings:[],initial_penetration:{penetration_pairs:[{a:'insert',b:'bolt1',common_volume_mm3:2}],unchecked_pairs:[{a:'group',b:'bolt2',reason_code:'TIME_BUDGET_EXHAUSTED'}],pairs:[{a:'bolt1',b:'bolt2',status:'calculation_failed',reason:'Boolean failed'}]}},runs:[]});
test('original member diagnostics select its group while repeated bolts remain separate',()=>{
 const rows=diagnosticRows(source());assert.deepEqual(rows.find(r=>r.category==='간섭').unitIds,['group','bolt1']);assert.deepEqual(rows.find(r=>r.category==='미검사').unitIds,['group','bolt2']);
});
test('a calculation failure and an unchecked pair stay distinct',()=>{
 const rows=diagnosticRows(source());assert.equal(rows.filter(r=>r.category==='계산 실패').length,1);assert.equal(rows.filter(r=>r.category==='미검사').length,1);assert.match(rows.find(r=>r.category==='계산 실패').raw.reason,/Boolean/);
});
test('OCCT entries group by message without invented part attribution',()=>{
 const s=source();s.transfer_report={entries:[{kind:'warning',entity_number:1,message:'Poor result from projection vertex / curve 3d'},{kind:'warning',entity_number:2,message:'Poor result from projection vertex / curve 3d'},{kind:'warning',entity_number:3,message:'Other warning'}]};const rows=diagnosticRows(s).filter(r=>r.category==='OCCT');assert.equal(rows.length,2);assert.equal(rows[0].raw.count,2);assert.deepEqual(rows[0].unitIds,[]);assert.notEqual(rows[0].key,rows[1].key);
});
test('runtime failure from a different input cannot be attributed to current units',()=>{
 const s=source();s.runs=[{reason_code:'ENGINE_CRASH',unit_revision:'old',input_revision:'c1',diagnostic_context:{unit_ids:['group']}}];assert.equal(diagnosticRows(s).filter(r=>r.category==='엔진').length,0);s.runs[0].unit_revision='u1';assert.equal(diagnosticRows(s)[0].category,'엔진');
});
test('missing historical pair details are explicitly reported',()=>{
 const s=source();s.quality_report.initial_penetration.unchecked_pair_details_truncated=7;const row=diagnosticRows(s).find(r=>r.code==='DETAILS_TRUNCATED');assert.deepEqual(row.unitIds,[]);assert.match(row.message,/7/);
});


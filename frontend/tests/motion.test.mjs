import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
const code=ts.transpileModule(readFileSync(new URL('../src/motion.ts',import.meta.url),'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
const {evaluateSceneAt,isExecutableStep,executablePrefix,pathEndpoints}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'));
const safe=(id)=>({id,unit_id:id,direction:'-Z',distance_mm:20,duration_s:2,status:'PASS',executable:true,path:{kind:'translation',start_translation_mm:[0,0,20],end_translation_mm:[0,0,0],verified:true,executable:true}});
test('old direction-only drafts and old verified paths never become motion',()=>{
  assert.equal(pathEndpoints({direction:'+X',distance_mm:50}),null);
  const old=safe('old');delete old.executable;delete old.path.executable;
  assert.equal(isExecutableStep(old),false);
  assert.equal(evaluateSceneAt([old],1).get('old').visible,false);
});
test('a collision and every later step stay hidden even when seeking beyond the timeline',()=>{
  const steps=[safe('a'),{...safe('b'),status:'COLLISION'},safe('c')];
  assert.equal(executablePrefix(steps).length,1);
  for(const time of [2,3,5,100]){
    const states=evaluateSceneAt(steps,time);
    assert.equal(states.get('a').visible,true);
    assert.equal(states.get('b').visible,false);
    assert.equal(states.get('c').visible,false);
  }
});
test('only canonical unchanged endpoints execute; full static inspection remains available',()=>{
  const step=safe('a');assert.deepEqual(evaluateSceneAt([step],1).get('a').translation,[0,0,10]);
  assert.deepEqual(evaluateSceneAt([step],2).get('a').translation,[0,0,0]);
  assert.equal(evaluateSceneAt([step],null).size,0);
  step.direction='+X';assert.equal(isExecutableStep(step),false);
  assert.equal(evaluateSceneAt([step],1).get('a').visible,false);
});

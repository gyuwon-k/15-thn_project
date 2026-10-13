import assert from 'node:assert/strict';
import test,{after} from 'node:test';
import {readFileSync,writeFileSync,rmSync} from 'node:fs';
import ts from 'typescript';
import {Matrix4} from 'three';
const files=[];
function compile(source,name){const url=new URL(name,import.meta.url);let content=ts.transpileModule(readFileSync(new URL(source,import.meta.url),'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}}).outputText;content=content.replace("'./asap/playback'","'./.playback.motion.mjs'");writeFileSync(url,content);files.push(url);return url;}
compile('../src/asap/playback.ts','.playback.motion.mjs');
const {asapFrame,asapPose,installsUnit}=await import(compile('../src/motion.ts','.motion.generated.mjs'));
after(()=>files.forEach(f=>rmSync(f,{force:true})));
const row=matrix=>matrix.clone().transpose().toArray();
const translate=x=>row(new Matrix4().makeTranslation(x,0,0));
test('native removal frames replay in reverse with metre-correct translation',()=>{
 const path=[translate(0),translate(1000)],motion={verified:true,validity:'current',step:{pose:translate(0),path}};
 assert.equal(asapFrame(motion,0).elements[12],1);assert.equal(asapFrame(motion,.5).elements[12],.5);assert.equal(asapFrame(motion,1).elements[12],0);assert.deepEqual(path,[translate(0),translate(1000)]);
});
test('stale, unavailable, invalid native frames produce no invented path',()=>{
 const motion={verified:true,step:{path:[translate(0),translate(100)]}};
 assert.equal(asapFrame({...motion,validity:'stale'},.5),null);assert.equal(asapFrame({...motion,verified:false},.5),null);assert.equal(asapFrame({verified:true,step:{path:[[NaN],translate(0)]}},.5),null);
});
test('base global pose keeps rotations and correct engine scaling',()=>{
 const forward=row(new Matrix4().makeScale(.01,.01,.01));const matrix=asapPose({coordinate_map:{forward_matrix:forward},assembly_pose:translate(2)});assert.equal(matrix.elements[12],.2);assert.equal(matrix.elements[0],1);
});
test('temporary and final tightening remain independent work without reinstalling',()=>{
 assert.equal(installsUnit({kind:'fasten',fastening_stage:'temporary'}),true);assert.equal(installsUnit({kind:'fasten',fastening_stage:'final'}),false);assert.equal(installsUnit({kind:'inspect'}),false);
});

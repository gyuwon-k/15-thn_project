import type {Row} from './api';
import {stepUnit} from './api';
export const AXES:Record<string,number[]>={'+X':[1,0,0],'-X':[-1,0,0],'+Y':[0,1,0],'-Y':[0,-1,0],'+Z':[0,0,1],'-Z':[0,0,-1]};
export const stepDuration=(step:Row)=>Math.max(.2,Number(step.duration_s)||2);
export function timelineDuration(steps:Row[]){return steps.reduce((sum,step)=>sum+stepDuration(step),0);}
export function stageAt(steps:Row[],time:number){let elapsed=0;for(let index=0;index<steps.length;index++){const duration=stepDuration(steps[index]);if(time<elapsed+duration||index===steps.length-1)return {step:steps[index],index,start:elapsed,progress:Math.min(1,Math.max(0,(time-elapsed)/duration))};elapsed+=duration;}return null;}
function triple(value:any):number[]|null{return Array.isArray(value)&&value.length>=3&&value.slice(0,3).every(Number.isFinite)?value.slice(0,3):null;}
export function pathEndpoints(step:Row):{start:number[],end:number[],stored:boolean}|null{
  const path=step.path;
  if(path){
    const start=triple(path.start_offset_mm)||triple(path.start_translation_mm)||triple(path.start_mm)||triple(path.start)||triple(path.offset_start);
    const end=triple(path.end_offset_mm)||triple(path.end_translation_mm)||triple(path.end_mm)||triple(path.end)||triple(path.offset_end);
    if(start&&end)return {start,end,stored:true};
    const points=Array.isArray(path)?path:path.translations_mm||path.points_mm||path.points;
    if(Array.isArray(points)&&points.length>=2){const a=triple(points[0]?.translation_mm||points[0]),b=triple(points.at(-1)?.translation_mm||points.at(-1));if(a&&b)return {start:a,end:b,stored:true};}
  }
  const direction=AXES[step.direction];const distance=Number(step.distance_mm);
  if(direction&&Number.isFinite(distance)&&distance>0)return {start:direction.map(x=>-x*distance),end:[0,0,0],stored:false};
  return null;
}
export function evaluateSceneAt(steps:Row[],time:number|null){
  const states=new Map<string,{visible:boolean,translation:number[],stored:boolean}>();
  if(time===null)return states;
  let elapsed=0;
  for(const step of steps){
    const duration=stepDuration(step),path=pathEndpoints(step),unit=stepUnit(step);
    if(time<elapsed)states.set(unit,{visible:false,translation:[0,0,0],stored:!!path?.stored});
    else if(time>=elapsed+duration)states.set(unit,{visible:true,translation:[0,0,0],stored:!!path?.stored});
    else if(path){const t=Math.min(1,Math.max(0,(time-elapsed)/duration));const smooth=t*t*(3-2*t);states.set(unit,{visible:true,translation:path.start.map((n,i)=>n+(path.end[i]-n)*smooth),stored:path.stored});}
    else states.set(unit,{visible:false,translation:[0,0,0],stored:false});
    elapsed+=duration;
  }
  return states;
}

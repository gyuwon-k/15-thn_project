import type {Row} from './api';
export const AXES:Record<string,number[]>={'+X':[1,0,0],'-X':[-1,0,0],'+Y':[0,1,0],'-Y':[0,-1,0],'+Z':[0,0,1],'-Z':[0,0,-1]};
export const stepDuration=(step:Row)=>Math.max(.2,Number(step.duration_s)||2);
export function timelineDuration(steps:Row[]){return steps.reduce((sum,step)=>sum+stepDuration(step),0);}
export function stageAt(steps:Row[],time:number){let elapsed=0;for(let index=0;index<steps.length;index++){const duration=stepDuration(steps[index]);if(time<elapsed+duration||index===steps.length-1)return {step:steps[index],index,start:elapsed,progress:Math.min(1,Math.max(0,(time-elapsed)/duration))};elapsed+=duration;}return null;}
function triple(value:any):number[]|null{return Array.isArray(value)&&value.length===3&&value.every(Number.isFinite)?value:null;}
// A direction is editable intent. Only the canonical server-checked path is motion.
export function pathEndpoints(step:Row):{start:number[],end:number[],stored:boolean}|null{
  const path=step.path,axis=AXES[step.direction],distance=Number(step.distance_mm);
  if(!path||path.kind!=='translation'||!axis||!Number.isFinite(distance)||distance<=0)return null;
  const start=triple(path.start_translation_mm),end=triple(path.end_translation_mm);
  if(!start||!end||start.some((v,i)=>Math.abs(v+axis[i]*distance)>1e-8)||end.some(v=>Math.abs(v)>1e-8))return null;
  return {start,end,stored:true};
}
export function isExecutableStep(step:Row){return step.status==='PASS'&&step.executable===true&&step.path?.verified===true&&step.path?.executable===true&&!!pathEndpoints(step);}
export function executablePrefix(steps:Row[]){let count=0;while(count<steps.length&&isExecutableStep(steps[count]))count++;return steps.slice(0,count);}
export function evaluateSceneAt(steps:Row[],time:number|null){
  const states=new Map<string,{visible:boolean,translation:number[],stored:boolean}>();
  if(time===null)return states;
  let elapsed=0,blocked=false;
  for(const step of steps){
    const duration=stepDuration(step),path=pathEndpoints(step),unit=step.unit_id||step.target_unit_ids?.[0]||'';
    blocked=blocked||!isExecutableStep(step);
    if(blocked||time<elapsed)states.set(unit,{visible:false,translation:[0,0,0],stored:false});
    else if(time>=elapsed+duration)states.set(unit,{visible:true,translation:path!.end,stored:true});
    else {const t=Math.min(1,Math.max(0,(time-elapsed)/duration));const smooth=t*t*(3-2*t);states.set(unit,{visible:true,translation:path!.start.map((n,i)=>n+(path!.end[i]-n)*smooth),stored:true});}
    elapsed+=duration;
  }
  return states;
}

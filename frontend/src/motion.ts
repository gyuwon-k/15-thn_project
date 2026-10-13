import type {Matrix4} from 'three';
import {engineToDisplay,interpolatedDisplay} from './asap/playback';
import type {Row} from './api';
export function installsUnit(operation:Row){return ['prepare','place','fasten','install_flexible'].includes(operation.kind)&&!(operation.kind==='fasten'&&operation.fastening_stage==='final');}
export function displayMeters(matrix:Matrix4){matrix.elements[12]/=1000;matrix.elements[13]/=1000;matrix.elements[14]/=1000;return matrix;}
export function asapPose(motion:Row){return displayMeters(engineToDisplay(motion.step?.pose??motion.assembly_pose??null,motion.coordinate_map));}
export function asapFrame(motion:Row,progress:number):Matrix4|null{
 const path=motion.step?.path;if(!motion.verified||motion.validity==='stale'||!Array.isArray(path)||path.length<2||path.some((f:any)=>!Array.isArray(f)||f.length!==16||f.some((n:any)=>typeof n!=='number'||!Number.isFinite(n))))return null;
 return displayMeters(interpolatedDisplay([...path].reverse(),progress,motion.coordinate_map));
}

import {useEffect, useRef, useState} from 'react';
import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {Box, Focus, RotateCcw, Layers3, Eye, LoaderCircle} from 'lucide-react';
import type {Row} from './api';
import {asapFrame,asapPose,installsUnit} from './motion';

export type SceneProps = {url?:string, instances:Row[],units:Row[],operations:Row[],time:number|null,selected?:string,onSelect?:(id:string)=>void,render?:boolean,onReady?:(ok:boolean)=>void,asapMotionValid?:boolean};
const colors = ['#879caa','#d89855','#bccad0','#293f4c','#c2b99e','#617b86','#d5dde0'];
const convert=(v:number[])=>new THREE.Vector3(v[0]/1000,v[2]/1000,-v[1]/1000);
export default function Scene(p:SceneProps){
 const host=useRef<HTMLDivElement>(null), engine=useRef<any>(null), latest=useRef(p);latest.current=p;
 const [loading,setLoading]=useState(false),[error,setError]=useState(''),[exploded,setExploded]=useState(false),[isolated,setIsolated]=useState(false),[count,setCount]=useState(0);
 useEffect(()=>{
  if(!host.current)return;let alive=true;
  const scene=new THREE.Scene();scene.background=new THREE.Color('#e9eff1');scene.fog=new THREE.Fog('#e9eff1',3,12);
  const renderer=new THREE.WebGLRenderer({antialias:true,preserveDrawingBuffer:true});renderer.setPixelRatio(Math.min(devicePixelRatio,2));renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.setClearColor('#e9eff1');
  renderer.setSize(host.current.clientWidth,host.current.clientHeight);host.current.appendChild(renderer.domElement);
  const camera=new THREE.PerspectiveCamera(34,host.current.clientWidth/host.current.clientHeight,.001,100);camera.position.set(.7,.55,.75);
  const controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=true;controls.dampingFactor=.09;controls.maxDistance=5;controls.minDistance=.03;controls.target.set(0,.08,0);
  scene.add(new THREE.HemisphereLight('#ffffff','#a4b4bd',2.8));
  const key=new THREE.DirectionalLight('#ffffff',3.2);key.position.set(1,2,1);scene.add(key);
  const fill=new THREE.DirectionalLight('#c7dce6',1.5);fill.position.set(-1,1,-2);scene.add(fill);
  const model=new THREE.Group();scene.add(model);const grid=new THREE.GridHelper(2,40,'#cbd7da','#d9e2e5');grid.position.y=-.1;scene.add(grid);
  const e:any={scene,renderer,camera,controls,model,grid,meshes:[],radius:.25,center:new THREE.Vector3(),explode:false,isolate:false,ready:false};engine.current=e;
  const resize=new ResizeObserver(()=>{if(!host.current)return;const w=host.current.clientWidth,h=host.current.clientHeight;renderer.setSize(w,h);camera.aspect=w/h;camera.updateProjectionMatrix();});resize.observe(host.current);
  let frame=0;const draw=()=>{if(!alive)return;frame=requestAnimationFrame(draw);controls.update();renderer.render(scene,camera);};if(!p.render)draw();
  const pointerStart={x:0,y:0};const down=(ev:PointerEvent)=>{pointerStart.x=ev.clientX;pointerStart.y=ev.clientY;};
  const up=(ev:PointerEvent)=>{if(Math.hypot(ev.clientX-pointerStart.x,ev.clientY-pointerStart.y)>5)return;const b=renderer.domElement.getBoundingClientRect(),pointer=new THREE.Vector2((ev.clientX-b.left)/b.width*2-1,-(ev.clientY-b.top)/b.height*2+1),ray=new THREE.Raycaster();ray.setFromCamera(pointer,camera);const hit=ray.intersectObjects(e.meshes).find(x=>x.object.visible);if(hit)latest.current.onSelect?.(hit.object.userData.instanceId);};
  renderer.domElement.addEventListener('pointerdown',down);renderer.domElement.addEventListener('pointerup',up);
  return()=>{alive=false;cancelAnimationFrame(frame);resize.disconnect();controls.dispose();model.traverse(o=>{if(o instanceof THREE.Mesh){o.geometry.dispose();(o.material as THREE.Material).dispose();}});renderer.dispose();renderer.domElement.remove();engine.current=null;};
 },[]);
 useEffect(()=>{
  const e=engine.current;if(!e)return;let dead=false;setError('');setLoading(Boolean(p.url));p.onReady?.(false);
  e.model.traverse((node:THREE.Object3D)=>{if(node instanceof THREE.Mesh){node.geometry.dispose();(node.material as THREE.Material).dispose();}});e.model.clear();e.meshes=[];setCount(0);e.ready=false;
  if(!p.url){setLoading(false);return;}
  fetch(p.url).then(async r=>{if(!r.ok)throw new Error('표시용 형상을 불러오지 못했습니다.');return r.json();}).then(data=>{
   if(dead)return;const meshes=Array.isArray(data)?data:(data.meshes||data.instances||[]);
   for(let index=0;index<meshes.length;index++){
    const item=meshes[index],source=item.geometry||data.geometries?.[item.mesh_ref]||item;let pos=source.positions||source.vertices||[];if(Array.isArray(pos[0]))pos=pos.flat();if(!pos.length)continue;
    const geometry=new THREE.BufferGeometry(),arr=new Float32Array(pos.length);
    const display=data.coordinate_system==='display_m'||data.coordinates==='display_m'||data.units==='m';
    for(let i=0;i<pos.length;i+=3){arr[i]=display?pos[i]:pos[i]/1000;arr[i+1]=display?pos[i+1]:pos[i+2]/1000;arr[i+2]=display?pos[i+2]:-pos[i+1]/1000;}
    geometry.setAttribute('position',new THREE.BufferAttribute(arr,3));let indices=source.indices||source.triangles;if(indices){if(Array.isArray(indices[0]))indices=indices.flat();geometry.setIndex(indices);}geometry.computeVertexNormals();geometry.computeBoundingBox();
    let color=item.color||source.color||colors[index%colors.length];if(Array.isArray(color))color=new THREE.Color(color[0],color[1],color[2]);
    const mat=new THREE.MeshStandardMaterial({color,roughness:.55,metalness:.25,side:THREE.DoubleSide});
    const mesh=new THREE.Mesh(geometry,mat);mesh.matrixAutoUpdate=false;mesh.name=item.name||item.id;mesh.userData.instanceId=item.instance_id||item.id;mesh.userData.baseColor=mat.color.clone();mesh.userData.center=geometry.boundingBox!.getCenter(new THREE.Vector3());
    e.model.add(mesh);e.meshes.push(mesh);
   }
   const bbox=new THREE.Box3().setFromObject(e.model),center=bbox.getCenter(new THREE.Vector3()),size=bbox.getSize(new THREE.Vector3());
   e.center=center;e.radius=Math.max(size.x,size.y,size.z,.001);e.model.position.copy(center).negate();e.grid.position.y=bbox.min.y-center.y-e.radius*.03;e.grid.scale.setScalar(e.radius*1.8);
   e.controls.target.set(0,0,0);e.controls.minDistance=e.radius*.02;e.controls.maxDistance=e.radius*20;e.camera.position.set(e.radius*1.6,e.radius*1.2,e.radius*1.6);e.camera.near=Math.max(.000001,e.radius*.0001);e.camera.far=Math.max(.1,e.radius*100);e.camera.updateProjectionMatrix();e.controls.update();e.ready=true;setCount(e.meshes.length);setLoading(false);updateScene(e,latest.current);e.renderer.render(e.scene,e.camera);p.onReady?.(true);
  }).catch(err=>{if(!dead){setError(err.message);setLoading(false);p.onReady?.(false);}});
  return()=>{dead=true;};
 },[p.url]);
 useEffect(()=>{const e=engine.current;if(e){e.explode=exploded;e.isolate=isolated;updateScene(e,p);e.renderer.render(e.scene,e.camera);}},[p.time,p.selected,p.units,p.operations,p.asapMotionValid,exploded,isolated]);
 function fit(){const e=engine.current;if(e){e.controls.target.set(0,0,0);e.camera.position.set(e.radius*1.6,e.radius*1.2,e.radius*1.6);e.controls.update();}}
 return <div className={'scene '+(p.render?'render-scene':'')}>
  <div className="scene-canvas" ref={host}/>
  {!p.render&&<><div className="viewport-label"><span className="live-dot"/> LIVE 3D <span className="viewport-divider"/> {count?`${count} 배치`:'모델 뷰어'}</div><div className="viewport-tools"><button title="전체 보기" onClick={fit}><Focus size={17}/></button><button title="분해 보기" className={exploded?'active':''} onClick={()=>setExploded(!exploded)}><Layers3 size={17}/></button><button title="선택 부품 격리" className={isolated?'active':''} onClick={()=>setIsolated(!isolated)} disabled={!p.selected}><Eye size={17}/></button><button title="시점 초기화" onClick={()=>{setExploded(false);setIsolated(false);fit();}}><RotateCcw size={16}/></button></div><div className="axis-widget"><span className="z">Z</span><span className="x">X</span><span className="y">Y</span><i/></div><div className="viewport-hint">드래그 회전 <b>·</b> 스크롤 확대 <b>·</b> 부품 클릭 선택</div>{p.time!==null&&<div className={'motion-source '+(p.asapMotionValid===false?'stale':'')}>{p.operations.some(o=>o.asap_motion)?p.asapMotionValid===false?'이전 경로 · 현재 순서의 움직임은 미검증':'ASAP 실제 경로 · 자세 전환은 경계 상태':'직선 진입 미리보기 · 실제 경로 미검증'}</div>}</>}
  {loading&&<div className="scene-state"><LoaderCircle className="spin" size={28}/><strong>실제 CAD 형상을 준비하고 있습니다</strong><span>변환된 삼각망을 불러오는 중</span></div>}
  {!loading&&!count&&<div className="scene-state"><Box size={40}/><strong>{error||'3D 모델을 불러오세요'}</strong><span>완제품과 부품 STEP 파일을 등록하면 조립 구조가 나타납니다.</span></div>}
 </div>;
}
function updateScene(e:any,p:SceneProps){
 const memberUnit=new Map<string,Row>();p.units.forEach(u=>(u.member_instance_ids||[]).forEach((id:string)=>memberUnit.set(id,u)));
 const installed=new Map<string,{start:number,end:number,op:Row}>();let time=0;
 p.operations.forEach(op=>{const dur=Number(op.duration_s)||2;if(installsUnit(op))(op.target_unit_ids||[]).forEach((id:string)=>{if(!installed.has(id))installed.set(id,{start:time,end:time+dur,op});});time+=dur;});
 const selectedUnit=p.units.find(u=>u.id===p.selected||u.member_instance_ids?.includes(p.selected));
 let elapsed=0,stage:Row|null=null;for(const op of p.operations){if(op.asap_motion)stage=op;if(p.time!==null&&p.time<elapsed+(Number(op.duration_s)||2))break;elapsed+=Number(op.duration_s)||2;}
 const stageMotion=stage?.asap_motion;
 const pose=p.time!==null&&p.asapMotionValid!==false&&stageMotion?asapPose(stageMotion):new THREE.Matrix4();
 e.meshes.forEach((mesh:THREE.Mesh)=>{
  const id=mesh.userData.instanceId,unit=p.units.find(u=>u.id===id)||memberUnit.get(id),event=unit?installed.get(unit.id):undefined;
  const selected=id===p.selected||unit?.id===p.selected||Boolean(selectedUnit?.member_instance_ids?.includes(id));
  mesh.visible=!e.isolate||selected;mesh.matrix.copy(pose);
  if(p.time!==null&&event){
   if(p.time<event.start)mesh.visible=false;
   else if(p.time<event.end&&event.op.kind!=='prepare'){
    const progress=Math.max(0,Math.min(1,(p.time-event.start)/(event.end-event.start))),motion=event.op.asap_motion;
    if(motion){const frame=p.asapMotionValid!==false?asapFrame(motion,progress):null;if(frame)mesh.matrix.copy(frame);else mesh.visible=false;}
    else{const t=1-progress*progress*(3-2*progress),dir=event.op.direction||'+Z',vec=dir==='-Z'?[0,0,-1]:dir==='+X'?[1,0,0]:dir==='-X'?[-1,0,0]:dir==='+Y'?[0,1,0]:dir==='-Y'?[0,-1,0]:[0,0,1];const offset=convert(vec).multiplyScalar((event.op.motion_distance_mm??e.radius*600)*t);mesh.matrix.setPosition(offset);}
   }
  }
  if(e.explode){const v=(mesh.userData.center as THREE.Vector3).clone().sub(e.center);if(v.length()>.000001){v.normalize().multiplyScalar(e.radius*.23);mesh.matrix.elements[12]+=v.x;mesh.matrix.elements[13]+=v.y;mesh.matrix.elements[14]+=v.z;}}
  const mat=mesh.material as THREE.MeshStandardMaterial;mat.emissive.set(selected?'#1b706c':'#000000');mat.emissiveIntensity=selected?.32:0;
  mesh.updateMatrixWorld(true);
 });
}

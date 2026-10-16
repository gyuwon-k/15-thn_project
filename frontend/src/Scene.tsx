import {useEffect,useRef,useState} from 'react';
import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {Box,Focus,RotateCcw,Layers3,Eye,LoaderCircle,Camera} from 'lucide-react';
import {api,unitMembers} from './api';
import type {Row} from './api';
import {evaluateSceneAt,AXES,stageAt} from './motion';

type Props={entryHint?:Row,url?:string,units:Row[],steps:Row[],time:number|null,selected?:string,selectedIds?:string[],hiddenIds?:string[],onSelect?:(id:string,multi?:boolean)=>void,render?:boolean,onReady?:(ready:boolean)=>void,onCameraSave?:(camera:Row)=>void,camera?:Row,autoCamera?:boolean};
const colors=['#a6b6bd','#bd7b40','#d5dde0','#566e78','#c9b888','#839ca5','#9aaab0'];
const convert=(v:number[])=>new THREE.Vector3(v[0]/1000,v[2]/1000,-v[1]/1000);
export default function Scene(props:Props){
 const host=useRef<HTMLDivElement>(null),engine=useRef<any>(null),latest=useRef(props);latest.current=props;
 const [loading,setLoading]=useState(false),[error,setError]=useState(''),[count,setCount]=useState(0),[exploded,setExploded]=useState(false),[isolated,setIsolated]=useState(false);
 useEffect(()=>{
  if(!host.current)return;let alive=true;
  let renderer:THREE.WebGLRenderer;
  try{renderer=new THREE.WebGLRenderer({antialias:true,preserveDrawingBuffer:true});}catch{setError('3D 그래픽을 초기화하지 못했습니다. 그래픽 드라이버를 확인해 주세요.');return;}
  renderer.setPixelRatio(Math.min(devicePixelRatio,2));renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=.9;renderer.setSize(host.current.clientWidth,host.current.clientHeight);host.current.appendChild(renderer.domElement);
  const scene=new THREE.Scene();scene.background=new THREE.Color(props.render?'#e8eef0':'#E5E5E7');
  const camera=new THREE.PerspectiveCamera(35,1,.00001,100);camera.position.set(.7,.55,.75);
  const controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=!props.render;controls.dampingFactor=.1;
  scene.add(new THREE.HemisphereLight('#ffffff','#9aafb9',2.8));const key=new THREE.DirectionalLight('#ffffff',3);key.position.set(1,2,1);scene.add(key);const fill=new THREE.DirectionalLight('#d4e8ed',1.6);fill.position.set(-1,1,-2);scene.add(fill);
  const model=new THREE.Group();scene.add(model);const grid=new THREE.GridHelper(2,30,'#bdcdd2','#d3dfe2');scene.add(grid);
  const selection=new THREE.Box3Helper(new THREE.Box3(),new THREE.Color('#7500ED'));selection.visible=false;scene.add(selection);
  const entryVisual=new THREE.Group();scene.add(entryVisual);
  const e={entryVisual,scene,renderer,camera,controls,model,grid,selection,meshes:[] as THREE.Mesh[],radius:.3,center:new THREE.Vector3(),explode:false,isolate:false,ready:false};engine.current=e;
  const resize=()=>{if(!host.current)return;const width=Math.max(1,host.current.clientWidth),height=Math.max(1,host.current.clientHeight);renderer.setSize(width,height);camera.aspect=width/height;camera.updateProjectionMatrix();renderer.render(scene,camera);};const observer=new ResizeObserver(resize);observer.observe(host.current);resize();
  let frame=0;const draw=()=>{if(!alive)return;frame=requestAnimationFrame(draw);controls.update();renderer.render(scene,camera);};draw();
  let down=[0,0];const pointerDown=(event:PointerEvent)=>{down=[event.clientX,event.clientY];};
  const pointerUp=(event:PointerEvent)=>{if(Math.hypot(event.clientX-down[0],event.clientY-down[1])>5)return;const rect=renderer.domElement.getBoundingClientRect(),pointer=new THREE.Vector2((event.clientX-rect.left)/rect.width*2-1,-(event.clientY-rect.top)/rect.height*2+1),ray=new THREE.Raycaster();ray.setFromCamera(pointer,camera);const hit=ray.intersectObjects(e.meshes).find(item=>item.object.visible);if(hit)latest.current.onSelect?.(hit.object.userData.occurrenceId,event.ctrlKey||event.metaKey);};
  renderer.domElement.addEventListener('pointerdown',pointerDown);renderer.domElement.addEventListener('pointerup',pointerUp);
  return()=>{alive=false;cancelAnimationFrame(frame);observer.disconnect();controls.dispose();e.meshes.forEach(mesh=>{mesh.geometry.dispose();(mesh.material as THREE.Material).dispose();});selection.geometry.dispose();(selection.material as THREE.Material).dispose();grid.geometry.dispose();grid.material.dispose();renderer.dispose();renderer.domElement.remove();engine.current=null;};
 },[]);
 useEffect(()=>{
  const e=engine.current;if(!e)return;let dead=false;setError('');setLoading(Boolean(props.url));props.onReady?.(false);e.ready=false;
  e.meshes.forEach((mesh:THREE.Mesh)=>{mesh.geometry.dispose();(mesh.material as THREE.Material).dispose();});e.model.clear();e.meshes=[];setCount(0);
  if(!props.url){setLoading(false);return;}
  const request=props.url.startsWith('/api/')?api(props.url.slice(4)):fetch(props.url).then(response=>{if(!response.ok)throw new Error('표시 형상을 불러오지 못했습니다.');return response.json();});
  request.then(data=>{
   if(dead)return;const meshes=Array.isArray(data)?data:data.meshes||[];
   for(let index=0;index<meshes.length;index++){
    const item=meshes[index],source=item.geometry||item;let positions=source.positions||source.vertices||[];if(Array.isArray(positions[0]))positions=positions.flat();if(!positions.length)continue;
    const geometry=new THREE.BufferGeometry(),array=new Float32Array(positions.length);
    for(let i=0;i<positions.length;i+=3){array[i]=positions[i]/1000;array[i+1]=positions[i+2]/1000;array[i+2]=-positions[i+1]/1000;}
    geometry.setAttribute('position',new THREE.BufferAttribute(array,3));let indices=source.indices||source.triangles;if(indices){if(Array.isArray(indices[0]))indices=indices.flat();geometry.setIndex(indices);}geometry.computeVertexNormals();geometry.computeBoundingBox();
    let color=item.color||source.color||colors[index%colors.length];if(Array.isArray(color))color=new THREE.Color(color[0],color[1],color[2]);
    const material=new THREE.MeshStandardMaterial({color,roughness:.6,metalness:.18,side:THREE.DoubleSide});const mesh=new THREE.Mesh(geometry,material);mesh.name=item.name||item.id;mesh.userData.occurrenceId=item.occurrence_id||item.id;mesh.userData.baseColor=material.color.clone();mesh.userData.center=geometry.boundingBox!.getCenter(new THREE.Vector3());e.meshes.push(mesh);e.model.add(mesh);
   }
   if(!e.meshes.length)throw new Error('표시 가능한 형상이 없습니다. 모델 변환 진단을 확인해 주세요.');
   const box=new THREE.Box3().setFromObject(e.model),center=box.getCenter(new THREE.Vector3()),size=box.getSize(new THREE.Vector3());e.center=center;e.radius=Math.max(size.x,size.y,size.z,.001);e.model.position.copy(center).negate();e.grid.position.y=box.min.y-center.y-e.radius*.03;e.grid.scale.setScalar(e.radius*1.8);e.controls.minDistance=e.radius*.02;e.controls.maxDistance=e.radius*30;e.camera.near=e.radius*.0001;e.camera.far=e.radius*200;e.camera.updateProjectionMatrix();fitCamera(e);e.ready=true;updateScene(e,latest.current);setCount(e.meshes.length);setLoading(false);requestAnimationFrame(()=>{e.renderer.render(e.scene,e.camera);if(!dead)props.onReady?.(true);});
  }).catch(err=>{if(!dead){setError(err.message);setLoading(false);props.onReady?.(false);}});
  return()=>{dead=true;};
 },[props.url]);
 useEffect(()=>{const e=engine.current;if(e){e.explode=exploded;e.isolate=isolated;updateScene(e,props);e.renderer.render(e.scene,e.camera);}},[props.entryHint,props.time,props.selected,props.selectedIds,props.hiddenIds,props.units,props.steps,props.camera,props.autoCamera,exploded,isolated]);
 function fit(selected=false){const e=engine.current;if(!e)return;if(selected&&e.selection.visible){const center=e.selection.box.getCenter(new THREE.Vector3()),size=e.selection.box.getSize(new THREE.Vector3()).length();e.controls.target.copy(center);e.camera.position.copy(center).add(new THREE.Vector3(size*1.3,size,size*1.3));e.controls.update();}else fitCamera(e);}
 function saveCamera(){const e=engine.current;if(e)props.onCameraSave?.({position:e.camera.position.toArray(),target:e.controls.target.toArray(),coordinate_system:'viewer_m'});}
 return <div className={'scene '+(props.render?'render-scene':'')}><div className="scene-canvas" ref={host}/>{!props.render&&<><div className="viewport-label"><i/> LIVE 3D <span>{count?`${count} 배치`:'모델 뷰어'}</span></div><div className="viewport-tools"><button title="전체 보기" aria-label="전체 보기" onClick={()=>fit()}><Focus size={17}/></button><button title="선택 중심 보기" aria-label="선택 중심 보기" onClick={()=>fit(true)} disabled={!props.selected}><Box size={17}/></button><button title="분해 보기" aria-label="분해 보기" className={exploded?'active':''} onClick={()=>setExploded(!exploded)}><Layers3 size={17}/></button><button title="선택 부품만 보기" aria-label="선택 부품만 보기" className={isolated?'active':''} disabled={!props.selected} onClick={()=>setIsolated(!isolated)}><Eye size={17}/></button>{props.onCameraSave&&<button title="이 시점을 출력 카메라로 저장" aria-label="출력 카메라 저장" onClick={saveCamera}><Camera size={17}/></button>}<button title="표시 초기화" aria-label="표시 초기화" onClick={()=>{setExploded(false);setIsolated(false);fit();}}><RotateCcw size={17}/></button></div><div className="axis-widget"><span className="axis-z">Z</span><span className="axis-x">X</span><span className="axis-y">Y</span></div><div className="viewport-hint">드래그 회전 · 스크롤 확대 · Ctrl+클릭 다중 선택</div></>}{loading&&<div className="scene-state"><LoaderCircle className="spin" size={28}/><strong>CAD 형상을 불러오는 중</strong><span>모델의 실제 배치와 표시 형상을 준비합니다.</span></div>}{!loading&&!count&&<div className="scene-state"><Box size={42}/><strong>{error||'완제품 STEP을 등록하세요'}</strong><span>{error?'모델 검증 결과에서 원인을 확인할 수 있습니다.':'등록된 제품의 실제 3D 모델이 이곳에 표시됩니다.'}</span></div>}</div>;
}
function fitCamera(e:any){e.controls.target.set(0,0,0);e.camera.position.set(e.radius*1.6,e.radius*1.2,e.radius*1.6);e.controls.update();}
function updateScene(e:any,p:Props){
 const members=new Map<string,Row>();p.units.forEach(unit=>unitMembers(unit).forEach(id=>members.set(id,unit)));const states=evaluateSceneAt(p.steps,p.time),selected=new Set([p.selected,...(p.selectedIds||[])].filter(Boolean)),hidden=new Set(p.hiddenIds||[]);const box=new THREE.Box3();let hasSelection=false;
 e.meshes.forEach((mesh:THREE.Mesh)=>{
  const id=mesh.userData.occurrenceId,unit=members.get(id),chosen=selected.has(id)||selected.has(unit?.id),state=states.get(unit?.id);mesh.visible=(!e.isolate||chosen)&&!hidden.has(id)&&!hidden.has(unit?.id)&&(state?.visible??true);mesh.position.copy(state?convert(state.translation):new THREE.Vector3());
  if(e.explode){const delta=mesh.userData.center.clone().sub(e.center);if(delta.length()>.000001)mesh.position.add(delta.normalize().multiplyScalar(e.radius*.23));}
  const material=mesh.material as THREE.MeshStandardMaterial;material.color.copy(chosen?new THREE.Color('#b567f5'):mesh.userData.baseColor);material.emissive.set(chosen?'#5100a3':'#000000');material.emissiveIntensity=chosen?.35:0;mesh.updateMatrixWorld(true);if(chosen&&mesh.visible){box.expandByObject(mesh);hasSelection=true;}
 });
 e.selection.visible=hasSelection&&!p.render;if(hasSelection)e.selection.box.copy(box);
 // Diagram only: arrows indicate the proposed assembly vector, never a certificate.
 e.entryVisual.children.slice().forEach((object:THREE.Object3D)=>{e.entryVisual.remove(object);object.traverse((child:any)=>{child.geometry?.dispose();if(Array.isArray(child.material))child.material.forEach((m:any)=>m.dispose());else child.material?.dispose();});});
 if(!p.render&&p.time===null&&p.entryHint?.direction){
  const u=p.units.find(u=>u.id===p.entryHint!.unit_id),a=AXES[p.entryHint.direction],d=p.entryHint.distance_mm;
  if(u?.bbox?.center&&a&&Number.isFinite(d)){
   const end=convert(u.bbox.center).sub(e.center),start=end.clone().sub(convert(a).multiplyScalar(d));
   const arrow=new THREE.ArrowHelper(end.clone().sub(start).normalize(),start,end.distanceTo(start),0x7500ed,e.radius*.08,e.radius*.045);e.entryVisual.add(arrow);
   for(const [point,color] of [[start,0x7500ed],[end,0x444444]] as [THREE.Vector3,number][]){const dot=new THREE.Mesh(new THREE.SphereGeometry(e.radius*.018,10,8),new THREE.MeshBasicMaterial({color}));dot.position.copy(point);e.entryVisual.add(dot);}
  }
 }
 const stage=p.time===null?null:stageAt(p.steps,p.time),camera=p.camera||stage?.step.camera;
 if(camera?.position&&camera?.target){e.camera.position.fromArray(camera.position);e.controls.target.fromArray(camera.target);e.controls.update();}
 else if(p.autoCamera&&stage){
  const memberIds=new Set(unitMembers(p.units.find(unit=>unit.id===stage.step.unit_id))),fitBox=new THREE.Box3();
  e.meshes.forEach((mesh:THREE.Mesh)=>{
   if(mesh.visible)fitBox.expandByObject(mesh);
   if(memberIds.has(mesh.userData.occurrenceId)){
    const currentBox=new THREE.Box3().setFromObject(mesh);
    fitBox.union(currentBox);
    // World-aligned mesh motion is translation only; preserve its final target in frame.
    fitBox.union(currentBox.clone().translate(mesh.position.clone().negate()));
   }
  });
  if(!fitBox.isEmpty()){
   const target=fitBox.getCenter(new THREE.Vector3()),radius=Math.max(fitBox.getSize(new THREE.Vector3()).length()/2,e.radius*.04);
   const verticalHalf=THREE.MathUtils.degToRad(e.camera.fov)/2,horizontalHalf=Math.atan(Math.tan(verticalHalf)*e.camera.aspect);
   const distance=radius/Math.sin(Math.min(verticalHalf,horizontalHalf))*1.12;
   const axis=convert(AXES[stage.step.direction]||[0,0,1]).normalize(),offset=new THREE.Vector3(1.5,1.1,1.5).addScaledVector(axis,.4).normalize().multiplyScalar(distance);
   e.controls.maxDistance=Math.max(e.radius*30,distance*1.2);e.camera.far=Math.max(e.radius*200,distance+radius*2);e.camera.updateProjectionMatrix();
   e.controls.target.copy(target);e.camera.position.copy(target).add(offset);e.controls.update();
  }
 }
}

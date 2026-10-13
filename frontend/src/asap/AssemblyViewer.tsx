import { useEffect, useRef, useState } from 'react';
import { Box, Focus, Layers3, Move3d } from 'lucide-react';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import type { Boundary, DisplayScene, Json, ViewerMode } from './types';
import { engineToDisplay, interpolatedDisplay, VIEW_MATRIX } from './playback';

interface Props {
  cad: DisplayScene | null; obj: DisplayScene | null; mode: ViewerMode;
  selected: string | null; isolate: boolean; showFuture: boolean; showPath: boolean;
  selectedIds?: string[];
  focusToken?: number;
  boundary: Boundary | null; progress: number; coordinateMap?: Json;
  onSelect: (id: string, additive?: boolean) => void;
}
interface Context {
  renderer: THREE.WebGLRenderer; scene: THREE.Scene; camera: THREE.PerspectiveCamera;
  controls: OrbitControls; cadGroup: THREE.Group; objGroup: THREE.Group; pathGroup: THREE.Group;
  grid: THREE.GridHelper; fit: () => void;
}
function disposeGroup(group: THREE.Group) {
  for (const item of [...group.children]) {
    group.remove(item);
    item.traverse(node => {
      const mesh = node as THREE.Mesh;
      mesh.geometry?.dispose();
      const materials = mesh.material ? (Array.isArray(mesh.material) ? mesh.material : [mesh.material]) : [];
      materials.forEach(material => material.dispose());
    });
  }
}
function createMeshes(data: DisplayScene | null, group: THREE.Group) {
  disposeGroup(group);
  data?.meshes.forEach(item => {
    if (!item.positions?.length) return;
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(item.positions, 3));
    if (item.indices?.length) geometry.setIndex(item.indices);
    if (item.normals?.length === item.positions.length) geometry.setAttribute('normal', new THREE.Float32BufferAttribute(item.normals, 3));
    else geometry.computeVertexNormals();
    geometry.applyMatrix4(VIEW_MATRIX);
    geometry.computeBoundingBox();
    const color = Array.isArray(item.color) ? new THREE.Color(...item.color.slice(0, 3) as [number, number, number]) : new THREE.Color(item.color ?? '#98a8b9');
    const material = new THREE.MeshStandardMaterial({ color, roughness: .62, metalness: .22, side: THREE.DoubleSide });
    const mesh = new THREE.Mesh(geometry, material);
    mesh.name = item.id; mesh.userData = { id: item.id, originalColor: color.clone() };
    mesh.matrixAutoUpdate = false;
    group.add(mesh);
  });
}
export default function AssemblyViewer(props: Props) {
  const container = useRef<HTMLDivElement>(null), ctx = useRef<Context | null>(null), selectRef = useRef(props.onSelect);
  const [error, setError] = useState('');
  selectRef.current = props.onSelect;
  useEffect(() => {
    const host = container.current!;
    let renderer: THREE.WebGLRenderer;
    try { renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: 'high-performance' }); }
    catch { setError('3D 화면을 초기화할 수 없습니다. 브라우저의 하드웨어 가속 및 WebGL 지원을 확인하세요.'); return; }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setClearColor('#edf1f5'); renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.domElement.setAttribute('aria-label', '조립체 3D 보기');
    host.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(40, 1, .01, 100000);
    camera.position.set(300, 230, 350);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true; controls.dampingFactor = .08;
    scene.add(new THREE.HemisphereLight('#ffffff', '#a0adbc', 2.3));
    const light = new THREE.DirectionalLight('#ffffff', 2.8); light.position.set(150, 350, 200); scene.add(light);
    const fill = new THREE.DirectionalLight('#dce9fc', 1.4); fill.position.set(-200, 100, -200); scene.add(fill);
    const cadGroup = new THREE.Group(), objGroup = new THREE.Group(), pathGroup = new THREE.Group();
    scene.add(cadGroup, objGroup, pathGroup);
    const grid = new THREE.GridHelper(600, 24, '#c5d0dc', '#dce3eb');
    (grid.material as THREE.Material).transparent = true; (grid.material as THREE.Material).opacity = .5;
    scene.add(grid);
    const axes = new THREE.AxesHelper(40); scene.add(axes);
    function fit() {
      const box = new THREE.Box3();
      [cadGroup, objGroup].filter(group => group.visible).forEach(group => group.children.forEach(child => {
        const mesh = child as THREE.Mesh;
        if (mesh.visible && mesh.geometry.boundingBox) box.union(mesh.geometry.boundingBox.clone().applyMatrix4(mesh.matrix));
      }));
      if (box.isEmpty()) cadGroup.children.forEach(child => { const mesh = child as THREE.Mesh; if (mesh.geometry.boundingBox) box.union(mesh.geometry.boundingBox.clone()); });
      if (box.isEmpty()) return;
      const center = box.getCenter(new THREE.Vector3()), size = box.getSize(new THREE.Vector3()), span = Math.max(size.x, size.y, size.z, 1);
      controls.target.copy(center);
      camera.position.copy(center).add(new THREE.Vector3(1.2, .9, 1.35).normalize().multiplyScalar(span * 2.05));
      camera.near = Math.max(span / 10000, .001); camera.far = span * 100; camera.updateProjectionMatrix();
      controls.minDistance = span / 100; controls.maxDistance = span * 20;
      grid.position.set(center.x, box.min.y - span * .018, center.z); grid.scale.setScalar(span / 450);
      axes.position.copy(center); axes.scale.setScalar(span / 350);
      controls.update();
    }
    ctx.current = { renderer, scene, camera, controls, cadGroup, objGroup, pathGroup, grid, fit };
    const resize = new ResizeObserver(() => {
      const w = Math.max(host.clientWidth, 1), h = Math.max(host.clientHeight, 1);
      renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix();
    });
    resize.observe(host);
    let down = new THREE.Vector2();
    const onDown = (event: PointerEvent) => { down.set(event.clientX, event.clientY); };
    const onUp = (event: PointerEvent) => {
      if (down.distanceTo(new THREE.Vector2(event.clientX, event.clientY)) > 5 || event.button !== 0) return;
      const rect = renderer.domElement.getBoundingClientRect();
      const ray = new THREE.Raycaster();
      ray.setFromCamera(new THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1), camera);
      const groups = [cadGroup, objGroup].filter(group => group.visible);
      const objects = groups.flatMap(group => group.children.filter(mesh => mesh.visible));
      const hit = ray.intersectObjects(objects).find(hit => hit.object.userData.id);
      if (hit) selectRef.current(hit.object.userData.id, event.ctrlKey || event.metaKey);
    };
    renderer.domElement.addEventListener('pointerdown', onDown); renderer.domElement.addEventListener('pointerup', onUp);
    let frame = 0;
    const render = () => { frame = requestAnimationFrame(render); controls.update(); renderer.render(scene, camera); };
    render();
    return () => {
      cancelAnimationFrame(frame); resize.disconnect(); controls.dispose();
      renderer.domElement.removeEventListener('pointerdown', onDown); renderer.domElement.removeEventListener('pointerup', onUp);
      disposeGroup(cadGroup); disposeGroup(objGroup); disposeGroup(pathGroup); grid.geometry.dispose(); (grid.material as THREE.Material).dispose();
      axes.geometry.dispose(); (Array.isArray(axes.material) ? axes.material : [axes.material]).forEach(material => material.dispose());
      renderer.dispose(); host.removeChild(renderer.domElement); ctx.current = null;
    };
  }, []);
  useEffect(() => { if (ctx.current) { createMeshes(props.cad, ctx.current.cadGroup); createMeshes(props.obj, ctx.current.objGroup); ctx.current.fit(); } }, [props.cad, props.obj]);
  useEffect(() => {
    const context = ctx.current; if (!context) return;
    const motion = props.mode === 'assembly' || props.mode === 'disassembly', boundary = props.boundary;
    context.cadGroup.visible = props.mode !== 'obj'; context.objGroup.visible = props.mode === 'obj' || props.mode === 'overlay';
    [context.cadGroup, context.objGroup].forEach(group => group.children.forEach(node => {
      const mesh = node as THREE.Mesh, material = mesh.material as THREE.MeshStandardMaterial, id = mesh.userData.id;
      const current = motion && boundary?.moving === id;
      const installed = !motion || !!boundary?.installed.has(id);
      const fixed = motion && !!boundary?.fixed.has(id);
      const selected = props.selectedIds ? props.selectedIds.includes(id) : props.selected === id;
      mesh.visible = (!props.isolate || selected) && (!motion || installed || (current && boundary?.hasPath) || props.showFuture);
      mesh.matrix.identity();
      if (motion && boundary) {
        mesh.matrix.copy(current && boundary.hasPath ? interpolatedDisplay(boundary.path, props.progress, props.coordinateMap) : engineToDisplay(boundary.pose, props.coordinateMap));
      }
      const ghost = motion && !installed && !current;
      const overlay = props.mode === 'overlay' && group === context.objGroup;
      material.color.copy(mesh.userData.originalColor);
      if (overlay) material.color.set('#009caf');
      if (ghost) material.color.set('#bcc6d2');
      if (fixed) material.color.set('#29a38e');
      if (current) material.color.set('#e8a045');
      if (selected && !overlay) material.color.set(props.selectedIds?.length===2 && props.selectedIds[1]===id ? '#e89538' : '#548bf0');
      material.transparent = ghost || overlay;
      material.opacity = ghost ? .13 : overlay ? .5 : 1;
      material.depthWrite = !(ghost || overlay);
      material.wireframe = overlay;
      material.emissive.set(selected ? '#142c50' : '#000000');
      mesh.updateMatrixWorld(true);
    }));
  }, [props.mode, props.selected, props.selectedIds, props.isolate, props.showFuture, props.boundary, props.progress, props.coordinateMap, props.cad, props.obj]);
  useEffect(() => { if (props.focusToken) ctx.current?.fit(); }, [props.focusToken]);
  useEffect(() => {
    const context = ctx.current; if (!context) return;
    disposeGroup(context.pathGroup);
    const boundary = props.boundary;
    if (!props.showPath || !boundary?.hasPath || !['assembly', 'disassembly'].includes(props.mode)) return;
    const source = props.cad?.meshes.find(mesh => mesh.id === boundary.moving);
    if (!source) return;
    const box = new THREE.Box3().setFromArray(source.positions), center = box.getCenter(new THREE.Vector3()).applyMatrix4(VIEW_MATRIX);
    const points = boundary.path.map(frame => center.clone().applyMatrix4(engineToDisplay(frame, props.coordinateMap)));
    const geometry = new THREE.BufferGeometry().setFromPoints(points);
    const line = new THREE.Line(geometry, new THREE.LineBasicMaterial({ color: '#df8c26', depthTest: false, transparent: true, opacity: .9 }));
    line.renderOrder = 20; context.pathGroup.add(line);
    if (points.length) {
      const dotGeometry = new THREE.BufferGeometry().setFromPoints([points[0], points[points.length - 1]]);
      const dots = new THREE.Points(dotGeometry, new THREE.PointsMaterial({ color: '#c77619', size: 8, sizeAttenuation: false, depthTest: false }));
      dots.renderOrder = 21; context.pathGroup.add(dots);
    }
  }, [props.showPath, props.boundary?.step, props.mode, props.coordinateMap, props.cad]);
  return <div className="viewer">
    <div ref={container} className="viewer-canvas" />
    <div className="viewer-tools"><button className="icon-button" title="전체 형상 맞춤" onClick={() => ctx.current?.fit()}><Focus size={17} /></button><span>드래그 회전 · 우클릭 이동 · 휠 확대</span></div>
    <div className="view-axis"><Move3d size={15} /> CAD mm · Z-up</div>
    {(!props.cad || !props.cad.meshes.length) && !error && <div className="viewer-empty"><div className="empty-icon"><Box size={34} /></div><h2>완제품에서 조립순서를 확인합니다</h2><p>STEP을 등록하고 부품별 OBJ 변환을 검증하세요.<br />실제 ASAP 결과가 연결되면 여기에서 경로를 재생합니다.</p><div className="empty-flow"><span>STEP</span><i>→</i><span>OBJ</span><i>→</i><span>ASAP</span><i>→</i><span>3D 순서</span></div></div>}
    {error && <div className="viewer-empty error"><p>{error}</p></div>}
    {props.mode === 'overlay' && <div className="view-note"><Layers3 size={15} /> 회색 원본 · 청록 OBJ 와이어</div>}
    {props.selectedIds?.length===2 && props.mode==='cad' && <div className="view-note">A 파랑: {props.cad?.meshes.find(m=>m.id===props.selectedIds![0])?.name}<br/>B 주황: {props.cad?.meshes.find(m=>m.id===props.selectedIds![1])?.name}</div>}
    {['assembly', 'disassembly'].includes(props.mode) && <div className="legend"><span><i className="dot amber" />이동 부품</span><span><i className="dot green" />지지 부품</span><span><i className="dot blue" />선택 배치</span></div>}
  </div>;
}

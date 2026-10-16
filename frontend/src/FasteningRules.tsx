import {useEffect,useState} from 'react';
import {ArrowDown,ArrowUp,Plus,Save,Search,Trash2} from 'lucide-react';
import {api,post,rows,unitMembers} from './api';
import type {Row} from './api';
import {Badge,Field,Notice} from './panels';

export default function FasteningRules({project,busy,onSave}:{project:Row,busy:boolean,onSave:(fields:Row)=>Promise<any>}){
 const rules=project.rules||{},units=rows(project.model?.units),groups=rows(rules.process_groups),policy=rules.process_policy||{};
 const [editing,setEditing]=useState<Row|null>(null),[proposal,setProposal]=useState<Row|null>(null),[error,setError]=useState(''),[loading,setLoading]=useState(false);
 useEffect(()=>{setProposal(null);setEditing(null);},[project.process_profile_hash]);
 const name=(id:string)=>{const u=units.find(item=>item.id===id);if(!u)return id;const members=rows(project.model?.occurrences).filter(o=>unitMembers(u).includes(o.id));return `${u.name} · ${members.length===1?members[0].name:`${members.length}개 배치`}`;};
 const fasteners=units.filter(unit=>['fastener','bolt','nut','washer','screw'].includes(unit.role));
 const selected:string[]=editing?.fastener_unit_ids||[];
 const candidates=rows(proposal?.process_groups||proposal?.groups||proposal?.rules?.process_groups);
 async function applyProposal(){
  setError('');setLoading(true);
  try{
   const current=await api(`/projects/${project.id}/process-suggestions`);
   if(current.dependency_hash!==project.process_profile_hash)throw new Error('부품 정보가 변경되었습니다. 최신 모델에서 체결품 후보를 다시 확인해 주세요.');
   await onSave({process_groups:current.process_groups,process_policy:current.process_policy,precedence:[...rows(rules.precedence),...rows(current.precedence).filter(edge=>!rows(rules.precedence).some(e=>e.before===edge.before&&e.after===edge.after))]});
  }catch(e:any){setError(e.message);setProposal(null);}finally{setLoading(false);}
 }
 function setOrder(ids:string[]){setEditing(current=>({...current,fastener_unit_ids:ids,ordered_fastener_groups:ids.map(id=>[id])}));}
 function sortByHeight(){
  const height=(id:string)=>units.find(u=>u.id===id)?.bbox?.center?.[2]||0;
  const uncertainty=(id:string)=>Math.max(0,...rows(project.model?.occurrences).filter(o=>unitMembers(units.find(u=>u.id===id)).includes(o.id)).map(o=>Number.isFinite(o.representation_uncertainty_mm)?o.representation_uncertainty_mm:0));
  const ordered=[...selected].sort((a,b)=>height(b)-height(a)),batches:string[][]=[];
  for(const id of ordered){const last=batches.at(-1),anchor=last?.[0];if(last&&anchor&&Math.abs(height(id)-height(anchor))<=Math.max(.1,uncertainty(id)+uncertainty(anchor)))last.push(id);else batches.push([id]);}
  setEditing(current=>({...current,fastener_unit_ids:ordered,ordered_fastener_groups:batches}));
 }
 async function saveGroup(event:React.FormEvent<HTMLFormElement>){
  event.preventDefault();if(!editing)return;
  const form=new FormData(event.currentTarget),mode=String(form.get('fixation_mode'));
  const group={...editing,id:editing.id||crypto.randomUUID(),target_unit_id:String(form.get('target')),fixation_mode:mode,
   fastener_unit_ids:mode==='separate_fasteners'?selected:[],ordered_fastener_groups:mode==='separate_fasteners'?(editing.ordered_fastener_groups||selected.map(id=>[id])):[],
   retaining_cover_unit_id:mode==='cover_seated'?String(form.get('retaining_cover_unit_id')):undefined,
   basis:String(form.get('basis')||''),status:form.get('confirmed')==='on'?'confirmed':'review'};
  const next=[...groups.filter(g=>g.target_unit_id!==group.target_unit_id&&g.id!==group.id),group];
  const required=[...new Set([...(policy.required_unit_ids||groups.map(g=>g.target_unit_id)),group.target_unit_id])];
  if(await onSave({process_groups:next,process_policy:{...policy,required_unit_ids:required}}))setEditing(null);
 }
 return <section className="card"><div className="card-heading"><h3>부품 배치 후 고정 체결</h3><span className="muted">{groups.length}개 부품</span></div><div className="card-body">
  <Notice>지정한 부품을 배치한 뒤 연결된 체결품을 완료해야 다음 부품으로 넘어갑니다. 볼트·와셔·너트의 실제 고정 대상과 체결 순서를 확인해 주세요.</Notice>
  <label className="review-checkbox"><input type="checkbox" checked={!!policy.require_fixing_before_next_component} disabled={busy} onChange={event=>onSave({process_policy:{...policy,require_fixing_before_next_component:event.target.checked,required_unit_ids:policy.required_unit_ids||groups.map(g=>g.target_unit_id)}})}/><span>지정 부품의 고정 완료 후 다음 부품 배치</span></label>
  <div className="rule-list">{groups.map(group=><div key={group.id||group.target_unit_id}><button className="text-button" onClick={()=>setEditing({...group})}>{name(group.target_unit_id)} · {group.fastener_unit_ids?.length||0}개 체결품</button>{group.status==='assumed'?<span className="muted">임시 공정 가정 · 현장 확인 필요</span>:<Badge status={group.status==='review'?'review_required':'reviewed'}/>} <button className="icon-button" aria-label="고정 규칙 삭제" disabled={busy} onClick={()=>onSave({process_groups:groups.filter(g=>g!==group),process_policy:{...policy,required_unit_ids:(policy.required_unit_ids||[]).filter((id:string)=>id!==group.target_unit_id)}})}><Trash2 size={16}/></button></div>)}</div>
  <div className="page-actions"><button className="button small" disabled={busy||!units.length} onClick={()=>setEditing({target_unit_id:'',fastener_unit_ids:[],fixation_mode:'separate_fasteners',status:'review',basis:''})}><Plus size={15}/>고정 대상 지정</button><button className="button small" disabled={busy||loading||!units.length} onClick={async()=>{setLoading(true);setError('');try{const prefix=`/projects/${project.id}/process-suggestions`;const job=await post(prefix,{expected_revision:project.revision});const deadline=Date.now()+420000;while(Date.now()<deadline){await new Promise(resolve=>setTimeout(resolve,1500));const value=await api(`/jobs/${job.id}`);if(value.status==='succeeded'){setProposal(await api(prefix));return;}if(['failed','canceled','interrupted'].includes(value.status))throw new Error(value.message||'체결품 후보 검사 실패');}throw new Error('체결품 후보 검사 시간 초과');}catch(e:any){setError(e.message);}finally{setLoading(false);}}}><Search size={15}/>체결품 후보 확인</button></div>
  {error&&<Notice warning>{error}</Notice>}
  {proposal&&<details open><summary>고정 대상 후보 · 적용 전 확인</summary><p className="muted">위치상 가까운 체결품은 후보입니다. 확인된 고정 그룹과 검토 대기를 구분합니다. 검토 대기 부품은 근거를 확인하기 전 실행하지 않습니다.</p><button className="button small" disabled={busy||loading} onClick={applyProposal}>공정 기준·체결 후보 적용</button>{candidates.map(group=><button className="button small" key={group.target_unit_id} onClick={()=>setEditing({...group})}>{name(group.target_unit_id)} · {group.status==='assumed'?'임시 공정 가정':group.status==='review'?'검토 대기':'형상 근거 확인'}</button>)}</details>}
  {editing&&<form className="contact-form" key={editing.id||editing.target_unit_id||'new'} onSubmit={saveGroup}>
   <Field label="고정할 부품"><select name="target" value={editing.target_unit_id||''} required onChange={event=>setEditing({...editing,target_unit_id:event.target.value})}><option value="">부품 선택</option>{units.filter(u=>!fasteners.includes(u)).map(u=><option key={u.id} value={u.id}>{name(u.id)}</option>)}</select></Field>
   <Field label="고정 방법"><select name="fixation_mode" value={editing.fixation_mode||'separate_fasteners'} onChange={event=>setEditing({...editing,fixation_mode:event.target.value})}><option value="separate_fasteners">별도 볼트·와셔·너트 체결</option><option value="self_fixed">공급 조립품 내 고정 또는 자체 고정</option><option value="not_required">별도 고정 작업 불필요</option><option value="cover_seated">홈에 배치 · 커버 조립으로 고정</option></select></Field>
   {editing.fixation_mode==='separate_fasteners'&&<><details><summary>연결할 체결품 선택 · {selected.length}개</summary><div className="check-list">{fasteners.filter(u=>u.id!==editing.target_unit_id).map(u=><label key={u.id}><input type="checkbox" checked={selected.includes(u.id)} onChange={event=>setOrder(event.target.checked?[...selected,u.id]:selected.filter(id=>id!==u.id))}/><span>{name(u.id)}</span></label>)}</div></details><p className="muted">아래 순서대로 체결합니다. 릴레이는 높이가 높은 고정 위치부터 체결하며, 높이가 비슷한 체결품끼리는 순서를 강제하지 않습니다.</p><button type="button" className="button small" onClick={sortByHeight}>높은 위치 우선 · 0.1 mm 이내는 동순위</button><p className="muted">동순위 묶음: {(editing.ordered_fastener_groups||[]).map((batch:string[],index:number)=>`${index+1}순위 ${batch.length}개`).join(' → ')}</p><div className="manual-plan-list">{selected.map((id,index)=><div key={id}><span>{index+1}</span><strong>{name(id)}</strong>{[-1,1].map(delta=><button key={delta} type="button" className="icon-button" aria-label={delta<0?'체결 순서 앞으로':'체결 순서 뒤로'} disabled={index+delta<0||index+delta>=selected.length} onClick={()=>{const next=[...selected];[next[index],next[index+delta]]=[next[index+delta],next[index]];setOrder(next);}}>{delta<0?<ArrowUp size={15}/>:<ArrowDown size={15}/>}</button>)}</div>)}</div></>}
   {editing.fixation_mode==='cover_seated'&&<Field label="배치 후 고정할 커버"><select name="retaining_cover_unit_id" defaultValue={editing.retaining_cover_unit_id||''} required><option value="">커버 선택</option>{units.filter(u=>u.id!==editing.target_unit_id&&!fasteners.includes(u)).map(u=><option key={u.id} value={u.id}>{name(u.id)}</option>)}</select></Field>}
   {editing.assumption&&<Notice warning>기본 공정 가정입니다. 실제 고정 방식을 확인한 결과가 아니며 현장 검토 후 수정할 수 있습니다.</Notice>}<Field label="고정 대상·순서 확인 근거"><textarea name="basis" defaultValue={editing.basis||''} required placeholder="도면, 공정 기준, 실제 고정 위치의 확인 내용"/></Field>
   <label className="review-checkbox"><input name="confirmed" type="checkbox" defaultChecked={['confirmed','verified_geometry','user_confirmed'].includes(editing.status)}/><span>고정 대상과 체결 순서를 확인했습니다.</span></label>
   <div className="page-actions"><button type="button" className="button small" onClick={()=>setEditing(null)}>닫기</button><button className="button primary small" disabled={busy}><Save size={15}/>고정 규칙 저장</button></div>
  </form>}
 </div></section>;
}

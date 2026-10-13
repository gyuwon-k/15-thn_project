"""Reference STEP registration and bounded, untrained rigid surface matching."""
from __future__ import annotations
import itertools,time,uuid,hashlib
from pathlib import Path
import numpy as np
from scipy.spatial import cKDTree
from asap_service.backend.storage import read_json,write_json,now
from asap_service.backend import unit_service

def catalog(directory):return read_json(Path(directory)/'templates.json',{'files':[],'templates':[]})

def parse_templates(directory,files):
    from asap_service.backend.cad_pipeline import parse_step
    data=catalog(directory)
    for item in files:
        folder=Path(directory)/'templates'/item['id']
        entry={**item,'status':'parsing'};data['files'].append(entry);write_json(Path(directory)/'templates.json',data)
        try:
            result=parse_step(folder,folder/'source/model.step')
            units=read_json(folder/'units.json');scene=read_json(folder/'display_scene.json')
            if len(scene['meshes'])!=len(units):raise ValueError('TEMPLATE_GEOMETRY_INCOMPLETE: 모든 기준 형상을 표시할 수 없습니다.')
            template={'id':item['id'],'file_id':item['id'],'name':item['name'],'status':'ready','part_number':'',
                      'scope_ids':[u['id'] for u in units],'solid_count':sum(u['solid_count'] for u in units),
                      'geometry_hash':result['manifest']['source']['sha256'],'matching_status':'not_matched'}
            data['templates'].append(template);entry.update(status='ready',diagnostics=result['diagnostics'])
        except Exception as exc:entry.update(status='failed',error={'reason_code':'TEMPLATE_PARSE_FAILED','message':str(exc)})
        write_json(Path(directory)/'templates.json',data)
    return {'status':'templates_ready','ready_count':sum(t['status']=='ready' for t in data['templates']),
            'failed_count':sum(f['status']=='failed' for f in data['files'])}

def mesh_arrays(meshes):
    vertices=[];faces=[];offset=0
    for mesh in meshes:
        v=np.asarray(mesh['positions'],float).reshape(-1,3);f=np.asarray(mesh['indices'],int).reshape(-1,3)
        vertices.append(v);faces.append(f+offset);offset+=len(v)
    if not faces:raise ValueError('MATCH_GEOMETRY_EMPTY: 비교할 형상이 없습니다.')
    return np.concatenate(vertices),np.concatenate(faces)

def surface(vertices,faces):
    tri=vertices[faces];cross=np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]);area=np.linalg.norm(cross,axis=1)/2
    keep=area>1e-12;tri,area,cross=tri[keep],area[keep],cross[keep]
    if not len(area):raise ValueError('MATCH_GEOMETRY_EMPTY: 유효한 면이 없습니다.')
    total=area.sum();s=tri.sum(axis=1);center=np.sum(s/3*area[:,None],axis=0)/total
    moment=(np.einsum('nki,nkj->nij',tri,tri)+np.einsum('ni,nj->nij',s,s))/12
    covariance=np.sum(moment*area[:,None,None],axis=0)/total-np.outer(center,center)
    eig,basis=np.linalg.eigh(covariance);bases=[basis]
    normals=cross/(2*area[:,None]);order=np.argsort(area)[::-1]
    n1=normals[order[0]]
    n2=next((normals[i] for i in order[:2000] if abs(np.dot(normals[i],n1))<.9),None)
    if n2 is not None:
        n2=n2-n1*np.dot(n1,n2);n2/=np.linalg.norm(n2);bases.append(np.column_stack((n1,n2,np.cross(n1,n2))))
    return {'tri':tri,'area':area,'center':center,'eig':eig,'bases':bases,'surface_area':float(total),
            'radius':float(np.max(np.linalg.norm(tri.reshape(-1,3)-center,axis=1))),
            'tree':cKDTree(tri.mean(axis=1))}

def sample(s,count=512):
    rng=np.random.default_rng(0);ids=rng.choice(len(s['area']),count,p=s['area']/s['area'].sum())
    u=rng.random((count,2));a=np.sqrt(u[:,0]);t=s['tri'][ids]
    return (1-a)[:,None]*t[:,0]+(a*(1-u[:,1]))[:,None]*t[:,1]+(a*u[:,1])[:,None]*t[:,2]

def nearest(points,s):
    k=min(32,len(s['tri']));_,ids=s['tree'].query(points,k=k)
    if k==1:ids=ids[:,None]
    t=s['tri'][ids];p=points[:,None,:];a,b,c=t[:,:,0],t[:,:,1],t[:,:,2];ab=b-a;ac=c-a;ap=p-a
    normal=np.cross(ab,ac);nn=np.einsum('nki,nki->nk',normal,normal)
    project=p-normal*(np.einsum('nki,nki->nk',ap,normal)/nn)[:,:,None]
    d00=np.einsum('nki,nki->nk',ab,ab);d01=np.einsum('nki,nki->nk',ab,ac);d11=np.einsum('nki,nki->nk',ac,ac)
    d20=np.einsum('nki,nki->nk',project-a,ab);d21=np.einsum('nki,nki->nk',project-a,ac)
    den=d00*d11-d01*d01;u=(d11*d20-d01*d21)/den;v=(d00*d21-d01*d20)/den
    inside=(u>=0)&(v>=0)&(u+v<=1)
    options=[]
    for x,y in [(a,b),(b,c),(c,a)]:
        edge=y-x;f=np.einsum('nki,nki->nk',p-x,edge)/np.einsum('nki,nki->nk',edge,edge)
        options.append(x+edge*np.clip(f,0,1)[:,:,None])
    distances=[np.linalg.norm(p-q,axis=2) for q in options];which=np.argmin(np.stack(distances,axis=2),axis=2)
    edge=np.take_along_axis(np.stack(options,axis=2),which[:,:,None,None],axis=2)[:,:,0,:]
    closest=np.where(inside[:,:,None],project,edge);dist=np.linalg.norm(p-closest,axis=2);best=np.argmin(dist,axis=1)
    return closest[np.arange(len(points)),best],dist[np.arange(len(points)),best]

def align(template,target,tolerance,deadline):
    cloud=sample(template);back=sample(target);poses=[]
    for left in template['bases']:
        for right in target['bases']:
            for permutation in itertools.permutations(range(3)):
                for signs in itertools.product([-1,1],repeat=3):
                    rotation=right[:,permutation]@np.diag(signs)@left.T
                    if np.linalg.det(rotation)<.99:continue
                    translation=target['center']-rotation@template['center']
                    _,dist=nearest(cloud[:96]@rotation.T+translation,target)
                    poses.append((float(np.mean(dist)),rotation,translation))
    poses.sort(key=lambda x:x[0]);best=None
    for _,rotation,translation in poses[:4]:
        for _ in range(15):
            if time.monotonic()>deadline:raise TimeoutError('MATCH_TIME_LIMIT: 매칭 시간 한도 초과')
            moved=cloud@rotation.T+translation;closest,dist=nearest(moved,target)
            mask=dist<=np.quantile(dist,.95);x=moved[mask];y=closest[mask]
            mx=x.mean(axis=0);my=y.mean(axis=0);u,_,vh=np.linalg.svd((x-mx).T@(y-my));r=vh.T@u.T
            if np.linalg.det(r)<0:vh[-1]*=-1;r=vh.T@u.T
            shift=my-r@mx;rotation=r@rotation;translation=r@translation+shift
            if np.linalg.norm(shift)<1e-6 and np.linalg.norm(r-np.eye(3))<1e-6:break
        _,forward=nearest(cloud@rotation.T+translation,target)
        _,reverse=nearest((back-translation)@rotation,template)
        distances=np.r_[forward,reverse];metric={'p95_surface_distance_mm':float(np.quantile(distances,.95)),
            'max_sample_distance_mm':float(distances.max()),'sample_coverage':float(np.mean(distances<=tolerance)),
            'rms_distance_mm':float(np.sqrt(np.mean(distances**2))),'sample_count':len(distances),'tolerance_mm':tolerance,
            'method':'area_covariance_and_face_normal_multistart_rigid_surface_ICP','finite_samples_only':True}
        matrix=np.eye(4);matrix[:3,:3]=rotation;matrix[:3,3]=translation
        if best is None or metric['p95_surface_distance_mm']<best[0]['p95_surface_distance_mm']:best=(metric,matrix)
    return best

def match(directory,template_ids,expected_revision,tolerance_mm=.5,timeout_s=120):
    directory=Path(directory);current=unit_service.state(directory)
    if current['revision']!=expected_revision:raise ValueError('REVISION_CONFLICT: 현재 단위를 다시 확인하세요.')
    data=catalog(directory);selected=[t for t in data['templates'] if t['id'] in template_ids and t['status']=='ready']
    if len(selected)!=len(set(template_ids)):raise ValueError('TEMPLATES_REQUIRED: 사용 가능한 기준 파일을 선택하세요.')
    job_id=uuid.uuid4().hex;folder=directory/'matching'/job_id;folder.mkdir(parents=True)
    original=read_json(directory/'units.json');raw=read_json(directory/'display_scene.json');mesh_map={m['id']:m for m in raw['meshes']}
    source={u['id']:u for u in original};sets={tuple([u['id']]) for u in original}
    active=read_json(unit_service.unit_directory(directory)/'units.json')
    for u in active:
        if all('solid_indices' not in m for m in u['member_refs']):sets.add(tuple(sorted(m['occurrence_id'] for m in u['member_refs'])))
    nodes=read_json(directory/'manifest.json')['nodes'];node_map={n['id']:n for n in nodes}
    def leaves(n):
        if n['id'] in source:return [n['id']]
        return [leaf for child in n['children'] for leaf in leaves(node_map[child])]
    for n in nodes:
        if n.get('is_assembly'):
            members=leaves(n)
            if 1<len(members)<=30:sets.add(tuple(sorted(members)))
    candidates=[];summaries=[];cache={};deadline=time.monotonic()+timeout_s
    for template in selected:
        geom=read_json(directory/'templates'/template['file_id']/'display_scene.json')
        tm=surface(*mesh_arrays([m for m in geom['meshes'] if m['id'] in template['scope_ids']]))
        candidate_sets=set(sets)
        # Flat STEP trees may split one supplier part across unrelated leaves.
        # Enumerate only small spatial neighborhoods with the exact solid count.
        if template['solid_count']>1:
            eligible=[u for u in original if u.get('bbox') and 0<u['solid_count']<=template['solid_count']]
            centers=np.asarray([u['bbox']['center'] for u in eligible])
            if len(centers):
                tree=cKDTree(centers);added=0
                for i,anchor in enumerate(eligible):
                    neighbors=tree.query_ball_point(centers[i],2*tm['radius']+tolerance_mm)
                    neighbors=sorted(neighbors,key=lambda j:np.linalg.norm(centers[j]-centers[i]))[:12]
                    def visit(start,chosen,total):
                        nonlocal added
                        if added>=300 or time.monotonic()>deadline:return
                        if total==template['solid_count']:
                            if len(chosen)>1:candidate_sets.add(tuple(sorted(eligible[j]['id'] for j in chosen)));added+=1
                            return
                        if len(chosen)>=12:return
                        for k in range(start,len(neighbors)):
                            j=neighbors[k]
                            if total+eligible[j]['solid_count']<=template['solid_count']:
                                visit(k+1,chosen+[j],total+eligible[j]['solid_count'])
                    visit(0,[],0)
                    if added>=300:break
        found=0
        for refs in sorted(candidate_sets):
            if time.monotonic()>deadline:break
            if any(id not in mesh_map for id in refs):continue
            solid_count=sum(source[id]['solid_count'] for id in refs)
            if solid_count!=template['solid_count']:continue
            if refs not in cache:cache[refs]=surface(*mesh_arrays([mesh_map[id] for id in refs]))
            target=cache[refs]
            if abs(target['surface_area']/tm['surface_area']-1)>.12:continue
            if not np.allclose(target['eig'],tm['eig'],rtol=.15,atol=tolerance_mm**2):continue
            try:metrics,matrix=align(tm,target,tolerance_mm,deadline)
            except TimeoutError:break
            if metrics['sample_coverage']<.98 or metrics['p95_surface_distance_mm']>tolerance_mm:continue
            found+=1;candidate={'id':uuid.uuid4().hex,'template_id':template['id'],'template_name':template['name'],
                'part_number':template.get('part_number',''),'member_refs':[{'occurrence_id':id} for id in refs],
                'member_names':[source[id]['name'] for id in refs],'template_to_cad_matrix':matrix.ravel().tolist(),
                'metrics':metrics,'status':'review_required','conflicts':[]}
            candidates.append(candidate)
        limited=time.monotonic()>deadline
        summaries.append({'template_id':template['id'],'candidate_count':found,'status':'time_limited' if limited else 'review_required' if found else 'not_matched',
                          'reason':'시간 한도로 매칭을 완료하지 못했습니다.' if limited else None if found else '허용 오차 안의 전체 형상 대응 없음. 범위·버전·단위 분할 또는 수동 대응을 확인하세요.'})
    for a,b in itertools.combinations(candidates,2):
        if set(m['occurrence_id'] for m in a['member_refs']) & set(m['occurrence_id'] for m in b['member_refs']):
            a['conflicts'].append(b['id']);b['conflicts'].append(a['id'])
    result={'id':job_id,'unit_revision':expected_revision,'candidates':candidates,'templates':summaries,
            'timeout_reached':time.monotonic()>deadline,'created_at':now(),'settings':{'tolerance_mm':tolerance_mm,'timeout_s':timeout_s},
            'original_geometry_modified':False,'automatic_confirmation':False}
    write_json(folder/'candidates.json',result);data['last_match_id']=job_id
    for t in data['templates']:
        summary=next((x for x in summaries if x['template_id']==t['id']),None)
        if summary:t.update(matching_status=summary['status'],candidate_count=summary['candidate_count'])
    write_json(directory/'templates.json',data)
    return {'status':'match_review_required','match_id':job_id,'candidate_count':len(candidates),'timeout_reached':result['timeout_reached']}

def accept(directory,match_id,candidate_ids,expected_revision):
    from asap_service.backend.storage import checked_id
    result=read_json(Path(directory)/'matching'/checked_id(match_id)/'candidates.json')
    if result['unit_revision']!=expected_revision:raise ValueError('REVISION_CONFLICT: 매칭 후보의 단위 리비전이 다릅니다.')
    selected=[c for c in result['candidates'] if c['id'] in candidate_ids]
    if len(selected)!=len(set(candidate_ids)) or not selected:raise ValueError('MATCH_CANDIDATE_INVALID: 후보를 선택하세요.')
    return unit_service.apply_templates(directory,expected_revision,selected)

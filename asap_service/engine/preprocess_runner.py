"""Validate full source coverage, qualify each body in an isolated native worker."""
from __future__ import annotations
import json,copy,subprocess,os,time,shutil,sys
from pathlib import Path

def prepare_isolated(adapter,assembly_dir,asset_folder,parameters,deadline,audit_path,emit,expected_revision,
                     expected_source_sha256,scale,native_provenance,engine_root,body_timeout_s=240,
                     verified_cache_reuse=None):
    assembly=Path(assembly_dir).resolve();audit_path=Path(audit_path)
    manifest=adapter.validate_manifest(assembly,expected_revision,expected_source_sha256)
    workspace=assembly.parent/'native_preflight';workspace.mkdir(exist_ok=True)
    report={'capability':adapter.CAPABILITY,'adapter_sha256':adapter.sha256(adapter.__file__),
        'manifest_sha256':adapter.sha256(assembly/'solids_manifest.json'),
        'preprocess_version':adapter.PREPROCESS_VERSION,'source_sha256':manifest['source_sha256'],
        'conversion_revision':manifest['conversion_revision'],'source_occurrence_count':manifest['source_occurrence_count'],
        'source_solid_count':manifest['source_solid_count'],'input_unit_count':len(manifest['bodies']),
        'original_solid_count':0,'prepared_body_count':0,'failed_body_count':0,'composed_body_count':0,'bvh_body_count':0,
        'source_unchanged':True,'planner_source_unchanged':True,'status':'preparing','bodies':{},'failures':[],
        'process_isolation':{'enabled':True,'body_timeout_s':body_timeout_s,'failed_parts_omitted':False},
        'native_core_unchanged':native_provenance.get('native_core_unchanged'),
        'native_source_patches':native_provenance.get('native_source_patches',[])}
    for key in ('redmax_core_sha256','base_redmax_core_sha256','native_binary_sha256','guarded_binary_verification'):
        if key in native_provenance:
            report[key]=copy.deepcopy(native_provenance[key])
    def persist():adapter.write_json(audit_path,report)
    persist()
    for part,body in sorted(manifest['bodies'].items()):
        adapter.check_deadline(deadline)
        folder=workspace/part;folder.mkdir(exist_ok=True);input_dir=folder/'input';input_dir.mkdir(exist_ok=True)
        shutil.copy2(assembly/(part+'.obj'),input_dir/(part+'.obj'))
        for component in body['components']:
            destination=input_dir/component['path'];destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(assembly/component['path'],destination)
        # Carry the verified fields into the isolated worker, not merely the
        # parent staging directory. Each worker still reloads native geometry,
        # frame/mass and current CAD occupancy probes before reporting success.
        eligible = {part+'.obj', *(component['path'] for component in body['components'])}
        worker_reuse = {'status':'complete','verified_cache_objects':{},'verified_cache_sha256':{}}
        if verified_cache_reuse and verified_cache_reuse.get('status') == 'complete':
            for relative, record in verified_cache_reuse.get('verified_cache_objects',{}).items():
                if relative not in eligible:
                    continue
                cache_relative = record.get('cache_path')
                if not isinstance(cache_relative,str):
                    raise adapter.SolidSDFError('CACHE_REUSE_INVALID','검증된 필드 캐시 경로가 없습니다.')
                source_cache=(assembly/cache_relative).resolve()
                source_obj=(assembly/relative).resolve()
                if (not source_cache.is_relative_to(assembly) or not source_obj.is_relative_to(assembly)
                    or adapter.sha256(source_obj)!=record.get('obj_sha256')
                    or adapter.sha256(source_cache)!=record.get('cache_sha256')):
                    raise adapter.SolidSDFError('CACHE_REUSE_INVALID','격리 작업자로 전달할 검증 캐시의 경로/해시가 다릅니다.')
                target=(input_dir/cache_relative).resolve()
                if not target.is_relative_to(input_dir.resolve()):
                    raise adapter.SolidSDFError('CACHE_REUSE_INVALID','격리 캐시 경로가 작업 폴더 밖입니다.')
                target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source_cache,target)
                worker_reuse['verified_cache_objects'][relative]=record
                worker_reuse['verified_cache_sha256'][cache_relative]=record['cache_sha256']
        subset=copy.deepcopy(manifest);count=len(body['components'])
        subset.update(bodies={part:body},source_occurrence_count=1,exported_occurrence_count=1,
                      source_solid_count=count,exported_solid_count=count)
        adapter.write_json(input_dir/'solids_manifest.json',subset)
        remaining=min(float(body_timeout_s),deadline-time.monotonic())
        if remaining<=0:adapter.check_deadline(deadline)
        request={'part':part,'engine_root':str(engine_root),'assembly_dir':str(input_dir),'asset_folder':str(asset_folder),
                 'parameters':parameters,'deadline':time.monotonic()+remaining,'expected_revision':expected_revision,
                 'source_sha256':expected_source_sha256,'scale':scale,'native_provenance':native_provenance,
                 'audit_path':str(folder/'audit.json')}
        request['verified_cache_reuse']=worker_reuse
        adapter.write_json(folder/'request.json',request)
        emit('native_unit_preflight','조립 단위별 네이티브 사전시험',part=part,unit_id=body['unit_id'],
             prepared_body_count=report['prepared_body_count'],input_unit_count=report['input_unit_count'])
        cmd=[sys.executable,'-u',str(Path(__file__).with_name('native_body_worker.py')),'--request',str(folder/'request.json'),'--output',str(folder/'result.json')]
        kwargs={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
        started=time.monotonic();failure=None
        with (folder/'worker.log').open('wb') as log:
            process=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT,**kwargs)
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill();process.wait()
                failure={'reason_code':'NATIVE_PREFLIGHT_TIMEOUT','message':'단위별 네이티브 준비 제한시간 초과'}
        raw=json.loads((folder/'result.json').read_text(encoding='utf-8')) if (folder/'result.json').is_file() else {}
        if not failure and (process.returncode!=0 or not raw.get('ok')):
            failure={'reason_code':raw.get('reason_code','NATIVE_PREFLIGHT_CRASH'),
                     'message':raw.get('message','네이티브 사전시험 프로세스 종료'),
                     'exit_code':process.returncode,'exit_hex':hex(process.returncode & 0xffffffff)}
        if failure:
            tail=(folder/'worker.log').read_text(encoding='utf-8',errors='replace')[-6000:]
            failure.update(part=part,unit_id=body['unit_id'],elapsed_s=time.monotonic()-started,log_ref=str(folder/'worker.log'),log_tail=tail)
            report['failures'].append(failure);report['failed_body_count']+=1
            emit('native_unit_preflight_failed',failure['message'],**{k:v for k,v in failure.items() if k not in ['message','log_tail']})
        else:
            prepared=raw['report'];verified=prepared['bodies'][part]
            if verified.get('status')!='passed':raise adapter.SolidSDFError('NATIVE_PREFLIGHT_INVALID','단위 검증 결과가 불완전합니다.')
            # Copy only generated field caches; meshes and source manifest stay immutable.
            for cache in input_dir.rglob('*.sdf'):
                relative=cache.relative_to(input_dir);destination=assembly/relative;destination.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(cache,destination)
            report['bodies'][part]=verified
            report.setdefault('verified_cache_reuse',{'native_field_reload_count':0,'current_occupancy_verification_skipped':False})
            report['verified_cache_reuse']['native_field_reload_count']+=prepared.get('verified_cache_reuse',{}).get('native_field_reload_count',0)
            report['original_solid_count']+=prepared['original_solid_count'];report['prepared_body_count']+=1
            report['composed_body_count']+=prepared['composed_body_count'];report['bvh_body_count']+=prepared['bvh_body_count']
            for key in ['mass_scope','voxel_accuracy_scope','bvh_accuracy_scope','native_query_roundtrip_tolerance_mm']:
                if key in prepared:report[key]=prepared[key]
            emit('native_unit_ready','조립 단위 네이티브 준비 완료',part=part,unit_id=body['unit_id'],prepared_body_count=report['prepared_body_count'],
                 input_unit_count=report['input_unit_count'],elapsed_s=time.monotonic()-started)
        persist()
    report['status']='failed' if report['failures'] else 'passed';persist()
    if report['failures']:
        raise adapter.SolidSDFError('UNIT_PREPROCESS_FAILED',f"{report['failed_body_count']}개 조립 단위의 네이티브 사전시험을 완료하지 못했습니다. 형상은 제외하지 않았습니다.")
    if report['original_solid_count']!=manifest['source_solid_count']:
        raise adapter.SolidSDFError('ORIGINAL_SOLID_COUNT_MISMATCH','단위별 사전시험에서 원본 솔리드 수량이 일치하지 않습니다.')
    return report

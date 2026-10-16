"""Replay actual failed worker results through the production publication path.

Input job folders are read-only. All writes use a new isolated storage directory.
This verifies persistence, not fresh CAD import or geometry certification.
"""
from pathlib import Path
import argparse
import copy
import hashlib
import json
import sys
import threading
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import domain
from backend.main import Services,release_info
from backend.storage import Storage,dumps


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--job',type=Path,action='append',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    folder=args.output.parent/('replay-'+uuid.uuid4().hex);folder.mkdir(parents=True)
    store=Storage(folder/'data').init();release=release_info()
    report={'build_id':release['build_id'],'scope':'Saved failing results replayed through Services.apply_result; no geometry recomputation or live data changes.',
        'actual_database_packet_bytes':store.server.packet_limit,'cases':[]}
    # Enforce the old 16 MiB boundary as well as the production 64 MiB server.
    store.server.packet_limit=16*1024**2
    try:
        service=Services.__new__(Services);service.root=store.data_dir;service.store=store
        service.lock=threading.RLock();service.release=release
        for source in args.job:
            raw_result=(source/'result.json').read_bytes();request=json.loads((source/'request.json').read_text(encoding='utf-8'))
            result=json.loads(raw_result)['result'];original=request['payload']['project']
            project=copy.deepcopy(original);project['id']=domain.uid('project-')
            project=store.create_project(project)
            before_validation=copy.deepcopy(result['validation'])
            attempts=copy.deepcopy(result['plan']['attempt_trace'])
            decisions=copy.deepcopy(result['plan'].get('decision_trace'))
            ident=domain.uid('job-')
            job={'id':ident,'kind':'generate','project_id':project['id'],'input_revision':project['revision'],
                'geometry_hash':domain.geometry_hash(project),'model_hash':domain.model_hash(project)}
            published=service.apply_result(job,result);saved=store.get_project(project['id'])
            assert 'attempt_trace' not in saved['plan']
            reference=saved['plan']['attempt_trace_artifact'];path=store.file_path(reference['file_id'],verify=True)
            retained=json.loads(path.read_text(encoding='utf-8'))
            assert retained['attempts']==attempts
            assert saved['plan']['decision_trace']==decisions
            assert saved['validation']['steps']==before_validation['steps']
            assert saved['validation']['status']==before_validation['status']=='BLOCKED'
            assert len(saved['plan']['steps'])==128
            assert domain.confirmation_errors(saved,release)
            with store.connect() as conn,conn.cursor() as cursor:
                command=cursor.mogrify('UPDATE project SET row_version=%s,body=%s,updated_at=%s WHERE id=%s',
                    (saved['row_version'],dumps(saved),saved['updated_at'],saved['id']))
                command_bytes=len(command.encode(conn.encoding))
            assert command_bytes<16*1024**2
            report['cases'].append({'source_job':source.name,'input_result_sha256':hashlib.sha256(raw_result).hexdigest(),
                'result_file_bytes':len(raw_result),'attempt_count':len(attempts),'trace_sha256':reference['sha256'],
                'trace_bytes':reference['size'],'saved_project_bytes':len(dumps(saved).encode('utf-8')),
                'escaped_update_command_bytes':command_bytes,'legacy_16_mib_boundary_passed':True,
                'steps':len(saved['plan']['steps']),'validation_status':saved['validation']['status'],
                'diagnostic_and_learning_evidence_preserved':True,'published':published,'project_id':saved['id']})
        backup=store.backup();Storage.validate_backup(backup['path'])
        restored_path=folder/'restored';store.restore(backup['path'],restored_path)
        restored=Storage(restored_path).init()
        try:
            for case in report['cases']:
                p=restored.get_project(case['project_id']);reference=p['plan']['attempt_trace_artifact']
                path=restored.file_path(reference['file_id'],verify=True)
                assert len(json.loads(path.read_text(encoding='utf-8'))['attempts'])==case['attempt_count']
                assert hashlib.sha256(path.read_bytes()).hexdigest()==case['trace_sha256']
        finally:restored.close()
        report.update(status='PASS',backup_and_restore_complete_trace_verified=True,test_directory=str(folder))
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False,indent=2))
    finally:store.close()


if __name__=='__main__':main()

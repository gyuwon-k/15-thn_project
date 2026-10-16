import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import pymysql
from backend.main import Services
from backend.storage import Storage, StorageError, dumps


class StoragePublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root=Path(__file__).resolve().parents[1]/'work/storage-tests'
        root.mkdir(parents=True,exist_ok=True)
        cls.temp=tempfile.TemporaryDirectory(prefix='publication-',dir=root)
        cls.store=Storage(Path(cls.temp.name)/'data').init()

    @classmethod
    def tearDownClass(cls):
        cls.store.close();cls.temp.cleanup()

    def test_complete_large_trace_is_registered_without_changing_proof_or_learning(self):
        service=Services.__new__(Services);service.root=self.store.data_dir;service.store=self.store;service.release={'build_id':'test'}
        project=self.store.create_project({'name':'Trace preservation'})
        trace=[{'index':i,'status':'COLLISION','detail':'한글\\"'*2048} for i in range(1024)]
        plan={'attempt_trace':trace,'steps':[{'id':'s1','path':None,'status':'UNKNOWN'}],
            'decision_trace':[{'selected_unit_id':None}],'algorithm_version':'unchanged'}
        original=copy.deepcopy(plan);self.assertGreater(len(dumps(trace).encode('utf-8')),16*1024**2)
        service.preserve_search_trace(plan,{'id':'job-test','project_id':project['id'],'geometry_hash':'g'})
        self.assertNotIn('attempt_trace',plan)
        record=plan['attempt_trace_artifact'];path=self.store.file_path(record['file_id'],verify=True)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['attempts'],trace)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),record['sha256'])
        self.assertEqual(plan['steps'],original['steps']);self.assertEqual(plan['decision_trace'],original['decision_trace'])
        self.assertEqual(plan['attempt_trace_summary']['status_counts'],{'COLLISION':1024})
        project['plan']=plan;saved=self.store.save_project(project,project['row_version'])
        self.assertEqual(self.store.get_project(saved['id'])['plan'],plan)

    def test_artifact_failure_keeps_complete_trace_for_retry(self):
        service=Services.__new__(Services);service.root=self.store.data_dir;service.store=Mock();service.release={'build_id':'test'}
        service.store.register_file.side_effect=OSError('disk publication failed')
        plan={'attempt_trace':[{'status':'UNKNOWN'}]}
        with self.assertRaises(OSError):service.preserve_search_trace(plan,{'id':'job-failed','project_id':'p','geometry_hash':'g'})
        self.assertEqual(plan,{'attempt_trace':[{'status':'UNKNOWN'}]})

    def test_escaped_command_is_rejected_before_send_and_transaction_keeps_old_row(self):
        project=self.store.create_project({'name':'Previous revision'})
        with self.assertRaises(StorageError) as caught:
            with self.store.connect() as conn,conn.cursor() as cursor:
                conn.pilot_packet_limit=4096
                cursor.execute('UPDATE project SET body=%s WHERE id=%s',('\\'*2200,project['id']))
        self.assertEqual(caught.exception.code,'STORAGE_PAYLOAD_TOO_LARGE')
        self.assertGreater(caught.exception.details['query_bytes'],4096)
        self.assertEqual(self.store.get_project(project['id'])['name'],'Previous revision')

    def test_failed_rollback_does_not_replace_original_database_error(self):
        original=pymysql.OperationalError(2006,'connection reset')
        conn=Mock();conn.rollback.side_effect=pymysql.InterfaceError(0,'')
        with patch('backend.storage.pymysql.connect',return_value=conn):
            with self.assertRaises(StorageError) as caught:
                with self.store.connect():raise original
        self.assertEqual(caught.exception.code,'STORAGE_CONNECTION_FAILED')
        self.assertIs(caught.exception.__cause__,original)
        self.assertEqual(caught.exception.details['database_error_code'],2006)
        self.assertIn('rollback',original.__notes__[0])
        self.assertNotEqual(str(caught.exception),"(0, '')")


if __name__=='__main__':unittest.main()

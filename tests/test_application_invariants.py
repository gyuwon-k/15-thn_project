import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import contextmanager
from backend import domain
from backend.jobs import JobService,atomic_json,path_values
from backend.learning import build_dataset,decision_id,preference_review,reviewed_histories,train_ranker
from backend.worker import run_worker


@contextmanager
def temporary_directory():
    test_root=(Path(__file__).resolve().parents[1]/"work"/"test-temp").resolve()
    test_root.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=test_root) as folder:
        assert Path(folder).resolve().is_relative_to(test_root)
        yield folder


def fixture():
    p=domain.new_project({"name":"Test"})
    p["model"]={"fingerprint":"shape-v1","quality":{"status":"PASS"},"occurrences":[
        {"id":"a","bbox":{"min":[0,0,0],"max":[2,2,2]},"collision_ready":True},
        {"id":"b","bbox":{"min":[4,0,0],"max":[5,1,1]},"collision_ready":True}],
        "units":[{"id":"u-a","occurrence_ids":["a"],"bbox":{"min":[0,0,0],"max":[2,2,2]}},
                 {"id":"u-b","occurrence_ids":["b"],"bbox":{"min":[4,0,0],"max":[5,1,1]}}]}
    p["rules"]["fixed_unit_ids"]=["u-a"]
    p["plan"]["steps"]=[{"id":"s-b","unit_id":"u-b","direction":"-Z","distance_mm":20,"reviewed":True,"torque_status":"not_applicable","caution_reviewed":True,"path":{"kind":"translation","start_translation_mm":[0,0,20],"end_translation_mm":[0,0,0],"verified":True}}]
    p["validation"]={"status":"PASS","dependency_hash":domain.geometry_hash(p)}
    return p


class ApplicationInvariantTests(unittest.TestCase):
    def test_empty_caution_requires_explicit_none_review(self):
        p=fixture();p["plan"]["steps"][0]["caution_reviewed"]=False
        self.assertTrue(any("주의사항" in reason for reason in domain.confirmation_errors(p)))
        p["plan"]["steps"][0]["caution"]="실제 공정 주의사항"
        self.assertEqual(domain.confirmation_errors(p),[])
    def test_video_publication_does_not_unconfirm_unchanged_plan(self):
        p=fixture();signature=domain.confirmation_hash(p)
        p["revision"]+=1;p["exports"].append({"file_id":"video"});p["updated_at"]="later"
        self.assertEqual(domain.confirmation_hash(p),signature)
        p["plan"]["steps"][0]["caution"]="Updated process information"
        self.assertNotEqual(domain.confirmation_hash(p),signature)
    def test_another_build_requires_revalidation(self):
        p=fixture();p["validation"].update(build_id="old",algorithm_version="a1")
        self.assertTrue(domain.confirmation_errors(p,{"build_id":"new","algorithm_version":"a1"}))
        self.assertEqual(domain.confirmation_errors(p,{"build_id":"old","algorithm_version":"a1"}),[])

    def test_required_torque_accepts_unit_object_and_rejects_nonfinite_or_wrong_units(self):
        p=fixture();step=p["plan"]["steps"][0];step["torque_status"]="required"
        for value in ({"value":3,"unit":"N·m"},3):
            step["torque"]=value;self.assertEqual(domain.confirmation_errors(p),[])
        for value in ({"value":3,"unit":"lb·ft"},float("inf"),float("nan"),True,0,None):
            step["torque"]=value;self.assertTrue(domain.confirmation_errors(p))

    def test_idempotency_key_cannot_reuse_different_work(self):
        service=JobService.__new__(JobService);service.lock=threading.RLock();service.stopping=False
        project=fixture();payload={"value":1}
        prior={"request_id":"r1","request_hash":domain.digest({"kind":"generate","payload":payload,"project_revision":project["revision"]})}
        class Stored:
            def list_jobs(self,ident):return [prior]
        service.storage=Stored()
        self.assertEqual(service.submit(project,"generate",payload,"r1"),prior)
        with self.assertRaises(domain.DomainError) as error:service.submit(project,"generate",{"value":2},"r1")
        self.assertEqual(error.exception.code,"REQUEST_ID_CONFLICT")

    def test_validated_plan_becomes_unconfirmable_after_direction_edit(self):
        p=fixture();self.assertEqual(domain.confirmation_errors(p),[])
        steps=copy.deepcopy(p["plan"]["steps"]);steps[0]["direction"]="+X"
        edited=domain.patch_project(p,{"expected_revision":1,"plan":{"steps":steps}})
        self.assertIsNone(edited["validation"])
        self.assertFalse(edited["plan"]["steps"][0]["path"]["verified"])
        self.assertTrue(domain.confirmation_errors(edited))

    def test_old_pass_fingerprint_cannot_confirm_modified_geometry(self):
        p=fixture();p["rules"]["directions"]=["+X"]
        self.assertTrue(any("변경" in item for item in domain.confirmation_errors(p)))

    def test_part_number_preserves_leading_zero_and_label_does_not_invalidate_geometry(self):
        p=fixture();edited=domain.patch_project(p,{"expected_revision":1,"part_number":"00123"})
        self.assertEqual(edited["part_number"],"00123")
        self.assertEqual(domain.confirmation_errors(edited),[])

    def test_stale_edit_rejected(self):
        with self.assertRaises(domain.DomainError) as result:
            domain.patch_project(fixture(),{"expected_revision":0,"name":"old"})
        self.assertEqual(result.exception.code,"REVISION_CONFLICT")

    def test_duplicate_or_missing_unit_members_are_rejected(self):
        p=fixture()
        for units in ([{"id":"one","occurrence_ids":["a","a","b"]}], [{"id":"one","occurrence_ids":["a"]}]):
            with self.assertRaises(domain.DomainError):domain.check_units(p["model"],units)

    def test_client_bounds_cannot_remove_an_obstacle(self):
        p=fixture();units=copy.deepcopy(p["model"]["units"])
        units[0]["bbox"]={"min":[1000,1000,1000],"max":[1001,1001,1001]}
        result=domain.patch_project(p,{"expected_revision":1,"units":units})
        self.assertEqual(result["model"]["units"][0]["bbox"]["min"],[0,0,0])

    def test_precedence_cycle_rejected(self):
        p=fixture();rules={**p["rules"],"precedence":[{"before":"u-a","after":"u-b"},{"before":"u-b","after":"u-a"}]}
        with self.assertRaises(domain.DomainError):domain.check_rules(p["model"],rules)

    def test_managed_reference_cannot_escape_storage(self):
        with temporary_directory() as folder:
            with self.assertRaises(domain.DomainError):path_values("@data/../../outside",Path(folder))

    def test_windows_transient_read_lock_does_not_abort_publication(self):
        with temporary_directory() as folder:
            output=Path(folder)/"progress.json";original=Path.replace;count=[0]
            def transient(path,target):
                count[0]+=1
                if count[0]==1:raise PermissionError("simulated Windows sharing violation")
                return original(path,target)
            with patch.object(Path,"replace",transient):atomic_json(output,{"progress":50})
            self.assertEqual(json.loads(output.read_text()),{"progress":50})
            self.assertEqual(count[0],2)

    def test_worker_rejects_another_release(self):
        with temporary_directory() as folder:
            request=Path(folder)/"request.json"
            request.write_text(json.dumps({"folder":folder,"data_dir":folder,"payload":{},"kind":"import","release":{"build_id":"not-current"}}))
            self.assertEqual(run_worker(request),1)
            self.assertIn("WORKER_BUILD_MISMATCH",json.loads((Path(folder)/"result.json").read_text(encoding="utf-8"))["error"])

    def test_automatic_confirmation_is_not_expert_training_label(self):
        result=build_dataset([{"id":"snap","status":"CONFIRMED","project":fixture()}])
        self.assertEqual(result["eligible_confirmations"],[])
        self.assertEqual(result["excluded"][0]["reason"],"expert_preference_not_reviewed")

    def test_expert_review_and_grouped_learning_preserve_real_choices(self):
        snapshots=[];reviews=[]
        for index in range(6):
            state=fixture();state["part_number"]=f"family-{index%3}"
            trace={"installed_ids":[],"feasible_unit_ids":["u-a","u-b"],"candidates":[{"unit_id":"u-a","status":"PASS"},{"unit_id":"u-b","status":"PASS"}],"selected_unit_id":None}
            state["plan"]["decision_trace"]=[trace]
            snapshot={"id":f"s{index}","status":"CONFIRMED","source_hash":f"hash{index}","project":state}
            choice={"snapshot_id":snapshot["id"],"reviewer":"test fixture expert","reference":"synthetic preference fixture","product_family":state["part_number"],"selections":[{"decision_id":decision_id(trace),"selected_unit_id":"u-a"}]}
            reviews.append(preference_review(snapshot,choice));snapshots.append(snapshot)
        reviewed=reviewed_histories(snapshots,reviews)
        self.assertIsNone(snapshots[0]["project"]["plan"]["decision_trace"][0]["selected_unit_id"])
        self.assertEqual(len(build_dataset(reviewed)["eligible_confirmations"]),6)
        with temporary_directory() as folder:
            model=train_ranker(reviewed,{"minimum_cases":3},folder)
            self.assertEqual(model["status"],"SHADOW_EVALUATION")
            self.assertFalse(model["active"])
            self.assertFalse(set(model["heldout_groups"])&set(model["training_groups"]))
            self.assertTrue(Path(model["dataset_path"]).is_file())
            self.assertTrue(Path(model["model_path"]).is_file())
        bad={**choice,"selections":[{"decision_id":decision_id(trace),"selected_unit_id":"unknown"}]}
        with self.assertRaises(domain.DomainError):preference_review(snapshot,bad)


if __name__=="__main__":unittest.main()

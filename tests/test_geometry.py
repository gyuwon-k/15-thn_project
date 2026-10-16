import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.geometry import match_units,combine_bbox


def member(ident,name,center,definition=None):
    bbox={"min":[v-.5 for v in center],"max":[v+.5 for v in center],"size":[1,1,1],"center":center}
    return {"id":ident,"name":name,"definition_name":name,"definition_id":definition or name,"bbox":bbox,"solid_count":1,"face_count":6,
        "valid":True,"collision_ready":True,"volume_mm3":1,"area_mm2":6}


class MatchingTests(unittest.TestCase):
    def test_cad_only_units_have_no_template_certificate_without_becoming_failed_matches(self):
        from backend.domain import refresh_matching_quality
        assembly={"instances":[member("a","Independent",[0,0,0])]}
        units,diagnostics=match_units(assembly,[])
        self.assertEqual(units[0]["matching"]["geometry_verification"]["status"],"NOT_APPLICABLE")
        self.assertFalse(units[0]["matching"]["review_required"])
        model={"units":units,"diagnostics":diagnostics,"quality":{"inventory_complete":True}}
        refresh_matching_quality(model)
        self.assertTrue(model["quality"]["matching_ready"])
        model["diagnostics"].append({"code":"MATCH_AMBIGUOUS"})
        refresh_matching_quality(model)
        self.assertFalse(model["quality"]["matching_ready"])

    def test_constellation_pruning_resolves_many_repeated_members(self):
        centers=[[i*4.,(i%3)*7.,0.] for i in range(9)]
        assembly={"instances":[member(f"a{i}","Pin.1",[x+20,y+30,z+40],"pin") for i,(x,y,z) in enumerate(centers)]}
        template={"source":{"name":"Supplied_Group.STP"},"instances":[member(f"p{i}","Pin",c,"pin") for i,c in enumerate(centers)]}
        units,diagnostics=match_units(assembly,[template],max_combinations=2000)
        self.assertEqual(len(units),1,diagnostics)
        self.assertEqual(len(units[0]["occurrence_ids"]),9)
        self.assertEqual(units[0]["matching"]["status"],"matched")
    def test_same_count_different_constellation_is_not_grouped(self):
        assembly={"instances":[member("a","Pin",[0,0,0]),member("b","Pin",[20,0,0])]}
        template={"source":{"name":"Group.STP"},"instances":[member("p","Pin",[0,0,0]),member("q","Pin",[10,0,0])]}
        units,diagnostics=match_units(assembly,[template])
        self.assertEqual(len(units),2)
        self.assertTrue(all(u["matching"]["status"]=="cad_leaf" for u in units))
    def test_matching_does_not_merge_repeated_placements(self):
        assembly={"instances":[member("a","Bolt.1",[0,0,0],"bolt"),member("b","Bolt.2",[10,0,0],"bolt")]}
        template={"source":{"name":"Bolt.STP"},"instances":[member("t","Bolt",[0,0,0],"bolt")]}
        units,diagnostics=match_units(assembly,[template])
        self.assertEqual(len(units),2)
        self.assertNotEqual(units[0]["id"],units[1]["id"])
        self.assertEqual({o for u in units for o in u["occurrence_ids"]},{"a","b"})
    def test_ambiguous_different_definitions_require_review(self):
        assembly={"instances":[member("a","SimilarA",[0,0,0],"A"),member("b","SimilarB",[10,0,0],"B")]}
        template={"source":{"name":"Unknown.STP"},"instances":[member("t","Unknown",[0,0,0],"T")]}
        units,diagnostics=match_units(assembly,[template])
        self.assertEqual(len(units),2)
        self.assertTrue(any(d["code"]=="MATCH_AMBIGUOUS" for d in diagnostics))

    def test_exhausted_search_cannot_claim_unique_group_from_first_proved_candidate(self):
        import numpy as np
        class VerifiedShapes:
            def compare(self,source,target):
                matrix=np.eye(4);matrix[:3,3]=np.array(target["bbox"]["center"])-source["bbox"]["center"]
                return {"status":"PASS","source_to_target_world":matrix.reshape(-1).tolist()}
        assembly={"instances":[member("a","Pin",[0,0,0]),member("b","Pin",[10,0,0]),member("c","Pin",[100,0,0])]}
        template={"source":{"name":"Group.STP"},"instances":[member("p","Pin",[0,0,0]),member("q","Pin",[10,0,0])]}
        units,diagnostics=match_units(assembly,[template],max_combinations=4,shape_verifier=VerifiedShapes())
        group=next(u for u in units if u["matching"].get("template_name"))
        proof=group["matching"]["geometry_verification"]
        self.assertTrue(group["matching"]["review_required"])
        self.assertEqual(proof["status"],"REVIEW")
        self.assertEqual(proof["reason"],"MATCH_CANDIDATE_SEARCH_INCOMPLETE")
        self.assertTrue(all(row["status"]=="PASS" for row in proof["members"]))
        self.assertEqual({ident for unit in units for ident in unit["occurrence_ids"]},{"a","b","c"})
        self.assertTrue(any(d["code"]=="MATCH_SEARCH_LIMITED" for d in diagnostics))


if __name__=="__main__":unittest.main()

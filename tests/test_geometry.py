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


if __name__=="__main__":unittest.main()

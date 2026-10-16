import unittest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox,BRepPrimAPI_MakeCylinder
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
from OCP.Geom import Geom_Line,Geom_Circle
from OCP.gp import gp_Ax1,gp_Ax2,gp_Dir,gp_Pnt
from backend.analytic_matching import audit_correspondence,curve_bound


class AnalyticMatchingTests(unittest.TestCase):
    def test_copied_box_complete_support_and_trim_certificate(self):
        box=BRepPrimAPI_MakeBox(2,3,4).Shape()
        result=audit_correspondence(box,BRepBuilderAPI_Copy(box).Shape())
        self.assertEqual(result["status"],"PASS",result)
        self.assertEqual(result["metrics"]["support_surfaces"]["bounded"],6)
        self.assertEqual(result["metrics"]["placed_2d_trim_traces"]["bounded"],24)

    def test_copied_cylinder_has_complete_periodic_trim_evidence(self):
        cylinder=BRepPrimAPI_MakeCylinder(3,4).Shape()
        result=audit_correspondence(cylinder,BRepBuilderAPI_Copy(cylinder).Shape())
        self.assertEqual(result["status"],"PASS",result)

    def test_same_counts_different_extents_do_not_pass(self):
        result=audit_correspondence(BRepPrimAPI_MakeBox(2,3,4).Shape(),BRepPrimAPI_MakeBox(2,4,3).Shape())
        self.assertEqual(result["status"],"REVIEW")

    def test_line_bound_covers_whole_interval_not_only_origin(self):
        a=Geom_Line(gp_Ax1(gp_Pnt(0,0,0),gp_Dir(1,0,0)))
        b=Geom_Line(gp_Ax1(gp_Pnt(0,0,0),gp_Dir(1,.01,0)))
        self.assertGreater(curve_bound(a,b,(0.,100.),(0.,100.)),.99)

    def test_circle_different_trim_arc_never_passes_from_same_support(self):
        circle=Geom_Circle(gp_Ax2(gp_Pnt(0,0,0),gp_Dir(0,0,1)),3.)
        self.assertGreater(curve_bound(circle,circle,(0.,1.),(0.,2.)),2.9)


if __name__=="__main__":unittest.main()

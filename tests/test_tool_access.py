from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt
from backend.tool_access import check_tool


class FixtureGeometry:
    instances={'obstacle':{'valid':True}}
    def __init__(self,x):self.solid=BRepPrimAPI_MakeBox(gp_Pnt(x,-1,5),2,2,2).Shape()
    def shape(self,i):return self.solid


def context():
    return {'tools':[{'id':'t','type':'powered','envelope':{'radius_mm':2,'length_mm':20}}],
        'units':[{'id':'target','member_instance_ids':['moving']},{'id':'fixed','member_instance_ids':['obstacle']}]},\
        {'id':'op','kind':'fasten','tool_id':'t','target_unit_ids':['target'],'direction':'+Z','tool_contact_point_mm':[0,0,0]}


def test_cylinder_detects_known_obstacle_and_does_not_bypass_with_manual_review():
    s,op=context();op['access_review']={'status':'reviewed','reviewer':'test','reason':'test'}
    result=check_tool(s,op,['fixed'],FixtureGeometry(0))
    assert result[0]['status']=='collision'


def test_separated_tool_envelope_and_missing_information_are_distinct():
    s,op=context()
    assert check_tool(s,op,['fixed'],FixtureGeometry(10))[0]['status']=='passed'
    assert check_tool(s,op,['fixed'])[0]['status']=='review_required'

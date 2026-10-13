"""Explicit tool-envelope check; field approval remains distinct from simulation."""
import math
from . import domain


def check_tool(state,op,assembled,engine=None):
    if op.get('kind')!='fasten':return []
    tool=next((t for t in state.get('tools',[]) if t['id']==op.get('tool_id')),None)
    if not tool:return [domain.make_finding('TOOL_ACCESS','체결 공구가 지정되지 않았습니다.',op['id'])]
    if tool.get('type')=='none':return [domain.make_finding('TOOL_ACCESS','명시적으로 무공구 작업을 선택했습니다. 실제 작업 조건은 검토 기록을 확인하세요.',op['id'],'info')]
    point=op.get('tool_contact_point_mm');envelope=tool.get('envelope')
    manual=op.get('access_review',{})
    if not point or not envelope or engine is None:
        if manual.get('status')=='reviewed' and manual.get('reviewer') and manual.get('reason'):
            return [domain.make_finding('TOOL_ACCESS_MANUAL','검토자가 현장 접근성 검토를 기록했습니다. 공구 형상 시뮬레이션 통과와 별도입니다.',op['id'],'info',review=manual)]
        return [domain.make_finding('TOOL_ACCESS','공구 외형·접촉 위치·CAD 형상이 부족합니다. 치수를 입력하거나 현장 접근성 근거를 검토하세요.',op['id'],tool_id=tool['id'])]
    if len(point)!=3 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in point):
        return [domain.make_finding('TOOL_ACCESS','공구 접촉점은 원본 CAD mm 좌표 3개입니다.',op['id'],'invalid')]
    units={u['id']:u for u in state['units']}
    obstacle_ids=[m for uid in assembled if uid not in op.get('target_unit_ids',[]) and uid in units for m in units[uid]['member_instance_ids']]
    if any(i not in engine.instances or not engine.instances[i].get('valid') for i in obstacle_ids):
        return [domain.make_finding('TOOL_ACCESS','공구 접근 경로의 일부 장애물 CAD를 정밀 계산할 수 없습니다.',op['id'],'calculation_failed')]
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2,gp_Pnt,gp_Dir
    from .cad import AXES,static_interference
    axis=op.get('tool_axis') or op.get('direction','+Z')
    if axis not in AXES:return [domain.make_finding('TOOL_ACCESS','공구축 방향을 확인하세요.',op['id'],'invalid')]
    shape=BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*point),gp_Dir(*AXES[axis])),envelope['radius_mm'],envelope['length_mm']).Shape()
    details=[]
    for iid in obstacle_ids:
        result=static_interference(shape,engine.shape(iid));details.append({'instance_id':iid,**result})
    if any(x['status']=='collision' for x in details):status='collision'
    elif any(x['status'] not in ('separated','contact') for x in details):status='calculation_failed'
    else:status='passed'
    return [domain.make_finding('TOOL_ACCESS_ENVELOPE','입력한 접촉점/공구축의 원통 외형을 현재 조립체와 비교했습니다.',op['id'],status,tool_id=tool['id'],details=details,
        conditions={'method':'precise_BREP_static_cylinder_envelope','radius_mm':envelope['radius_mm'],'length_mm':envelope['length_mm'],'contact_point_mm':point,'axis':axis,
        'limitations':'원통 외형의 현재 자세 검사. 손·케이블·임의 회전·연속 공구 이동은 별도 검토'})]

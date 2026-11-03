import copy,json
from pathlib import Path
import pytest
from steel_engine.bim_contract import validate


def split_package():
    p=json.loads((Path(__file__).parent/'fixtures/synthetic_bim_package.json').read_text())
    original=p['physical']['members'][0]; aid='analysis-beam'
    a,b=original['axis']; middle=[(x+y)/2 for x,y in zip(a,b)]
    p['analysis']={'nodes':[{'id':'a','point':a},{'id':'b','point':b}],'elements':[{'id':aid,'node_ids':['a','b']}]}
    left=copy.deepcopy(original);right=copy.deepcopy(original)
    left.update(id='left',axis=[a,middle],analysis_ids=[],analysis_segments=[{'analysis_id':aid,'start':0,'end':.5}])
    right.update(id='right',axis=[middle,b],analysis_ids=[],analysis_segments=[{'analysis_id':aid,'start':.5,'end':1}])
    p['physical']['members']=[left,right]
    for j in p['physical']['joints']:
        for e in j['member_ends']: e['member_id']='left' if e['end']=='start' else 'right'
    p['physical']['joints'].append({'id':'splice','point':middle,'member_ends':[{'member_id':'left','end':'end'},{'member_id':'right','end':'start'}]})
    p['physical']['joint_dispositions'].append({'joint_id':'splice','reason':'Synthetic continuous test joint'})
    return p


def test_disjoint_analysis_intervals(): assert not validate(split_package())


@pytest.mark.parametrize('value,code',[(.4,'analysis_overlap'),(.6,'analysis_mapping')])
def test_analysis_interval_overlap_and_gap(value,code):
    p=split_package();p['physical']['members'][1]['analysis_segments'][0]['start']=value
    assert any(f['code']==code for f in validate(p))


def test_interior_support_attachment_and_bad_station():
    p=split_package();m=p['physical']['members'][0];a,b=m['axis'];point=[(x+y)/2 for x,y in zip(a,b)]
    p['physical']['joints'].append({'id':'attachment','point':point,'member_ends':[],'attachments':[{'member_id':'left','station':.5}]})
    p['physical']['joint_dispositions'].append({'joint_id':'attachment','reason':'Synthetic test attachment'})
    assert not validate(p)
    p['physical']['joints'][-1]['attachments'][0]['station']=.6
    assert any(f['code']=='joint_attachment' for f in validate(p))


def test_physical_contradictions_are_needs_input_despite_stored_ready_flag():
    p=json.loads((Path(__file__).parent/'fixtures/synthetic_bim_package.json').read_text())
    physical=p['physical'];p['readiness']['status']='ready'
    physical['profiles'].append(dict(id='2xTEST',family='built_up',dimensions={},status='declared',
        components=[dict(profile_id='TEST',offset=[0,0],rotation_deg=0)]))
    physical['connections'][0]['parts'][0]['geometry']['origin']=[1000,0,0]
    physical['panels']=[dict(id='wall',kind='wall',status='declared',material_id='steel',thickness=.5,
        vertices=[[0,0,0],[120,0,0],[120,0,120],[0,0,120]])]
    physical['openings']=[dict(id='door',host_id='wall',vertices=[[150,0,0],[180,0,0],[180,0,84],[150,0,84]])]
    before=copy.deepcopy(p);findings=validate(p)
    assert {'physical_ply_count','physical_opening_host','physical_remote_part'}<={f['code'] for f in findings}
    assert all(f['severity']=='needs_input' for f in findings)
    assert p==before


def test_opening_inside_host_bounds_but_off_host_plane_is_not_ready():
    p=json.loads((Path(__file__).parent/'fixtures/synthetic_bim_package.json').read_text())
    # A mistaken diagonal strip has the right bounding box but cannot host
    # the vertical end-wall window. This occurred in a fresh CFS IFC return.
    p['physical']['panels']=[dict(id='wall',kind='wall',status='declared',material_id='steel',thickness=6,
        vertices=[[-3,0,0],[3,0,0],[3,744,128],[-3,744,128]])]
    p['physical']['openings']=[dict(id='window',host_id='wall',vertices=[[0,132,36],[0,204,36],[0,204,84],[0,132,84]])]
    before=copy.deepcopy(p);findings=validate(p)
    assert any(f['code']=='physical_opening_host_plane' and f['severity']=='needs_input' for f in findings)
    assert not any(f['code']=='physical_opening_host' for f in findings)
    assert p==before
    p['physical']['panels'][0]['vertices']=[[0,0,0],[0,744,0],[0,744,128],[0,0,128]]
    assert not validate(p)


def test_sloped_host_accepts_only_coplanar_opening():
    p=json.loads((Path(__file__).parent/'fixtures/synthetic_bim_package.json').read_text())
    p['physical']['panels']=[dict(id='roof',kind='roof',status='declared',material_id='steel',thickness=.1,
        vertices=[[0,0,0],[120,0,60],[120,120,60],[0,120,0]])]
    p['physical']['openings']=[dict(id='opening',host_id='roof',vertices=[[20,20,10],[40,20,20],[40,40,20],[20,40,10]])]
    assert not validate(p)
    for point in p['physical']['openings'][0]['vertices']:point[2]+=1
    assert any(f['code']=='physical_opening_host_plane' for f in validate(p))

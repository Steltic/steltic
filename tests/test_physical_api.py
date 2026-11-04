"""Exercise native HTTP decisions and model replay without a paid model call."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from test_physical_inputs import native_job,P,MODULE
from steltic.physical_api import router
from steel_engine.physical_layout import split_member,wall,reservation
from steel_engine.physical_selection import record,selected


def test_http_answer_replay_and_idempotency(tmp_path):
    root=native_job(tmp_path);model=P.PhysicalModel(MODULE,P.read(root))
    model.request('Q','Confirm the connection clearance','Retain current clearances',fictional=True)
    model.save(root)
    app=FastAPI();app.include_router(router(lambda request:tmp_path));client=TestClient(app)
    register=client.get('/api/physical/job/requests').json()
    body=dict(request_id='Q',author='Test engineer',note='Accepted saved fictional answer',action='accept_test',
              decision_id='one',expected_revision=register['revision'],expected_sha256=register['sha256'])
    response=client.post('/api/physical/job/answers',json=body)
    assert response.status_code==200,response.text
    saved=response.json();retry=client.post('/api/physical/job/answers',json=body)
    assert retry.json()['duplicate'] and P.read(root)['revision']==saved['revision']
    assert client.post('/api/physical/job/answers',json=dict(body,note='different')).status_code==409
    export=client.post('/api/physical/job/replay',json=dict(expected_revision=saved['revision'],expected_sha256=saved['sha256']))
    assert export.status_code==200,export.text
    assert export.json()['analysis_executed'] is False and export.json()['readiness']['status']=='ready'
    assert client.post('/api/physical/job/replay',json=dict(expected_revision=register['revision'],expected_sha256=register['sha256'])).status_code==409
    assert client.get('/api/physical/missing/requests').status_code==404


def test_typed_source_selection_and_physical_splits(tmp_path):
    root=native_job(tmp_path);source=record(root,MODULE,'connections','c1')
    model=P.PhysicalModel(MODULE)
    selected(model,'connections','actual',source,dict(selected_description='/selection'),explicit=dict(joint_id='joint'))
    entity=model.value['declarations']['connections'][0]
    assert entity['design_record_ids']==['connections/c1']
    assert entity['selection_source']['source_pointer']=='/connections/0'
    with pytest.raises(ValueError,match='absent'):
        selected(model,'connections','missing',source,dict(plate_thickness='/unknown'))
    member=dict(id='column',kind='column',axis=[[0,0,0],[0,0,360]],local_y=[0,1,0],profile_id='TEST',material_id='steel',
                end_offsets=[[0,0,1],[0,0,-1]],reference_line='centroid',analysis_ids=['A1'])
    split_member(model,member,[144,288])
    pieces=model.value['declarations']['members']
    assert [p['analysis_segments'][0]['end'] for p in pieces]==[.4,.8,1]
    assert pieces[0]['end_offsets']==[[0,0,1],[0,0,0]] and pieces[-1]['end_offsets']==[[0,0,0],[0,0,-1]]
    with pytest.raises(ValueError):split_member(model,member,[288,144])


def test_wall_layout_and_unhosted_reservation_are_not_analysis_proxies():
    model=P.PhysicalModel(MODULE)
    selection=dict(profile_id='C600',material_id='cfs',reference_line='web centre',end_offsets=[[0,0,0],[0,0,0]])
    wall(model,'W',[[0,0,0],[96,0,0]],108,[0,16,32,48,64,80,96],stud=selection,track=selection,normal=[0,1,0])
    rows=model.value['declarations']['members']
    assert len(rows)==9 and rows[1]['axis']==[[16,0,0],[16,0,108]]
    assert rows[1]['local_y']==[1,0,0] # depth across wall, flanges along wall
    assert rows[-1]['axis']==[[0,0,108],[96,0,108]]
    reservation(model,'DOOR',[[24,0,0],[60,0,0],[60,0,84],[24,0,84]],request_id='D1',responsible_party='Architect')
    assert not model.value['declarations']['openings']
    assert model.value['declarations']['interfaces'][0]['vertices'][1]==[60,0,0]


def test_equivalent_wall_group_preserves_contiguous_checked_width():
    from types import SimpleNamespace
    line=SimpleNamespace(name='W',segments={1:[(30,10)]})
    cfg=dict(lines_y=[line])
    value=P.empty('CFS')
    value['layout']=dict(wall_segments_ft={'Y-W':{'1':[[1,11],[11,21],[21,31]]}},
                         wall_segment_groups={'Y-W':{'1':[[1,2,3]]}})
    placed=P.apply_layout(cfg,value)
    assert placed['lines_y'][0].segments[1]==[(10,10)]*3
    assert line.segments[1]==[(30,10)]
    value['layout']['wall_segments_ft']['Y-W']['1'][1]=[12,22]
    with pytest.raises(ValueError,match='continuous'):
        P.apply_layout(cfg,value)


def test_column_pieces_follow_field_splice_not_analysis_level():
    from steel_engine.physical_pieces import stacked_pieces
    lower=dict(id='lower',axis=[[0,0,0],[0,0,120]],analysis_ids=['A1'],profile_id='LOWER',local_y=[1,0,0],design_record_ids=['CHECK-LOWER'])
    upper=dict(id='upper',axis=[[0,0,120],[0,0,240]],analysis_ids=['A2'],profile_id='UPPER',local_y=[1,0,0],design_record_ids=['CHECK-UPPER'])
    pieces=stacked_pieces('column',[lower,upper],[(0,168,lower),(168,240,upper)])
    assert pieces[0]['axis'][1]==[0,0,168] and pieces[0]['profile_id']=='LOWER'
    assert pieces[0]['analysis_segments']==[dict(analysis_id='A1',start=0.,end=1.),dict(analysis_id='A2',start=0.,end=.4)]
    assert pieces[1]['analysis_segments']==[dict(analysis_id='A2',start=.4,end=1.)]
    assert upper['axis'][0]==[0,0,120]
    assert pieces[0]['design_record_ids']==['CHECK-LOWER']
    assert pieces[0]['analysis_design_evidence'][1]['design_record_ids']==['CHECK-UPPER']
    assert pieces[0]['analysis_design_evidence'][1]['analysis_segment']==dict(analysis_id='A2',start=0.,end=.4)
    with pytest.raises(ValueError,match='partition'):
        stacked_pieces('column',[lower,upper],[(0,168,lower),(169,240,upper)])

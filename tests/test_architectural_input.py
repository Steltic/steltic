import copy
import json
import pytest
from steel_engine import architectural_input as A
from steel_engine import bim_export as B
from test_bim_export import job,exported


def brief(openings=True):
    sections={'GRIDS':[dict(label='A',direction='U',points_m=[[0,0,0],[0,3.048,0]]),dict(label='1',direction='V',points_m=[[0,0,0],[3.048,0,0]])],
              'STOREYS':[dict(name='Ground',elevation_m=0),dict(name='Roof',elevation_m=3.048)],
              'DOORS_WINDOWS':[dict(guid='source-door',name='Door',bounds_m=[[.6096,1.524],[-.0254,.0254],[0,2.1336]],host_wall_guids=['source-wall'])] if openings else []}
    return '\n'.join([A.MARKER,'IFC source: architect.ifc','SHA256: '+'a'*64,'Coordinates below are metres; convert to design units explicitly.',*[line for name,value in sections.items() for line in (name,json.dumps(value))]])


def package_with_source(exported):
    package=copy.deepcopy(exported[2]);source=A.parse_handoff(brief())
    package['architectural_input']=source;package['reference_system']=copy.deepcopy(source['reference_system'])
    package['physical']['panels']=[dict(id='wall',kind='wall_segment',vertices=[[0,0,0],[120,0,0],[120,0,120],[0,0,120]],thickness=.5,material_id='steel',status='declared')]
    package['physical']['openings']=[dict(id='door',host_id='wall',source_ifc_guid='source-door',vertices=[[24,0,0],[60,0,0],[60,0,84],[24,0,84]],status='declared')]
    return package


def test_parser_converts_source_data_without_design_inference():
    value=A.parse_handoff(brief());assert value['source_ifc_sha256']=='a'*64
    assert value['reference_system']['levels'][1]['elevation_in']==120
    assert [v for pair in value['openings'][0]['bounds_in'] for v in pair]==pytest.approx([24,60,-1,1,0,84])
    assert not any(k in value for k in ('members','connections','loads'))
    assert A.parse_handoff('An ordinary design brief') is None


@pytest.mark.parametrize('replace',[
    lambda t:t.replace('a'*64,'not-a-hash'),
    lambda t:t.replace('Coordinates below are metres;','Coordinates below are feet;'),
    lambda t:t.replace('"elevation_m": 0','"elevation_m": NaN'),
    lambda t:t.replace('GRIDS\n[','GRIDS\nnull\n['),
    lambda t:t+'\n'+A.MARKER,
])
def test_bad_handoff_cannot_be_silently_accepted(replace):
    with pytest.raises(ValueError):A.parse_handoff(replace(brief()))


def test_capture_survives_log_start_and_uses_latest_explicit_handoff(tmp_path):
    from steltic.job_tools import JobWorkspace
    ws=JobWorkspace(tmp_path/'jobs');ws.building='case';ws.prepare_new_design()
    root=ws._job_dir();messages=[dict(role='user',content=brief())]
    assert A.capture(root,messages)
    assert (root/'architectural_handoff.txt').read_text()==brief()
    before=B.read_json(root/'design/bim_request.json')
    ws.new_activity_log('case')
    assert B.read_json(root/'design/bim_request.json')==before
    assert 'error' in ws.write_file('architectural_input.json','{}')
    assert 'error' in ws.write_file('architectural_handoff.txt','changed')
    assert 'error' in ws.write_file('design/bim_request.json','{}')
    assert A.capture(root,messages+[dict(role='user',content='Continue the existing design')])
    revised=brief().replace('a'*64,'b'*64)
    assert A.capture(root,messages+[dict(role='user',content=[dict(type='text',text=revised)])])
    assert B.read_json(root/'architectural_input.json')['source_ifc_sha256']=='b'*64
    assert len(list((root/'architectural_input_history').glob('*.json')))==1


def test_source_identity_is_copied_and_binding_is_part_of_replay(tmp_path):
    root=job(tmp_path);A.capture(root,[dict(role='user',content=brief(openings=False))])
    package=B.replay(root,tmp_path/'out/bim_package.json',20)
    assert package['readiness']['status']=='ready',package['readiness']
    assert package['reference_system']==A.parse_handoff(brief(False))['reference_system']
    assert 'architectural_input.json' in package['provenance']['source_files']
    assert 'architectural_handoff.txt' in package['provenance']['source_files']
    (root/'architectural_input.json').write_text('{}')
    package=B.replay(root,tmp_path/'changed/bim_package.json',20)
    assert 'architectural_input_changed' in {i['code'] for i in package['readiness']['findings']}


def test_source_opening_and_reference_coverage_is_independent_of_declared_complete(exported):
    package=package_with_source(exported)
    assert not B.validate(package),B.validate(package)
    package['physical']['openings']=[]
    assert package['physical']['coverage']['openings']['complete']
    assert 'architectural_openings' in {i['code'] for i in B.validate(package)}


@pytest.mark.parametrize('mutate,code',[
    (lambda p:p['reference_system']['levels'].pop(),'architectural_levels'),
    (lambda p:p['reference_system']['levels'][0].pop('name'),'architectural_levels'),
    (lambda p:p['reference_system']['grids'][0].update(label='X'),'architectural_grids'),
    (lambda p:p['reference_system']['grids'][0]['points_in'][0].__setitem__(0,1),'architectural_grids'),
    (lambda p:p['physical']['openings'][0]['vertices'][0].__setitem__(0,0),'architectural_opening_bounds'),
    (lambda p:p['physical']['openings'][0].pop('source_ifc_guid'),'architectural_openings'),
    (lambda p:p['physical']['openings'].append({**p['physical']['openings'][0],'id':'duplicate'}),'architectural_opening_duplicates'),
    (lambda p:p['physical']['members'].append({**p['physical']['members'][0],'id':'duplicate'}),'duplicate_physical_members'),
])
def test_source_mismatches_never_claim_ready(exported,mutate,code):
    package=package_with_source(exported);mutate(package)
    assert code in {i['code'] for i in B.validate(package)}


def test_run_binds_architecture_before_the_first_model_call(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from steltic import agent
    from steltic.job_tools import JobWorkspace
    ws=JobWorkspace(tmp_path/'jobs')
    def chat(*args,**kwargs):
        assert B.read_json(ws.jobs/'case/architectural_input.json')['source_ifc_sha256']=='a'*64
        assert any(m.get('content')==A.NOTICE for m in args[3])
        if False:yield None
        raise agent.UserStop()
    monkeypatch.setattr(agent,'_chat_with_retry',chat)
    events=list(agent.run_design(ws,SimpleNamespace(name='test'),'http://invalid','unused','test','case',brief()))
    assert events[-1]['type']=='paused'

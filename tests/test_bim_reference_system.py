import copy
import pytest
from steel_engine import bim_export as B
from test_bim_export import job,exported


REFERENCE=dict(basis='Explicit architectural reference coordinates, inches',source_ifc_sha256='a'*64,
    levels=[dict(level=0,name='Ground',elevation_in=0),dict(level=1,name='Platform',elevation_in=144)],
    grids=[dict(id='A',label='A',direction='U',points_in=[[0,0,0],[120,120,0]]),
           dict(id='1',label='1',direction='V',points_in=[[0,120,0],[120,0,0]])])


def test_reference_system_and_strict_connection_requirement_survive_replay(tmp_path):
    root=job(tmp_path,cfg_tail="cfg['bim']['reference_system']="+repr(REFERENCE)+'\n')
    B.atomic_json(root/'design/bim_request.json',dict(required=True,require_connection_parts=True))
    package=B.replay(root,tmp_path/'out/bim_package.json',20)
    assert package['reference_system']==REFERENCE
    assert package['require_connection_parts'] is True
    assert package['readiness']['status']=='ready'


def test_new_job_cannot_finish_with_only_designed_connection_references(exported):
    package=copy.deepcopy(exported[2]);connection=package['physical']['connections'][0]
    connection.update(parts=[],detail_ref=dict(id='D1',revision='A'))
    assert not B.validate(package)  # legacy source retains its existing contract
    package['require_connection_parts']=True
    assert 'connection_parts_required' in {i['code'] for i in B.validate(package)}
    connection.update(scope='delegated',responsible_party='Named supplier',representation='Explicit supplier interface')
    assert not B.validate(package)


@pytest.mark.parametrize('mutate,code',[
    (lambda r:r['levels'].append(r['levels'][0]),'reference_levels'),
    (lambda r:r['grids'].append(r['grids'][0]),'reference_grids'),
    (lambda r:r['grids'][0].update(points_in=[[0,0,0],[0,0,0]]),'reference_grids'),
    (lambda r:r['grids'].pop(),'reference_grids'),
    (lambda r:r['levels'][0].update(elevation_in='zero'),'schema'),
])
def test_invalid_reference_data_cannot_claim_readiness(exported,mutate,code):
    package=copy.deepcopy(exported[2]);package['reference_system']=copy.deepcopy(REFERENCE)
    mutate(package['reference_system'])
    assert code in {i['code'] for i in B.validate(package)}


def test_consistency_preserves_required_connection_geometry_marker(tmp_path,monkeypatch):
    root=job(tmp_path);marker=root/'design/bim_request.json'
    B.atomic_json(marker,dict(required=True,require_connection_parts=True))
    def replay(*args,**kwargs):
        assert B.read_json(marker)['require_connection_parts'] is True
        return dict(readiness=dict(status='ready'))
    monkeypatch.setattr(B,'replay',replay)
    assert B.after_consistency(root,dict(bim=dict(required=True)),[])==[]
    assert B.read_json(marker)['require_connection_parts'] is True

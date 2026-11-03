import copy
import json
from pathlib import Path
import pytest
from steel_engine.bim_contract import fastener_quantity_findings, validate


def package(count=4, actual=1):
    p=json.loads((Path(__file__).parent/'fixtures/synthetic_bim_package.json').read_text())
    p['require_connection_parts']=True
    c=p['physical']['connections'][0]
    material=c['parts'][0]['material_id']
    c['parts']=[dict(id='bolt-'+str(i),kind='bolt',material_id=material,parameters=dict(count=count),
        geometry=dict(type='cylinder',origin=[i*3.,0.,0.],axis=[0.,0.,1.],length=2.,radius=.375))
        for i in range(actual)]
    return p,c


@pytest.mark.parametrize('kind', ['bolt group','anchor_rods','screws'])
@pytest.mark.parametrize('shape', ['cylinder','box'])
def test_one_representative_solid_does_not_supply_declared_fasteners(kind,shape):
    p,c=package();c['parts'][0]['kind']=kind
    if shape=='box':c['parts'][0]['geometry']=dict(type='box',origin=[0,0,0],size=[14,14,24],x_axis=[1,0,0],y_axis=[0,1,0])
    before=copy.deepcopy(p);finding=fastener_quantity_findings(p)[0]
    assert finding['examples'][0]['actual_distinct_solids']==1
    assert finding['examples'][0]['declared_count']==4
    assert 'connection_fastener_quantity' in {f['code'] for f in validate(p)}
    assert p==before


def test_array_metadata_may_repeat_on_each_real_fastener():
    p,_=package(actual=4)
    assert not fastener_quantity_findings(p)


def test_split_local_assembly_can_supply_the_pattern_but_unrelated_parts_cannot():
    p,c=package(actual=4);other=copy.deepcopy(c);other['id']='other'
    other['parts']=other['parts'][2:];c['parts']=c['parts'][:2]
    p['physical']['connections'].append(other)
    assert not fastener_quantity_findings(p)
    other['design_record_ids']=['unrelated']
    assert fastener_quantity_findings(p)
    other['design_record_ids']=c['design_record_ids'];other['joint_id']='remote'
    assert fastener_quantity_findings(p)


def test_coincident_copies_and_washers_do_not_supply_extra_shafts():
    p,c=package(actual=4)
    for part in c['parts']:part['geometry']['origin']=[0,0,0]
    assert fastener_quantity_findings(p)[0]['examples'][0]['actual_distinct_solids']==1
    for part in c['parts'][1:]:part['kind']='bolt washer'
    assert fastener_quantity_findings(p)


def test_legacy_delegated_and_reference_only_parts_retain_their_scope():
    p,c=package();p['require_connection_parts']=False
    assert not fastener_quantity_findings(p)
    p['require_connection_parts']=True;c['scope']='delegated'
    assert not fastener_quantity_findings(p)
    c['scope']='designed';c['parts'][0]['geometry']=dict(type='polyline',points=[[0,0,0],[1,0,0]])
    assert not fastener_quantity_findings(p)

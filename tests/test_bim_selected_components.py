import copy
import json
from pathlib import Path
import pytest
from steel_engine.bim_contract import connection_component_findings, validate


def package(selected,field='components'):
    p=json.loads((Path(__file__).parent/'fixtures/synthetic_bim_package.json').read_text())
    p['require_connection_parts']=True
    c=p['physical']['connections'][0]
    record=next(r for r in p['design_records'] if r['id']==c['design_record_ids'][0])
    record['table']='connections';record['data']={field:selected}
    c['parts'][0]['kind']='base plate'
    return p,c,record


@pytest.mark.parametrize('text,field,missing',[
    ('PL 24x24x1.5; 4x 1.25-in anchor rods; 5/16-in fillet welds','components',['anchor','weld']),
    ('CJP groove welds both flanges and full-depth web CJP','components',['weld']),
    ('uplift clip with (4) No.10 screws each leg','selection',['screw']),
    ('clip with 12x #14 screws, phiPn=1 kip per screw','capacity',['screw']),
    ('4x 3/4-in A325 bolts','selection',['bolt']),
    ('4 anchor bolts','selection',['anchor']),
    ('PL34x34x3; 12-1.50in F1554-Gr55 rods; CJP','components',['anchor','weld']),
])
def test_explicit_selected_hardware_cannot_be_replaced_by_a_plate(text,field,missing):
    p,c,_=package(text,field);before=copy.deepcopy(p)
    findings=connection_component_findings(p)
    assert findings[0]['examples'][0]['missing_component_categories']==missing
    assert 'connection_selected_components' in {f['code'] for f in validate(p)}
    assert p==before
    for category in missing:
        c['parts'].append(dict(id=category,kind=category+' group',material_id='steel',geometry=dict(type='polyline',points=[[0,0,0],[1,0,0]])))
    assert not connection_component_findings(p)
    assert not validate(p)


@pytest.mark.parametrize('text',[
    'Bearing plate; no field welds; no bolts',
    'Bearing plate, bolts not required; anchor rods are not required',
    'Bearing plate without additional screws; no CJP welds',
])
def test_explicit_component_exclusions_do_not_create_hardware(text):
    p,_,_=package(text)
    assert not connection_component_findings(p)


def test_general_code_references_are_not_a_component_selection():
    p,_,record=package('bearing plate')
    record['data'].update(cited='AISC J3 bolt strength, J2 weld requirements',limit_state='bolt/weld connection guidance',capacity='screw bearing may govern')
    assert not connection_component_findings(p)


def test_legacy_and_explicit_delegated_scope_retain_their_contract():
    p,c,_=package('4 bolts')
    p.pop('require_connection_parts');assert not connection_component_findings(p)
    p['require_connection_parts']=True;c['scope']='delegated'
    assert not connection_component_findings(p)


def test_anchor_bolt_and_screw_aliases_need_actual_fastener_geometry():
    p,c,_=package('4 anchor rods and 4 screws')
    plate=c['parts'][0];plate['parameters']={'anchor_count':4,'screws':4}
    assert connection_component_findings(p)
    c['parts'] += [dict(id='anchor-1',kind='bolt group',material_id='steel',geometry=dict(type='polyline',points=[[0,0,0],[1,0,0]])),
        dict(id='fasteners',kind='fastener group',material_id='steel',parameters={'designation':'No.10 ASTM C1513'},geometry=dict(type='polyline',points=[[0,0,0],[1,0,0]]))]
    assert not connection_component_findings(p)


def test_separate_components_of_the_same_local_assembly_are_not_duplicated():
    p,c,_=package('base plate and 4 anchor rods')
    other=copy.deepcopy(c);other['id']='anchors';other['parts'][0]['kind']='anchor group'
    p['physical']['connections'].append(other)
    assert not connection_component_findings(p)
    other['joint_id']='elsewhere'
    assert connection_component_findings(p)

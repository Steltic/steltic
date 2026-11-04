import copy
import json
from pathlib import Path
import pytest
from steel_engine.bim_contract import connection_component_findings, duplicate_connection_part_findings, validate


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
    'Existing encased grillage, no designed anchor rods',
    'The end plate acts as the anchor plate for uplift; anchor reinforcement As = 154 in2',
    'Deck bears without welds, screws or studs in the protected zone',
])
def test_explicit_component_exclusions_do_not_create_hardware(text):
    p,_,_=package(text)
    assert not connection_component_findings(p)


def test_exclusion_list_does_not_hide_later_positive_selection():
    p,_,_=package('Deck without welds, screws or studs; outside the zone use four bolts and 2 anchor rods')
    missing=connection_component_findings(p)[0]['examples'][0]['missing_component_categories']
    assert missing==['anchor','bolt']


def test_general_code_references_are_not_a_component_selection():
    p,_,record=package('bearing plate')
    record['data'].update(cited='AISC J3 bolt strength, J2 weld requirements',limit_state='bolt/weld connection guidance',capacity='screw bearing may govern')
    assert not connection_component_findings(p)


def test_foundation_anchor_reinforcement_does_not_invent_rods_or_hide_selected_rods():
    p,c,record=package('Embedded shaft; anchor reinforcement As >= 12 in2 by foundation engineer')
    assert not connection_component_findings(p)
    record['data']['components']+='; four erection anchor rods'
    assert connection_component_findings(p)[0]['examples'][0]['missing_component_categories']==['anchor']


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


def test_nested_hr_selection_and_numeric_counts_are_not_ignored():
    p,c,r=package({'components':'CJP flange welds', 'n_bolts':4, 'bolt_dia_in':.75})
    assert connection_component_findings(p)[0]['examples'][0]['missing_component_categories']==['bolt','weld']
    r['data']['components']={'n_bolts':0,'components':['bearing plate',{'selection':'4 F1554 anchor rods'}]}
    assert connection_component_findings(p)[0]['examples'][0]['missing_component_categories']==['anchor']
    r['data']['components']['n_bolts']=4
    assert connection_component_findings(p)[0]['examples'][0]['missing_component_categories']==['anchor']
    r['data']['components']={'n_bolts':0,'cited':'J3 bolts, J2 welds','note':'No field welding is allowed'}
    assert not connection_component_findings(p)


def test_selected_schedule_fasteners_apply_to_linked_connection_occurrences():
    p,c,r=package('2 No.14 screws at each frame','selection');r['table']='schedules'
    assert connection_component_findings(p)[0]['examples'][0]['missing_component_categories']==['screw']
    r['waived']=True
    assert not connection_component_findings(p)


def test_two_top_bottom_solids_cannot_occupy_the_same_coordinates():
    p,c,_=package('bearing plates')
    first=c['parts'][0];second=copy.deepcopy(first);second['id']='bottom-plate'
    c['parts'].append(second);before=copy.deepcopy(p)
    assert duplicate_connection_part_findings(p)[0]['examples'][0]['part_ids']==[first['id'],'bottom-plate']
    assert 'connection_duplicate_parts' in {f['code'] for f in validate(p)}
    assert p==before
    second['geometry']['origin'][2]+=12
    assert not duplicate_connection_part_findings(p)
    second['geometry']=copy.deepcopy(first['geometry']);p.pop('require_connection_parts')
    assert not duplicate_connection_part_findings(p)


def test_explicit_reference_groups_are_supported_but_cannot_be_duplicated():
    p,c,_=package('bolts')
    first=c['parts'][0];first.update(kind='bolt group',parameters={'count':4})
    second=copy.deepcopy(first);second['id']='second-group';c['parts'].append(second)
    assert duplicate_connection_part_findings(p)
    second['geometry']['origin'][0]+=3
    assert not duplicate_connection_part_findings(p)


def test_covered_by_member_note_does_not_create_its_selected_anchors():
    p,c,r=package('4 F1554 anchors','selection')
    c['design_record_ids']=[]
    member=p['physical']['members'][0]
    p['physical']['record_dispositions']=[dict(record_id=r['id'],status='covered_by_assembly',entity_ids=[member['id']],reason='Covered by this member')]
    assert connection_component_findings(p)[0]['code']=='connection_selected_family'
    member.update(kind='anchor rod',material_id='steel')
    assert not connection_component_findings(p)
    member['kind']='beam';p['physical']['record_dispositions'][0]['status']='delegated'
    assert not connection_component_findings(p)


def test_family_can_reference_an_actual_shared_connection_assembly():
    p,c,r=package('4 bolts','selection');c['design_record_ids']=[]
    p['physical']['record_dispositions']=[dict(record_id=r['id'],status='covered_by_assembly',entity_ids=[c['id']],reason='Actual shared local assembly')]
    assert connection_component_findings(p)
    c['parts'][0]['kind']='bolt group'
    assert not connection_component_findings(p)

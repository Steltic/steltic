import copy
import json
from pathlib import Path

import pytest
from steel_engine.bim_contract import joint_disposition_findings, validate


def package():
    p = json.loads((Path(__file__).parent/'fixtures/synthetic_bim_package.json').read_text())
    p['require_connection_parts'] = True
    p['physical']['joint_dispositions'][0].pop('kind',None)
    return p


def disposition(p, kind, **kw):
    d = p['physical']['joint_dispositions'][0]
    d.update(kind=kind, **kw)
    return d


def test_legacy_reason_is_readable_but_cannot_complete_a_new_required_job():
    p = package(); before = copy.deepcopy(p)
    assert {f['code'] for f in validate(p)} == {'joint_disposition_kind'}
    assert p == before
    p.pop('require_connection_parts')
    assert not validate(p)
    p['require_connection_parts'] = True
    disposition(p,'free_end')
    assert not validate(p)


def test_free_end_does_not_exempt_a_connected_branch():
    p = package(); disposition(p,'free_end')
    p['physical']['joints'][1]['attachments'] = [dict(member_id='beam',station=1)]
    assert any(f['code']=='joint_free_end' for f in joint_disposition_findings(p))


def test_continuity_uses_actual_ends_and_accepts_a_true_split():
    p = package(); disposition(p,'continuous_member')
    assert any(f['code']=='joint_continuity' for f in joint_disposition_findings(p))
    member = copy.deepcopy(p['physical']['members'][0]); member.update(id='next',axis=[[120,0,0],[240,0,0]])
    p['physical']['members'].append(member)
    p['physical']['joints'][1]['member_ends'].append(dict(member_id='next',end='start'))
    assert not joint_disposition_findings(p)
    member['axis'][1] = [120,120,0]
    assert any(f['code']=='joint_continuity' for f in joint_disposition_findings(p))
    member['axis'][1] = [240,0,0]; member['end_offsets'][0] = [1,0,0]
    assert any(f['code']=='joint_continuity' for f in joint_disposition_findings(p))


def test_interior_station_is_not_a_connection_to_another_member():
    p = package(); disposition(p,'continuous_member')
    joint = p['physical']['joints'][1]
    joint.update(point=[60,0,0],member_ends=[],attachments=[dict(member_id='beam',station=.5)])
    assert not joint_disposition_findings(p)
    joint['member_ends'] = [dict(member_id='beam',end='end')]
    assert any(f['code']=='joint_continuity' for f in joint_disposition_findings(p))


def test_bearing_requires_actual_calculation_and_support_without_invented_hardware():
    p = package(); d = disposition(p,'bearing_interface')
    assert {f['code'] for f in joint_disposition_findings(p)} == {'joint_bearing_design','joint_bearing_support'}
    support = copy.deepcopy(p['physical']['members'][0]); support.update(id='supporting-beam',axis=[[120,-120,0],[120,120,0]])
    p['physical']['members'].append(support)
    d.update(design_record_ids=[p['design_records'][0]['id']],supporting_entity_ids=['supporting-beam'])
    assert not joint_disposition_findings(p)
    d['supporting_entity_ids'] = ['beam']
    assert any(f['code']=='joint_bearing_support' for f in joint_disposition_findings(p))
    d['supporting_entity_ids'] = ['supporting-beam']; support['axis'] = [[2000,-120,0],[2000,120,0]]
    assert any(f['code']=='joint_bearing_support' for f in joint_disposition_findings(p))


def test_shared_assembly_needs_explicit_existing_local_connection():
    p = package(); d = disposition(p,'shared_connection')
    assert any(f['code']=='joint_shared_connection' for f in joint_disposition_findings(p))
    c = p['physical']['connections'][0]; d['connection_ids'] = [c['id']]
    assert any(f['code']=='joint_shared_connection' for f in joint_disposition_findings(p))
    c['parts'][0]['geometry']['origin'] = [115,0,0]
    assert not joint_disposition_findings(p)
    d['connection_ids'] = ['missing']
    assert any(f['code']=='joint_shared_connection' for f in joint_disposition_findings(p))


@pytest.mark.parametrize('geometry',[
    dict(type='polyline',points=[[120,0,0],[120,1,0]]),
    dict(type='cylinder',origin=[120,0,0],axis=[1,0,0],length=1,radius=.25),
    dict(type='extruded_profile',origin=[120,0,0],axis=[1,0,0],local_y=[0,1,0],length=1,profile_id='TEST'),
])
def test_shared_reference_part_bounds_include_supported_geometries(geometry):
    p = package(); c = p['physical']['connections'][0]
    disposition(p,'shared_connection',connection_ids=[c['id']])
    c['parts'][0]['geometry'] = geometry
    assert not joint_disposition_findings(p)


@pytest.mark.parametrize('ring',[
    [[0,0,0],[432,0,0],[432,0,192],[0,0,192],[216,0,240]],
    [[0,0,0],[100,0,100],[100,0,0],[0,0,100]],
    [[0,0,0],[100,0,0],[50,0,0],[50,0,100],[0,0,100]],
])
def test_crossed_or_backtracking_panel_ring_is_rejected_before_export(ring):
    p = package(); disposition(p,'free_end')
    p['physical']['panels'] = [dict(id='wall',kind='wall',status='declared',material_id='steel',thickness=.5,vertices=ring)]
    assert any(f['code']=='panel_geometry' for f in validate(p))


@pytest.mark.parametrize('ring',[
    [[0,0,0],[432,0,0],[432,0,192],[216,0,240],[0,0,192]],
    [[0,0,0],[50,0,0],[100,0,0],[100,0,100],[50,0,50],[0,0,100]],
    [[0,0,0],[100,100,50],[0,100,50],[0,0,0]],
])
def test_simple_concave_sloped_and_closed_boundaries_still_work(ring):
    p = package(); disposition(p,'free_end')
    p['physical']['panels'] = [dict(id='wall',kind='wall',status='declared',material_id='steel',thickness=.5,vertices=ring)]
    assert not validate(p)

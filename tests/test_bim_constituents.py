import copy
import json
from pathlib import Path

import pytest

from steel_engine.bim_contract import _convex_overlap, physical_screens, validate


def assembly(offset=.8125):
    channel=dict(id='C',family='channel',status='declared',dimensions=dict(d=8,b=1.625,t=.1,lip=.5,r=.1))
    pair=dict(id='pair',family='built_up',status='declared',dimensions={},components=[
        dict(profile_id='C',offset=[-offset,0],rotation_deg=0),
        dict(profile_id='C',offset=[offset,0],rotation_deg=180)])
    return dict(profiles=[channel,pair])


def overlaps(physical):
    return [f for f in physical_screens(physical) if f['code']=='physical_constituent_overlap']


def test_channel_bounding_centre_error_detected_without_mutation():
    physical=assembly();before=copy.deepcopy(physical)
    findings=overlaps(physical)
    assert findings[0]['examples'][0]['components']==[0,1]
    assert findings[0]['examples'][0]['overlap_fraction']==pytest.approx(.305/1.105,abs=1e-6)
    assert physical==before


def test_correct_box_and_touching_back_to_back_webs_are_allowed():
    assert not overlaps(assembly(1.625))
    physical=assembly(.05)
    for component in physical['profiles'][1]['components']:
        component['rotation_deg']=180-component['rotation_deg']
    assert not overlaps(physical)


def test_rotated_clipping_and_touching_edges():
    square=[[-1,-1],[1,-1],[1,1],[-1,1]]
    diamond=[[0,-1],[1,0],[0,1],[-1,0]]
    assert _convex_overlap(square,diamond)==pytest.approx(2)
    assert _convex_overlap(diamond,square)==pytest.approx(2)
    adjacent=[[1,-1],[3,-1],[3,1],[1,1]]
    assert _convex_overlap(square,adjacent)==pytest.approx(0)


def test_multiple_pairs_identified_in_six_ply_member():
    physical=assembly();parts=physical['profiles'][1]['components']
    physical['profiles'][1]['components']=[dict(c,offset=[c['offset'][0]+shift,0]) for shift in (-10,0,10) for c in parts]
    assert [r['components'] for r in overlaps(physical)[0]['examples']]==[[0,1],[2,3],[4,5]]


def test_readiness_cannot_override_constituent_overlap():
    package=json.loads((Path(__file__).parent/'fixtures/synthetic_bim_package.json').read_text())
    package['physical']['profiles']+=assembly()['profiles']
    package['readiness']['status']='ready'
    finding=next(f for f in validate(package) if f['code']=='physical_constituent_overlap')
    assert finding['severity']=='needs_input'
    assert finding['entity_ids']==['pair']

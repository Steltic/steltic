import copy
import json
from pathlib import Path

import pytest

from steel_engine.bim_contract import physical_screens, validate


def part(geometry, kind='gusset', **parameters):
    return dict(connections=[dict(id='connection', parts=[dict(id='part', kind=kind, geometry=geometry, parameters=parameters)])])


def findings(data):
    return [f for f in physical_screens(data) if f['code']=='physical_part_dimensions']


@pytest.mark.parametrize('size', [[12,1.2,18],[18,12,1.2],[1.2,18,12]])
def test_plate_geometry_contradicting_its_thickness_is_reported(size):
    result=findings(part(dict(type='box',size=size),thickness_in=.137))
    assert result[0]['examples'][0]['declared_value_in']==.137
    assert result[0]['examples'][0]['geometry_dimensions_in']==size


@pytest.mark.parametrize('size', [[12,.137,18],[18,12,.14],[.137,18,12]])
def test_matching_rotated_or_rounded_plate_thickness(size):
    assert not findings(part(dict(type='box',size=size),thickness_in=.137))


def test_base_plate_alias_and_cylindrical_shaft():
    assert findings(part(dict(type='box',size=[18,18,1]),kind='base plate',plate_thickness_in=.75))
    assert findings(part(dict(type='cylinder',radius=.5),kind='anchor',diameter_in=.75))
    assert not findings(part(dict(type='cylinder',radius=.375),kind='anchor',diameter_in=.75))


def test_nominal_diameter_on_head_is_not_its_outer_diameter():
    assert not findings(part(dict(type='cylinder',radius=.5),kind='bolt head',diameter_in=.75))


@pytest.mark.parametrize('value', [None, '0.137 in', True, -1, 0])
def test_non_numeric_or_unusable_parameter_is_not_inferred(value):
    assert not findings(part(dict(type='box',size=[12,1.2,18]),thickness_in=value))


def test_ready_flag_cannot_override_contradictory_part_dimensions():
    package=json.loads((Path(__file__).parent/'fixtures/synthetic_bim_package.json').read_text())
    target=package['physical']['connections'][0]['parts'][0]
    target['kind']='plate';target['geometry']['size']=[18,18,1]
    target.setdefault('parameters',{})['thickness_in']=.75
    package['readiness']['status']='ready';before=copy.deepcopy(package)
    result=next(f for f in validate(package) if f['code']=='physical_part_dimensions')
    assert result['severity']=='needs_input'
    assert package==before

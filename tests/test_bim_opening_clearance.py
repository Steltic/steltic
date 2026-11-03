"""Opening reservation regressions from fresh architectural IFC workflows."""
import copy
import json
import math
from pathlib import Path

import pytest

from steel_engine.bim_contract import physical_screens, validate


def physical(axis, offsets=None, vertices=None):
    return dict(
        members=[dict(id='member', status='declared', axis=axis,
                      end_offsets=offsets or [[0, 0, 0], [0, 0, 0]])],
        panels=[dict(id='wall', thickness=6,
                     vertices=[[-100, 0, -100], [100, 0, -100], [100, 0, 100], [-100, 0, 100]])],
        openings=[dict(id='door', host_id='wall',
                       vertices=vertices or [[0, 0, 0], [40, 0, 0], [40, 0, 84], [0, 0, 84]])])


def clashes(data):
    return [f for f in physical_screens(data) if f['code'] == 'physical_opening_workline']


@pytest.mark.parametrize('axis', [
    [[20, 0, -10], [20, 0, 100]],  # column through doorway
    [[-20, 0, -10], [60, 0, 100]],  # diagonal brace
    [[20, -10, 40], [20, 10, 40]],  # transverse member
    [[20, 0, 40], [20, 0, 50]],  # entirely contained
])
def test_member_crossing_opening(axis):
    finding = clashes(physical(axis))[0]
    assert finding['examples'] == [dict(opening_id='door', host_id='wall', member_id='member')]


@pytest.mark.parametrize('axis', [
    [[0, 0, -10], [0, 0, 100]],  # boundary contact
    [[.1, 0, -10], [.1, 0, 100]],  # within 5 mm tolerance
    [[20, 4, -10], [20, 4, 100]],  # outside host thickness
    [[-20, 0, 40], [-10, 0, 40]],
])
def test_boundary_or_separate_workline_is_not_a_clash(axis):
    assert not clashes(physical(axis))


def test_actual_offsets_and_reversed_opening_winding():
    data = physical([[20, 0, -10], [20, 0, 100]])
    data['openings'][0]['vertices'].reverse()
    assert clashes(data)
    data['members'][0]['end_offsets'] = [[30, 0, 0], [30, 0, 0]]
    assert not clashes(data)


def test_rotated_wall_uses_its_actual_plane():
    data = physical([[20, 0, -10], [20, 0, 100]])
    def rotate(p):
        c = math.sqrt(.5)
        return [c*(p[0]-p[1])+500, c*(p[0]+p[1])-300, p[2]+50]
    for row in data['panels']+data['openings']:
        row['vertices'] = [rotate(p) for p in row['vertices']]
    data['members'][0]['axis'] = [rotate(p) for p in data['members'][0]['axis']]
    assert clashes(data)


def test_triangle_does_not_use_its_enclosing_rectangle():
    triangle = [[0, 0, 0], [40, 0, 0], [0, 0, 40]]
    assert not clashes(physical([[35, -10, 35], [35, 10, 35]], vertices=triangle))
    assert clashes(physical([[10, -10, 10], [10, 10, 10]], vertices=triangle))


def test_unsupported_boundaries_are_not_replaced_by_bounding_boxes():
    concave = [[0, 0, 0], [40, 0, 0], [40, 0, 20], [20, 0, 20], [20, 0, 40], [0, 0, 40]]
    assert not clashes(physical([[10, -10, 10], [10, 10, 10]], vertices=concave))
    nonplanar = [[0, 0, 0], [40, 0, 0], [40, 1, 84], [0, 0, 84]]
    assert not clashes(physical([[20, 0, -10], [20, 0, 100]], vertices=nonplanar))


def test_a_stored_ready_flag_cannot_override_opening_clash():
    package = json.loads((Path(__file__).parent/'fixtures/synthetic_bim_package.json').read_text())
    data = package['physical']
    data['panels'] = [dict(id='wall', kind='wall', status='declared', material_id='steel', thickness=6,
                          vertices=[[0, 0, -60], [120, 0, -60], [120, 0, 60], [0, 0, 60]])]
    data['openings'] = [dict(id='opening', host_id='wall', vertices=[[40, 0, -20], [80, 0, -20], [80, 0, 20], [40, 0, 20]])]
    package['readiness']['status'] = 'ready'
    original = copy.deepcopy(package)
    finding = next(f for f in validate(package) if f['code'] == 'physical_opening_workline')
    assert finding['severity'] == 'needs_input'
    assert set(finding['entity_ids']) == {'beam', 'wall', 'opening'}
    assert package == original

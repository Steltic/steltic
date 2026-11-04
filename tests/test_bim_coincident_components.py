"""A real split solver shaft must preserve both records without duplicate steel."""
import copy
import pytest
from steel_engine.bim_contract import mapping_geometry_findings


def package():
    return dict(analysis=dict(nodes=[dict(id='a',point=[0,0,0]),dict(id='b',point=[360,0,0])],elements=[
        dict(id='axial',node_ids=['a','b'],builder_arguments=['Truss',1,1,2,196,1]),
        dict(id='bending',node_ids=['a','b'],builder_arguments=['elasticBeamColumn',2,1,2,.001,29000])]),
        physical=dict(members=[dict(id='chord',status='declared',axis=[[0,0,0],[360,0,0]],analysis_ids=['axial','bending'],
            analysis_component_roles={'axial':'axial','bending':'flexural'})]))


def test_explicit_coincident_axial_flexural_shaft_keeps_both_identities():
    p=package();saved=copy.deepcopy(p)
    assert not mapping_geometry_findings(p)
    assert p==saved


@pytest.mark.parametrize('mutation',[
    lambda p:p['physical']['members'][0].pop('analysis_component_roles'),
    lambda p:p['physical']['members'][0].update(analysis_component_roles={'axial':'axial','bending':'axial'}),
    lambda p:p['physical']['members'][0].update(analysis_component_roles={'axial':[],'bending':'flexural'}),
    lambda p:p['physical']['members'][0].update(analysis_ids=['axial','bending','axial']),
    lambda p:p['analysis']['elements'][1]['builder_arguments'].__setitem__(4,196),
    lambda p:p['analysis']['elements'][1]['builder_arguments'].__setitem__(0,'Truss'),
    lambda p:p['analysis']['elements'][1].update(node_ids=['a','a']),
    lambda p:p['analysis']['elements'][1].update(node_ids=['a','missing']),
    lambda p:p['physical']['members'][0]['axis'][1].__setitem__(0,720),
    lambda p:p['physical']['members'][0]['axis'][0].__setitem__(1,12),
])
def test_roles_cannot_hide_duplicate_shafts_gaps_or_wrong_geometry(mutation):
    p=package();mutation(p)
    assert mapping_geometry_findings(p)

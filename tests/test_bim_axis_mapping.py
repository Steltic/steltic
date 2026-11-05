"""Axis relabelling preserves exact source interval/extent validation."""
import copy,json
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator
from steel_engine.bim_contract import mapping_geometry_findings


def package():
    return dict(analysis=dict(nodes=[dict(id='a',point=[0,150,288]),dict(id='b',point=[300,150,288]),dict(id='c',point=[600,150,288])],
        elements=[dict(id='e1',node_ids=['a','b']),dict(id='e2',node_ids=['b','c'])]),
        physical=dict(members=[dict(id='beam',status='declared',axis=[[150,0,288],[150,600,288]],analysis_ids=['e1','e2'])]))


def mapping():return dict(axis_order=[1,0,2],basis='Source span/station/Z becomes physical station/span/Z; inch units retained.')


def test_permutation_is_explicit_preserves_source_and_does_not_ignore_geometry():
    p=package();source=copy.deepcopy(p['analysis']);m=p['physical']['members'][0]
    assert mapping_geometry_findings(p)[0]['code']=='analysis_occurrence_geometry'
    m['analysis_coordinate_mapping']=mapping()
    assert mapping_geometry_findings(p)==[]
    assert p['analysis']==source
    m['axis'][1][1]+=1
    assert mapping_geometry_findings(p)[0]['code']=='analysis_occurrence_geometry'
    m['axis'][1][1]-=1;p['analysis']['nodes'][1]['point'][2]+=1
    assert mapping_geometry_findings(p)[0]['code']=='analysis_occurrence_geometry'


@pytest.mark.parametrize('bad',[
    dict(axis_order=[1,1,2],basis='Duplicate axis'),dict(axis_order=[1,0],basis='Omitted axis'),
    dict(axis_order=[1,0,3],basis='Unknown axis'),dict(axis_order=[1,0,True],basis='Boolean'),
    dict(axis_order=[1,0,2],basis=''),dict(axis_order=[1,0,2],basis='   '),
    dict(axis_order=[1,0,2],basis='Translation',origin=[1,0,0]),'not an object'])
def test_invalid_mapping_cannot_bypass_geometry_gate(bad):
    p=package();p['physical']['members'][0]['analysis_coordinate_mapping']=bad
    assert mapping_geometry_findings(p)[0]['code']=='analysis_coordinate_mapping'


def test_full_source_intervals_are_still_required_after_relabelling():
    p=package();m=p['physical']['members'][0];m['analysis_coordinate_mapping']=mapping()
    m['analysis_ids']=['e1']
    assert mapping_geometry_findings(p)
    m['analysis_ids']=[];m['analysis_segments']=[dict(analysis_id='e1',start=0,end=1),dict(analysis_id='e2',start=.1,end=1)]
    assert mapping_geometry_findings(p)
    m['analysis_segments'][1]['start']=0
    assert mapping_geometry_findings(p)==[]
    m['analysis_ids']=['e1'];assert mapping_geometry_findings(p)


def test_schema_rejects_scaling_and_repeated_or_missing_axes():
    schema=json.loads((Path(__file__).resolve().parents[1]/'steel_engine/bim_package.schema.json').read_text())
    rule=schema['$defs']['member']['properties']['analysis_coordinate_mapping']
    check=Draft202012Validator(rule)
    assert not list(check.iter_errors(mapping()))
    assert list(check.iter_errors(dict(axis_order=[1,1,2],basis='Invalid')))
    assert list(check.iter_errors(dict(axis_order=[1,0],basis='Invalid')))
    assert list(check.iter_errors(dict(axis_order=[1,0,2],basis='Invalid',scale=12)))

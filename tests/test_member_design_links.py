import copy
import json
from pathlib import Path
import pytest
from steel_engine.bim_contract import member_record_findings,validate


def model(profile='W14X90',section='W14X730',family='i'):
    return dict(physical=dict(profiles=[dict(id=profile,family=family)],
               members=[dict(id='column-1',profile_id=profile,design_record_ids=['members/column'])]),
               design_records=[dict(id='members/column',table='members',data=dict(inputs=dict(section=section)))])


def test_mismatched_member_record_is_rejected_without_mutation():
    package=model();before=copy.deepcopy(package)
    finding=member_record_findings(package)[0]
    assert finding['code']=='member_design_section' and finding['severity']=='needs_input'
    assert finding['examples']==[dict(member_id='column-1',profile_id='W14X90',record_id='members/column',record_section='W14X730')]
    assert package==before


@pytest.mark.parametrize('profile,section', [('W14X90','w14 x 90'),('HSS8X8X1/2','HSS8\u00d78\u00d71/2'),('WT6X20','WT6X20')])
def test_equivalent_explicit_labels_match(profile,section):
    assert not member_record_findings(model(profile,section))


def test_one_correct_record_does_not_hide_a_second_wrong_link():
    package=model(section='W14X90')
    package['design_records'].append(dict(id='members/wrong',table='members',data=dict(inputs=dict(section='W24X76'))))
    package['physical']['members'][0]['design_record_ids'].append('members/wrong')
    assert member_record_findings(package)[0]['count']==1


@pytest.mark.parametrize('package',[model('CUSTOM-A','W14X90'),model(family='built_up'),model(section=None)])
def test_unproven_labels_and_constituent_designs_are_not_guessed(package):
    assert not member_record_findings(package)


def test_connection_records_do_not_become_member_section_checks():
    package=model();package['design_records'][0]['table']='connections'
    assert not member_record_findings(package)


def test_ready_package_cannot_override_wrong_member_link():
    package=json.loads((Path(__file__).parent/'fixtures/synthetic_bim_package.json').read_text())
    profile=package['physical']['profiles'][0];old=profile['id'];profile['id']='W14X90'
    for member in package['physical']['members']:
        if member['profile_id']==old:member['profile_id']=profile['id']
    target=package['physical']['members'][0];target['design_record_ids']=['members/wrong']
    package['design_records'].append(dict(id='members/wrong',table='members',source_pointer='/members/0',waived=False,data=dict(inputs=dict(section='W24X76'))))
    package['readiness']['status']='ready'
    assert 'member_design_section' in {finding['code'] for finding in validate(package)}

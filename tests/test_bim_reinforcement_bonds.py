import copy,json
from pathlib import Path

import pytest
import jsonschema

try:
    from steel_engine import bim_assemblies as A,bim_bonds as R
except ModuleNotFoundError:
    from steltic_openbim import bim_assemblies as A,bim_bonds as R


def fixture(kind='reinforcement_bond_lap'):
    members=[dict(id='bar0',kind='reinforcement_bar',axis=[[5,10,-1],[45,10,-1]],
        profile_id='R5',material_id='steel',local_y=[0,1,0]),
        dict(id='bar1',kind='reinforcement_bar',axis=[[20,10.625,-1],[60,10.625,-1]],
        profile_id='R5',material_id='steel',local_y=[0,1,0])]
    bars=['BAR0','BAR1'] if kind.endswith('lap') else ['BAR0']
    receivers=[dict(id='BAR'+str(i),kind='member',axis=copy.deepcopy(m['axis']),profile_id='R5') for i,m in enumerate(members)]
    if len(bars)==1:receivers=receivers[:1]
    receivers.append(dict(id='HOST',kind='panel'))
    bond=dict(id='BOND',kind=kind,host_receiver='HOST',bar_receivers=bars,
        basis='Accepted fictional test fit only',engineering_status='not_verified')
    if len(bars)==2:bond['length_in']=25
    template=dict(id='bond',parts=[],receivers=receivers,bond_interfaces=[bond],source_record_ids=[],basis='Test fit, no capacity approval')
    template['revision']=A.fingerprint(template)
    bindings={r['id']:('floor' if r['id']=='HOST' else 'bar'+r['id'][-1]) for r in receivers}
    c=dict(id='joint',parts=[],design_record_ids=[],member_ids=[bindings[r] for r in bars],assembly_instance=dict(
        template_id='bond',revision=template['revision'],origin=[0,0,0],x_axis=[1,0,0],y_axis=[0,1,0],bindings=bindings))
    return dict(members=members,profiles=[dict(id='R5',family='round',dimensions=dict(d=.625))],
        panels=[dict(id='floor',kind='floor',vertices=[[0,0,0],[100,0,0],[100,50,0],[0,50,0]],
                     thickness=2.5,thickness_direction='negative_global_z',material_id='concrete')],
        materials=[dict(id='concrete',specification="Lightweight concrete f'c=3000 psi"),dict(id='steel',specification='Source Gr60 reinforcing steel')],
        openings=[],interfaces=[],assemblies=[template],connections=[c],joints=[])


@pytest.mark.parametrize('kind',['reinforcement_bond_development','reinforcement_bond_lap'])
def test_valid_bonds_require_actual_host_geometry_before_satisfying_connection(kind):
    p=fixture(kind);errors,valid=R.evaluate(p)
    assert errors==[] and valid=={'joint'}
    c=p['connections'][0];templates=A.index(p)
    assert not A.has_physical_connection(c,templates)
    assert A.has_physical_connection(c,templates,verified_bonds=valid)
    assert A.findings(dict(physical=p,design_records=[]))==[]


@pytest.mark.parametrize('defect',['missing_host','wrong_host_type','wrong_material','unknown_datum','outside_edge',
    'outside_thickness','wrong_lap','noncontact','nonparallel','fake_bar','unknown_opening','through_opening','reservation_opening',
    'wrong_receiver_axis','malformed_bonds','unverified_basis_missing'])
def test_invalid_bonds_never_count_as_a_physical_connection(defect):
    p=fixture();t=p['assemblies'][0];c=p['connections'][0];host=p['panels'][0]
    if defect=='missing_host':p['panels']=[]
    elif defect=='wrong_host_type':c['assembly_instance']['bindings']['HOST']='bar0'
    elif defect=='wrong_material':p['materials'][0]['specification']='Wood sheathing'
    elif defect=='unknown_datum':host.pop('thickness_direction')
    elif defect=='outside_edge':p['members'][0]['axis'][0][0]=-.1
    elif defect=='outside_thickness':host['thickness']=.5
    elif defect=='wrong_lap':t['bond_interfaces'][0]['length_in']=30
    elif defect=='noncontact':
        for q in p['members'][1]['axis']:q[1]+=1
        for q in t['receivers'][1]['axis']:q[1]+=1
    elif defect=='nonparallel':
        p['members'][1]['axis'][1][1]+=1;t['receivers'][1]['axis'][1][1]+=1
    elif defect=='fake_bar':p['members'][0]['kind']='column'
    elif defect=='unknown_opening':host['openings']=['missing']
    elif defect in ('through_opening','reservation_opening'):
        o=dict(id='opening',vertices=[[30,9,0],[35,9,0],[35,12,0],[30,12,0]])
        host['openings']=['opening']
        if defect=='through_opening':p['openings']=[o]
        else:p['interfaces']=[dict(id='reservation:opening',kind='opening_reservation',vertices=o['vertices'],source_opening=dict(id='opening'))]
    elif defect=='wrong_receiver_axis':t['receivers'][0]['axis'][0][1]+=1
    elif defect=='malformed_bonds':t['bond_interfaces']={'not':'a list'}
    else:t['bond_interfaces'][0].pop('basis')
    errors,valid=R.evaluate(p)
    assert errors and not valid
    assert not A.has_physical_connection(c,A.index(p),verified_bonds=valid)


def test_whole_bar_bond_cannot_dispose_of_an_unchecked_remote_end():
    p=fixture('reinforcement_bond_development')
    p['assemblies'][0]['receivers'][0]['axis'][1][0]=30
    errors,valid=R.evaluate(p)
    assert errors and not valid


def test_empty_hardware_is_schema_valid_only_with_a_typed_bond():
    root=json.loads(Path(A.__file__).with_name('bim_package.schema.json').read_text())
    schema=dict(root['properties']['physical']['properties']['assemblies']['items'],**{'$defs':root['$defs']})
    template=fixture()['assemblies'][0]
    jsonschema.validate(template,schema)
    template.pop('bond_interfaces')
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(template,schema)


def test_bad_assembly_revision_still_fails_even_for_a_geometric_bond():
    p=fixture();p['assemblies'][0]['basis']='Changed after authoring'
    assert any(f['code']=='assembly_revision' for f in A.findings(dict(physical=p,design_records=[])))


def test_member_and_host_with_same_id_are_bound_by_type():
    p=fixture();p['panels'][0]['id']='bar0'
    p['connections'][0]['assembly_instance']['bindings']['HOST']='bar0'
    assert R.evaluate(p)==([],{'joint'})
    assert A.findings(dict(physical=p,design_records=[]))==[]
    p['panels']=[]
    assert any(f['code']=='assembly_binding' for f in A.findings(dict(physical=p,design_records=[])))


@pytest.mark.parametrize('kind',['reinforcement_bond_development','reinforcement_bond_lap'])
def test_transformed_and_reversed_bars_preserve_fit_and_reference_extent(kind):
    p=fixture(kind);frame=p['connections'][0]['assembly_instance']
    frame.update(origin=[200,300,48],x_axis=[0,1,0],y_axis=[-1,0,0])
    for member in p['members']:
        member['axis']=[A.transformed(q,frame) for q in reversed(member['axis'])]
    p['panels'][0]['vertices']=[A.transformed(q,frame) for q in p['panels'][0]['vertices']]
    if kind.endswith('lap'):
        p['assemblies'][0]['receivers'][1]['axis'].reverse()
    assert R.evaluate(p)==([],{'joint'})
    lines=R.reference_lines(p['assemblies'][0])
    import math
    assert [math.dist(*line) for line in lines]==([25,25] if kind.endswith('lap') else [40])


def test_malformed_concrete_opening_references_report_a_finding():
    p=fixture();p['panels'][0]['openings']=[dict(id='bad')]
    errors,valid=R.evaluate(p)
    assert errors and not valid

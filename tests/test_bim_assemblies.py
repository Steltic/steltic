"""Located assemblies must retain revisions, actual receivers and selected parts."""
import copy
from test_bim_export import exported,B
from steel_engine import bim_assemblies as A


def assembly(package):
    p=copy.deepcopy(package);connection=p['physical']['connections'][0]
    template=dict(id='clip',parts=connection.pop('parts'),source_record_ids=['connections/c1'],
                  receivers=[dict(id='beam',kind='member',axis=[[0,0,0],[24,0,0]],profile_id='TEST')],
                  basis='Synthetic selected plate assembly, not an engineering design')
    template['revision']=A.fingerprint(template);p['physical']['assemblies']=[template]
    connection.update(parts=[],assembly_instance=dict(template_id='clip',revision=template['revision'],
        origin=[0,0,0],x_axis=[1,0,0],y_axis=[0,1,0],bindings=dict(beam='beam')))
    return p


def test_template_binds_to_real_receiver_and_keeps_native_validation(exported):
    p=assembly(exported[2]);assert B.validate(p)==[]
    p['physical']['connections'][0]['assembly_instance']['origin']=[1000,0,0]
    assert 'assembly_joint' in {f['code'] for f in B.validate(p)}
    p=assembly(exported[2]);p['physical']['connections'][0]['assembly_instance']['x_axis']=[0,1,0]
    assert 'assembly_frame' in {f['code'] for f in B.validate(p)}
    p=assembly(exported[2]);p['physical']['members'][0]['axis'][0][1]=1
    assert 'assembly_receiver_fit' in {f['code'] for f in B.validate(p)}


def test_template_parts_cannot_change_without_new_occurrence_revision(exported):
    p=assembly(exported[2]);p['physical']['assemblies'][0]['parts'][0]['geometry']['size'][0]=1
    assert 'assembly_revision' in {f['code'] for f in B.validate(p)}
    template=p['physical']['assemblies'][0];template['revision']=A.fingerprint({k:v for k,v in template.items() if k!='revision'})
    assert 'assembly_revision' in {f['code'] for f in B.validate(p)}


def test_fictional_connection_cannot_disguise_missing_engineering_evidence(exported):
    p=assembly(exported[2]);c=p['physical']['connections'][0]
    c.update(scope='coordination',design_record_ids=[],decision_ids=['Q'],engineering_status='not_checked')
    assert 'connection_coordination_basis' in {f['code'] for f in B.validate(p)}
    # Accepted fictional information is still not allowed to drop a selected
    # design record from a template originally derived from that design.
    p['information']=dict(revision=1,fictional=True,requests=[dict(id='Q',status='accepted_test',fictional=True,quantitatively_resolved=True)],decisions=[])
    assert 'assembly_design' in {f['code'] for f in B.validate(p)}


def test_rigid_transformation_keeps_shape_and_places_every_component():
    frame=dict(origin=[120,240,360],x_axis=[0,1,0],y_axis=[-1,0,0])
    geometry=dict(type='cylinder',origin=[1,2,3],axis=[1,0,0],length=4,radius=.375)
    placed=A.transform_geometry(geometry,frame)
    assert placed==dict(type='cylinder',origin=[118,241,363],axis=[0,1,0],length=4,radius=.375)
    assert geometry['origin']==[1,2,3]


def test_weld_only_joint_requires_two_distinct_bound_physical_members():
    template=dict(id='welded',receivers=[dict(id='left',kind='member'),dict(id='right',kind='member')],
        parts=[dict(id='W',kind='weld',connects=['left','right'],geometry=dict(type='polyline',points=[[0,0,0],[0,12,0]]))])
    c=dict(parts=[],assembly_instance=dict(template_id='welded',origin=[0,0,0],x_axis=[1,0,0],y_axis=[0,1,0],bindings=dict(left='m1',right='m2')))
    assert A.has_physical_connection(c,{'welded':template})
    c['assembly_instance']['bindings']['right']='m1'
    assert not A.has_physical_connection(c,{'welded':template})
    c['assembly_instance']['bindings'].pop('right')
    assert not A.has_physical_connection(c,{'welded':template})
    c['assembly_instance']['bindings']['right']='m2'
    template['parts'][0]['kind']='continuity_note'
    assert not A.has_physical_connection(c,{'welded':template})

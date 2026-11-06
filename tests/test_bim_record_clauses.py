import copy
from steel_engine.bim_contract import connection_component_findings


def fixture():
    text='base: 5/8 in. anchor bolts at 8 in. o.c.; floors: No. 12 screws at 2 in. o.c.'
    p=dict(require_connection_parts=True,design_records=[dict(id='a',table='anchorage',data=dict(selection=text))],
           physical=dict(members=[],connections=[],assemblies=[],materials=[]))
    for level,label,selected,hardware in [(1,'base','5/8 in. anchor bolts at 8 in. o.c.','anchor'),(2,'floors','No. 12 screws at 2 in. o.c.','screw')]:
        mid='track'+str(level);cid='joint'+str(level)
        p['physical']['members'].append(dict(id=mid,kind='bottom_track',level=level))
        p['physical']['connections'].append(dict(id=cid,joint_id=cid,member_ids=[mid],scope='designed',design_record_ids=['a'],
            parts=[dict(id='hardware',kind=hardware)],source_record_selections=[dict(record_id='a',field='selection',clause=label,text=selected,level=level,basis='Exact labelled source schedule at this track storey')]))
    return p


def test_base_and_floor_clauses_preserve_full_source_and_require_different_hardware():
    p=fixture();source=copy.deepcopy(p['design_records'])
    assert connection_component_findings(p)==[]
    assert p['design_records']==source
    p['physical']['connections'][1]['parts']=[]
    failures=connection_component_findings(p)
    assert any(f['code']=='connection_selected_components' and f['entity_ids']==['joint2'] for f in failures)


def test_an_unapplied_clause_is_a_failure_even_when_base_hardware_exists():
    p=fixture();p['physical']['connections'].pop()
    assert any('No located application' in f['message'] for f in connection_component_findings(p))


def test_clause_must_be_exact_source_and_the_matching_receiving_storey():
    for field,value in [('text','No anchors required'),('level',2),('clause','floors')]:
        p=fixture();p['physical']['connections'][0]['source_record_selections'][0][field]=value
        assert any(f['code']=='source_record_selection' for f in connection_component_findings(p))


def test_unlabelled_record_cannot_use_clause_selection_to_drop_requirements():
    p=fixture();p['design_records'][0]['data']['selection']='Anchor bolts and screws'
    assert any(f['code']=='source_record_selection' for f in connection_component_findings(p))


def test_malformed_clause_input_returns_a_finding_instead_of_crashing():
    for malformed in ['bad',None,[None],[dict(record_id=[],field={},clause=[])]]:
        p=fixture();p['physical']['connections'][0]['source_record_selections']=malformed
        assert any(f['code']=='source_record_selection' for f in connection_component_findings(p))


def post_fixture():
    p=dict(require_connection_parts=True,design_records=[dict(id='p',table='connections',data=dict(components='top: two bolts and a welded clip; base: four anchor rods and welded washers'))],
           physical=dict(members=[dict(id='post',kind='endwall_post',level=1,axis=[[0,0,0],[0,0,456]])],connections=[],assemblies=[],materials=[]))
    for label,z,text,kinds in [('base',0,'four anchor rods and welded washers',['anchor','weld']),('top',456,'two bolts and a welded clip',['bolt','weld'])]:
        p['physical']['connections'].append(dict(id=label,joint_id=label,point=[0,0,z],member_ids=['post'],scope='designed',design_record_ids=['p'],
            parts=[dict(id=k,kind=k) for k in kinds],source_record_selections=[dict(record_id='p',field='components',clause=label,text=text,level=1,basis='Actual post end')]))
    return p


def test_post_head_and_base_have_different_source_hardware_at_actual_ends():
    p=post_fixture();before=copy.deepcopy(p['design_records'])
    assert connection_component_findings(p)==[] and p['design_records']==before
    p['physical']['connections'][1]['parts'].pop(0)
    assert any(f['code']=='connection_selected_components' for f in connection_component_findings(p))


def test_post_clause_cannot_be_moved_to_wrong_end_unrelated_member_or_hide_a_clause():
    for mutation in ('point','kind','missing','source','malformed'):
        p=post_fixture()
        if mutation=='point':p['physical']['connections'][1]['point']=[0,0,100]
        if mutation=='kind':p['physical']['members'][0]['kind']='beam'
        if mutation=='missing':p['physical']['connections'].pop()
        if mutation=='source':p['physical']['connections'][1]['source_record_selections'][0]['text']='welded clip'
        if mutation=='malformed':p['physical']['members'][0]['axis']=[None,None]
        assert any(f['code']=='source_record_selection' for f in connection_component_findings(p)),mutation


def portal_fixture():
    from steel_engine.bim_record_clauses import clauses
    text='exterior bases: three gussets welded to plate; four A325 bolts per pair and four anchor rods; interior HSS6X6X1/4 columns: four anchor rods and a perimeter weld'
    options=clauses(text)
    p=dict(require_connection_parts=True,design_records=[dict(id='base',table='anchorage',data=dict(selection=text))],
           physical=dict(members=[],connections=[],assemblies=[],materials=[]))
    for ident,x,kind in [('left',0,'column'),('prop',600,'prop'),('right',1032,'column')]:
        m=dict(id=ident,kind=kind,level=1,axis=[[x,0,0],[x,0,216]],profile_id='HSS6X6X1/4' if kind=='prop' else '6X1400S300-97')
        label='interior hss6x6x1/4 columns' if kind=='prop' else 'exterior bases'
        kinds=['anchor','weld']+(['bolt'] if kind=='column' else [])
        p['physical']['members'].append(m)
        p['physical']['connections'].append(dict(id=ident+'-base',joint_id=ident+'-base',point=m['axis'][0][:],member_ids=[ident],scope='designed',design_record_ids=['base'],
            parts=[dict(id=k,kind=k) for k in kinds],source_record_selections=[dict(record_id='base',field='selection',clause=label,text=options[label],level=1,basis='Actual flanked valley prop or exterior frame-column base')]))
    return p


def test_portal_base_clauses_keep_source_and_select_actual_different_hardware():
    p=portal_fixture();before=copy.deepcopy(p['design_records'])
    assert connection_component_findings(p)==[] and p['design_records']==before
    p['physical']['connections'][0]['parts'].pop()
    assert any(f['code']=='connection_selected_components' for f in connection_component_findings(p))


def test_portal_clause_requires_real_flanking_columns_base_and_named_hss():
    for mutation in ('point','profile','kind','outside','missing','source','malformed'):
        p=portal_fixture();prop=p['physical']['members'][1];c=p['physical']['connections'][1]
        if mutation=='point':c['point'][2]=216
        if mutation=='profile':prop['profile_id']='HSS4X4X1/4'
        if mutation=='kind':prop['kind']='beam'
        if mutation=='outside':
            prop['axis']=[[1200,0,0],[1200,0,216]];c['point']=[1200,0,0]
        if mutation=='missing':p['physical']['connections'].pop(1)
        if mutation=='source':c['source_record_selections'][0]['text']='No anchors required'
        if mutation=='malformed':prop['axis']=[None,None]
        assert any(f['code']=='source_record_selection' for f in connection_component_findings(p)),mutation


def test_portal_selections_are_accepted_by_the_shipped_package_schema():
    import json
    from pathlib import Path
    import jsonschema
    from steel_engine import bim_contract
    schema=json.loads(Path(bim_contract.__file__).with_name('bim_package.schema.json').read_text())
    selection=schema['$defs']['connection']['properties']['source_record_selections']['items']
    validator=jsonschema.Draft202012Validator(selection)
    for c in portal_fixture()['physical']['connections']:
        entry=c['source_record_selections'][0]
        assert not list(validator.iter_errors(entry))
        assert list(validator.iter_errors(dict(entry,clause='unlabelled arbitrary subset')))

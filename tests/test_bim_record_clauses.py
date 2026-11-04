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

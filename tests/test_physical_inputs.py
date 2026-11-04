"""Native physical-input recording, concurrency and design-provenance regressions."""
import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from steel_engine import physical_inputs as P
from steel_engine import bim_export as B
from test_bim_export import MODULE,PACKAGE,job,declarations


def native_job(tmp_path):
    root=job(tmp_path)
    # Move the SAME already-selected declarations into the first-class input.
    text=(root/'cfg.py').read_text(encoding='utf-8')
    text=text[:text.index("cfg['bim']")]
    (root/'cfg.py').write_text(text,encoding='utf-8')
    stamp=B.read_json(root/'design/consistency_result.json');stamp['cfg_sha256']=B.digest(root/'cfg.py')
    B.atomic_json(root/'design/consistency_result.json',stamp)
    model=P.PhysicalModel(MODULE);model.value['declarations']=declarations()
    model.save(root);P.bind_checked_design(root)
    return root


def test_native_input_export_does_not_change_checked_design(tmp_path):
    root=native_job(tmp_path)
    before={p.name:p.read_bytes() for p in (root/'cfg.py',root/'design'/PACKAGE,root/'design/consistency_result.json')}
    p=B.replay(root,tmp_path/'out/bim_package.json',30)
    assert p['readiness']['status']=='ready',p['readiness']
    assert p['physical']['members'][0]['declaration_source']=='physical_inputs.json#/declarations'
    assert p['provenance']['physical_inputs']['sha256']==P.digest(root/P.NAME)
    assert p['provenance']['model_capture']['independent_configuration_rebuild'] is True
    assert before=={p.name:p.read_bytes() for p in (root/'cfg.py',root/'design'/PACKAGE,root/'design/consistency_result.json')}


def test_source_changes_do_not_rebind_during_export(tmp_path):
    root=native_job(tmp_path);before=(root/P.NAME).read_bytes()
    (root/'cfg.py').write_text((root/'cfg.py').read_text()+'\nchanged_design_input=True\n')
    p=B.replay(root,tmp_path/'out/bim_package.json',30)
    codes={f['code'] for f in p['readiness']['findings']}
    assert {'design_provenance','physical_design_binding'}<=codes
    assert (root/P.NAME).read_bytes()==before
    with pytest.raises(ValueError,match='stale'):P.bind_checked_design(root)


def test_information_decisions_are_revisioned_and_cannot_rewrite_design(tmp_path):
    root=native_job(tmp_path);model=P.PhysicalModel(MODULE,P.read(root))
    model.request('Q1','Confirm selected member end offset','Keep the recorded offset; TBD until confirmed',
        bindings=[dict(collection='members',entity_id='beam',field='end_offsets')])
    model.save(root);register=P.request_register(root)
    baseline=(root/'design'/PACKAGE).read_bytes()
    args=dict(request_id='Q1',author='Engineer',note='Coordinated with the architectural clearance',action='confirm',
        expected_revision=register['revision'],expected_sha256=register['sha256'])
    with pytest.raises(ValueError,match='outside'):
        P.apply_decision(root,**args,changes=[dict(collection='members',entity_id='beam',field='design_record_ids',value=['invented'])])
    answer=P.apply_decision(root,**args,changes=[dict(collection='members',entity_id='beam',field='end_offsets',value=[[0,0,0],[-1,0,0]])])
    assert answer['structural_review_required']
    assert P.request_register(root)['requests'][0]['quantitatively_resolved']
    with pytest.raises(ValueError,match='refresh'):P.apply_decision(root,**args)
    assert (root/'design'/PACKAGE).read_bytes()==baseline
    assert any(f['code']=='physical_input_design_review' for f in P.binding_findings(root,P.read(root)))


def test_accepted_proposal_is_not_quantitative_confirmation(tmp_path):
    root=native_job(tmp_path);model=P.PhysicalModel(MODULE,P.read(root))
    model.request('opening','Confirm door position','Use the east bay, dimensions TBD');model.save(root)
    register=P.request_register(root)
    r=P.apply_decision(root,request_id='opening',author='Engineer',note='Keep proposal for coordination',action='accept_proposal',
        expected_revision=register['revision'],expected_sha256=register['sha256'])
    assert r['request']['status']=='accepted_proposal' and not r['request']['quantitatively_resolved']
    assert P.request_register(root)['can_generate_provisional']


def test_repeated_members_are_actual_occurrences_not_group_proxies():
    model=P.PhysicalModel(MODULE)
    model.repeated_members('beam-', 'beam',[[0,0,120],[240,0,120]],[0,96,192],[0,1,0],
        profile_id='TEST',material_id='steel',local_y=[0,1,0],reference_line='centroid',end_offsets=[[0,0,0],[0,0,0]])
    ms=model.value['declarations']['members']
    assert [m['axis'][0] for m in ms]==[[0,0,120],[0,96,120],[0,192,120]]
    assert len({m['id'] for m in ms})==3


@pytest.mark.skipif(MODULE!='HR',reason='HR native OpenSees snapshot')
def test_portable_checked_model_survives_missing_helper_and_detects_tampering(tmp_path):
    from steel_engine import portable_model as M
    root=native_job(tmp_path)
    text='''import openseespy.opensees as ops
ops.wipe()
ops.model('basic','-ndm',3,'-ndf',6)
ops.node(1,0,0,0)
ops.node(2,120,0,0)
ops.geomTransf('Linear',1,0,0,1)
ops.element('elasticBeamColumn',1,1,2,10,29000,11000,1,20,100,1)
'''
    (root/'model_opensees.py').write_text(text)
    (root/'viewer_3d.html').write_text('const DATA='+json.dumps(dict(elements=[dict(tag=1,type='beam',sec='TEST',n=[1,2])],meta=dict(levels=[0])))+';')
    (root/'cfg.py').write_text('import unarchived_gold_helper\ncfg={}\n')
    stamp=B.read_json(root/'design/consistency_result.json');stamp['cfg_sha256']=B.digest(root/'cfg.py');B.atomic_json(root/'design/consistency_result.json',stamp)
    P.bind_checked_design(root);M.freeze(root)
    output=B.replay(root,tmp_path/'out/bim_package.json',30)
    assert output['readiness']['status']=='ready',output['readiness']
    assert output['provenance']['model_capture']['kind']=='portable_checked_model'
    assert output['provenance']['model_capture']['independent_configuration_rebuild'] is False
    snapshot=P.read(root);assert snapshot is not None
    data=json.loads((root/M.NAME).read_text());data['metadata']['ele'][0][2]='DIFFERENT'
    P.atomic_json(root/M.NAME,data)
    with pytest.raises(ValueError,match='metadata differs'):M.read(root)


def test_design_binding_cannot_escape_job(tmp_path):
    root=native_job(tmp_path);data=P.read(root)
    outside=tmp_path/'outside.txt';outside.write_text('private input')
    data['design_sources']['../outside.txt']=P.digest(outside)
    assert any(f['code']=='physical_design_binding' for f in P.binding_findings(root,data))


@pytest.mark.skipif(MODULE!='HR',reason='HR saved-model completion integration')
def test_completion_automatically_archives_checked_model_before_native_replay(tmp_path):
    from steel_engine import portable_model as M
    root=native_job(tmp_path)
    (root/'model_opensees.py').write_text("import openseespy.opensees as ops\nops.wipe()\nops.model('basic','-ndm',3,'-ndf',6)\nops.node(1,0,0,0)\nops.node(2,120,0,0)\nops.geomTransf('Linear',1,0,0,1)\nops.element('elasticBeamColumn',1,1,2,10,29000,11000,1,20,100,1)\n")
    (root/'viewer_3d.html').write_text('const DATA='+json.dumps(dict(elements=[dict(tag=1,type='beam',sec='TEST',n=[1,2])],meta=dict(levels=[0])))+';')
    (root/'cfg.py').write_text('import unavailable_completed_model_helper\ncfg={}\n')
    stamp=B.read_json(root/'design/consistency_result.json');stamp['cfg_sha256']=B.digest(root/'cfg.py');B.atomic_json(root/'design/consistency_result.json',stamp)
    original=(root/'design'/PACKAGE).read_bytes()
    assert B.after_consistency(root,{},['Synthetic engineering failure'])==[]
    assert not (root/M.NAME).exists() and not (root/'design/bim_package.json').exists()
    assert B.after_consistency(root,{},[])==[]
    assert (root/M.NAME).is_file()
    output=B.read_json(root/'design/bim_package.json')
    assert output['readiness']['status']=='ready'
    assert output['provenance']['model_capture']['kind']=='portable_checked_model'
    assert output['provenance']['model_capture']['independent_configuration_rebuild'] is False
    assert (root/'design'/PACKAGE).read_bytes()==original
    assert B.completion_issues(root)==[]


def test_affected_engineering_review_needs_explicit_current_checked_records(tmp_path):
    root=native_job(tmp_path);model=P.PhysicalModel(MODULE,P.read(root))
    model.request('Q1','Confirm end fit','Review the recorded offset',bindings=[dict(collection='members',entity_id='beam',field='end_offsets')])
    model.save(root);reg=P.request_register(root)
    P.apply_decision(root,request_id='Q1',author='Engineer',note='Coordinated bearing change',action='confirm',
        expected_revision=reg['revision'],expected_sha256=reg['sha256'],changes=[dict(collection='members',entity_id='beam',field='end_offsets',value=[[0,0,0],[-1,0,0]])])
    # A normal consistency binding cannot acknowledge the geometry review.
    P.bind_checked_design(root);reg=P.request_register(root)
    assert reg['pending_design_reviews']
    args=dict(request_id='Q1',author='Checking engineer',note='Reviewed revised bearing against the selected connection record',expected_revision=reg['revision'],expected_sha256=reg['sha256'])
    before=(root/P.NAME).read_bytes()
    with pytest.raises(ValueError,match='Unknown checked'):P.record_design_review(root,**args,source_pointers=['/missing/0'])
    assert (root/P.NAME).read_bytes()==before
    calc=json.loads((root/'design'/PACKAGE).read_text())
    pointer=next('/'+k+'/0' for k,v in calc.items() if isinstance(v,list) and v and isinstance(v[0],dict) and v[0].get('id'))
    result=P.record_design_review(root,**args,source_pointers=[pointer])
    assert result['review']['records'][0]['record_sha256']
    assert not P.request_register(root)['pending_design_reviews']
    with pytest.raises(ValueError,match='refresh'):P.record_design_review(root,**args,source_pointers=[pointer])

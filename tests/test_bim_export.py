"""Contract, replay isolation and source-preservation regression tests.

Fixtures are synthetic data-contract examples, not structural designs.
This file is shared with the independently installed CFS module.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from steel_engine import bim_export as B

MODULE="CFS" if 'MODULE="CFS"' in (ROOT/"steel_engine/bim_adapter.py").read_text() else "HR"
PACKAGE="calc_package_cfs.json" if MODULE=="CFS" else "calc_package.json"


def declarations():
    return {"schema_version":"1.0","required":True,
        "profiles":[{"id":"TEST","family":"i","dimensions":{"d":8,"bf":4,"tf":0.4,"tw":0.25}}],
        "materials":[{"id":"steel","specification":"Synthetic fixture material; not a design"}],
        "members":[{"id":"beam","kind":"beam","axis":[[0,0,0],[120,0,0]],"local_y":[0,1,0],"profile_id":"TEST","material_id":"steel",
                    "reference_line":"centroid","end_offsets":[[0,0,0],[0,0,0]],"analysis_ids":["hr:element:1"] if MODULE=="HR" else []}],
        "joints":[{"id":"support","point":[0,0,0],"member_ends":[{"member_id":"beam","end":"start"}]},
                  {"id":"free","point":[120,0,0],"member_ends":[{"member_id":"beam","end":"end"}]}],
        "joint_dispositions":[{"joint_id":"free","reason":"Synthetic free end"}],
        "coverage":{k:{"complete":True,"basis":"Complete synthetic one-member fixture; no other geometry"} for k in B.COVERAGE}}


def connections():
    return {"schema_version":"1.0","connections":[{"id":"support-detail","joint_id":"support","scope":"designed","design_record_ids":["connections/c1"],
            "parts":[{"id":"plate","kind":"plate","material_id":"steel","geometry":{"type":"box","origin":[0,0,0],"size":[0.5,4,6],"x_axis":[1,0,0],"y_axis":[0,1,0]}}]}]}


def job(tmp_path, cfg_tail="", missing_import=False):
    root=tmp_path/"job"; (root/"design").mkdir(parents=True)
    if MODULE=="HR":
        script='''import openseespy.opensees as ops
def build(cfg, transf):
    ops.wipe()
    ops.model('basic','-ndm',3,'-ndf',6)
    ops.node(1,0,0,0)
    ops.node(2,120,0,0)
    ops.geomTransf('Linear',1,0,0,1)
    ops.element('elasticBeamColumn',1,1,2,10,29000,11000,1,20,100,1)
    return dict(ele=[(1,'beam','TEST',1,2)],z=[0],NF=0,cm={},present={})
cfg=dict(custom_build=build,present={})
'''
    else:
        script="cfg=dict(heights_ft=[10],lines_x=[],lines_y=[])\n"
    script+="cfg['bim']="+repr(declarations())+"\n"+cfg_tail
    if missing_import: script="import absent_bim_fixture_dependency\n"+script
    (root/"cfg.py").write_text(script,encoding="utf-8")
    pkg={"members":[],"connections":[{"id":"c1","selection":"Synthetic fixture only","DC":0.5}],"bim":connections()}
    B.atomic_json(root/"design"/PACKAGE,pkg)
    stamp={"result":"PASS","calc_package_sha256":B.digest(root/"design"/PACKAGE),"cfg_sha256":B.digest(root/"cfg.py")}
    B.atomic_json(root/"design/consistency_result.json",stamp)
    return root


@pytest.fixture
def exported(tmp_path):
    root=job(tmp_path)
    out=tmp_path/"out/bim_package.json"
    return root,out,B.replay(root,out,20)


def test_complete_declared_package_round_trip_and_no_source_changes(exported,tmp_path):
    root,out,p=exported
    before=B.tracked_sources(root)
    assert p["readiness"]["status"]=="ready",p["readiness"]
    assert B.validate(p)==[]
    assert len(p["physical"]["members"])==1
    assert p["provenance"]["analysis_executed"] is False
    q=B.replay(root,tmp_path/"second/bim_package.json",20)
    assert q==p
    assert B.tracked_sources(root)==before
    assert not (root/"design/bim_package.json").exists()


@pytest.mark.parametrize("mutation,code",[
    (lambda p:p["units"].update(length="ft"),"schema"),
    (lambda p:p["physical"]["members"][0].update(local_y=[1,0,0]),"member_axes"),
    (lambda p:p["physical"]["members"][0].update(material_id="missing"),"member_material"),
    (lambda p:p["physical"]["members"][0].update(profile_id="missing"),"member_profile"),
    (lambda p:p["physical"]["members"][0].update(end_offsets=None),"member_placement"),
    (lambda p:p["physical"]["connections"][0].update(joint_id="missing"),"connection_joint"),
    (lambda p:p["physical"]["joints"][0].update(point=[1000,0,0]),"joint_placement"),
    (lambda p:p["physical"]["connections"][0].update(design_record_ids=["connections/missing"]),"connection_design"),
    (lambda p:p["physical"]["connections"][0]["parts"][0]["geometry"].update(size=[1,-1,3]),"connection_geometry"),
    (lambda p:p["physical"]["connections"][0].update(parts=[]),"connection_geometry"),
    (lambda p:p["physical"]["coverage"].pop("openings"),"coverage_openings"),
    (lambda p:p["physical"]["profiles"][0].update(dimensions={"d":8}),"profile_geometry"),
    (lambda p:p["physical"]["members"].append(copy.deepcopy(p["physical"]["members"][0])),"duplicate_id"),
    (lambda p:p["physical"]["members"][0]["axis"][0].__setitem__(0,float("nan")),"json_value"),
])
def test_invalid_data_never_becomes_ready(exported,mutation,code):
    p=copy.deepcopy(exported[2]); mutation(p)
    p["readiness"]={"status":"ready"}  # consumer must recompute
    assert code in {f["code"] for f in B.validate(p)}


def test_stale_source_fails_completion_gate(exported):
    root,out,p=exported
    B.atomic_json(root/"design/bim_package.json",p)
    assert not B.completion_issues(root)
    (root/"cfg.py").write_text((root/"cfg.py").read_text()+"\n# changed\n")
    assert "stale" in B.completion_issues(root)[0]


def test_required_missing_export_cannot_pass(tmp_path):
    root=job(tmp_path)
    B.atomic_json(root/"design/bim_request.json",{"required":True})
    assert "missing" in B.completion_issues(root)[0]


def test_analysis_is_disabled_and_does_not_publish(tmp_path):
    root=job(tmp_path,"\nimport openseespy.opensees as ops\nops.analyze(1)\n")
    before=B.tracked_sources(root)
    out=tmp_path/"out/bim_package.json"
    with pytest.raises(RuntimeError,match="disabled"):
        B.replay(root,out,20)
    assert not out.exists()
    assert B.tracked_sources(root)==before


def test_timeout_does_not_publish(tmp_path):
    root=job(tmp_path,"\nimport time\ntime.sleep(10)\n")
    out=tmp_path/"out/bim_package.json"
    with pytest.raises(subprocess.TimeoutExpired): B.replay(root,out,0.3)
    assert not out.exists()


def test_source_cannot_be_overwritten(tmp_path):
    root=job(tmp_path)
    before=B.digest(root/"cfg.py")
    with pytest.raises(ValueError,match="source"): B.replay(root,root/"cfg.py")
    assert B.digest(root/"cfg.py")==before


def test_finalization_hook_creates_required_export(tmp_path):
    root=job(tmp_path)
    findings=B.after_consistency(root,{"bim":{"required":True}},[])
    assert findings==[],findings
    assert B.read_json(root/"design/bim_request.json")["required"]
    assert B.read_json(root/"design/bim_package.json")["readiness"]["status"]=="ready"


def test_legacy_consistency_is_not_forced_to_export(tmp_path):
    root=job(tmp_path)
    assert B.after_consistency(root,{},[])==[]
    assert not (root/"design/bim_package.json").exists()


def test_new_jobs_require_bim_but_existing_jobs_keep_legacy_rules(tmp_path):
    from steltic.job_tools import JobWorkspace
    ws=JobWorkspace(tmp_path/"jobs")
    ws.new_activity_log("new")
    assert "Physical data export" in ws.read_file("contract/BIM_EXPORT.md")["content"]
    assert B.read_json(tmp_path/"jobs/new/design/bim_request.json")["required"]
    assert B.completion_issues(tmp_path/"jobs/new")
    existing=tmp_path/"jobs/legacy"; existing.mkdir()
    (existing/"cfg.py").write_text("cfg={}")
    ws.new_activity_log("legacy")
    assert not (existing/"design/bim_request.json").exists()


def test_new_job_cannot_skip_bim_declaration(tmp_path):
    root=job(tmp_path)
    B.atomic_json(root/"design/bim_request.json",{"required":True})
    assert "declaration missing" in B.after_consistency(root,{},[])[0]


def test_required_request_enforces_readiness_even_without_cfg_flag(tmp_path):
    root=job(tmp_path,"cfg['bim']['required']=False\ncfg['bim']['coverage']={}\n")
    stamp=B.read_json(root/"design/consistency_result.json")
    stamp["cfg_sha256"]=B.digest(root/"cfg.py")
    B.atomic_json(root/"design/consistency_result.json",stamp)
    B.atomic_json(root/"design/bim_request.json",{"required":True})
    assert any("coverage" in s for s in B.after_consistency(root,{"bim":{"schema_version":"1.0"}},[]))


def test_nested_configuration_helper_is_fingerprinted(tmp_path):
    root=job(tmp_path)
    helper=root/"helpers"; helper.mkdir()
    (helper/"geometry.py").write_text("LENGTH=120\n")
    assert "helpers/geometry.py" in B.tracked_sources(root)


def test_batch_continues_after_failed_job(tmp_path,monkeypatch):
    root=tmp_path/"jobs"
    for name in ("bad","good"):
        (root/name).mkdir(parents=True)
        (root/name/"cfg.py").write_text("cfg={}")
    calls=[]
    def replay(path,*args):
        calls.append(path.name)
        if path.name=="bad": raise ValueError("fixture replay failure")
        return {"readiness":{"status":"ready","counts":{},"findings":[]}}
    monkeypatch.setattr(B,"replay",replay)
    assert B.main(["batch",str(root),"--output-root",str(tmp_path/"out")])==1
    assert calls==["bad","good"]
    assert [r["status"] for r in B.read_json(tmp_path/"out/replay_summary.json")]==["failed","ready"]


def test_grouped_record_requires_occurrence_or_disposition(exported):
    p=copy.deepcopy(exported[2])
    p["design_records"].append({"id":"connections/c2","table":"connections","source_pointer":"/connections/1","waived":False,"data":{"id":"c2"}})
    assert "connection_mapping" in {f["code"] for f in B.validate(p)}
    p["physical"]["record_dispositions"]=[{"record_id":"connections/c2","status":"not_applicable","reason":"Explicit fixture no-splice note"}]
    assert not B.validate(p)


def test_delegated_connection_requires_defined_interface(exported):
    p=copy.deepcopy(exported[2]); c=p["physical"]["connections"][0]
    c.update(scope="delegated",parts=[])
    assert "delegated_interface" in {f["code"] for f in B.validate(p)}
    c.update(responsible_party="Fixture supplier",detail_ref={"id":"supplier-interface","revision":"A"},representation="Design-envelope reference")
    assert not B.validate(p)


@pytest.mark.skipif(MODULE!="HR",reason="HR saved-model replay")
def test_saved_model_fallback_skips_modal_selfcheck(tmp_path):
    root=job(tmp_path,missing_import=True)
    (root/"model_opensees.py").write_text("""import openseespy.opensees as ops
ops.wipe()
ops.model('basic','-ndm',3,'-ndf',6)
ops.node(1,0,0,0)
ops.node(2,120,0,0)
ops.geomTransf('Linear',1,0,0,1)
ops.element('elasticBeamColumn',1,1,2,10,29000,11000,1,20,100,1)
ops.eigen(3)
raise RuntimeError('Must never execute saved Python file')
""")
    view={"meta":{"levels":[0]},"elements":[{"tag":1,"type":"beam","sec":"TEST","n":[1,2]}]}
    (root/"viewer_3d.html").write_text("const DATA = "+json.dumps(view)+";")
    p=B.replay(root,tmp_path/"out/bim_package.json",20)
    assert p["analysis"]["comparison"]["status"]=="replayed_saved_model"
    assert "legacy_configuration_dependency" in {f["code"] for f in p["readiness"]["findings"]}


@pytest.mark.skipif(MODULE!="CFS",reason="CFS native geometry")
def test_cfs_uncapped_frames_and_wall_ranges():
    sys.path.insert(0,str(ROOT/"steel_engine"))
    spec=importlib.util.spec_from_file_location("test_cfs_bim_adapter",ROOT/"steel_engine/bim_adapter.py")
    adapter=importlib.util.module_from_spec(spec); spec.loader.exec_module(adapter)
    assert len(adapter.frame_positions({"n_frames":40,"spacing_ft":10,"length_ft":390}))==40
    with pytest.raises(ValueError): adapter.frame_positions({"spacing_ft":10,"length_ft":25})
    import wall_line as WL
    line=WL.WallLine("A",10,{1:[(8,10)]},segment_positions_ft={1:[(4,12)]})
    assert line.length(1)==8
    p=adapter.walls({"heights_ft":[10],"lines_x":[line]}, {})
    assert p["physical"]["panels"][0]["vertices"]==[[48,120,0],[144,120,0],[144,120,120],[48,120,120]]
    unknown=adapter.walls({"heights_ft":[10],"lines_x":[WL.WallLine("A",10,{1:[(8,10)]})]}, {})
    assert unknown["physical"]["panels"]==[]
    assert unknown["findings"][0]["code"]=="wall_segment_positions"


@pytest.mark.skipif(MODULE!="CFS",reason="CFS native geometry")
def test_cfs_portal_captures_all_frames_and_knee_profiles_without_solve(monkeypatch):
    sys.path.insert(0,str(ROOT/"steel_engine"))
    import bim_adapter as adapter
    import cfs_frame as CF
    def forbidden(*args,**kwargs): raise AssertionError("must not solve")
    monkeypatch.setattr(CF.Frame2D,"solve",forbidden)
    cfg=CF._demo_cfg()
    cfg.update(n_frames=40,length_ft=39*cfg["spacing_ft"],knee_braces={"section":cfg["col_section"],"col_drop_ft":3,"raf_run_ft":3})
    p=adapter.portal(cfg,{})
    assert len({n["point"][1] for n in p["analysis"]["nodes"]})==40
    knees=[m for m in p["physical"]["members"] if m["kind"]=="knee_brace"]
    assert len(knees)==80
    assert {m["profile_id"] for m in knees}=={cfg["col_section"]}
    assert len(p["analysis"]["elements"])==len({i for m in p["physical"]["members"] for i in m["analysis_ids"]})
    bent=CF.Frame2D()
    for tag,x,y in ((1,0,0),(2,10,0),(3,10,10)): bent.node(tag,x,y)
    bent.elem(1,2,1,1,"bend"); bent.elem(2,3,1,1,"bend")
    assert len(list(adapter.components(bent,[0,1])))==2


def test_source_schedule_cannot_silently_disappear(exported):
    p=copy.deepcopy(exported[2])
    p["design_records"].append({"id":"headers/h1","table":"headers","source_pointer":"/headers/0","waived":False,"data":{"id":"h1"}})
    assert "assembly_mapping" in {f["code"] for f in B.validate(p)}
    p["physical"]["members"][0]["design_record_ids"]=["headers/h1"]
    assert not B.validate(p)


@pytest.mark.skipif(MODULE!="HR",reason="HR saved-model comparison")
def test_replay_detects_orientation_difference_from_gold(tmp_path):
    root=job(tmp_path)
    script=(root/"cfg.py").read_text()
    body=script.split("def build(cfg, transf):\n",1)[1].split("    return dict",1)[0]
    body="\n".join(line[4:] for line in body.splitlines())
    (root/"model_opensees.py").write_text(body.replace("'Linear',1,0,0,1","'Linear',1,0,1,0"))
    p=B.replay(root,tmp_path/"out/bim_package.json",20)
    assert p["readiness"]["status"]=="invalid"
    assert p["analysis"]["comparison"]["status"]=="mismatch"
    assert any("orientation/offsets 1" in f["entity_ids"] for f in p["readiness"]["findings"])

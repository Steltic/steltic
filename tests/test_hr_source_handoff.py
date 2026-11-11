import copy
import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from steel_engine.bim_contract import validate
from steel_engine.bim_json import file_digest
from steel_engine.bim_materials import material_schedule
from steel_engine.bim_source_checks import rolled_catalog
from steel_engine.bim_source_fields import applications, digest_value, selection_record
from steel_engine.bim_source_status import consistency_errors
from steel_engine.saved_handoff import _finish_handoff, attach_current_handoff, bind_rendered_report, build_selections, export_saved


FIXTURE = Path(__file__).parent / "fixtures/synthetic_bim_package.json"


def source(text, *, label="selected connection", identity="c1"):
    return {"id": "connections/" + identity, "table": "connections", "source_pointer": "/connections/0",
            "data": {"id": identity, "type": label, "components": text}, "waived": False}


def actual_fields(record, **parameters):
    selected = selection_record(record)
    connection = {"id": "joint-1", "design_record_ids": [record["id"]], "parameters": parameters}
    link = applications(connection, {record["id"]: selected})[0]
    return {(field["role"], field["field"]): field["value"] for i in link["selected"]
            for field in [selected["fields"][i]]}, selected, link


def test_pin_family_and_outer_weld_are_read_from_the_selected_clause():
    r = source("pin-hinge release beyond the corner gusset: the beam joins the stub only through a "
               "5 in pin (ASTM A193 Gr B7 bar, holes d + 1/32 in); knife plate PL 2-1/2 in, "
               "3/4 in fillets to both flanges on both faces; two clevis plates PL 1-1/2 in, "
               "with 3/4 in fillets to both flanges on their outer faces")
    f, selected, _ = actual_fields(r)
    assert f["connection", "variant"] == "beam_pin_hinge"
    assert f["pin", "diameter_in"] == 5
    assert f["pin", "material"] == "ASTM A193 Gr B7"
    assert f["pin_hole", "clearance_in"] == 1/32
    assert f["clevis_outer_fillet", "size_in"] == .75
    assert f["knife_plate", "thickness_in"] == 2.5
    for field in selected["fields"]:
        evidence = field["source"]
        if "span" in evidence:
            a, b = evidence["span"]
            assert r["data"][evidence["pointer"].rsplit("/", 1)[1]][a:b] == evidence["literal"]
    r["data"]["components"] = "single plate 0.75 x 17.5 in A572 Gr 50; 6 x 1 in A490-X bolts at 3 in. A nearby pin is excluded."
    f, _, _ = actual_fields(r)
    assert f["connection", "variant"] == "single_plate_shear"
    assert (f["bolt", "count"], f["bolt", "diameter_in"], f["bolt", "material"]) == (6, 1, "A490-X")
    assert ("pin", "diameter_in") not in f


def test_column_base_and_apex_retained_separately():
    r = source("corner gusset at the column base: 5 in dia. pin (AISI 4140 Q&T, Fy 105 ksi) in double shear; "
               "pin hole 5-1/16 = d + 1/16 in | apex gusset: 4 in dia. pin (AISI 4140 Q&T, Fy 105 ksi) in double shear")
    assert actual_fields(r, joint="base")[0]["pin", "diameter_in"] == 5
    assert actual_fields(r, joint="base")[0]["pin_hole", "diameter_in"] == 5.0625
    assert actual_fields(r, joint="apex")[0]["pin", "diameter_in"] == 4


def test_reinforcement_and_main_slot_welds_have_separate_end_scopes():
    r = source("upper end: HSS5.563X0.375 slotted for a 0.75 in A572 Gr 50 gusset, four 3/8 in E70 welds x 12 in; "
               "slot reinforced: 2 PL 1/4 x 2 x 9 in A572 Gr 50 slot reinforcement, centred on the slot end, "
               "each welded with 3/16 in E70 longitudinal fillets (two lines per plate), 3.5 in long on each side "
               "of the reduced section; lower end: apex gusset 0.75 in A572 Gr 50")
    f, selected, _ = actual_fields(r, joint="corner", upper_end=True)
    assert f["reinforcement_fillet", "size_in"] == .1875
    assert f["brace_slot_weld", "size_in"] == .375
    assert f["slot_reinforcement", "length_in"] == 9
    f, _, link = actual_fields(r, joint="apex", upper_end=False)
    assert ("reinforcement_fillet", "size_in") not in f
    assert any(selected["fields"][i]["role"] == "reinforcement_fillet" for i in link["conditional"])


def test_selected_anchor_setout_and_grout():
    r = source("base plate PL 0.75 x 16 x 32 in ASTM A572 Gr 50 on 2 in non-shrink grout; "
               "4 x 1 in ASTM F1554-36 anchor rods, 2 per flange side in one row outside the flanges at 12.5 in, "
               "1-3/4 in from the plate edges")
    f, _, _ = actual_fields(r)
    assert (f["anchor_pattern", "spacing_in"], f["anchor_pattern", "edge_in"]) == (12.5, 1.75)
    assert (f["base_plate", "width_in"], f["base_plate", "length_in"]) == (16, 32)
    assert (f["grout", "material"], f["grout", "thickness_in"]) == ("non-shrink grout", 2)


@pytest.mark.parametrize("columns", [1, 2])
def test_double_plate_bolt_grid_is_one_group_and_has_a_distinct_collector_interface(columns):
    local = source("two plates PL 0.375 x 20.5 in A572 Gr 50 (one each face of the beam web); "
                   f"7 x {columns} x 1 in A490-X bolts in double shear, STD holes, 3 in pitch; "
                   "beam end set back 1 in from the column face")
    local["data"]["section"] = "W24X94"
    collector = source("single plate 0.75 x 14.5 in A572 Gr 50; 5 x 0.875 in A490-X bolts at 3 in",
                       label="collector-to-braced-frame connection (single plate, shear + axial), all braced lines",
                       identity="collector")
    collector["data"]["demand"] = {"P_basis_note": "governing line Y5, level 5, Y direction: ..."}
    records = {r["id"]: selection_record(r) for r in (local, collector)}
    members = [{"id": "beam", "kind": "beam", "profile_id": "W24X94", "axis": [[0, 300, 168], [0, 600, 168]]}]
    c = {"id": "end", "point": [0, 300, 168], "member_ids": ["beam"], "design_record_ids": list(records),
         "parameters": {"collector": True}}
    physical = {"connections": [c], "members": members}
    grids = [{"label": "1", "points_in": [[0, 0, 0], [0, 900, 0]]},
             {"label": "B", "points_in": [[0, 300, 0], [600, 300, 0]]}]
    ref = {"grids": grids, "levels": [{"level": 1, "elevation_in": 168}]}
    active, related = build_selections(records, physical, ref)["end"]["applications"]
    fields = records[local["id"]]["fields"]
    actual = {(fields[i]["role"], fields[i]["field"]): fields[i]["value"] for i in active["selected"]}
    assert actual["connection", "variant"] == "double_plate_shear"
    assert actual["shear_plate", "count"] == 2
    assert actual["bolt", "count"] == 7 * columns
    assert (actual["bolt", "rows"], actual["bolt", "columns"], actual["bolt", "diameter_in"]) == (7, columns, 1)
    assert (actual["bolt", "material"], actual["bolt", "pitch_in"], actual["beam_end", "cutback_in"]) == ("A490-X", 3, 1)
    assert len([field for field in fields if (field["role"], field["field"]) == ("bolt", "count")]) == 1
    assert related["selected"] == [] and related["conditional"]
    assert related["interface_application"] == {"role": "collector_to_frame", "status": "related_interface",
        "local_beam_end_records": {local["id"]: ["beam"]}, "point_in": [0, 300, 168], "grids": ["1", "B"],
        "levels": [1], "at_governing_grid_and_level": False}
    c["design_record_ids"] = [collector["id"]]
    located = build_selections(records, physical, ref)["end"]["applications"][0]
    assert located["selected"] and located["interface_application"]["status"] == "located_interface"


def test_stmf_terminal_and_crossing_welds_are_bound_to_their_own_members_and_exact_source():
    r = source("flat bars welded to 3/4-in gussets; end diagonals HSS8X8X5/8 slotted on 3/4-in gussets; "
               "HSS8X8X5/8 slotted for a 0.75 in A572 Gr 50 gusset; four 5/16 in E70 fillet welds x 16.0 in; "
               "slot width tg + 1/8 in", label="STMF Y trusses: special-segment flat-bar ends, X crossing, end-diagonal gussets")
    r["data"]["checks"] = [{"limit_state": "flat bar 1.5 x 0.625 end connection, two 0.5-in fillets x 3 in "
                                          "to the gusset (welded, no bolts), required RyFyAg"},
        {"limit_state": "flat bars interconnected at the crossing for 0.25 x the nominal tensile strength (1/4-in fillets, 4 x 2 in)"},
        {"limit_state": "Hypothetical flat bar 99 x 99 end connection, two 1-in fillets x 100 in to the gusset"}]
    records = {r["id"]: selection_record(r)}
    profiles = [{"id": "FLAT", "family": "flat", "dimensions": {"width": 1.5, "thickness": .625}},
                {"id": "SMALL", "family": "flat", "dimensions": {"width": 1.25, "thickness": .625}},
                {"id": "HSS8X8X5/8", "family": "rhs", "dimensions": {}}]
    members = [{"id": "flat", "kind": "truss_web", "profile_id": "FLAT", "axis": [[0, 0, 0], [10, 0, 10]]},
               {"id": "small", "kind": "truss_web", "profile_id": "SMALL", "axis": [[0, 0, 10], [10, 0, 0]]},
               {"id": "diag", "kind": "truss_web", "profile_id": "HSS8X8X5/8", "axis": [[0, 0, 0], [10, 0, 20]]}]
    locations = [("terminal", [0, 0, 0], ["flat", "diag"]), ("cross", [5, 0, 5], ["small", "flat"]),
                 ("upper", [10, 0, 0], ["small"])]
    conns = [{"id": cid, "point": point, "member_ids": mids, "design_record_ids": [r["id"]]} for cid, point, mids in locations]
    apps = build_selections(records, {"members": members, "profiles": profiles, "connections": conns})
    fields = records[r["id"]]["fields"]
    def values(cid, status="selected"):
        return {(fields[i]["role"], fields[i]["field"]): fields[i]["value"]
                for i in apps[cid]["applications"][0][status]}
    assert values("terminal")["flat_bar_end_fillet", "length_in"] == 3
    assert values("terminal")["flat_bar_end_fillet", "size_in"] == .5
    assert values("terminal")["brace_slot_weld", "length_in"] == 16
    assert ("flat_bar_crossing_fillet", "size_in") not in values("terminal")
    assert values("cross")["flat_bar_crossing_fillet", "size_in"] == .25
    assert values("cross")["flat_bar_crossing_fillet", "count"] == 4
    assert values("cross")["flat_bar_crossing_fillet", "length_in"] == 2
    assert ("flat_bar_end_fillet", "size_in") not in values("cross")
    assert ("flat_bar_end", "width_in") not in values("upper")
    assert values("upper", "conditional")["flat_bar_end", "width_in"] == 1.5
    for field in fields:
        evidence = field["source"]
        if "/checks/" in evidence["pointer"]:
            n = int(evidence["pointer"].split("/")[-2])
            lo, hi = evidence["span"]
            assert n in (0, 1) and r["data"]["checks"][n]["limit_state"][lo:hi] == evidence["literal"]
    from steel_engine.bim_source_crossings import crossing_interfaces
    data = {"members": members, "profiles": profiles, "connections": [c for c in conns if c["id"] != "cross"]}
    required, missing = crossing_interfaces(records, data)
    assert not missing and len(required) == 1
    assert required[0]["point_in"] == [5, 0, 5]
    assert required[0]["member_stations"] == [.5, .5]
    assert required[0]["member_ids"] == ["flat", "small"]
    assert required[0]["native_connection_ids"] == []
    assert required[0]["status"] == "requires_openbim_geometry"
    data["connections"] = conns
    assert crossing_interfaces(records, data)[0][0]["native_connection_ids"] == ["cross"]


def test_source_named_opposite_end_is_distinct_from_an_adjacent_beam_at_the_same_node():
    corner = source("two plates PL 0.375 x 14.5 in A572 Gr 50 (one each face of the beam web); 5 x 2 x 7/8 in A490-X bolts",
                    identity="braced-bay")
    corner["data"]["design_basis"] = "The other ends of these beams have no gusset (record ordinary)."
    ordinary = source("single plate 0.5 x 17.5 in A572 Gr 50; 6 x 0.875 in A325-N bolts", identity="ordinary")
    ordinary["data"]["section"] = "W21X44"
    records = {r["id"]: selection_record(r) for r in (corner, ordinary)}
    members = [{"id": mid, "kind": "beam", "profile_id": "W21X44", "axis": [[0, 0, 0], end]}
               for mid, end in (("brace_receiver", [10, 0, 0]), ("adjacent", [0, 10, 0]))]
    connections = [{"id": m["id"] + "-end", "point": [0, 0, 0], "member_ids": [m["id"]], "design_record_ids": list(records)}
                   for m in members]
    connections.append({"id": "gusset", "parameters": {"joint": "corner"}, "point": [0, 0, 0],
                        "member_ids": ["brace", "column", "brace_receiver"], "design_record_ids": []})
    found = build_selections(records, {"members": members, "connections": connections})
    a, b = found["brace_receiver-end"]["applications"]
    assert a["selected"] and b["selected"] == []
    assert a["alternate_end"]["corner_connection_ids"] == ["gusset"]
    a, b = found["adjacent-end"]["applications"]
    assert a["selected"] == [] and b["selected"]


def test_collector_and_conventional_detail_in_same_source_row_follow_the_physical_grid_level_and_direction():
    r = source("collector / strut detail at every W18X35 end that carries a collector force or an analysis axial force "
               "-- 72 beams (Grid 2 (Y line) levels 1-3, 12-13; Grid 4 (Y line) levels 1-2, 6-8, 13-14): "
               "single plate 0.75 x 13 in A572 Gr 50; 2 x 3 = 6 bolts 1-1/4 in ASTM F3125 A490-X, pitch and gauge 4 in. "
               "The other 95 W18X35 beams (not on a collector line): conventional gravity single plate, "
               "single plate 0.375 in x 12 in A572 Gr 50; 4 x 0.75 in A325-N bolts")
    r["data"]["section"] = "W18X35"
    r["data"]["checks"] = [{"limit_state": "bolt shear, critical bolt: 2 lines x 3 rows of 1-1/4 in A490-X in standard holes"}]
    target = source("W24X94 collector on Grid C (X frame), level 7: end connection = the collector / strut detail of "
                    "conn-floor-W24X94: single plate 0.75 x 17 in A572 Gr 50", identity="collector-line")
    records = {t["id"]: selection_record(t) for t in (r, target)}
    members, connections = [], []
    for cid, origin, dx, dy in [("on_line", [0, 0, 70], 0, 10), ("other_level", [0, 0, 50], 0, 10),
                                ("other_direction", [0, 0, 70], 10, 0)]:
        members.append({"id": cid, "kind": "beam", "axis": [origin, [origin[0]+dx, origin[1]+dy, origin[2]]],
                        "profile_id": "W18X35"})
        connections.append({"id": cid, "point": origin, "member_ids": [cid], "design_record_ids": list(records),
                            "parameters": {"collector": True}})
    ref = {"grids": [{"label": "4", "points_in": [[0, 0, 0], [0, 10, 0]]},
                     {"label": "C", "points_in": [[0, 0, 0], [10, 0, 0]]}],
           "levels": [{"level": 5, "elevation_in": 50}, {"level": 7, "elevation_in": 70}]}
    occurrences = build_selections(records, {"members": members, "connections": connections}, ref)
    fields = records[r["id"]]["fields"]
    for cid, group, count, thickness, diameter in [("on_line", "collector", 6, .75, 1.25),
                                                  ("other_level", "gravity", 4, .375, .75),
                                                  ("other_direction", "gravity", 4, .375, .75)]:
        local, collector = occurrences[cid]["applications"]
        assert local["beam_end_group"]["selected"] == group
        values = {(fields[i]["role"], fields[i]["field"]): fields[i]["value"] for i in local["selected"]}
        assert (values["bolt", "count"], values["shear_plate", "thickness_in"], values["bolt", "diameter_in"]) == (count, thickness, diameter)
        assert collector["interface_application"]["status"] == "different_location" and collector["selected"] == []
    r["data"]["components"] = r["data"]["components"].replace("2 x 3 = 6", "2 x 3 = 8")
    with pytest.raises(ValueError, match="bolt-grid"):
        selection_record(r)


def test_source_conflict_requires_a_located_field_specific_decision():
    r = source("")
    r["data"]["bim_selection"] = [
        {"role": "pin", "field": "diameter_in", "value": d, "unit": "in"} for d in (3, 4)]
    records = {r["id"]: selection_record(r)}
    physical = {"connections": [{"id": "c", "design_record_ids": [r["id"]]}]}
    files = {"design/calc_package.json": "a"*64}
    generic = [{"decision_id": "fit", "action": "confirm", "author": "engineer", "note": "Choose missing local geometry"}]
    def run(decisions):
        return _finish_handoff("job", files, {}, {}, records, physical, decisions)
    assert len(run(generic)["source_conflicts"]) == 1
    change = {"connection_id": "c", "record_id": r["id"], "field_index": 0,
              "role": "pin", "field": "diameter_in", "source_value": 3, "value": 4, "unit": "in",
              "calc_sha256": "a"*64, "record_sha256": digest_value(r["data"]),
              "source_pointer": "/connections/0/bim_selection/0/value"}
    explicit = [dict(generic[0], selection_overrides=[change])]
    resolved = run(explicit)
    assert resolved["source_conflicts"] == []
    assert resolved["records"][r["id"]]["fields"][0]["value"] == 3
    assert resolved["occurrences"]["c"]["overrides"][0]["value"] == 4
    change["calc_sha256"] = "b"*64
    with pytest.raises(ValueError, match="different source revision"):
        run(explicit)


def _bytes(obj):
    return json.dumps(obj, sort_keys=True).encode()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def saved_job(tmp_path):
    root = tmp_path / "original" / "saved-job"
    design = root / "design"
    design.mkdir(parents=True)
    (root/"cfg.py").write_text("raise AssertionError('saved design must never execute configuration')", encoding="utf-8")
    (design/"member_schedule.csv").write_text("ele_tag,section\n1,W8X10\n", encoding="utf-8")
    r = source("single plate 0.75 x 17.5 in A572 Gr 50; 6 x 1 in A490-X bolts at 3 in")
    (root/"report.html").write_text("<html><h3>Materials</h3><table><tr><td>Plates</td><td>ASTM A572 Grade 50</td></tr></table>"
                                    "<h4 id='c-c1'>c1 &mdash; selected connection</h4><table class='calc kv'><tr><td>Components</td>"
                                    f"<td>{r['data']['components']}</td></tr></table></html>", encoding="utf-8")
    calc = {"connections": [r["data"]]}
    (design/"calc_package.json").write_bytes(_bytes(calc))
    stamp = {"result": "PASS", "n_issues": 0, "issues": [], "cfg_sha256": file_digest(root/"cfg.py"),
             "calc_package_sha256": file_digest(design/"calc_package.json")}
    (design/"consistency_result.json").write_bytes(_bytes(stamp))
    source_files = {p.relative_to(root).as_posix(): file_digest(p) for p in (root/"cfg.py", *design.iterdir())}
    physical = {"module": "HR", "revision": 2, "design_sources": source_files,
                "gold_material_schedule": {"source": {"path": "report.html", "sha256": file_digest(root/"report.html")},
                                           "materials": [{"item": "Plates", "spec": "ASTM A572 Grade 50"}]},
                "decisions": []}
    pbytes = _bytes(physical)
    package = json.loads(FIXTURE.read_text(encoding="utf-8"))
    package["building_id"] = root.name
    package["design_records"] = [r]
    package["provenance"].update(source_files=source_files, physical_inputs={"path": "physical_inputs.json", "sha256": _sha(pbytes), "revision": 2})
    native = tmp_path/"native.json"
    native.write_bytes(_bytes(package))
    names = {"physical_inputs.json": pbytes, "example_decisions.json": b"{}",
             "adopted_answers.json": b"{}", "accepted_decisions.txt": b"Accepted geometry"}
    manifest = {"schema": "steltic.example_decision_pack", "module": "HR", "building_id": root.name,
                "native_physical_revision": 2, "native_physical_sha256": _sha(pbytes),
                "native_bim_file_sha256": file_digest(native), "files_sha256": {k: _sha(v) for k, v in names.items()}}
    archive = tmp_path/"decisions.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for k, v in names.items():
            z.writestr(k, v)
        z.writestr("manifest.json", _bytes(manifest))
    return root, native, archive


def test_saved_entry_point_binds_original_report_and_omission_is_detected(tmp_path):
    root, native, archive = saved_job(tmp_path)
    before = {p: file_digest(p) for p in root.rglob("*") if p.is_file()}
    output = tmp_path/"new/bim_package.json"
    summary = export_saved(root, native, archive, output)
    package = json.loads(output.read_text(encoding="utf-8"))
    handoff = package["source_handoff"]
    assert summary["analysis_executed"] is False
    assert summary["report_sha256"] == before[root/"report.html"]
    assert handoff["source_bindings"]["native_content_sha256"] == digest_value(json.loads(native.read_text()))
    assert handoff["source_bindings"]["report_selection_readback"]["connection_component_rows_checked"] == 1
    assert handoff["records"]["connections/c1"]["legacy_components"]["clauses"]
    assert len(handoff["occurrences"]) == len(package["physical"]["connections"])
    assert {p: file_digest(p) for p in root.rglob("*") if p.is_file()} == before
    assert not validate(package)
    edited = copy.deepcopy(package)
    edited["physical"]["members"][0]["mark"] = "changed-after-handoff"
    assert "hr_handoff_native_content" in {f["code"] for f in validate(edited)}
    del handoff["occurrences"][next(iter(handoff["occurrences"]))]
    assert {"hr_handoff_revision", "hr_handoff_occurrences"} <= {f["code"] for f in validate(package)}
    output_before = file_digest(output)
    (root/"report.html").write_bytes(b"changed")
    with pytest.raises(ValueError, match="report.html"):
        export_saved(root, native, archive, output)
    assert file_digest(output) == output_before
    with pytest.raises(ValueError, match="outside the original"):
        export_saved(root, native, archive, root/"report.html")


@pytest.mark.parametrize("change", [{"cfg_sha256": None}, {"n_issues": 1}, {"n_issues": False}, {"issues": ["pending"]}])
def test_pass_stamp_needs_current_hashes_and_empty_issue_evidence(tmp_path, change):
    root, _, _ = saved_job(tmp_path)
    stamp = json.loads((root/"design/consistency_result.json").read_text())
    assert not consistency_errors(root, stamp)
    stamp.update(change)
    assert consistency_errors(root, stamp)


def test_contract_detects_the_three_independent_mutations():
    base = json.loads(FIXTURE.read_text())
    p = copy.deepcopy(base)
    p["require_connection_parts"] = True
    p["design_records"][0]["data"] = {"components": {"n_bolts": 4, "diameter_in": .75, "specification": "ASTM F3125 A325"}}
    p["physical"]["connections"][0]["parts"].append({"id": "one", "kind": "bolt", "material_id": "steel",
        "geometry": {"type": "cylinder", "origin": [0, 1, 1], "axis": [1, 0, 0], "radius": .25, "length": 2}})
    assert {"hr_selected_fastener_count", "hr_selected_fastener_diameter"} <= {f["code"] for f in validate(p)}
    p = copy.deepcopy(base)
    p["physical"]["profiles"][0].update(id="W8X10", dimensions=dict(rolled_catalog()["W8X10"]))
    p["physical"]["members"][0].update(profile_id="W8X10", design_record_ids=["members/m1"])
    p["design_records"].append({"id": "members/m1", "table": "members", "source_pointer": "/members/0",
                                "waived": False, "data": {"inputs": {"section": "W8X10", "Fy": 50}}})
    p["physical"]["materials"][0].update(specification="ASTM A36", Fy=36)
    p["physical"]["profiles"][0]["dimensions"]["d"] *= 10
    assert {"hr_catalog_dimension", "hr_selected_material_strength"} <= {f["code"] for f in validate(p)}
    p["physical"]["profiles"][0]["dimensions"].update(rolled_catalog()["W8X10"])
    p["physical"]["members"][0]["analysis_ids"] = ["hr:element:1"]
    p["analysis"] = {"nodes": [{"id": "a", "point": [0, 0, 0]}, {"id": "b", "point": [120, 0, 0]}],
                     "elements": [{"id": "hr:element:1", "node_ids": ["a", "b"], "profile_id": "W12X26",
                                   "source_schedule": {"section": "W12X26"}}]}
    assert "hr_analysis_profile_mapping" in {f["code"] for f in validate(p)}


def test_report_materials_preserve_explicit_grade_conflicts():
    assert material_schedule({}, {})["status"] == "unresolved"
    schedule = material_schedule({"materials": {"Pins": "AISI 4140"}}, {"materials": [{"item": "Pins", "spec": "ASTM A193 B7"}]})
    assert schedule["status"] == "source_conflict"
    assert {r["spec"] for r in schedule["materials"]} == {"AISI 4140", "ASTM A193 B7"}


def test_new_report_and_handoff_share_occurrence_materials_and_detached_status(tmp_path, monkeypatch):
    import importlib
    monkeypatch.syspath_prepend(str(FIXTURE.parents[2] / "steel_engine"))
    report = importlib.import_module("steel_engine.report")
    row = source("pin-hinge release beyond the corner gusset: the beam joins the stub only through a "
                 "4 in pin (ASTM A572 Gr 50 round bar)")
    html = report._materials_table({}, {"connections": [row["data"]]})
    assert "ASTM A572 Gr 50" in html and "connections/c1" in html
    assert "unresolved" in report._materials_table({}, {})
    assert "delivery_certificate.json" in report._delivery_section()
    assert "verified on the contract documents" not in report._delivery_section()
    root, native, _ = saved_job(tmp_path)
    original = json.loads(native.read_text())
    calc = json.loads((root/"design/calc_package.json").read_text())
    attach_current_handoff(original, {}, calc)
    package_file = root/"design/bim_package.json"
    package_file.write_text(json.dumps(original), encoding="utf-8")
    previous = (root/"report.html").read_text(encoding="utf-8")
    (root/"report.html").write_text(previous.replace("A490-X", "A325-N"), encoding="utf-8")
    with pytest.raises(ValueError, match="at c1"):
        bind_rendered_report(root)
    (root/"report.html").write_text(previous.replace("</html>", "<footer>new report revision</footer></html>"), encoding="utf-8")
    bind_rendered_report(root)
    after = json.loads(package_file.read_text(encoding="utf-8"))
    assert after["source_handoff"]["source_files"]["report.html"] == file_digest(root/"report.html")
    assert after["provenance"]["source_files"]["report.html"] == file_digest(root/"report.html")
    assert after["source_handoff"]["contract_sha256"] != original["source_handoff"]["contract_sha256"]
    assert not validate(after)


def test_explicit_terminal_crop_detects_global_member_continuing_past_joint():
    from steel_engine.bim_assemblies import fingerprint, findings
    p = json.loads(FIXTURE.read_text())
    physical = p["physical"]
    receiver = {"id": "BRACE", "kind": "member", "profile_id": "TEST", "axis": [[6, 0, 0], [30, 0, 0]],
                "local_y": [0, 1, 0], "application": {"role": "terminal", "member_end": 0}}
    c = physical["connections"][0]
    template = {"id": "t", "basis": "fixture", "receivers": [receiver], "parts": c.pop("parts"),
                "source_record_ids": c["design_record_ids"]}
    template["revision"] = fingerprint(template)
    physical["assemblies"] = [template]
    c["parts"] = []
    c["assembly_instance"] = {"template_id": "t", "revision": template["revision"], "origin": [0, 0, 0],
                              "x_axis": [1, 0, 0], "y_axis": [0, 1, 0], "bindings": {"BRACE": physical["members"][0]["id"]}}
    assert "assembly_terminal_receiver" in {f["code"] for f in findings(p)}
    physical["members"][0]["end_offsets"][0] = [6, 0, 0]
    assert "assembly_terminal_receiver" not in {f["code"] for f in findings(p)}

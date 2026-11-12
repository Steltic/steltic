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


def test_located_web_override_preserves_adjacent_same_section_and_requires_both_ends():
    base = source("end diagonals HSS8X8X5/8 slotted for a 0.75 in A572 Gr 50 gusset; "
                  "four 5/16 in E70 fillet welds x 16.0 in",
                  label="STMF Y trusses: special-segment flat-bar ends")
    short = source("", identity="short")
    short["data"].update(
        slotted_web_replacement=dict(member_ends={"a": ["corner", "joint"]}, section="HSS8X8X5/8",
                                     axis="diagonal", supersedes=base["id"]),
        bim_selection=[dict(role="brace_slot_weld", field="length_in", unit="in", value=9.5)])
    records = {r["id"]: selection_record(r) for r in (base, short)}
    joint = dict(id="joint", point=[1, 0, 1], member_ids=["a", "b"], design_record_ids=list(records))
    corner = dict(id="corner", point=[0, 0, 0], member_ids=["a"], design_record_ids=[short["id"]])
    physical = dict(members=[dict(id=mid, kind="truss_web", profile_id="HSS8X8X5/8", axis=axis) for mid,axis in
                             (("a", [[0,0,0],[1,0,1]]), ("b", [[1,0,1],[2,0,0]]))],
                    profiles=[dict(id="HSS8X8X5/8",family="rhs")], connections=[joint,corner])
    links = build_selections(records,physical)["joint"]["applications"]
    assert links[0]["hss_members"] == {"a": "superseded", "b": "selected"}
    assert links[1]["hss_members"] == {"a": "selected"}
    corner["design_record_ids"] = []
    with pytest.raises(ValueError, match="Missing located slotted-web end"):
        build_selections(records,physical)


def actual_fields(record, **parameters):
    selected = selection_record(record)
    connection = {"id": "joint-1", "design_record_ids": [record["id"]], "parameters": parameters}
    link = applications(connection, {record["id"]: selected})[0]
    return {(field["role"], field["field"]): field["value"] for i in link["selected"]
            for field in [selected["fields"][i]]}, selected, link


def test_ordinary_brb_corner_and_apex_pin_dimensions_replay_the_original_group_e_moment():
    r = source('corner gusset at the column base: 4 in dia. pin in double shear; free-edge stiffeners PL 3/4 x 6 both sides of each free edge; apex gusset: 4 in dia. pin in double shear')
    r['data']['checks'] = [
        {'limit_state':'corner gusset: pin flexure, M = P/2 (t/4 + gap + tclevis/2) = 565 kip-in', 'Mu_kipin':564.51},
        {'limit_state':'corner gusset: gusset at the pin: tensile rupture, be = 6.63 in, t = 3 in', 'Tu_kip':645.15},
        {'limit_state':'corner gusset: cheek plate to gusset fillet welds (9/16 in, 40 in around each plate), share of bearing'},
        {'limit_state':'corner gusset: gusset Whitmore yielding (Whitmore section 28.2 in long, 8 in beyond the cheek plates, t = 1.25 in)'},
        {'limit_state':'apex gusset: pin flexure, M = P/2 (t/4 + gap + tclevis/2) = 600 kip-in', 'Mu_kipin':600.},
        {'limit_state':'apex gusset: apex gusset at the pin: tensile rupture, be = 6.63 in, t = 3 in', 'Tu_kip':645.15}]
    indexed = selection_record(r)['fields']
    joint = lambda name: {(f['role'],f['field']):f for f in indexed if f['applicability'].get('joint')==name}
    base = joint('base')
    assert base['clevis_plate','side_gap_in']['value'] == .25
    assert base['clevis_plate','thickness_in']['value'] == 1.5 and base['pin','grip_in']['value'] == 6.5
    assert base['cheek_fillet','perimeter_each_in']['value'] == 40
    assert base['gusset','whitmore_distance_beyond_cheek_in']['value'] == 8
    assert ('clevis_plate','side_gap_in') not in joint('apex')


def test_basement_girder_detail_requires_its_named_physical_member_and_column():
    from steel_engine.bim_collector_applications import receiver_application
    target={'role':'basement_girder_end','member_record':'members/basement-W30X99'}
    site={'point_in':[0,0,0],'columns':{'column':{'source_ids':[]}},
          'end_members':{'girder':{'kind':'girder','section':'W30X99','source_ids':['members/basement-W30X99']}}}
    assert receiver_application(target,site,{})['status']=='located_interface'
    site['end_members']['girder']['source_ids']=['members/floor-W30X99']
    assert receiver_application(target,site,{})['status']=='different_receiver'


def test_located_transfer_support_is_stable_after_native_json_reopen():
    row=source('double gusset plates 2 PL 1 1/2 in of the support node welded to the W14X455 flange tips (4 lines x 42 in, 1 in E70)',
               identity='conn-truss-support-C2-C5',label='transfer truss support on the Grid C2 / C5 columns at Level 3 (welded gussets)')
    selected=selection_record(row)
    assert selected['transfer_target']['grid_intersections']==[['C','2'],['C','5']]
    assert json.loads(json.dumps(selected))==selected


def test_selected_pin_stack_retains_its_order_grip_material_and_attachment_evidence():
    record = source("Three moving plates on one pin.")
    plies = [{"role": role, "connected_to": target, "material": "ASTM A572 Grade 50", "thickness_in": thickness}
             for role, target, thickness in (("left", "brace_end", 1), ("left_cheek", "primary", .5),
                 ("primary", "host", 1.25), ("right_cheek", "primary", .5), ("right", "brace_end", 1))]
    stack = {"selected_stack": plies, "pin_diameter_in": 4, "pin_hole_diameter_in": 4.0625,
             "pin_material": "ASTM A434 Grade 4140 Class BD", "clearance_between_moving_groups_in": .0625,
             "grip_in": 4.375}
    record["data"]["pin_stack"] = stack
    selected = selection_record(record)
    assert selected["data"]["pin_stack"] == stack
    assert selected["pin_stack_source_pointer"] == "/connections/0/pin_stack"
    for changes in ({"pin_hole_diameter_in": 3.9375}, {"grip_in": 4.25}, {"pin_material": " "},
                    {"selected_stack": [plies[0], plies[1], plies[1], plies[3], plies[4]]},
                    {"selected_stack": [plies[1], plies[0], *plies[2:]]},
                    {"selected_stack": [*plies[:1], dict(plies[1], connected_to="unidentified"), *plies[2:]]}):
        record["data"]["pin_stack"] = dict(stack, **changes)
        with pytest.raises(ValueError, match="pin"):
            selection_record(record)


def test_crossing_plate_binds_opposite_bar_faces_and_selected_welds_at_the_actual_intersection():
    from steel_engine.bim_source_crossings import crossing_interfaces

    record = source("Two flat bars connected to a centered crossing plate.")
    plate = {"material": "ASTM A572 Grade 50", "thickness_in": .75, "length_in": 8, "width_in": 8,
             "welds": {"count_per_bar": 2, "length_in": 5, "size_in": .375, "material": "E70XX", "run": "longitudinal"}}
    record["data"]["crossing_plate"] = plate
    members = [{"id": name, "kind": "truss_web", "profile_id": "BAR", "axis": axis}
               for name, axis in (("A", [[0, 0, 0], [10, 0, 20]]), ("B", [[10, 0, 0], [0, 0, 20]]))]
    physical = {"members": members, "profiles": [{"id": "BAR", "family": "flat"}],
                "connections": [{"id": m["id"] + "-end", "member_ids": [m["id"]], "point": m["axis"][0],
                                 "design_record_ids": [record["id"]]} for m in members]}
    selected = selection_record(record)
    interfaces, errors = crossing_interfaces({record["id"]: selected}, physical)
    assert not errors and len(interfaces) == 1
    interface = interfaces[0]
    assert interface["point_in"] == [5, 0, 10] and interface["plate"]["normal"] == [0, 1, 0]
    assert interface["kind"] == "flat_bar_crossing_plate"
    assert interface["member_faces"] == [{"member_id": "A", "face_sign": -1}, {"member_id": "B", "face_sign": 1}]
    assert interface["welds"] == plate["welds"]
    fields = [selected["fields"][i] for i in interface["selected"]]
    assert len(fields) == 9
    for field in fields:
        target = record["data"]
        for name in field["source"]["pointer"].removeprefix("/connections/0/").split("/"):
            target = target[name]
        assert field["value"] == target
        if field["role"] == "flat_bar_crossing_fillet":
            assert field["attachment_roles"] == ["flat_bar", "crossing_plate"]
    for changes in ({"width_in": 0}, {"welds": dict(plate["welds"], count_per_bar=True)},
                    {"welds": dict(plate["welds"], material="")}):
        record["data"]["crossing_plate"] = dict(plate, **changes)
        with pytest.raises(ValueError, match="flat-bar crossing"):
            selection_record(record)


def test_anchor_requirements_and_checked_whitmore_width_keep_numbers_and_end_scope():
    base = source("base plate PL 1.25 x 19 x 24 in ASTM A572 Gr 50; a1 = 2.00 in from the flange face, "
                  "rods 13.2 in apart, 2.41 in or more from the plate edges, 1.8125 in holes (AISC Manual Table 14-2) "
                  "with plate washers; shear lug ASTM A572 Gr 50 PL 2 x 16 in, 6 in embedded below the grout. "
                  "The single shear lug has a face normal along world Y.")
    f, _, _ = actual_fields(base)
    assert f["anchor_hole", "diameter_in"] == 1.8125
    assert f["anchor_pattern", "minimum_edge_in"] == 2.41 and ("anchor_pattern", "edge_in") not in f
    assert f["anchor_pattern", "flange_offset_in"] == 2 and f["anchor_pattern", "spacing_in"] == 13.2
    assert ("washer_hole", "diameter_in") not in f
    assert f["shear_lug", "face_normal"] == [0, 1, 0] and f["shear_lug", "embedment_in"] == 6
    brace = source("lower end: HSS6X6X3/8 slotted for a 0.5 in A572 Gr 50 gusset; Whitmore width 16.4 in; "
                   "upper end: apex gusset 0.5 in A572 Gr 50, 36 in along the beam.")
    brace["data"]["checks"] = [{"limit_state": "gusset Whitmore yielding (Lw = 16.4 in, J4.1)"}]
    lower, record, _ = actual_fields(brace, upper_end=False, joint="base")
    upper, _, _ = actual_fields(brace, upper_end=True, joint="apex")
    assert lower["gusset", "whitmore_effective_width_in"] == 16.4 and ("gusset", "whitmore_effective_width_in") not in upper
    selected = next(f for f in record["fields"] if f["field"] == "whitmore_effective_width_in")
    assert selected["check_sources"][0]["pointer"] == "/connections/0/checks/0/limit_state"
    assert all(f["role"] != "brace_end_clearance" for f in record["fields"])


def test_crossing_receiver_and_post_end_scopes_cannot_migrate_to_nearby_members():
    cross = source("X-brace crossing: HSS4X4X5/16 continuous, slotted on two opposite walls; "
                   "the interrupted HSS4X4X5/16 in two halves: four 5/16 in E70 welds x 11 in; 2t linear clearance.")
    cross["data"]["checks"] = [{"limit_state": "slot welds, gusset to the continuous diagonal: "
        "slot fillets 1/4 in on the four slot edges, 18 in long, for the out-of-plane restraint"}]
    ordinary = source("HSS4X4X5/16 slotted for a 0.625 in A572 Gr 50 gusset, four 3/8 in E70 welds x 13 in", identity="ordinary")
    members = [{"id": key, "kind": "brace", "profile_id": "HSS4X4X5/16", "axis": axis}
               for key, axis in (("a", [[0,0,0],[10,0,10]]), ("b", [[0,0,10],[5,0,5]]), ("c", [[5,0,5],[10,0,0]]))]
    con = {"id":"cross","point":[5,0,5],"member_ids":["a","b","c"],"parameters":{"joint":"crossing"},
           "design_record_ids":[cross["id"],ordinary["id"]],"assembly_instance":{"bindings":{"CONT":"a","SPLIT-1":"b","SPLIT1":"c"}}}
    records = {r["id"]: selection_record(r) for r in (cross, ordinary)}
    links = build_selections(records, {"members":members, "connections":[con]})["cross"]["applications"]
    assert links[0]["crossing_members"] == {"continuous":["a"],"split":["b","c"]}
    fields = [records[cross["id"]]["fields"][i] for i in links[0]["selected"]]
    vals = {(f["role"],f["field"]):f["value"] for f in fields}
    assert vals["continuous_brace_slot_weld","length_in"] == 18 and vals["split_brace_slot_weld","length_in"] == 11
    assert all(f["applicability"]["crossing_receiver"] == "split" for f in fields if f["role"] == "brace_end_clearance")
    assert all(records[ordinary["id"]]["fields"][i]["field"] in ("source_type","design_variant_id") for i in links[1]["selected"])
    post = source("top: PL 3/8 x 4 x 8 in A36 clip, 2 x 3/4-in A325-N bolts; "
                  "base: base plate PL 0.75 x 15 x 18 in ASTM A572 Gr 50; 4 x 0.75 in ASTM F1554-36 anchor rods.")
    rec = {post["id"]:selection_record(post)}
    m = {"id":"p","kind":"column","axis":[[0,0,20],[0,0,0]],"profile_id":"W12X40"}
    joints = [{"id":name,"member_ids":["p"],"point":point,"design_record_ids":[post["id"]],
               "assembly_instance":{"bindings":{"POST":"p"}}} for name,point in zip(("high","low"),m["axis"])]
    apps = build_selections(rec,{"members":[m],"connections":joints})
    roles = {n:{rec[post["id"]]["fields"][i]["role"] for i in v["applications"][0]["selected"]} for n,v in apps.items()}
    assert "bolt" in roles["high"] and "anchor" not in roles["high"]
    assert "anchor" in roles["low"] and "bolt" not in roles["low"]


def test_base_brace_variants_follow_actual_column_and_override_only_the_support_side():
    general = source("HSS6X6X3/8 slotted for a 1.25 in A572 Gr 50 gusset; four 3/8 in E70 welds x 12 in; "
                     "column edge 32 in (0.75 in fillets both sides)", identity="general")
    base = source("base corner gusset 1.25 in A572 Gr 50 on the column wall: column edge 44 in (3/4 in fillets both sides), "
                  "bottom edge 9 in welded to a PL 1 in collar plate at the cap face; "
                  "at BOX30X4: gusset 1.25 in, column edge (40 in, CJP groove weld), bottom edge (1 in) "
                  "clear of the cap face (no foundation interface)",
                  label="HSS6X6X3/8 story-1 brace: base corner gusset to the column (BOX24X4) at the pile cap", identity="base")
    records={r["id"]:selection_record(r) for r in (general,base)}
    members=[{"id":s,"kind":"column","axis":[[0,i,0],[0,i,12]],"profile_id":s} for i,s in enumerate(("BOX24X4","BOX30X4"))]
    conns=[{"id":m["id"],"member_ids":[m["id"]],"point":m["axis"][0],"design_record_ids":list(records),
            "parameters":{"joint":"base"}} for m in members]
    apps=build_selections(records,{"members":members,"connections":conns})
    def values(cid):
        return {(f["role"],f["field"]):f["value"] for line in apps[cid]["applications"] for i in line["selected"]
                for f in [records[line["record_id"]]["fields"][i]]}
    a,b=values("BOX24X4"),values("BOX30X4")
    assert a["gusset_column_weld","length_in"]==44 and a["gusset_collar_weld","length_in"]==9
    assert b["gusset_column_weld","length_in"]==40 and b["gusset_column_weld","weld_type"]=="CJP"
    assert b["gusset_foundation_clearance","weld_to_foundation"] is False and ("gusset_collar_weld","length_in") not in b
    assert all(item["brace_slot_weld","size_in"]==.375 for item in (a,b))


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


def test_normal_corner_weld_after_a_common_brace_detail_does_not_apply_to_base_or_apex():
    row = source("HSS10.750X0.625 slotted for a 1 in A572 Gr 50 gusset, four 5/8 in E70 welds x 21 in; "
                 "corner gusset 1 in A572 Gr 50, column edge 32.0 in (3/4 in fillets both sides)")
    assert actual_fields(row, joint="corner")[0]["gusset_column_weld", "length_in"] == 32
    for joint in ("apex", "base"):
        fields, _, _ = actual_fields(row, joint=joint)
        assert fields["brace_slot_weld", "length_in"] == 21 and ("gusset_column_weld", "length_in") not in fields
    base = source("base corner gusset 1 in A572 Gr 50 on the column wall: column edge 44.0 in (3/4 in fillets both sides)")
    assert actual_fields(base, joint="base")[0]["gusset_column_weld", "length_in"] == 44
    assert ("gusset_column_weld", "length_in") not in actual_fields(base, joint="corner")[0]


def test_selected_anchor_setout_and_grout():
    r = source("base plate PL 0.75 x 16 x 32 in ASTM A572 Gr 50 on 2 in non-shrink grout; "
               "4 x 1 in ASTM F1554-36 anchor rods, 2 per flange side in one row outside the flanges at 12.5 in, "
               "1-3/4 in from the plate edges")
    f, _, _ = actual_fields(r)
    assert (f["anchor_pattern", "spacing_in"], f["anchor_pattern", "edge_in"]) == (12.5, 1.75)
    assert (f["base_plate", "width_in"], f["base_plate", "length_in"]) == (16, 32)
    assert (f["grout", "material"], f["grout", "thickness_in"]) == ("non-shrink grout", 2)
    heavy = source("6 x 3.5 in ASTM A193 Grade B7 (Fy = 95 ksi, Fu = 115 ksi) anchor rods; "
                   "the first row 6.50 in from the flange face, rods at 14.50 in centres and 4.75 in from the plate edges")
    f, _, _ = actual_fields(heavy)
    assert (f["anchor", "count"], f["anchor", "diameter_in"], f["anchor", "material"]) == (6, 3.5, "ASTM A193 Grade B7")
    assert (f["anchor_pattern", "flange_offset_in"], f["anchor_pattern", "spacing_in"], f["anchor_pattern", "edge_in"]) == (6.5, 14.5, 4.75)
    revised = source("provided rod-centre edge distances 4.85 in normal to the column flanges and 5.00 in along the flanges; "
                     "minimum required rod-centre edge 4.50 in")
    edges, _, _ = actual_fields(revised)
    assert [edges["anchor_pattern", name] for name in ("edge_flange_normal_in", "edge_along_flange_in", "minimum_edge_in")] == [4.85, 5, 4.5]


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


def test_collector_receiver_needs_the_matching_member_source_and_support():
    from steel_engine.bim_collector_applications import member_sources
    member_rows = [{"id": "members/collector-X-floor-W24X55", "table": "members", "source_pointer": "/members/0",
                    "data": {"inputs": {"role": "collector", "section": "W24X55"}}},
                   {"id": "members/column", "table": "members", "source_pointer": "/members/1",
                    "data": {"inputs": {"role": "gravity_col", "section": "W14X132"}}}]
    collector = source("single plate 0.5 x 15 in A572 Gr 50; 5 x 0.875 in A325-N bolts",
                       identity="conn-collector-X-floor", label="collector (drag strut) to SMF column: single plate")
    gravity = source("single plate 0.75 x 15 in A572 Gr 50; 5 x 0.875 in A490-X bolts",
                     label="single-plate shear with analysis axial force -- gravity beam ends")
    gravity["data"]["section"] = "W24X55"
    records = {r["id"]: selection_record(r) for r in (collector, gravity)}
    members = [{"id": "beam", "kind": "beam", "axis": [[0, 0, 5], [10, 0, 5]], "profile_id": "W24X55",
                "design_record_ids": [member_rows[0]["id"]]},
               {"id": "column", "kind": "column", "axis": [[0, 0, 0], [0, 0, 10]], "profile_id": "W14X132",
                "design_record_ids": [member_rows[1]["id"]]}]
    c = {"id": "c", "point": [0, 0, 5], "member_ids": ["beam", "column"], "design_record_ids": list(records),
         "parameters": {"collector": True}}
    def check():
        links = build_selections(records, {"members": members, "connections": [c]}, member_catalog=member_sources(member_rows))
        return links["c"]["applications"]
    col, local = check()
    assert col["interface_application"]["status"] == "different_receiver" and not col["selected"] and local["selected"]
    member_rows[1]["data"].update(inputs={"role": "lateral_col", "section": "W14X132"}, design_note="SMF column: selected section")
    col, local = check()
    assert col["interface_application"]["status"] == "located_interface" and col["selected"] and not local["selected"]
    members[0]["profile_id"] = "W21X101"
    assert check()[0]["interface_application"]["status"] == "different_member_section"


def test_web_and_flange_variants_use_the_receiving_column_section_axes_and_grid():
    r = source("ends on a column flange: two plates PL 0.375 x 17.5 in A572 Gr 50 (one each face of the beam web); "
               "6 x 1 x 7/8 in A490-X bolts; web-side ends on the W14X605 (Grid 7/B (Level 3)): "
               "two plates PL 0.4375 x 14.5 in A572 Gr 50 (one each face of the beam web); 5 x 2 x 7/8 in A490-X bolts")
    r["data"]["section"] = "W21X83"
    records = {r["id"]: selection_record(r)}
    ms = [{"id": "beam", "kind": "beam", "profile_id": "W21X83", "axis": [[0, 0, 3], [10, 0, 3]]},
          {"id": "column", "kind": "column", "profile_id": "W14X605", "local_y": [1, 0, 0], "axis": [[0, 0, 0], [0, 0, 10]]}]
    c = {"id": "c", "point": [0, 0, 3], "member_ids": ["beam", "column"], "design_record_ids": list(records)}
    physical = {"members": ms, "profiles": [{"id": "W14X605", "family": "i"}], "connections": [c]}
    ref = {"grids": [{"label": "7", "points_in": [[0, 0, 0], [0, 10, 0]]},
                     {"label": "B", "points_in": [[0, 0, 0], [10, 0, 0]]}], "levels": [{"level": 3, "elevation_in": 3}]}
    def apply():
        link = build_selections(records, physical, ref)["c"]["applications"][0]
        fields = records[r["id"]]["fields"]
        return link, [fields[i]["value"] for i in link["selected"] if (fields[i]["role"], fields[i]["field"]) == ("bolt", "count")]
    assert apply()[1] == [10]
    ms[1]["local_y"] = [0, 1, 0]
    assert apply()[1] == [6]
    ms[1]["local_y"] = [1, 0, 0]
    ref["levels"][0]["level"] = 9
    assert apply()[0]["support_application"]["selected_group"] is None
    assert apply()[1] == []


def test_source_referenced_floor_detail_does_not_apply_to_roof_end_of_the_same_section():
    target = source("end connection = the collector / strut detail of conn-floor-W24X68 "
                    "(one detail for every W24X68 end with a collector or axial force); 8 x 1 in A490-X bolts",
                    identity="collector-Y", label="collector (drag strut) end connection on the braced line")
    roof = source("6 x 1 in A490-X bolts", identity="conn-roof-W24X68")
    roof["data"]["section"] = "W24X68"
    records = {r["id"]: selection_record(r) for r in (target, roof)}
    physical = {"members": [{"id": "beam", "kind": "beam", "profile_id": "W24X68", "axis": [[0, 0, 100], [0, 10, 100]]}],
                "connections": [{"id": "c", "point": [0, 0, 100], "member_ids": ["beam"], "design_record_ids": list(records)}]}
    alias, local = build_selections(records, physical)["c"]["applications"]
    assert alias["interface_application"]["status"] == "different_source_end"
    assert alias["selected"] == [] and local["selected"]


def test_combined_brace_and_collector_selection_requires_the_same_brace_and_beam_binding():
    special = source("single plate 0.75 x 18 in A572 Gr 50; 2 column(s) x 6 rows of 0.875 in ASTM F3125 A325-X bolts",
                     identity="conn-bracedbay-beam-X-HSS8X8X1/2", label="braced-bay beam-to-column connection (single plate, shear + axial)")
    special["data"]["demand"] = {"basis": "beam reaction + UFM Vb; UFM Hb + collector"}
    simple = source("single plate 0.375 in x 12 in A572 Gr 50; 4 x 0.75 in A325-N bolts",
                    identity="simple", label="single-plate shear connection (conventional configuration, AISC Manual Part 10)")
    records = {r["id"]: selection_record(r) for r in (special, simple)}
    ms = [{"id": "beam", "kind": "beam", "profile_id": "W21X68", "axis": [[0, 0, 10], [10, 0, 10]]},
          {"id": "brace", "kind": "brace", "profile_id": "HSS8X8X1/2", "axis": [[0, 0, 10], [10, 0, 0]]}]
    physical = {"members": ms, "connections": [
        {"id": "c", "point": [0, 0, 10], "member_ids": ["beam"], "design_record_ids": list(records)},
        {"id": "x", "point": [0, 0, 10], "member_ids": ["brace", "beam"], "parameters": {"joint": "corner"}}]}
    combined, local = build_selections(records, physical)["c"]["applications"]
    assert combined["interface_application"]["status"] == "located_interface"
    assert local["selected"] == [] and local["included_in_combined_end"] == [special["id"]]
    fields = records[special["id"]]["fields"]
    assert [(fields[i]["role"], fields[i]["value"]) for i in combined["selected"] if fields[i]["field"] == "count"] == [("bolt", 12)]
    physical["connections"][1]["member_ids"] = ["brace", "unrelated-beam"]
    combined, local = build_selections(records, physical)["c"]["applications"]
    assert not combined["selected"] and local["selected"]


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

def test_original_gravity_base_precision_round_trip():
    text=("base plate PL 1 x 18 x 23 in ASTM A572 Gr 50; 4 x 1 in ASTM F1554-36 anchor rods; "
          "rod layout: 2 rods outside each flange in one row of 2 (a1 = 2.00 in from the flange face), "
          "rods 13.2 in apart (not less than 4 da = 4.0 in), 2.41 in or more from the plate edges "
          "(at least the AISC 360-22 Table J3.4 / J3.5 distance for the hole), "
          "1.8125 in holes (AISC Manual Table 14-2) with plate washers; no net uplift at this base")
    fields={(f["role"],f["field"]):f for f in selection_record(source(text))["fields"]}
    spacing=fields["anchor_pattern","spacing_in"]
    assert (spacing["value"],spacing["printed_value"],spacing["printed_decimal_places"])==(13.1875,13.2,1)
    assert fields["anchor_pattern","minimum_edge_in"]["value"]==2.40625
    assert not any("derivation" in f for f in selection_record(source(text.replace("13.2 in apart","13.8 in apart")))["fields"])


def test_gravity_base_precision_on_actual_uplift_and_top_washer_branch():
    text = ("base plate PL 1 x 13 x 23 in ASTM A572 Gr 50; 4 x 1 in ASTM F1554-36 anchor rods; "
            "rod layout: 2 rods outside each flange in one row of 2 (a1 = 2.00 in from the flange face), "
            "rods 8.2 in apart (not less than 4 da = 4.0 in), 2.41 in or more from the plate edges "
            "(at least the AISC 360-22 Table J3.4 / J3.5 distance for the hole), "
            "1.8125 in holes (AISC Manual Table 14-2) with top plate washers; anchor reinforcement As >= 0.8 in2")
    row = source(text)
    row["data"]["id"] = "conn-base-gravity_col-W14X61"
    fields = {(f["role"], f["field"]): f for f in selection_record(row)["fields"]}
    assert fields["anchor_pattern", "spacing_in"]["value"] == 8.1875
    assert fields["anchor_pattern", "minimum_edge_in"]["printed_value"] == 2.41
    row["data"]["id"] = "conn-base-gravity_col-UNKNOWN"
    assert not any("derivation" in f for f in selection_record(row)["fields"])


def test_signed_support_fillets_resolve_two_faces_and_the_named_end():
    r=source("lower end: HSS6X6X3/8 slotted for a 0.5 in A572 Gr 50 gusset; upper end: apex gusset 0.5 in.")
    r["data"]["checks"]=[{"limit_state":"lower end: base gusset-to-base-plate welds, two 3/8 in fillets, base-edge weld length 5.0 in"},
                         {"limit_state":"upper end: apex gusset-to-beam flange welds, two 3/8 in fillets x 36 in"}]
    lower,_,_=actual_fields(r,upper_end=False,joint="base")
    upper,_,_=actual_fields(r,upper_end=True,joint="apex")
    assert lower["gusset_base_weld","faces"]==2 and ("gusset_beam_weld","faces") not in lower
    assert upper["gusset_beam_weld","faces"]==2 and upper["gusset_beam_weld","length_in"]==36


def test_four_rod_original_pinned_base_edges_and_bore():
    r=source("base plate PL 1 5/8 x 23 x 23 in (ASTM A572 Gr 50) on 2 in non-shrink grout; "
             "4 anchor rods 3/4 in F1554-36, two outside each flange 2.25 in from the flange face at 3.25 in, "
             "2.000 in from the plate edges, with plate washers; cruciform shear lug PL 1 in "
             "(legs 16 in wide, 3 in below the grout; ASTM A572 Gr 50)")
    r["data"]["checks"]=[{"limit_state":"rod centre ... half the 2 in plate washer) (1.3125 in hole, AISC Manual Table 14-2)"}]
    f,_,_=actual_fields(r)
    assert f["anchor_pattern","edge_flange_normal_in"]==2 and f["anchor_pattern","edge_along_flange_in"]==9.875
    assert f["anchor_hole","diameter_in"]==1.3125 and f["anchor","diameter_in"]==.75
    assert f["cruciform_shear_lug","thickness_in"]==1 and f["plate_washer","width_in"]==2


def test_reentrant_opening_corner_distinguishes_actual_edge_from_continuation():
    from steel_engine.bim_collector_applications import opening_corner
    def quad(x, y):
        return [[x,y,0],[x+10,y,0],[x+10,y+10,0],[x,y+10,0]]
    def beam(x,y):
        return {"kind":"beam", "axis":[[10,10,0],[x,y,0]]}
    members = {"edge":beam(10,20), "continuation":beam(10,0)}
    panels = [quad(0,0),quad(0,10),quad(10,0)]
    assert opening_corner([10,10,0], members, panels) == (True,["edge"])
    assert opening_corner([10,10,0], members, panels + [quad(10,10)]) == (False,[])


def test_crossing_same_as_end_selects_only_split_fillet_and_keeps_external_whitmore():
    r = source("HSS6X6X3/8 slotted for a 0.5 in A572 Gr 50 gusset; four 1/4 in E70 fillet welds x 9.0 in; "
               "Whitmore width 16.4 in. Crossing: one diagonal continuous, the other connected through a 1/2 in "
               "gusset passing through the slotted continuous brace, same welds as the end connection")
    record = selection_record(r)
    row = {"id":"X-CROSS-0","design_record_ids":[r["id"]],"parameters":{"joint":"crossing"}}
    source_location = {"crossing_members":{"split":["B1","B2"],"continuous":["B3"]},"end_members":{}}
    link, = applications(row, {r["id"]:record}, source_location)
    f = {(record["fields"][i]["role"],record["fields"][i]["field"]):record["fields"][i]["value"] for i in link["selected"]}
    assert f["split_brace_slot_weld","length_in"] == 9
    assert ("brace_slot_weld","length_in") not in f and ("gusset","whitmore_effective_width_in") not in f

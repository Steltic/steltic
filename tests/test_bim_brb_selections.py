from copy import deepcopy
import math

from steel_engine.bim_brb_selections import checked_bolt_pattern, member_selections


def test_checked_terminal_survives_real_member_handoff_and_disagreement_is_rejected():
    import pytest
    terminal = {'yielding_length_workpoint_ratio':.53,'minimum_transition_length_in':12.,'end_area_ratio':2.4,
                'head_joint_weld':'CJP','head_joint_electrode':'E70','core_material':'ASTM A36',
                'head_material':'ASTM A572 Grade 50','pin_clearance_diameter_in':.0625}
    record={'table':'members','id':'members/brace-BRB20','source_pointer':'/members/2','data':{
        'inputs':{'section':'BRB20'},'capacity':{'Asc_in2':20},'brb_core_terminal':terminal,
        'checks':[{'limit_state':'core strain over the yielding length 53 in (0.53 Lwp)'},
                  {'limit_state':'global stability of the buckling-restraining casing, HSS18X18X1/2 ASTM A500 Grade C; concrete fill neglected'}]}}
    physical={'members':[{'id':'B','kind':'brace','profile_id':'BRB20','axis':[[0,0,0],[100,0,0]],'design_record_ids':[record['id']]}]}
    member=member_selections([record],physical,{'materials':[]})['B']
    lookup={(f['role'],f['field']):f for f in member['fields']}
    assert member['yielding_core_length_in']==53
    assert lookup['brb_core_terminal','head_joint_weld']['source']['pointer']=='/members/2/brb_core_terminal/head_joint_weld'
    assert lookup['brb_casing','material']['value']=='ASTM A500 Grade C'
    record['data']['brb_core_terminal']['yielding_length_workpoint_ratio']=.6
    with pytest.raises(ValueError,match='disagrees with the member calculation'):
        member_selections([record],physical,{'materials':[]})


def test_selected_core_uses_each_workline_and_the_named_material():
    source = {"table": "members", "id": "members/brace-BRB10", "source_pointer": "/members/3",
              "data": {"inputs": {"section": "BRB10", "expected_strength": {"system": "BRBF", "basis": "Fysc 46, Ry 1.00"}},
                       "capacity": {"Asc_in2": 10},
                       "checks": [{"limit_state": "core yield (Fysc,min = 38 ksi)"},
                                  {"limit_state": "core strain over the yielding length 142 in (0.60 Lwp, Lwp = 237 in)"}]}}
    def brace(mid, length, profile):
        return {"id": mid, "kind": "brace", "profile_id": profile, "axis": [[0, 0, 0], [0, length, 0]],
                "design_record_ids": [source["id"]]}
    physical = {"members": [brace("short", 214, "BRB10"), brace("long", 237, "BRB10"), brace("wrong", 225, "BRB11")]}
    materials = {"materials": [{"item": "Plates", "spec": "ASTM A36"},
                               {"item": "Buckling-restrained braces", "spec": "steel core ASTM A36 with coupon-certified yield stress"}]}
    result = member_selections([source], physical, materials)
    assert math.isclose(result["short"]["yielding_core_length_in"], 128.4)
    assert math.isclose(result["long"]["yielding_core_length_in"], 142.2)
    assert result["wrong"]["status"] == "unresolved_member_selection"
    fields = {x["field"]: x for x in result["short"]["fields"]}
    assert {k: f["value"] for k, f in fields.items()} == {
        "area_in2": 10, "fy_min_ksi": 38, "fy_max_ksi": 46, "yielding_length_workpoint_ratio": .6, "material": "ASTM A36"}
    assert fields["material"]["source"]["pointer"] == "/material_schedule/materials/1/spec"


def test_checked_block_shear_and_whitmore_pin_the_longitudinal_bolt_pattern():
    row = {"source_pointer": "/connections/3", "data": {
        "components": "BRB end (supplier casing/core extension) bolted between two splice plates to a 1 in A572 Gr 50 gusset with 6 x 1.125 in A490-X bolts (2 rows at 3.5 in)",
        "checks": [{"limit_state": "gusset Whitmore yielding (width 24.4 in)"},
                   {"limit_state": "gusset block shear around the bolt group", "phiTn_kip": 965.25}]}}
    fields = {x["field"]: x["value"] for x in checked_bolt_pattern(row)}
    assert fields == {"longitudinal_stations": 3, "transverse_lines": 2, "longitudinal_pitch_in": 3.5,
                      "loaded_end_distance_in": 2., "transverse_shear_plane_spacing_in": 14., "block_shear_path_length_in": 9.}
    for i, name, altered in [(1, "phiTn_kip", 870), (0, "limit_state", "gusset Whitmore yielding (width 22.4 in)")]:
        changed = deepcopy(row)
        changed["data"]["checks"][i][name] = altered
        assert checked_bolt_pattern(changed) == []


def test_selected_casing_infill_and_reported_elongation_have_independent_sources():
    record = {"table":"members", "id":"members/brace-BRB10", "source_pointer":"/members/2",
              "data":{"inputs":{"section":"BRB10"}, "capacity":{"Asc_in2":10}, "checks":[
                  {"limit_state":"core strain: brace elongation 3.01 in over the yielding length 133 in (0.60 Lwp)"},
                  {"limit_state":"global stability of the buckling-restraining casing (steel HSS12X12X3/8 A500 Gr C, concrete fill neglected)"}]}}
    physical = {"members":[{"id":"B","kind":"brace","profile_id":"BRB10","axis":[[0,0,0],[0,120,0]],
                            "design_record_ids":[record["id"]]}]}
    def values(spec):
        rows = member_selections([record], physical, {"materials":[{"item":"brace cores","spec":spec}]})["B"]["fields"]
        return {(f["role"],f["field"]):f for f in rows}
    f=values("ASTM A36 steel core in a mortar-filled steel casing by the BRB manufacturer")
    assert f["brb_casing","material"]["value"] == "ASTM A500 Grade C"
    assert f["brb_casing","section"]["value"] == "HSS12X12X3/8"
    assert f["brb_casing","infill_material"]["source"]["pointer"] == "/material_schedule/materials/0/spec"
    assert f["brb_core","expected_elongation_in"]["value"] == 3.01
    record["data"]["checks"].pop()
    f=values("steel core ASTM A36; casing, core extensions and end details by the BRB supplier")
    assert f["brb_casing","design_mode"]["value"] == "supplier_delegated"
    assert ("brb_casing","material") not in f and ("brb_casing","infill_material") not in f

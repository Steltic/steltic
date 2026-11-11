from copy import deepcopy
import math

from steel_engine.bim_brb_selections import checked_bolt_pattern, member_selections


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

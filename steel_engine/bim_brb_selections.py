"""Bind BRB core dimensions and checked splice-group orientation to their source."""
import math
import re


def _field(role, key, value, unit, pointer, literal):
    return {"role": role, "field": key, "value": value, "unit": unit, "applicability": {},
            "source": {"pointer": pointer, "literal": literal}}


def _core_material(materials):
    matches = []
    for i, row in enumerate(materials.get("materials", [])):
        if "brace" not in row.get("item", "").lower():
            continue
        spec = row.get("spec", "")
        if re.search(r"\bASTM A36 (?:steel )?core\b|\b(?:steel )?core ASTM A36\b", spec, re.I):
            field = _field("brb_core", "material", "ASTM A36", "literal",
                           f"/material_schedule/materials/{i}/spec", spec)
            field["source"]["document"] = row.get("source", materials.get("source", {}))
            matches.append(field)
    return matches[:1]


def _member_fields(record, materials):
    data = record["data"]
    base = record["source_pointer"]
    capacity = data.get("capacity", {})
    area = capacity.get("Asc_in2")
    fields = []
    if isinstance(area, (int, float)) and not isinstance(area, bool) and math.isfinite(area) and area > 0:
        fields.append(_field("brb_core", "area_in2", area, "in2", base + "/capacity/Asc_in2", area))
    for i, check in enumerate(data.get("checks", [])):
        literal = check.get("limit_state", "")
        pointer = f"{base}/checks/{i}/limit_state"
        ratio = re.search(r"over the yielding length [^()]*\(([.\d]+)\s*Lwp\b", literal, re.I)
        if ratio:
            fields.append(_field("brb_core", "yielding_length_workpoint_ratio", float(ratio[1]),
                                 "ratio", pointer, literal))
        lower = re.search(r"Fysc,\s*min\s*=\s*([\d.]+)\s*ksi\b", literal, re.I)
        if lower:
            fields.append(_field("brb_core", "fy_min_ksi", float(lower[1]), "ksi", pointer, literal))
    expected = data.get("inputs", {}).get("expected_strength", {})
    basis = expected.get("basis", "")
    upper = re.search(r"\bFysc\s+([\d.]+)\s*,\s*Ry\b", basis, re.I)
    if expected.get("system") == "BRBF" and upper:
        fields.append(_field("brb_core", "fy_max_ksi", float(upper[1]), "ksi",
                             base + "/inputs/expected_strength/basis", basis))
    return fields + _core_material(materials)


def member_selections(native_records, physical, materials):
    """Scale the selected ratio on each located workline, including unequal grouped members."""
    catalog = {r["id"]: r for r in native_records if r.get("table") == "members" and not r.get("waived")
               and str(r.get("data", {}).get("inputs", {}).get("section", "")).startswith("BRB")}
    selected = {}
    for m in physical.get("members", []):
        if m.get("kind") != "brace" or not str(m.get("profile_id", "")).startswith("BRB"):
            continue
        refs = m.get("selected_design_record_ids") or m.get("design_record_ids", [])
        matches = [catalog[k] for k in refs if k in catalog and
                   catalog[k]["data"]["inputs"]["section"] == m["profile_id"]]
        if len(matches) != 1 or len(m.get("axis", [])) != 2:
            selected[m["id"]] = {"status": "unresolved_member_selection", "physical_profile": m["profile_id"],
                                 "candidate_record_ids": [r["id"] for r in matches]}
            continue
        record = matches[0]
        axis = m["axis"]
        length = math.dist(*axis)
        fields = _member_fields(record, materials)
        ratios = {f["value"] for f in fields if f["field"] == "yielding_length_workpoint_ratio"}
        item = {"status": "located", "record_id": record["id"], "source_pointer": record["source_pointer"],
                "physical_profile": m["profile_id"], "axis_in": axis, "workpoint_length_in": length, "fields": fields}
        if len(ratios) == 1 and 0 < next(iter(ratios)) < 1 and length > 0:
            item["yielding_core_length_in"] = length * next(iter(ratios))
        selected[m["id"]] = item
    return selected


def checked_bolt_pattern(record):
    """Recover the legacy Group D layout only if its two printed strength results agree."""
    data = record["data"]
    text = data.get("components", "")
    if not isinstance(text, str):
        return []
    group = re.match(r"BRB end \(supplier casing/core extension\) bolted between two splice plates to a "
                     r"([\d.]+) in A572 Gr 50 gusset with (\d+) x 1\.125 in A490-X bolts "
                     r"\(2 rows at 3\.5 in\)", text, re.I)
    if not group:
        return []
    t, count = float(group[1]), int(group[2])
    if count < 4 or count % 2:
        return []
    rows, pitch, end, bc, hole = count // 2, 3.5, 2., 14., 1.25
    length = (rows - 1) * pitch + end
    whitmore = bc + 2 * length * math.tan(math.radians(30))
    shear = min(.6 * 65 * 2 * (length - (rows - .5) * hole) * t, .6 * 50 * 2 * length * t)
    phi_block = .75 * (shear + 65 * (bc - hole) * t)
    checks = data.get("checks", [])
    block = next(((i, c) for i, c in enumerate(checks)
                  if c.get("limit_state") == "gusset block shear around the bolt group"), None)
    width = next(((i, re.fullmatch(r"gusset Whitmore yielding \(width ([\d.]+) in\)", c.get("limit_state", "")))
                  for i, c in enumerate(checks) if c.get("limit_state", "").startswith("gusset Whitmore yielding (width")), None)
    if not block or not width or not width[1]:
        return []
    if abs(float(width[1][1]) - whitmore) > .050001 or not math.isclose(
            block[1].get("phiTn_kip", -1), phi_block, abs_tol=.006):
        return []
    base = record["source_pointer"]
    proof = [base + "/components", f"{base}/checks/{block[0]}/phiTn_kip", f"{base}/checks/{width[0]}/limit_state"]
    result = []
    for key, value, unit in (("longitudinal_stations", rows, "count"), ("transverse_lines", 2, "count"),
                             ("longitudinal_pitch_in", pitch, "in"), ("loaded_end_distance_in", end, "in"),
                             ("block_shear_path_length_in", length, "in"), ("transverse_shear_plane_spacing_in", bc, "in")):
        field = _field("brb_bolt_pattern", key, value, unit, base + "/components", group.group())
        field["source"].update(basis="original Group D brb_conn checked bolt layout", corroborating_pointers=proof,
                               source_routine="stk_ext/groupD_fill.py:brb_conn",
                               equations="Lg=(n/2-1)s+e; Whitmore=bc+2Lg*tan(30deg); J4.3 Anv and Ant from Lg and bc")
        result.append(field)
    return result

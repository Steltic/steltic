"""Source selections used by the HR-to-openBIM handoff.

New calculation records can supply typed selections. For older records the
reader recognizes explicit component clauses, records the literal and its
character range, and retains the complete original record. A later reference
to a pin in a design check does not select a pin connection.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from fractions import Fraction


NUMBER = r"(?:\d+[- ]\d+/\d+|\d+/\d+|\d+(?:\.\d+)?|\.\d+)"
COUNT = r"(?:\d+|two|three|four|six|eight|ten|twelve)"
X = r"\s*(?:x|\u00d7)\s*"
IN = r"\s*(?:-| )?in\.?\b"
GRADE = r"(?:ASTM\s+)?(?:A572\s+(?:Gr(?:ade)?\.?\s*)?50|A36|A588|A992|A193\s+(?:Gr(?:ade)?\.?\s*)?B7)"
PIN_GRADE = r"(?:AISI\s+4140(?:\s+Q\s*&\s*T)?|ASTM\s+A434\s+Class\s+BD|ASTM\s+A572\s+Gr(?:ade)?\.?\s*50|ASTM\s+A193\s+Gr(?:ade)?\.?\s*B7)"
BOLT_GRADE = r"(?:ASTM\s+)?(?:F3125\s+(?:Gr(?:ade)?\.?\s*)?)?A(?:325|490)(?:-[NX])?"
WORDS = dict(two=2, three=3, four=4, six=6, eight=8, ten=10, twelve=12)
ATTACHMENT_ROLES = {
    "clevis_outer_fillet": ["clevis_plate_outer_face", "beam_flange"],
    "brace_slot_weld": ["brace", "gusset"],
    "reinforcement_fillet": ["brace", "slot_reinforcement"],
    "gusset_column_weld": ["gusset", "column"],
    "gusset_beam_weld": ["gusset", "beam"],
    "gusset_base_weld": ["gusset", "base_plate"],
}


def digest_value(value):
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def number(text):
    """Read conventional construction fractions, including 1-3/4 inches."""
    text = str(text).strip().lower()
    if text in WORDS:
        return WORDS[text]
    mixed = re.fullmatch(r"(\d+)[- ](\d+/\d+)", text)
    value = Fraction(mixed[1]) + Fraction(mixed[2]) if mixed else Fraction(text)
    return int(value) if value.denominator == 1 else float(value)


def _scope(joint=None, end=None, story=None):
    return {key: value for key, value in (("joint", joint), ("end", end), ("story", story)) if value is not None}


def _scope_at(text, position):
    """Apply only explicit source end/diagram labels preceding this clause."""
    context = text[:position]
    # A vertical bar separates complete corner/apex detail descriptions.
    section = re.split(r"\s*\|\s*", context)[-1]
    end = re.findall(r"(?:^|;)\s*(lower|upper)\s+end\s*:", section, re.I)
    end = end[-1].lower() if end else None
    if re.match(r"\s*(?:at (?:the )?)?apex gusset\b", section, re.I):
        return _scope(joint="apex", end=end)
    if re.match(r"\s*corner gusset at the (?:column base|base plate)\b", section, re.I):
        return _scope(joint="base", end=end)
    if re.match(r"\s*corner gusset\b", section, re.I):
        return _scope(joint="corner", end=end)
    # A source record may select end-specific reinforcement while describing
    # the separate apex later. Do not apply that field to its opposite end.
    return _scope(end=end)


def _field(role, name, value, unit, pointer, literal, span=None, applicability=None):
    source = {"pointer": pointer, "literal": literal}
    if span is not None:
        source["span"] = list(span)
    field = {"role": role, "field": name, "value": value, "unit": unit,
             "applicability": applicability or {}, "source": source}
    if role in ATTACHMENT_ROLES:
        field["attachment_roles"] = ATTACHMENT_ROLES[role][:]
    return field


def _add_matches(fields, text, pointer, pattern, role, groups, scope=True):
    for match in re.finditer(pattern, text, re.I):
        applies = _scope_at(text, match.start()) if scope else {}
        for name, unit in groups:
            raw = match.groupdict().get(name)
            if raw is None:
                continue
            value = number(raw) if unit in ("in", "count", "ratio", "ksi") else raw.strip()
            fields.append(_field(role, name, value, unit, pointer, match.group(), match.span(), applies))


def _legacy_fields(record):
    data, ptr = record["data"], record["source_pointer"]
    text = data.get("components")
    fields = []
    # Structured historic component records retain their original pointers.
    if isinstance(text, dict):
        count_keys = ("n_bolts", "bolt_count")
        count = next((key for key in count_keys if key in text), None)
        if count:
            for key, field, unit in ((count, "count", "count"), ("diameter_in", "diameter_in", "in"),
                                     ("bolt_dia_in", "diameter_in", "in"), ("specification", "material", "literal")):
                if key in text:
                    fields.append(_field("bolt", field, text[key], unit, ptr + "/components/" + key, text[key]))
        return fields
    if not isinstance(text, str):
        return fields
    src = ptr + "/components"
    # The family is determined from an explicit selection clause, never from
    # citations, capacities, incidental mentions of pins, or template names.
    variants = (
        ("brb_bolted", r"\bBRB end\s*\([^)]*\)\s*bolted between two splice plates"),
        ("brb_pinned", r"\b(?:corner|apex) gusset\b[^|;]{0,260}\b(?:pin\s+" + NUMBER + IN + r"\s*dia|" + NUMBER + IN + r"\s*dia\.?\s*pin)\b"),
        ("beam_pin_hinge", r"(?:^|:)\s*pin[- ]hinge release beyond the corner gusset"),
        ("beam_pin_hinge", r"\bthe beam joins the stub only through a\s+" + NUMBER + IN + r"\s*pin\b"),
        ("beam_pin_hinge", r"^pin\s+" + NUMBER + IN + r"\s*dia\.?\s*\([^)]+\)\s+in double shear,[^;]{0,160}\bflange gap\b"),
        ("single_plate_shear", r"\bsingle[- ]plate\s+(?:PL\s*)?" + NUMBER + r"(?:\s*in\.?)?" + X + NUMBER + IN),
        ("slotted_hss_gusset", r"\bHSS[\d./X-]+\s+(?:brace\s+)?slotted for (?:an?|the)\s+" + NUMBER + IN),
        ("base_plate_anchors", r"^\s*(?:(?:pinned|fixed)\s+)?base plate\s+PL\s*" + NUMBER + X + NUMBER + X + NUMBER),
    )
    chosen = {}
    for variant, pattern in variants:
        match = re.search(pattern, text, re.I)
        if match and variant not in chosen:
            chosen[variant] = match
    for variant, match in chosen.items():
        fields.append(_field("connection", "variant", variant, "enum", src, match.group(), match.span()))
    if isinstance(data.get("id"), str):
        fields.append(_field("connection", "design_variant_id", data["id"], "literal", ptr + "/id", data["id"]))
    if isinstance(data.get("type"), str):
        source_type = data["type"]
        fields.append(_field("connection", "source_type", source_type, "literal", ptr + "/type", source_type))
        if not chosen:
            # Use the selected connection type field for these classifications.
            # The complete type remains available when it includes a location,
            # independent end variant, or a more specific source design.
            for variant, pattern in (
                ("column_continuity", r"^no column splice\b"),
                ("column_splice", r"^(?:(?:gravity|SFRS|SMF|SCBF|OCBF|SPSW|STMF|CJP[- ]welded|field)\s+)*column splice\b|^splice of (?:a|the)\b.*?\bcolumns?\b"),
                ("reduced_beam_section", r"^SMF reduced beam section\b"),
                ("moment_frame_joint", r"^SMF beam[- ](?:to[- ])?column joint\b"),
                ("brace_frame_beam_end", r"^braced[- ](?:bay|frame) beam(?:[- ]to[- ]column| end) (?:connection|joint)\b"),
                ("fixed_column_base", r"^(?:SMF fixed base|fixed (?:SFRS )?column base)\b"),
                ("pinned_column_base", r"^pinned (?:SFRS )?column base\b"),
                ("brace_crossing", r"^x[- ]brace crossing connection\b"),
                ("coupling_beam_wall", r"^coupling beam[- ]to[- ]wall connection\b"),
                ("wall_foundation", r"^composite wall[- ]to[- ]mat foundation connection\b"),
                ("truss_column", r"^STMF truss[- ]to[- ]column connection\b"),
                ("collector_end", r"^(?:setback transfer )?collector end connections?\b"),
                ("braced_corner", r"^SCBF corner joint\b"),
            ):
                match = re.search(pattern, source_type, re.I)
                if match:
                    fields.append(_field("connection", "variant", variant, "enum", ptr + "/type", match.group(), match.span()))
                    break

    pin_after = rf"(?P<diameter_in>{NUMBER}){IN}\s*(?:dia(?:meter)?\.?\s*)?pin\s*\(\s*(?P<material>{PIN_GRADE})"
    pin_before = rf"\bpin\s+(?P<diameter_in>{NUMBER}){IN}\s*dia(?:meter)?\.?\s*\(\s*(?P<material>{PIN_GRADE})"
    for pattern in (pin_after, pin_before):
        _add_matches(fields, text, src, pattern, "pin", (("diameter_in", "in"), ("material", "literal")))
    _add_matches(fields, text, src, rf"\bpin hole\s+(?P<diameter_in>{NUMBER})(?:\s*in|\s*=)", "pin_hole",
                 (("diameter_in", "in"),))
    _add_matches(fields, text, src, rf"\bholes?\s+d\s*\+\s*(?P<clearance_in>{NUMBER}){IN}",
                 "pin_hole", (("clearance_in", "in"),))
    _add_matches(fields, text, src, rf"\bwith\s+(?P<count>{COUNT})\s+(?P<thickness_in>{NUMBER}){IN}\s+cheek plates",
                 "cheek_plate", (("count", "count"), ("thickness_in", "in")))
    _add_matches(fields, text, src, rf"\bat the hole,\s*width\s+(?P<width_at_hole_in>{NUMBER}){IN},\s*extension\s+(?P<extension_in>{NUMBER}){IN}",
                 "gusset", (("width_at_hole_in", "in"), ("extension_in", "in")))
    _add_matches(fields, text, src, rf"\bknife plate\s+(?:PL\s*)?(?P<thickness_in>{NUMBER}){IN}",
                 "knife_plate", (("thickness_in", "in"),))
    _add_matches(fields, text, src, rf"\b(?P<count>{COUNT})\s+clevis plates\s+(?:PL\s*)?(?P<thickness_in>{NUMBER}){IN}",
                 "clevis_plate", (("count", "count"), ("thickness_in", "in")))
    for role, axis in (("knife_plate", "Lk"), ("clevis_plate", "Lc")):
        _add_matches(fields, text, src, rf"\binsertion\s+{axis}\s*=\s*(?P<insertion_in>{NUMBER}){IN}",
                     role, (("insertion_in", "in"),))
    _add_matches(fields, text, src, rf"(?P<gap_in>{NUMBER}){IN}\s+flange gap\b", "beam_flange", (("gap_in", "in"),))

    # These two constructions identify the outside face of the clevis, as
    # opposed to adjacent knife/stub/inner-face welds.
    for tail in (r"on the outer faces to both (?:beam )?flanges", r"to both (?:beam )?flanges on their outer faces"):
        _add_matches(fields, text, src, rf"(?P<size_in>{NUMBER}){IN}\s*(?:E70\w*\s+)?fillets?\s+{tail}",
                     "clevis_outer_fillet", (("size_in", "in"),))
    _add_matches(fields, text, src, rf"\b(?P<count>{COUNT}){X}(?P<diameter_in>{NUMBER}){IN}\s+(?P<material>{BOLT_GRADE})\s+bolts?\b",
                 "bolt", (("count", "count"), ("diameter_in", "in"), ("material", "literal")))
    _add_matches(fields, text, src, rf"\bbolts?\s*\(\s*(?P<rows>\d+)\s+rows?\s+at\s+(?P<pitch_in>{NUMBER}){IN}",
                 "bolt", (("rows", "count"), ("pitch_in", "in")))
    _add_matches(fields, text, src, rf"\b{BOLT_GRADE}\s+bolts?\s+at\s+(?P<pitch_in>{NUMBER}){IN}",
                 "bolt", (("pitch_in", "in"),))
    _add_matches(fields, text, src, rf"\bsingle[- ]plate\s+(?:PL\s*)?(?P<thickness_in>{NUMBER})(?:\s*in\.?)?{X}(?P<depth_in>{NUMBER}){IN}\s+(?P<material>{GRADE})",
                 "shear_plate", (("thickness_in", "in"), ("depth_in", "in"), ("material", "literal")))
    for axis in ("vertical", "horizontal"):
        _add_matches(fields, text, src, rf"\b{axis} edge distance\s+(?P<{axis}_edge_in>{NUMBER}){IN}",
                     "bolt", ((axis + "_edge_in", "in"),))
    _add_matches(fields, text, src, rf"\bbeam end set back\s+(?P<cutback_in>{NUMBER}){IN}\s+from the support face",
                 "beam_end", (("cutback_in", "in"),))
    _add_matches(fields, text, src, rf"\bbase plate\s+PL\s*(?P<thickness_in>{NUMBER}){X}(?P<width_in>{NUMBER}){X}(?P<length_in>{NUMBER}){IN}(?:\s+(?P<material>{GRADE}))?",
                 "base_plate", (("thickness_in", "in"), ("width_in", "in"), ("length_in", "in"), ("material", "literal")))
    _add_matches(fields, text, src, rf"\b(?P<count>{COUNT}){X}(?P<diameter_in>{NUMBER}){IN}\s+(?P<material>(?:ASTM\s+)?F1554[- ](?:Gr(?:ade)?\.?\s*)?(?:36|55|105))\s+anchor rods?",
                 "anchor", (("count", "count"), ("diameter_in", "in"), ("material", "literal")))
    _add_matches(fields, text, src, rf"\bone row outside the flanges at\s+(?P<spacing_in>{NUMBER}){IN}",
                 "anchor_pattern", (("spacing_in", "in"),))
    _add_matches(fields, text, src, rf"(?P<edge_in>{NUMBER}){IN}\s+from the plate edges\b",
                 "anchor_pattern", (("edge_in", "in"),))
    _add_matches(fields, text, src, rf"\bon\s+(?P<thickness_in>{NUMBER}){IN}\s+(?P<material>(?:cementitious\s+)?non[- ]shrink grout)",
                 "grout", (("thickness_in", "in"), ("material", "literal")))
    for pattern in (
        rf"\b(?:corner|apex|base) gusset\s+(?P<thickness_in>{NUMBER}){IN}(?:\s+(?P<material>{GRADE}))?",
        rf"\b(?:slotted for|splice plates to)\s+(?:an?|the)\s+(?P<thickness_in>{NUMBER}){IN}\s+(?P<material>{GRADE})\s+gusset",
    ):
        _add_matches(fields, text, src, pattern, "gusset", (("thickness_in", "in"), ("material", "literal")))
    _add_matches(fields, text, src, rf"\b(?P<count>{COUNT})\s+(?P<size_in>{NUMBER}){IN}\s+E70\w*\s+(?:fillet\s+)?welds?{X}(?P<length_in>{NUMBER}){IN}",
                 "brace_slot_weld", (("count", "count"), ("size_in", "in"), ("length_in", "in")))
    _add_matches(fields, text, src, rf"\bslot reinforced:\s*(?P<count>{COUNT})\s+PL\s+(?P<thickness_in>{NUMBER}){X}(?P<width_in>{NUMBER}){X}(?P<length_in>{NUMBER}){IN}\s+(?P<material>{GRADE})\s+slot reinforcement",
                 "slot_reinforcement", (("count", "count"), ("thickness_in", "in"), ("width_in", "in"), ("length_in", "in"), ("material", "literal")))
    _add_matches(fields, text, src, rf"\beach welded with\s+(?P<size_in>{NUMBER}){IN}\s+E70\w*\s+longitudinal fillets?\s*\((?P<lines_per_plate>{COUNT})\s+lines? per plate\),\s*(?P<length_each_side_in>{NUMBER}){IN}\s+long on each side of the reduced section",
                 "reinforcement_fillet", (("size_in", "in"), ("lines_per_plate", "count"), ("length_each_side_in", "in")))
    _add_matches(fields, text, src, rf"\bslot width\s*(?:=|of)?\s*t(?:_?g)?\s*\+\s*(?P<clearance_in>{NUMBER}){IN}",
                 "slot", (("clearance_in", "in"),))
    for edge in ("column", "beam"):
        _add_matches(fields, text, src, rf"\b{edge}[- ]edge weld length\s+(?P<length_in>{NUMBER}){IN}",
                     "gusset_" + edge + "_weld", (("length_in", "in"),))
        _add_matches(fields, text, src, rf"\b{edge} edge\s+(?P<length_in>{NUMBER}){IN}\s*\((?P<size_in>{NUMBER}){IN}\s+fillets? both sides\)",
                     "gusset_" + edge + "_weld", (("length_in", "in"), ("size_in", "in")))
    _add_matches(fields, text, src, rf"\bbase[- ]plate weld length\s+(?P<length_in>{NUMBER}){IN}",
                 "gusset_base_weld", (("length_in", "in"),))
    _add_matches(fields, text, src, rf"\b(?P<depths>{NUMBER})t\s+linear clearance\b",
                 "brace_end_clearance", (("depths", "ratio"),))
    _add_matches(fields, text, src, rf"(?P<clearance_in>{NUMBER}){IN}\s+clear of (?:the )?column\b",
                 "gusset_column_clearance", (("clearance_in", "in"),))
    return fields


def _typed_fields(record):
    selections = record["data"]["bim_selection"]
    if not isinstance(selections, list) or not selections:
        raise ValueError("bim_selection must be a nonempty list in " + record["id"])
    fields = []
    for index, entry in enumerate(selections):
        if not isinstance(entry, dict) or any(not isinstance(entry.get(k), str) or not entry[k] for k in ("role", "field", "unit")):
            raise ValueError("Typed selection needs a role, field and unit in " + record["id"])
        value = entry.get("value")
        if type(value) not in (str, int, float, bool) or isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Typed selection needs a finite scalar in " + record["id"])
        if entry["unit"] in ("in", "ratio", "ksi", "kip", "degree") and type(value) not in (int, float):
            raise ValueError("Numerical selected fields require finite numbers in " + record["id"])
        if entry["unit"] == "count" and (type(value) is not int or value < 0):
            raise ValueError("Selected count must be a nonnegative integer in " + record["id"])
        if entry["unit"] in ("enum", "literal") and (not isinstance(value, str) or not value.strip()):
            raise ValueError("Selected specifications and variants require nonempty text in " + record["id"])
        applies = entry.get("applicability", {})
        if not isinstance(applies, dict) or set(applies) - {"joint", "end", "story", "connection_ids"}:
            raise ValueError("Unsupported typed selection applicability in " + record["id"])
        if ("end" in applies and applies["end"] not in ("upper", "lower")) or (
                "connection_ids" in applies and (not isinstance(applies["connection_ids"], list) or
                                                  not all(isinstance(v, str) for v in applies["connection_ids"]))):
            raise ValueError("Invalid typed selection applicability in " + record["id"])
        field = _field(entry["role"], entry["field"], value, entry["unit"],
                       record["source_pointer"] + f"/bim_selection/{index}/value", value, applicability=applies)
        if "attachment_roles" in entry:
            roles = entry["attachment_roles"]
            if not isinstance(roles, list) or len(roles) < 2 or not all(isinstance(v, str) and v.strip() for v in roles):
                raise ValueError("Attachment requires at least two named roles in " + record["id"])
            field["attachment_roles"] = list(roles)
        fields.append(field)
    return fields


def selection_record(record):
    typed = "bim_selection" in record["data"]
    fields = _typed_fields(record) if typed else _legacy_fields(record)
    result = {"source_pointer": record["source_pointer"], "record_sha256": digest_value(record["data"]),
              "basis": "typed_calc_fields" if typed else "literal_legacy_clauses", "fields": fields}
    text = record["data"].get("components")
    if not typed and isinstance(text, str):
        pointer = record["source_pointer"] + "/components"
        clauses = []
        for match in re.finditer(r"[^;|]+", text):
            if not match.group().strip():
                continue
            assigned = [i for i, f in enumerate(fields) if f["source"]["pointer"] == pointer and
                        (s := f["source"].get("span")) and s[0] < match.end() and s[1] > match.start()]
            clauses.append({"span": list(match.span()), "typed_field_indexes": assigned})
        result["legacy_components"] = {"pointer": pointer, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                                       "clauses": clauses, "coverage": "original text retained; typed indexes identify recognized selections"}
    result["variant_basis"] = ("selected_variant_field" if any(f["role"] == "connection" and f["field"] == "variant" for f in fields)
                               else "source_record_and_type_require_consumer_interpretation")
    return result


def applicability(field, connection):
    """Return selected, conditional or different for this physical occurrence."""
    scope = field["applicability"]
    if not scope:
        return "selected"
    if "connection_ids" in scope and connection["id"] not in scope["connection_ids"]:
        return "different"
    params = connection.get("parameters", {})
    for key in ("joint", "story"):
        if key in scope:
            if key not in params:
                return "conditional"
            if params[key] != scope[key]:
                return "different"
    if "end" in scope:
        if type(params.get("upper_end")) is not bool:
            return "conditional"
        if params["upper_end"] != (scope["end"] == "upper"):
            # A record can describe corner and apex ends, but restate only
            # the different apex support geometry. Do not invent its weld size.
            return "conditional" if params.get("joint") == "apex" else "different"
    return "selected"


def applications(connection, records):
    result = []
    for rid in connection.get("design_record_ids", []):
        if rid not in records:
            continue
        active, conditional = [], []
        for index, field in enumerate(records[rid]["fields"]):
            status = applicability(field, connection)
            if status == "selected":
                active.append(index)
            elif status == "conditional":
                conditional.append(index)
        result.append({"record_id": rid, "selected": active, "conditional": conditional})
    return result


def brace_receiver_roles(physical):
    """Locate a detail crop on its full physical member using actual bound axes."""
    from .bim_assemblies import finite, frame_errors, transformed
    members = {m["id"]: m for m in physical.get("members", [])
               if re.search(r"brace|truss_web|diagonal", m.get("kind", ""), re.I) or
               m.get("profile_id", "").upper().startswith("BRB")}
    templates = {a["id"]: a for a in physical.get("assemblies", [])}
    result = {}
    tol = 1e-5
    for connection in physical.get("connections", []):
        frame = connection.get("assembly_instance")
        if not frame or frame_errors(frame):
            continue
        roles = {}
        for receiver in templates.get(frame.get("template_id"), {}).get("receivers", []):
            rid = receiver.get("id")
            mid = frame.get("bindings", {}).get(rid)
            if mid not in members:
                continue
            member = members[mid]
            axis = member.get("axis", [])
            offsets = member.get("end_offsets", [])
            local = receiver.get("axis", [])
            if any(len(v) != 2 or not all(finite(p) for p in v) for v in (axis, offsets, local)):
                roles[rid] = {"role": "unresolved", "member_id": mid, "basis": "missing or malformed physical axes"}
                continue
            ends = [[p[k] + v[k] for k in range(3)] for p, v in zip(axis, offsets)]
            world = [transformed(p, frame) for p in local]
            point = connection.get("point", frame["origin"])
            workpoint_ends = [i for i in (0, 1) if math.dist(point, axis[i]) < tol]
            matches = [i for i in (0, 1) if min(math.dist(ends[i], p) for p in world) < tol]
            if len(workpoint_ends) == 1:
                role, indices, basis = "terminal", workpoint_ends, "located connection workpoint"
            elif len(matches) == 2:
                role, indices, basis = "whole_member", matches, "both offset physical endpoints"
            elif len(matches) == 1:
                role, indices, basis = "terminal", matches, "located offset physical endpoint"
            else:
                d = [b-a for a, b in zip(*ends)]
                length2 = sum(v*v for v in d)
                stations = [sum((p[k]-ends[0][k])*d[k] for k in range(3))/length2 for p in world] if length2 else []
                interior = len(stations) == 2 and all(tol < t < 1-tol and
                           math.dist(p, [ends[0][k] + t*d[k] for k in range(3)]) < tol for p, t in zip(world, stations))
                role, indices, basis = ("midspan", [], "interior receiver axis") if interior else ("unresolved", [], "receiver needs a located application")
            roles[rid] = {"role": role, "member_id": mid, "member_end_indices": indices, "basis": basis}
        if roles:
            result[connection["id"]] = roles
    return result

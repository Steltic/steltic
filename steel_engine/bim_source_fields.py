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
    "flat_bar_end_fillet": ["flat_bar_end", "gusset"],
    "flat_bar_crossing_fillet": ["crossing_flat_bar", "crossing_flat_bar"],
}


def digest_value(value):
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def native_content_digest(package):
    """Canonical SHA-256 of every native field before the additive HR handoff."""
    return digest_value({key: value for key, value in package.items() if key != "source_handoff"})


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
        ("double_plate_shear", r"\btwo plates\s+(?:PL\s*)?" + NUMBER + X + NUMBER + IN +
         r"[^;]{0,70}\bone (?:on )?each face of the beam web\b"),
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
    # "7 x 1 x 1 in" is a row-by-column grid and diameter. Match the entire
    # group once, so its last column cannot also be read as a one-bolt group.
    bolt_group = (rf"\b(?P<first>{COUNT}){X}(?:(?P<columns>\d+){X})?"
                  rf"(?P<diameter_in>{NUMBER}){IN}\s+(?P<material>{BOLT_GRADE})\s+bolts?\b")
    for match in re.finditer(bolt_group, text, re.I):
        first = number(match["first"])
        entries = [("diameter_in", number(match["diameter_in"]), "in"), ("material", match["material"], "literal")]
        if match["columns"]:
            columns = int(match["columns"])
            entries.extend([("rows", first, "count"), ("columns", columns, "count"), ("count", first * columns, "count")])
        else:
            entries.append(("count", first, "count"))
        fields.extend(_field("bolt", key, value, unit, src, match.group(), match.span(), _scope_at(text, match.start()))
                      for key, value, unit in entries)
    stated_group = (rf"\b(?P<a>\d+){X}(?P<b>\d+)\s*=\s*(?P<count>\d+) bolts?\s+"
                    rf"(?P<diameter_in>{NUMBER}){IN}\s+(?P<material>{BOLT_GRADE})")
    for match in re.finditer(stated_group, text, re.I):
        if int(match["a"]) * int(match["b"]) != int(match["count"]):
            raise ValueError("Conflicting bolt-grid factors and stated count at " + src)
        for key, unit in (("count", "count"), ("diameter_in", "in"), ("material", "literal")):
            fields.append(_field("bolt", key, match[key] if key == "material" else number(match[key]),
                                 unit, src, match.group(), match.span()))
    _add_matches(fields, text, src, rf"\bpitch and gauge (?P<pitch_in>{NUMBER}){IN}", "bolt", (("pitch_in", "in"),))
    _add_matches(fields, text, src, rf"\bpitch and gauge (?P<gauge_in>{NUMBER}){IN}", "bolt", (("gauge_in", "in"),))
    _add_matches(fields, text, src, rf"\bbolts?\s*\(\s*(?P<rows>\d+)\s+rows?\s+at\s+(?P<pitch_in>{NUMBER}){IN}",
                 "bolt", (("rows", "count"), ("pitch_in", "in")))
    _add_matches(fields, text, src, rf"\b{BOLT_GRADE}\s+bolts?\s+at\s+(?P<pitch_in>{NUMBER}){IN}",
                 "bolt", (("pitch_in", "in"),))
    _add_matches(fields, text, src, rf"\b{BOLT_GRADE}\s+bolts? in double shear,[^;]{{0,80}}?,\s*(?P<pitch_in>{NUMBER}){IN}\s+pitch",
                 "bolt", (("pitch_in", "in"),))
    _add_matches(fields, text, src, rf"\bsingle[- ]plate\s+(?:PL\s*)?(?P<thickness_in>{NUMBER})(?:\s*in\.?)?{X}(?P<depth_in>{NUMBER}){IN}\s+(?P<material>{GRADE})",
                 "shear_plate", (("thickness_in", "in"), ("depth_in", "in"), ("material", "literal")))
    _add_matches(fields, text, src, rf"\b(?P<count>two) plates\s+(?:PL\s*)?(?P<thickness_in>{NUMBER}){X}(?P<depth_in>{NUMBER}){IN}"
                 rf"\s+(?P<material>{GRADE})\s*\(one (?:on )?each face of the beam web\)", "shear_plate",
                 (("count", "count"), ("thickness_in", "in"), ("depth_in", "in"), ("material", "literal")))
    for axis in ("vertical", "horizontal"):
        _add_matches(fields, text, src, rf"\b{axis} edge distance\s+(?P<{axis}_edge_in>{NUMBER}){IN}",
                     "bolt", ((axis + "_edge_in", "in"),))
    _add_matches(fields, text, src, rf"\bbeam end set back\s+(?P<cutback_in>{NUMBER}){IN}\s+from the (?:support|column) face",
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


def _flat_bar_check_fields(record):
    """A legacy limit-state row can state a physical attachment in its own right."""
    fields = []
    terminal = (rf"^flat bar (?P<width_in>{NUMBER}){X}(?P<thickness_in>{NUMBER}) end connection,\s*"
                rf"(?P<count>{COUNT})\s+(?P<size_in>{NUMBER}){IN}\s+fillets?{X}(?P<length_in>{NUMBER}){IN}"
                r"\s+to the gusset \(welded, no bolts\)")
    crossing = (rf"^flat bars interconnected at the crossing\b[^(]*\((?P<size_in>{NUMBER}){IN}\s+fillets?,\s*"
                rf"(?P<count>{COUNT}){X}(?P<length_in>{NUMBER}){IN}\)")
    for index, check in enumerate(record["data"].get("checks", [])):
        text = check.get("limit_state") if isinstance(check, dict) else None
        if not isinstance(text, str):
            continue
        is_end = re.match(terminal, text, re.I)
        match = is_end or re.match(crossing, text, re.I)
        if not match:
            continue
        pointer = record["source_pointer"] + f"/checks/{index}/limit_state"
        scope = {"attachment": "flat_bar_end" if is_end else "flat_bar_crossing"}
        if is_end:
            scope["section_in"] = [number(match["width_in"]), number(match["thickness_in"])]
        for key, raw in match.groupdict().items():
            role = "flat_bar_end" if key in ("width_in", "thickness_in") else scope["attachment"] + "_fillet"
            fields.append(_field(role, key, number(raw), "count" if key == "count" else "in",
                                 pointer, match.group(), match.span(), scope))
    return fields


def _beam_end_groups(record, fields):
    text = record["data"].get("components", "")
    if not isinstance(text, str):
        return None
    head = re.match(r"collector / strut detail at every ([A-Z0-9./-]+) end\b.*? -- \d+ beams? \(", text, re.I)
    if not head:
        return None
    pointer = record["source_pointer"] + "/components"
    first_plate = re.search(r"\b(?:single plate|two plates)\s+(?:PL\s*)?[\d.]", text[head.end():], re.I)
    if not first_plate:
        return None
    interval = text[head.end():head.end() + first_plate.start()]
    group_rows = []
    for match in re.finditer(r"\bGrid ([A-Z0-9]+) \(([XY]) line\) levels? (\d+(?:-\d+)?(?:\s*,\s*\d+(?:-\d+)?)*)", interval, re.I):
        start, stop = (head.end() + s for s in match.span())
        spans = [[int(n) for n in token.strip().split("-")] for token in match[3].split(",") if token.strip()]
        group_rows.append({"grid": match[1], "direction": match[2], "levels": [n for s in spans for n in range(s[0], s[-1] + 1)],
                           "source": {"pointer": pointer, "span": [start, stop], "literal": text[start:stop]}})
    if not group_rows:
        return None
    other = re.search(r"\bThe other \d+ [A-Z0-9./-]+ beams? \(not on a collector line", text, re.I)
    for field in fields:
        src = field["source"]
        if field["role"] != "connection" and src["pointer"] == pointer and "span" in src:
            field["applicability"]["beam_group"] = "gravity" if other and src["span"][0] >= other.start() else "collector"
    # The equal-sign shorthand gives dimensions without assigning horizontal
    # and vertical directions. The original bolt check names lines and rows.
    for n, entry in enumerate(record["data"].get("checks", [])):
        literal = entry.get("limit_state", "") if isinstance(entry, dict) else ""
        match = re.match(r"bolt shear, critical bolt: (\d+) lines? x (\d+) rows? of ", literal, re.I)
        if match:
            for field_name, value in zip(("columns", "rows"), match.groups()):
                fields.append(_field("bolt", field_name, int(value), "count", record["source_pointer"] + f"/checks/{n}/limit_state",
                                     match.group(), match.span(), {"beam_group": "collector"}))
            break
    return {"section": head[1], "collector_lines": group_rows, "other_gravity_ends": bool(other)}


def selection_record(record):
    typed = "bim_selection" in record["data"]
    fields = _typed_fields(record) if typed else _legacy_fields(record) + _flat_bar_check_fields(record)
    result = {"source_pointer": record["source_pointer"], "record_sha256": digest_value(record["data"]),
              "basis": "typed_calc_fields" if typed else "literal_legacy_clauses", "fields": fields}
    data = record["data"]
    alternate = re.search(r"\bThe other ends of these beams have no gusset \(record ([A-Z0-9./-]+)\)",
                          str(data.get("design_basis", "")), re.I)
    if not typed and alternate:
        result["corner_alternative"] = {"other_end_record": "connections/" + alternate[1],
                                        "source": {"pointer": record["source_pointer"] + "/design_basis",
                                        "literal": alternate.group(), "span": list(alternate.span())}}
    beam_groups = None if typed else _beam_end_groups(record, fields)
    if beam_groups:
        result["beam_end_groups"] = beam_groups
    if isinstance(data.get("section"), str):
        result["member_section"] = {"value": data["section"], "pointer": record["source_pointer"] + "/section"}
    source_type = str(data.get("type", ""))
    if not typed and re.match(r"^STMF [XY] trusses: (?:verticals to the chords|special-segment flat-bar ends)", source_type, re.I):
        axis = "vertical" if "verticals to the chords" in source_type.lower() else "diagonal"
        match = re.search(r"\b(HSS[\dX./-]+) slotted for (?:a|an|the) ", str(data.get("components", "")), re.I)
        if match:
            scope = {"attachment": "slotted_hss", "member_axis": axis, "member_section": match[1].upper()}
            result["hss_attachment"] = {"applicability": scope, "source": {"pointer": record["source_pointer"] + "/components",
                                        "span": list(match.span()), "literal": match.group()}}
            for field in fields:
                if field["role"] in ("gusset", "slot", "brace_slot_weld") or field["value"] == "slotted_hss_gusset":
                    field["applicability"] = scope
    named_grid = re.match(r"([A-Z0-9./-]+) collector on Grid ([A-Z0-9]+) \(([XY]) frame\), level (\d+):"
                          r" end connection = the collector / strut detail of ([A-Z0-9/-]+)", str(data.get("components", "")), re.I)
    if not typed and (named_grid or re.match(r"^collector-to-braced-frame connection\s*\(single plate\b", source_type, re.I)):
        target = {"role": "collector_to_frame", "source": {"pointer": record["source_pointer"] + "/type", "literal": data["type"]}}
        if named_grid:
            target["specified_location"] = {"section": named_grid[1], "grid": named_grid[2], "direction": named_grid[3],
                                            "level": int(named_grid[4]), "referenced_end_record": "connections/" + named_grid[5],
                                            "source": {"pointer": record["source_pointer"] + "/components",
                                            "literal": named_grid.group(), "span": list(named_grid.span())}}
        note = (data.get("demand") or {}).get("P_basis_note", "")
        match = re.search(r"\bgoverning (?:\w+ )?line ([XY])([A-Z0-9]+), level (\d+), ([XY]) direction\b", note, re.I)
        if match:
            target["governing_location"] = {"line": match[1] + match[2], "grid": match[2], "level": int(match[3]),
                                            "direction": match[4].upper(), "source": {"pointer": record["source_pointer"] +
                                            "/demand/P_basis_note", "span": list(match.span()), "literal": match.group()}}
        result["application_target"] = target
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


def applicability(field, connection, location=None):
    """Return selected, conditional or different for this physical occurrence."""
    scope = field["applicability"]
    if not scope:
        return "selected"
    if "connection_ids" in scope and connection["id"] not in scope["connection_ids"]:
        return "different"
    if "attachment" in scope:
        if location is None:
            return "conditional"
        if scope["attachment"] == "slotted_hss":
            ends = [m for m in location["end_members"].values() if m.get("web_axis") == scope["member_axis"] and
                    str(m["section"]).upper().startswith("HSS")]
            if not ends:
                return "different"
            return "selected" if any(m["section"] == scope["member_section"] for m in ends) else "conditional"
        if scope["attachment"] == "flat_bar_crossing":
            return "selected" if location["flat_bar_crossing"] else "different"
        if not location["flat_bar_ends"]:
            return "different"
        # A checked 1.75-in bar cannot change the section of a bound 1.25-in
        # bar. The latter's own connection selection must establish its weld.
        return "selected" if scope["section_in"] in location["flat_bar_ends"] else "conditional"
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


def application_location(connection, members, profiles, reference=None):
    """Locate a scoped source detail using independent member axes and actual datums."""
    point = connection.get("point", (connection.get("assembly_instance") or {}).get("origin"))
    if not isinstance(point, (list, tuple)) or len(point) != 3:
        return None
    result = {"point_in": list(point), "end_members": {}, "flat_bar_ends": [], "flat_bar_crossing": False}
    crossing_axes = []
    for mid in connection.get("member_ids", []):
        member = members.get(mid, {})
        axis = member.get("axis", [])
        if len(axis) != 2 or any(len(end) != 3 for end in axis):
            continue
        profile_id = member.get("profile_id")
        profile = profiles.get(profile_id, {})
        endpoint = min(math.dist(point, end) for end in axis) < 1e-5
        if endpoint:
            result["end_members"][mid] = {"section": profile_id, "kind": member.get("kind")}
            dx, dy = abs(axis[1][0] - axis[0][0]), abs(axis[1][1] - axis[0][1])
            if dx > 1e-5 and dy < 1e-5:
                result["end_members"][mid]["direction"] = "X"
            elif dy > 1e-5 and dx < 1e-5:
                result["end_members"][mid]["direction"] = "Y"
            if member.get("kind") == "truss_web":
                horizontal = math.dist(axis[0][:2], axis[1][:2])
                result["end_members"][mid]["web_axis"] = "vertical" if horizontal < 1e-5 else "diagonal"
        if profile.get("family") != "flat":
            continue
        if endpoint:
            dims = profile.get("dimensions", {})
            result["flat_bar_ends"].append([dims.get("width"), dims.get("thickness")])
        elif abs(math.dist(point, axis[0]) + math.dist(point, axis[1]) - math.dist(*axis)) < 1e-6:
            crossing_axes.append([b - a for a, b in zip(*axis)])
    for i, a in enumerate(crossing_axes):
        for b in crossing_axes[i + 1:]:
            if any(abs(a[j] * b[(j + 1) % 3] - b[j] * a[(j + 1) % 3]) > 1e-6 for j in range(3)):
                result["flat_bar_crossing"] = True
    if reference:
        result["grids"] = []
        for grid in reference.get("grids", []):
            path = grid.get("points_in", [])
            if len(path) == 2 and abs(math.dist(point[:2], path[0][:2]) + math.dist(point[:2], path[1][:2]) -
                                       math.dist(path[0][:2], path[1][:2])) < 1e-6:
                result["grids"].append(grid["label"])
        result["levels"] = [x["level"] for x in reference.get("levels", [])
                            if isinstance(x.get("elevation_in"), (float, int)) and abs(x["elevation_in"] - point[2]) < 1e-5]
    return result


def applications(connection, records, location=None):
    result = []
    alternatives = {}
    for rid in connection.get("design_record_ids", []):
        alternative = records.get(rid, {}).get("corner_alternative")
        if not alternative:
            continue
        base = alternative["other_end_record"]
        section = records.get(base, {}).get("member_section", {}).get("value")
        members = {mid for mid, m in (location or {}).get("end_members", {}).items() if m["section"] == section}
        corner = {cid: mids for cid, mids in (location or {}).get("corner_members", {}).items() if members.intersection(mids)}
        if not members:
            status = "conditional"
        else:
            status = "gusset_end" if corner else "other_end"
        binding = {"located_end": status, "corner_connection_ids": sorted(corner), "bound_member_ids": sorted(members),
                   "gusset_record": rid, "other_end_record": base}
        alternatives[rid] = (binding, "gusset_end")
        alternatives[base] = (binding, "other_end")
    for rid in connection.get("design_record_ids", []):
        if rid not in records:
            continue
        record = records[rid]
        target = record.get("application_target", {})
        binding = None
        group = None
        if groups := record.get("beam_end_groups"):
            site = location or {}
            directions = {m["direction"] for m in site.get("end_members", {}).values()
                          if m["section"] == groups["section"] and m.get("direction")}
            if directions and site.get("grids") and site.get("levels"):
                matched = any(line["direction"] in directions and line["grid"] in site["grids"] and
                              set(line["levels"]).intersection(site["levels"]) for line in groups["collector_lines"])
                group = "collector" if matched else "gravity" if groups["other_gravity_ends"] else None
        if target.get("role") == "collector_to_frame":
            end_members = (location or {}).get("end_members", {})
            local = {key: [mid for mid, member in end_members.items() if member["section"] == source["member_section"]["value"]]
                     for key in connection.get("design_record_ids", []) if (source := records.get(key, {})).get("member_section")}
            local = {key: mids for key, mids in local.items() if mids}
            binding = {"role": target["role"], "local_beam_end_records": local, "point_in": (location or {}).get("point_in"),
                       "grids": (location or {}).get("grids", []), "levels": (location or {}).get("levels", [])}
            if target.get("governing_location"):
                g = target["governing_location"]
                binding["at_governing_grid_and_level"] = g["grid"] in binding["grids"] and g["level"] in binding["levels"]
            # A collector force in the same member does not install a second
            # bolt group onto a section-specific beam-end assembly. Its
            # separate frame interface remains visible at this location.
            binding["status"] = "related_interface" if local else "located_interface" if (
                connection.get("parameters", {}).get("collector") is True or
                any(m["kind"] == "collector" for m in end_members.values())) else "unlocated_interface"
            if specified := target.get("specified_location"):
                match = (specified["grid"] in binding["grids"] and specified["level"] in binding["levels"] and
                         any(m["section"] == specified["section"] and m.get("direction") == specified["direction"]
                             for m in end_members.values()))
                binding["at_specified_member_grid_and_level"] = match
                if not match:
                    binding["status"] = "different_location" if location and binding["levels"] else "unlocated_interface"
        active, conditional = [], []
        for index, field in enumerate(record["fields"]):
            status = applicability(field, connection, location)
            if "beam_group" in field["applicability"]:
                status = "conditional" if group is None else "selected" if group == field["applicability"]["beam_group"] else "different"
            if rid in alternatives and status == "selected":
                place, applies_at = alternatives[rid]
                if place["located_end"] != applies_at:
                    status = "conditional" if place["located_end"] == "conditional" else "different"
            if binding and binding["status"] != "located_interface" and status == "selected":
                status = "conditional"
            if status == "selected":
                active.append(index)
            elif status == "conditional":
                conditional.append(index)
        entry = {"record_id": rid, "selected": active, "conditional": conditional}
        if binding:
            entry["interface_application"] = binding
        if groups:
            entry["beam_end_group"] = {"selected": group, "grids": (location or {}).get("grids", []),
                                       "levels": (location or {}).get("levels", [])}
        if rid in alternatives:
            entry["alternate_end"] = alternatives[rid][0]
        if record.get("hss_attachment") and location:
            scope = record["hss_attachment"]["applicability"]
            entry["hss_members"] = {mid: ("selected" if m["section"] == scope["member_section"] else "conditional")
                                    for mid, m in location["end_members"].items() if m.get("web_axis") == scope["member_axis"]
                                    and str(m["section"]).upper().startswith("HSS")}
        result.append(entry)
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

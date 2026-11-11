"""Source selections used by the HR-to-openBIM handoff.

New calculation records can supply typed selections. For older records the
reader recognizes explicit component clauses, records the literal and its
character range, and retains the complete original record. A later reference
to a pin in a design check does not select a pin connection.
"""
from __future__ import annotations

import hashlib
import csv
import json
import math
import re
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from .bim_collector_applications import receiver_application, receiver_target
from .bim_brb_selections import checked_bolt_pattern


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
    heading = re.match(r"(?:base corner|corner|apex) gusset\b(?: at the (?:column base|base plate))?", text[position:], re.I)
    if heading:
        section += heading.group()
    end = re.findall(r"(?:^|;)\s*(lower|upper)\s+end\s*:", section, re.I)
    end = end[-1].lower() if end else None
    marker = r"(?:^|;)\s*(?:(?:lower|upper) end:\s*)?(?:at (?:the )?)?(base corner|corner|apex) gusset\b"
    markers = list(re.finditer(marker, section, re.I))
    if markers:
        label = markers[-1][1].lower()
        if label == "base corner" or re.match(r"\s+at the (?:column base|base plate)\b", section[markers[-1].end():], re.I):
            label = "base"
        return _scope(joint=label, end=end)
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
        ("beam_pin_hinge", r"^"+NUMBER+IN+r"\s+dia\.?\s+pin \([^)]*\) in double shear; knife plate"),
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

    _add_matches(fields,text,src,rf"knife plate (?P<thickness_in>{NUMBER}){IN} at the hole, width (?P<width_in>{NUMBER}){IN}, extension (?P<extension_in>{NUMBER}){IN}",
                 "knife_plate",(("thickness_in","in"),("width_in","in"),("extension_in","in")))
    _add_matches(fields,text,src,rf"(?P<count>{COUNT}) clevis plates (?P<thickness_in>{NUMBER}){IN} welded to the beam end", "clevis_plate",
                 (("count","count"),("thickness_in","in")))
    _add_matches(fields,text,src,rf"clevis plates {NUMBER}{IN} welded to the beam end \((?P<size_in>{NUMBER}){IN} fillets\)",
                 "clevis_beam_fillet",(("size_in","in"),))
    _add_matches(fields,text,src,rf"(?P<gap_in>{NUMBER}){IN} clear gap at the flanges","beam_flange",(("gap_in","in"),))
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
    labelled_grid = (rf"\b(?P<columns>\d+)\s+(?:columns?|column\(s\)|lines?){X}(?P<rows>\d+)\s+rows?\s+of\s+"
                     rf"(?P<diameter_in>{NUMBER}){IN}\s+(?P<material>{BOLT_GRADE})\s+bolts?\b")
    for match in re.finditer(labelled_grid, text, re.I):
        for key, unit in (("columns", "count"), ("rows", "count"), ("diameter_in", "in"), ("material", "literal")):
            fields.append(_field("bolt", key, match[key] if key == "material" else number(match[key]),
                                 unit, src, match.group(), match.span()))
        fields.append(_field("bolt", "count", int(match["rows"])*int(match["columns"]), "count", src, match.group(), match.span()))
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
    _add_matches(fields, text, src, rf"\bbase plate\s+PL\s*(?P<thickness_in>{NUMBER}){X}(?P<width_in>{NUMBER}){X}(?P<length_in>{NUMBER}){IN}(?:\s+\(?(?P<material>{GRADE})\)?)?",
                 "base_plate", (("thickness_in", "in"), ("width_in", "in"), ("length_in", "in"), ("material", "literal")))
    _add_matches(fields, text, src, rf"\b(?P<count>{COUNT}){X}(?P<diameter_in>{NUMBER}){IN}\s+(?P<material>(?:ASTM\s+)?F1554[- ](?:Gr(?:ade)?\.?\s*)?(?:36|55|105))\s+anchor rods?",
                 "anchor", (("count", "count"), ("diameter_in", "in"), ("material", "literal")))
    _add_matches(fields, text, src, rf"\b(?P<count>{COUNT}){X}(?P<diameter_in>{NUMBER}){IN}\s+"
                 rf"(?P<material>(?:ASTM\s+)?A193\s+Gr(?:ade)?\.?\s*B7)\s*(?:\([^)]{{0,220}}\)\s*)?anchor rods?",
                 "anchor", (("count", "count"), ("diameter_in", "in"), ("material", "literal")))
    _add_matches(fields, text, src, rf"\b(?P<count>{COUNT})\s+anchor rods?\s+(?P<diameter_in>{NUMBER}){IN}\s+"
                 rf"(?P<material>(?:ASTM\s+)?F1554[- ](?:36|55|105))\b",
                 "anchor", (("count", "count"), ("diameter_in", "in"), ("material", "literal")))
    group_b_layout = re.search(rf"two outside each flange (?P<flange_offset_in>{NUMBER}){IN} from the flange face at "
                               rf"(?P<spacing_in>{NUMBER}){IN}, (?P<edge_flange_normal_in>{NUMBER}){IN} from the plate edges", text, re.I)
    if group_b_layout:
        for key, literal in group_b_layout.groupdict().items():
            fields.append(_field("anchor_pattern", key, number(literal), "in", src,
                                 group_b_layout.group(), group_b_layout.span()))
        # The centered transverse pitch fixes the other installed edge.
        # The original pinned_base producer explicitly sets g on the
        # flange-normal axis (x_r = (N - d)/2 - g).
        width = next((f["value"] for f in fields if f["role"] == "base_plate" and f["field"] == "width_in"), None)
        if width:
            edge = (width - number(group_b_layout["spacing_in"])) / 2
            fields.append(_field("anchor_pattern", "edge_along_flange_in", edge, "in", src,
                                 group_b_layout.group(), group_b_layout.span()))
        _add_matches(fields, text, src, rf"cruciform shear lug PL (?P<thickness_in>{NUMBER}){IN} \(legs (?P<width_in>{NUMBER}){IN} wide, "
                     rf"(?P<embedment_in>{NUMBER}){IN} below the grout; (?P<material>{GRADE})\)", "cruciform_shear_lug",
                     (("thickness_in", "in"), ("width_in", "in"), ("embedment_in", "in"), ("material", "literal")))
        for index, check in enumerate(data.get("checks", [])):
            clause = check.get("limit_state", "") if isinstance(check, dict) else ""
            pointer = ptr + f"/checks/{index}/limit_state"
            _add_matches(fields, clause, pointer, rf"half the (?P<width_in>{NUMBER}){IN} plate washer", "plate_washer", (("width_in", "in"),))
            _add_matches(fields, clause, pointer, rf"\((?P<diameter_in>{NUMBER}){IN} hole, AISC Manual Table 14-2\)", "anchor_hole", (("diameter_in", "in"),))
    _add_matches(fields, text, src, rf"\bone row outside the flanges at\s+(?P<spacing_in>{NUMBER}){IN}",
                 "anchor_pattern", (("spacing_in", "in"),))
    # An edge described as "or more" is a minimum; directional supplied
    # edges and pitches stay independently checkable at each real base.
    _add_matches(fields, text, src, rf"\brods\s+(?P<spacing_in>{NUMBER}){IN}\s+apart\b",
                 "anchor_pattern", (("spacing_in", "in"),))
    _add_matches(fields, text, src, rf"\ba1\s*=\s*(?P<flange_offset_in>{NUMBER}){IN}\s+from the flange face",
                 "anchor_pattern", (("flange_offset_in", "in"),))
    _add_matches(fields, text, src, rf"(?P<minimum_edge_in>{NUMBER}){IN}\s+or more from the plate edges",
                 "anchor_pattern", (("minimum_edge_in", "in"),))
    _add_matches(fields, text, src, rf"(?P<diameter_in>{NUMBER}){IN}\s+holes\s*\(AISC Manual Table 14-2\)\s+with (?:top )?plate washers",
                 "anchor_hole", (("diameter_in", "in"),))
    _add_matches(fields, text, src, rf"\brod holes\s+(?P<diameter_in>{NUMBER}){IN}\b",
                 "anchor_hole", (("diameter_in", "in"),))
    _add_matches(fields, text, src, rf"\bwasher holes\s+(?P<diameter_in>{NUMBER}){IN}\b",
                 "washer_hole", (("diameter_in", "in"),))
    if not re.search(r"\bcruciform shear lug\b|\bsecond (?:shear[- ]lug )?arm\b", text, re.I):
        _add_matches(fields, text, src, rf"(?:^|;\s*)shear lug\s+(?P<material>{GRADE})\s+PL\s+(?P<thickness_in>{NUMBER})"
                     rf"{X}(?P<width_in>{NUMBER}){IN},\s*(?P<embedment_in>{NUMBER}){IN}\s+embedded below the grout",
                     "shear_lug", (("thickness_in", "in"), ("width_in", "in"), ("embedment_in", "in"), ("material", "literal")))
        normal = re.search(r"\bsingle shear lug has a face normal along world ([XYZ])\b", text)
        if normal:
            vector = [int(axis == normal[1]) for axis in "XYZ"]
            fields.append(_field("shear_lug", "face_normal", vector, "unit_vector", src, normal.group(), normal.span()))
    if not group_b_layout:
        _add_matches(fields, text, src, rf"(?P<edge_in>{NUMBER}){IN}\s+from the plate edges\b",
                     "anchor_pattern", (("edge_in", "in"),))
    _add_matches(fields, text, src, rf"\bthe first row (?P<flange_offset_in>{NUMBER}){IN} from the flange face,\s*"
                 rf"rods at (?P<spacing_in>{NUMBER}){IN} centres", "anchor_pattern", (("flange_offset_in", "in"), ("spacing_in", "in")))
    _add_matches(fields, text, src, rf"\bprovided rod-centre edge distances (?P<edge_flange_normal_in>{NUMBER}){IN} "
                 rf"normal to the column flanges and (?P<edge_along_flange_in>{NUMBER}){IN} along the flanges; "
                 rf"minimum required rod-centre edge (?P<minimum_edge_in>{NUMBER}){IN}", "anchor_pattern",
                 (("edge_flange_normal_in", "in"), ("edge_along_flange_in", "in"), ("minimum_edge_in", "in")))
    _add_matches(fields, text, src, rf"\bon\s+(?P<thickness_in>{NUMBER}){IN}\s+(?P<material>(?:cementitious\s+)?non[- ]shrink grout)",
                 "grout", (("thickness_in", "in"), ("material", "literal")))
    _recover_layout_precision(fields, text, data)
    _selected_gusset_fillet_checks(record, fields)
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
    _add_matches(fields, text, src, rf"\bWhitmore width\s+(?P<whitmore_effective_width_in>{NUMBER}){IN}",
                 "gusset", (("whitmore_effective_width_in", "in"),))
    for field in fields:
        if field["field"] == "whitmore_effective_width_in":
            field["evidence_kind"] = "effective_section_used_in_source_check"
            field["check_sources"] = []
            for i, check in enumerate(data.get("checks", [])):
                clause = check.get("limit_state", "") if isinstance(check, dict) else ""
                match = re.search(rf"\bgusset Whitmore yielding \(Lw = ({NUMBER}){IN}", clause, re.I)
                if match and number(match[1]) == field["value"]:
                    field["check_sources"].append({"pointer": ptr + f"/checks/{i}/limit_state",
                                                   "literal": match.group(), "span": list(match.span())})
        if field["role"] == "brace_end_clearance":
            # A record-wide design-basis requirement applies to both brace
            # ends even when its component description starts with one end.
            basis = str(data.get("design_basis", ""))
            match = re.search(rf"\b({NUMBER})t linear fold-line clearance\b", basis, re.I)
            if match and number(match[1]) == field["value"] and not text.lower().startswith("x-brace crossing:"):
                field["applicability"] = {}
                field["application_source"] = {"pointer": ptr + "/design_basis", "span": list(match.span()),
                                               "literal": match.group(), "scope": "brace buckling accommodation"}
    _add_matches(fields, text, src, rf"(?P<clearance_in>{NUMBER}){IN}\s+clear of (?:the )?column\b",
                 "gusset_column_clearance", (("clearance_in", "in"),))
    return fields


@lru_cache(maxsize=1)
def _catalog_flange_widths():
    with Path(__file__).with_name("aisc_shapes.csv").open(encoding="utf-8-sig", newline="") as stream:
        return {r["AISC_Manual_Label"]: float(r["bf"]) for r in csv.DictReader(stream) if r.get("Type") == "W"}


def _recover_layout_precision(fields, text, data):
    """Recover the original Group A rod layout when its printed branch and values agree."""
    if ("provided rod-centre edge distances" in text
            or "4 da =" not in text or "AISC 360-22 Table J3.4 / J3.5 distance for the hole" not in text):
        return
    layout = re.search(r"rod layout: [0-9]+ rods outside each flange in one row of ([0-9]+) ", text)
    by_key = {(f["role"], f["field"]): f for f in fields}
    needed = [("base_plate", "width_in"), ("anchor", "diameter_in"), ("anchor_hole", "diameter_in"),
              ("anchor_pattern", "spacing_in"), ("anchor_pattern", "minimum_edge_in")]
    if not layout or int(layout[1]) < 2 or any(k not in by_key for k in needed):
        return
    width, rod, hole, printed_pitch, printed_edge = (by_key[k]["value"] for k in needed)
    edge = max(1.75 * rod, hole / 2 + 1.5)
    pitch = (width - 2 * edge) / (int(layout[1]) - 1)
    if "no net uplift at this base" not in text:
        # With uplift the original producer limits the outer rods to the
        # 45-degree load spread and keeps the washer clear of the gusset.
        offset = by_key.get(("anchor_pattern", "flange_offset_in"))
        sec = re.search(r"-(W[\d.]+X[\d.]+)$", str(data.get("id", "")), re.I)
        if ("anchor reinforcement As >=" not in text or not offset or not sec
                or (bf := _catalog_flange_widths().get(sec[1])) is None):
            return
        nr = int(layout[1])
        pitch = max(4 * rod, 2 * (1.5 + rod) if nr % 2 == 0 else 0,
                    min(pitch, (bf + 2 * offset["value"]) / (nr - 1)))
    if float(f"{pitch:.1f}") != printed_pitch or float(f"{edge:.2f}") != printed_edge:
        return
    method = {"producer": "stk_ext/groupA.py:_rod_geometry and base_dg1",
              "producer_sha256": "7aa5a16d8319cea7689eb585d74e9ec3b38517472f2cd0f7359dacdb72092d82",
              "width_in": width, "rod_diameter_in": rod, "hole_in": hole, "rods_per_row": int(layout[1])}
    for key, exact, digits in (("spacing_in", pitch, 1), ("minimum_edge_in", edge, 2)):
        field = by_key["anchor_pattern", key]
        field["printed_value"], field["printed_decimal_places"] = field["value"], digits
        field["value"] = exact
        field["derivation"] = method
        field["evidence_kind"] = "recovered_original_layout_precision"


def _selected_gusset_fillet_checks(record, fields):
    """A limit-state clause may specify the actual two face welds independently of end hardware."""
    for i, check in enumerate(record["data"].get("checks", [])):
        label = check.get("limit_state", "") if isinstance(check, dict) else ""
        match = re.match(rf"(?:(lower|upper) end:\s*)?(?:base |corner |apex )?gusset-to-(column|beam|base-plate)(?: flange)? "
                         rf"welds?,\s*(two) ({NUMBER}){IN} (?:E70\w* )?fillets?\b(?:\s*x\s*({NUMBER}){IN})?", label, re.I)
        if not match:
            continue
        end, host, _, size, run = match.groups()
        target = "base" if host.lower() == "base-plate" else host.lower()
        role = "gusset_" + target + "_weld"
        # The same source can carry general corner and separate base or apex details.
        # Its check prefix plus actual receiver identifies the application.
        literal = match.group().lower()
        joint = ("base" if "base gusset" in literal or target == "base" else
                 "apex" if "apex gusset" in literal else "corner" if not end or target == "column" else None)
        scope = _scope(joint=joint, end=end.lower() if end else None)
        for key, val, unit in (("faces", 2, "count"), ("size_in", number(size), "in"), ("length_in", number(run) if run else None, "in")):
            if val is not None:
                fields.append(_field(role, key, val, unit, record["source_pointer"] + f"/checks/{i}/limit_state",
                                     match.group(), match.span(), scope))


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


def _support_groups(record, fields):
    text = record["data"].get("components", "")
    if not isinstance(text, str) or not text.lower().startswith("ends on a column flange:"):
        return None
    groups = [{"side": "flange", "start": 0}]
    for match in re.finditer(r"\bweb-side ends on the (W[\dX.]+) \((Grid [^:]+?)\):", text, re.I):
        locations = [{"grids": [g[1], g[2]], "level": int(g[3])}
                     for g in re.finditer(r"Grid ([A-Z0-9]+)/([A-Z0-9]+) \(Level (\d+)\)", match[2], re.I)]
        groups.append({"side": "web", "section": match[1], "locations": locations, "start": match.start(),
                       "source": {"pointer": record["source_pointer"] + "/components", "literal": match.group(), "span": list(match.span())}})
    for field in fields:
        src = field["source"]
        if field["role"] != "connection" and src["pointer"].endswith("/components") and "span" in src:
            group = max(i for i, g in enumerate(groups) if g["start"] <= src["span"][0])
            field["applicability"]["support_group"] = group
    return groups


def _brace_support(record, fields):
    data = record["data"]
    components = data.get("components")
    if not isinstance(components, str):
        return None
    label = data.get("type", "")
    ptr = record["source_pointer"] + "/components"
    main = re.search(r"base corner gusset to the column \((BOX[\d.X]+)\) at the pile cap", label, re.I)
    local = re.match(r"OCBF base gusset at the lower end", label, re.I)
    if not main and not local:
        return None
    optional = r"\(?\s*"
    closing = r"\s*\)?"
    for role, pattern, keys in (
        ("gusset", rf"\bgusset\s+(?P<thickness_in>{NUMBER}){IN},\s*column edge", ("thickness_in",)),
        ("gusset_column_weld", rf"\bcolumn edge\s+{optional}(?P<length_in>{NUMBER}){IN}[^;]{{0,4}}CJP groove weld", ("length_in",)),
        ("gusset_column_weld", rf"\bcolumn edge\s+(?P<length_in>{NUMBER}){IN}\s+on the column (?:flange|web)", ("length_in",)),
        ("gusset_base_weld", rf"\bbase edge\s+(?P<length_in>{NUMBER}){IN}\s+on the base plate", ("length_in",)),
        ("gusset_collar_weld", rf"\bbottom edge\s+{optional}(?P<length_in>{NUMBER}){IN}{closing}\s+welded to a PL", ("length_in",)),
        ("collar_plate", rf"\bwelded to a PL\s+{optional}(?P<thickness_in>{NUMBER}){IN}{closing}\s+collar plate", ("thickness_in",)),
        ("collar_column_weld", rf"\bLw\s*=\s*{optional}(?P<length_in>{NUMBER}){IN}", ("length_in",)),
        ("collar_plate", rf"\b(?P<reinforcement_gap_in>{NUMBER}){IN}\s+between the plates, the gusset and its welds", ("reinforcement_gap_in",)),
        ("gusset_foundation_clearance", rf"\bbottom edge\s+{optional}(?P<clearance_in>{NUMBER}){IN}{closing}\s+clear of the cap face", ("clearance_in",)),
    ):
        _add_matches(fields, components, ptr, pattern, role, [(key, "in") for key in keys])
    for match in re.finditer(rf"\bcolumn edge\s+{optional}{NUMBER}{IN}\s*[, (]+(?P<weld_type>CJP) groove weld", components, re.I):
        fields.append(_field("gusset_column_weld", "weld_type", "CJP", "enum", ptr, match.group(), match.span()))
    for role, marker in (("collar_column_weld", r"CJP-welded to the \(?[\d.]+ in\)? box wall"),
                         ("gusset_foundation_clearance", r"no foundation interface")):
        for match in re.finditer(marker, components, re.I):
            key, value = ("weld_type", "CJP") if role == "collar_column_weld" else ("weld_to_foundation", False)
            fields.append(_field(role, key, value, "enum" if value else "bool", ptr, match.group(), match.span()))
    for role in ("gusset_column_weld", "gusset_base_weld"):
        _add_matches(fields, components, ptr, rf"(?P<size_in>{NUMBER}){IN}\s+E70\w* fillets both faces on both edges",
                     role, (("size_in", "in"),))
    both = re.search(r"E70\w* fillets both faces on both edges",components,re.I)
    if both:
        for role in ("gusset_column_weld", "gusset_base_weld"):
            fields.append(_field(role,"faces",2,"count",ptr,both.group(),both.span()))
    if main:
        groups = [(0, main[1].upper())]
        groups.extend((m.start(), m[1].upper()) for m in re.finditer(r"\bat (BOX[\d.X]+): gusset", components, re.I))
        for field in fields:
            src = field["source"]
            if src["pointer"] == ptr and "span" in src and field["role"] != "connection":
                column = next(v for start, v in reversed(groups) if start <= src["span"][0])
                field["applicability"] = {"joint": "base", "column_section": column}
        # Some checked geometry states a longer installed weld for the same
        # receiver. Preserve the design-check minimum separately from it.
        longer = re.search(rf"at (BOX[\d.X]+) the reinforcing plates are set ({NUMBER}){IN} apart.*?weld there is ({NUMBER}){IN} longer", components, re.I)
        if longer:
            column = longer[1].upper()
            scoped = {"joint": "base", "column_section": column}
            for f in list(fields):
                if f["role"] == "collar_column_weld" and f["field"] == "length_in" and f["applicability"] == scoped:
                    f["field"] = "checked_minimum_length_in"
                    selected = _field("collar_column_weld", "length_in", f["value"] + number(longer[3]),
                                      "in", ptr, longer.group(), longer.span(), scoped)
                    selected["additional_source"] = f["source"]
                    fields.append(selected)
                if f["role"] == "collar_plate" and f["field"] == "reinforcement_gap_in" and f["applicability"] == scoped:
                    f["field"] = "checked_minimum_gap_in"
                    fields.append(_field("collar_plate", "reinforcement_gap_in", number(longer[2]),
                                         "in", ptr, longer.group(), longer.span(), scoped))
        return {"column_sections": [v for _, v in groups]}
    web = bool(re.search(r"column edge [^;]{0,80} on the column web", components, re.I))
    side = "web" if web else "flange"
    for field in fields:
        if field["role"] != "connection":
            field["applicability"].update(joint="base", column_face=side)
    return {"scope":"located brace base","column_face":side}


def _compound_receivers(record, fields):
    data = record["data"]
    text = data.get("components")
    if not isinstance(text, str):
        return None
    ptr = record["source_pointer"] + "/components"
    prefix = re.match(r"top:\s*", text, re.I)
    base = re.search(r";\s*base:\s*", text, re.I)
    if prefix and base:
        for f in fields:
            if f["source"]["pointer"] == ptr and "span" in f["source"]:
                f["applicability"]["post_end"] = "lower" if f["source"]["span"][0] >= base.start() else "upper"
        return "post_end"
    if not text.lower().startswith("x-brace crossing:"):
        return None
    split = re.search(r"\bthe interrupted HSS[\dX./-]+ in two halves:", text, re.I)
    if not split:
        return None
    for f in fields:
        src = f["source"]
        if src["pointer"] != ptr or "span" not in src or f["role"] in ("connection", "gusset"):
            continue
        receiver = "split" if src["span"][0] >= split.start() else "continuous"
        f["applicability"]["crossing_receiver"] = receiver
        if f["role"] in ("brace_slot_weld", "reinforcement_fillet", "slot_reinforcement"):
            f["role"] = receiver + "_" + f["role"]
    for i, check in enumerate(data.get("checks", [])):
        label = check.get("limit_state", "") if isinstance(check, dict) else ""
        match = re.match(rf"slot welds, gusset to the continuous diagonal: slot fillets ({NUMBER}){IN} on the (\w+) slot edges, ({NUMBER}){IN} long", label, re.I)
        if match:
            for name, val, unit in (("size_in", match[1], "in"), ("count", match[2], "count"), ("length_in", match[3], "in")):
                fields.append(_field("continuous_brace_slot_weld", name, number(val), unit,
                    record["source_pointer"] + f"/checks/{i}/limit_state", match.group(), match.span(),
                    {"joint": "crossing", "crossing_receiver": "continuous"}))
    return "crossing"


def _transfer_receivers(record, fields):
    data = record["data"]
    text = data.get("components", "")
    label = str(data.get("type", ""))
    if not isinstance(text, str):
        return None
    flange = re.match(r"transfer truss (chord|diagonal) joints", label, re.I)
    support = re.match(r"transfer truss support on the Grid (\w+) / (\w+) columns at Level (\d+)", label, re.I)
    if not flange and not support:
        return None
    key = "transfer_" + (flange[1].lower() if flange else "support")
    selected = re.match(rf"double gusset plates (?P<count>{COUNT}) PL (?P<thickness>{NUMBER}){IN}", text, re.I)
    section = re.search(r"flange tips of the (W[\dX.]+)|welded to the (W[\dX.]+) flange tips", text, re.I)
    if not selected or not section:
        return None
    member_section = section[1] or section[2]
    scope = {"transfer_receiver": key}
    pointer = record["source_pointer"] + "/components"
    plate = key + "_gusset"
    for name, value, unit in (("count",number(selected["count"]),"count"),("thickness_in",number(selected["thickness"]),"in")):
        fields.append(_field(plate,name,value,unit,pointer,selected.group(),selected.span(),scope))
    grade = re.search(r"\((A572 Gr 50)\)",text,re.I)
    if grade:
        fields.append(_field(plate,"material",grade[1],"literal",pointer,grade.group(),grade.span(),scope))
    width = re.search(rf"Whitmore width ({NUMBER}){IN}",text,re.I)
    if width:
        f = _field(plate,"whitmore_effective_width_in",number(width[1]),"in",pointer,width.group(),width.span(),scope)
        f["evidence_kind"] = "effective_section_used_in_source_check"
        fields.append(f)
    a = re.search(rf"(\d+) lines of ({NUMBER}){IN} (E70\w*) fillet welds x ({NUMBER}){IN}",text,re.I)
    b = re.search(rf"(\d+) lines x ({NUMBER}){IN}, ({NUMBER}){IN} (E70\w*)",text,re.I)
    match = a or b
    if match:
        count,size,metal,length = a.groups() if a else (b[1],b[3],b[4],b[2])
        for name,value,unit in (("count",number(count),"count"),("size_in",number(size),"in"),("length_in",number(length),"in"),("material",metal,"literal")):
            f = _field(plate+"_weld",name,value,unit,pointer,match.group(),match.span(),scope)
            f["attachment_roles"] = [key,plate]
            fields.append(f)
    target = {"role":key,"kind":key if flange else "column","section":member_section}
    if support:
        target.update(grid_intersections=[m.groups() for name in support.groups()[:2] if (m:=re.fullmatch(r"([A-Z]+)(\d+)",name))],level=int(support[3]))
    return target


def _crossing_selections(record, fields):
    data = record["data"]
    text = data.get("components","")
    if not isinstance(text,str):
        return None
    common = re.search(r"crossing: one diagonal continuous, the other (?:spliced through it with a "
                       r"[^.;]+?gusset and the same welds|connected through a [^.;]+? gusset passing "
                       r"through the slotted continuous brace, same welds as the end connection)", text, re.I)
    split = re.search(r"the spliced diagonal's halves slotted onto the gusset with the end-connection welds",text,re.I)
    if not (common or split):
        return None
    pointer=record["source_pointer"]+"/components"
    for field in list(fields):
        if field["role"] != "brace_slot_weld":
            continue
        for field_role in ("split",):
            f = _field(field_role+"_brace_slot_weld",field["field"],field["value"],field["unit"],pointer,
                       field["source"]["literal"],field["source"].get("span"),{"joint":"crossing","crossing_receiver":field_role})
            f["application_source"]={"pointer":pointer,"literal":(split or common).group(),"span":list((split or common).span())}
            f["attachment_roles"]=["split_diagonal_half","crossing_gusset"]
            fields.append(f)
    around = re.search(r"fillet-welded to it all round the slot",text,re.I)
    if around:
        fields.append(_field("continuous_brace_slot_weld","run","all_round_slot","enum",pointer,
                             around.group(),around.span(),{"joint":"crossing","crossing_receiver":"continuous"}))
    return {"kind":"compound" if common else "dedicated","scope":"crossing"}


def _positive_dimension(data, key):
    value = data.get(key)
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _text_value(data, key):
    return isinstance(data.get(key), str) and bool(data[key].strip())


def _checked_pin_stack(record):
    """Verify the through-pin stack, bore and specified clearances before binding its source."""
    stack = record["data"]["pin_stack"]
    dimensions = ("pin_diameter_in", "pin_hole_diameter_in", "grip_in")
    if not isinstance(stack, dict) or not all(_positive_dimension(stack, k) for k in dimensions):
        raise ValueError("Invalid selected pin dimensions in " + record["id"])
    gap = stack.get("clearance_between_moving_groups_in")
    if type(gap) not in (int, float) or not math.isfinite(gap) or gap < 0 or not _text_value(stack, "pin_material"):
        raise ValueError("Invalid selected pin material or moving clearance in " + record["id"])
    plies = stack.get("selected_stack")
    if not isinstance(plies, list) or len(plies) < 3:
        raise ValueError("Invalid selected pin plies in " + record["id"])
    for ply in plies:
        if (not isinstance(ply, dict) or not _positive_dimension(ply, "thickness_in")
                or not all(_text_value(ply, k) for k in ("role", "material", "connected_to"))):
            raise ValueError("Invalid selected pin ply in " + record["id"])
    roles = {ply["role"]: ply for ply in plies}
    if len(roles) != len(plies) or stack["pin_hole_diameter_in"] < stack["pin_diameter_in"]:
        raise ValueError("Conflicting selected pin stack in " + record["id"])

    # Attached cheek plies move with their named primary leaf. Directly
    # connected host leaves remain separate across a moving brace ply.
    owners = []
    for ply in plies:
        target = ply["connected_to"]
        if target == "host":
            owners.append(ply["role"])
        elif target == "brace_end":
            owners.append(target)
        elif target in roles and roles[target]["connected_to"] == "host":
            owners.append(target)
        else:
            raise ValueError("Unknown pin-ply attachment in " + record["id"])
    clearances = sum(a != b for a, b in zip(owners, owners[1:]))
    grip = sum(ply["thickness_in"] for ply in plies) + clearances * gap
    if not math.isclose(grip, stack["grip_in"], rel_tol=1e-9, abs_tol=1e-7):
        raise ValueError("Selected pin grip disagrees with the ply order and clearances in " + record["id"])
    return stack


def _add_crossing_plate(record, fields):
    plate = record["data"]["crossing_plate"]
    plate_fields = (("length_in", "in"), ("width_in", "in"), ("thickness_in", "in"), ("material", "literal"))
    weld_fields = (("count_per_bar", "count"), ("size_in", "in"), ("length_in", "in"),
                   ("material", "literal"), ("run", "enum"))
    if (not isinstance(plate, dict) or not _text_value(plate, "material")
            or not all(_positive_dimension(plate, k) for k, unit in plate_fields if unit == "in")):
        raise ValueError("Invalid selected flat-bar crossing plate in " + record["id"])
    weld = plate.get("welds")
    if (not isinstance(weld, dict) or type(weld.get("count_per_bar")) is not int or weld["count_per_bar"] < 1
            or not all(_positive_dimension(weld, k) for k in ("size_in", "length_in"))
            or not all(_text_value(weld, k) for k in ("material", "run"))):
        raise ValueError("Invalid selected flat-bar crossing welds in " + record["id"])

    pointer = record["source_pointer"] + "/crossing_plate"
    for key, unit in plate_fields:
        fields.append(_field("flat_bar_crossing_plate", key, plate[key], unit, pointer + "/" + key,
                             plate[key], applicability={"attachment": "flat_bar_crossing"}))
    for key, unit in weld_fields:
        field = _field("flat_bar_crossing_fillet", key, weld[key], unit, pointer + "/welds/" + key,
                       weld[key], applicability={"attachment": "flat_bar_crossing"})
        field["attachment_roles"] = ["flat_bar", "crossing_plate"]
        fields.append(field)
    return {"value": plate, "source_pointer": pointer}


def selection_record(record):
    typed = "bim_selection" in record["data"]
    fields = _typed_fields(record) if typed else _legacy_fields(record) + _flat_bar_check_fields(record) + checked_bolt_pattern(record)
    result = {"source_pointer": record["source_pointer"], "record_sha256": digest_value(record["data"]),
              "basis": "typed_calc_fields" if typed else "literal_legacy_clauses", "fields": fields}
    data = record["data"]
    if not typed and (support := _brace_support(record, fields)):
        result["dedicated_brace_base"] = support
    if not typed and (receivers := _compound_receivers(record, fields)):
        result["compound_receivers"] = receivers
    if not typed and (transfer := _transfer_receivers(record, fields)):
        result["transfer_target"] = transfer
    if not typed and (crossing := _crossing_selections(record, fields)):
        result["crossing_source"] = crossing
    if "pin_stack" in data:
        result["data"] = {"pin_stack": _checked_pin_stack(record)}
        stack = result["data"]["pin_stack"]
        base_pointer = record["source_pointer"] + "/pin_stack"
        result["pin_stack_source_pointer"] = base_pointer
        located = data.get("receiver_selection",{})
        if located.get("brace_member_id") and located.get("native_connection_id"):
            scope = {"connection_ids":[located["native_connection_id"]]}
            fields.append(_field("connection","variant","brb_pinned","enum",base_pointer+"/selected_stack",
                                 stack["selected_stack"],applicability=scope))
            for role,key,target,unit in (("pin","diameter_in","pin_diameter_in","in"),("pin","material","pin_material","literal"),
                                         ("pin_hole","diameter_in","pin_hole_diameter_in","in")):
                fields.append(_field(role,key,stack[target],unit,base_pointer+"/"+target,stack[target],applicability=scope))
            for i,ply in enumerate(stack["selected_stack"]):
                if ply["connected_to"]!="host":
                    continue
                for key,unit in (("thickness_in","in"),("material","literal")):
                    if key == "material":
                        normalize = lambda value: re.sub(r"\bASTM\s*|\b(?:GRADE|GR)\.?\s*|\s+", "", str(value).upper())
                        equivalent = [f for f in fields if f["role"]=="gusset" and f["field"]==key and
                                      normalize(f["value"]) == normalize(ply[key])]
                        for quoted in equivalent:
                            if quoted["value"] != ply[key]:
                                quoted["published_value"] = quoted["value"]
                                quoted["value"] = ply[key]
                    fields.append(_field("gusset",key,ply[key],unit,base_pointer+f"/selected_stack/{i}/"+key,ply[key],applicability=scope))
    if "receiver_selection" in data:
        receiver = data["receiver_selection"]
        if (not isinstance(receiver, dict) or not _text_value(receiver, "native_connection_id")
                or not _text_value(receiver, "column_id") or not _text_value(receiver, "brace_member_id")):
            raise ValueError("Invalid located brace receiver selection in " + record["id"])
        scopes = {"connection_ids": [receiver["native_connection_id"]]}
        for edge, role in (("column_edge", "gusset_column_weld"), ("horizontal_edge", "gusset_base_weld"
                if receiver.get("support_type") == "column_base" else "gusset_beam_weld")):
            selected = receiver.get(edge)
            if (not isinstance(selected, dict) or not all(_positive_dimension(selected, k) for k in ("size_in", "length_in"))
                    or type(selected.get("count_per_leaf")) is not int or selected["count_per_leaf"] < 1
                    or not all(_text_value(selected, k) for k in ("weld_type", "material"))):
                raise ValueError("Invalid located brace support welds in " + record["id"])
            for key, unit in (("size_in", "in"), ("length_in", "in"), ("count_per_leaf", "count"),
                              ("weld_type", "enum"), ("material", "literal")):
                pointer = record["source_pointer"] + "/receiver_selection/" + edge + "/" + key
                precise = _field(role, key, selected[key], unit, pointer, selected[key], applicability=scopes)
                printed = [f for f in fields if f["role"] == role and f["field"] == key]
                for existing in printed:
                    if existing["value"] != selected[key] and not (unit == "in" and
                            math.isclose(existing["value"], selected[key], rel_tol=0, abs_tol=.05)):
                        raise ValueError("Located brace selection conflicts with its component row in " + record["id"])
                if printed:
                    precise["printed_sources"] = [f["source"] for f in printed]
                    fields[:] = [f for f in fields if f not in printed]
                fields.append(precise)
        result.setdefault("data", {})["receiver_selection"] = receiver
        result["receiver_selection_source_pointer"] = record["source_pointer"] + "/receiver_selection"
    if "crossing_plate" in data:
        result["crossing_plate"] = _add_crossing_plate(record, fields)
    alternate = re.search(r"\bThe other (?:ends of these beams|W[\dX.]+ beam ends) have no gusset \(record ([A-Z0-9./-]+)\)",
                          str(data.get("design_basis", "")), re.I)
    if not typed and alternate:
        result["corner_alternative"] = {"other_end_record": "connections/" + alternate[1],
                                        "source": {"pointer": record["source_pointer"] + "/design_basis",
                                        "literal": alternate.group(), "span": list(alternate.span())}}
    beam_groups = None if typed else _beam_end_groups(record, fields)
    if beam_groups:
        result["beam_end_groups"] = beam_groups
    support_groups = None if typed else _support_groups(record, fields)
    if support_groups:
        result["support_groups"] = support_groups
    if isinstance(data.get("section"), str):
        result["member_section"] = {"value": data["section"], "pointer": record["source_pointer"] + "/section"}
    source_type = str(data.get("type", ""))
    if not result.get("member_section") and (match := re.search(r"\bsimple ends of the (W[\dX.]+) beams\b", source_type, re.I)):
        result["member_section"] = {"value": match[1].upper(), "pointer": record["source_pointer"] + "/type",
                                    "literal": match.group(), "span": list(match.span())}
    if not typed and (target := receiver_target(record)):
        result["receiver_target"] = target
        if target["role"] == "scheduled_collector" and isinstance(data.get("components"), str):
            text = data["components"]
            groups = list(re.finditer(r"(?:^|;\s*)(W[\dX.]+) (?:link-roof )?girder ends:", text, re.I))
            if len(groups) > 1:
                for field in fields:
                    src = field["source"]
                    if src["pointer"].endswith("/components") and field["role"] != "connection" and "span" in src:
                        matches = [g for g in groups if g.start() <= src["span"][0]]
                        if matches:
                            field["applicability"]["girder_section"] = matches[-1][1]
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
    alias = re.search(r"\bend connection = the collector / strut detail of ([A-Z0-9./-]+)", str(data.get("components", "")), re.I)
    if not typed and (named_grid or alias or re.match(r"^collector-to-braced-frame connection\s*\(single plate\b", source_type, re.I)):
        target = {"role": "collector_to_frame", "source": {"pointer": record["source_pointer"] + "/type", "literal": data["type"]}}
        if alias:
            target["referenced_end"] = {"record_id": "connections/" + alias[1],
                                        "source": {"pointer": record["source_pointer"] + "/components",
                                        "literal": alias.group(), "span": list(alias.span())}}
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
    if "column_section" in scope:
        columns = {m["section"] for m in (location or {}).get("columns", {}).values()}
        if not columns:
            return "conditional"
        if scope["column_section"] not in columns:
            return "different"
    if "column_face" in scope:
        weak = (connection.get("parameters") or {}).get("weak_axis_gusset")
        if type(weak) is not bool:
            return "conditional"
        if scope["column_face"] != ("web" if weak else "flange"):
            return "different"
    if "post_end" in scope:
        post = (connection.get("assembly_instance") or {}).get("bindings", {}).get("POST")
        end = (location or {}).get("end_members", {}).get(post, {}).get("vertical_end")
        return "conditional" if not end else "selected" if end == scope["post_end"] else "different"
    if "crossing_receiver" in scope:
        binding = ((location or {}).get("crossing_members") or {}).get(scope["crossing_receiver"])
        if not binding:
            return "conditional"
    if "girder_section" in scope:
        ends = (location or {}).get("end_members", {})
        return ("conditional" if not ends else "selected" if any(
            m.get("kind") == "beam" and m.get("section") == scope["girder_section"] for m in ends.values()) else "different")
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
    result = {"point_in": list(point), "end_members": {}, "columns": {}, "flat_bar_ends": [], "flat_bar_crossing": False}
    crossing_axes = []
    columns, beam_axes = [], []
    for mid in connection.get("member_ids", []):
        member = members.get(mid, {})
        axis = member.get("axis", [])
        if len(axis) != 2 or any(len(end) != 3 for end in axis):
            continue
        profile_id = member.get("profile_id")
        profile = profiles.get(profile_id, {})
        endpoint = min(math.dist(point, end) for end in axis) < 1e-5
        if member.get("kind") == "column":
            result["columns"][mid] = {"section": profile_id, "source_ids": member.get("design_record_ids", [])}
        if member.get("kind") == "column" and profile.get("family") == "i":
            columns.append(member)
        if endpoint and member.get("kind") == "beam":
            beam_axes.append([b-a for a, b in zip(*axis)])
        if endpoint:
            result["end_members"][mid] = {"section": profile_id, "kind": member.get("kind"), "source_ids": member.get("design_record_ids", [])}
            if abs(axis[0][2] - axis[1][2]) > 1e-5:
                result["end_members"][mid]["vertical_end"] = "upper" if abs(max(p[2] for p in axis) - point[2]) < 1e-5 else "lower"
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
    bindings = (connection.get("assembly_instance") or {}).get("bindings", {})
    continuous = [mid for name, mid in bindings.items() if name == "CONT" and mid in members]
    split = sorted(mid for name, mid in bindings.items() if name.startswith("SPLIT") and mid in members)
    if (connection.get("parameters", {}).get("joint") == "crossing" and len(continuous) == 1 and len(split) == 2
            and len(set(continuous + split)) == 3 and set(continuous + split) <= set(connection.get("member_ids", []))):
        result["crossing_members"] = {"continuous": continuous, "split": split}
    result["support_faces"] = []
    if len(columns) == len(beam_axes) == 1:
        column, beam = columns[0], beam_axes[0]
        y = column.get("local_y", [])
        length = math.sqrt(sum(v*v for v in beam)*sum(v*v for v in y))
        if len(y) == 3 and length:
            normal = abs(sum(v*w for v, w in zip(beam, y)))/length
            side = "web" if abs(normal-1) < 1e-6 else "flange" if normal < 1e-6 else None
            if side:
                result["support_faces"].append({"side": side, "section": column["profile_id"], "member_id": column["id"]})
    if reference:
        result["grids"] = []
        grid_axes = {}
        terminal_ids = list(result["end_members"])
        for mid in terminal_ids:
            result["end_members"][mid]["axis_grids"] = [[], []]
        for grid in reference.get("grids", []):
            grid_axes.setdefault(grid.get("direction"), []).append(grid["label"])
            path = grid.get("points_in", [])
            if len(path) == 2 and abs(math.dist(point[:2], path[0][:2]) + math.dist(point[:2], path[1][:2]) -
                                       math.dist(path[0][:2], path[1][:2])) < 1e-6:
                result["grids"].append(grid["label"])
            if len(path) == 2:
                length = math.dist(path[0][:2], path[1][:2])
                for mid in terminal_ids:
                    for i, end in enumerate(members[mid]["axis"]):
                        if abs(math.dist(end[:2], path[0][:2]) + math.dist(end[:2], path[1][:2]) - length) < 1e-6:
                            result["end_members"][mid]["axis_grids"][i].append(grid["label"])
        result["grid_axes"] = list(grid_axes.values())
        result["levels"] = [x["level"] for x in reference.get("levels", [])
                            if isinstance(x.get("elevation_in"), (float, int)) and abs(x["elevation_in"] - point[2]) < 1e-5]
    return result


def applications(connection, records, location=None, member_catalog=None):
    result = []
    alternatives = {}
    source_ids = connection.get("design_record_ids", [])
    base_records = [rid for rid in source_ids if records.get(rid, {}).get("dedicated_brace_base")]
    crossings = [rid for rid in source_ids if records.get(rid, {}).get("compound_receivers") == "crossing"]
    receiver_links = {rid: receiver_application(target, location, member_catalog or {})
                      for rid in connection.get("design_record_ids", []) if (target := records.get(rid, {}).get("receiver_target"))}
    matched_receivers = [rid for rid, link in receiver_links.items() if link["status"] == "located_interface"]
    specific_infills = [rid for rid in matched_receivers if records[rid]["receiver_target"].get("specific")]
    if specific_infills:
        for rid in matched_receivers:
            if receiver_links[rid]["role"] == "infill_end" and rid not in specific_infills:
                receiver_links[rid].update(status="related_interface", located_specific_records=specific_infills)
    scheduled = [rid for rid in matched_receivers if receiver_links[rid]["role"] in
                 ("scheduled_collector", "reentrant_frame", "collector_beam_end", "opening_edge", "infill_end")]
    if scheduled:
        for rid in matched_receivers:
            if receiver_links[rid]["role"] == "smf_column":
                receiver_links[rid].update(status="related_interface", located_specific_records=scheduled)
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
        rtype = next((f["value"] for f in record["fields"] if f["role"] == "connection" and f["field"] == "source_type"), "")
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
        support = None
        supports = record.get("support_groups", [])
        if supports and location and len(location["support_faces"]) == 1:
            face = location["support_faces"][0]
            if face["side"] == "flange":
                support = 0
            else:
                matches = [i for i, option in enumerate(supports) if option.get("section") == face["section"] and
                           any(set(loc["grids"]) <= set(location.get("grids", [])) and loc["level"] in location.get("levels", [])
                               for loc in option.get("locations", []))]
                if len(matches) == 1:
                    support = matches[0]
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
            if alias := target.get("referenced_end"):
                reference_id = alias["record_id"]
                binding["referenced_end_record"] = reference_id
                if local and reference_id not in local:
                    binding["status"] = "different_source_end"
            if specified := target.get("specified_location"):
                match = (specified["grid"] in binding["grids"] and specified["level"] in binding["levels"] and
                         any(m["section"] == specified["section"] and m.get("direction") == specified["direction"]
                             for m in end_members.values()))
                binding["at_specified_member_grid_and_level"] = match
                if not match:
                    binding["status"] = "different_location" if location and binding["levels"] else "unlocated_interface"
        if rid in receiver_links:
            binding = receiver_links[rid]
        combined = any(receiver_links[key]["role"] == "combined_braced_bay" for key in matched_receivers)
        included = None
        if combined and rid not in matched_receivers and re.search(
                r"^single-plate shear connection \(conventional configuration|^collector / drag strut connection \(braced-bay beam", rtype, re.I):
            included = [key for key in matched_receivers if receiver_links[key]["role"] == "combined_braced_bay"]
        transfer = record.get("transfer_target")
        transfer_ids = []
        if transfer:
            place = location or {}
            matches = not transfer.get("grid_intersections") or (
                      any(m["kind"]=="transfer_chord" for m in place.get("end_members",{}).values()) and
                      any(set(pair)<=set(place.get("grids",[])) for pair in transfer["grid_intersections"]))
            if matches:
                if transfer["kind"] == "column":
                    transfer_ids = sorted(mid for mid,m in place.get("columns",{}).items() if m["section"]==transfer["section"])
                else:
                    transfer_ids = sorted(mid for mid, m in place.get("end_members",{}).items()
                                          if m["kind"]==transfer["kind"] and m["section"]==transfer["section"])
        crossing_joint = (connection.get("parameters") or {}).get("joint") == "crossing"
        active, conditional = [], []
        for index, field in enumerate(record["fields"]):
            status = applicability(field, connection, location)
            if "transfer_receiver" in field["applicability"] and not transfer_ids:
                status = "different"
            if crossing_joint and record.get("crossing_source") and field["role"] in {
                    "brace_slot_weld","gusset_column_weld","gusset_beam_weld","gusset_base_weld","brace_end_clearance"}:
                status = "different"
            if crossing_joint and record.get("crossing_source") and field["field"] == "whitmore_effective_width_in":
                status = "different"
            if crossings and rid not in crossings and (field["role"] != "connection" or field["field"] == "variant"):
                status = "different"
            if base_records and rid not in base_records and field["role"] in {
                    "gusset_column_weld", "gusset_beam_weld", "gusset_base_weld", "gusset_column_clearance"}:
                status = "different"
            if (base_records and rid not in base_records and field["role"] == "gusset"
                    and field["field"] in ("material", "thickness_in")):
                status = "different"
            if "beam_group" in field["applicability"]:
                status = "conditional" if group is None else "selected" if group == field["applicability"]["beam_group"] else "different"
            if "support_group" in field["applicability"]:
                status = "conditional" if support is None else "selected" if support == field["applicability"]["support_group"] else "different"
            if rid in alternatives and status == "selected":
                place, applies_at = alternatives[rid]
                if place["located_end"] != applies_at:
                    status = "conditional" if place["located_end"] == "conditional" else "different"
            if field["role"] == "gusset_beam_weld" and (connection.get("parameters") or {}).get("joint") == "base":
                status = "different"
            if binding and binding["status"] != "located_interface" and status == "selected":
                status = "conditional"
            if matched_receivers and rid not in receiver_links and record.get("member_section") and status == "selected":
                if re.search(r"\bgravity beam ends\b|\bsimple ends of the W[\dX.]+ beams\b", rtype, re.I):
                    status = "different"
                if scheduled and re.search(r"^(?:collector beam-to-column connection|single-plate shear connection|"
                                           r"single-plate shear \+ axial connection|SCBF mid-gusset beam-to-column connection|beam-to-column connection)\b", rtype, re.I):
                    status = "conditional"
            if included and status == "selected":
                status = "conditional"
            if status == "selected":
                active.append(index)
            elif status == "conditional":
                conditional.append(index)
        entry = {"record_id": rid, "selected": active, "conditional": conditional}
        if transfer:
            entry["transfer_members"] = {transfer["role"]:transfer_ids}
        if (rid in crossings or (crossing_joint and record.get("crossing_source"))) and location:
            entry["crossing_members"] = location.get("crossing_members", {})
        if base_records and rid not in base_records:
            entry["local_base_support_records"] = base_records
        if binding:
            entry["interface_application"] = binding
        if included:
            entry["included_in_combined_end"] = included
        if scheduled and rid not in scheduled and record.get("member_section") and not active:
            entry["located_specific_records"] = scheduled
        if groups:
            entry["beam_end_group"] = {"selected": group, "grids": (location or {}).get("grids", []),
                                       "levels": (location or {}).get("levels", [])}
        if supports:
            entry["support_application"] = {"selected_group": support, "faces": (location or {}).get("support_faces", []),
                                            "grids": (location or {}).get("grids", []), "levels": (location or {}).get("levels", [])}
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

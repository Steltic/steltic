"""Check HR source bindings and explicit quantitative selections before geometry export."""
import csv
from functools import lru_cache
import math
from pathlib import Path
import re

from . import bim_assemblies
from .bim_collector_applications import member_sources
from .bim_source_crossings import crossing_interfaces
from .bim_source_fields import applications, brace_receiver_roles, digest_value, native_content_digest, selection_record


def _issue(code, message, *ids):
    return {"code": code, "message": message, "entity_ids": sorted(set(str(x) for x in ids)), "severity": "needs_input"}


@lru_cache(maxsize=1)
def rolled_catalog():
    path = Path(__file__).with_name("aisc_shapes.csv")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return {r["AISC_Manual_Label"].upper(): {k: float(r[k]) for k in ("d", "bf", "tf", "tw")}
                for r in csv.DictReader(stream) if r["Type"] in ("W", "S", "HP", "M") and
                all(r.get(k) for k in ("d", "bf", "tf", "tw"))}


def _numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def _equal_number(left, right):
    return _numeric(left) and _numeric(right) and math.isclose(left, right, rel_tol=1e-7, abs_tol=1e-7)


def _member_findings(package):
    physical = package["physical"]
    profiles = {row["id"]: row for row in physical["profiles"]}
    materials = {row["id"]: row for row in physical["materials"]}
    records = {row["id"]: row for row in package["design_records"]}
    elements = {row["id"]: row for row in package["analysis"]["elements"]}
    findings = []
    catalog = rolled_catalog()
    for pid, profile in profiles.items():
        reference = catalog.get(pid.upper())
        if not reference or profile.get("family") != "i":
            continue
        for field, expected in reference.items():
            if not _equal_number(profile.get("dimensions", {}).get(field), expected):
                findings.append(_issue("hr_catalog_dimension", f"Rolled profile {field} differs from the checked HR section catalogue",
                                       pid, "aisc_shapes.csv:" + pid + "/" + field))
    for member in physical["members"]:
        profile = profiles.get(member.get("profile_id"), {})
        material = materials.get(member.get("material_id"), {})
        mid, selected = member["id"], member.get("profile_id")
        for rid in member.get("design_record_ids", []):
            row = records.get(rid, {})
            inputs = row.get("data", {}).get("inputs", {})
            if row.get("table") != "members" or not isinstance(inputs, dict) or inputs.get("section") != selected:
                continue
            for key in ("Fy", "Fu"):
                expected = inputs.get(key)
                actual = material.get(key, material.get(key.lower() + "_ksi"))
                if _numeric(expected) and _numeric(actual) and not _equal_number(actual, expected):
                    findings.append(_issue("hr_selected_material_strength", f"Physical material {key} differs from selected source value",
                                           mid, material.get("id"), row["source_pointer"] + "/inputs/" + key))
            dimensions = inputs.get("profile_dimensions")
            if isinstance(dimensions, dict):
                for key, expected in dimensions.items():
                    if _numeric(expected) and not _equal_number(expected, profile.get("dimensions", {}).get(key)):
                        findings.append(_issue("hr_selected_profile_dimension", "Physical section differs from selected numeric " + key,
                                               mid, selected, row["source_pointer"] + "/inputs/profile_dimensions/" + key))
        # A directly bound one-to-one rolled element has a separate source
        # schedule. Subdivided physical columns and modeled constituents use
        # their explicit interval/component mapping in mapping_geometry_findings.
        direct = member.get("analysis_ids", [])
        if len(direct) != 1 or member.get("analysis_segments") or member.get("analysis_component_roles") or selected not in catalog:
            continue
        element = elements.get(direct[0], {})
        analysis_section = element.get("profile_id")
        schedule_section = (element.get("source_schedule") or {}).get("section")
        if analysis_section != selected or (schedule_section and schedule_section != selected):
            proof = member.get("selected_profile_mapping", {})
            rid = proof.get("record_id")
            record = records.get(rid, {})
            data = record.get("data", {})
            # A broad record link alone does not establish applicability.
            # The immutable source row names the exact analysis member and
            # physical location/section. Grouped selections require a located
            # interval/constituent proof in the dedicated physical mapping.
            if (rid in member.get("design_record_ids", []) and proof.get("record_sha256") == digest_value(data) and
                    data.get("bim_member_application") == {"analysis_id": direct[0], "physical_id": mid,
                    "analysis_section": analysis_section, "schedule_section": schedule_section,
                    "physical_section": selected, "axis": member.get("axis")}):
                continue
            findings.append(_issue("hr_analysis_profile_mapping", "A direct physical/analysis section change requires its located source application",
                                   mid, direct[0], selected, analysis_section, schedule_section))
    return findings


def _part_role(part):
    explicit = part.get("selection_role") or part.get("parameters", {}).get("selection_role")
    if explicit:
        return explicit
    kind = re.sub(r"[_-]", " ", part.get("kind", "").lower())
    if re.search(r"\b(?:head|nut|washer|plate)\b", kind):
        return None
    for role in ("anchor", "bolt", "pin", "screw"):
        if re.search(r"\b" + role + r"s?\b", kind):
            return role
    return None


def _part_findings(package, records):
    if not package.get("require_connection_parts"):
        return []
    physical = package["physical"]
    templates = bim_assemblies.index(physical)
    materials = {m["id"]: m for m in physical["materials"]}
    findings = []
    for connection in physical["connections"]:
        if connection.get("scope") == "delegated":
            continue
        parts = bim_assemblies.parts(connection, templates)
        groups = {}
        for part in parts:
            groups.setdefault(_part_role(part), []).append(part)
        occurrence = package.get("source_handoff", {}).get("occurrences", {}).get(connection["id"], {})
        overrides = {(o["record_id"], o["field_index"]): o["value"] for o in occurrence.get("overrides", [])}
        for link in applications(connection, records):
            record = records[link["record_id"]]
            # Legacy narrative values are delivered with their literal spans
            # for openBIM's assembly recipes and final artifact checks. Strict
            # producer checks below apply to typed selections with clear roles.
            if record["basis"] != "typed_calc_fields" and not record.get("structured_components"):
                continue
            for index in link["selected"]:
                field = record["fields"][index]
                role, key = field["role"], field["field"]
                expected = overrides.get((link["record_id"], index), field["value"])
                origin = field["source"]["pointer"]
                if role == "connection" and key == "variant":
                    actual = connection.get("selected_variant")
                    if actual != expected:
                        findings.append(_issue("hr_selected_variant", "Physical connection variant differs from its selected source",
                                               connection["id"], origin))
                    continue
                if role not in ("pin", "bolt", "screw", "anchor"):
                    # An HR source need not provide a gusset polygon or local
                    # fit. Compare named source values on actual physical
                    # declarations; openBIM owns any remaining derived geometry.
                    for part in groups.get(role, []):
                        params = part.get("parameters", {})
                        if key == "material":
                            material = materials.get(part.get("material_id"), {})
                            actual = material.get("grade") or material.get("specification")
                        else:
                            actual = params.get(key, params.get(key.removesuffix("_in")))
                        if actual is None:
                            continue
                        if not (_equal_number(actual, expected) if _numeric(expected) else actual == expected):
                            findings.append(_issue("hr_selected_component_field", f"Physical {role}.{key} differs from the selected field",
                                                   connection["id"], part["id"], origin))
                        actual_roles = params.get("attachment_roles")
                        if field.get("attachment_roles") and actual_roles is not None and set(actual_roles) != set(field["attachment_roles"]):
                            findings.append(_issue("hr_selected_attachment", "The physical attachment serves different source roles",
                                                   connection["id"], part["id"], origin))
                    continue
                shafts = [p for p in groups.get(role, []) if p.get("geometry", {}).get("type") in
                          ("cylinder", "mesh", "extruded_profile", "box")]
                if key == "count":
                    actual = len({digest_value(p["geometry"]) for p in shafts})
                    if actual != expected:
                        findings.append(_issue("hr_selected_fastener_count", f"Selected {expected} {role} shafts, found {actual}",
                                               connection["id"], origin))
                elif key == "diameter_in":
                    for part in shafts:
                        geom = part["geometry"]
                        value = 2 * geom["radius"] if geom["type"] == "cylinder" else part.get("parameters", {}).get(key)
                        if not _equal_number(value, expected):
                            findings.append(_issue("hr_selected_fastener_diameter", "Physical shaft diameter differs from selected inches",
                                                   connection["id"], part["id"], origin))
                elif key == "material":
                    for part in shafts:
                        material = materials.get(part.get("material_id"), {})
                        actual = material.get("grade") or material.get("specification")
                        if not isinstance(actual, str) or actual.casefold().strip() != str(expected).casefold().strip():
                            findings.append(_issue("hr_selected_fastener_material", "Bind the selected fastener material to its exact specification",
                                                   connection["id"], part["id"], origin))
    return findings


def findings(package):
    records = {}
    for r in package["design_records"]:
        if r["table"] == "connections" and not r.get("waived"):
            try:
                records[r["id"]] = selection_record(r)
            except (ValueError, KeyError, TypeError) as error:
                return [_issue("hr_selection_invalid", str(error), r["id"])]
    handoff = package.get("source_handoff")
    errors = []
    if handoff:
        from .saved_handoff import build_selections, _specific_overrides, _typed_conflicts
        expected_hash = digest_value({k: v for k, v in handoff.items() if k != "contract_sha256"})
        if handoff.get("contract_sha256") != expected_hash or handoff.get("building_id") != package["building_id"]:
            errors.append(_issue("hr_handoff_revision", "Source handoff content or building differs from its bound revision"))
        if handoff["source_bindings"].get("native_content_sha256") != native_content_digest(package):
            errors.append(_issue("hr_handoff_native_content", "Native geometry or other package content changed after HR binding"))
        if handoff.get("records") != records:
            errors.append(_issue("hr_handoff_record", "Source field values/provenance differ from the immutable calculation records"))
        crossings, unresolved = crossing_interfaces(records, package["physical"])
        if handoff.get("crossing_interfaces") != crossings or handoff.get("unresolved_crossing_interfaces") != unresolved:
            errors.append(_issue("hr_handoff_crossings", "Source crossing coverage differs from the native member locations"))
        for item in unresolved:
            errors.append(_issue("hr_unlocated_crossing", item["reason"], item["record_id"], *item["unmatched_member_ids"]))
        occurrences = handoff.get("occurrences", {})
        source_files = handoff["source_files"]
        native_files = package.get("provenance", {}).get("source_files", {})
        if any(digest != native_files.get(name) for name, digest in source_files.items() if name != "report.html"):
            errors.append(_issue("hr_handoff_source_binding", "Selected source hashes differ from the native package provenance"))
        original_report = handoff.get("material_schedule", {}).get("source", {})
        if original_report.get("path") == "report.html" and original_report.get("sha256") != source_files.get("report.html"):
            errors.append(_issue("hr_handoff_report_binding", "Original report bytes differ from the saved material source binding"))
        if handoff["source_bindings"].get("report_binding") == "bound" and not source_files.get("report.html"):
            errors.append(_issue("hr_handoff_report_binding", "A bound report requires its SHA-256"))
        if set(occurrences) != {c["id"] for c in package["physical"]["connections"]}:
            errors.append(_issue("hr_handoff_occurrences", "Source handoff does not cover the exact connection occurrence set"))
        catalog = member_sources(package["design_records"])
        if handoff["source_bindings"].get("member_application_sources") != catalog:
            errors.append(_issue("hr_handoff_member_sources", "Collector and support roles differ from the original source member records"))
        expected_occurrences = build_selections(records, package["physical"], package.get("reference_system"), catalog)
        for cid, roles in brace_receiver_roles(package["physical"]).items():
            expected_occurrences[cid]["receiver_roles"] = roles
        try:
            expected_conflicts = _specific_overrides(records, expected_occurrences, package.get("information", {}).get("decisions", []),
                                                     source_files.get("design/calc_package.json"))
        except (ValueError, KeyError, IndexError, TypeError) as error:
            return errors + [_issue("hr_handoff_override", str(error))]
        expected_conflicts.extend(_typed_conflicts(records, expected_occurrences))
        expected_conflicts.extend(handoff["material_schedule"].get("conflicts", []))
        if handoff.get("source_conflicts") != expected_conflicts:
            errors.append(_issue("hr_handoff_source_conflicts", "Source conflict register differs from the selected fields and accepted decisions"))
        for cid, expected in expected_occurrences.items():
            if occurrences.get(cid) != expected:
                errors.append(_issue("hr_handoff_applicability", "Source fields, receiver roles or overrides differ from the located application", cid))
        for conflict in expected_conflicts:
            errors.append(_issue("hr_source_conflict", "Competing selected values require an exact field-specific decision", conflict.get("connection_id", ""), conflict.get("role", "")))
    for r in package["design_records"]:
        if r["id"] in records and isinstance(r["data"].get("components"), dict):
            records[r["id"]]["structured_components"] = True
    errors.extend(_member_findings(package))
    errors.extend(_part_findings(package, records))
    return errors

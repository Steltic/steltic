"""Export a fresh HR handoff from an authenticated saved design without replay.

The immutable native snapshot supplies building geometry. Original calculation
records supply design selections. The decision archive binds the snapshot, the
accepted physical inputs and the material table in the original HTML report.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import tempfile
import zipfile

from .bim_json import file_digest
from .bim_report_readback import check_report_selections
from .bim_source_crossings import crossing_interfaces
from .bim_source_fields import application_location, applications, brace_receiver_roles, digest_value, native_content_digest, selection_record
from .bim_source_status import consistency_errors
from .bim_materials import material_schedule as current_material_schedule, original_report_material_rows


SCHEMA = "steltic.hr_source_handoff"
VERSION = 1
REQUIRED_SOURCES = {"cfg.py", "design/calc_package.json", "design/member_schedule.csv",
                    "design/consistency_result.json"}


def _check(condition, message):
    if not condition:
        raise ValueError(message)


def _job_file(root, relative):
    _check(isinstance(relative, str) and relative and not Path(relative).is_absolute(), "Invalid job-relative source path")
    path = (root / relative).resolve()
    _check(path.is_relative_to(root) and path.is_file(), "Missing or outside-job source: " + relative)
    return path


def _check_hash(actual, expected, name):
    _check(isinstance(expected, str) and len(expected) == 64 and actual == expected,
           "Missing or stale SHA-256 for " + name)


def _pointer(data, pointer):
    _check(isinstance(pointer, str) and pointer.startswith("/"), "Invalid calc source pointer")
    for key in pointer[1:].split("/"):
        key = key.replace("~1", "/").replace("~0", "~")
        if isinstance(data, list):
            _check(key.isdecimal() and (key == "0" or not key.startswith("0")), "Invalid array source pointer: " + pointer)
            data = data[int(key)]
        else:
            data = data[key]
    return data


def _read_archive(path, native_file_hash):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        _check(len(set(names)) == len(names), "Duplicate decision archive member")
        manifest = json.loads(archive.read("manifest.json"))
        _check(manifest.get("schema") == "steltic.example_decision_pack" and manifest.get("module") == "HR",
               "Expected an HR example decision pack")
        _check_hash(native_file_hash, manifest.get("native_bim_file_sha256"), "saved native BIM package")
        checksums = manifest.get("files_sha256", {})
        for required in ("physical_inputs.json", "accepted_decisions.txt", "example_decisions.json", "adopted_answers.json"):
            _check(required in checksums, "Decision archive omits required file binding: " + required)
        for name, expected in checksums.items():
            with archive.open(name) as stream:
                digest = hashlib.sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            _check_hash(digest.hexdigest(), expected, "decision archive " + name)
        _check_hash(checksums["physical_inputs.json"], manifest.get("native_physical_sha256"), "physical input manifest")
        with archive.open("physical_inputs.json") as stream:
            physical_inputs = json.load(stream)
    return manifest, physical_inputs


def _evidence(root, native, manifest, physical):
    _check(native.get("schema") == "steltic.bim_package" and native.get("module") == "HR", "Expected an HR native package")
    _check(native.get("building_id") == manifest.get("building_id") == root.name, "Cross-job saved-design inputs")
    _check(physical.get("module") == "HR" and physical.get("revision") == manifest.get("native_physical_revision"),
           "Cross-revision physical inputs")
    native_inputs = native.get("provenance", {}).get("physical_inputs", {})
    _check_hash(manifest["native_physical_sha256"], native_inputs.get("sha256"), "native physical-input binding")
    _check(native_inputs.get("revision") == physical["revision"], "Native physical revision differs from decision archive")
    design_sources = dict(physical.get("design_sources", {}))
    _check(REQUIRED_SOURCES <= set(design_sources), "Saved decision pack lacks required design-source bindings")
    material_schedule = physical.get("gold_material_schedule", {})
    report_source = material_schedule.get("source", {})
    _check(report_source.get("path") == "report.html", "Saved physical package lacks original HR report binding")
    design_sources["report.html"] = report_source.get("sha256")
    native_sources = native.get("provenance", {}).get("source_files", {})
    for name, expected in design_sources.items():
        _check_hash(file_digest(_job_file(root, name)), expected, name)
        if name != "report.html":
            _check_hash(expected, native_sources.get(name), "native source binding " + name)
    archived_rows = [(" ".join(r["item"].split()), " ".join(r["spec"].split())) for r in material_schedule.get("materials", [])]
    actual_rows = original_report_material_rows(_job_file(root, "report.html"))
    _check(bool(archived_rows) and Counter(actual_rows) == Counter(archived_rows),
           "Saved material roles/specifications differ from the original report Materials table")
    stamp = json.loads(_job_file(root, "design/consistency_result.json").read_text(encoding="utf-8-sig"))
    errors = consistency_errors(root, stamp)
    _check(not errors, ", ".join(errors))
    return design_sources, material_schedule


def _records(native, calc):
    catalog = {}
    seen = set()
    for row in native["design_records"]:
        rid = row["id"]
        _check(rid not in seen, "Duplicate native design record " + rid)
        seen.add(rid)
        original = _pointer(calc, row["source_pointer"])
        _check(original == row["data"], "Native record differs from original " + row["source_pointer"])
        if row["table"] == "connections" and not row.get("waived"):
            _check(rid == "connections/" + str(original.get("id", "@" + row["source_pointer"].rsplit("/", 1)[-1])),
                   "Native connection identity differs from original " + row["source_pointer"])
            catalog[rid] = selection_record(row)
    expected = {"connections/" + str(row.get("id", "@" + str(i)))
                for i, row in enumerate(calc.get("connections", [])) if isinstance(row, dict) and not row.get("waived")}
    _check(set(catalog) == expected, "Native package lacks original selected connection records")
    return catalog


def build_selections(records, physical, reference=None):
    """Locate selected fields for each occurrence and retain ambiguous fields."""
    members = {m["id"]: m for m in physical.get("members", [])}
    profiles = {p["id"]: p for p in physical.get("profiles", [])}
    located = {rid for rid, record in records.items() if record.get("application_target") or record.get("beam_end_groups") or
               record.get("corner_alternative") or
               any("attachment" in f["applicability"] for f in record["fields"])}
    corners = {}
    for joint in physical["connections"]:
        if joint.get("parameters", {}).get("joint") == "corner" and joint.get("point"):
            point = tuple(round(v, 5) for v in joint["point"])
            corners.setdefault(point, {})[joint["id"]] = joint.get("member_ids", [])
    occurrences = {}
    for connection in physical["connections"]:
        cid = connection["id"]
        _check(cid not in occurrences, "Duplicate physical connection " + cid)
        location = None
        if located.intersection(connection.get("design_record_ids", [])):
            location = application_location(connection, members, profiles, reference)
            if location:
                location["corner_members"] = corners.get(tuple(round(v, 5) for v in location["point_in"]), {})
        links = applications(connection, records, location)
        occurrences[cid] = {"applications": links, "scope": connection.get("scope"),
                            "selection_basis": "located_source_records" if links else
                            ("adopted_physical_detail" if connection.get("basis") == "A" else "no_linked_connection_record")}
    return occurrences


def _typed_conflicts(records, occurrences):
    conflicts = []
    for cid, occurrence in occurrences.items():
        # Competing values in one explicitly typed role and applicability are
        # a source conflict. Several legacy hardware groups in one paragraph
        # are kept separately with source spans for the consumer to locate.
        selected = {}
        overrides = {(o["record_id"], o["field_index"]): o["value"] for o in occurrence.get("overrides", [])}
        for link in occurrence["applications"]:
            record = records[link["record_id"]]
            if record["basis"] != "typed_calc_fields":
                continue
            for index in link["selected"]:
                source = record["fields"][index]
                field = dict(source, value=overrides.get((link["record_id"], index), source["value"]))
                key = (field["role"], field["field"])
                previous = selected.get(key)
                if previous and (previous["value"], previous["unit"]) != (field["value"], field["unit"]):
                    conflicts.append({"connection_id": cid, "role": key[0], "field": key[1],
                                      "sources": [previous["source"], field["source"]],
                                      "values": [previous["value"], field["value"]]})
                selected[key] = field
    return conflicts


def _specific_overrides(records, occurrences, decisions, calc_sha256):
    """Accept a replacement only for the named occurrence, original value and source revision."""
    conflicts = []
    seen = {}
    for decision in decisions:
        replacements = list(decision.get("selection_overrides", []))
        for change in decision.get("changes", []):
            if change.get("collection") == "connections" and change.get("field") == "selection_overrides":
                for override in change["value"]:
                    _check(override.get("connection_id") == change["entity_id"], "Selection override exceeds the accepted connection binding")
                    replacements.append(override)
        if not replacements:
            continue
        _check(decision.get("action") in ("accept", "accept_test", "confirm") and
               all(isinstance(decision.get(k), str) and decision[k].strip() for k in ("decision_id", "author", "note")),
               "Field overrides require a named, accepted decision with author and reason")
        for change in replacements:
            cid, rid, index = change["connection_id"], change["record_id"], change["field_index"]
            _check(cid in occurrences and rid in records and type(index) is int and index >= 0, "Invalid selection override target")
            record = records[rid]
            _check(change.get("calc_sha256") == calc_sha256 and change.get("record_sha256") == record["record_sha256"],
                   "Selection override refers to a different source revision")
            link = next((link for link in occurrences[cid]["applications"] if link["record_id"] == rid), None)
            _check(link and index in link["selected"] + link["conditional"], "Selection override is outside its occurrence")
            field = record["fields"][index]
            _check(change.get("source_pointer") == field["source"]["pointer"] and change.get("source_value") == field["value"]
                   and change.get("unit") == field["unit"] and change.get("role") == field["role"] and change.get("field") == field["field"],
                   "Selection override must name the exact selected field, role, unit and source value")
            replacement = change.get("value")
            _check(type(replacement) in (str, int, float, bool) and isinstance(replacement, str) == isinstance(field["value"], str),
                   "Selection override needs a replacement of the selected type")
            key = (cid, rid, index)
            override = dict(change, decision_id=decision["decision_id"], author=decision["author"], reason=decision["note"])
            if key in seen and seen[key]["value"] != replacement:
                conflicts.append({"connection_id": cid, "role": field["role"], "field": field["field"],
                                  "reason": "Competing accepted field-specific overrides",
                                  "decision_ids": [seen[key]["decision_id"], decision["decision_id"]]})
            seen[key] = override
            occurrences[cid].setdefault("overrides", []).append(override)
    return conflicts


def _finish_handoff(building, source_files, bindings, materials, records, physical, decisions, reference=None):
    occurrences = build_selections(records, physical, reference)
    for cid, roles in brace_receiver_roles(physical).items():
        occurrences[cid]["receiver_roles"] = roles
    conflicts = _specific_overrides(records, occurrences, decisions, source_files.get("design/calc_package.json"))
    conflicts.extend(_typed_conflicts(records, occurrences))
    conflicts.extend(materials.get("conflicts", []))
    crossings, unresolved = crossing_interfaces(records, physical)
    code = Path(__file__).parent
    bindings["generator_files"] = {name: file_digest(code / name) for name in
                                   ("saved_handoff.py", "bim_source_fields.py", "bim_source_status.py", "bim_materials.py",
                                    "bim_source_checks.py", "bim_source_crossings.py", "bim_report_readback.py",
                                    "bim_assemblies.py", "aisc_shapes.csv")}
    bindings["geometry_completion_owner"] = "openBIM"
    handoff = {"schema": SCHEMA, "version": VERSION, "building_id": building,
               "source_files": source_files, "material_schedule": materials, "source_bindings": bindings,
               "records": records, "occurrences": occurrences, "source_conflicts": conflicts,
               "crossing_interfaces": crossings, "unresolved_crossing_interfaces": unresolved}
    handoff["contract_sha256"] = digest_value(handoff)
    return handoff


def attach_current_handoff(package, cfg, calc, selected_inputs=None):
    """Use the same field contract at the normal HR package export entry point."""
    records = _records(package, calc)
    files = dict(package["provenance"]["source_files"])
    materials = current_material_schedule(cfg, calc, selected_inputs)
    package["source_handoff"] = _finish_handoff(
        package["building_id"], files, {"kind": "current_hr_export", "analysis_executed": False,
        "native_content_sha256": native_content_digest(package),
        "report_binding": "bound" if "report.html" in files else "awaiting_report"},
        materials, records, package["physical"], (selected_inputs or {}).get("decisions", []), package.get("reference_system"))


def seal_current_package(package):
    """Bind producer-owned readiness and final report references to the current native body."""
    handoff = package["source_handoff"]
    _check(handoff["source_bindings"]["kind"] == "current_hr_export", "Only a live producer can seal changed native content")
    handoff["source_bindings"]["native_content_sha256"] = native_content_digest(package)
    handoff.pop("contract_sha256", None)
    handoff["contract_sha256"] = digest_value(handoff)


def _checked_report(report_path, calc):
    evidence = check_report_selections(report_path, calc)
    if not evidence["passed"]:
        first = evidence["findings"][0]
        target = first.get("connection", first.get("member", "unknown"))
        raise ValueError(f"HR report selection mismatch at {target}: {first['issue']}")
    return evidence


def bind_rendered_report(root, calc=None):
    """Refresh only the report dependency after a new report has been written."""
    root = Path(root).resolve()
    if calc is None and (root / "design/calc_package.json").is_file():
        calc = json.loads((root / "design/calc_package.json").read_text(encoding="utf-8-sig"))
    checked = _checked_report(root / "report.html", calc) if calc is not None else None
    path = root / "design/bim_package.json"
    if not path.is_file():
        return
    with path.open(encoding="utf-8-sig") as stream:
        package = json.load(stream)
    handoff = package.get("source_handoff", {})
    if handoff.get("source_bindings", {}).get("kind") != "current_hr_export":
        return
    _check_hash(native_content_digest(package), handoff["source_bindings"].get("native_content_sha256"), "current native content before report binding")
    sources = package["provenance"]["source_files"]
    for name, expected in sources.items():
        if name != "report.html":
            _check_hash(file_digest(_job_file(root, name)), expected, name)
    report_digest = file_digest(_job_file(root, "report.html"))
    sources["report.html"] = report_digest
    handoff["source_files"]["report.html"] = report_digest
    handoff["source_bindings"]["report_binding"] = "bound"
    if checked is not None:
        handoff["source_bindings"]["report_selection_readback"] = checked
    seal_current_package(package)
    _atomic_output(path, package)


def _atomic_output(path, value):
    """Publish a compact candidate atomically without a second whole-model string."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".hr-handoff-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def export_saved(job, native_path, decisions, output, *, contract_only=False):
    root, native_path, decisions, output = (Path(p).resolve() for p in (job, native_path, decisions, output))
    _check(output.suffix.lower() == ".json" and not output.is_relative_to(root) and output not in (native_path, decisions),
           "Saved-design output must use a new path outside the original job and input packages")
    native_hash, decisions_hash = file_digest(native_path), file_digest(decisions)
    manifest, physical = _read_archive(decisions, native_hash)
    with native_path.open(encoding="utf-8-sig") as stream:
        native = json.load(stream)
    source_files, material_schedule = _evidence(root, native, manifest, physical)
    decisions_data = physical.get("decisions", [])
    del physical
    calc = json.loads(_job_file(root, "design/calc_package.json").read_text(encoding="utf-8-sig"))
    records = _records(native, calc)
    report_readback = _checked_report(_job_file(root, "report.html"), calc)
    handoff = _finish_handoff(root.name, source_files,
                              {"kind": "authenticated_saved_design", "decision_archive_sha256": decisions_hash,
                               "native_content_sha256": native_content_digest(native),
                               "report_selection_readback": report_readback,
                               "native_bim_file_sha256": native_hash, "native_physical_sha256": manifest["native_physical_sha256"],
                               "native_physical_revision": manifest["native_physical_revision"], "report_binding": "bound",
                               "physical_input_scope": "accepted saved declarations for candidate regeneration",
                               "report_material_rows_verified": len(material_schedule["materials"]),
                               "analysis_executed": False}, material_schedule, records, native["physical"], decisions_data,
                              native.get("reference_system"))
    native["source_handoff"] = handoff
    # Close the read/write race for changed original evidence. Output always
    # resides in its own candidate directory, even for the compact sidecar.
    for name, expected in source_files.items():
        _check_hash(file_digest(_job_file(root, name)), expected, name)
    _check_hash(file_digest(native_path), native_hash, "saved native BIM package")
    _check_hash(file_digest(decisions), decisions_hash, "decision archive")
    _atomic_output(output, handoff if contract_only else native)
    return {"building_id": root.name, "connection_records": len(records), "connection_occurrences": len(handoff["occurrences"]),
            "source_conflicts": len(handoff["source_conflicts"]), "contract_sha256": handoff["contract_sha256"],
            "report_sha256": source_files["report.html"], "analysis_executed": False, "output": str(output)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("job", "native", "decisions", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--contract-only", action="store_true", help="Write the source_handoff section by itself")
    args = parser.parse_args(argv)
    try:
        summary = export_saved(args.job, args.native, args.decisions, args.output, contract_only=args.contract_only)
    except (OSError, ValueError, KeyError, IndexError, TypeError, zipfile.BadZipFile) as error:
        parser.exit(2, "HR saved handoff failed: " + str(error) + "\n")
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

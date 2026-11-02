"""Engine-independent physical-data contract v1, shared by HR, CFS and openBIM.

Validation is structural data checking, not engineering certification.
Keep this file and its adjacent JSON schema byte-identical in all three repos.
"""
import json
import math
from pathlib import Path

VERSION = "1.0"
COLLECTIONS = ("members", "panels", "openings", "profiles", "materials", "joints", "connections", "interfaces")
COVERAGE = ("primary_members", "secondary_members", "openings", "connections")
DRAWING_TABLES = {"members","connections","anchorage","holddowns","collectors","diaphragms","schedules","bases","chords","frames","framing","gravity_framing","headers","joists","wall_lines","studs","materials","segment_framing","braced_wall_layout","braced_bay_layout"}


def issue(code, message, ids=(), severity="needs_input"):
    return {"code": code, "message": message, "entity_ids": sorted(set(map(str, ids))), "severity": severity}


def number(value, positive=False):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and (not positive or value > 0)


def vector(value):
    return isinstance(value, list) and len(value) == 3 and all(number(v) for v in value)


def norm(value):
    return math.sqrt(sum(v*v for v in value))


def unit(value):
    return vector(value) and abs(norm(value)-1) < 1e-6


def perpendicular(a, b):
    return unit(a) and unit(b) and abs(sum(x*y for x,y in zip(a,b))) < 1e-6


def geometry_errors(g, profiles):
    if not isinstance(g, dict):
        return ["geometry must be an object"]
    kind = g.get("type")
    if kind == "polyline":
        points = g.get("points", [])
        return [] if len(points) >= 2 and all(vector(p) for p in points) and any(p != points[0] for p in points) else ["invalid polyline"]
    errors = [] if vector(g.get("origin")) else ["origin must have three finite coordinates"]
    if kind == "box":
        if not vector(g.get("size")) or not all(x > 0 for x in g["size"]): errors.append("box size must be positive")
        if not perpendicular(g.get("x_axis"), g.get("y_axis")): errors.append("box axes must be orthonormal")
    elif kind in ("cylinder", "extruded_profile"):
        if not number(g.get("length"), True) or not unit(g.get("axis")): errors.append("invalid extrusion axis/length")
        if kind == "cylinder" and not number(g.get("radius"), True): errors.append("radius must be positive")
        if kind == "extruded_profile":
            if profiles.get(g.get("profile_id"),{}).get("status") != "declared": errors.append("unknown or undeclared profile")
            if not perpendicular(g.get("axis"), g.get("local_y")): errors.append("extrusion axes must be orthonormal")
    else:
        errors.append("unknown geometry type")
    return errors


def validate(package):
    """Validate finite geometry, references, coverage and readiness independently.

    Returns findings. The reported ready flag is never trusted by a consumer.
    This is data validation, not a structural capacity or constructability check.
    """
    from jsonschema import Draft202012Validator
    try:
        json.dumps(package,allow_nan=False)
    except (ValueError,TypeError) as ex:
        return [issue("json_value",str(ex),severity="error")]
    schema=json.loads(Path(__file__).with_name("bim_package.schema.json").read_text(encoding="utf-8"))
    structural=list(Draft202012Validator(schema).iter_errors(package))
    if structural:
        return [issue("schema",e.message,["/"+"/".join(map(str,e.absolute_path))],"error") for e in structural]
    errors = []
    def bad(code, msg, ids=(), severity="error"):
        errors.append(issue(code, msg, ids, severity))
    if package.get("schema") != "steltic.bim_package" or package.get("schema_version") != VERSION:
        bad("schema", "Unsupported BIM schema/version")
    if package.get("units") != {"length":"in", "force":"kip", "stress":"ksi"}:
        bad("units", "Canonical units must be in / kip / ksi")
    physical = package.get("physical", {})
    indexes = {}
    for key in COLLECTIONS:
        rows = physical.get(key, [])
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get("id"), str) or not r["id"] for r in rows):
            bad("entity_id", "Invalid collection/ID: " + key)
            indexes[key] = {}
            continue
        indexes[key] = {r["id"]:r for r in rows}
        if len(indexes[key]) != len(rows): bad("duplicate_id", "Duplicate IDs in " + key)
    profiles, materials = indexes["profiles"], indexes["materials"]
    profile_dimensions={"i":("d","bf","tf","tw"),"rhs":("h","b","t"),"chs":("d","t"),
                        "channel":("d","b","lip","t","r"),"z":("d","b","lip","t","r"),
                        "angle":("h","b","t"),"flat":("width","thickness"),"round":("d",),"box_envelope":("width","depth")}
    for p in profiles.values():
        if p.get("status") == "declared":
            dims = p.get("dimensions", {})
            family=p.get("family")
            if family=="built_up":
                if not p.get("components"): bad("profile_geometry","Built-up profile needs constituent profiles and placements",[p["id"]])
                for component in p.get("components",[]):
                    target=profiles.get(component["profile_id"],{})
                    if target.get("status")!="declared" or target.get("family")=="built_up": bad("profile_geometry","Constituents must reference declared non-built-up profiles",[p["id"]])
            elif family not in profile_dimensions or any(not number(dims.get(k)) or (dims[k]<0 if k in ("lip","r") else dims[k]<=0) for k in profile_dimensions[family]):
                bad("profile_geometry", "Declare required dimensions for a supported profile family", [p["id"]])
            if family=="box_envelope" and (p.get("scope")!="delegated" or not p.get("basis")): bad("profile_geometry","An envelope needs delegated scope and its basis",[p["id"]])
            if family in profile_dimensions and all(number(dims.get(k),True) for k in profile_dimensions[family]):
                if ((family=="i" and (2*dims["tf"]>=dims["d"] or dims["tw"]>=dims["bf"])) or
                    (family=="rhs" and 2*dims["t"]>=min(dims["h"],dims["b"])) or
                    (family=="chs" and 2*dims["t"]>=dims["d"])):
                    bad("profile_geometry","Thickness consumes or exceeds the profile envelope",[p["id"]])
    for m in materials.values():
        if not m.get("specification"): bad("material_specification", "Material specification missing", [m["id"]])
    analysis_nodes=package["analysis"]["nodes"]
    node_ids={n["id"] for n in analysis_nodes}
    analysis_elements=package["analysis"]["elements"]
    analysis_ids = {str(e["id"]) for e in analysis_elements}
    if len(node_ids)!=len(analysis_nodes) or len(analysis_ids)!=len(analysis_elements): bad("analysis_ids","Duplicate analysis node/element IDs")
    for e in analysis_elements:
        if any(n not in node_ids for n in e["node_ids"]): bad("analysis_nodes","Analysis element references missing nodes",[e["id"]])
    represented = {}
    portions = {}
    candidates = []
    for m in indexes["members"].values():
        mid = m["id"]
        axis = m.get("axis", [])
        if len(axis) != 2 or not all(vector(p) for p in axis) or axis[0] == axis[1]:
            bad("member_axis", "Invalid member axis", [mid]); continue
        for aid in m.get("analysis_ids", []):
            if aid not in analysis_ids: bad("analysis_reference", "Unknown analysis element", [mid, aid])
            represented.setdefault(aid, []).append(mid)
            portions.setdefault(aid, []).append((0.0, 1.0, mid))
        for segment in m.get("analysis_segments", []):
            aid, a, b = segment["analysis_id"], segment["start"], segment["end"]
            if aid not in analysis_ids: bad("analysis_reference", "Unknown analysis element", [mid, aid])
            if not (0 <= a < b <= 1): bad("analysis_interval", "Analysis segment must be a nonempty fraction within [0,1]", [mid, aid])
            represented.setdefault(aid, []).append(mid)
            portions.setdefault(aid, []).append((a, b, mid))
        if m.get("status") != "declared":
            candidates.append(mid); continue
        direction = [b-a for a,b in zip(*axis)]
        direction = [v/norm(direction) for v in direction]
        if not perpendicular(direction, m.get("local_y")): bad("member_axes", "Member local_y must be perpendicular unit vector", [mid])
        if m.get("profile_id") not in profiles or profiles.get(m.get("profile_id"), {}).get("status") != "declared": bad("member_profile", "Unresolved physical profile", [mid])
        if m.get("material_id") not in materials: bad("member_material", "Unresolved material", [mid])
        if not m.get("reference_line") or not isinstance(m.get("end_offsets"), list) or len(m["end_offsets"]) != 2 or not all(vector(v) for v in m["end_offsets"]):
            bad("member_placement", "Declare reference line and both physical end offsets", [mid])
    if candidates: bad("member_placement", "Confirm physical placement/profile/material of recovered analysis members", candidates, "needs_input")
    for aid, rows in portions.items():
        cursor = 0.0
        for a, b, mid in sorted(rows):
            if a < cursor - 1e-7: bad("analysis_overlap", "Physical members claim overlapping analysis intervals", [aid, mid])
            if a > cursor + 1e-7: bad("analysis_mapping", "Unmapped analysis interval", [aid], "needs_input")
            cursor = max(cursor, b)
        if cursor < 1 - 1e-7: bad("analysis_mapping", "Unmapped analysis interval", [aid], "needs_input")
    excluded = {}
    for row in physical.get("analysis_only", []):
        if not isinstance(row, dict) or row.get("analysis_id") not in analysis_ids or not row.get("reason"):
            bad("analysis_only", "Invalid analysis-only declaration"); continue
        aid = row["analysis_id"]
        if aid in excluded or aid in represented: bad("analysis_overlap", "Duplicate/overlapping analysis-only declaration", [aid])
        excluded[aid] = row
    missing_analysis = analysis_ids - set(represented) - set(excluded)
    if missing_analysis: bad("analysis_mapping", "Map or explicitly classify every analysis element", missing_analysis, "needs_input")
    panel_ids = indexes["panels"]
    for group in ("panels", "openings"):
        for p in indexes[group].values():
            points = p.get("vertices", [])
            if len(points) < 3 or not all(vector(v) for v in points): bad("panel_geometry", "Invalid boundary vertices", [p["id"]])
            elif len({tuple(v) for v in points}) < 3: bad("panel_geometry", "Degenerate boundary", [p["id"]])
            else:
                a=[x-y for x,y in zip(points[1],points[0])]
                normals=[]
                for v in points[2:]:
                    b=[x-y for x,y in zip(v,points[0])]
                    normal=[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]
                    if norm(normal)>1e-8: normals.append(normal)
                if not normals: bad("panel_geometry","Collinear boundary",[p["id"]])
                elif any(abs(sum(n*(x-y) for n,x,y in zip(normals[0],v,points[0])))/norm(normals[0])>1e-6 for v in points): bad("panel_geometry","Nonplanar boundary",[p["id"]])
            if group == "panels":
                if p.get("status") != "declared": bad("panel_placement", "Confirm recovered panel assembly and placement", [p["id"]], "needs_input")
                elif not number(p.get("thickness"), True) or p.get("material_id") not in materials: bad("panel_specification", "Panel thickness/material missing", [p["id"]])
            elif p.get("host_id") not in panel_ids and p.get("host_id") not in indexes["members"]:
                bad("opening_host", "Unknown opening host", [p["id"]])
    ends = {}
    for j in indexes["joints"].values():
        if not vector(j.get("point")) or not (j.get("member_ends") or j.get("attachments")): bad("joint_geometry", "Joint needs a point and member ends or support attachments", [j["id"]])
        for end in j.get("member_ends", []):
            key = (end.get("member_id"), end.get("end"))
            if key[0] not in indexes["members"] or key[1] not in ("start", "end"): bad("joint_member", "Invalid joint member/end", [j["id"]])
            else:
                member=indexes["members"][key[0]]
                endpoint=member["axis"][0 if key[1]=="start" else 1]
                offset=end.get("joint_offset",[0,0,0])
                if norm([p-a-o for p,a,o in zip(j["point"],endpoint,offset)])>1e-6:
                    bad("joint_placement","Joint point must match the member reference end plus its explicit joint_offset",[j["id"],key[0]])
            if key in ends: bad("duplicate_joint_end", "Member end assigned to multiple joints", [j["id"], ends[key]])
            ends[key] = j["id"]
        for attachment in j.get("attachments", []):
            mid, t = attachment["member_id"], attachment["station"]
            if mid not in indexes["members"] or not 0 <= t <= 1:
                bad("joint_attachment", "Support attachment must lie on a known member", [j["id"], mid]); continue
            a, b = indexes["members"][mid]["axis"]
            offset = attachment.get("offset", [0,0,0])
            expected = [x + t*(y-x) + o for x,y,o in zip(a,b,offset)]
            if norm([p-q for p,q in zip(j["point"],expected)]) > 1e-6:
                bad("joint_attachment", "Joint does not match its declared support station and offset", [j["id"], mid])
    records = {r["id"]:r for r in package.get("design_records", [])}
    if len(records)!=len(package["design_records"]): bad("design_record_ids","Duplicate design record IDs")
    linked = set(); joined = set()
    for collection in COLLECTIONS:
        if collection=="connections": continue
        for entity in indexes[collection].values():
            refs=entity.get("design_record_ids",[])
            if not isinstance(refs,list) or any(not isinstance(ref,str) or ref not in records for ref in refs): bad("entity_design","Physical entity has invalid design record references",[entity["id"]])
            else: linked.update(refs)
    for c in indexes["connections"].values():
        cid = c["id"]
        if c.get("joint_id") not in indexes["joints"]: bad("connection_joint", "Connection has unknown joint", [cid])
        else: joined.add(c["joint_id"])
        refs = c.get("design_record_ids", [])
        if not refs or any(ref not in records for ref in refs): bad("connection_design", "Connection needs valid design record IDs", [cid])
        linked.update(refs)
        parts = c.get("parts", [])
        detail = c.get("detail_ref", {})
        if c.get("scope") == "delegated":
            if not c.get("responsible_party") or not detail.get("id") or not detail.get("revision") or not c.get("representation"):
                bad("delegated_interface", "Delegated connection needs party, versioned detail and representation", [cid])
        elif not parts and not (detail.get("id") and detail.get("revision")):
            bad("connection_geometry", "Connection needs parts or a versioned design-detail reference", [cid])
        part_ids = set()
        for part in parts:
            if not isinstance(part, dict) or not part.get("id") or part["id"] in part_ids:
                bad("connection_part", "Invalid/duplicate connection part ID", [cid]); continue
            part_ids.add(part["id"])
            if not part.get("kind") or part.get("material_id") not in materials: bad("connection_part", "Part kind/material missing", [cid, part["id"]])
            for msg in geometry_errors(part.get("geometry"), profiles): bad("connection_geometry", msg, [cid, part["id"]])
    dispositions = {}
    for d in physical.get("record_dispositions", []):
        rid = d.get("record_id")
        if rid not in records or rid in dispositions or not d.get("reason") or d.get("status") not in ("not_applicable", "delegated", "covered_by_assembly"):
            bad("record_disposition", "Invalid source-record disposition", [str(rid)])
        dispositions[rid] = d
        if d.get("status")=="delegated" and d.get("interface_id") not in indexes["interfaces"]: bad("record_disposition","Delegation needs a declared interface",[str(rid)])
        if d.get("status")=="covered_by_assembly" and (not d.get("entity_ids") or any(not any(e in indexes[k] for k in COLLECTIONS) for e in d["entity_ids"])): bad("record_disposition","Assembly coverage needs physical entity references",[str(rid)])
    unlinked = [r["id"] for r in records.values() if r["table"] == "connections" and not r.get("waived") and r["id"] not in linked and r["id"] not in dispositions]
    if unlinked: bad("connection_mapping", "Assign each connection record to occurrences or document its disposition", unlinked, "needs_input")
    unassigned=[r["id"] for r in records.values() if r["table"] in DRAWING_TABLES-{"connections"} and not r.get("waived") and r["id"] not in linked and r["id"] not in dispositions]
    if unassigned: bad("assembly_mapping","Assign drawing-related member/schedule records to physical entities or documented scope",unassigned,"needs_input")
    unjoined = [m["id"]+":"+end for m in indexes["members"].values() if m.get("status") == "declared" for end in ("start", "end") if (m["id"],end) not in ends]
    if unjoined: bad("member_end_mapping", "Assign physical member ends to joints", unjoined, "needs_input")
    free_joints = set(indexes["joints"]) - joined
    exemptions = physical.get("joint_dispositions", [])
    for d in exemptions:
        if d.get("joint_id") not in indexes["joints"] or not d.get("reason"): bad("joint_disposition", "Invalid free/continuous joint declaration")
        free_joints.discard(d.get("joint_id"))
    if free_joints: bad("joint_connection", "Provide connections or free/continuous-joint reasons", free_joints, "needs_input")
    coverage = physical.get("coverage", {})
    for key in COVERAGE:
        item = coverage.get(key, {})
        if not isinstance(item, dict) or item.get("complete") is not True or not item.get("basis"):
            bad("coverage_"+key, "Declare and substantiate completeness of "+key, severity="needs_input")
    if not indexes["members"] and not indexes["panels"]: bad("empty_model", "No physical members or panels", severity="needs_input")
    for item in package.get("review", {}).get("items", []):
        if item["status"] != "resolved":
            bad("coordination_required", item["title"], item["entity_ids"] or [item["id"]], "needs_input")
    for supplied in package.get("source_findings", []):
        errors.append(supplied)
    combined={}
    for finding in errors:
        key=(finding["code"],finding["message"],finding["severity"])
        if key not in combined: combined[key]=dict(finding,entity_ids=[])
        combined[key]["entity_ids"].extend(finding["entity_ids"])
    return [dict(f,entity_ids=sorted(set(f["entity_ids"]))) for f in combined.values()]

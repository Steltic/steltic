"""Locate original collector-to-frame selections on their actual member and receiving support."""
import re


def member_sources(rows):
    result = {}
    for row in rows:
        data = row.get("data", {})
        inputs = data.get("inputs") or {}
        if row.get("table") != "members" or not isinstance(inputs, dict) or not inputs.get("role") or not inputs.get("section"):
            continue
        item = {"role": inputs["role"], "section": inputs["section"], "pointer": row["source_pointer"] + "/inputs"}
        if row["id"].startswith("members/collector-"):
            item["collector_id_pointer"] = row["source_pointer"] + "/id"
        if re.match(r"^SMF column\b", str(data.get("design_note", "")), re.I):
            item["support"] = "smf_column"
            item["support_pointer"] = row["source_pointer"] + "/design_note"
        elif re.search(r"\bVBE\b", str(data.get("design_note", "")), re.I) and inputs["role"] == "lateral_col":
            item["support"] = "vbe"
            item["support_pointer"] = row["source_pointer"] + "/design_note"
        result[row["id"]] = item
    return result


def receiver_target(row):
    data = row["data"]
    text = str(data.get("type", ""))
    schedule = _schedule_target(row)
    if schedule:
        return schedule
    chord_end = re.match(r"^collector / chord beam end on the ([XY]) frame lines outside the braced bays \((floor|roof)\)", text, re.I)
    collector_end = re.match(r"^collector beam-to-column connection \(single plate\), gravity shear \+ collector axial\b", text, re.I)
    if chord_end or collector_end:
        named = re.search(r"conn-collector-(?:beam-)?([XY])-(floor|roof)-(W[\dX.]+)$", row["id"], re.I)
        if named:
            return {"role": "collector_beam_end", "direction": named[1], "outside_braced_bay": bool(chord_end),
                    "source_member_prefix": f"members/collector-{named[1]}-{named[2]}-{named[3]}",
                    "source": {"pointer": row["source_pointer"] + "/type", "literal": text}}
    brace = re.search(r"conn-bracedbay-beam-([XY])-(HSS[\dX./-]+)$", row["id"], re.I)
    combined = str((data.get("demand") or {}).get("basis", ""))
    if brace and re.match(r"^braced-bay beam-to-column connection", text, re.I) and re.search(r"\bUFM\b.*\bcollector\b", combined, re.I):
        return {"role": "combined_braced_bay", "direction": brace[1].upper(), "brace_section": brace[2].upper(),
                "source": {"pointer": row["source_pointer"] + "/demand/basis", "literal": combined},
                "scope_source": {"pointer": row["source_pointer"] + "/id", "literal": data["id"]}}
    patterns = [("smf_column", r"^collector \(drag strut\) to SMF column\b"),
                ("vbe", r"^collector-to-VBE connection\b"),
                ("core_wall", r"^collector-to-core-wall connection\b"),
                ("reentrant_frame", r"^collector connections at the re-entrant corner\b")]
    for target, pattern in patterns:
        if not re.search(pattern, text, re.I):
            continue
        result = {"role": target, "source": {"pointer": row["source_pointer"] + "/type", "literal": text}}
        direction = re.search(r"conn-collector-([XY])-(floor|roof)(?:-|$)", row["id"], re.I)
        if direction:
            result["source_member_prefix"] = f"members/collector-{direction[1].upper()}-{direction[2].lower()}"
            result["direction"] = direction[1].upper()
        if target == "reentrant_frame":
            result["lines"] = []
            for line, values in (data.get("demand", {}).get("lines") or {}).items():
                scope = re.match(r"Grid ([A-Z0-9]+) at Grids ([A-Z0-9]+)-([A-Z0-9]+) into ([XY])(\d+)", line, re.I)
                if not scope or not isinstance(values, dict):
                    continue
                levels = re.fullmatch(r"(\d+)-(\d+)", str(values.get("levels", "")))
                if levels:
                    result["lines"].append({"grid": scope[1], "first": scope[2], "last": scope[3], "direction": scope[4],
                                            "frame": scope[4] + scope[5], "levels": list(range(int(levels[1]), int(levels[2])+1)),
                                            "source": {"pointer": row["source_pointer"] + "/demand/lines/" +
                                            line.replace("~", "~0").replace("/", "~1") + "/levels", "literal": values["levels"]}})
        return result
    return None


def _schedule_target(row):
    data = row["data"]
    text = str(data.get("type", ""))
    basis = str(data.get("design_basis", ""))
    components = str(data.get("components", ""))
    places = []
    pointer, literal = row["source_pointer"] + "/type", text
    level_line = re.match(r"^Level-(\d+) collector Grid ([A-Z0-9]+)\b(.*?) to column:", text, re.I)
    continuation = re.match(r"^collector continuation beyond the opening corner, Grids (\w+) / (\w+), bays "
                            r"(\w+)-(\w+) and (\w+)-(\w+) \(([XY]) loading\): (W[\dX.]+) end ", text, re.I)
    corner = re.match(r"^collector connections at the (?:courtyard re-entrant corners|eight inside-corner grid segments)\b", text, re.I)
    grid6 = re.search(r"collector girders on Grid (\w+) to the braced-frame column at Grid (\w+); "
                      r"the governing connection is the Level-(\d+) (W[\dX.]+) girder", basis, re.I)
    neck = re.search(r"collector lines = Grids (\w+) and (\w+) from Grid (\w+) to Grid (\w+) \(through the neck\), "
                     r"levels (\d+) and (\d+): the (W[\dX.]+) floor girders \(Grid (\w+), Grids (\w+)-(\w+); "
                     r"Grid (\w+), Grids (\w+)-(\w+) and, at level (\d+), Grids (\w+)-(\w+)\), "
                     r"the (W[\dX.]+) link-roof girder \(Grid (\w+), Grids (\w+)-(\w+), level (\d+)\)", components, re.I)
    all_lines = re.search(r"one detail at every collector-beam end of grids (\w+), (\w+) and (\w+), floors and roof", basis, re.I)
    direction = re.search(r"conn-collector-([XY])-lines$", row["id"], re.I)
    podium = re.match(r"collector connections at the podium setback \(L(\d+), podium-only bay to the Tower [A-Z] frames on Grids (\w+), (\w+) and (\w+)\)", text, re.I)
    if all_lines and direction:
        return {"role": "scheduled_collector", "places": [{"direction": direction[1], "line": grid}
                 for grid in all_lines.groups()], "source": {"pointer": row["source_pointer"]+"/design_basis", "literal": all_lines.group()}}
    if podium:
        level, a, b, number = podium.groups()
        return {"role": "scheduled_collector", "requires_collector": True,
                "places": [{"line": v, "direction": "X", "levels": [int(level)]} for v in (a,b)]+
                          [{"line": number, "direction": "Y", "levels": [int(level)]}],
                "source": {"pointer": row["source_pointer"]+"/type","literal":podium.group()}}
    if level_line:
        p = {"line": level_line[2], "levels": [int(level_line[1])]}
        bay = re.search(r"\(([A-Z]+)(\d+)-\1(\d+)", level_line[3], re.I)
        if bay:
            p["bay"] = [bay[2], bay[3]]
        places.append(p)
    elif continuation:
        for grid in (continuation[1], continuation[2]):
            for bay in ([continuation[3], continuation[4]], [continuation[5], continuation[6]]):
                places.append({"line": grid, "bay": bay, "direction": continuation[7], "section": continuation[8]})
    elif corner:
        pair = re.search(r"\(Grid (\w+) at Grids (\w+) and (\w+);", text, re.I)
        quad = re.search(r"\(Grids (\w+) and (\w+) at (\w+) and (\w+);", text, re.I)
        levels = [v.get("levels") for v in data.get("demand", {}).get("lines", {}).values() if isinstance(v, dict)]
        same = next(iter(set(levels)), None) if len(set(levels)) == 1 else None
        limits = re.fullmatch(r"(\d+)-(\d+)", str(same))
        if limits:
            grid_pairs = ([[pair[1], pair[2]], [pair[1], pair[3]]] if pair else
                          [[a, b] for a in (quad[1], quad[2]) for b in (quad[3], quad[4])] if quad else [])
            places = [{"grids": x, "levels": list(range(int(limits[1]), int(limits[2])+1))} for x in grid_pairs]
    elif grid6:
        places = [{"grids": [grid6[1], grid6[2]], "levels": [int(grid6[3])], "section": grid6[4]}]
        pointer, literal = row["source_pointer"] + "/design_basis", grid6.group()
    elif neck:
        levels = [int(neck[5]), int(neck[6])]
        places = [{"line": neck[8], "bay": [neck[9], neck[10]], "levels": levels, "section": neck[7]},
                  {"line": neck[11], "bay": [neck[12], neck[13]], "levels": levels, "section": neck[7]},
                  {"line": neck[11], "bay": [neck[15], neck[16]], "levels": [int(neck[14])], "section": neck[7]},
                  {"line": neck[18], "bay": [neck[19], neck[20]], "levels": [int(neck[21])], "section": neck[17]}]
        pointer, literal = row["source_pointer"] + "/components", neck.group()
    if places:
        return {"role": "scheduled_collector", "places": places, "source": {"pointer": pointer, "literal": literal}}
    return None


def _in_place(place, site, ends):
    if "grids" in place and not set(place["grids"]) <= set(site.get("grids", [])):
        return False
    if "levels" in place and not set(place["levels"]).intersection(site.get("levels", [])):
        return False
    for m in ends.values():
        if any(place.get(key) and place[key] != m.get(key) for key in ("direction", "section")):
            continue
        axes = m.get("axis_grids", [[], []])
        if "line" in place and not all(place["line"] in grids for grids in axes):
            continue
        if "bay" in place:
            a, b = place["bay"]
            if not ((a in axes[0] and b in axes[1]) or (b in axes[0] and a in axes[1])):
                continue
        return True
    return False


def receiver_application(target, location, catalog):
    site = location or {}
    ends = {mid: m for mid, m in site.get("end_members", {}).items() if m.get("kind") == "beam"}
    columns = site.get("columns", {})
    prefix = target.get("source_member_prefix", "members/collector-")
    candidates = {rid: value for rid, value in catalog.items() if (value["role"] == "collector" or value.get("collector_id_pointer")) and
                  (rid == prefix or rid.startswith(prefix if prefix.endswith("-") else prefix + "-"))}
    direct = {rid for m in ends.values() for rid in m.get("source_ids", []) if rid in candidates and
              candidates[rid]["section"] == m["section"] and (not target.get("direction") or m.get("direction") == target["direction"])}
    different_section = candidates and ends and not any(
        m["section"] == value["section"] for m in ends.values() for value in candidates.values())
    supports = {rid: catalog[rid] for m in columns.values() for rid in m.get("source_ids", [])
                if rid in catalog and catalog[rid]["section"] == m["section"]}
    role = target["role"]
    status = "unlocated_interface"
    if role == "collector_beam_end":
        if target["outside_braced_bay"] and set(ends).intersection(site.get("braced_member_ids", [])):
            status = "different_receiver"
        elif direct:
            status = "located_interface"
        elif different_section:
            status = "different_member_section"
    elif role == "scheduled_collector":
        if ends and site.get("grids") and site.get("levels"):
            status = "located_interface" if any(_in_place(p, site, ends) for p in target["places"]) else "different_location"
    elif role == "combined_braced_bay":
        joints = site.get("corner_bearings", [])
        matched = [b for b in joints if b["direction"] == target["direction"] and b["section"] == target["brace_section"]
                   and set(b["member_ids"]).intersection(ends)]
        if matched:
            status = "located_interface"
        elif ends:
            status = "different_receiver"
    elif role == "reentrant_frame":
        for direction in {m.get("direction") for m in ends.values()}:
            for line in target.get("lines", []):
                if direction != line["direction"] or line["grid"] not in site.get("grids", []) or not set(line["levels"]).intersection(site.get("levels", [])):
                    continue
                label_order = next((axis for axis in site.get("grid_axes", []) if line["first"] in axis and line["last"] in axis), [])
                if not label_order:
                    continue
                first, last = sorted([label_order.index(line["first"]), label_order.index(line["last"])])
                inside_run = set(label_order[first:last+1]).intersection(site.get("grids", []))
                if inside_run and direction in site.get("brace_frame_directions", []):
                    status = "located_interface"
        if site.get("grids") and site.get("levels") and target.get("lines") and status != "located_interface":
            status = "different_location"
    elif role == "core_wall":
        if site.get("wall_receivers"):
            status = "located_interface"
        elif columns and ends:
            status = "different_receiver"
    elif different_section:
        status = "different_member_section"
    elif any(v.get("support") == role for v in supports.values()) and direct:
        status = "located_interface"
    elif supports and all(v["role"] == "gravity_col" for v in supports.values()):
        status = "different_receiver"
    return {"role": role, "status": status, "point_in": site.get("point_in"), "grids": site.get("grids", []),
            "levels": site.get("levels", []), "bound_beam_sections": {mid: m["section"] for mid, m in ends.items()},
            "matching_member_source_ids": sorted(direct), "source_collector_sections": sorted({v["section"] for v in candidates.values()}),
            "receiving_column_source_ids": sorted(supports), "brace_frame_directions": site.get("brace_frame_directions", [])}

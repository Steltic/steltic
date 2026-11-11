"""Locate explicitly selected flat-bar crossing connections on native member axes."""
from collections import defaultdict
from itertools import combinations
import math

from .bim_source_fields import digest_value


def _intersection(left, right):
    a, b = left
    c, d = right
    u, v, w = ([j-i for i, j in zip(a, b)], [j-i for i, j in zip(c, d)], [i-j for i, j in zip(a, c)])
    aa = sum(x*x for x in u)
    ab = sum(x*y for x, y in zip(u, v))
    bb = sum(x*x for x in v)
    aw = sum(x*y for x, y in zip(u, w))
    bw = sum(x*y for x, y in zip(v, w))
    det = aa*bb - ab*ab
    if det <= 1e-12*aa*bb:
        return None
    s, t = (ab*bw - bb*aw)/det, (aa*bw - ab*aw)/det
    if not (1e-7 < s < 1-1e-7 and 1e-7 < t < 1-1e-7):
        return None
    p, q = [i+s*j for i, j in zip(a, u)], [i+t*j for i, j in zip(c, v)]
    if math.dist(p, q) > 1e-5:
        return None
    return [(i+j)/2 for i, j in zip(p, q)], [s, t]


def crossing_interfaces(records, physical):
    fields = {rid: [i for i, f in enumerate(r["fields"]) if f["role"] == "flat_bar_crossing_fillet"]
              for rid, r in records.items()}
    fields = {rid: indexes for rid, indexes in fields.items() if indexes}
    if not fields:
        return [], []
    flat = {p["id"] for p in physical.get("profiles", []) if p.get("family") == "flat"}
    members = {m["id"]: m["axis"] for m in physical.get("members", []) if m.get("kind") == "truss_web" and
               m.get("profile_id") in flat and len(m.get("axis", [])) == 2}
    ends = defaultdict(lambda: defaultdict(set))
    existing = defaultdict(list)
    for connection in physical["connections"]:
        point = connection.get("point")
        if not point:
            continue
        member_ids = set(connection.get("member_ids", []))
        point_key = tuple(round(v, 5) for v in point)
        existing[point_key].append((connection["id"], member_ids))
        for rid in fields.keys() & set(connection.get("design_record_ids", [])):
            for mid in member_ids & members.keys():
                if min(math.dist(point, end) for end in members[mid]) < 1e-5:
                    ends[rid][mid].add(connection["id"])
    interfaces, unresolved = [], []
    for rid, indexes in sorted(fields.items()):
        candidates = ends[rid]
        matched = set()
        for a, b in combinations(sorted(candidates), 2):
            hit = _intersection(members[a], members[b])
            if hit is None:
                continue
            point, stations = hit
            point_key = tuple(round(v, 5) for v in point)
            actual = sorted(cid for cid, mids in existing[point_key] if {a, b} <= mids)
            interfaces.append({"id": "hr:source-crossing:" + digest_value([rid, a, b])[:20], "kind": "flat_bar_crossing",
                               "record_id": rid, "selected": indexes, "member_ids": [a, b], "member_stations": stations,
                               "point_in": point, "source_end_connection_ids": sorted(candidates[a] | candidates[b]),
                               "native_connection_ids": actual, "status": "native_connection" if actual else "requires_openbim_geometry"})
            matched.update((a, b))
        missing = sorted(set(candidates) - matched)
        if not candidates or missing:
            unresolved.append({"record_id": rid, "role": "flat_bar_crossing", "selected": indexes, "unmatched_member_ids": missing,
                               "reason": "Source crossing selection lacks a pair of intersecting bound flat bars"})
    return interfaces, unresolved

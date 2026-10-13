"""Building description -> HR cfg.py, one for one. Nothing is sized, nothing is added.

The cfg carries the user's members as literal tables (NODES, COLUMNS, BEAMS, BRACES) and a custom_build that
replays them with the engine's own helpers (add_column / add_beam / Truss, rigidDiaphragm, attach_diaphragm_mass).
What the translator DERIVES (never invents) is listed in the returned provenance with the rule that produced it:
  * grid indices and node tags (grid intersections -> ntag(i, j, k); any other point -> k*100000 + 90000 + n);
  * a grid line through every column coordinate that is not on a declared grid line ("x=12.5ft");
  * column segments split at every level where a member frames into the column; beams split where another
    member's end lands inside their span (chevron work points, link ends); end releases kept on the outer ends;
  * the floor plate per level = the bays whose four corner grid points are framed at that level;
  * drift limit (ASCE 7-22 Table 12.12-1 "all other structures" by Risk Category), Cu (Table 12.8-1),
    Ct and x (Table 12.8-2 for the declared system), Kd = 0.85 and GCpi by enclosure (wind).
"""
from __future__ import annotations

import hashlib
import json
import math
import pprint

from . import schema as S

FT = 12.0
TOL = 1e-4          # ft
OFFGRID0 = 90000    # off-grid node tags: k*100000 + 90000 + n (n < 9998; mtag/cmtag stay free)


class TranslateError(Exception):
    pass


def _num0(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _r(v):
    return round(float(v), 4)


def _hss_or_area(lab):
    return S.shapes()[lab.upper()]


def build_tables(desc) -> dict:
    """Resolve the description into model tables (inches). Raises TranslateError when it is not ready."""
    v = S.validate(desc)
    if not v["ready"]:
        raise TranslateError("description not ready: %d key, %d default, %d refused item(s) open"
                             % (v["counts"]["key"], v["counts"]["default"], v["counts"]["refused"]))
    G = S.Geometry(desc)
    derived = []
    levels = G.levels
    zbase = levels[0][1]
    zin = [(z - zbase) * FT for _, z in levels]
    lidx = {n: k for k, (n, _) in enumerate(levels)}

    cols = [c for c in desc.get("columns") or [] if isinstance(c, dict)]
    beams = [b for b in desc.get("beams") or [] if isinstance(b, dict)]
    braces = [b for b in desc.get("braces") or [] if isinstance(b, dict)]

    # ---- grid: declared lines + a derived line through every column coordinate off the grid
    def lines(ax):
        tab = G.gx if ax == "x" else G.gy
        pts = sorted({_r(c) for c in tab.values()})
        labels = {}
        for lab, c in tab.items():
            labels.setdefault(_r(c), lab)
        for c in cols:
            xy = G.xy(c)
            if xy is None:
                continue
            q = _r(xy[0] if ax == "x" else xy[1])
            if all(abs(q - p) > TOL for p in pts):
                pts.append(q); pts.sort()
                labels[q] = "%s=%gft" % (ax, q)
                derived.append(dict(what="grid line %s=%g ft" % (ax, q), rule="a column stands at this coordinate, off the declared %s grid lines" % ax.upper()))
        return pts, labels
    xs, xlab = lines("x")
    ys, ylab = lines("y")
    if len(xs) > 99 or len(ys) > 99:
        raise TranslateError("more than 99 grid lines in one direction after adding the column lines")

    def gidx(val, arr):
        for i, p in enumerate(arr):
            if abs(val - p) <= TOL:
                return i
        return None

    # ---- nodes
    nodes = {}                    # tag -> (x_in, y_in, z_in)
    key2tag = {}                  # (x_ft, y_ft, k) -> tag
    offgrid_n = {}

    def node(x, y, k):
        key = (_r(x), _r(y), k)
        t = key2tag.get(key)
        if t is not None:
            return t
        i, j = gidx(_r(x), xs), gidx(_r(y), ys)
        if i is not None and j is not None:
            t = k * 100000 + i * 100 + j
        else:
            n = offgrid_n.get(k, 0)
            if n >= 9998:
                raise TranslateError("level %d: more than 9998 off-grid points" % k)
            offgrid_n[k] = n + 1
            t = k * 100000 + OFFGRID0 + n
        key2tag[key] = t
        nodes[t] = (round((_r(x) - xs[0]) * FT, 4), round((_r(y) - ys[0]) * FT, 4), zin[k])
        return t

    # points where something frames in, per level (plan coords)
    framing = {}                  # k -> set((x, y))
    for b in beams:
        k = lidx[str(b["level"])]
        for e in ("i", "j"):
            framing.setdefault(k, set()).add(tuple(map(_r, G.xy(b[e]))))
    for br in braces:
        for e in ("i", "j"):
            k = lidx[str(br[e]["level"])]
            framing.setdefault(k, set()).add(tuple(map(_r, G.xy(br[e]))))

    # ---- column stacks: a joint between two segments of one column that nothing frames into and that the user
    # took out of the diaphragm (an ETABS story joint of a column passing a level) joins them into one member,
    # when section, orientation and releases allow it
    excl_pts = set()
    for e in desc.get("diaphragm_exclude") or []:
        xy = G.xy(e); k = lidx.get(str(e.get("level")))
        if xy is not None and k is not None:
            excl_pts.add((_r(xy[0]), _r(xy[1]), k))
    stacks = {}
    for c in cols:
        stacks.setdefault(tuple(map(_r, G.xy(c))), []).append(c)
    merged = []
    for (x, y), segs in stacks.items():
        segs = sorted(segs, key=lambda c: lidx[str(c["base_level"])])
        cur = dict(segs[0])
        for nxt in segs[1:]:
            kj = lidx[str(nxt["base_level"])]
            if (lidx[str(cur["top_level"])] == kj and (x, y, kj) in excl_pts and (x, y) not in framing.get(kj, set())
                    and S.resolve_section(desc, cur["section"]) == S.resolve_section(desc, nxt["section"])
                    and str(cur.get("strong_axis")) == str(nxt.get("strong_axis"))
                    and str(cur.get("release") or "none") in ("none", "base") and str(nxt.get("release") or "none") in ("none", "top")):
                rel = {("none", "none"): "none", ("base", "none"): "base", ("none", "top"): "top", ("base", "top"): "both"}[
                    (str(cur.get("release") or "none"), str(nxt.get("release") or "none"))]
                derived.append(dict(what="column %s and %s joined through %s" % (cur["id"], nxt["id"], levels[kj][0]),
                                    rule="one column passing a level where nothing frames in and the joint is out of the diaphragm"))
                cur = dict(cur, top_level=nxt["top_level"], release=rel, id=cur["id"])
            else:
                merged.append(cur)
                cur = dict(nxt)
        merged.append(cur)
    cols = merged

    # ---- columns, split at framed levels
    COLS = []
    col_at = {}                   # (x, y) -> [(k0, k1)]
    et = 1
    for c in cols:
        x, y = map(_r, G.xy(c))
        k0, k1 = lidx[str(c["base_level"])], lidx[str(c["top_level"])]
        cuts = [k0] + [k for k in range(k0 + 1, k1) if (x, y) in framing.get(k, set())] + [k1]
        skipped = [levels[k][0] for k in range(k0 + 1, k1) if k not in cuts]
        if skipped:
            derived.append(dict(what="column %s continuous through %s" % (c["id"], ", ".join(skipped)),
                                rule="no member frames into it at those levels"))
        lab = S.resolve_section(desc, c["section"])
        sd = str(c.get("strong_axis") or "X").upper()
        crel = str(c.get("release") or "none").lower()
        nseg = len(cuts) - 1
        for s_, (a, b) in enumerate(zip(cuts, cuts[1:])):
            ri = crel in ("base", "both") and s_ == 0
            rj = crel in ("top", "both") and s_ == nseg - 1
            rel = "both" if (ri and rj) else ("I" if ri else ("J" if rj else "none"))
            COLS.append((et, node(x, y, a), node(x, y, b), lab, sd, c["id"], rel))
            et += 1
        col_at.setdefault((x, y), []).append((k0, k1))

    # ---- beams, split where another member's end lands inside the span
    BEAMS = []
    rel = {"none": "none", "i": "I", "j": "J", "both": "both"}
    link_pairs = []
    rbs_by_sec = {}
    for b in beams:
        k = lidx[str(b["level"])]
        a, c2 = tuple(map(_r, G.xy(b["i"]))), tuple(map(_r, G.xy(b["j"])))
        along = 0 if abs(a[1] - c2[1]) < TOL else 1
        lo, hi = (a, c2)
        inner = sorted({p for p in framing.get(k, set()) | {q for q in col_at if any(k0 <= k <= k1 for k0, k1 in col_at[q])}
                        if abs(p[1 - along] - a[1 - along]) < TOL and
                        min(lo[along], hi[along]) + TOL < p[along] < max(lo[along], hi[along]) - TOL},
                       key=lambda p: abs(p[along] - a[along]))
        pts = [a] + inner + [c2]
        r = rel[str(b.get("release")).strip().lower()]
        ri = "I" if r in ("I", "both") else None
        rj = "J" if r in ("J", "both") else None
        lab = S.resolve_section(desc, b["section"])
        nseg = len(pts) - 1
        for s, (p, q) in enumerate(zip(pts, pts[1:])):
            ends = []
            if s == 0 and ri:
                ends.append("I")
            if s == nseg - 1 and rj:
                ends.append("J")
            relz = "both" if len(ends) == 2 else (ends[0] if ends else "none")
            n1, n2 = node(p[0], p[1], k), node(q[0], q[1], k)
            BEAMS.append((et, n1, n2, lab, relz, "none", b["id"]))
            if b.get("link"):
                link_pairs.append((n1, n2))
            et += 1
        if nseg > 1:
            derived.append(dict(what="beam %s split into %d segments" % (b["id"], nseg),
                                rule="member ends land inside its span (work points)"))
        if isinstance(b.get("rbs"), dict):
            rbs_by_sec.setdefault(lab, dict(a_in=float(b["rbs"]["a_in"]), b_in=float(b["rbs"]["b_in"]),
                                            c_in=float(b["rbs"]["c_in"]), beams=[]))["beams"].append(b["id"])

    # ---- braces
    BRACES = []
    se = desc.get("seismic") or {}
    sysd = se.get("system") or {}
    sysk = {d: S.norm_system(sysd.get(d) if isinstance(sysd, dict) else sysd) for d in ("X", "Y")}
    brb_asc = {}
    brace_sys = {}
    brace_len = {}
    mat = desc.get("materials") or {}
    KF = float(((mat.get("brb") or {}).get("KF")) or 1.0)
    for br in braces:
        ka, kb = lidx[str(br["i"]["level"])], lidx[str(br["j"]["level"])]
        pa, pb = tuple(map(_r, G.xy(br["i"]))), tuple(map(_r, G.xy(br["j"])))
        n1, n2 = node(pa[0], pa[1], ka), node(pb[0], pb[1], kb)
        dx, dy = abs(pb[0] - pa[0]), abs(pb[1] - pa[1])
        dirn = "X" if dx >= dy else "Y"
        if isinstance(br.get("brb"), dict):
            asc = float(br["brb"]["Asc_in2"])
            lab = S.brb_label(asc); brb_asc[lab] = asc
            area = KF * asc
            bsys = "BRBF"
        else:
            lab = S.resolve_section(desc, br["section"])
            area = float(_hss_or_area(lab)["A"])
            bsys = (S.norm_system(br["system"]) if br.get("system") else S.braced_system(sysk.get(dirn)) or
                    S.braced_system(sysk.get("Y" if dirn == "X" else "X")))
        if lab in brace_sys and brace_sys[lab] != bsys:
            raise TranslateError("section %s is used by braces of two systems (%s, %s): version 1 keys the brace system "
                                 "by section" % (lab, brace_sys[lab], bsys))
        brace_sys[lab] = bsys
        if br.get("length_factor") is not None:
            L = math.dist(nodes[n1], nodes[n2])
            brace_len.setdefault(lab, []).append((float(br["length_factor"]) * L, float(br["length_factor"])))
        BRACES.append((et, n1, n2, lab, area, br["id"]))
        et += 1

    # ---- supports
    sup = desc.get("supports") or {}
    fixity = {}
    dflt = str(sup.get("default") or "").lower()
    for a in sup.get("at") or []:
        xy = G.xy(a)
        if xy:
            fixity[tuple(map(_r, xy))] = str(a["fixity"]).lower()
    FIX = {}
    for t, (x, y, z) in nodes.items():
        if t // 100000 != 0:
            continue
        f = fixity.get((_r(x / FT + xs[0]), _r(y / FT + ys[0])), dflt or "pinned")
        FIX[t] = (1, 1, 1, 1, 1, 1) if f == "fixed" else (1, 1, 1, 0, 0, 0)

    # ---- per level: grid nodes (present / diaphragm) and the floor plate
    NF = len(levels) - 1
    present = {}
    for t in nodes:
        k, rem = divmod(t, 100000)
        if rem < OFFGRID0:
            present.setdefault(k, set()).add((rem // 100, rem % 100))
    # diaphragm membership: every point at a level (as ETABS constrains a story's joints), except the points the
    # user excludes and -- unless the user puts them in -- the EBF link ends
    excl = set()
    for e in desc.get("diaphragm_exclude") or []:
        xy = G.xy(e); k = lidx.get(str(e.get("level")))
        if xy is not None and k is not None:
            t = key2tag.get((_r(xy[0]), _r(xy[1]), k))
            if t is not None:
                excl.add(t)
    if link_pairs and not (desc.get("details") or {}).get("link_ends_in_diaphragm"):
        for a, b in link_pairs:
            excl |= {a, b}
    diaph = {}
    for t in sorted(nodes):
        k, rem = divmod(t, 100000)
        if k >= 1 and t not in excl:
            diaph.setdefault(k, []).append(t)
    diaph = {k: sorted(v, key=lambda t: (t % 100000 >= OFFGRID0, t)) for k, v in diaph.items()}
    openings = {}
    for o in desc.get("openings") or []:
        xy = G.xy(o); k = lidx.get(str(o.get("level")))
        if xy is None or k is None:
            continue
        i, j = gidx(_r(xy[0]), xs), gidx(_r(xy[1]), ys)
        if i is not None and j is not None:
            openings.setdefault(k, set()).add((i, j))
    # a grid point the user took out of the diaphragm that nothing frames into (a column joint between floors, e.g.
    # an ETABS story joint of a double-height column) is not part of that level's floor plate
    loose = {}
    for t in excl:
        k, rem = divmod(t, 100000)
        if rem < OFFGRID0:
            x, y, _ = nodes[t]
            if (_r(x / FT + xs[0]), _r(y / FT + ys[0])) not in framing.get(k, set()):
                loose.setdefault(k, set()).add((rem // 100, rem % 100))
    floor_bays = {}
    for k in range(1, NF + 1):
        P = present.get(k, set()) - loose.get(k, set())
        floor_bays[k] = sorted((i, j) for i in range(len(xs) - 1) for j in range(len(ys) - 1)
                               if {(i, j), (i + 1, j), (i, j + 1), (i + 1, j + 1)} <= P and (i, j) not in openings.get(k, set()))
        if not P:
            raise TranslateError("level %s has no framed grid point" % levels[k][0])
        area = sum((xs[i + 1] - xs[i]) * (ys[j + 1] - ys[j]) for (i, j) in floor_bays[k])
        derived.append(dict(what="floor plate %s: %d bays, %.0f ft2%s" % (levels[k][0], len(floor_bays[k]), area,
                                                                       (" (%d opening(s) as given)" % len(openings[k])) if openings.get(k) else ""),
                            rule="the bays whose four corner grid points are framed at the level, less the openings given"))
    return dict(levels=levels, zin=zin, xs=xs, ys=ys, xlab=xlab, ylab=ylab, nodes=nodes, FIX=FIX, COLS=COLS,
                BEAMS=BEAMS, BRACES=BRACES, link_pairs=link_pairs, brb_asc=brb_asc, brace_sys=brace_sys,
                brace_len=brace_len, rbs_by_sec=rbs_by_sec, present={k: sorted(v) for k, v in present.items()},
                floor_bays=floor_bays, sysk=sysk, derived=derived, KF=KF, diaph=diaph, excl=sorted(excl))


# ------------------------------------------------------------------------------------------- cfg text
def _lvl_map(spec, levels, roof_key="roof", floor_key="floor"):
    """{'floor':, 'roof':} or {'<level name>': v} or a number -> (floor, roof, {k: v} overrides)."""
    NF = len(levels) - 1
    if not isinstance(spec, dict):
        v = float(spec)
        return v, v, {}
    names = {n: k for k, (n, _) in enumerate(levels)}
    fl = spec.get(floor_key); rf = spec.get(roof_key)
    by = {}
    for n, v in spec.items():
        if n in names and names[n] >= 1:
            by[names[n]] = float(v)
    if fl is None:
        fl = by.get(1, next(iter(by.values()), 0.0)) if by else 0.0
    if rf is None:
        rf = by.get(NF, fl)
    return float(fl), float(rf), by


def _seis_block(desc, T):
    se = desc["seismic"]
    out = {}
    for d in ("X", "Y"):
        k = T["sysk"][d]
        row = S.SYSTEMS_V1[k]
        g = lambda f: float((se.get(f) or {}).get(d) if isinstance(se.get(f), dict) else se.get(f))
        name = ("dual %s+%s" % (row["mf"], row["braced"])) if row["kind"] == "DUAL" else k
        out[d] = dict(R=g("R"), Cd=g("Cd"), Om0=g("Om0"), Ct=row["Ct"], x=row["x"], system=name, rho=g("rho"))
    return out


def cfg_text(desc, name="building") -> tuple:
    """(cfg.py text, provenance dict). The description must validate as ready."""
    T = build_tables(desc)
    se = desc["seismic"]
    sb = _seis_block(desc, T)
    levels = T["levels"]
    NF = len(levels) - 1
    lo = desc.get("loads") or {}
    Dfl, Drf, Dby = _lvl_map(lo["D_psf"], levels)
    if isinstance(lo.get("L_psf"), dict) or lo.get("L_psf") is not None:
        Lfl, _Lrf, Lby = _lvl_map(lo["L_psf"], levels)
    else:
        Lfl, Lby = 0.0, {}
    rc = str(se["risk_category"]).upper().replace("RISK CATEGORY", "").strip()
    drift = {"I": 0.020, "II": 0.020, "III": 0.015, "IV": 0.010}[rc]
    mat = desc.get("materials") or {}
    grW = S.norm_grade(mat.get("W") or "A992") or "A992"
    grH = S.norm_grade(mat.get("HSS") or "A500 Gr C") or "A500 GR C"
    desc_sha = hashlib.sha256(json.dumps({k: v for k, v in desc.items() if not k.startswith("_")}, sort_keys=True, default=str).encode()).hexdigest()

    cfg = {}
    prov = {}

    def put(key, val, source, note=None, path=None):
        cfg[key] = val
        prov[key] = dict(source=source, **({"path": path} if path else {}), **({"note": note} if note else {}))

    def src(path):
        return S.source_of(desc, path)

    put("arch", "user-designed building (build-only): %s" % ((desc.get("project") or {}).get("name") or name), "computed")
    put("build_only", dict(description_sha256=desc_sha, schema=desc.get("schema"), version=desc.get("version"),
                           source=desc.get("source")), "computed", "marks the package as built from a user design")
    x0, y0 = T["xs"][0], T["ys"][0]
    put("NX", len(T["xs"]) - 1, "derived", "grid lines in X")
    put("NY", len(T["ys"]) - 1, "derived", "grid lines in Y")
    put("xcoords", [round((x - x0) * FT, 4) for x in T["xs"]], src("grid/x"), "grid X coordinates (in)", "grid/x")
    put("ycoords", [round((y - y0) * FT, 4) for y in T["ys"]], src("grid/y"), "grid Y coordinates (in)", "grid/y")
    put("SX", round((T["xs"][-1] - T["xs"][0]) * FT / max(1, len(T["xs"]) - 1), 4), "derived", "mean X bay (in); xcoords govern")
    put("SY", round((T["ys"][-1] - T["ys"][0]) * FT / max(1, len(T["ys"]) - 1), 4), "derived", "mean Y bay (in); ycoords govern")
    put("heights", [round(T["zin"][k] - T["zin"][k - 1], 4) for k in range(1, NF + 1)], src("levels"), "story heights (in)", "levels")
    put("grid_labels", dict(x=[T["xlab"].get(_r(v), "?") for v in T["xs"]], y=[T["ylab"].get(_r(v), "?") for v in T["ys"]]),
        "derived", "grid labels by index (record only)")
    put("level_names", [n for n, _ in levels], src("levels"), "level names by index k (record only)", "levels")
    basek = {f for f in T["FIX"].values()}
    bases = "fixed" if basek == {(1, 1, 1, 1, 1, 1)} else ("pinned" if basek == {(1, 1, 1, 0, 0, 0)} else "mixed")
    rels = {b[4] for b in T["BEAMS"]}
    joints = "rigid" if rels == {"none"} else ("pinned" if rels == {"both"} else "mixed")
    put("base", bases, src("supports"), path="supports")
    put("model", dict(bases=bases, joints=joints, gravity="framed"), "derived", "declared from the supports and beam releases as given")
    put("diaphragm", str(desc["diaphragm"]).lower().replace("semirigid", "semi-rigid"), src("diaphragm"), path="diaphragm")
    sysX, sysY = sb["X"]["system"], sb["Y"]["system"]
    put("system", sysX if sysX == sysY else "%s (X) / %s (Y)" % (sysX, sysY), src("seismic/system"), path="seismic/system")
    put("system_X", sysX, src("seismic/system/X"), path="seismic/system/X")
    put("system_Y", sysY, src("seismic/system/Y"), path="seismic/system/Y")
    SDS, SD1, S1 = float(se["SDS"]), float(se["SD1"]), float(se["S1"])
    Cu = round(S.cu_from_sd1(SD1), 4)
    sx = sb["X"]
    seis = dict(SDS=SDS, SD1=SD1, S1=S1, R=sx["R"], Ct=sx["Ct"], x=sx["x"], Cu=Cu, Ie=float(se["Ie"]), Cd=sx["Cd"],
                Om0=sx["Om0"], TL=float(se["TL"]), system=sysX)
    put("seis", seis, "mixed", "SDS/SD1/S1/TL/Ie from the description; R/Cd/Om0 X as given or accepted; Ct, x Table 12.8-2 "
        "for %s; Cu = %.3f Table 12.8-1 at SD1 = %.3f" % (sysX, Cu, SD1), "seismic")
    sy = sb["Y"]
    if any(abs(sy[f] - sx[f]) > 1e-9 for f in ("R", "Cd", "Om0", "Ct", "x")) or sysX != sysY:
        put("seis_Y", dict(R=sy["R"], Cd=sy["Cd"], Om0=sy["Om0"], Ct=sy["Ct"], x=sy["x"], system=sysY), "mixed",
            "Y-direction factors (ASCE 7-22 12.2.2)", "seismic")
    put("rho_X", sx["rho"], src("seismic/rho/X"), path="seismic/rho/X")
    put("rho_Y", sy["rho"], src("seismic/rho/Y"), path="seismic/rho/Y")
    put("rho", max(sx["rho"], sy["rho"]), "derived", "the larger direction value, for keys read building-wide")
    put("risk_category", rc, src("seismic/risk_category"), path="seismic/risk_category")
    put("site_class", str(se["site_class"]).upper(), src("seismic/site_class"), path="seismic/site_class")
    an = str(se.get("analysis") or "ELF").upper()
    mrsa = "MRSA" in an or an == "RS"
    put("analyses", ["ELF", "RS"] if mrsa else ["ELF"], src("seismic/analysis"), "the linear procedure of the user's design",
        "seismic/analysis")
    if mrsa:
        put("seismic_demand_basis", "MRSA", src("seismic/analysis"), "forces scaled per 12.9.1.4.1, drifts per 12.9.1.4.2",
            "seismic/analysis")
    put("drift_limit", drift, "computed", "ASCE 7-22 Table 12.12-1, all other structures, Risk Category %s" % rc)
    put("D_floor", Dfl, src("loads/D_psf"), path="loads/D_psf")
    put("D_roof", Drf, src("loads/D_psf"), path="loads/D_psf")
    if Dby:
        put("D_by_level", Dby, src("loads/D_psf"), path="loads/D_psf")
    put("L_floor", Lfl, src("loads/L_psf"), path="loads/L_psf")
    if Lby:
        put("L_by_level", {k: v for k, v in Lby.items() if k < NF}, src("loads/L_psf"), path="loads/L_psf")
    put("Lr", float(lo["Lr_psf"]), src("loads/Lr_psf"), path="loads/Lr_psf")
    put("snow", float(lo["snow_psf"]), src("loads/snow_psf"), path="loads/snow_psf")
    put("clad", float(lo["clad_psf"]), src("loads/clad_psf"), path="loads/clad_psf")
    put("floor_system", str(lo["floor_system"]).lower(), src("loads/floor_system"), path="loads/floor_system")
    if str(lo["floor_system"]).lower() == "one-way":
        put("deck_span", str(lo["deck_span"]).upper(), src("loads/deck_span"), path="loads/deck_span")
    if lo.get("storage_levels"):
        names = {n: k for k, (n, _) in enumerate(levels)}
        put("storage_levels", [names[str(n)] for n in lo["storage_levels"] if str(n) in names], src("loads/storage_levels"),
            path="loads/storage_levels")
    names = {n: k for k, (n, _) in enumerate(levels)}
    for dk, ck, what in (("extra_dead_psf", "extra_mass_floors", "extra dead load per level (gravity and mass)"),
                         ("seismic_only_psf", "seismic_mass_only", "seismic-weight-only area load per level"),
                         ("seismic_weight_kip", "seismic_weight_kip", "lumped seismic weight per level")):
        if isinstance(lo.get(dk), dict) and lo[dk]:
            put(ck, {names[str(n)]: float(v) for n, v in lo[dk].items() if str(n) in names}, src("loads/" + dk), what, "loads/" + dk)
    if _num0(lo.get("infill_spacing_in")):
        put("infill_spacing", float(lo["infill_spacing_in"]), src("loads/infill_spacing_in"), "secondary framing spacing (not modelled)",
            "loads/infill_spacing_in")
    put("floor_bays", T["floor_bays"], "derived", "bays whose four corner grid points are framed at the level")
    put("auto_omitted_columns", False, "derived", "the floor plate is floor_bays; nothing is inferred")
    gW = S.GRADES[grW]; gH = S.GRADES[grH]
    put("materials", {"W": dict(spec="ASTM " + grW, Fy=gW["Fy"], Fu=gW["Fu"], Ry=gW["Ry"]),
                      "HSS": dict(spec="ASTM " + grH, Fy=gH["Fy"], Fu=gH["Fu"], Ry=gH["Ry"])}, src("materials"),
        "grades as given or accepted; Fy/Fu nominal, Ry AISC 341-22 Table A3.2", "materials")
    if mat.get("existing"):
        ex = dict(mat.get("existing_info") or {})
        ex.setdefault("grade", "ASTM " + grW)
        put("existing_structure", ex, src("materials/existing"), path="materials/existing")
        cfg["materials"] = {("existing_" + k): v for k, v in cfg["materials"].items()}
    if T["BRACES"]:
        bt = {S.section_type(l) for l in T["brace_sys"] if not l.startswith("BRB-")}
        g = gH if (bt <= {"HSS", "PIPE"}) else gW
        put("brace_material", dict(Fy=g["Fy"], Fu=g["Fu"], Ry=g["Ry"], spec="ASTM " + (grH if bt <= {"HSS", "PIPE"} else grW)),
            src("materials"), "brace grade", "materials")
        syss = set(T["brace_sys"].values())
        put("brace_system", next(iter(syss)) if len(syss) == 1 else dict(T["brace_sys"]), "derived",
            "each brace's system: as given, else the braced system of its direction")
        if T["brace_len"]:
            facs = {f for v in T["brace_len"].values() for (_, f) in v}
            n_all = sum(len(v) for v in T["brace_len"].values())
            if len(facs) == 1 and n_all == len(T["BRACES"]):
                put("brace_length_factor", facs.pop(), "file", "the buckling length factor of every brace, as given")
            else:
                put("brace_length", {l: round(max(L for (L, _) in v), 3) for l, v in T["brace_len"].items()}, "file",
                    "length_factor x work-point length; braces of one section with different lengths take the longest "
                    "(the engines key the buckling length by section)")
    if T["brb_asc"]:
        bb = mat["brb"]
        put("brb", dict(Asc=dict(T["brb_asc"]), Fysc=float(bb["Fysc_ksi"]), Ry=1.0, beta=float(bb["beta"]),
                        omega=float(bb["omega"]), KF=float(bb["KF"])), src("materials/brb"), path="materials/brb")
    if T["link_pairs"]:
        es = sorted({round(math.dist(T["nodes"][a], T["nodes"][b]), 3) for a, b in T["link_pairs"]})
        put("ebf", dict(Fy=gW["Fy"], Ry=gW["Ry"], e_in=es[0] if len(es) == 1 else es), "derived", "link lengths from the geometry")
        put("ebf_links", [list(p) for p in T["link_pairs"]], "file", "beams marked as links", "beams")
        T["derived"].append(dict(what="EBF link-end nodes %s the floor diaphragm" % ("in" if (desc.get("details") or {}).get("link_ends_in_diaphragm") else "not in"),
                                 rule="details.link_ends_in_diaphragm as given or accepted"))
    if T["rbs_by_sec"]:
        dirs = sorted({("X" if abs(T["nodes"][b[1]][1] - T["nodes"][b[2]][1]) < 1e-6 else "Y")
                       for b in T["BEAMS"] if b[3] in T["rbs_by_sec"]})
        cb = sorted({round(v["c_in"] / float(S.shapes()[s.upper()]["bf"]), 4) for s, v in T["rbs_by_sec"].items()})
        put("rbs", {"c_over_bf": max(cb), "dirs": dirs, "by_section": {s: {k: v[k] for k in ("a_in", "b_in", "c_in")}
                                                                         for s, v in T["rbs_by_sec"].items()}},
            "file", "RBS cuts as given; c/bf for the AISC 358 5.7 drift factor (largest)", "beams")
    w = desc.get("wind")
    par = _num0(lo.get("parapet_ft")) or 0.0
    if par and not w and _num0(lo.get("clad_psf")):
        # no wind data: the parapet cladding still weighs -> lumped at the roof (perimeter of the roof plate)
        bays = set(map(tuple, T["floor_bays"][NF]))
        per = 0.0
        for (i, j) in bays:
            dxb, dyb = T["xs"][i + 1] - T["xs"][i], T["ys"][j + 1] - T["ys"][j]
            per += dyb * ((i - 1, j) not in bays) + dyb * ((i + 1, j) not in bays) + dxb * ((i, j - 1) not in bays) + dxb * ((i, j + 1) not in bays)
        extra = float(lo["clad_psf"]) * par * per / 1000.0
        swk = dict(cfg.get("seismic_weight_kip") or {})
        swk[NF] = round(swk.get(NF, 0.0) + extra, 3)
        put("seismic_weight_kip", swk, "derived", "parapet cladding %.1f kip = clad %.1f psf x %.2f ft x roof perimeter %.1f ft "
            "(no wind data, so the engine's parapet band is not available)" % (extra, float(lo["clad_psf"]), par, per))
    if w:
        gcpi = {"enclosed": 0.18, "partially enclosed": 0.55, "partially open": 0.18, "open": 0.0}.get(
            str(w.get("enclosure") or "enclosed").lower(), 0.18)
        put("wind", dict(V=float(w["V_mph"]), exposure=str(w.get("exposure") or "C").upper(), Kd=0.85,
                         Kzt=float(w.get("Kzt") or 1.0), Ke=1.0, GCpi=gcpi, roof_uplift=True, parapet_ft=par), "mixed",
            "V, exposure, Kzt, enclosure as given; Kd = 0.85 (ASCE 7-22 Table 26.6-1 MWFRS), GCpi by enclosure (Table 26.13-1)", "wind")
    sec0 = lambda tab: tab[0][3] if tab else None
    put("col", sec0(T["COLS"]), "derived", "first column section (engine fallback only)")
    if T["BEAMS"]:
        put("beam", sec0(T["BEAMS"]), "derived", "first beam section (engine fallback only)")
    if T["BRACES"]:
        put("brace", T["BRACES"][0][3], "derived", "first brace section (engine fallback only)")

    # ---- the module text
    lines = []
    w_ = lines.append
    w_('"""cfg.py -- %s  (BUILD-ONLY: the user\'s own design, modelled one for one)' % name)
    w_("")
    w_("Generated by steltic.build_only from building_description.json (sha256 %s)." % desc_sha)
    w_("DO NOT EDIT. HR does not redesign a user-designed building: every section, release, brace, grade and")
    w_("dimension below is the user's. provenance.json gives the source of every value (file / answer / accepted")
    w_("default / derived). The completion gate refuses a cfg.py that differs from the one generated here.")
    w_('"""')
    w_("import openseespy.opensees as ops")
    w_("import engine3d as eng")
    w_("")
    w_("DESCRIPTION_SHA256 = %r" % desc_sha)
    w_("# node tag -> (x, y, z) in inches; grid nodes ntag(i, j, k), other points k*100000 + 90000 + n")
    w_("NODES = %s" % pprint.pformat({t: tuple(v) for t, v in sorted(T["nodes"].items())}, width=110, compact=True))
    w_("FIX = %s" % pprint.pformat(dict(sorted(T["FIX"].items())), width=110, compact=True))
    w_("# (tag, node_i, node_j, section, strong-axis direction, member id, end release about both axes: none/I/J/both)")
    w_("COLUMNS = %s" % pprint.pformat(T["COLS"], width=110, compact=True))
    w_("# (tag, node_i, node_j, section, major-axis release, minor-axis release, member id)")
    w_("BEAMS = %s" % pprint.pformat(T["BEAMS"], width=110, compact=True))
    w_("# (tag, node_i, node_j, section label, truss area in2, member id)")
    w_("BRACES = %s" % pprint.pformat(T["BRACES"], width=110, compact=True))
    w_("PRESENT = %s" % pprint.pformat(T["present"], width=110, compact=True))
    w_("# nodes slaved to each level's rigid diaphragm (all points at the level except the excluded ones)")
    w_("DIAPH = %s" % pprint.pformat(T["diaph"], width=110, compact=True))
    w_("")
    w_("")
    w_("def custom_build(cfg, transf=\"PDelta\"):")
    w_("    ops.wipe(); ops.model(\"basic\", \"-ndm\", 3, \"-ndf\", 6)")
    w_("    z = eng.zlevels(cfg); NF = len(cfg[\"heights\"])")
    w_("    for t, (x, y, zz) in NODES.items():")
    w_("        ops.node(t, x, y, zz)")
    w_("    for t, f in FIX.items():")
    w_("        ops.fix(t, *f)")
    w_("    eles = []")
    w_("    for (t, n1, n2, sec, sd, _id, rel) in COLUMNS:")
    w_("        if rel == \"none\":")
    w_("            eng.add_column(t, n1, n2, sec, sd)")
    w_("        else:                         # the user's pinned column end: both axes released (engine section ordering)")
    w_("            eng._ensure_col_transf()")
    w_("            A, Ix, Iy, J = eng.Ipack(sec)")
    w_("            r = {\"I\": 1, \"J\": 2, \"both\": 3}[rel]")
    w_("            ops.element(\"elasticBeamColumn\", t, n1, n2, A, eng.E, eng.Gmod, J, Iy, Ix, eng.col_transf(sd), \"-releasey\", r, \"-releasez\", r)")
    w_("            eng._COL_DIR[t] = sd")
    w_("        eles.append((t, \"col\", sec, n1, n2))")
    w_("    for (t, n1, n2, sec, relz, rely, _id) in BEAMS:")
    w_("        eng.add_beam(t, n1, n2, sec, releases=(relz, rely)); eles.append((t, \"beam\", sec, n1, n2))")
    w_("    if BRACES:")
    w_("        ops.uniaxialMaterial(\"Elastic\", 1, eng.E)")
    w_("    for (t, n1, n2, sec, area, _id) in BRACES:")
    w_("        ops.element(\"Truss\", t, n1, n2, area, 1); eles.append((t, \"brace\", sec, n1, n2))")
    w_("    present = {k: set(map(tuple, v)) for k, v in PRESENT.items()}")
    w_("    cm = {}")
    w_("    for k in range(1, NF + 1):")
    w_("        tags = [k * 100000 + i * 100 + j for (i, j) in sorted(present.get(k, ()))]")
    w_("        xs = [NODES[t][0] for t in tags]; ys = [NODES[t][1] for t in tags]")
    w_("        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)")
    w_("        cm[k] = (cx, cy)")
    w_("        ops.node(eng.mtag(k), cx, cy, z[k]); ops.fix(eng.mtag(k), 0, 0, 1, 1, 1, 0)")
    w_("        ops.rigidDiaphragm(3, eng.mtag(k), *DIAPH[k])")
    w_("    info = {\"cm\": cm, \"present\": present, \"z\": z, \"NF\": NF, \"ele\": eles}")
    w_("    eng.attach_diaphragm_mass(cfg, info)")
    w_("    return info")
    w_("")
    w_("")
    w_("cfg = %s" % pprint.pformat(cfg, width=110, sort_dicts=False))
    w_("cfg[\"custom_build\"] = custom_build")
    w_("")
    text = "\n".join(lines)
    provenance = dict(schema="steltic.provenance", version=1, building=name, description_sha256=desc_sha,
                      cfg_sha256=hashlib.sha256(text.encode()).hexdigest(), cfg=prov, derived=T["derived"],
                      accepted_defaults=[dict(item=k, value=v.get("value")) for k, v in (desc.get("_answers") or {}).items() if v.get("accept")],
                      answers=[dict(item=k, value=v.get("value")) for k, v in (desc.get("_answers") or {}).items() if not v.get("accept")],
                      members=dict(columns=len(desc.get("columns") or []), beams=len(desc.get("beams") or []),
                                   braces=len(desc.get("braces") or []),
                                   elements=dict(col=len(T["COLS"]), beam=len(T["BEAMS"]), brace=len(T["BRACES"]))),
                      member_map=dict(col={c[0]: c[5] for c in T["COLS"]}, beam={t: i for (t, *_r2, i) in T["BEAMS"]},
                                      brace={t: i for (t, *_r2, i) in T["BRACES"]}))
    return text, provenance

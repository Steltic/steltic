"""ETABS text model (.e2k) -> building description.

What the file states is taken as the user's data (source "file"): stories, grids, points, frame objects
(columns / beams / braces) per story, section names, local-axis angles, end releases, base restraints,
diaphragm types, steel design preferences (system, R, Cd, Omega0, Ie, SDS) and seismic load-pattern parameters.

What has to be INTERPRETED is never written as data; it becomes a proposal the user must accept (or replace):
the dead and live load per level (area loads + slab / deck self-weight + framing self-weight, with the breakdown
shown), the system per direction where the design preferences name one frame type for a building that also has
braces, the steel grade from a material's name / Fy.

Whatever version 1 cannot carry is listed as refused (springs, partial fixity, points dropped off a story, shell
walls, auto-select sections, ...), never simplified away.

ETABS conventions used (CSI Analysis Reference / the .e2k text format):
  * STORIES are listed top-down, the base last with its ELEV; each story has its HEIGHT.
  * POINT "p" x y [drop]: plan coordinates; a third value drops the point below its story.
  * LINE "n" COLUMN|BEAM|BRACE "p1" "p2" k: the line exists on every story it is assigned to (LINEASSIGN); there
    p2 sits at the story and p1 k stories below (k = 0: both at the story).
  * Column local axes: at ANG 0 the local 2 axis is global +X, and an I-shape's major axis (M3) bends in the
    1-2 plane -> its strong axis resists X; at ANG 90 it resists Y.
  * RELEASE "M3I M3J ..." releases the major-axis moment at I / J; "PINNED" releases M2 and M3 at both ends.
"""
from __future__ import annotations

import hashlib
import math
import pathlib
import re
import shlex

from . import schema as S

LEN_FT = {"IN": 1 / 12.0, "INCH": 1 / 12.0, "FT": 1.0, "FEET": 1.0, "MM": 1 / 304.8, "CM": 1 / 30.48, "M": 1 / 0.3048}
FORCE_KIP = {"KIP": 1.0, "KIPS": 1.0, "LB": 1e-3, "LBF": 1e-3, "N": 2.248089e-4, "KN": 0.2248089, "KGF": 2.204623e-3,
             "TONF": 2.204623, "TON": 2.204623}
STEEL_PCF = 490.0


def _toks(line):
    try:
        return shlex.split(line, posix=True)
    except ValueError:
        return line.split()


def _kv(toks, start=0):
    """KEY value pairs of a record (a value may be a quoted string)."""
    out = {}
    i = start
    while i < len(toks) - 1:
        k = toks[i].upper()
        out.setdefault(k, toks[i + 1])
        i += 1
    return out


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse(text: str) -> dict:
    """Raw .e2k records, grouped by section."""
    sec = None
    R = dict(units=None, stories=[], grids=[], gridsys={}, diaph={}, materials={}, sections={}, autolists=set(),
             points={}, lines={}, pt_assign={}, line_assign={}, area_conn={}, area_assign={}, loadpat={},
             loadsets={}, arealoads=[], lineloads=[], shellprops={}, steelpref={}, seismic=[], massource=[], other_sections=set())
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("$"):
            sec = line[1:].strip().upper()
            continue
        t = _toks(line)
        if not t:
            continue
        key = t[0].upper()
        if sec == "CONTROLS" and key == "UNITS":
            R["units"] = (t[1].upper(), t[2].upper())
        elif sec and sec.startswith("STORIES") and key == "STORY":
            kv = _kv(t, 2)
            R["stories"].append(dict(name=t[1], height=_f(kv.get("HEIGHT")), elev=_f(kv.get("ELEV"))))
        elif sec == "GRIDS" and key == "GRIDSYSTEM":
            R["gridsys"][t[1]] = _kv(t, 2)
        elif sec == "GRIDS" and key == "GRID":
            kv = _kv(t, 2)
            if kv.get("DIR", "").upper() in ("X", "Y") and _f(kv.get("COORD")) is not None:
                R["grids"].append(dict(sys=t[1], label=kv.get("LABEL"), dir=kv["DIR"].upper(), coord=_f(kv["COORD"])))
        elif sec == "DIAPHRAGM NAMES" and key == "DIAPHRAGM":
            R["diaph"][t[1]] = _kv(t, 2).get("TYPE", "").upper()
        elif sec == "MATERIAL PROPERTIES" and key == "MATERIAL":
            m = R["materials"].setdefault(t[1], {})
            m.update({k: v for k, v in _kv(t, 2).items()})
        elif sec == "FRAME SECTIONS" and key == "FRAMESECTION":
            R["sections"].setdefault(t[1], {}).update(_kv(t, 2))
        elif sec and "AUTO SELECT" in sec:
            if len(t) > 1:
                R["autolists"].add(t[1])
        elif sec == "POINT COORDINATES" and key == "POINT":
            R["points"][t[1]] = (_f(t[2]), _f(t[3]), _f(t[4], 0.0) if len(t) > 4 else 0.0)
        elif sec == "LINE CONNECTIVITIES" and key == "LINE":
            R["lines"][t[1]] = dict(kind=t[2].upper(), p1=t[3], p2=t[4], n=int(_f(t[5], 0)) if len(t) > 5 else 0)
        elif sec == "POINT ASSIGNS" and key == "POINTASSIGN":
            R["pt_assign"].setdefault((t[1], t[2]), {}).update(_kv(t, 3))
        elif sec == "LINE ASSIGNS" and key == "LINEASSIGN":
            R["line_assign"].setdefault((t[1], t[2]), {}).update(_kv(t, 3))
        elif sec == "AREA CONNECTIVITIES" and key == "AREA":
            n = int(_f(t[3], 0))
            R["area_conn"][t[1]] = dict(kind=t[2].upper(), pts=t[4:4 + n])
        elif sec == "AREA ASSIGNS" and key == "AREAASSIGN":
            R["area_assign"].setdefault((t[1], t[2]), {}).update(_kv(t, 3))
        elif sec == "LOAD PATTERNS" and key == "LOADPATTERN":
            R["loadpat"][t[1]] = _kv(t, 2)
        elif sec == "LOAD PATTERNS" and key in ("SEISMIC", "AUTOSEISMIC", "SEISMICLOAD") or (sec == "LOAD PATTERNS" and "ASCE" in line.upper()):
            R["seismic"].append(t)
        elif sec == "SHELL UNIFORM LOAD SETS" and key == "SHELLUNIFORMLOADSET":
            kv = _kv(t, 2)
            R["loadsets"].setdefault(t[1], {})[kv.get("LOADPAT")] = _f(kv.get("VALUE"), 0.0)
        elif sec == "SHELL OBJECT LOADS" and key == "AREALOAD":
            R["arealoads"].append(t)
        elif sec == "FRAME OBJECT LOADS" and key == "LINELOAD":
            R["lineloads"].append(t)
        elif sec in ("SLAB PROPERTIES", "DECK PROPERTIES", "WALL PROPERTIES") and key == "SHELLPROP":
            R["shellprops"].setdefault(t[1], {}).update(_kv(t, 2))
        elif sec == "STEEL DESIGN PREFERENCES" and key == "STEELPREFERENCE":
            R["steelpref"].update(_kv(t, 1))
        elif sec == "MASS SOURCE":
            R["massource"].append(t)
        elif sec:
            R["other_sections"].add(sec)
    return R


def _system_from_frametype(ft):
    s = str(ft or "").upper().replace(" ", "")
    m = {"SMF": "SMF", "SPECIALMOMENTFRAME": "SMF", "IMF": "IMF", "OMF": "OMF", "SCBF": "SCBF", "OCBF": "OCBF",
         "BRBF": "BRBF", "EBF": "EBF", "STMF": "STMF", "OCBFI": "OCBF"}
    for k, v in m.items():
        if s.startswith(k):
            return v
    return None


def _grade(mat):
    """(grade key, why) from an ETABS steel material record; (None, why) when it does not name a known grade."""
    if not mat:
        return None, "no material"
    nm = " ".join(str(v) for v in (mat.get("_name"), mat.get("GRADE"), mat.get("STANDARD"))).upper()
    fy = _f(mat.get("FY"))
    for key in ("A992", "A572", "A500", "A1085", "A36", "A53"):
        if key in nm:
            if key == "A500":
                return ("A500 GR B" if "GR B" in nm or "GRADE B" in nm else "A500 GR C"), "material '%s'" % mat.get("_name")
            if key == "A572":
                return "A572 GR 50", "material '%s'" % mat.get("_name")
            if key == "A53":
                return "A53 GR B", "material '%s'" % mat.get("_name")
            return key, "material '%s'" % mat.get("_name")
    return None, "material '%s' (Fy %s in file units) does not name a grade version 1 knows" % (mat.get("_name"), fy)


def read_e2k(path, text=None) -> dict:
    path = pathlib.Path(path)
    raw = path.read_bytes() if text is None else text.encode()
    sha = hashlib.sha256(raw).hexdigest()
    txt = raw.decode("utf-8", errors="replace") if text is None else text
    R = parse(txt)
    d = S.new_description("e2k", path.name, sha)
    unsup, notes, props = [], [], {}
    d["project"] = {"name": path.stem}
    if not R["units"]:
        unsup.append("no UNITS record in CONTROLS: the file's units are unknown")
        L, F = 1.0, 1.0
    else:
        L = LEN_FT.get(R["units"][1]); F = FORCE_KIP.get(R["units"][0])
        if L is None or F is None:
            unsup.append("units %s / %s are not recognised" % R["units"])
            L, F = L or 1.0, F or 1.0
    PSF = F * 1000.0 / (L * L)              # file force/length^2 -> psf

    # ---- stories (top-down in the file) -> levels (base up)
    st = R["stories"]
    if not st:
        unsup.append("no STORIES")
    base = st[-1] if st else None
    elev = {}
    if base:
        z = (base.get("elev") or 0.0) * L
        elev[base["name"]] = z
        for s in reversed(st[:-1]):
            z += (s.get("height") or 0.0) * L
            elev[s["name"]] = z
    order = [s["name"] for s in reversed(st)]                     # base first
    d["levels"] = [{"name": n, "elev_ft": round(elev[n], 6)} for n in order]
    S.mark(d, "levels", "file")
    sidx = {n: k for k, n in enumerate(order)}                    # base = 0

    # ---- grids (cartesian systems only)
    labs = {}
    for sysname, kv in R["gridsys"].items():
        if str(kv.get("TYPE", "CARTESIAN")).upper() != "CARTESIAN":
            unsup.append("grid system '%s' is %s: version 1 reads cartesian grids only" % (sysname, kv.get("TYPE")))
        if _f(kv.get("ROTATION"), 0.0) or _f(kv.get("ORIGINX"), 0.0) or _f(kv.get("ORIGINY"), 0.0):
            unsup.append("grid system '%s' is rotated / offset: version 1 reads unrotated grids at the origin" % sysname)
    for g in R["grids"]:
        ax = g["dir"].lower()
        lab = str(g["label"])
        if (ax, lab) in labs:
            lab = "%s:%s" % (g["sys"], lab)
        labs[(ax, lab)] = True
        d["grid"][ax].append({"label": lab, "coord_ft": round(g["coord"] * L, 6)})
    for ax in ("x", "y"):
        d["grid"][ax].sort(key=lambda e: e["coord_ft"])
    S.mark(d, "grid", "file")
    G = S.Geometry(d)

    def plan(p):
        x, y, drop = R["points"][p]
        xf, yf = x * L, y * L
        px = next((g["label"] for g in d["grid"]["x"] if abs(g["coord_ft"] - xf) < 1e-4), round(xf, 6))
        py = next((g["label"] for g in d["grid"]["y"] if abs(g["coord_ft"] - yf) < 1e-4), round(yf, 6))
        return px, py, drop

    # ---- materials / sections
    for nm, m in R["materials"].items():
        m["_name"] = nm
    sec_map = {}
    for nm, s in R["sections"].items():
        lab = S.norm_section(nm) or S.norm_section(s.get("SHAPE"))
        sec_map[nm] = lab or nm                                     # an unknown name stays -> the validator asks
    grades = {"W": set(), "HSS": set()}
    conc_use = {}
    for (ln, story), a in R["line_assign"].items():
        sec_ = a.get("SECTION")
        mt = (R["materials"].get((R["sections"].get(sec_) or {}).get("MATERIAL")) or {})
        if str(mt.get("TYPE", "")).upper() == "CONCRETE":
            conc_use[sec_] = conc_use.get(sec_, 0) + 1
    for sec_, n in sorted(conc_use.items()):
        unsup.append("section '%s' is concrete (material %s, %d member(s)): version 1 models steel buildings only"
                     % (sec_, (R["sections"].get(sec_) or {}).get("MATERIAL"), n))

    # ---- frame objects per story
    released_minor, ignored = set(), {}
    cols_raw = []
    for (ln, story), a in R["line_assign"].items():
        li = R["lines"].get(ln)
        if li is None or story not in sidx:
            continue
        k = sidx[story]
        secname = a.get("SECTION")
        if secname in R["autolists"]:
            unsup.append("%s %s @ %s: auto-select section list '%s' -- give the final section" % (li["kind"].lower(), ln, story, secname))
        lab = sec_map.get(secname, secname)
        mat = R["materials"].get((R["sections"].get(secname) or {}).get("MATERIAL"))
        if lab and S.section_type(lab):
            g, why = _grade(mat)
            grades["HSS" if S.section_type(lab) in ("HSS", "PIPE") else "W"].add(g or ("?" + (mat or {}).get("_name", "?")))
        for flag in ("SPRINGPROP", "PROPERTYMODIFIERS", "NONPRISMATIC"):
            if flag in a:
                if flag == "SPRINGPROP":
                    unsup.append("%s %s @ %s: line springs are not modelled in version 1" % (li["kind"].lower(), ln, story))
                else:
                    ignored.setdefault(flag, []).append("%s@%s" % (ln, story))
        for flag in ("LENGTHOFFI", "LENGTHOFFJ", "RIGIDZONE", "CARDINALPT"):
            if flag in a and _f(a[flag], 0.0):
                ignored.setdefault(flag, []).append("%s@%s" % (ln, story))
        rel = str(a.get("RELEASE") or "").upper().split()
        if "PINNED" in rel:
            rel = ["M2I", "M3I", "M2J", "M3J"]
        if any(r.endswith("PF") or "PARTIAL" in r for r in rel):
            unsup.append("%s %s @ %s: partial-fixity release (%s) -- version 1 models rigid or pinned ends only" % (li["kind"].lower(), ln, story, " ".join(rel)))
        k1 = k - li["n"] if li["kind"] in ("COLUMN", "BRACE") else k
        if k1 < 0:
            unsup.append("%s %s @ %s: its lower end is below the base" % (li["kind"].lower(), ln, story))
            continue
        x1, y1, dr1 = plan(li["p1"]); x2, y2, dr2 = plan(li["p2"])
        if dr1 or dr2:
            unsup.append("%s %s @ %s: a point is dropped below its story (%s) -- member ends must be on levels" % (li["kind"].lower(), ln, story, li["p1"] if dr1 else li["p2"]))
            continue
        lvl = order[k]; lvl1 = order[k1]
        if li["kind"] == "COLUMN":
            if k1 == k:
                unsup.append("column %s @ %s: both ends on one story (a sloped / horizontal column)" % (ln, story))
                continue
            if (x1, y1) != (x2, y2):
                unsup.append("column %s @ %s: inclined (its ends are at different plan points)" % (ln, story))
                continue
            ang = _f(a.get("ANG"), 0.0) % 180.0
            sa = "X" if min(ang, 180 - ang) < 1e-3 else ("Y" if abs(ang - 90) < 1e-3 else None)
            if sa is None and lab and S.section_type(lab) in ("W", "HP", "M", "S"):
                unsup.append("column %s @ %s: local-axis angle %g deg -- version 1 orients columns along X or Y" % (ln, story, ang))
            col = dict(id="%s@%s" % (ln, story), x=x1, y=y1, base_level=lvl1, top_level=lvl, section=lab,
                       strong_axis=sa, _where="LINE %s story %s" % (ln, story))
            ri, rj = {"M2I", "M3I"} <= set(rel), {"M2J", "M3J"} <= set(rel)
            if ri or rj:
                col["release"] = "both" if (ri and rj) else ("base" if ri else "top")
            if any(r in rel for r in ("M2I", "M3I", "M2J", "M3J")) and not (ri or rj) or \
               (ri and ({"M2J", "M3J"} & set(rel)) and not rj) or (rj and ({"M2I", "M3I"} & set(rel)) and not ri):
                unsup.append("column %s @ %s: a moment release about one axis only (%s)" % (ln, story, " ".join(rel)))
            cols_raw.append(col)
        elif li["kind"] == "BEAM":
            m3 = ("I" if "M3I" in rel else "") + ("J" if "M3J" in rel else "")
            if any(r in rel for r in ("M2I", "M2J")) and m3 not in ("IJ",):
                released_minor.add("%s@%s" % (ln, story))
            d["beams"].append(dict(id="%s@%s" % (ln, story), level=lvl, i={"x": x1, "y": y1}, j={"x": x2, "y": y2}, section=lab,
                                   release={"": "none", "I": "I", "J": "J", "IJ": "both"}[m3], _where="LINE %s story %s" % (ln, story)))
        elif li["kind"] == "BRACE":
            d["braces"].append(dict(id="%s@%s" % (ln, story), i={"x": x1, "y": y1, "level": lvl1}, j={"x": x2, "y": y2, "level": lvl},
                                    section=lab, _where="LINE %s story %s" % (ln, story)))
    d["columns"] = cols_raw
    if released_minor:
        notes.append("%d beam(s) release only the minor-axis moment: modelled with the major-axis moment continuous" % len(released_minor))
    for flag, where in ignored.items():
        notes.append("%s on %d member(s) (e.g. %s) is not carried: members are modelled work point to work point, prismatic"
                     % (flag, len(where), ", ".join(where[:3])))
    S.mark(d, "columns", "file"); S.mark(d, "beams", "file"); S.mark(d, "braces", "file")

    # ---- shells: walls are outside version 1
    walls = [n for n, a in R["area_conn"].items() if a["kind"] in ("PANEL", "WALL")]
    if walls:
        wall_assigned = [n for (n, s) in R["area_assign"] if n in walls]
        if wall_assigned:
            unsup.append("%d wall / panel area object(s) (e.g. %s): shear walls and wall panels are not modelled in version 1"
                         % (len(set(wall_assigned)), ", ".join(sorted(set(wall_assigned))[:3])))

    # ---- supports
    restr = {}
    for (p, story), a in R["pt_assign"].items():
        if "SPRINGPROP" in a:
            unsup.append("point %s @ %s: a point spring -- version 1 models fixed or pinned supports only" % (p, story))
        if "RESTRAINT" in a:
            r = set(str(a["RESTRAINT"]).upper().split())
            kind = "fixed" if r >= {"UX", "UY", "UZ", "RX", "RY", "RZ"} else ("pinned" if r >= {"UX", "UY", "UZ"} else None)
            if story != order[0]:
                unsup.append("point %s @ %s: a support above the base" % (p, story))
            elif kind is None:
                unsup.append("point %s @ base: restraint '%s' is neither fixed nor pinned" % (p, a["RESTRAINT"]))
            else:
                x, y, _ = plan(p)
                restr[(str(x), str(y))] = kind
    kinds = set(restr.values())
    if len(kinds) == 1:
        d["supports"] = {"default": kinds.pop()}
        S.mark(d, "supports", "file")
    elif kinds:
        d["supports"] = {"default": None, "at": [{"x": k[0], "y": k[1], "fixity": v} for k, v in restr.items()]}
        S.mark(d, "supports", "file")
    # base columns without any restraint at their foot
    if d.get("supports"):
        base_cols = [c for c in cols_raw if c["base_level"] == order[0]]
        free = [c for c in base_cols if (str(c["x"]), str(c["y"])) not in restr]
        if free:
            unsup.append("%d base column(s) have no restraint at the base (e.g. %s)" % (len(free), free[0]["id"]))

    # ---- diaphragm
    disc = [(p, s) for (p, s), a in R["pt_assign"].items() if str(a.get("DIAPH") or a.get("DIAPHRAGM") or "").upper() == "DISCONNECTED"]
    if disc:                                     # ETABS: a point taken out of its story's diaphragm
        ex = []
        for p, s in disc:
            if p in R["points"] and s in sidx and sidx[s] > 0:
                x, y, _ = plan(p)
                ex.append({"x": x, "y": y, "level": s})
        if ex:
            d["diaphragm_exclude"] = ex
            S.mark(d, "diaphragm_exclude", "file", "points assigned DIAPH DISCONNECTED")
    # a story where some points carry a diaphragm: the member points at that story that carry none are out of it
    # (ETABS constrains only the joints assigned to the diaphragm)
    has_d = {s for (p, s), a in R["pt_assign"].items() if a.get("DIAPH") or a.get("DIAPHRAGM")}
    if has_d:
        assigned = {(p, s) for (p, s), a in R["pt_assign"].items() if a.get("DIAPH") or a.get("DIAPHRAGM")}
        ex = list(d.get("diaphragm_exclude") or [])
        seen = {(str(e["x"]), str(e["y"]), e["level"]) for e in ex}
        for (ln, story), a in R["line_assign"].items():
            li = R["lines"].get(ln)
            if li is None or story not in has_d or story not in sidx:
                continue
            ends = [(li["p2"], story)]
            if li["kind"] == "BEAM" or li["n"] == 0:
                ends.append((li["p1"], story))
            for (p, s) in ends:
                if (p, s) in assigned or p not in R["points"]:
                    continue
                x, y, _ = plan(p)
                key = (str(x), str(y), s)
                if key not in seen:
                    seen.add(key)
                    ex.append({"x": x, "y": y, "level": s})
        if ex:
            d["diaphragm_exclude"] = ex
            S.mark(d, "diaphragm_exclude", "file", "story joints without a diaphragm assignment")
    used = {a.get("DIAPH") or a.get("DIAPHRAGM") for (_, _), a in R["pt_assign"].items()
            if (a.get("DIAPH") or a.get("DIAPHRAGM")) and str(a.get("DIAPH") or a.get("DIAPHRAGM")).upper() != "DISCONNECTED"}
    used |= {a.get("DIAPH") for (_, _), a in R["area_assign"].items() if a.get("DIAPH")}
    types = {R["diaph"].get(u, "") for u in used if u}
    tmap = {"RIGID": "rigid", "SEMIRIGID": "semi-rigid", "SEMI-RIGID": "semi-rigid", "FLEXIBLE": "flexible"}
    if len(types) == 1:
        d["diaphragm"] = tmap.get(types.pop())
        S.mark(d, "diaphragm", "file")
    elif len(types) > 1:
        d["diaphragm"] = {u: tmap.get(R["diaph"].get(u), R["diaph"].get(u)) for u in used if u}
    elif R["diaph"]:
        notes.append("diaphragms are defined (%s) but none is assigned" % ", ".join(R["diaph"]))

    # ---- seismic basis: steel design preferences + seismic load patterns
    sp = R["steelpref"]
    se = d["seismic"]
    ft = _system_from_frametype(sp.get("FRAMETYPE"))
    for k, ek in (("SDS", "SYSTEMSDS"), ("Ie", "IMPORTANCEFACTOR")):
        if _f(sp.get(ek)) is not None:
            se[k] = _f(sp[ek]); S.mark(d, "seismic/" + k, "file", "STEELPREFERENCE %s" % ek)
    for t in R["seismic"]:
        kv = _kv(t, 1)
        for k, ek in (("SDS", "SDS"), ("SD1", "SD1"), ("S1", "S1"), ("TL", "TL")):
            if se.get(k) is None and _f(kv.get(ek)) is not None:
                se[k] = _f(kv[ek]); S.mark(d, "seismic/" + k, "file", "seismic load pattern")
        if not se.get("site_class") and kv.get("SITECLASS"):
            se["site_class"] = kv["SITECLASS"]; S.mark(d, "seismic/site_class", "file", "seismic load pattern")
    braced = bool(d["braces"])
    if ft:
        k = S.norm_system(ft)
        if isinstance(k, str) and (S.SYSTEMS_V1[k]["kind"] == "MF") == (not braced):
            se["system"] = {"X": k, "Y": k}
            S.mark(d, "seismic/system", "file", "STEELPREFERENCE FRAMETYPE %s" % sp.get("FRAMETYPE"))
            for f, ek in (("R", "SYSTEMR"), ("Om0", "OMEGA0"), ("Cd", "SYSTEMCD")):
                if _f(sp.get(ek)) is not None:
                    se[f] = {"X": _f(sp[ek]), "Y": _f(sp[ek])}; S.mark(d, "seismic/" + f, "file", "STEELPREFERENCE %s" % ek)
            if _f(sp.get("SYSTEMRHO")) is not None:
                se["rho"] = {"X": _f(sp["SYSTEMRHO"]), "Y": _f(sp["SYSTEMRHO"])}; S.mark(d, "seismic/rho", "file")
        elif isinstance(k, tuple):
            unsup.append("design preferences name frame type %s: %s" % (sp.get("FRAMETYPE"), S.REFUSED_SYSTEMS.get(k[1], k[1])))
        else:
            notes.append("design preferences name one frame type (%s) for a building %s braces: the system per direction "
                         "is asked" % (sp.get("FRAMETYPE"), "with" if braced else "without"))

    # ---- materials
    ma = d["materials"]
    for t in ("W", "HSS"):
        gs = grades[t]
        if len(gs) == 1:
            g = next(iter(gs))
            ma[t] = g if not g.startswith("?") else g[1:]          # an unknown name is kept -> the validator asks
            S.mark(d, "materials/" + t, "file")
        elif len(gs) > 1:
            ma["by_member"] = {t + str(n): g for n, g in enumerate(sorted(gs))}

    # ---- loads per level: proposals with their breakdown, never data
    pat_type = {n: str(kv.get("TYPE", "")).upper() for n, kv in R["loadpat"].items()}
    def kind_of(p):
        t = pat_type.get(p, "")
        if t in ("DEAD", "SUPERDEAD", "SUPER DEAD"):
            return "D"
        if t in ("LIVE", "REDUCIBLE LIVE", "REDUCE LIVE", "LIVE (REDUCIBLE)"):
            return "L"
        if "ROOF" in t:
            return "Lr"
        if "SNOW" in t:
            return "S"
        return None
    area = {}                                                   # level -> total floor area (ft2)
    load = {}                                                   # level -> {D/L/Lr/S: kip}
    slabw = {}                                                  # level -> slab / deck kip
    def poly_area(pts):
        xy = [(R["points"][p][0] * L, R["points"][p][1] * L) for p in pts if p in R["points"]]
        return 0.5 * abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(xy, xy[1:] + xy[:1])))
    floors = {n: a for n, a in R["area_conn"].items() if a["kind"] == "FLOOR"}
    fl_area = {}
    for (n, story), a in R["area_assign"].items():
        if n in floors and story in sidx:
            A = poly_area(floors[n]["pts"])
            fl_area[(n, story)] = A
            area[story] = area.get(story, 0.0) + A
            sp_ = R["shellprops"].get(a.get("SECTION")) or {}
            w = _slab_psf(sp_, R, PSF, L, F)
            if w:
                slabw[story] = slabw.get(story, 0.0) + w * A / 1000.0
    for t in R["arealoads"]:
        if len(t) < 3:
            continue
        n, story = t[1], t[2]
        A = fl_area.get((n, story))
        if A is None:
            continue
        kv = _kv(t, 3)
        if str(kv.get("TYPE", "")).upper() == "UNIFF" and str(kv.get("DIR", "GRAV")).upper() in ("GRAV", "GRAVITY", "Z"):
            kk = kind_of(kv.get("LC"))
            if kk:
                v = _f(kv.get("FVAL"), 0.0) * PSF
                if str(kv.get("DIR", "GRAV")).upper() == "Z":
                    v = -v
                load.setdefault(story, {}).setdefault(kk, 0.0)
                load[story][kk] += v * A / 1000.0
        elif str(kv.get("TYPE", "")).upper() == "UNIFLOADSET":
            setname = t[t.index("UNIFLOADSET") + 1] if "UNIFLOADSET" in t else None
            for p, val in (R["loadsets"].get(setname) or {}).items():
                kk = kind_of(p)
                if kk:
                    load.setdefault(story, {}).setdefault(kk + "_set", 0.0)
                    load[story][kk + "_set"] += (val or 0.0) * PSF * A / 1000.0
    selfw = any(_f(kv.get("SELFWEIGHT"), 0.0) for n, kv in R["loadpat"].items() if kind_of(n) == "D")
    frame_kip = {}
    if selfw:
        for m in d["columns"] + d["beams"] + d["braces"]:
            lab = m.get("section"); r = S.shapes().get(str(lab or "").upper())
            if not r:
                continue
            if "level" in m:
                lv = m["level"]; Lm = _len(G, m["i"], m["j"])
            elif "base_level" in m:
                lv = m["top_level"]; Lm = abs((G.z(m["top_level"]) or 0) - (G.z(m["base_level"]) or 0))
            else:
                lv = m["j"]["level"]; Lm = _len3(G, m["i"], m["j"])
            if Lm:
                frame_kip[lv] = frame_kip.get(lv, 0.0) + float(r["A"]) / 144.0 * Lm * STEEL_PCF / 1000.0
    Dp, Lp, why = {}, {}, []
    for n in order[1:]:
        A = area.get(n)
        if not A:
            continue
        ld = load.get(n, {})
        sup = 1000.0 * ld.get("D", 0.0) / A; sset = 1000.0 * ld.get("D_set", 0.0) / A
        sl = 1000.0 * slabw.get(n, 0.0) / A; fr = 1000.0 * frame_kip.get(n, 0.0) / A
        Dp[n] = round(sup + sset + sl + fr, 2)
        Lp[n] = round(1000.0 * (ld.get("L", 0.0) + ld.get("L_set", 0.0)) / A, 2)
        why.append("%s: %.1f area loads + %.1f uniform load sets + %.1f slab/deck + %.1f framing self-weight = %.1f psf over %.0f ft2"
                   % (n, sup, sset, sl, fr, Dp[n], A))
        if sup and sset:
            notes.append("%s: floors carry BOTH direct area loads and uniform load sets -- check they are not the same load "
                         "twice before accepting D and L" % n)
    if Dp:
        props["loads/D_psf"] = dict(value=Dp, reason="derived from the .e2k -- " + "; ".join(why))
        props["loads/L_psf"] = dict(value=Lp, reason="derived from the .e2k area live loads per level (live / reducible live patterns)")
        top = order[-1]
        A = area.get(top)
        lt = load.get(top, {})
        if A and (lt.get("Lr") is not None or lt.get("Lr_set") is not None):
            props["loads/Lr_psf"] = dict(value=round(1000.0 * (lt.get("Lr", 0.0) + lt.get("Lr_set", 0.0)) / A, 2),
                                         reason="derived from the .e2k roof live area loads at %s" % top)
        if A and (lt.get("S") is not None or lt.get("S_set") is not None):
            props["loads/snow_psf"] = dict(value=round(1000.0 * (lt.get("S", 0.0) + lt.get("S_set", 0.0)) / A, 2),
                                           reason="derived from the .e2k snow area loads at %s" % top)
    else:
        notes.append("no floor area objects with area loads: the dead and live loads per level are asked")
    if R["lineloads"]:
        notes.append("%d frame line load(s) (e.g. cladding) are not carried as such: give the cladding weight in psf of wall"
                     % len(R["lineloads"]))
    d["_proposals"] = props
    d["_intake"] = dict(units=R["units"], notes=notes, floor_area_ft2={n: round(a, 1) for n, a in area.items()},
                        ignored_sections=sorted(s for s in R["other_sections"] if s not in ("PROGRAM INFORMATION", "LOG")),
                        mass_source=[" ".join(t) for t in R["massource"]][:6])
    if unsup:
        d["_unsupported"] = _dedupe(unsup)
    d["details"] = {}
    return d


def _dedupe(lst, cap=60):
    out, seen = [], set()
    for x in lst:
        k = re.sub(r"\b\S+@\S+\b", "", x)[:80]
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    if len(out) > cap:
        out = out[:cap] + ["... and %d more" % (len(out) - cap)]
    return out


def _len(G, a, b):
    p, q = G.xy(a), G.xy(b)
    return math.dist(p, q) if p and q else None


def _len3(G, a, b):
    p, q = G.xy(a), G.xy(b)
    za, zb = G.z(a.get("level")), G.z(b.get("level"))
    if not (p and q) or za is None or zb is None:
        return None
    return math.dist(p + (za,), q + (zb,))


def _slab_psf(sp, R, PSF, L, F):
    """Self-weight (psf) of a slab / deck shell property, from its thickness and material unit weight."""
    if not sp:
        return 0.0
    mat = R["materials"].get(sp.get("MATERIAL") or sp.get("SLABMATERIAL")) or {}
    gam = _f(mat.get("WEIGHTPERVOLUME"))              # force / length^3 (file units)
    if gam is None:
        return 0.0
    gam_pcf = gam * F * 1000.0 / (L ** 3)
    t = _f(sp.get("SLABTHICKNESS")) or _f(sp.get("THICKNESS"))
    if t:
        return gam_pcf * t * L
    ds, dr = _f(sp.get("DECKSLABDEPTH")), _f(sp.get("DECKRIBDEPTH"))
    if ds is not None:
        wr, ws = _f(sp.get("DECKRIBWIDTHTOP"), 0.0) or 0.0, _f(sp.get("DECKRIBSPACING"), 0.0) or 0.0
        frac = (wr / ws) if ws else 0.5
        unit = (_f(sp.get("DECKUNITWEIGHT"), 0.0) or 0.0) * PSF
        return gam_pcf * (ds + (dr or 0.0) * frac) * L + unit
    return 0.0

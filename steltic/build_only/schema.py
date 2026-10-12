"""The building description (version 1): format, validation and the user's answers.

A description is one JSON object. Units: feet for plan coordinates and elevations, psf for area loads, kip,
inches for section / detail dimensions, ksi for strengths. Shape of the object (every key optional until the
validator asks for it):

    {"schema": "steltic.building_description", "version": 1,
     "source": {"kind": "template|e2k|json|hr_job", "file": "...", "sha256": "..."},
     "project": {"name": "...", "code": "ASCE 7-22", "notes": "..."},
     "seismic": {"SDS":, "SD1":, "S1":, "TL":, "risk_category": "II", "Ie":, "site_class": "D", "analysis": "ELF|MRSA",
                 "system": {"X": "SMF", "Y": "SCBF"}, "R": {"X":, "Y":}, "Cd": {...}, "Om0": {...}, "rho": {...}},
     "grid":   {"x": [{"label": "1", "coord_ft": 0.0}, ...], "y": [{"label": "A", "coord_ft": 0.0}, ...]},
     "levels": [{"name": "Base", "elev_ft": 0.0}, {"name": "L2", "elev_ft": 13.0}, ...],      (ascending; first = base)
     "columns": [{"id": "C1", "x": "1", "y": "A", "base_level": "Base", "top_level": "L2", "section": "W14X90",
                  "strong_axis": "X", "release": "none|base|top|both"}],    (release: pinned about both axes at that end)
     "beams":   [{"id": "B1", "level": "L2", "i": {"x": "1", "y": "A"}, "j": {"x": "2", "y": "A"},
                  "section": "W24X76", "release": "none|I|J|both", "link": false,
                  "rbs": {"a_in":, "b_in":, "c_in":} | true | null}],
     "braces":  [{"id": "D1", "i": {"x": "1", "y": "A", "level": "Base"}, "j": {"x": 12.5, "y": "A", "level": "L2"},
                  "section": "HSS8X8X1/2" | null, "brb": {"Asc_in2": 6.0} | null, "system": "SCBF" | null,
                  "length_factor": 1.0 | null}],
     "supports": {"default": "fixed|pinned", "at": [{"x": "1", "y": "A", "fixity": "pinned"}]},
     "diaphragm": "rigid|flexible|semi-rigid",
     "loads": {"D_psf": {"floor":, "roof":} | {"<level>": psf}, "L_psf": {...}, "Lr_psf":, "snow_psf":,
               "clad_psf":, "parapet_ft":, "floor_system": "one-way|two-way", "deck_span": "X|Y",
               "infill_spacing_in": (secondary framing not modelled), "storage_levels": [...],
               "extra_dead_psf": {"<level>": psf}, "seismic_only_psf": {...}, "seismic_weight_kip": {"<level>": kip}},
     "materials": {"W": "A992", "HSS": "A500 Gr C", "existing": false, "existing_info": {"year":, "grade":},
                   "brb": {"Fysc_ksi":, "KF":, "beta":, "omega":}},
     "details": {"doublers": [{"level": "L2", "x": "1", "y": "A", "t_in": 0.5}] | [], "beam_Lb_in": {"W24X76": 96},
                 "beam_bracing_in": 90 (lateral bracing spacing of the moment-frame beams, AISC 341 D1.2)},
     "diaphragm_exclude": [{"x":, "y":, "level":}],   (points NOT slaved to their level's rigid diaphragm)
     "openings": [{"level":, "x":, "y":}],              (open bays -- atria, shafts -- by their lower-left grid corner)
     "wind": {"V_mph":, "exposure": "C", "Kzt":, "enclosure": "enclosed"} | null,
     "user_results": {"T1_s": {"X":, "Y":}, "V_kip": {"X":, "Y":}, "W_kip":, "drift_pct": {"X":, "Y":}} | null,
     "_provenance": {"<path>": {"source": "file|derived|answer|default", "note": "..."}}}

A plan point is {"x": <grid label or feet>, "y": <grid label or feet>} (+ "level" for a brace end).

validate(desc) returns the items the description does not settle:
    key       -- HR stops and asks; there is no default (the build never starts without it)
    default   -- HR proposes a value with its reason; it is used only once the user accepts it
    refused   -- outside version 1; listed back as "not supported in version 1", never simplified
    info      -- nothing to answer; disclosed (e.g. no wind data -> DDM wind cases not evaluated)
Item ids are stable, so answers survive a re-validation. apply_answers() writes the answers into the
description and records their source in _provenance.
"""
from __future__ import annotations

import copy
import csv
import math
import os
import pathlib
import re

from . import SCHEMA, VERSION

# --------------------------------------------------------------------------------------------- sections
_ENGINE_DIR = pathlib.Path(os.environ.get("STELTIC_ENGINE_DIR") or (pathlib.Path(__file__).resolve().parents[2] / "steel_engine"))
_SHAPES = None


def shapes() -> dict:
    """AISC shapes table (steel_engine/aisc_shapes.csv): UPPER label -> row dict (Type, A, d, bf, ...)."""
    global _SHAPES
    if _SHAPES is None:
        _SHAPES = {}
        p = _ENGINE_DIR / "aisc_shapes.csv"
        with open(p, newline="") as f:
            for row in csv.DictReader(f):
                lab = (row.get("AISC_Manual_Label") or "").strip()
                if lab:
                    _SHAPES[lab.upper()] = dict(row, label=lab)
    return _SHAPES


def norm_section(name):
    """Canonical AISC label for a user's section name, or None when it is not an AISC shape.
    Accepts case / spacing variants: 'w14x90', 'W 14 X 90', 'HSS8x8x1/2', 'HSS 8X8X.500' (decimal thickness)."""
    if name is None:
        return None
    s = re.sub(r"\s+", "", str(name)).upper().replace("×", "X")
    if not s:
        return None
    db = shapes()
    if s in db:
        return db[s]["label"]
    lab = _metric(s, db)                                          # W360X60 -> W14X40 (the AISC metric designation)
    if lab:
        return lab
    m = re.match(r"^(HSS[\d.]+X[\d.]+X)(0?\.\d+)$", s)            # HSS8X8X.500 -> HSS8X8X1/2
    if m:
        t = float(m.group(2))
        for frac in ("1/8", "3/16", "1/4", "5/16", "3/8", "7/16", "1/2", "5/8", "3/4", "7/8", "1"):
            a, _, b = frac.partition("/")
            v = float(a) / float(b or 1)
            if abs(v - t) < 0.002 and (m.group(1) + frac) in db:
                return db[m.group(1) + frac]["label"]
    return None


def map_key(raw):
    """Key of a user's section name in description['section_map'] (path-safe)."""
    from urllib.parse import quote
    return quote(str(raw), safe="")


def resolve_section(desc, raw):
    """AISC label of a member's section: the name itself, else the user's mapping section_map[name]."""
    lab = norm_section(raw)
    if lab is None and raw is not None:
        lab = norm_section(((desc or {}).get("section_map") or {}).get(map_key(raw)))
    return lab


_W_METRIC = {"W100": "W4", "W130": "W5", "W150": "W6", "W200": "W8", "W250": "W10", "W310": "W12", "W360": "W14",
             "W410": "W16", "W460": "W18", "W530": "W21", "W610": "W24", "W690": "W27", "W760": "W30", "W840": "W33",
             "W920": "W36", "W1000": "W40", "W1100": "W44"}
_FRAC = ("1/8", "3/16", "1/4", "5/16", "3/8", "7/16", "1/2", "5/8", "3/4", "7/8", "1")


def _metric(s, db):
    """The AISC metric designation of a W or HSS shape -> its US label (AISC Manual 'Metric' columns): W<mm>X<kg/m>
    -> W<in>X<lb/ft> with the mass within 1.5 %; HSS<mm>X<mm>X<mm> -> the inch label with the dimensions within
    0.5 mm and the thickness within 0.15 mm. None when no shape matches -- the user is then asked."""
    m = re.match(r"^(W\d{3,4})X([\d.]+)$", s)
    if m and m.group(1) in _W_METRIC:
        want = float(m.group(2)) / 1.48816
        best = [(abs(float(k.split("X")[1]) - want) / want, v["label"]) for k, v in db.items()
                if k.startswith(_W_METRIC[m.group(1)] + "X") and re.match(r"^W\d+X[\d.]+$", k)]
        best.sort()
        if best and best[0][0] <= 0.015:
            return best[0][1]
    m = re.match(r"^HSS([\d.]+)X([\d.]+)X([\d.]+)$", s)
    if m and float(m.group(1)) > 50:                             # millimetres
        a, b, t = (float(x) / 25.4 for x in m.groups())
        for k, v in db.items():
            mm = re.match(r"^HSS([\d.]+)X([\d.]+)X([\d/]+)$", k)
            if not mm:
                continue
            try:
                A, B = float(mm.group(1)), float(mm.group(2))
                n, _, d = mm.group(3).partition("/")
                T = float(n) / float(d or 1)
            except ValueError:
                continue
            if abs(A - a) * 25.4 <= 0.5 and abs(B - b) * 25.4 <= 0.5 and abs(T - t) * 25.4 <= 0.15:
                return v["label"]
    return None


def section_type(label):
    r = shapes().get(str(label or "").upper())
    return r["Type"] if r else None


def is_square(label):
    r = shapes().get(str(label or "").upper())
    if not r:
        return False
    if r["Type"] == "PIPE":
        return True
    if r["Type"] == "HSS":
        m = re.match(r"^HSS([\d.]+)X([\d.]+)X", r["label"].upper())
        return bool(m) and abs(float(m.group(1)) - float(m.group(2))) < 1e-9 or not m   # round HSS -> symmetric
    return False


def brb_label(asc):
    return "BRB-Asc%g" % float(asc)


# ---------------------------------------------------------------------------------------- systems / code
# ASCE 7-22 Table 12.2-1 (R, Om0, Cd) and Table 12.8-2 (Ct, x) for the version 1 systems -- the same rows as the
# HR engine's SFRS_TABLE.
SYSTEMS_V1 = {
    "SMF":  dict(R=8.0,  Om0=3.0, Cd=5.5,  Ct=0.028, x=0.8,  row="C.1 steel special moment frames", kind="MF"),
    "IMF":  dict(R=4.5,  Om0=3.0, Cd=4.0,  Ct=0.028, x=0.8,  row="C.3 steel intermediate moment frames", kind="MF"),
    "OMF":  dict(R=3.5,  Om0=3.0, Cd=3.0,  Ct=0.028, x=0.8,  row="C.4 steel ordinary moment frames", kind="MF"),
    "SCBF": dict(R=6.0,  Om0=2.0, Cd=5.0,  Ct=0.02,  x=0.75, row="B.2 steel special concentrically braced frames", kind="BF"),
    "OCBF": dict(R=3.25, Om0=2.0, Cd=3.25, Ct=0.02,  x=0.75, row="B.3 steel ordinary concentrically braced frames", kind="BF"),
    "BRBF": dict(R=8.0,  Om0=2.5, Cd=5.0,  Ct=0.03,  x=0.75, row="B.26 steel buckling-restrained braced frames", kind="BF"),
    "EBF":  dict(R=8.0,  Om0=2.0, Cd=4.0,  Ct=0.03,  x=0.75, row="B.1 steel eccentrically braced frames", kind="BF"),
    "DUAL_SMF_EBF":  dict(R=8.0, Om0=2.5, Cd=4.0, Ct=0.03, x=0.75, row="D.1 dual: SMF + steel EBF", kind="DUAL", braced="EBF", mf="SMF"),
    "DUAL_SMF_SCBF": dict(R=7.0, Om0=2.5, Cd=5.5, Ct=0.02, x=0.75, row="D.2 dual: SMF + steel SCBF", kind="DUAL", braced="SCBF", mf="SMF"),
    "DUAL_SMF_BRBF": dict(R=8.0, Om0=2.5, Cd=5.0, Ct=0.03, x=0.75, row="D.13 dual: SMF + steel BRBF", kind="DUAL", braced="BRBF", mf="SMF"),
    "DUAL_IMF_SCBF": dict(R=6.0, Om0=2.5, Cd=5.0, Ct=0.02, x=0.75, row="E.1 dual: IMF + steel SCBF", kind="DUAL", braced="SCBF", mf="IMF"),
}
REFUSED_SYSTEMS = {"SPSW": "steel plate shear walls", "C-PSW": "composite plate shear walls (C-PSW/CF)",
                   "STMF": "special truss moment frames", "NSD": "steel systems not specifically detailed (R = 3)"}
IE_BY_RC = {"I": 1.0, "II": 1.0, "III": 1.25, "IV": 1.5}
GRADES = {   # nominal Fy and AISC 341-22 Table A3.2 Ry for the grades version 1 knows
    "A992": dict(Fy=50.0, Fu=65.0, Ry=1.1, types=("W", "HP", "M", "S", "WT")),
    "A572 GR 50": dict(Fy=50.0, Fu=65.0, Ry=1.1, types=("W", "HP", "M", "S", "WT", "C", "MC", "L")),
    "A36": dict(Fy=36.0, Fu=58.0, Ry=1.5, types=("W", "HP", "M", "S", "WT", "C", "MC", "L")),
    "A500 GR C": dict(Fy=50.0, Fu=62.0, Ry=1.3, types=("HSS",)),
    "A500 GR B": dict(Fy=46.0, Fu=58.0, Ry=1.4, types=("HSS",)),
    "A1085": dict(Fy=50.0, Fu=65.0, Ry=1.25, types=("HSS",)),
    "A53 GR B": dict(Fy=35.0, Fu=60.0, Ry=1.6, types=("PIPE",)),
}


def norm_grade(g):
    if g is None:
        return None
    s = re.sub(r"\s+", " ", str(g).upper().replace("GRADE", "GR").replace("ASTM", "").replace(".", "")).strip()
    s = s.replace("GR50", "GR 50").replace("GRC", "GR C").replace("GRB", "GR B")
    return s if s in GRADES else None


def norm_system(name):
    """Version 1 system key for a declared name ('SMF', 'special moment frame', 'dual SMF+BRBF', ...);
    ('REFUSED', key) for a known system outside version 1; None when it cannot be resolved."""
    s = str(name or "").lower().replace("_", " ").replace("-", " ").replace("+", " ")
    if not s.strip():
        return None
    def has(*ws):
        return any(re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(w), s) for w in ws)
    if has("spsw", "plate shear wall", "plate shear walls") and not has("c psw", "composite"):
        return ("REFUSED", "SPSW")
    if has("c psw", "cpsw", "composite plate shear", "speedcore", "ccpsw"):
        return ("REFUSED", "C-PSW")
    if has("stmf", "truss moment"):
        return ("REFUSED", "STMF")
    if "not specifically detailed" in s or has("nsd"):
        return ("REFUSED", "NSD")
    found = []
    for key, words in (("BRBF", ("brbf", "buckling restrained")), ("EBF", ("ebf", "eccentrically braced", "eccentric")),
                       ("SCBF", ("scbf", "special concentrically braced", "special concentric")),
                       ("OCBF", ("ocbf", "ordinary concentrically braced", "ordinary concentric")),
                       ("SMF", ("smf", "special moment")), ("IMF", ("imf", "intermediate moment")),
                       ("OMF", ("omf", "ordinary moment"))):
        if has(*words):
            found.append(key)
    if "dual" in s or (len(found) == 2 and any(f in ("SMF", "IMF") for f in found)):
        mf = [f for f in found if f in ("SMF", "IMF")]
        other = [f for f in found if f not in ("SMF", "IMF", "OMF")]
        if len(mf) == 1 and len(other) == 1:
            k = "DUAL_%s_%s" % (mf[0], other[0])
            return k if k in SYSTEMS_V1 else ("REFUSED", "dual %s + %s" % (mf[0], other[0]))
        return None
    return found[0] if len(found) == 1 else None


def braced_system(sysname):
    r = SYSTEMS_V1.get(sysname or "")
    if not r:
        return None
    if r["kind"] == "BF":
        return sysname
    return r.get("braced")


def cu_from_sd1(sd1):
    """ASCE 7-22 Table 12.8-1 coefficient Cu (linear interpolation between the tabulated SD1)."""
    pts = [(0.1, 1.7), (0.15, 1.6), (0.2, 1.5), (0.3, 1.4), (0.4, 1.4)]
    if sd1 <= pts[0][0]:
        return pts[0][1]
    for (a, ca), (b, cb) in zip(pts, pts[1:]):
        if sd1 <= b:
            return ca + (cb - ca) * (sd1 - a) / (b - a)
    return 1.4


# --------------------------------------------------------------------------------------------- paths
def _segs(path):
    out = []
    for part in path.split("/"):
        m = re.match(r"^(\w+)\[id=(.*)\]$", part)
        if m:
            out += [m.group(1), {"id": m.group(2)}]
        else:
            out.append(part)
    return out


def get_path(d, path, default=None):
    cur = d
    for s in _segs(path):
        if isinstance(s, dict):
            if not isinstance(cur, list):
                return default
            cur = next((e for e in cur if isinstance(e, dict) and str(e.get("id")) == s["id"]), None)
        elif isinstance(cur, dict):
            cur = cur.get(s)
        else:
            return default
        if cur is None:
            return default
    return cur


def set_path(d, path, value):
    segs = _segs(path)
    cur = d
    for n, s in enumerate(segs[:-1]):
        nxt = segs[n + 1]
        if isinstance(s, dict):
            cur = next(e for e in cur if isinstance(e, dict) and str(e.get("id")) == s["id"])
            continue
        if cur.get(s) is None:
            cur[s] = [] if isinstance(nxt, dict) else {}
        cur = cur[s]
    last = segs[-1]
    cur[last] = value


def mark(desc, path, source, note=None):
    pv = desc.setdefault("_provenance", {})
    pv[path] = {"source": source, **({"note": note} if note else {})}


def source_of(desc, path):
    pv = desc.get("_provenance") or {}
    if path in pv:
        return pv[path]["source"]
    while "/" in path:                       # a value inside a block the file gave as a whole
        path = path.rsplit("/", 1)[0]
        if path in pv:
            return pv[path]["source"]
    return "file"


# ------------------------------------------------------------------------------------------- geometry
def _num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


class Geometry:
    """Resolves grid labels, level names and points of a description (feet)."""

    def __init__(self, desc):
        g = desc.get("grid") or {}
        self.gx = {str(e.get("label")): _num(e.get("coord_ft")) for e in (g.get("x") or []) if isinstance(e, dict)}
        self.gy = {str(e.get("label")): _num(e.get("coord_ft")) for e in (g.get("y") or []) if isinstance(e, dict)}
        self.levels = [(str(e.get("name")), _num(e.get("elev_ft"))) for e in (desc.get("levels") or []) if isinstance(e, dict)]
        self.lev = {n: z for n, z in self.levels}
        self.lev_index = {n: k for k, (n, _) in enumerate(self.levels)}

    def coord(self, v, axis):
        if v is None:
            return None
        tab = self.gx if axis == "x" else self.gy
        if isinstance(v, str) and v in tab:
            return tab[v]
        if isinstance(v, (int, float)) and not isinstance(v, bool) and ("%g" % v) in tab:
            return tab["%g" % v]              # a number that is a grid label (e.g. grid line 3) is the label
        f = _num(v)
        if f is not None and not (isinstance(v, str) and v in tab):
            return f
        return None

    def xy(self, p):
        if not isinstance(p, dict):
            return None
        x, y = self.coord(p.get("x"), "x"), self.coord(p.get("y"), "y")
        return None if x is None or y is None else (x, y)

    def z(self, level):
        return self.lev.get(str(level)) if level is not None else None


# --------------------------------------------------------------------------------------------- items
def _item(items, iid, kind, path, group, question, needed_for, default=None, reason=None, options=None, where=None):
    it = dict(id=iid, kind=kind, path=path, group=group, question=question, needed_for=needed_for)
    if where:
        it["where"] = where
    if kind == "default":
        it["default"] = default
        it["default_reason"] = reason
    if options:
        it["options"] = options
    items.append(it)


def _fmt_pt(p):
    if not isinstance(p, dict):
        return "?"
    s = "%s/%s" % (p.get("x"), p.get("y"))
    return s + ("@%s" % p["level"] if p.get("level") is not None else "")


def validate(desc) -> dict:
    """Items the description does not settle (see the module docstring), plus summary counts.
    Returns {"items": [...], "counts": {"key":, "default":, "refused":, "info":}, "ready": bool}."""
    d = desc or {}
    items = []
    acc = set((d.get("_answers") or {}).keys())          # ids already answered / accepted (kept for display)
    G = Geometry(d)

    # ---------------------------------------------------------------- project / code
    code = (d.get("project") or {}).get("code")
    if not code:
        _item(items, "project.code", "default", "project/code", "Project and basis",
              "Which code edition is the building designed to?", "the Chapter 16 checks NL applies",
              default="ASCE 7-22", reason="NL implements ASCE 7-22 Chapter 16; confirm the building's basis is ASCE 7-22")
    elif "7-22" not in str(code) and "7 22" not in str(code):
        _item(items, "project.code", "refused", "project/code", "Project and basis",
              "Code edition '%s': NL implements ASCE 7-22 Chapter 16 only." % code, "the whole NL suite")

    # ---------------------------------------------------------------- seismic basis
    se = d.get("seismic") or {}
    for k, what in (("SDS", "design spectral acceleration SDS (g)"), ("SD1", "design spectral acceleration SD1 (g)"),
                    ("S1", "mapped spectral acceleration S1 (g)")):
        if _num(se.get(k)) is None or _num(se.get(k)) <= 0:
            _item(items, "seismic." + k, "key", "seismic/" + k, "Project and basis",
                  "Give %s." % what, "the target spectrum, record scaling and the Chapter 16 limits")
    if _num(se.get("TL")) is None:
        _item(items, "seismic.TL", "default", "seismic/TL", "Project and basis",
              "Long-period transition period TL (s)?", "the target spectrum beyond TL",
              default=8.0, reason="ASCE 7-22 map value used by the HR engine; replace it with your site's TL")
    rc = str(se.get("risk_category") or "").upper().replace("RISK CATEGORY", "").strip()
    if rc not in IE_BY_RC:
        _item(items, "seismic.risk_category", "key", "seismic/risk_category", "Project and basis",
              "Risk Category (I, II, III or IV)?", "Ie, the drift limits and the BPON performance levels",
              options=["I", "II", "III", "IV"])
    elif _num(se.get("Ie")) is None:
        _item(items, "seismic.Ie", "default", "seismic/Ie", "Project and basis",
              "Seismic importance factor Ie?", "the design base shear",
              default=IE_BY_RC[rc], reason="ASCE 7-22 Table 1.5-2 for Risk Category %s" % rc)
    if not se.get("site_class"):
        _item(items, "seismic.site_class", "key", "seismic/site_class", "Project and basis",
              "Site class (A, B, BC, C, CD, D, DE, E)?", "the record selection and scaling",
              options=["A", "B", "BC", "C", "CD", "D", "DE", "E"])
    an = str(se.get("analysis") or "").upper().replace("RSA", "MRSA").replace("MMRSA", "MRSA")
    if an not in ("ELF", "MRSA"):
        _item(items, "seismic.analysis", "default", "seismic/analysis", "Project and basis",
              "Linear analysis procedure your design used: ELF or MRSA (modal response spectrum)?",
              "the design drifts and forces HR reports alongside, and the 16.1.2 linear analysis statement",
              default="ELF", reason="the equivalent lateral force procedure, unless you used modal response spectrum analysis",
              options=["ELF", "MRSA"])
    sysd = se.get("system") or {}
    sys_ok = {}
    for dirn in ("X", "Y"):
        raw = sysd.get(dirn) if isinstance(sysd, dict) else sysd
        if not raw:
            _item(items, "seismic.system.%s" % dirn, "key", "seismic/system/%s" % dirn, "Project and basis",
                  "Seismic force-resisting system in the %s direction?" % dirn,
                  "hinge types, the acceptance criteria and R, Cd, Omega0",
                  options=sorted(SYSTEMS_V1))
            continue
        k = norm_system(raw)
        if isinstance(k, tuple):
            _item(items, "seismic.system.%s" % dirn, "refused", "seismic/system/%s" % dirn, "Project and basis",
                  "System '%s' in %s: %s are not supported in version 1." % (raw, dirn, REFUSED_SYSTEMS.get(k[1], k[1])),
                  "the whole NL suite")
            continue
        if k is None:
            _item(items, "seismic.system.%s" % dirn, "key", "seismic/system/%s" % dirn, "Project and basis",
                  "System '%s' in %s is not recognised: name one of %s." % (raw, dirn, ", ".join(sorted(SYSTEMS_V1))),
                  "hinge types, the acceptance criteria and R, Cd, Omega0", options=sorted(SYSTEMS_V1))
            continue
        sys_ok[dirn] = k
        row = SYSTEMS_V1[k]
        for f, nm in (("R", "R"), ("Cd", "Cd"), ("Om0", "Omega0")):
            v = (se.get(f) or {}).get(dirn) if isinstance(se.get(f), dict) else se.get(f)
            if _num(v) is None:
                _item(items, "seismic.%s.%s" % (f, dirn), "default", "seismic/%s/%s" % (f, dirn), "Project and basis",
                      "%s in the %s direction?" % (nm, dirn), "the design base shear, drifts and overstrength",
                      default=row[f], reason="ASCE 7-22 Table 12.2-1 row %s" % row["row"])
        v = (se.get("rho") or {}).get(dirn) if isinstance(se.get("rho"), dict) else se.get("rho")
        if _num(v) is None:
            _item(items, "seismic.rho.%s" % dirn, "default", "seismic/rho/%s" % dirn, "Project and basis",
                  "Redundancy factor rho in the %s direction?" % dirn, "the HR design forces reported alongside",
                  default=1.3, reason="ASCE 7-22 12.3.4.1; give 1.0 where your design met 12.3.4.2")

    # ---------------------------------------------------------------- grid and levels
    for ax in ("x", "y"):
        lines = ((d.get("grid") or {}).get(ax)) or []
        good = [e for e in lines if isinstance(e, dict) and _num(e.get("coord_ft")) is not None and e.get("label") is not None]
        if len(good) < 2:
            _item(items, "grid.%s" % ax, "key", "grid/%s" % ax, "Grid and levels",
                  "Give the %s grid lines (label and coordinate in ft); at least two." % ax.upper(), "the whole model")
        elif len(good) > 99:
            _item(items, "grid.%s" % ax, "refused", "grid/%s" % ax, "Grid and levels",
                  "%d %s grid lines: version 1 handles at most 99 per direction." % (len(good), ax.upper()), "node numbering")
        labs = [str(e.get("label")) for e in good]
        if len(set(labs)) != len(labs):
            _item(items, "grid.%s.labels" % ax, "key", "grid/%s" % ax, "Grid and levels",
                  "%s grid labels repeat (%s): every grid line needs its own label." % (ax.upper(), ", ".join(sorted({l for l in labs if labs.count(l) > 1}))),
                  "placing members")
    lv = G.levels
    if len(lv) < 2 or any(z is None for _, z in lv):
        _item(items, "levels", "key", "levels", "Grid and levels",
              "Give the levels from the base up (name and elevation in ft); at least the base and one floor.", "the whole model")
    else:
        zs = [z for _, z in lv]
        if any(b <= a for a, b in zip(zs, zs[1:])):
            _item(items, "levels.order", "key", "levels", "Grid and levels",
                  "Level elevations must rise from the base up; they do not (%s)." % ", ".join("%s %.2f" % t for t in lv), "the whole model")
        if len(lv) > 90:
            _item(items, "levels", "refused", "levels", "Grid and levels",
                  "%d levels: version 1 handles at most 89 above the base." % (len(lv) - 1), "node numbering")
        if len(set(n for n, _ in lv)) != len(lv):
            _item(items, "levels.names", "key", "levels", "Grid and levels", "Level names repeat: every level needs its own name.", "placing members")

    # ---------------------------------------------------------------- members
    cols = [c for c in (d.get("columns") or []) if isinstance(c, dict)]
    beams = [b for b in (d.get("beams") or []) if isinstance(b, dict)]
    braces = [b for b in (d.get("braces") or []) if isinstance(b, dict)]
    if not cols:
        _item(items, "columns", "key", "columns", "Members", "No columns given: list every column.", "the whole model")
    ids = [str(m.get("id")) for m in cols + beams + braces]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        _item(items, "members.ids", "key", "columns", "Members",
              "Member ids repeat (%s): every column, beam and brace needs its own id." % ", ".join(dup[:12]), "provenance and answers")
    grid_ok = not any(i["id"].startswith("grid.") or i["id"].startswith("levels") for i in items)

    def sec_check(m, kind, path):
        raw = m.get("section")
        if not raw:
            _item(items, "%s.%s.section" % (kind, m.get("id")), "key", path + "/section", "Members",
                  "%s %s: give the AISC section." % (kind[:-1].capitalize(), m.get("id")), "stiffness, strength and the hinge model",
                  where=m.get("_where"))
            return None
        lab = resolve_section(d, raw)
        if lab is None:
            q = map_key(raw)
            users = bad_sections.setdefault(q, [raw, []])
            users[1].append(str(m.get("id")))
            return None
        return lab

    bad_sections = {}

    col_nodes = {}                                  # (x, y) -> [(z0, z1)]
    for c in cols:
        path = "columns[id=%s]" % c.get("id")
        lab = sec_check(c, "columns", path)
        xy = G.xy(c)
        if grid_ok and xy is None:
            _item(items, "columns.%s.at" % c.get("id"), "key", path, "Members",
                  "Column %s: position '%s' is not a grid intersection or a coordinate." % (c.get("id"), _fmt_pt(c)), "the whole model")
        z0, z1 = G.z(c.get("base_level")), G.z(c.get("top_level"))
        if grid_ok and (z0 is None or z1 is None or z1 <= z0):
            _item(items, "columns.%s.levels" % c.get("id"), "key", path, "Members",
                  "Column %s: base level '%s' / top level '%s' are not two levels with the top above the base."
                  % (c.get("id"), c.get("base_level"), c.get("top_level")), "the whole model")
        elif xy is not None:
            col_nodes.setdefault((round(xy[0], 4), round(xy[1], 4)), []).append((z0, z1))
        if str(c.get("release") or "none").lower() not in ("none", "base", "top", "both"):
            _item(items, "columns.%s.release" % c.get("id"), "key", path + "/release", "Members",
                  "Column %s: end release '%s' -- give none, base, top or both (pinned about both axes at that end)." % (c.get("id"), c.get("release")),
                  "column end moments", options=["none", "base", "top", "both"])
        if lab and section_type(lab) in ("W", "HP", "M", "S") or (lab and section_type(lab) == "HSS" and not is_square(lab)):
            sa = str(c.get("strong_axis") or "").upper()
            if sa not in ("X", "Y"):
                _item(items, "columns.%s.strong_axis" % c.get("id"), "key", path + "/strong_axis", "Members",
                      "Column %s (%s): which direction does its STRONG axis resist, X or Y?" % (c.get("id"), lab),
                      "column stiffness and hinges in each frame direction", options=["X", "Y"])
        elif lab and section_type(lab) not in ("W", "HP", "M", "S", "HSS", "PIPE"):
            _item(items, "columns.%s.section" % c.get("id"), "refused", path + "/section", "Members",
                  "Column %s: %s shapes are not supported as columns in version 1." % (c.get("id"), section_type(lab)), "the model")

    def flush_sections():
        for q, (raw, users) in bad_sections.items():
            mapped = (d.get("section_map") or {}).get(q)
            extra = (" (the name you gave, '%s', is not one either)" % mapped) if mapped else ""
            _item(items, "section_map.%s" % q, "key", "section_map/%s" % q, "Members",
                  "Section '%s' (%d member(s), e.g. %s) is not an AISC shape%s. Give the AISC label that is the same "
                  "section (e.g. W14X90, HSS8X8X1/2); version 1 has no other section library." % (raw, len(users), ", ".join(users[:3]), extra),
                  "stiffness, strength and the hinge model")
        bad_sections.clear()

    def on_column(xy, z):
        for (z0, z1) in col_nodes.get((round(xy[0], 4), round(xy[1], 4)), []):
            if z0 - 1e-6 <= z <= z1 + 1e-6:
                return True
        return False

    node_pts = set()                                # every point a member end lands on: (x, y, z)
    for (x, y), spans in col_nodes.items():
        for z0, z1 in spans:
            for _, zz in lv:
                if zz is not None and z0 - 1e-6 <= zz <= z1 + 1e-6:
                    node_pts.add((x, y, round(zz, 4)))
    rel_ok = {"none", "i", "j", "both"}
    link_beams = []
    for b in beams:
        path = "beams[id=%s]" % b.get("id")
        sec_check(b, "beams", path)
        z = G.z(b.get("level"))
        a, c = G.xy(b.get("i")), G.xy(b.get("j"))
        if grid_ok and (z is None or a is None or c is None):
            _item(items, "beams.%s.at" % b.get("id"), "key", path, "Members",
                  "Beam %s: level '%s' or ends %s - %s do not resolve." % (b.get("id"), b.get("level"), _fmt_pt(b.get("i")), _fmt_pt(b.get("j"))),
                  "the whole model")
        elif a is not None and c is not None:
            if abs(a[0] - c[0]) > 1e-6 and abs(a[1] - c[1]) > 1e-6:
                _item(items, "beams.%s.at" % b.get("id"), "refused", path, "Members",
                      "Beam %s runs diagonally in plan (%s - %s): version 1 models beams along X or Y only." % (b.get("id"), _fmt_pt(b.get("i")), _fmt_pt(b.get("j"))),
                      "floor load paths")
            elif a == c:
                _item(items, "beams.%s.at" % b.get("id"), "key", path, "Members", "Beam %s has zero length." % b.get("id"), "the model")
            else:
                node_pts.add((round(a[0], 4), round(a[1], 4), round(z, 4)))
                node_pts.add((round(c[0], 4), round(c[1], 4), round(z, 4)))
        r = str(b.get("release") if b.get("release") is not None else "").strip().lower()
        if r not in rel_ok:
            _item(items, "beams.%s.release" % b.get("id"), "key", path + "/release", "Members",
                  "Beam %s: end connections? none (moment at both ends), I or J (that end pinned), or both (simple)." % b.get("id"),
                  "which beams are in moment frames and where hinges form", options=["none", "I", "J", "both"])
        rb = b.get("rbs")
        if rb is True or (isinstance(rb, dict) and not all(_num(rb.get(k)) for k in ("a_in", "b_in", "c_in"))):
            _item(items, "beams.%s.rbs" % b.get("id"), "key", path + "/rbs", "Member details",
                  "Beam %s is an RBS: give the cut a, b and c (in)." % b.get("id"), "the RBS hinge strength and location")
        if b.get("link"):
            link_beams.append(b)
    any_brb = False
    for br in braces:
        path = "braces[id=%s]" % br.get("id")
        if isinstance(br.get("brb"), dict):
            any_brb = True
            if _num(br["brb"].get("Asc_in2")) is None:
                _item(items, "braces.%s.brb.Asc" % br.get("id"), "key", path + "/brb/Asc_in2", "Member details",
                      "BRB %s: steel core area Asc (in2)?" % br.get("id"), "the BRB strength and stiffness")
        else:
            lab = sec_check(br, "braces", path)
            if lab and section_type(lab) not in ("HSS", "PIPE", "W", "HP", "2L", "L", "WT"):
                _item(items, "braces.%s.section" % br.get("id"), "refused", path + "/section", "Members",
                      "Brace %s: %s shapes are not supported as braces in version 1." % (br.get("id"), section_type(lab)), "the brace model")
        a, c = G.xy(br.get("i")), G.xy(br.get("j"))
        za, zc = G.z((br.get("i") or {}).get("level")), G.z((br.get("j") or {}).get("level"))
        if grid_ok and (a is None or c is None or za is None or zc is None):
            _item(items, "braces.%s.at" % br.get("id"), "key", path, "Members",
                  "Brace %s: ends %s - %s do not resolve to a point and a level." % (br.get("id"), _fmt_pt(br.get("i")), _fmt_pt(br.get("j"))),
                  "the whole model")
        elif a is not None and c is not None:
            node_pts.add((round(a[0], 4), round(a[1], 4), round(za, 4)))
            node_pts.add((round(c[0], 4), round(c[1], 4), round(zc, 4)))
        bs = br.get("system")
        if bs:
            k = norm_system(bs)
            if not (isinstance(k, str) and SYSTEMS_V1[k]["kind"] == "BF"):
                _item(items, "braces.%s.system" % br.get("id"), "key", path + "/system", "Frames",
                      "Brace %s: system '%s' is not one of SCBF, OCBF, BRBF, EBF." % (br.get("id"), bs), "the brace model",
                      options=["SCBF", "OCBF", "BRBF", "EBF"])

    flush_sections()

    # every beam end and brace end must land on something
    if grid_ok:
        for b in beams:
            z = G.z(b.get("level"))
            for end in ("i", "j"):
                p = G.xy(b.get(end))
                if p is None or z is None:
                    continue
                key = (round(p[0], 4), round(p[1], 4), round(z, 4))
                others = sum(1 for bb in beams if bb is not b and G.z(bb.get("level")) == z and any(
                    G.xy(bb.get(e)) is not None and (round(G.xy(bb.get(e))[0], 4), round(G.xy(bb.get(e))[1], 4)) == key[:2] for e in ("i", "j")))
                br_end = any((G.xy(br.get(e)) is not None and G.z((br.get(e) or {}).get("level")) is not None and
                              (round(G.xy(br.get(e))[0], 4), round(G.xy(br.get(e))[1], 4), round(G.z((br.get(e) or {}).get("level")), 4)) == key)
                             for br in braces for e in ("i", "j"))
                if not on_column(p, z) and not others and not br_end and not _on_member_line(p, z, beams, b, G):
                    _item(items, "beams.%s.end_%s" % (b.get("id"), end), "key", "beams[id=%s]/%s" % (b.get("id"), end), "Members",
                          "Beam %s: end %s (%s at %s) frames into nothing (no column, beam or brace there)." % (b.get("id"), end.upper(), _fmt_pt(b.get(end)), b.get("level")),
                          "a stable model")
        for br in braces:
            for end in ("i", "j"):
                p = G.xy(br.get(end)); z = G.z((br.get(end) or {}).get("level"))
                if p is None or z is None:
                    continue
                if not on_column(p, z) and not _on_member_line(p, z, beams, None, G) and abs(z - (lv[0][1] or 0.0)) > 1e-6:
                    _item(items, "braces.%s.end_%s" % (br.get("id"), end), "key", "braces[id=%s]/%s" % (br.get("id"), end), "Members",
                          "Brace %s: end %s (%s) does not land on a column or a beam." % (br.get("id"), end.upper(), _fmt_pt(br.get(end))),
                          "a stable model")
    for b in link_beams:
        z = G.z(b.get("level"))
        ends = [G.xy(b.get(e)) for e in ("i", "j")]
        hit = any(G.xy(br.get(e)) is not None and G.z((br.get(e) or {}).get("level")) == z and
                  any(q is not None and abs(G.xy(br.get(e))[0] - q[0]) < 1e-6 and abs(G.xy(br.get(e))[1] - q[1]) < 1e-6 for q in ends)
                  for br in braces for e in ("i", "j"))
        if not hit:
            _item(items, "beams.%s.link" % b.get("id"), "key", "beams[id=%s]/link" % b.get("id"), "Frames",
                  "Beam %s is marked as an EBF link but no brace frames into either of its ends." % b.get("id"), "the EBF link model")
    if link_beams and (d.get("details") or {}).get("link_ends_in_diaphragm") is None:
        _item(items, "details.link_ends_in_diaphragm", "default", "details/link_ends_in_diaphragm", "Frames",
              "EBF link ends: are they slaved to the floor diaphragm?", "the axial force in the link and the beam outside it",
              default=False, reason="not slaved: the link and the beam outside it carry axial force (the usual EBF modelling)",
              options=[False, True])
    if link_beams and not any(SYSTEMS_V1.get(s, {}).get("braced") == "EBF" or s == "EBF" for s in sys_ok.values()):
        _item(items, "frames.ebf", "key", "seismic/system", "Frames",
              "Beams are marked as EBF links but neither direction's system is EBF.", "the link model and acceptance")
    braced_dirs = {dr for dr, s in sys_ok.items() if braced_system(s)}
    if braces and not braced_dirs and sys_ok:
        _item(items, "frames.braces", "key", "seismic/system", "Frames",
              "Braces are given but neither direction's system is a braced frame: which system do they belong to?",
              "the brace model", options=["SCBF", "OCBF", "BRBF", "EBF"])

    # ---------------------------------------------------------------- supports, diaphragm
    sup = d.get("supports") or {}
    if str(sup.get("default") or "").lower() not in ("fixed", "pinned"):
        explicit = {(str(a.get("x")), str(a.get("y"))) for a in (sup.get("at") or []) if isinstance(a, dict)
                    and str(a.get("fixity") or "").lower() in ("fixed", "pinned")}
        base = lv[0][0] if lv else None
        missing = [c for c in cols if str(c.get("base_level")) == str(base) and (str(c.get("x")), str(c.get("y"))) not in explicit]
        if missing:
            _item(items, "supports.default", "key", "supports/default", "Supports and diaphragms",
                  "Column bases: fixed or pinned? (%d base column(s) have no fixity given)" % len(missing),
                  "the boundary conditions", options=["fixed", "pinned"])
    for a in (sup.get("at") or []):
        if isinstance(a, dict) and str(a.get("fixity") or "").lower() not in ("fixed", "pinned"):
            _item(items, "supports.at.%s.%s" % (a.get("x"), a.get("y")), "refused", "supports/at", "Supports and diaphragms",
                  "Support at %s: '%s' -- version 1 models fixed or pinned bases only (no springs)." % (_fmt_pt(a), a.get("fixity")),
                  "the boundary conditions")
    dia = str(d.get("diaphragm") or "").lower().replace("semirigid", "semi-rigid")
    if dia not in ("rigid", "flexible", "semi-rigid"):
        if isinstance(d.get("diaphragm"), dict):
            _item(items, "diaphragm", "refused", "diaphragm", "Supports and diaphragms",
                  "Diaphragm types differ by level: version 1 takes one diaphragm type for the whole building.", "the floor model")
        else:
            _item(items, "diaphragm", "key", "diaphragm", "Supports and diaphragms",
                  "Floor diaphragms: rigid, flexible or semi-rigid?", "mass distribution and torsion",
                  options=["rigid", "flexible", "semi-rigid"])

    # ---------------------------------------------------------------- loads
    lo = d.get("loads") or {}
    for k, nm in (("D_psf", "dead load D"), ("L_psf", "floor live load L")):
        v = lo.get(k)
        ok = _num(v) is not None or (isinstance(v, dict) and v and all(_num(x) is not None for x in v.values()))
        if not ok and not (k == "L_psf" and len(lv) == 2 and isinstance(v, dict) and "roof" in v):
            _item(items, "loads." + k, "key", "loads/" + k, "Gravity loads",
                  "Give the %s in psf (one value, or floor / roof, or per level)." % nm, "masses, Chapter 16 gravity and DDM load cases")
    for k, dv, why in (("Lr_psf", 20.0, "ASCE 7-22 Table 4.3-1 ordinary flat roof"),
                       ("snow_psf", 0.0, "no snow load given"),
                       ("clad_psf", 0.0, "no cladding weight given (it is then taken as included in D)")):
        if _num(lo.get(k)) is None:
            _item(items, "loads." + k, "default", "loads/" + k, "Gravity loads",
                  "%s (psf)?" % {"Lr_psf": "Roof live load Lr", "snow_psf": "Flat-roof snow load pf",
                                 "clad_psf": "Exterior wall (cladding) weight"}[k], "masses and gravity cases", default=dv, reason=why)
    if _num(lo.get("clad_psf")) and _num(lo.get("parapet_ft")) is None:
        _item(items, "loads.parapet_ft", "default", "loads/parapet_ft", "Gravity loads",
              "Parapet height above the roof (ft)?", "the cladding mass at the roof (and the wind on the parapet)",
              default=0.0, reason="no parapet given")
    if str(lo.get("floor_system") or "").lower() not in ("one-way", "two-way"):
        _item(items, "loads.floor_system", "default", "loads/floor_system", "Gravity loads",
              "Floor load distribution to the beams: one-way (give the deck span X or Y) or two-way?",
              "beam gravity demands", default="two-way", reason="tributary two-way distribution when the deck span is not given",
              options=["one-way", "two-way"])
    elif str(lo.get("floor_system")).lower() == "one-way" and str(lo.get("deck_span") or "").upper() not in ("X", "Y"):
        _item(items, "loads.deck_span", "key", "loads/deck_span", "Gravity loads", "One-way floors: does the deck span in X or Y?",
              "beam gravity demands", options=["X", "Y"])

    # ---------------------------------------------------------------- materials
    ma = d.get("materials") or {}
    types = {section_type(resolve_section(d, m.get("section"))) for m in cols + beams + braces if resolve_section(d, m.get("section"))}
    if ma.get("existing"):
        for k, t in (("W", "W"), ("HSS", "HSS")):
            if t in types and not norm_grade(ma.get(k)):
                _item(items, "materials." + k, "key", "materials/" + k, "Materials",
                      "Existing structure: the %s-shape steel grade (and era)?" % t, "expected strengths Ry Fy")
        if not (ma.get("existing_info") or {}).get("year"):
            _item(items, "materials.existing_year", "key", "materials/existing_info/year", "Materials",
                  "Existing structure: year (or decade) of construction?", "the existing-material properties and the report warning")
    else:
        for k, t, dv in (("W", "W", "A992"), ("HSS", "HSS", "A500 Gr C")):
            if t in types or (k == "W" and types & {"HP", "M", "S", "WT"}):
                if ma.get(k) and not norm_grade(ma.get(k)):
                    _item(items, "materials." + k, "key", "materials/" + k, "Materials",
                          "%s grade '%s' is not one version 1 knows (%s)." % (k, ma.get(k), ", ".join(sorted(GRADES))),
                          "expected strengths Ry Fy", options=sorted(GRADES))
                elif not ma.get(k):
                    _item(items, "materials." + k, "default", "materials/" + k, "Materials",
                          "%s-shape steel grade?" % k, "expected strengths Ry Fy", default=dv,
                          reason="the usual grade for new %s shapes" % k)
    if isinstance(ma.get("by_member"), dict) and len({norm_grade(v) for v in ma["by_member"].values()}) > 1:
        _item(items, "materials.by_member", "refused", "materials/by_member", "Materials",
              "Different grades for members of the same shape type: version 1 takes one grade per shape type.",
              "expected strengths")
    if any_brb:
        bb = ma.get("brb") or {}
        for k, q in (("Fysc_ksi", "BRB core yield stress Fysc (ksi, the adjusted / measured value)"),
                     ("KF", "BRB stiffness modification factor KF (supplier)"),
                     ("beta", "BRB compression strength adjustment factor beta (supplier tests)"),
                     ("omega", "BRB strain hardening adjustment factor omega (supplier tests)")):
            if _num(bb.get(k)) is None:
                _item(items, "materials.brb." + k, "key", "materials/brb/" + k, "Member details", q + "?", "the BRB model")

    # ---------------------------------------------------------------- details
    de = d.get("details") or {}
    has_mf = any(str(b.get("release") or "").lower() in ("none", "i", "j") and not b.get("link") for b in beams)
    mf_dirs = {dr for dr, s in sys_ok.items() if SYSTEMS_V1[s]["kind"] in ("MF", "DUAL")}
    if has_mf and mf_dirs and de.get("doublers") is None:
        _item(items, "details.doublers", "default", "details/doublers", "Member details",
              "Panel-zone doubler plates in the moment frames? List them (level, column, thickness), or accept none.",
              "panel-zone stiffness and strength", default=[], reason="no doubler plates given")

    # ---------------------------------------------------------------- optional data
    if not d.get("wind"):
        _item(items, "wind", "info", "wind", "Wind (for the DDM)", "No wind data: the DDM wind and roof-uplift cases are reported NOT EVALUATED.", "DDM wind cases")
    if not d.get("user_results"):
        _item(items, "user_results", "info", "user_results", "Your own results",
              "No results of your own analysis: the confirmation check of T1, V and W is skipped, and the report says so.",
              "the confirmation check")

    for n, why in enumerate(d.get("_unsupported") or []):
        _item(items, "unsupported.%d" % n, "refused", "_unsupported", "Not supported in version 1", str(why), "the model")

    # an intake proposal (e.g. D derived from the .e2k area loads + slab weight) turns a key item into a default
    props = d.get("_proposals") or {}
    for it in items:
        pr = props.get(it["path"])
        if it["kind"] in ("key", "default") and pr is not None:
            it["kind"] = "default"
            it["default"] = pr.get("value")
            it["default_reason"] = pr.get("reason")

    # answered / accepted items drop out
    ans = d.get("_answers") or {}
    out = []
    for it in items:
        a = ans.get(it["id"])
        if a and it["kind"] == "default" and a.get("accept"):
            continue
        out.append(it)
    counts = {k: sum(1 for i in out if i["kind"] == k) for k in ("key", "default", "refused", "info")}
    return dict(items=out, counts=counts, ready=counts["key"] == 0 and counts["default"] == 0 and counts["refused"] == 0)


def _on_member_line(p, z, beams, skip, G):
    """True when plan point p at elevation z lies on (inside) another beam at that level -- a node the translator
    makes by splitting that beam (chevron work point, link end)."""
    for b in beams:
        if b is skip or G.z(b.get("level")) is None or abs(G.z(b.get("level")) - z) > 1e-6:
            continue
        a, c = G.xy(b.get("i")), G.xy(b.get("j"))
        if a is None or c is None:
            continue
        if abs(a[1] - c[1]) < 1e-6 and abs(p[1] - a[1]) < 1e-6 and min(a[0], c[0]) - 1e-6 <= p[0] <= max(a[0], c[0]) + 1e-6:
            return True
        if abs(a[0] - c[0]) < 1e-6 and abs(p[0] - a[0]) < 1e-6 and min(a[1], c[1]) - 1e-6 <= p[1] <= max(a[1], c[1]) + 1e-6:
            return True
    return False


# ------------------------------------------------------------------------------------------- answers
def apply_answers(desc, answers) -> dict:
    """A copy of desc with the answers written in. answers = {item_id: {"value": v} | {"accept": true}} where
    "accept" takes the item's proposed default. Items are matched against a fresh validation, so an answer to an
    item that no longer exists is ignored (and reported under _answers_unused)."""
    d = copy.deepcopy(desc)
    d.setdefault("_answers", {})
    unused = []
    for _round in range(3):                   # an answer can unlock new items (e.g. the system -> R defaults)
        items = {i["id"]: i for i in validate(d)["items"]}
        changed = False
        for iid, a in (answers or {}).items():
            it = items.get(iid)
            if it is None or iid in d["_answers"]:
                continue
            if a.get("accept") and it["kind"] == "default":
                set_path(d, it["path"], copy.deepcopy(it["default"]))
                mark(d, it["path"], "default", it.get("default_reason"))
                d["_answers"][iid] = {"accept": True, "value": it["default"]}
                changed = True
            elif "value" in a and it["kind"] in ("key", "default"):
                v = a["value"]
                if (it["path"].endswith("/section") or it["path"].startswith("section_map/")) and norm_section(v):
                    v = norm_section(v)
                set_path(d, it["path"], v)
                mark(d, it["path"], "answer")
                d["_answers"][iid] = {"value": v}
                changed = True
        if not changed:
            break
    left = {i["id"] for i in validate(d)["items"]}
    unused = [k for k in (answers or {}) if k not in d["_answers"] and k in left] + \
             [k for k in (answers or {}) if k not in d["_answers"] and k not in left]
    if unused:
        d["_answers_unused"] = sorted(set(unused))
    return d


def new_description(kind="json", file=None, sha256=None) -> dict:
    return {"schema": SCHEMA, "version": VERSION, "source": {"kind": kind, "file": file, "sha256": sha256},
            "project": {}, "seismic": {}, "grid": {"x": [], "y": []}, "levels": [], "columns": [], "beams": [],
            "braces": [], "supports": {}, "diaphragm": None, "loads": {}, "materials": {}, "details": {},
            "wind": None, "user_results": None, "_provenance": {}}


def missing_items_doc(desc, name=None) -> dict:
    """missing_items.json: what HR needs before it can build, grouped for one pass."""
    v = validate(desc)
    groups = {}
    for it in v["items"]:
        groups.setdefault(it["group"], []).append(it)
    return {"schema": "steltic.missing_items", "version": 1, "building": name, "ready": v["ready"], "counts": v["counts"],
            "groups": [{"group": g, "items": its} for g, its in groups.items()],
            "how_to_answer": "answers.json = {\"<item id>\": {\"value\": <your value>} | {\"accept\": true}}; "
                             "\"accept\" takes the proposed default. Refused items cannot be answered: they are outside version 1."}

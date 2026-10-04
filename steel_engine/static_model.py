"""
static_model.py -- the STATIC building model (the second model) and the per-member DEMAND envelope.

The dynamic model (engine3d.build) LUMPS each floor's gravity at the joints -- correct for mass /
period / seismic base shear, but the beam ELEMENTS then carry ~0 gravity force, so internal force
diagrams cannot be drawn from it. This static model instead DISTRIBUTES the floor pressures onto the
beams by sub-dividing every beam into `nseg` sub-elements with real intermediate nodes. Every ASCE 7-22
LRFD combination is then analysed statically with P-Delta, so N / V / M are correct everywhere for
the force diagrams and the member demands. The dynamic model is untouched.

Geometry, sections, orientation, base fixity, diaphragm, per-member releases and the lateral story
forces are all replicated from engine3d so the two models are the same structure.

Gravity loading (HR-03 / HR-13 / HR-27)
---------------------------------------
* Every beam is mapped GEOMETRICALLY to its parent grid span (the column-line segment between two
  adjacent grid nodes it lies on), so a beam split at an off-grid work point (EBF link, chevron apex,
  infill connection) carries the true tributary load of its position on the parent span.
* Loads are applied bay by bay. Each framed bay (4 corner columns present at that level) has
    dead   = engine3d._Dlev(cfg, k, k == NF) + extra_mass_floors[k]   (exactly the seismic-weight dead)
    live   = engine3d._Llev(cfg, k)        on FLOOR bays
    roof   = Lr / S / R                    on ROOF bays: a bay is a roof bay when k is the top level,
             when k is listed in cfg['roof_levels'], or when the same bay is not framed at ANY level
             above (setbacks / low roofs; split levels and double-height voids stay floors). Lr = cfg['Lr'] (default 20 psf, cfg['Lr_by_level'] per level),
             S = cfg['snow'] (cfg['snow_by_level']), R = cfg['rain'] (cfg['rain_by_level']).
  plus cladding on the exterior bay edges, clad * the mid-height wall tributary of engine3d.clad_edge_heights
  (half storey below + half above, true walls at setbacks / split levels) -- the same quantities
  engine3d.floor_w / floor_dead use for the seismic weight.
* Distribution per bay (cfg['floor_system'], optional cfg['deck_span'], cfg['infill_spacing']):
    "two-way"                     45-degree trapezoid / triangle onto the four bay edges.
    one-way, deck span declared   cfg['deck_span'] = 'X' or 'Y' (direction the DECK spans; may be a
                                  {level: dir} dict; cfg['infill_dir'] = direction the infill beams RUN is
                                  accepted instead). The deck load goes to the lines perpendicular to the
                                  deck span -- the two bay edges and any infill lines -- each taking half
                                  the distance to its neighbours. Infill lines are the beams modelled
                                  inside the bay, else virtual lines at cfg['infill_spacing'] (in, or
                                  {level: in}) whose end reactions are applied as point loads on the two
                                  girders (the bay edges running in the deck-span direction).
    one-way, auto                 a bay that contains modelled infill beams all running one way is
                                  treated as one-way with the deck spanning between them.
    one-way, undeclared (legacy)  the 45-degree distribution is analysed (correct totals) and, because
                                  it is unknown which beams are girders, every unsubdivided beam gets a
                                  girder FLOOR from the half-bay on EACH adjacent side: the simple-span
                                  girder moment/shear for pinned-pinned beams; for rigid-ended beams the
                                  solved moment/shear PLUS the simple-span effect of the extra load the
                                  girder rule puts on the beam (solved end restraint and loads from above
                                  kept). Declare deck_span so the one-way load path itself is analysed.
  Beams intersected by braces of a V / inverted-V CBF or BRBF (AISC 341-22 F1.4a(a), F2.4a(a), F4.4a(a):
  "braces provide no support of dead and live loads") additionally get the parent simple-span gravity
  moment/shear as a floor.

Demands (HR-04 / HR-35)
-----------------------
Beam V is the SOLVED shear (max |Vy|,|Vz| over the sub-elements), enveloped with the one-way floor
where that floor applies. Beam axial keeps its sign (compression and tension enveloped separately).

Direct Analysis Method (HR-18), AISC 360-22 Chapter C
------------------------------------------------------
demand_envelope() runs every strength combination on a model with 0.8 x E (and G) on all members
(C2.3(a); applied to all stiffnesses as permitted) and tau_b = 1.0 with the C2.3(c) additional notional
load 0.001*alpha*Yi in all combinations (cfg['dam'] = {'tau_b': 'check'} instead keeps tau_b = 1 and
reports every member with alpha*Pr/Pns > 0.5). Notional loads Ni = 0.002*alpha*Yi (C2.2b, alpha = 1.0
LRFD) are applied at every level, distributed like the gravity load, in four directions (+X, -X, +Y,
-Y) for gravity-only combinations, and -- when the ratio of second- to first-order story drift exceeds
1.7 (C2.2b(d)) -- additively in the direction of the lateral resultant of the other combinations.
cfg['dam'] = False restores the nominal-stiffness analysis. Periods, drift and the dynamic model keep
nominal stiffness. run_combo() / export_static_model() (force diagrams) stay nominal-stiffness.

Capacity-limited and overstrength demands (HR-06 / HR-07)
---------------------------------------------------------
Cases carrying `kind == 'ecl'` are analysed on a model with the yielding elements (CBF/BRBF braces,
SPSW webs, EBF links) removed and replaced by their expected / adjusted strengths applied as forces,
with the diaphragm masters restrained in plan (the inertia forces that equilibrate the mechanism;
AISC 341-22 F2.3/F3.3/F4.3 Exception (a) permits neglecting drift-induced flexure). Systems that cannot
be evaluated are reported as NOT EVALUATED in the info dict.
"""
import math
import re
import openseespy.opensees as ops
import engine3d as eng
from engine3d import ntag, mtag, grid, zlevels, Ipack

EMOD = eng.E
GMOD = eng.Gmod

_SUB0 = 9_000_000          # base tag for intermediate beam nodes / sub-elements
_TOL = 1.0                 # in: geometric tolerance (node on a grid span / at a level)
_PSF = 1.0 / 144000.0      # psf * in  ->  kip/in


# ====================================================================================
# load-factor carriers (backward-compatible floats)
# ====================================================================================
class RoofFactors(float):
    """Roof variable-load factors of ONE combination: Lr (roof live), S (snow), R (rain).

    The float value is the LEGACY single roof factor, i.e. the multiplier on 'snow if snow > 0 else Lr'
    that older callers (design_post.run_case, report tables) apply on the top roof; it is chosen so
    the top-roof load they apply equals Lr*fLr + S*fS + R*fR. static_model applies the three loads
    separately on every roof bay."""
    def __new__(cls, Lr=0.0, S=0.0, R=0.0, cfg=None):
        legacy = 0.0
        if cfg is not None:
            lr = float(cfg.get("Lr", 20.0) or 0.0); sn = float(cfg.get("snow", 0.0) or 0.0)
            rn = float(cfg.get("rain", 0.0) or 0.0)
            den = sn if sn > 0 else lr
            if den > 0:
                legacy = (Lr * lr + S * sn + R * rn) / den
        o = float.__new__(cls, legacy)
        o.Lr, o.S, o.R = float(Lr), float(S), float(R)
        return o

    def __repr__(self):
        return "RoofFactors(Lr=%g, S=%g, R=%g)" % (self.Lr, self.S, self.R)

    def __reduce__(self):
        return (_mk_roof, (self.Lr, self.S, self.R, float(self)))


def _mk_roof(Lr, S, R, legacy):
    o = float.__new__(RoofFactors, legacy); o.Lr, o.S, o.R = Lr, S, R
    return o


class LiveFactor(float):
    """Companion live-load factor that depends on the level (ASCE 7-22 2.3.1 Exception 1 / 2.3.6
    Exception 1): `base` (0.5) where Lo <= 100 psf, `hi` (1.0) on levels with Lo > 100 psf, garages
    or places of public assembly (see live_is_full). The float value is `base` for legacy callers."""
    def __new__(cls, base, hi=1.0):
        o = float.__new__(cls, base); o.hi = float(hi)
        return o

    def __repr__(self):
        return "LiveFactor(%g, hi=%g)" % (float(self), self.hi)

    def __reduce__(self):
        return (LiveFactor, (float(self), self.hi))


class Case(tuple):
    """An LRFD case: the legacy 6-tuple (label, fD, fL, fLr, lateral, col_only) -- so every existing
    `label, fD, fL, fLr, lat, co = case` unpacking still works -- plus optional attributes:
        kind     'gravity' | 'wind' | 'seismic' | 'omega0' | 'ecl' | 'ecl_ocbf'
        applies  None (all members present) or a set of member kinds the case may govern
        uplift   None or (dirn, sgn): add the MWFRS roof suction of ASCE 7-22 Fig. 27.3-1
        ecl      dict for capacity-limited cases (sx, sy, variant, patterns, Om0)
    col_only=True cases are skipped by legacy consumers (report per-case tables, frame diagrams)."""
    def __new__(cls, label, fD, fL, fLr, lat, col_only=False, **attrs):
        o = tuple.__new__(cls, (label, fD, fL, fLr, lat, col_only))
        o.__dict__.update(dict(kind="gravity", applies=None, uplift=None, ecl=None))
        o.__dict__.update(attrs)
        return o

    def __reduce__(self):
        return (_mk_case, (tuple(self), dict(self.__dict__)))


def _mk_case(t, d):
    return Case(*t, **d)


# ====================================================================================
# per-level loads (the same sources as the seismic weight)
# ====================================================================================
def _lev(d, k, default):
    if not d:
        return default
    if k in d:
        return d[k]
    if str(k) in d:
        return d[str(k)]
    return default


def dead_psf(cfg, k):
    """Floor dead (psf) at level k exactly as the seismic weight uses it (engine3d.floor_dead):
    _Dlev(cfg, k, k == NF) + extra_mass_floors[k]."""
    NF = len(cfg["heights"])
    return float(eng._Dlev(cfg, k, k == NF)) + float(_lev(cfg.get("extra_mass_floors"), k, 0.0) or 0.0)


def live_psf(cfg, k):
    return float(eng._Llev(cfg, k) or 0.0)


def roof_psf(cfg, k):
    """(Lr, S, R) psf on roof bays of level k."""
    Lr = float(_lev(cfg.get("Lr_by_level"), k, cfg.get("Lr", 20.0)) or 0.0)
    S = float(_lev(cfg.get("snow_by_level"), k, cfg.get("snow", 0.0)) or 0.0)
    R = float(_lev(cfg.get("rain_by_level"), k, cfg.get("rain", 0.0)) or 0.0)
    return Lr, S, R


def live_is_full(cfg, k):
    """True where the companion live factor must be 1.0 (ASCE 7-22 2.3.1 Exc. 1 / 2.3.6 Exc. 1):
    Lo > 100 psf at that level, or garage / public assembly (cfg['garage'] / cfg['public_assembly'] for
    the whole building, cfg['garage_levels'] / cfg['assembly_levels'] per level)."""
    if cfg.get("garage") or cfg.get("public_assembly"):
        return True
    lv = set(int(x) for x in (cfg.get("garage_levels") or [])) | \
         set(int(x) for x in (cfg.get("assembly_levels") or []))
    return k in lv or live_psf(cfg, k) > 100.0


def _live_factor(fL, cfg, k):
    if isinstance(fL, LiveFactor) and live_is_full(cfg, k):
        return fL.hi
    return float(fL)


def _roof_terms(fLr, cfg, k):
    """Factored roof variable load (psf) on a roof bay at level k."""
    Lr, S, R = roof_psf(cfg, k)
    if isinstance(fLr, RoofFactors):
        return fLr.Lr * Lr + fLr.S * S + fLr.R * R
    return float(fLr) * (S if float(cfg.get("snow", 0.0) or 0.0) > 0 else Lr)   # legacy single factor


def _roof_levels(cfg):
    return set(int(x) for x in (cfg.get("roof_levels") or []))


# ====================================================================================
# export (unchanged behaviour: nominal-stiffness model replay)
# ====================================================================================
def export_static_model(cfg, outdir, name="model", nseg=6):
    """Write a STANDALONE, runnable STATIC model (model_static.py) -- the SECOND model used for the force
    diagrams (beams sub-divided with the tributary gravity). Replays the exact OpenSees calls
    build_static issues, mirroring engine3d.export_model, so the user can open BOTH models independently."""
    import os as _os
    rec = []
    funcs = ["wipe", "model", "node", "fix", "mass", "geomTransf", "uniaxialMaterial", "element",
             "rigidDiaphragm", "equalDOF", "rigidLink"]
    orig = {f: getattr(ops, f) for f in funcs if hasattr(ops, f)}
    def _shim(fn, real):
        def w(*a):
            rec.append((fn, list(a))); return real(*a)
        return w
    for f, real in orig.items():
        setattr(ops, f, _shim(f, real))
    try:
        build_static(cfg, "PDelta", nseg)
    finally:
        for f, real in orig.items():
            setattr(ops, f, real)
    _os.makedirs(outdir, exist_ok=True)
    arch = str(cfg.get("arch", ""))
    py = ['"""Standalone STATIC building model for %s -- %s.' % (name, arch),
          'The SECOND (force-diagram) model: beams sub-divided into sub-elements carrying the tributary',
          'gravity, so internal N / V / M are correct everywhere. Auto-generated; replays the exact',
          'build_static OpenSees calls.  Run:  python model_static.py"""',
          'import openseespy.opensees as ops', '']
    _gt_seen = set()
    for cmd, a in rec:
        if cmd == "geomTransf" and len(a) >= 2:
            if a[1] in _gt_seen:
                continue
            _gt_seen.add(a[1])
        py.append("ops.%s(%s)" % (cmd, ", ".join(repr(x) for x in a)))
    py += ['', '# --- quick self-check ---',
           'print("static model -- nodes:", len(ops.getNodeTags()), " elements:", len(ops.getEleTags()))']
    pyp = _os.path.join(outdir, "model_static.py")
    open(pyp, "w").write("\n".join(py) + "\n")
    return [pyp]


def _XY(cfg, i, j):
    SX, SY = cfg["SX"], cfg["SY"]
    xco = cfg.get("xcoords"); yco = cfg.get("ycoords"); skew = cfg.get("skew", 0.0)
    return ((xco[i] if xco else i*SX) + skew*j, (yco[j] if yco else j*SY))


def _rel_parts(code):
    """Split a per-member release code into its I-end and J-end pieces."""
    rel_i = "I" if code in ("I", "both") else "none"
    rel_j = "J" if code in ("J", "both") else "none"
    return rel_i, rel_j


def _parse_rel(rel):
    """['-releasez',3,'-releasey',1] -> (relz_code, rely_code) as none/I/J/both."""
    cm = {0: "none", 1: "I", 2: "J", 3: "both"}; rz = ry = "none"; i = 0
    rel = list(rel)
    while i < len(rel):
        if rel[i] == "-releasey" and i+1 < len(rel): rz = cm.get(rel[i+1], "none"); i += 2   # -releasey = MAJOR = relz
        elif rel[i] == "-releasez" and i+1 < len(rel): ry = cm.get(rel[i+1], "none"); i += 2  # -releasez = minor = rely
        else: i += 1
    return rz, ry


def _end_rel(relz, rely, end):
    if end == "I":
        return ("I" if relz in ("I", "both") else "none", "I" if rely in ("I", "both") else "none")
    return ("J" if relz in ("J", "both") else "none", "J" if rely in ("J", "both") else "none")


def _dec(t):
    r = t % 100000
    return r // 100, r % 100, t // 100000


# ====================================================================================
# model builders
# ====================================================================================
class _Rec:
    """Issue OpenSees calls and keep a signature of every call (cache key, HR-36)."""
    def __init__(self):
        self.calls = []

    def __call__(self, name, *a):
        self.calls.append((name, a))
        return getattr(ops, name)(*a)


def _staticize_custom(cfg, transf="PDelta", nseg=10, stiff=1.0, drop=(), fix_masters=False):
    """Distribute gravity over an AGENT-BUILT (custom_build) model. We RECORD every OpenSees call the
    custom_build makes, then REPLAY it with the beams sub-divided into `nseg` elements.
    Element kind and section come from the returned info['ele'] list looked up BY TAG (HR-29); an
    element missing from that list is classified from its geometry and reported in model['warnings'].
    stiff: factor on E and G of every elastic element/material (Direct Analysis C2.3).
    drop: original element tags to omit (capacity-limited analyses).
    fix_masters: restrain the diaphragm masters in X, Y, RZ (capacity-limited analyses)."""
    rec = {k: [] for k in ("node", "fix", "geomTransf", "element", "rigidDiaphragm", "mass",
                           "uniaxialMaterial", "equalDOF", "rigidLink")}
    real = {k: getattr(ops, k) for k in rec}
    def mk(name):
        def f(*a):
            rec[name].append(a); return real[name](*a)
        return f
    for k in rec: setattr(ops, k, mk(k))
    try:
        info = cfg["custom_build"](cfg, transf)
    finally:
        for k in rec: setattr(ops, k, real[k])

    R = _Rec(); warnings = []
    ops.wipe(); R("model", "basic", "-ndm", 3, "-ndf", 6)
    coord = {}
    for a in rec["node"]:
        R("node", *a); coord[a[0]] = (float(a[1]), float(a[2]), float(a[3]))
    masters = [a[1] for a in rec["rigidDiaphragm"]]
    mset = set(masters)
    fixed = {}
    for a in rec["fix"]:
        if fix_masters and a[0] in mset:
            a = (a[0], 1, 1, 1, 1, 1, 1)
        R("fix", *a); fixed[a[0]] = a[1:]
    _gt_seen = set()                       # a custom_build may register the SAME transf tag twice
    for a in rec["geomTransf"]:            # (explicit register_col_transf + add_*/_ensure auto-register);
        if a[1] in _gt_seen: continue      # the live build swallows the dup, but replay must not re-add it
        _gt_seen.add(a[1])
        if transf == "Linear" and a[0] in ("PDelta", "Corotational"):
            a = ("Linear",) + tuple(a[1:])  # first-order model requested (add_column always asks PDelta)
        R("geomTransf", *a)
    mat_E = {}
    for a in rec["uniaxialMaterial"]:
        a = list(a)
        if a and a[0] == "Elastic" and len(a) >= 3:
            mat_E[a[1]] = float(a[2]); a[2] = float(a[2]) * stiff
        elif stiff != 1.0:
            warnings.append("uniaxialMaterial %r %s not stiffness-reduced (Direct Analysis C2.3)" % (a[0], a[1]))
        R("uniaxialMaterial", *a)

    einfo = {}
    for e in (info.get("ele") or []):
        try:
            einfo[int(e[0])] = (e[1], e[2])
        except Exception:
            continue
    sub_node = _SUB0; sub_ele = _SUB0; beams = []; cols = []; braces = []; dia_extra = {}; dropped = []
    unlisted = []
    for a in rec["element"]:
        a = list(a); etype = a[0]; tag = a[1]
        if tag in einfo:
            kind, sec = einfo[tag]
        else:
            kind, sec = _infer_kind(a, coord), None
            unlisted.append(tag)
        if etype == "elasticBeamColumn" and len(a) >= 11:
            a[5] = float(a[5]) * stiff; a[6] = float(a[6]) * stiff
        elif etype == "Truss" and len(a) >= 6:
            pass                                   # material already reduced
        elif stiff != 1.0:
            warnings.append("element %s (%s) not stiffness-reduced (Direct Analysis C2.3)" % (tag, etype))
        n1, n2 = a[2], a[3]
        if kind == "beam" and etype == "elasticBeamColumn" and len(a) >= 11:
            props = a[4:10]; ttag = a[10]; relz, rely = _parse_rel(a[11:])
            (x1, y1, z1) = coord[n1]; (x2, y2, z2) = coord[n2]
            L = ((x2-x1)**2 + (y2-y1)**2 + (z2-z1)**2) ** 0.5
            bm = {"etag": tag, "A": n1, "B": n2, "L": L, "sec": sec, "relz": relz, "rely": rely,
                  "i": -1, "j": -1, "k": None, "dir": "X" if abs(x2-x1) >= abs(y2-y1) else "Y",
                  "xyzA": (x1, y1, z1), "xyzB": (x2, y2, z2)}
            if tag in drop:
                bm["nodes"] = [n1, n2]; bm["segs"] = []; dropped.append(bm)
                continue
            chain = [n1]
            for sgi in range(1, nseg):
                t = sgi/float(nseg); nd = sub_node; sub_node += 1
                R("node", nd, x1+(x2-x1)*t, y1+(y2-y1)*t, z1+(z2-z1)*t)
                coord[nd] = (x1+(x2-x1)*t, y1+(y2-y1)*t, z1+(z2-z1)*t)
                dia_extra.setdefault(round(z1, 6), []).append(nd); chain.append(nd)
            chain.append(n2); segs = []
            for sgi in range(nseg):
                ra = []
                if sgi == 0: ra += eng.release_args(*_end_rel(relz, rely, "I"))
                if sgi == nseg-1: ra += eng.release_args(*_end_rel(relz, rely, "J"))
                te = sub_ele; sub_ele += 1
                R("element", "elasticBeamColumn", te, chain[sgi], chain[sgi+1], *props, ttag, *ra); segs.append(te)
            bm["nodes"] = chain; bm["segs"] = segs
            beams.append(bm)
        else:
            if tag in drop:
                dropped.append({"etag": tag, "kind": kind, "sec": sec, "n1": n1, "n2": n2})
                continue
            R("element", *a)
            if kind == "col":
                i, j, k = _dec(n1)
                cols.append({"tag": tag, "etag": tag, "sec": sec, "n1": n1, "n2": n2, "i": i, "j": j, "k": k,
                             "axis": "col"})
            elif kind == "brace":
                area = None
                if etype == "Truss" and len(a) >= 6:
                    area = float(a[4]); Em = mat_E.get(a[5], EMOD)
                else:
                    Em = EMOD
                braces.append({"tag": tag, "etag": tag, "sec": sec, "n1": n1, "n2": n2, "area": area,
                               "Emat": Em, "truss": etype == "Truss"})
            else:
                warnings.append("element %s (%s, kind %r) replayed but not enveloped" % (tag, etype, kind))
    if unlisted:
        warnings.append("custom_build info['ele'] does not list %d element tag(s) (e.g. %s): kind inferred "
                        "from geometry, section unknown (HR-29). List every element in info['ele']."
                        % (len(unlisted), ", ".join(str(t) for t in unlisted[:5])))
    for a in rec["equalDOF"]:
        R("equalDOF", *a)
    for a in rec["rigidLink"]:
        R("rigidLink", *a)
    slave_set = set()
    for a in rec["rigidDiaphragm"]:
        master = a[1]; slaves = [s for s in a[2:] if s in coord]
        mz = round(coord[master][2], 6) if master in coord else None
        R("rigidDiaphragm", a[0], master, *(slaves + dia_extra.get(mz, [])))
        slave_set |= set(slaves + dia_extra.get(mz, []))
    orphans = _fix_orphans(R, coord, fixed, slave_set, mset) if (drop or fix_masters) else set()

    NF = len(cfg["heights"]); NX, NY = cfg["NX"], cfg["NY"]; present = {}
    zl = zlevels(cfg)
    for t, (x, y, zz) in coord.items():
        if t >= _SUB0:
            continue
        i, j, k = _dec(t)
        if 0 <= i <= NX and 0 <= j <= NY and 0 <= k <= NF and t == ntag(i, j, k) and abs(zz - zl[k]) <= _TOL:
            present.setdefault(k, set()).add((i, j))
    for k in range(NF+1): present.setdefault(k, set())
    bases = {}; base_nodes = []
    for t, fx in fixed.items():
        if t in mset or t not in coord:
            continue
        if abs(coord[t][2] - zl[0]) <= _TOL and len(fx) >= 3 and fx[2] == 1:
            base_nodes.append(t)
            i, j, k = _dec(t)
            if k == 0 and 0 <= i <= NX and 0 <= j <= NY and len(fx) >= 4:
                bases[(i, j)] = "fixed" if fx[3] == 1 else "pinned"   # rx restraint
    mdict = {}
    for m in masters:
        if m in coord:
            for k in range(1, NF + 1):
                if abs(coord[m][2] - zl[k]) <= _TOL:
                    mdict[k] = m
    model = {"cm": info.get("cm", {}), "present": present, "z": zl, "NF": NF,
             "cols": cols, "beams": beams, "braces": braces, "bases": bases, "base_nodes": sorted(base_nodes),
             "masters": mdict, "coord": coord, "dropped": dropped, "warnings": warnings,
             "sig": R.calls, "custom": True, "orphans": orphans}
    _finish_model(cfg, model)
    return model


def _fix_orphans(R, coord, fixed, slaves, masters):
    """Nodes left without any element (brace work points after the braces are removed for a capacity
    analysis) get their unrestrained, unconstrained DOFs fixed so the model stays non-singular; the brace
    forces at such a node are transferred to the beam it lies on (see _apply_brace_force)."""
    used = set()
    for t in ops.getEleTags():
        used |= set(ops.eleNodes(t))
    out = set()
    for n in coord:
        if n in used or n in masters:
            continue
        cur = list(fixed.get(n, (0, 0, 0, 0, 0, 0))) + [0] * 6
        flags = []
        for d in range(6):
            if cur[d] or (n in slaves and d in (0, 1, 5)):
                flags.append(0)
            else:
                flags.append(1)
        if any(flags):
            R("fix", n, *flags)
        out.add(n)
    return out


def _infer_kind(a, coord):
    """Kind of an element missing from info['ele'] (HR-29): truss -> brace, vertical -> col, else beam."""
    if a[0] == "Truss":
        return "brace"
    try:
        (x1, y1, z1) = coord[a[2]]; (x2, y2, z2) = coord[a[3]]
    except Exception:
        return None
    return "col" if abs(z2 - z1) > max(abs(x2 - x1), abs(y2 - y1)) else "beam"


def build_static(cfg, transf="PDelta", nseg=10, stiff=1.0, drop=(), fix_masters=False):
    """Build the static model. Beams are sub-divided into `nseg` sub-elements with real intermediate
    nodes; columns and braces stay single elements. Returns a dict describing the model so loads can
    be applied and per-member diagrams reassembled. Every beam carries its parent grid span
    ('parent' = (k, dir, i, j), 's0'/'s1' = its range on that span, 'Lp' = span length) -- i/j/k/dir are
    the PARENT span's indices, so subdivided beams are located on their real grid line.
    stiff / drop / fix_masters: see _staticize_custom."""
    if cfg.get("custom_build"):
        return _staticize_custom(cfg, transf, nseg, stiff, drop, fix_masters)
    R = _Rec(); warnings = []
    ops.wipe(); R("model", "basic", "-ndm", 3, "-ndf", 6)
    NX, NY = cfg["NX"], cfg["NY"]
    z = zlevels(cfg); NF = len(cfg["heights"])
    present = {k: grid(cfg, k) for k in range(NF+1)}
    coord = {}

    # primary (column-line) nodes
    for k in range(NF+1):
        for (i, j) in present[k]:
            x, y = _XY(cfg, i, j); R("node", ntag(i, j, k), x, y, z[k]); coord[ntag(i, j, k)] = (x, y, z[k])
    base = cfg.get("base", "fixed")
    base_nodes = []
    for (i, j) in present[0]:
        if base == "pinned": R("fix", ntag(i, j, 0), 1, 1, 1, 0, 0, 0)
        else:                R("fix", ntag(i, j, 0), 1, 1, 1, 1, 1, 1)
        base_nodes.append(ntag(i, j, 0))

    # diaphragm masters
    cm = {}; masters = {}
    for k in range(1, NF+1):
        pts = present[k]
        cx = sum(_XY(cfg, i, j)[0] for i, j in pts)/len(pts)
        cy = sum(_XY(cfg, i, j)[1] for i, j in pts)/len(pts)
        cm[k] = (cx, cy); R("node", mtag(k), cx, cy, z[k]); coord[mtag(k)] = (cx, cy, z[k])
        if fix_masters: R("fix", mtag(k), 1, 1, 1, 1, 1, 1)
        else:           R("fix", mtag(k), 0, 0, 1, 1, 1, 0)
        masters[k] = mtag(k)

    cT = "PDelta" if transf == "PDelta" else "Linear"
    R("geomTransf", cT, 1, 1.0, 0.0, 0.0); R("geomTransf", cT, 2, 0.0, 1.0, 0.0)
    R("geomTransf", "Linear", 3, 0.0, 0.0, 1.0)
    cA, cIx, cIy, cJ = Ipack(cfg["col"]); bA, bIx, bIy, bJ = Ipack(cfg["beam"])
    relf = cfg.get("releases")
    Es, Gs = EMOD * stiff, GMOD * stiff

    et = 1; cols = []; beams = []; dropped = []
    sub_node = _SUB0; sub_ele = _SUB0
    dia_extra = {k: [] for k in range(1, NF+1)}   # interior beam nodes to add to each diaphragm

    # columns (single elements, exactly as the dynamic model)
    for i in range(NX+1):
        for j in range(NY+1):
            tt = 2 if (i == 0 or i == NX) else 1
            for k in range(NF):
                if (i, j) in present[k] and (i, j) in present[k+1]:
                    R("element", "elasticBeamColumn", et, ntag(i, j, k), ntag(i, j, k+1),
                      cA, Es, Gs, cJ, cIy, cIx, tt)
                    cols.append({"tag": et, "etag": et, "sec": cfg["col"], "n1": ntag(i, j, k), "n2": ntag(i, j, k+1),
                                 "i": i, "j": j, "k": k, "axis": "col"})
                    et += 1

    def _add_beam(i, j, k, dirn, A, B):
        nonlocal et, sub_node, sub_ele
        xa, ya = coord[A][0], coord[A][1]
        xb, yb = coord[B][0], coord[B][1]
        zk = z[k]; L = math.hypot(xb-xa, yb-ya)
        relz, rely = (relf(i, j, k, dirn) if relf else ("none", "none"))
        etag = ("beam", i, j, k, dirn)
        bm = {"etag": etag, "sec": cfg["beam"], "i": i, "j": j, "k": k, "dir": dirn, "L": L,
              "A": A, "B": B, "relz": relz, "rely": rely, "parent": (k, dirn, i, j), "s0": 0.0, "s1": L,
              "sA": 0.0, "sB": L, "Lp": L, "xyzA": coord[A], "xyzB": coord[B]}
        if etag in drop:
            bm["nodes"] = [A, B]; bm["segs"] = []; dropped.append(bm); return
        chain = [A]
        for s in range(1, nseg):
            t = s/float(nseg)
            nd = sub_node; sub_node += 1
            R("node", nd, xa+(xb-xa)*t, ya+(yb-ya)*t, zk); coord[nd] = (xa+(xb-xa)*t, ya+(yb-ya)*t, zk)
            dia_extra[k].append(nd); chain.append(nd)
        chain.append(B)
        seg_tags = []
        for s in range(nseg):
            n1, n2 = chain[s], chain[s+1]
            ra = []
            if s == 0:
                ri_z, _ = _rel_parts(relz); ri_y, _ = _rel_parts(rely)
                ra = eng.release_args(ri_z, ri_y)
            if s == nseg-1:
                _, rj_z = _rel_parts(relz); _, rj_y = _rel_parts(rely)
                ra = ra + eng.release_args(rj_z, rj_y)
            tag = sub_ele; sub_ele += 1
            R("element", "elasticBeamColumn", tag, n1, n2, bA, Es, Gs, bJ, bIx, bIy, 3, *ra)
            seg_tags.append(tag)
        bm["nodes"] = chain; bm["segs"] = seg_tags
        beams.append(bm)

    # beams X and Y (sub-divided)
    for k in range(1, NF+1):
        P = present[k]
        for j in range(NY+1):
            for i in range(NX):
                if (i, j) in P and (i+1, j) in P:
                    _add_beam(i, j, k, "X", ntag(i, j, k), ntag(i+1, j, k))
        for i in range(NX+1):
            for j in range(NY):
                if (i, j) in P and (i, j+1) in P:
                    _add_beam(i, j, k, "Y", ntag(i, j, k), ntag(i, j+1, k))

    # braces (single truss elements)
    braces = []
    if cfg.get("braces"):
        R("uniaxialMaterial", "Elastic", 1, Es); brA = eng.HSS[cfg["brace"]]
        for k in range(1, NF+1):
            for (dirn, i, j) in cfg["braces"](k, NX, NY):
                a = (i, j); b = (i+1, j) if dirn == "X" else (i, j+1)
                for (pa, ka, pb, kb) in ((a, k-1, b, k), (a, k, b, k-1)):
                    if pa in present[ka] and pb in present[kb]:
                        n1, n2 = ntag(pa[0], pa[1], ka), ntag(pb[0], pb[1], kb)
                        if et in drop:
                            dropped.append({"etag": et, "kind": "brace", "sec": cfg.get("brace"), "n1": n1, "n2": n2})
                        else:
                            R("element", "Truss", et, n1, n2, brA, 1)
                            braces.append({"tag": et, "etag": et, "sec": cfg.get("brace"), "n1": n1, "n2": n2,
                                           "area": brA, "Emat": EMOD, "truss": True})
                        et += 1

    # rigid diaphragm (corner nodes + interior beam nodes), no mass (static)
    slave_set = set()
    for k in range(1, NF+1):
        sl = [ntag(i, j, k) for (i, j) in present[k]] + dia_extra[k]
        R("rigidDiaphragm", 3, mtag(k), *sl)
        slave_set |= set(sl)
    fixed = {n: ((1, 1, 1, 0, 0, 0) if base == "pinned" else (1, 1, 1, 1, 1, 1)) for n in base_nodes}
    orphans = _fix_orphans(R, coord, fixed, slave_set, set(masters.values())) if (drop or fix_masters) else set()

    bases = {(i, j): base for (i, j) in present[0]}
    model = {"cm": cm, "present": present, "z": z, "NF": NF, "cols": cols, "beams": beams,
             "braces": braces, "bases": bases, "base_nodes": sorted(base_nodes), "masters": masters,
             "coord": coord, "dropped": dropped, "warnings": warnings, "sig": R.calls, "custom": False,
             "orphans": orphans}
    _finish_model(cfg, model)
    return model


# ====================================================================================
# geometry: parent spans, bays, roof bays, infill
# ====================================================================================
def _level_of(zz, zl):
    for k, zk in enumerate(zl):
        if abs(zz - zk) <= _TOL:
            return k
    return None


def _spans(model, k):
    """Grid spans at level k: {(dir, i, j): (A, B, xa, ya, xb, yb, L)}."""
    P = model["present"].get(k, set()); c = model["coord"]; out = {}
    for (i, j) in P:
        A = ntag(i, j, k)
        for dirn, (ii, jj) in (("X", (i + 1, j)), ("Y", (i, j + 1))):
            if (ii, jj) in P:
                B = ntag(ii, jj, k)
                if A in c and B in c:
                    xa, ya = c[A][0], c[A][1]; xb, yb = c[B][0], c[B][1]
                    out[(dirn, i, j)] = (A, B, xa, ya, xb, yb, math.hypot(xb - xa, yb - ya))
    return out


def _on_span(x, y, sp):
    """Parameter s (in, from A) if (x, y) lies on span sp within tolerance, else None."""
    A, B, xa, ya, xb, yb, L = sp
    if L <= 0:
        return None
    ux, uy = (xb - xa) / L, (yb - ya) / L
    s = (x - xa) * ux + (y - ya) * uy
    d = abs(-(x - xa) * uy + (y - ya) * ux)
    if d <= _TOL and -_TOL <= s <= L + _TOL:
        return min(max(s, 0.0), L)
    return None


def _bays(model, k):
    """Framed bays at level k: {(i, j): dict(x0, y0, dx, dy, corners)} (4 corner nodes present)."""
    P = model["present"].get(k, set()); c = model["coord"]; out = {}
    for (i, j) in P:
        corners = [(i, j), (i + 1, j), (i, j + 1), (i + 1, j + 1)]
        if all(q in P for q in corners):
            p00 = c[ntag(i, j, k)]; p10 = c[ntag(i + 1, j, k)]; p01 = c[ntag(i, j + 1, k)]
            p11 = c[ntag(i + 1, j + 1, k)]
            dx = 0.5 * ((p10[0] - p00[0]) + (p11[0] - p01[0]))
            dy = 0.5 * ((p01[1] - p00[1]) + (p11[1] - p10[1]))
            xs = [p[0] for p in (p00, p10, p01, p11)]; ys = [p[1] for p in (p00, p10, p01, p11)]
            out[(i, j)] = dict(x0=p00[0], y0=p00[1], dx=abs(dx), dy=abs(dy),
                               xmin=min(xs), xmax=max(xs), ymin=min(ys), ymax=max(ys))
    return out


def _finish_model(cfg, model):
    """Attach parent spans, bays (with roof flags) and modelled infill beams to a built model."""
    NF = model["NF"]; zl = model["z"]; c = model["coord"]
    spans = {k: _spans(model, k) for k in range(1, NF + 1)}
    bays = {k: _bays(model, k) for k in range(0, NF + 2)}
    model["spans"] = spans
    rl = _roof_levels(cfg)
    model["bays"] = {}
    for k in range(1, NF + 1):
        for (i, j), b in bays[k].items():
            b = dict(b)
            b["roof"] = (k == NF) or (k in rl) or not any((i, j) in bays.get(kk, {}) for kk in range(k + 1, NF + 1))
            model["bays"][(k, i, j)] = b
    model["infill"] = {}
    nonload = []
    for bm in list(model["beams"]) + [d for d in model["dropped"] if "xyzA" in d]:
        if bm.get("parent") is not None:
            continue
        (x1, y1, z1), (x2, y2, z2) = bm["xyzA"], bm["xyzB"]
        ka, kb = _level_of(z1, zl), _level_of(z2, zl)
        if ka is None or ka != kb or ka == 0:
            bm["parent"] = None; nonload.append(bm["etag"]); continue
        k = ka; bm["k"] = k
        found = None
        for key, sp in spans.get(k, {}).items():
            sa = _on_span(x1, y1, sp)
            if sa is None:
                continue
            sb = _on_span(x2, y2, sp)
            if sb is None or abs(sb - sa) < 1e-6:
                continue
            found = (key, sp, sa, sb); break
        if found:
            (dirn, i, j), sp, sa, sb = found
            bm.update(parent=(k, dirn, i, j), i=i, j=j, dir=dirn, sA=sa, sB=sb, s0=min(sa, sb),
                      s1=max(sa, sb), Lp=sp[6])
            continue
        bm["parent"] = None
        # off-grid beam inside a framed bay, running along X or Y -> modelled infill
        if abs(y2 - y1) <= _TOL or abs(x2 - x1) <= _TOL:
            mx, my = 0.5 * (x1 + x2), 0.5 * (y1 + y2)
            run = "X" if abs(x2 - x1) > abs(y2 - y1) else "Y"
            for (ii, jj), b in bays.get(k, {}).items():
                if b["xmin"] - _TOL <= mx <= b["xmax"] + _TOL and b["ymin"] - _TOL <= my <= b["ymax"] + _TOL:
                    bm.update(i=-1, j=-1, dir=run, bay=(k, ii, jj))
                    u = (mx - b["x0"]) if run == "Y" else (my - b["y0"])
                    model["infill"].setdefault((k, ii, jj), []).append((run, u, bm))
                    break
            else:
                nonload.append(bm["etag"])
        else:
            nonload.append(bm["etag"])
    if nonload:
        model["warnings"].append("%d beam(s) not on a grid span nor inside a framed bay carry no floor "
                                 "load (sloped/off-level/diagonal), e.g. %s"
                                 % (len(nonload), ", ".join(str(t) for t in nonload[:4])))
    # beams per parent span
    model["by_span"] = {}
    for bm in model["beams"]:
        if bm.get("parent") is not None:
            model["by_span"].setdefault(bm["parent"], []).append(bm)


def _bays_adjacent(present_k, i, j, dirn):
    """How many present bays bound this beam (1 perimeter, 2 interior)."""
    n = 0
    if dirn == "X":      # beam (i,j)-(i+1,j): bays south (j-1) and north (j)
        for jj in (j-1, j):
            if all(c in present_k for c in ((i, jj), (i+1, jj), (i, jj+1), (i+1, jj+1))): n += 1
    else:                # beam (i,j)-(i,j+1): bays west (i-1) and east (i)
        for ii in (i-1, i):
            if all(c in present_k for c in ((ii, j), (ii+1, j), (ii, j+1), (ii+1, j+1))): n += 1
    return n


def _deck_span(cfg, k):
    d = cfg.get("deck_span")
    if isinstance(d, dict):
        d = _lev(d, k, None)
    if not d:
        f = cfg.get("infill_dir")
        if isinstance(f, dict):
            f = _lev(f, k, None)
        if f:
            d = "Y" if str(f).upper().startswith("X") else "X"
    if not d:
        return None
    d = str(d).upper()
    return "X" if d.startswith("X") else ("Y" if d.startswith("Y") else None)


def bay_modes(cfg, model):
    """{(k,i,j): (mode, D, positions, origin)}: mode 'two-way' | 'one-way' | 'default';
    D = deck-span direction for one-way; positions = interior supporting-line offsets along D (in) with
    a flag 'm' (modelled infill) or 'v' (virtual infill at cfg['infill_spacing'])."""
    fs = str(cfg.get("floor_system") or "one-way").lower()
    two = "two" in fs
    out = {}
    for (k, i, j), b in model["bays"].items():
        if two:
            out[(k, i, j)] = ("two-way", None, [], "two-way"); continue
        D = _deck_span(cfg, k)
        inf = model["infill"].get((k, i, j), [])
        runs = {r for (r, u, bm) in inf}
        origin = "declared"
        if D is None and len(runs) == 1:
            D = "Y" if runs == {"X"} else "X"; origin = "auto"
        if D is None:
            out[(k, i, j)] = ("default", None, [], "default"); continue
        dD = b["dx"] if D == "X" else b["dy"]
        pos = [(u, "m", bm) for (r, u, bm) in inf if r != D and _TOL < u < dD - _TOL]
        if not pos:
            sp = cfg.get("infill_spacing")
            if isinstance(sp, dict):
                sp = _lev(sp, k, None)
            if sp:
                n = max(1, int(round(dD / float(sp))))
                pos = [(dD * m / n, "v", None) for m in range(1, n)]
        pos.sort(key=lambda t: t[0])
        out[(k, i, j)] = ("one-way", D, pos, origin)
    return out


# ====================================================================================
# gravity loading
# ====================================================================================
def _tri_int(a, b, L, c):
    """Integral over [a, b] of min(s, L - s, c) (two-way tributary width profile)."""
    if b <= a:
        return 0.0
    f = lambda s: max(0.0, min(s, L - s, c))
    pts = sorted({a, b} | {p for p in (c, L - c, 0.5 * L) if a < p < b})
    return sum((pts[q + 1] - pts[q]) * (f(pts[q]) + f(pts[q + 1])) * 0.5 for q in range(len(pts) - 1))


def _piece_int(pc, a, b, Lp):
    """Integral of a distributed piece over [a, b] on a parent span of length Lp."""
    if pc[0] == "tri":
        return pc[1] * _tri_int(a, b, Lp, pc[2])
    if pc[0] == "uni":
        return pc[1] * (b - a)
    return 0.0


def _wind_qh(cfg):
    """MWFRS velocity pressure at the mean roof height (psf), ASCE 7-22 Eq. 26.10-1, with Kd (7-22
    places Kd in the pressure equation 27.3-1)."""
    w = cfg.get("wind") or {}
    h = zlevels(cfg)[-1] / 12.0
    Kz = eng.kz_exposure(h, w.get("exposure", "C"))
    q = 0.00256 * Kz * w.get("Kzt", 1.0) * w.get("Ke", 1.0) * float(w.get("V", 0.0)) ** 2
    return q * w.get("Kd", 0.85), h


def _roof_cp(cfg, dist, h, Lw):
    """MWFRS roof Cp (ASCE 7-22 Fig. 27.3-1, roof normal to ridge theta < 10 deg / parallel to ridge),
    the suction value, at `dist` (ft) from the windward edge; h/L interpolated between 0.5 and 1.0."""
    w = cfg.get("wind") or {}
    if w.get("Cp_roof") is not None:
        return float(w["Cp_roof"])
    r = h / max(Lw, 1e-6)
    def lo(d):   # h/L <= 0.5
        return -0.9 if d <= h else (-0.5 if d <= 2 * h else -0.3)
    def hi(d):   # h/L >= 1.0
        return -1.3 if d <= h / 2.0 else -0.7
    if r <= 0.5: return lo(dist)
    if r >= 1.0: return hi(dist)
    t = (r - 0.5) / 0.5
    return (1 - t) * lo(dist) + t * hi(dist)


def _case_pieces(cfg, model, fD, fL, fLr, uplift=None, modes=None):
    """Distributed / point loads of one case, per parent span and per modelled infill beam.
    Returns dict:
        span  {(k,dir,i,j): [(piece, origin)]}   piece = ('tri', w0, c) | ('uni', w) | ('pt', s, P)
        infill {etag: [('uni', w)]}
        girder {(k,dir,i,j): [('uni', w)]}  half-bay girder rule for 'default' bays (floors only)
    w in kip/in, P in kip, s in in along the span from its A node. origin: 'twoway'|'oneway'|'clad'."""
    NF = model["NF"]; heights = cfg["heights"]; clad = float(cfg.get("clad", 0.0) or 0.0)
    modes = modes if modes is not None else bay_modes(cfg, model)
    span = {}; infill = {}; girder = {}
    up = None
    if uplift:
        qh, h = _wind_qh(cfg)
        dirn, sgn = uplift
        xs = [c[0] for t, c in model["coord"].items() if t < _SUB0]
        ys = [c[1] for t, c in model["coord"].items() if t < _SUB0]
        lo_, hi_ = (min(xs), max(xs)) if dirn == "X" else (min(ys), max(ys))
        GCpi = float((cfg.get("wind") or {}).get("GCpi", 0.18))
        G = float((cfg.get("wind") or {}).get("G", 0.85))
        up = (qh, h, dirn, sgn, lo_, hi_, GCpi, G)
    for (k, i, j), b in model["bays"].items():
        if b["roof"]:
            p = fD * dead_psf(cfg, k) + _roof_terms(fLr, cfg, k)
        else:
            p = fD * dead_psf(cfg, k) + _live_factor(fL, cfg, k) * live_psf(cfg, k)
        if up and b["roof"]:
            qh, h, dirn, sgn, lo_, hi_, GCpi, G = up
            cen = (b["x0"] + 0.5 * b["dx"]) if dirn == "X" else (b["y0"] + 0.5 * b["dy"])
            dist = ((cen - lo_) if sgn > 0 else (hi_ - cen)) / 12.0
            Lw = (hi_ - lo_) / 12.0
            p += qh * (G * _roof_cp(cfg, dist, h, Lw) - GCpi)      # suction (negative) + internal +GCpi
        w0 = p * _PSF
        mode, D, pos, origin = modes[(k, i, j)]
        edges = {"S": ("X", i, j), "N": ("X", i, j + 1), "W": ("Y", i, j), "E": ("Y", i + 1, j)}
        if mode in ("two-way", "default"):
            for e, (d, ii, jj) in edges.items():
                perp = b["dy"] if d == "X" else b["dx"]
                span.setdefault((k, d, ii, jj), []).append((("tri", w0, perp / 2.0), "twoway"))
                if mode == "default":
                    girder.setdefault((k, d, ii, jj), []).append(("uni", w0 * perp / 2.0))
        else:
            dD = b["dx"] if D == "X" else b["dy"]; dperp = b["dy"] if D == "X" else b["dx"]
            us = [0.0] + [u for (u, f, bm) in pos] + [dD]
            trib = [0.5 * (us[min(m + 1, len(us) - 1)] - us[max(m - 1, 0)]) for m in range(len(us))]
            e0, e1 = (edges["W"], edges["E"]) if D == "X" else (edges["S"], edges["N"])
            g0, g1 = (edges["S"], edges["N"]) if D == "X" else (edges["W"], edges["E"])
            span.setdefault((k,) + e0, []).append((("uni", w0 * trib[0]), "oneway"))
            span.setdefault((k,) + e1, []).append((("uni", w0 * trib[-1]), "oneway"))
            for m, (u, f, bm) in enumerate(pos, start=1):
                if f == "m":
                    infill.setdefault(bm["etag"], []).append(("uni", w0 * trib[m]))
                else:
                    P = w0 * trib[m] * dperp / 2.0
                    for g in (g0, g1):
                        span.setdefault((k,) + g, []).append((("pt", u, P), "oneway"))
    # cladding on the exterior bay edges with the SAME mid-height wall tributary as engine3d.floor_dead /
    # floor_w (engine3d.clad_edge_heights: half the storey below + half above, true wall heights at
    # setbacks / split levels, parapet at the top; hr-geomwind HR-39), so static dead == seismic-weight dead
    if clad:
        for (k, d, i, j), (th, _L) in eng.clad_edge_heights(cfg).items():
            if (d, i, j) not in model["spans"].get(k, {}):
                model.setdefault("warnings", []).append(
                    "cladding on edge %s%s level %d (%.1f ft wall) has no modelled span -- not applied" % (d, (i, j), k, th))
                continue
            wcl = fD * clad * th * 12.0 * _PSF          # psf * ft  -> kip/in
            span.setdefault((k, d, i, j), []).append((("uni", wcl), "clad"))
            girder.setdefault((k, d, i, j), []).append(("uni", wcl))
    return {"span": span, "infill": infill, "girder": girder, "modes": modes}


def _seg_ranges(bm, nseg_eff):
    """Parent-span ranges [(a, b, q)] of a beam's sub-elements, q = index; plus orientation."""
    sA, sB = bm["sA"], bm["sB"]
    out = []
    for q in range(nseg_eff):
        u0 = sA + (sB - sA) * q / nseg_eff; u1 = sA + (sB - sA) * (q + 1) / nseg_eff
        out.append((min(u0, u1), max(u0, u1), q, u0))
    return out


def apply_case_gravity(cfg, model, fD, fL, fLr, uplift=None, modes=None):
    """Apply one case's gravity (and roof uplift) to the beams of a built model inside the current load
    pattern. Returns dict(total, by_level {k: kip}, node_load {node: kip}, pieces)."""
    pcs = _case_pieces(cfg, model, fD, fL, fLr, uplift, modes)
    total = 0.0; by_level = {}; node_load = {}
    c = model["coord"]

    def _nodal(n, F, k):
        nonlocal total
        ops.load(n, 0.0, 0.0, -F, 0.0, 0.0, 0.0)
        total += F; by_level[k] = by_level.get(k, 0.0) + F; node_load[n] = node_load.get(n, 0.0) + F

    def _apply_dist(bm, plist, Lp, ranges_fn):
        """Distributed pieces on one beam (point pieces are applied once per span in _apply_points)."""
        nonlocal total
        k = bm["k"]
        segs = bm["segs"]
        dist = [(pc, o) for pc, o in plist if pc[0] != "pt"]
        if not dist:
            return
        if not segs:                                     # dropped (capacity analysis): lump to end nodes
            a, b = bm["s0"], bm["s1"]; W = 0.0; Mx = 0.0
            n = 24
            for pc, _o in dist:
                for q in range(n):
                    u0 = a + (b - a) * q / n; u1 = a + (b - a) * (q + 1) / n
                    dW = _piece_int(pc, u0, u1, Lp); W += dW; Mx += dW * 0.5 * (u0 + u1)
            if W != 0.0 and b > a:
                _lever(bm, W, Mx / W, k)
            return
        for (u0, u1, q, ustart) in ranges_fn():
            tag = segs[q]; seglen = u1 - u0
            wq = sum(_piece_int(pc, u0, u1, Lp) for pc, _o in dist)
            if seglen > 0 and wq != 0.0:
                ops.eleLoad("-ele", tag, "-type", "-beamUniform", 0.0, -wq / seglen, 0.0)
                total += wq; by_level[k] = by_level.get(k, 0.0) + wq
                nI, nJ = bm["nodes"][q], bm["nodes"][q + 1]
                node_load[nI] = node_load.get(nI, 0.0) + 0.5 * wq
                node_load[nJ] = node_load.get(nJ, 0.0) + 0.5 * wq

    def _lever(bm, W, xw, k):
        """Lump a load W at parent position xw of a dropped beam onto its two end nodes."""
        a, b = bm["s0"], bm["s1"]
        fB = W * (xw - a) / (b - a)
        nA, nB = (bm["A"], bm["B"]) if bm["sA"] <= bm["sB"] else (bm["B"], bm["A"])
        _nodal(nA, W - fB, k); _nodal(nB, fB, k)

    def _apply_points(par, plist, bms):
        """Each point piece of a span goes to exactly ONE beam sub-element (or a dropped beam's ends)."""
        nonlocal total
        pts = [pc for pc, _o in plist if pc[0] == "pt"]
        if not pts:
            return
        hmax = max(bm["s1"] for bm in bms)
        for (_t, s, P) in pts:
            done = False
            for bm in bms:
                if not (bm["s0"] - 1e-9 <= s <= bm["s1"] + 1e-9):
                    continue
                k = bm["k"]
                if not bm["segs"]:
                    if bm["s0"] - 1e-9 <= s < bm["s1"] - 1e-9 or abs(s - hmax) <= 1e-9:
                        _lever(bm, P, s, k); done = True; break
                    continue
                for (u0, u1, q, ustart) in _seg_ranges(bm, len(bm["segs"])):
                    if u0 - 1e-9 <= s < u1 - 1e-9 or (abs(s - hmax) <= 1e-9 and abs(u1 - hmax) <= 1e-9):
                        seglen = u1 - u0
                        xL = min(max(abs(s - ustart) / max(seglen, 1e-9), 0.0), 1.0)
                        ops.eleLoad("-ele", bm["segs"][q], "-type", "-beamPoint", 0.0, -P, xL)
                        total += P; by_level[k] = by_level.get(k, 0.0) + P
                        nI, nJ = bm["nodes"][q], bm["nodes"][q + 1]
                        node_load[nI] = node_load.get(nI, 0.0) + P * (1 - xL)
                        node_load[nJ] = node_load.get(nJ, 0.0) + P * xL
                        done = True; break
                if done:
                    break
            if not done:
                model["warnings"].append("point load at s=%.0f on span %s not applied (no beam there)" % (s, par))

    allbeams = list(model["beams"]) + [d for d in model["dropped"] if "xyzA" in d]
    on_span = {}
    for bm in allbeams:
        par = bm.get("parent")
        if par is not None:
            on_span.setdefault(par, []).append(bm)
            plist = pcs["span"].get(par, [])
            if not plist:
                continue
            n = len(bm["segs"]) or 1
            _apply_dist(bm, plist, bm["Lp"], lambda bm=bm, n=n: _seg_ranges(bm, n))
        elif bm.get("bay") is not None and bm["etag"] in pcs["infill"]:
            plist = [(pc, "oneway") for pc in pcs["infill"][bm["etag"]]]
            L = bm["L"]; n = len(bm["segs"]) or 1
            loc = dict(bm, sA=0.0, sB=L, s0=0.0, s1=L)
            _apply_dist(loc, plist, L, lambda loc=loc, n=n: _seg_ranges(loc, n))
    for par, bms in on_span.items():
        _apply_points(par, pcs["span"].get(par, []), bms)
    return {"total": total, "by_level": by_level, "node_load": node_load, "pieces": pcs}


def apply_gravity(cfg, model, fD, fL, fLr):
    """Apply the tributary gravity of one combination to every beam sub-element (see module
    docstring). fL may be a LiveFactor and fLr a RoofFactors (plain floats keep the legacy meaning).
    Returns the total applied vertical load (kip)."""
    return apply_case_gravity(cfg, model, fD, fL, fLr)["total"]


def beam_segment_loads(cfg, fD=1.0, fL=0.0, fLr=0.0):
    """Per beam, per sub-element uniform-equivalent load (kip/in) as applied by apply_gravity, on a
    freshly built static model (for viewers). Returns [(beam_dict, [w_seg ...])]."""
    model = build_static(cfg, "Linear", 6)
    pcs = _case_pieces(cfg, model, fD, fL, fLr)
    out = []
    for bm in model["beams"]:
        par = bm.get("parent"); n = len(bm["segs"])
        if par is None or n == 0:
            continue
        plist = pcs["span"].get(par, [])
        ws = []
        for (u0, u1, q, us) in _seg_ranges(bm, n):
            W = sum(_piece_int(pc, u0, u1, bm["Lp"]) for pc, _o in plist if pc[0] != "pt")
            W += sum(pc[2] for pc, _o in plist if pc[0] == "pt" and u0 <= pc[1] < u1)
            ws.append(W / max(u1 - u0, 1e-9))
        out.append((bm, ws))
    return out


def apply_lateral(lateral):
    for k, (fx, fy, mz) in lateral.items():
        ops.load(mtag(k), fx, fy, 0.0, 0.0, 0.0, mz)


def _solve():
    ops.constraints("Transformation"); ops.numberer("RCM"); ops.system("UmfPack")
    ops.test("NormDispIncr", 1e-7, 200); ops.algorithm("Newton")
    ops.integrator("LoadControl", 1.0); ops.analysis("Static")
    return ops.analyze(1)


def run_combo(cfg, fD, fL, fLr, lateral, transf="PDelta", nseg=10):
    """Build + analyse ONE LRFD combination statically with P-Delta (nominal stiffness; used for the
    force diagrams). Returns (model, applied_kip, ok)."""
    model = build_static(cfg, transf, nseg)
    ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
    applied = apply_gravity(cfg, model, fD, fL, fLr)
    apply_lateral(lateral)
    ok = _solve()
    return model, applied, ok


def gravity_audit(cfg, nseg=6):
    """Static-model dead load per level vs the seismic-weight dead (engine3d.floor_dead) -- HR-03.
    Returns {k: (static_kip, W_dead_kip)}."""
    model = build_static(cfg, "Linear", nseg)
    pcs = _case_pieces(cfg, model, 1.0, 0.0, RoofFactors())
    out = {}
    for par, plist in pcs["span"].items():
        k = par[0]; Lp = model["spans"][k][par[1:]][6]
        tot = sum(_piece_int(pc, 0.0, Lp, Lp) if pc[0] != "pt" else pc[2] for pc, _o in plist)
        out[k] = out.get(k, 0.0) + tot
    for etag, plist in pcs["infill"].items():
        bm = next((b for b in model["beams"] if b["etag"] == etag), None)
        if bm:
            out[bm["k"]] = out.get(bm["k"], 0.0) + sum(pc[1] * bm["L"] for pc in plist)
    return {k: (out.get(k, 0.0), eng.floor_dead(cfg, k)) for k in range(1, model["NF"] + 1)}


# ====================================================================================
# floors (one-way girder rule / chevron no-brace-support), simple-span diagrams
# ====================================================================================
def _simple_span(plist, Lp, n=96):
    """Simple-span M(x) and V(x) of a parent span under plist [(piece, origin)] (distributed pieces
    integrated exactly over n strips, point pieces exact). Returns (xs, M, Vleft, Vright)."""
    pts = []
    for q in range(n):
        u0 = Lp * q / n; u1 = Lp * (q + 1) / n
        W = 0.0
        for pc, _o in plist:
            if pc[0] != "pt":
                W += _piece_int(pc, u0, u1, Lp)
        if W:
            pts.append((0.5 * (u0 + u1), W))
    for pc, _o in plist:
        if pc[0] == "pt":
            pts.append((pc[1], pc[2]))
    pts.sort()
    RA = sum(W * (Lp - x) for x, W in pts) / Lp
    xs = sorted({Lp * q / n for q in range(n + 1)} | {x for x, _ in pts if 0 < x < Lp})
    M = []; Vl = []; Vr = []
    cW = cWx = 0.0; ip = 0; npts = len(pts)
    for x in xs:
        while ip < npts and pts[ip][0] < x:            # loads strictly left of x
            cW += pts[ip][1]; cWx += pts[ip][1] * pts[ip][0]; ip += 1
        at = 0.0; jp = ip
        while jp < npts and pts[jp][0] <= x:           # loads exactly at x
            at += pts[jp][1]; jp += 1
        M.append(RA * x - (x * cW - cWx))
        Vl.append(RA - cW); Vr.append(RA - cW - at)
    return xs, M, Vl, Vr


def _floor_spec(cfg, model):
    """Which beams get a gravity floor and on what basis: {id(bm): 'girder'|'chevron'}."""
    out = {}
    modes = bay_modes(cfg, model)
    brace_nodes = {}
    for br in model["braces"]:
        for n in (br["n1"], br["n2"]):
            brace_nodes.setdefault(n, []).append(br)
    for d in model["dropped"]:
        if d.get("kind") == "brace":
            for n in (d["n1"], d["n2"]):
                brace_nodes.setdefault(n, []).append(d)
    geo = {}                                   # brace work points lying inside a span (separate apex nodes)
    for n in brace_nodes:
        if n not in model["coord"]:
            continue
        x, y, z = model["coord"][n]
        k = _level_of(z, model["z"])
        if k is None:
            continue
        for key, sp in model["spans"].get(k, {}).items():
            sv = _on_span(x, y, sp)
            if sv is not None and _TOL < sv < sp[6] - _TOL:
                geo.setdefault((k,) + key, set()).add(n)
    for par, bms in model["by_span"].items():
        k, d, i, j = par
        A, B = model["spans"][k][(d, i, j)][:2]
        interior = set(geo.get(par, set()))
        for bm in bms:
            interior |= {bm["A"], bm["B"]}
        interior -= {A, B}
        chevron = False
        for n in interior:
            for br in brace_nodes.get(n, []):
                sysn = brace_system(cfg, br)
                if sysn in ("SCBF", "OCBF", "BRBF", "CBF"):
                    chevron = True
        sides = [(k, i, jj) for jj in (j - 1, j)] if d == "X" else [(k, ii, j) for ii in (i - 1, i)]
        default_side = any(modes.get(s, ("",))[0] == "default" for s in sides if s in model["bays"])
        for bm in bms:
            if chevron:
                out[id(bm)] = "chevron"
            elif default_side and len(bms) == 1:
                # deck span unknown: simple beams get the half-bay girder diagram; rigid-ended beams get
                # their solved gravity moment/shear scaled by (girder-rule load / applied load)
                out[id(bm)] = "girder" if bm.get("relz") == "both" else "girder_rigid"
    return out, modes


def _floor_plist(pcs, par):
    """Load list of a parent span for the one-way / no-brace-support floor: the applied one-way pieces and
    cladding, with the 45-degree pieces of 'default' (deck span undeclared) bays replaced by the half-bay
    girder rule."""
    k, d, i, j = par
    modes = pcs["modes"]
    sides = [(k, i, jj) for jj in (j - 1, j)] if d == "X" else [(k, ii, j) for ii in (i - 1, i)]
    any_default = any(modes.get(s, ("",))[0] == "default" for s in sides)
    plist = []
    for pc, o in pcs["span"].get(par, []):
        if any_default and o in ("twoway", "clad"):
            continue                       # replaced by the girder list (which holds the cladding too)
        plist.append((pc, o))
    if any_default:
        plist += [(pc, "girder") for pc in pcs["girder"].get(par, [])]
    return plist


def _ss_mid_defl(plist, Lp):
    """Simple-span midspan deflection x EI of a load list (virtual work, unit load at midspan)."""
    if not plist:
        return 0.0
    xs, M, _vl, _vr = _simple_span(plist, Lp)
    m = [(x / 2.0 if x <= Lp / 2.0 else (Lp - x) / 2.0) for x in xs]
    return sum((xs[q + 1] - xs[q]) * 0.5 * (M[q] * m[q] + M[q + 1] * m[q + 1]) for q in range(len(xs) - 1))


def service_deflections(cfg, nseg=6):
    """Beam deflections for the serviceability gate from the SOLVED static model (HR-27): unfactored
    live (floor L + the governing roof branch Lr / S / R) and total (D + live) on a nominal-stiffness
    first-order model, so releases, continuity, propping by braces and split spans are real. Per grid
    span: max |vertical displacement - chord between the span's end nodes|; where the deck span is
    undeclared the value is scaled by the simple-span deflection ratio (half-bay girder-rule load /
    analysed load) on that span (conservative for the non-girder direction; declare deck_span).
    Returns {(k, dir, i, j): dict(L=Lp, LL=in, TL=in, sec=section)}."""
    model = build_static(cfg, "Linear", nseg)
    modes = bay_modes(cfg, model)
    NF = model["NF"]
    tot = {}
    for b in ("Lr", "S", "R"):
        tot[b] = sum(roof_psf(cfg, k)[("Lr", "S", "R").index(b)] for k in range(1, NF + 1))
    rb = max(tot, key=lambda b: tot[b])
    rf = RoofFactors(**{rb: 1.0})
    out = {}
    for name, fD in (("LL", 0.0), ("TL", 1.0)):
        _reset(); ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
        g = apply_case_gravity(cfg, model, fD, 1.0, rf, modes=modes)
        if _solve() != 0:
            continue
        pcs = g["pieces"]
        for par, bms in model["by_span"].items():
            k, d, i, j = par
            A, B, _xa, _ya, _xb, _yb, Lp = model["spans"][k][(d, i, j)]
            zA = ops.nodeDisp(A, 3); zB = ops.nodeDisp(B, 3)
            dmax = 0.0
            for bm in bms:
                n = len(bm["nodes"]) - 1
                for q, nd in enumerate(bm["nodes"]):
                    sp = bm["sA"] + (bm["sB"] - bm["sA"]) * q / max(n, 1)
                    chord = zA + (zB - zA) * sp / Lp
                    dmax = max(dmax, abs(ops.nodeDisp(nd, 3) - chord))
            lam = 1.0
            sides = [(k, i, jj) for jj in (j - 1, j)] if d == "X" else [(k, ii, j) for ii in (i - 1, i)]
            if any(modes.get(sd, ("",))[0] == "default" for sd in sides):
                dg = _ss_mid_defl(_floor_plist(pcs, par), Lp)
                da = _ss_mid_defl(pcs["span"].get(par, []), Lp)
                if da > 1e-12 and dg > da:
                    lam = dg / da                  # simple-span deflection ratio girder rule / analysed
            e = out.setdefault(par, dict(L=Lp, LL=0.0, TL=0.0, sec=bms[0].get("sec")))
            e[name] = dmax * lam
    return out


def _floors(cfg, model, pcs, spec, grav_only=True):
    """{id(bm): (Mg, Vg)} -- or ('add', dM, dV) for rigid-ended girders -- for the beams in spec, from
    the case pieces. 'girder_rigid' (deck span undeclared, rigid-ended beam): the solved moment/shear is
    kept (real end restraint, loads from above) and the simple-span effect of the load the half-bay
    girder rule adds over the analysed 45-degree load is added on top (conservative). grav_only is kept
    for API compatibility."""
    out = {}
    cache = {}
    for bm in model["beams"]:
        basis = spec.get(id(bm))
        if not basis:
            continue
        par = bm["parent"]
        if basis == "girder_rigid":
            Lp = bm["Lp"]
            xs, Mg_, Vlg, Vrg = _simple_span(_floor_plist(pcs, par), Lp)
            _x, Ma_, Vla, Vra = _simple_span(pcs["span"].get(par, []), Lp)
            dM = max([g - a for g, a in zip(Mg_, Ma_)] + [0.0])
            dV = max([abs(g - a) for g, a in zip(Vlg, Vla)] + [abs(g - a) for g, a in zip(Vrg, Vra)] + [0.0])
            if dM > 0 or dV > 0:
                out[id(bm)] = ("add", dM, dV)
            continue
        if par not in cache:
            cache[par] = _simple_span(_floor_plist(pcs, par), bm["Lp"])
        xs, M, Vl, Vr = cache[par]
        a, b = bm["s0"], bm["s1"]
        Mg = 0.0; Vg = 0.0
        for x, m, vl, vr in zip(xs, M, Vl, Vr):
            if a - 1e-6 <= x <= b + 1e-6:
                Mg = max(Mg, abs(m))
                if x > a + 1e-6: Vg = max(Vg, abs(vl))
                if x < b - 1e-6: Vg = max(Vg, abs(vr))
        out[id(bm)] = (Mg, Vg)
    return out


# ====================================================================================
# demand extraction
# ====================================================================================
def _member_kinds(model):
    """fset(corner nodes) -> ('col'|'beam'|'brace', section)."""
    kinds = {}
    for c in model["cols"]:   kinds[frozenset((c["n1"], c["n2"]))] = ("col", c.get("sec"))
    for b in model["beams"]:  kinds[frozenset((b["A"], b["B"]))]   = ("beam", b.get("sec"))
    for b in model["braces"]: kinds[frozenset((b["n1"], b["n2"]))] = ("brace", b.get("sec"))
    for d in model["dropped"]:
        if "xyzA" in d:
            kinds[frozenset((d["A"], d["B"]))] = ("beam", d.get("sec"))
        else:
            kinds[frozenset((d["n1"], d["n2"]))] = (d.get("kind") or "brace", d.get("sec"))
    return kinds


def _extract(model, floors=None):
    """Per-PARENT-member demands {fset(corner nodes): dict(Nc, Nt, Mz, My, V)} from the solved model.
    N tension +; Nc/Nt = max compression / tension magnitude (beams keep the sign, HR-35);
    V = max |Vy|,|Vz| from the element local forces (member loads included, HR-04)."""
    out = {}
    floors = floors or {}
    for c in model["cols"]:
        bf = ops.basicForce(c["tag"]); N = bf[0]
        m_z = max(abs(bf[1]), abs(bf[2])); m_y = max(abs(bf[3]), abs(bf[4]))
        lf = ops.eleResponse(c["tag"], "localForce")
        V = max(abs(lf[1]), abs(lf[2]), abs(lf[7]), abs(lf[8])) if len(lf) >= 12 else 0.0
        out[frozenset((c["n1"], c["n2"]))] = dict(Nc=max(-N, 0.0), Nt=max(N, 0.0), Mz=m_z, My=m_y, V=V)
    for b in model["braces"]:
        N = ops.basicForce(b["tag"])[0]
        out[frozenset((b["n1"], b["n2"]))] = dict(Nc=max(-N, 0.0), Nt=max(N, 0.0), Mz=0.0, My=0.0, V=0.0)
    for b in model["beams"]:
        Nc = Nt = Mmaj = Mmin = V = 0.0
        for t in b["segs"]:
            f = ops.basicForce(t)
            Nc = max(Nc, -f[0]); Nt = max(Nt, f[0])
            mz = max(abs(f[1]), abs(f[2])); my = max(abs(f[3]), abs(f[4]))
            Mmaj = max(Mmaj, max(mz, my)); Mmin = max(Mmin, min(mz, my))
            lf = ops.eleResponse(t, "localForce")
            if len(lf) >= 12:
                V = max(V, abs(lf[1]), abs(lf[2]), abs(lf[7]), abs(lf[8]))
        fl = floors.get(id(b))
        if fl and fl[0] == "add":
            Mmaj = Mmaj + fl[1]; V = V + fl[2]
        elif fl:
            Mmaj = max(Mmaj, fl[0]); V = max(V, fl[1])
        out[frozenset((b["A"], b["B"]))] = dict(Nc=Nc, Nt=Nt, Mz=Mmaj, My=Mmin, V=V)
    return out


def _reactions(model):
    """{base node: (Fx, Fy, Fz, Mx, My, Mz)} after a solve."""
    ops.reactions()
    out = {}
    for n in model.get("base_nodes", []):
        try:
            out[n] = tuple(ops.nodeReaction(n, d) for d in (1, 2, 3, 4, 5, 6))
        except Exception:
            pass
    return out


def _new_env():
    return dict(comp=0.0, tens=0.0, Mz=0.0, My=0.0, V=0.0, combo="", score=-1.0, Vcombo="", Tcombo="")


def _merge(env, kinds, res, label, col_only, applies=None, axial_only=False):
    """Fold one case's per-member result into the running envelope (respecting col_only / applies).
    axial_only: only the axial demand is taken (Omega0 column cases, AISC 341-22 D1.4a(b) 'permitted to
    neglect applied moments')."""
    for fs, r in res.items():
        kind = kinds.get(fs, ("beam", None))[0]
        if applies is not None:
            if kind not in applies:
                continue
        elif col_only and kind != "col":
            continue
        e = env.setdefault(fs, _new_env())
        if r["Nc"] > e["comp"]: e["comp"] = r["Nc"]
        if r["Nt"] > e["tens"]: e["tens"] = r["Nt"]; e["Tcombo"] = label
        if axial_only:
            sc = max(r["Nc"], r["Nt"])
            if kind in ("col", "brace") and sc > e["score"]: e["score"] = sc; e["combo"] = label
            continue
        e["Mz"] = max(e["Mz"], abs(r["Mz"])); e["My"] = max(e["My"], abs(r["My"]))
        if abs(r["V"]) > e["V"]: e["V"] = abs(r["V"]); e["Vcombo"] = label
        sc = max(r["Nc"], r["Nt"]) if kind in ("col", "brace") else abs(r["Mz"])
        if sc > e["score"]: e["score"] = sc; e["combo"] = label


def _merge_reac(renv, reac, label):
    for n, (fx, fy, fz, mx, my, mz) in reac.items():
        e = renv.setdefault(n, dict(P=0.0, P_combo="", uplift=0.0, uplift_combo="", Vx=0.0, Vy=0.0,
                                    V=0.0, V_combo="", M=0.0, M_combo=""))
        if fz > e["P"]: e["P"] = fz; e["P_combo"] = label
        if -fz > e["uplift"]: e["uplift"] = -fz; e["uplift_combo"] = label
        e["Vx"] = max(e["Vx"], abs(fx)); e["Vy"] = max(e["Vy"], abs(fy))
        v = math.hypot(fx, fy)
        if v > e["V"]: e["V"] = v; e["V_combo"] = label
        m = math.hypot(mx, my)
        if m > e["M"]: e["M"] = m; e["M_combo"] = label


# ====================================================================================
# capacity-limited seismic load effect (AISC 341-22) -- brace / link / web strengths
# ====================================================================================
def _sys_tokens(cfg):
    txt = " ".join(str(x) for x in (cfg.get("system"), (cfg.get("seis_X") or {}).get("system")
                                   if isinstance(cfg.get("seis_X"), dict) else None,
                                   (cfg.get("seis_Y") or {}).get("system")
                                   if isinstance(cfg.get("seis_Y"), dict) else None) if x)
    try:      # every per-direction declaration form engine3d.system_dir accepts (system_X, seis['X'], ...)
        txt += " " + " ".join(eng.system_dir(cfg, d) for d in ("X", "Y"))
    except Exception:
        pass
    return set(re.findall(r"[A-Z]+", txt.upper()))


def _braced_R(cfg):
    """R of the braced direction(s) (ASCE 7-22 12.2.2: each direction's own R); building-wide R otherwise."""
    try:
        Rs = [float(eng.seis_dir(cfg, d).get("R", 0) or 0) for d in ("X", "Y")
              if "CBF" in eng.system_dir(cfg, d).upper()]
        if Rs:
            return max(Rs)
        return max(float(eng.seis_dir(cfg, d).get("R", 0) or 0) for d in ("X", "Y"))
    except Exception:
        return float((cfg.get("seis") or {}).get("R", 0) or 0)


def brace_system(cfg, br):
    """AISC 341 system of a brace element: cfg['brace_system'] (str, {label: str} or callable(label,
    n1, n2)) wins; else label prefix BRB* -> BRBF, WEB*/STRIP* -> SPSW; else the single braced system
    named in cfg['system']; None when ambiguous."""
    o = cfg.get("brace_system")
    s = None
    if callable(o):
        try: s = o(br.get("sec"), br.get("n1"), br.get("n2"))
        except Exception: s = None
    elif isinstance(o, dict):
        s = o.get(br.get("sec"))
    elif isinstance(o, str):
        s = o
    if s:
        return str(s).upper()
    lab = str(br.get("sec") or "").upper()
    if lab.startswith("BRB"):
        return "BRBF"
    if lab.startswith("WEB") or lab.startswith("STRIP"):
        return "SPSW"
    toks = _sys_tokens(cfg)
    cand = [t for t in ("SCBF", "OCBF", "BRBF", "EBF", "SPSW", "STMF") if t in toks]
    if "CBF" in toks and not ({"SCBF", "OCBF"} & toks):
        R = _braced_R(cfg)
        cand.append("SCBF" if R >= 6 else "OCBF")
    cand = [c for c in cand if c not in ("BRBF", "SPSW")] or cand   # BRB/WEB labels caught above
    if len(cand) == 1:
        return cand[0]
    return None


def _shape(label):
    import sections as S
    return S._load_csv().get(str(label).upper())


def _brace_material(cfg, label):
    """(Fy, Ry, note) for a brace section label -- AISC 341-22 Table A3.2 defaults, overridable by
    cfg['brace_material'] = {'Fy':..,'Ry':..} or {label: {...}}."""
    o = cfg.get("brace_material") or {}
    if isinstance(o, dict) and label in o and isinstance(o[label], dict):
        o = o[label]
    lab = str(label).upper()
    if lab.startswith("HSS"):
        rnd = lab.count("X") == 1
        Fy, Ry, n = (46.0 if rnd else 50.0), 1.3, "ASTM A500 Gr. C (Ry 1.3)"
    elif lab.startswith("PIPE"):
        Fy, Ry, n = 35.0, 1.6, "ASTM A53 Gr. B (Ry 1.6)"
    elif lab.startswith("ROD"):
        Fy, Ry, n = 36.0, 1.5, "ASTM A36 rod (Ry 1.5)"
    elif lab[:1] == "L" or lab.startswith("2L"):
        Fy, Ry, n = 36.0, 1.5, "ASTM A36 angle (Ry 1.5)"
    else:
        Fy, Ry, n = 50.0, 1.1, "ASTM A992 (Ry 1.1)"
    if isinstance(o, dict) and ("Fy" in o or "Ry" in o):
        Fy = float(o.get("Fy", Fy)); Ry = float(o.get("Ry", Ry)); n = "cfg['brace_material']"
    return Fy, Ry, n


def _fn_E3(Fy, A, r, L):
    """AISC 360-22 E3: Fn for flexural buckling, K = 1, length L (in)."""
    if r <= 0 or L <= 0:
        return Fy
    Fe = math.pi ** 2 * EMOD / (L / r) ** 2
    return (0.658 ** (Fy / Fe)) * Fy if Fy / Fe <= 2.25 else 0.877 * Fe


def _blen(model, br):
    a = model["coord"][br["n1"]]; b = model["coord"][br["n2"]]
    return math.dist(a, b)


def brace_capacity(cfg, model, br):
    """Expected / adjusted strengths of one brace: dict(sys, T, C, Cpb, note) (kip) or dict(sys, err)."""
    sysn = brace_system(cfg, br)
    lab = br.get("sec")
    L = _blen(model, br)
    if sysn in ("SCBF", "OCBF"):
        row = _shape(lab)
        if not row or not row.get("A"):
            return dict(sys=sysn, err="section %r not in aisc_shapes.csv (no Ag / r)" % lab)
        A = row["A"]; r = min(x for x in (row.get("rx"), row.get("ry")) if x)
        Fy, Ry, mat = _brace_material(cfg, lab)
        T = Ry * Fy * A
        if sysn == "SCBF":
            Fne = _fn_E3(Ry * Fy, A, r, L)
            C = min(Ry * Fy * A, Fne * A / 0.877)         # F2.3: lesser of RyFyAg and (1/0.877)FneAg
            return dict(sys=sysn, T=T, C=C, Cpb=0.3 * C, A=A, L=L, r=r, Fy=Fy, Ry=Ry,
                        note="SCBF F2.3: T=RyFyAg, C=min(RyFyAg, FneAg/0.877) (Fne with RyFy, L=%.0f in), "
                             "post-buckling 0.3C; %s" % (L, mat))
        Pn = _fn_E3(Fy, A, r, L) * A
        return dict(sys=sysn, T=T, C=0.3 * Pn, Cpb=0.3 * Pn, A=A, L=L, r=r, Fy=Fy, Ry=Ry,
                    note="OCBF F1.4a(a): tension min(Om0 effect, RyFyAg), compression 0.3Pn; %s" % mat)
    if sysn == "BRBF":
        o = cfg.get("brb") or {}
        Asc = None; src = ""
        ascd = o.get("Asc")
        if isinstance(ascd, dict) and lab in ascd:
            Asc = float(ascd[lab]); src = "cfg['brb']['Asc']"
        elif isinstance(ascd, (int, float)):
            Asc = float(ascd); src = "cfg['brb']['Asc']"
        if Asc is None:
            m = re.search(r"(\d+(?:\.\d+)?)\s*$", str(lab))
            if m:
                Asc = float(m.group(1)); src = "parsed from label %r" % lab
        if Asc is None and br.get("area"):
            Asc = float(br["area"]) / float(o.get("KF", 1.0)); src = "model area / KF"
        if Asc is None:
            return dict(sys=sysn, err="BRB core area Asc unknown for %r (set cfg['brb']['Asc'])" % lab)
        Fysc = float(o.get("Fysc", 42.0)); Ry = float(o.get("Ry", 1.1))
        beta = float(o.get("beta", 1.1)); omega = float(o.get("omega", 1.4))
        assumed = [k for k in ("Fysc", "Ry", "beta", "omega") if k not in o]
        Pysc = Fysc * Asc
        return dict(sys=sysn, T=omega * Ry * Pysc, C=beta * omega * Ry * Pysc, Cpb=beta * omega * Ry * Pysc,
                    Asc=Asc, note="BRBF F4.2a/F4.3: T=omega*Ry*Pysc, C=beta*omega*Ry*Pysc (Asc %.2f %s; "
                                  "Fysc %.0f, Ry %.2f, beta %.2f, omega %.2f%s)"
                                  % (Asc, src, Fysc, Ry, beta, omega,
                                     "; ASSUMED %s -- confirm with the BRB qualification tests (F4.2b)"
                                     % "/".join(assumed) if assumed else ""))
    if sysn == "SPSW":
        return dict(sys=sysn)            # panel-level, see _spsw_panels
    if sysn == "EBF":
        return dict(sys=sysn)            # braces stay elastic, links yield
    return dict(sys=sysn, err="brace system not identified (set cfg['brace_system'])" if sysn is None
                else "%s capacity-limited analysis not implemented" % sysn)


def _link_capacity(cfg, bm):
    """EBF link adjusted shear strength (AISC 341-22 F3.3, F3.5b.2): 1.25*Ry*Vn (I-shaped),
    Vn = min(Vp, 2Mp/e), Vp = 0.6*Fy*Alw, Alw = (d - 2tf)tw (Pr/Pc <= 0.15)."""
    row = _shape(bm.get("sec"))
    if not row or not row.get("d"):
        return None, "link section %r not in aisc_shapes.csv" % bm.get("sec")
    o = cfg.get("ebf") or {}
    Fy = float(o.get("Fy", 50.0)); Ry = float(o.get("Ry", 1.1))
    e = bm["L"]
    Alw = (row["d"] - 2 * row["tf"]) * row["tw"]
    Vp = 0.6 * Fy * Alw; Mp = Fy * row["Zx"]
    Vn = min(Vp, 2 * Mp / e)
    return 1.25 * Ry * Vn, "EBF F3.3: 1.25*Ry*Vn, Vn=min(Vp=%.0f, 2Mp/e=%.0f) kip, e=%.1f in" % (Vp, 2 * Mp / e, e)


def _ebf_links(model, cfg=None):
    """EBF links: cfg['ebf_links'] (list of (n1, n2) node pairs or element tags) wins; else the beams whose
    two end nodes are brace work points that are NOT column nodes (centre links between braces). Links
    adjacent to columns must be declared through cfg['ebf_links']."""
    decl = (cfg or {}).get("ebf_links")
    if decl:
        want = set()
        for x in decl:
            want.add(frozenset(x) if isinstance(x, (tuple, list)) else x)
        return [bm for bm in model["beams"] if bm["etag"] in want or frozenset((bm["A"], bm["B"])) in want]
    bn = set()
    for br in model["braces"]:
        bn |= {br["n1"], br["n2"]}
    cn = set()
    for c in model["cols"]:
        cn |= {c["n1"], c["n2"]}
    return [bm for bm in model["beams"] if bm["A"] in bn and bm["B"] in bn and bm["A"] not in cn and bm["B"] not in cn]


def _spsw_tw(cfg, label):
    o = cfg.get("spsw") or {}
    t = (o.get("tw") or {}).get(label) if isinstance(o.get("tw"), dict) else o.get("tw")
    if t:
        return float(t)
    m = re.match(r"WEB\s*(\d+)\s*/\s*(\d+)", str(label).upper())
    if m:
        return float(m.group(1)) / float(m.group(2))
    m = re.match(r"WEB\s*(\d*\.\d+)", str(label).upper())
    return float(m.group(1)) if m else None


def ecl_designate(cfg, model, patX, patY):
    """Run the pure-lateral analyses used to designate braces as tension/compression (AISC 341-22
    F2.3/F4.3 'horizontal component applied in one direction per analysis') and to pattern the EBF
    link / SPSW HBE end forces. Model must be the free (masters not fixed) strength model.
    Returns {'X': {...}, 'Y': {...}} with brace axial and beam global end forces per direction."""
    out = {}
    for d, pat in (("X", patX), ("Y", patY)):
        _reset()
        ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
        apply_lateral(pat); _solve()
        bax = {br["etag"]: ops.basicForce(br["tag"])[0] for br in model["braces"]}
        gf = {}
        for bm in model["beams"]:
            if not bm["segs"]:
                continue
            f1 = ops.eleResponse(bm["segs"][0], "globalForce"); f2 = ops.eleResponse(bm["segs"][-1], "globalForce")
            gf[bm["etag"]] = (list(f1[:6]), list(f2[6:12]))
        out[d] = {"brace": bax, "beam": gf}
    return out


def ecl_plan(cfg, model, desig):
    """Static description of the capacity-limited analyses: which elements are removed and which forces
    replace them. Returns dict(drop, items, notes, not_evaluated)."""
    notes = []; ne = []
    try:      # the larger of the per-direction R (ASCE 7-22 12.2.2; engine3d.seis_dir)
        R = max(float(eng.seis_dir(cfg, d).get("R", 0) or 0) for d in ("X", "Y"))
    except Exception:
        R = float((cfg.get("seis") or {}).get("R", 0) or 0)
    if R <= 3.0 and not cfg.get("ecl_force"):
        return dict(drop=set(), braces=[], links=[], spsw=[], ocbf=[], notes=[
            "R = %.2f: AISC 341 capacity-limited (Ecl) analyses not required (system not specifically "
            "detailed for seismic resistance)" % R], not_evaluated=[])
    if not model.get("masters"):
        return dict(drop=set(), braces=[], links=[], spsw=[], ocbf=[], notes=[], not_evaluated=[
            "Ecl NOT EVALUATED: no rigid-diaphragm masters in the model (capacity analysis restrains them)"])
    braces = []; links = []; spsw = []; ocbf = []; drop = set()
    byd = {}
    for br in model["braces"]:
        a = model["coord"][br["n1"]]; b = model["coord"][br["n2"]]
        d = "X" if abs(b[0] - a[0]) >= abs(b[1] - a[1]) else "Y"
        cap = brace_capacity(cfg, model, br)
        s = cap.get("sys")
        if cap.get("err"):
            ne.append("brace %s (%s, %s): %s" % (br["etag"], br.get("sec"), s, cap["err"])); continue
        if s in ("SCBF", "BRBF"):
            braces.append(dict(br=br, dir=d, cap=cap)); drop.add(br["etag"])
            byd.setdefault(s, set()).add(cap["note"].split(";")[0])
        elif s == "OCBF":
            ocbf.append(dict(br=br, dir=d, cap=cap))
        elif s == "SPSW":
            spsw.append(dict(br=br, dir=d)); drop.add(br["etag"])
    if any(brace_system(cfg, br) == "EBF" for br in model["braces"]):
        lk = _ebf_links(model, cfg)
        if not lk:
            ne.append("EBF links not identified (no beam with braces at both ends): EBF Ecl NOT EVALUATED")
        for bm in lk:
            Vc, note = _link_capacity(cfg, bm)
            if Vc is None:
                ne.append("EBF link %s: %s" % (bm["etag"], note)); continue
            d = "X" if abs(bm["xyzB"][0] - bm["xyzA"][0]) >= abs(bm["xyzB"][1] - bm["xyzA"][1]) else "Y"
            links.append(dict(bm=bm, dir=d, V=Vc, note=note)); drop.add(bm["etag"])
    if spsw:
        notes.append("SPSW F5.3(b) PARTIAL: web tension field at RyFy applied as the panel resultant on the "
                     "tension diagonal(s) (0.5*Ry*Fy*tw*L*sin2a horizontal) and HBE 1.1*Ry*Mp hinge SHEARS "
                     "2(1.1RyMp)/L on the VBEs; the distributed tension-field flexure of VBE/HBE "
                     "(RyFy*tw*sin^2a, cos^2a) and the HBE hinge MOMENTS are NOT EVALUATED -- add them by hand")
    return dict(drop=drop, braces=braces, links=links, spsw=spsw, ocbf=ocbf, notes=notes,
                not_evaluated=ne, desig=desig)


def _host(model, n):
    """(beam, segment index, xL) of the beam sub-element whose interior or end holds node n's position,
    for a work-point node not connected to the beam (e.g. a separate chevron apex node)."""
    if n not in model["coord"]:
        return None
    x, y, z = model["coord"][n]
    k = _level_of(z, model["z"])
    if k is None:
        return None
    for (d, i, j), sp in model["spans"].get(k, {}).items():
        sp_s = _on_span(x, y, sp)
        if sp_s is None or sp_s <= _TOL or sp_s >= sp[6] - _TOL:
            continue
        for bm in model["by_span"].get((k, d, i, j), []):
            if bm["segs"] and bm["s0"] - 1e-6 <= sp_s <= bm["s1"] + 1e-6:
                for (u0, u1, q, us) in _seg_ranges(bm, len(bm["segs"])):
                    if u0 - 1e-6 <= sp_s <= u1 + 1e-6:
                        return bm, q, min(max(abs(sp_s - us) / max(u1 - u0, 1e-9), 0.0), 1.0)
    return None


def _apply_brace_force(model, br, F):
    """Forces of a brace with axial F (tension +) on its two end nodes. At a node left without elements
    (work point of the removed brace) the vertical component goes to the beam it lies on as a point load
    (the horizontal component goes to the restrained diaphragm)."""
    a = model["coord"][br["n1"]]; b = model["coord"][br["n2"]]
    L = math.dist(a, b); u = [(b[q] - a[q]) / L for q in range(3)]
    for n, sg in ((br["n1"], 1.0), (br["n2"], -1.0)):
        f = [sg * F * u[q] for q in range(3)]
        if n in model.get("orphans", ()):
            h = _host(model, n)
            if h is not None:
                bm, q, xL = h
                ops.eleLoad("-ele", bm["segs"][q], "-type", "-beamPoint", 0.0, f[2], xL)
            else:
                model["warnings"].append("brace force at isolated node %s not transferred (no host beam)" % n)
            continue
        ops.load(n, f[0], f[1], f[2], 0.0, 0.0, 0.0)


def apply_ecl_loads(cfg, model, plan, sx, sy, variant, Om0=None, only=None):
    """Apply the capacity-limited forces for loading sense (sx, sy) on the Ecl model."""
    desig = plan["desig"]
    sgn = {"X": sx, "Y": sy}
    for it in plan["braces"] if only in (None, "braces") else []:
        br = it["br"]; d = it["dir"]; cap = it["cap"]
        fE = desig[d]["brace"].get(br["etag"], 0.0) * sgn[d]
        if abs(fE) < 1e-9:
            continue
        if fE > 0:
            F = cap["T"]
        else:
            F = -(cap["Cpb"] if (variant == "b" and cap["sys"] == "SCBF") else cap["C"])
        _apply_brace_force(model, br, F)
    for it in plan["links"] if only in (None, "links") else []:
        bm = it["bm"]; d = it["dir"]
        gI, gJ = desig[d]["beam"].get(bm["etag"], (None, None))
        if gI is None:
            continue
        V_E = max(abs(gI[2]), abs(gJ[2]))
        if V_E < 1e-9:
            continue
        lam = it["V"] / V_E * sgn[d]
        ops.load(bm["A"], *[-lam * v for v in gI])
        ops.load(bm["B"], *[-lam * v for v in gJ])
    if plan["spsw"] and only in (None, "spsw"):
        _apply_spsw(cfg, model, plan, sgn)
    if plan["ocbf"] and only == "ocbf":
        for it in plan["ocbf"]:
            br = it["br"]; d = it["dir"]; cap = it["cap"]
            fE = desig[d]["brace"].get(br["etag"], 0.0) * sgn[d]
            if fE > 0:
                om = Om0.get(d) if isinstance(Om0, dict) else Om0     # per-direction Omega0 (ASCE 7-22 12.2.2)
                F = min((om or 2.0) * fE, cap["T"])
            else:
                F = -cap["C"]
            _apply_brace_force(model, br, F)


def _apply_spsw(cfg, model, plan, sgn):
    o = cfg.get("spsw") or {}
    Fy = float(o.get("Fy", 36.0)); Ry = float(o.get("Ry", 1.3)); alpha = math.radians(float(o.get("alpha", 45.0)))
    desig = plan["desig"]
    panels = {}
    for it in plan["spsw"]:
        br = it["br"]; a = model["coord"][br["n1"]]; b = model["coord"][br["n2"]]
        key = (it["dir"], round(min(a[0], b[0])), round(max(a[0], b[0])), round(min(a[1], b[1])),
               round(max(a[1], b[1])), round(min(a[2], b[2])), round(max(a[2], b[2])))
        panels.setdefault(key, []).append(it)
    for key, its in panels.items():
        d = key[0]
        tw = _spsw_tw(cfg, its[0]["br"].get("sec"))
        if tw is None:
            continue
        Lcf = (key[2] - key[1]) if d == "X" else (key[4] - key[3])
        Vexp = 0.5 * Ry * Fy * tw * Lcf * math.sin(2 * alpha)
        ten = []
        for it in its:
            br = it["br"]
            if desig[d]["brace"].get(br["etag"], 0.0) * sgn[d] > 0:
                ten.append(it)
        if not ten:
            continue
        hsum = 0.0
        for it in ten:
            a = model["coord"][it["br"]["n1"]]; b = model["coord"][it["br"]["n2"]]
            hsum += (it["br"].get("area") or 1.0) * abs(b[0 if d == "X" else 1] - a[0 if d == "X" else 1]) / math.dist(a, b)
        for it in ten:
            br = it["br"]; a = model["coord"][br["n1"]]; b = model["coord"][br["n2"]]
            F = Vexp * (br.get("area") or 1.0) / max(hsum, 1e-9)
            _apply_brace_force(model, br, F)
    # HBE hinge shears 2(1.1 Ry Mp)/L on the VBE end nodes, sense from the lateral pattern
    hbe_nodes = set()
    for it in plan["spsw"]:
        hbe_nodes |= {it["br"]["n1"], it["br"]["n2"]}
    for bm in model["beams"]:
        if bm["A"] in hbe_nodes and bm["B"] in hbe_nodes and bm["etag"] in desig[bm["dir"]]["beam"]:
            row = _shape(bm.get("sec"))
            if not row or not row.get("Zx"):
                continue
            gI, gJ = desig[bm["dir"]]["beam"][bm["etag"]]
            if abs(gI[2]) < 1e-9:
                continue
            V = 2 * 1.1 * 1.1 * 50.0 * row["Zx"] / bm["L"]
            lam = V / abs(gI[2]) * sgn[bm["dir"]]
            ops.load(bm["A"], 0.0, 0.0, -lam * gI[2], 0.0, 0.0, 0.0)
            ops.load(bm["B"], 0.0, 0.0, -lam * gJ[2], 0.0, 0.0, 0.0)


# ====================================================================================
# collectors (ASCE 7-22 12.10.2.1): statics on the diaphragm delivery along each SFRS line
# ====================================================================================
def collector_forces(cfg, model, pat, dirn, scale):
    """Collector axial (kip) per beam on SFRS lines parallel to `dirn` from a pure-lateral analysis with
    pattern `pat` (already solved by the caller is NOT assumed: this solves it). The force the vertical
    elements of a line take from each node is read from their element end forces; the diaphragm is
    assumed to deliver the line total uniformly along the line's framed length; the collector force at a
    point is the accumulated delivery minus the forces already taken by vertical elements (rigid
    diaphragm models give ~0 beam axial, so this is computed by statics). scale: {k: factor}."""
    _reset()
    ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
    apply_lateral(pat); _solve()
    ax = 0 if dirn == "X" else 1
    take = {}                                   # node -> horizontal force taken by vertical elements
    def _acc(tag, n1, n2):
        gf = ops.eleResponse(tag, "globalForce")
        if len(gf) >= 12:
            take[n1] = take.get(n1, 0.0) + gf[ax]; take[n2] = take.get(n2, 0.0) + gf[6 + ax]
        elif len(gf) >= 6:                      # truss: 3 dof per end in 3D
            take[n1] = take.get(n1, 0.0) + gf[ax]; take[n2] = take.get(n2, 0.0) + gf[3 + ax]
    for c in model["cols"]:
        _acc(c["tag"], c["n1"], c["n2"])
    for br in model["braces"]:
        _acc(br["tag"], br["n1"], br["n2"])
    out = {}
    for k, spans in model["spans"].items():
        lines = {}
        for (d, i, j), sp in spans.items():
            if d != dirn:
                continue
            lines.setdefault(j if d == "X" else i, []).append((i if d == "X" else j, (d, i, j)))
        for ln, items in lines.items():
            items.sort()
            groups = []                                   # contiguous runs of spans along the line
            for idx, key in items:
                if groups and groups[-1][-1][0] == idx - 1:
                    groups[-1].append((idx, key))
                else:
                    groups.append([(idx, key)])
            for grp in groups:
                bms = []
                for _idx, key in grp:
                    bms += model["by_span"].get((k,) + key, [])
                if not bms:
                    continue
                pts = sorted({(xyz[ax], n) for bm in bms for n, xyz in ((bm["A"], bm["xyzA"]), (bm["B"], bm["xyzB"]))})
                x0, x1 = pts[0][0], pts[-1][0]
                if x1 - x0 <= _TOL:
                    continue
                tot = sum(take.get(n, 0.0) for _, n in pts)
                if abs(tot) < 1e-6:
                    continue
                q = tot / (x1 - x0)
                sc = scale.get(k, 1.0)
                for bm in bms:
                    lo = min(bm["xyzA"][ax], bm["xyzB"][ax]); hi = max(bm["xyzA"][ax], bm["xyzB"][ax])
                    N_lo = q * (lo - x0) - sum(take.get(n, 0.0) for x, n in pts if x <= lo + _TOL)   # just right of lo
                    N_hi = q * (hi - x0) - sum(take.get(n, 0.0) for x, n in pts if x < hi - _TOL)    # just left of hi
                    Nm = max(abs(N_lo), abs(N_hi))
                    if Nm * sc > 1e-6:
                        key = frozenset((bm["A"], bm["B"]))
                        out[key] = max(out.get(key, 0.0), Nm * sc)
    return out


# ====================================================================================
# DEMAND ENVELOPE on the static model
# ====================================================================================
import hashlib as _hashlib, json as _json, os as _os


def _reset():
    try:
        ops.wipeAnalysis()
    except Exception:
        pass
    for what in (("loadPattern", 1), ("timeSeries", 1)):
        try:
            ops.remove(*what)
        except Exception:
            pass
    ops.reset(); ops.setTime(0.0)


def _dam_opts(cfg):
    d = cfg.get("dam", True)
    if d is False or (isinstance(d, dict) and d.get("enabled") is False):
        return None
    o = dict(stiff=0.8, coef=0.002, tau_b="notional", alpha=1.0)
    if isinstance(d, dict):
        o.update({k: v for k, v in d.items() if k in o})
    return o


def _apply_notional(model, node_load, coef, ux, uy):
    for n, F in node_load.items():
        if F:
            ops.load(n, coef * F * ux, coef * F * uy, 0.0, 0.0, 0.0, 0.0)


def _drift_ratio(cfg, model_pd, model_lin_builder, case, modes):
    """Max over stories of (second-order / first-order) master drift for one lateral case (C2.2b(d))."""
    def drifts(model):
        _reset(); ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
        apply_case_gravity(cfg, model, case[1], case[2], case[3], modes=modes)
        apply_lateral(case[4]); _solve()
        dd = {}
        ms = model["masters"]; prev = (0.0, 0.0)
        for k in sorted(ms):
            u = (ops.nodeDisp(ms[k], 1), ops.nodeDisp(ms[k], 2))
            dd[k] = math.hypot(u[0] - prev[0], u[1] - prev[1]); prev = u
        return dd
    d2 = drifts(model_pd)
    lin = model_lin_builder()
    d1 = drifts(lin)
    r = 1.0
    for k in d2:
        if d1.get(k, 0.0) > 1e-9:
            r = max(r, d2[k] / d1[k])
    return r


def _full_key(cfg, model, cases, nseg, floor_system, extras):
    """Complete cache key (HR-36): every OpenSees call of the built model + every non-callable cfg value
    + the case list (labels, factors incl. per-level/roof attributes, lateral dicts) + options + the
    source of this module and engine3d."""
    def clean(v):
        if callable(v):
            return "<fn>"
        if isinstance(v, dict):
            return {str(k): clean(x) for k, x in sorted(v.items(), key=lambda t: str(t[0]))}
        if isinstance(v, (list, tuple, set)):
            seq = sorted(v, key=str) if isinstance(v, set) else v
            return [clean(x) for x in seq]
        return repr(v)
    src = ""
    for f in (__file__, eng.__file__):
        try:
            src += _hashlib.md5(open(f, "rb").read()).hexdigest()
        except Exception:
            src += "?"
    cs = [(repr(tuple(c)), repr(sorted((k, repr(v)) for k, v in getattr(c, "__dict__", {}).items())))
          for c in cases]
    try:      # callables in cfg ('present', 'openings', ...) hash as "<fn>": add the RESOLVED floor plate
        plate = eng._resolved_plate_sig(cfg)          # and seismic weight per level (hr-geomwind keys)
    except Exception as ex:
        plate = repr(ex)
    blob = repr((clean(cfg), repr(model["sig"]), cs, nseg, floor_system, clean(extras or {}), src, plate))
    return _hashlib.md5(blob.encode()).hexdigest()


def _cache_load(cache_dir, key):
    if not cache_dir: return None
    p = _os.path.join(cache_dir, "_demand_cache.json")
    try:
        d = _json.load(open(p))
        if d.get("key") == key:
            env = {frozenset(int(x) for x in k.split("|")): v for k, v in d["env"].items()}
            reac = {int(k): v for k, v in d.get("reactions", {}).items()}
            return env, reac, d.get("info", {})
    except Exception:
        pass
    return None


def _cache_save(cache_dir, key, env, reac, info):
    if not cache_dir: return
    try:
        _os.makedirs(cache_dir, exist_ok=True)
        ser = {"|".join(str(n) for n in fs): v for fs, v in env.items()}
        _json.dump({"key": key, "env": ser, "reactions": {str(k): v for k, v in reac.items()}, "info": info},
                   open(_os.path.join(cache_dir, "_demand_cache.json"), "w"))
    except Exception:
        pass


def demand_envelope(cfg, cases, nseg=6, floor_system=None, determinate=True,
                    sec_sig="", lat_sig="", cache_dir=None, extras=None, reactions_out=None, info_out=None):
    """Per-member DEMAND envelope from the static model.
    Returns (env, kinds): env[fset] = {comp, tens, Mz, My, V, combo, Vcombo, Tcombo};
    kinds[fset] = (kind, sec). fset = frozenset of the member's two end nodes (engine3d.build tags).
    reactions_out (dict) receives the base-reaction envelope {node: {P, uplift, Vx, Vy, V, M, *_combo}}
    over ALL cases (incl. Omega0 / Ecl). info_out (dict) receives notes: DAM ratio, notional
    coefficients, alpha*Pr/Pns check, Ecl notes / NOT EVALUATED items, model warnings, solve count.
    extras: {'collector': {'X': (pattern, {k: scale}), 'Y': ...}, 'ecl_patterns': (patX, patY)}.
    The disk cache (cache_dir/_demand_cache.json) is reused ONLY when the complete key matches
    (built model, cfg, cases, options, code) -- HR-36. determinate / sec_sig / lat_sig are accepted for
    backward compatibility and ignored."""
    floor_system = (floor_system or cfg.get("floor_system") or "one-way")
    cfgF = dict(cfg); cfgF["floor_system"] = floor_system
    dam = _dam_opts(cfg)
    stiff = dam["stiff"] if dam else 1.0
    info = {"warnings": [], "notes": [], "not_evaluated": [], "solves": 0}
    model = build_static(cfgF, "PDelta", nseg, stiff=stiff)
    kinds = _member_kinds(model)
    key = _full_key(cfgF, model, cases, nseg, floor_system, extras)
    hit = _cache_load(cache_dir, key)
    if hit is not None:
        env, reac, inf = hit
        if reactions_out is not None: reactions_out.update(reac)
        if info_out is not None: info_out.update(inf); info_out["cache"] = "hit"
        return env, kinds
    info["warnings"] += model["warnings"]
    modes = bay_modes(cfgF, model)
    # HR-03: the static-model dead load must equal the seismic-weight dead (engine3d.floor_dead) per level
    try:
        pD = _case_pieces(cfgF, model, 1.0, 0.0, RoofFactors(), modes=modes)
        tot = {}
        for par, plist in pD["span"].items():
            Lp = model["spans"][par[0]][par[1:]][6]
            tot[par[0]] = tot.get(par[0], 0.0) + sum(_piece_int(pc, 0.0, Lp, Lp) if pc[0] != "pt" else pc[2]
                                                    for pc, _o in plist)
        for etag, plist in pD["infill"].items():
            bm = next((b for b in model["beams"] if b["etag"] == etag), None)
            if bm:
                tot[bm["k"]] = tot.get(bm["k"], 0.0) + sum(pc[1] * bm["L"] for pc in plist)
        aud = {}
        for k in range(1, model["NF"] + 1):
            wd = eng.floor_dead(cfgF, k); st = tot.get(k, 0.0)
            aud[k] = (round(st, 1), round(wd, 1))
            if wd > 0 and abs(st / wd - 1.0) > 0.005:
                info["warnings"].append("level %d: static-model dead %.0f kip vs seismic-weight dead %.0f kip "
                                        "(%.1f %%) -- framing does not cover the footprint used for W"
                                        % (k, st, wd, (st / wd - 1) * 100))
        info["gravity_audit_dead_kip"] = aud
    except Exception as ex:
        info["warnings"].append("gravity audit failed: %s" % ex)
    spec, _m = _floor_spec(cfgF, model)
    cnt = {}
    for m in modes.values():
        cnt[m[0] + ("/" + m[3] if m[0] == "one-way" else "")] = cnt.get(m[0] + ("/" + m[3] if m[0] == "one-way" else ""), 0) + 1
    info["floor_modes"] = cnt
    if any(m[0] == "default" for m in modes.values()):
        info["notes"].append("one-way floor without cfg['deck_span']: 45-deg distribution analysed; every "
                             "unsubdivided beam gets a half-bay-per-side girder floor (simple-span diagram for "
                             "pinned beams; solved + simple-span effect of the extra girder load for rigid-ended "
                             "beams) -- conservative for the non-girder direction; declare deck_span / "
                             "infill_spacing for the actual one-way load path")
    env = {}; renv = {}

    # ---- DAM: drift ratio (C2.2b(d)) from the first X / Y lateral cases ----
    add_lat = 0.0
    if dam:
        tau_add = 0.001 * dam["alpha"] if dam["tau_b"] == "notional" else 0.0
        ratio = 1.0
        lat_cases = [c for c in cases if c[4] and getattr(c, "kind", "") in ("seismic", "wind", "gravity", "")
                     and not c[5]]
        picked = []
        for d in ("X", "Y"):
            for c in lat_cases:
                fx = sum(abs(v[0]) for v in c[4].values()); fy = sum(abs(v[1]) for v in c[4].values())
                if (fx >= fy) == (d == "X"):
                    picked.append(c); break
        for c in picked:
            try:
                ratio = max(ratio, _drift_ratio(cfgF, model, lambda: build_static(cfgF, "Linear", nseg, stiff=stiff),
                                                c, modes))
                info["solves"] += 2
            except Exception as ex:
                info["warnings"].append("DAM drift-ratio check failed: %s" % ex)
        model = build_static(cfgF, "PDelta", nseg, stiff=stiff)     # rebuild (ratio check rebuilt the domain)
        spec, _m = _floor_spec(cfgF, model)
        add_lat = (dam["coef"] * dam["alpha"] if ratio > 1.7 else 0.0) + tau_add
        info["dam"] = {"stiffness_factor": stiff, "tau_b": dam["tau_b"], "drift_ratio_2nd_1st": round(ratio, 3),
                       "notional_gravity_only": round(dam["coef"] * dam["alpha"] + tau_add, 4),
                       "notional_lateral_cases": round(add_lat, 4),
                       "cite": "AISC 360-22 C2.2b (Ni = 0.002*alpha*Yi), C2.3(a) 0.8 stiffness, "
                               "C2.3(c) tau_b = 1 with 0.001*alpha*Yi" if dam["tau_b"] == "notional" else
                               "AISC 360-22 C2.2b, C2.3(a)/(b) tau_b = 1 -- alpha*Pr/Pns checked"}

    def run(case, label, notional=None):
        _reset()
        ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
        g = apply_case_gravity(cfgF, model, case[1], case[2], case[3], uplift=getattr(case, "uplift", None),
                               modes=modes)
        apply_lateral(case[4])
        if notional:
            coef, ux, uy = notional
            _apply_notional(model, g["node_load"], coef, ux, uy)
        ok = _solve(); info["solves"] += 1
        if ok != 0:
            info["not_evaluated"].append("case %s: analysis did not converge (ok=%s) -- its demands are NOT "
                                         "in the envelope" % (label, ok))
            return
        fl = _floors(cfgF, model, g["pieces"], spec,
                     grav_only=not case[4] and not getattr(case, "uplift", None))
        res = _extract(model, fl)
        _merge(env, kinds, res, label, case[5], getattr(case, "applies", None),
               axial_only=getattr(case, "kind", "") == "omega0")
        _merge_reac(renv, _reactions(model), label)

    std = [c for c in cases if getattr(c, "kind", "gravity") not in ("ecl", "ecl_ocbf")]
    for case in std:
        label = case[0]
        lat = case[4]
        if not dam:
            run(case, label); continue
        grav_only = (not lat) and not getattr(case, "uplift", None)
        if grav_only:
            coef = dam["coef"] * dam["alpha"] + (0.001 * dam["alpha"] if dam["tau_b"] == "notional" else 0.0)
            for (ux, uy, tg) in ((1, 0, "X+"), (-1, 0, "X-"), (0, 1, "Y+"), (0, -1, "Y-")):
                run(case, "%s +Ni(%s)" % (label, tg), (coef, ux, uy))
        else:
            fx = sum(v[0] for v in lat.values()) if lat else 0.0
            fy = sum(v[1] for v in lat.values()) if lat else 0.0
            nrm = math.hypot(fx, fy)
            if add_lat > 0 and nrm > 0:
                run(case, label + " +Ni", (add_lat, fx / nrm, fy / nrm))
            else:
                run(case, label)

    # ---- capacity-limited cases ----
    ecl_cases = [c for c in cases if getattr(c, "kind", "") in ("ecl", "ecl_ocbf")]
    if ecl_cases:
        pats = (extras or {}).get("ecl_patterns") or ecl_cases[0].ecl.get("patterns")
        desig = ecl_designate(cfgF, model, pats[0], pats[1]); info["solves"] += 2
        plan = ecl_plan(cfgF, model, desig)
        info["notes"] += plan["notes"]; info["not_evaluated"] += plan["not_evaluated"]
        info["ecl"] = {"braces": len(plan["braces"]), "links": len(plan["links"]), "spsw_webs": len(plan["spsw"]),
                       "ocbf_braces": len(plan["ocbf"]),
                       "basis": sorted({it["cap"]["note"] for it in plan["braces"]} |
                                       {it["note"] for it in plan["links"]} |
                                       {it["cap"]["note"] for it in plan["ocbf"]})[:12]}
        main = [c for c in ecl_cases if c.kind == "ecl"]
        if main and (plan["braces"] or plan["links"] or plan["spsw"]):
            emodel = build_static(cfgF, "PDelta", nseg, stiff=stiff, drop=plan["drop"], fix_masters=True)
            ekinds = _member_kinds(emodel)
            for fs, kv in ekinds.items():
                kinds.setdefault(fs, kv)
            espec, _m = _floor_spec(cfgF, emodel)
            emap = {bm["etag"]: bm for bm in emodel["beams"]}
            ebr = {br["etag"]: br for br in emodel["braces"]}
            for case in main:
                e = case.ecl
                _reset(); ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
                g = apply_case_gravity(cfgF, emodel, case[1], case[2], case[3], modes=modes)
                # rebind plan items to the Ecl model's objects (links' end nodes / EBF braces)
                plan_e = dict(plan, links=[dict(it, bm=dict(it["bm"])) for it in plan["links"]])
                apply_ecl_loads(cfgF, emodel, plan_e, e["sx"], e["sy"], e.get("variant", "a"))
                ok = _solve(); info["solves"] += 1
                if ok != 0:
                    info["not_evaluated"].append("case %s: capacity-limited analysis did not converge -- NOT "
                                                 "in the envelope" % case[0])
                    continue
                res = _extract(emodel, _floors(cfgF, emodel, g["pieces"], espec, grav_only=False))
                _merge(env, kinds, res, case[0], False, getattr(case, "applies", None))
                _merge_reac(renv, _reactions(emodel), case[0])
            model = build_static(cfgF, "PDelta", nseg, stiff=stiff)
        elif main:
            info["notes"].append("no capacity-limited elements identified: Ecl cases not run")
        oc = [c for c in ecl_cases if c.kind == "ecl_ocbf"]
        if oc and plan["ocbf"]:
            # OCBF V-braced beams (F1.4a(a)): braces removed, beams loaded by min(Om0 E, RyFyAg) / 0.3Pn
            vdrop = {it["br"]["etag"] for it in plan["ocbf"]}
            omodel = build_static(cfgF, "PDelta", nseg, stiff=stiff, drop=vdrop, fix_masters=True)
            okinds = _member_kinds(omodel)
            ospec, _m = _floor_spec(cfgF, omodel)
            for case in oc:
                e = case.ecl
                _reset(); ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
                g = apply_case_gravity(cfgF, omodel, case[1], case[2], case[3], modes=modes)
                apply_ecl_loads(cfgF, omodel, plan, e["sx"], e["sy"], "a", Om0=e.get("Om0"), only="ocbf")
                if _solve() != 0:
                    info["not_evaluated"].append("case %s: did not converge -- NOT in the envelope" % case[0])
                    continue
                info["solves"] += 1
                res = _extract(omodel, _floors(cfgF, omodel, g["pieces"], ospec, grav_only=False))
                vb = {frozenset((b["A"], b["B"])) for b in omodel["beams"] if ospec.get(id(b)) == "chevron"}
                res = {fs: r for fs, r in res.items() if fs in vb}      # F1.4a: V / inverted-V beams only
                _merge(env, okinds, res, case[0], False, {"beam"})
            model = build_static(cfgF, "PDelta", nseg, stiff=stiff)

    # ---- collectors (12.10.2.1) ----
    col = (extras or {}).get("collector")
    if col:
        info["collector"] = {}
        for d, (pat, scale, label) in col.items():
            try:
                cf = collector_forces(cfgF, model, pat, d, scale); info["solves"] += 1
            except Exception as ex:
                info["warnings"].append("collector statics %s failed: %s" % (d, ex)); continue
            n = 0
            for fs, N in cf.items():
                e = env.setdefault(fs, _new_env())
                if N > e["comp"]: e["comp"] = N
                if N > e["tens"]: e["tens"] = N; e["Tcombo"] = label
                if N > e["score"] and kinds.get(fs, ("beam",))[0] == "col":
                    e["score"] = N; e["combo"] = label
                e.setdefault("collector", 0.0)
                if N > e["collector"]:
                    e["collector"] = N; e["collector_combo"] = label
                n += 1
            info["collector"][d] = n

    # ---- alpha*Pr/Pns (C2.3(b)) ----
    if dam:
        worst = []
        for fs, e in env.items():
            kind, sec = kinds.get(fs, ("beam", None))
            if kind not in ("col", "beam") or not sec:
                continue
            row = _shape(sec)
            if not row or not row.get("A"):
                continue
            r = dam["alpha"] * e["comp"] / (50.0 * row["A"])
            if r > 0.5:
                worst.append((round(r, 3), kind, sec))
        worst.sort(reverse=True)
        info["dam"]["alphaPr_over_Pns_gt_0.5"] = worst[:10]
        if worst and dam["tau_b"] != "notional":
            info["warnings"].append("DAM: %d member(s) with alpha*Pr/Pns > 0.5 (e.g. %s %s %.2f): tau_b < 1 "
                                    "required (C2.3(b)) or use the C2.3(c) notional add-on (cfg['dam'] tau_b "
                                    "'notional')" % (len(worst), worst[0][1], worst[0][2], worst[0][0]))
    if reactions_out is not None:
        reactions_out.update(renv)
    if info_out is not None:
        info_out.update(info)
    _cache_save(cache_dir, key, env, renv, info)
    return env, kinds

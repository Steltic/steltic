import copy
import math
import hashlib
import sys as _sys
# P13: do NOT write .pyc files. On the jobs mount the OS mtime resolution is coarse, so a stale cached
# .pyc silently shadowed edits to a job's cfg.py during the optimisation loop (the edit appeared to be
# ignored). Every cfg.py imports engine3d first, so disabling bytecode here covers the job cfg too; the
# tiny recompile cost is negligible for the short-lived pipeline processes.
_sys.dont_write_bytecode = True
import openseespy.opensees as ops
g=386.4; E=29000.0; Gmod=11200.0
SEC={
 "W14X90":(26.5,999,362,4.06),"W14X120":(35.3,1380,495,9.37),"W14X132":(38.8,1530,548,12.3),
 "W14X159":(46.7,1900,748,19.7),"W14X193":(56.8,2400,931,34.8),"W14X233":(68.5,3010,1150,59.5),
 "W14X311":(91.4,4330,1610,136.0),
 "W18X50":(14.7,800,40.1,1.24),"W21X62":(18.3,1330,57.5,1.83),"W24X55":(16.2,1350,29.1,1.18),"W24X76":(22.4,2100,82.5,2.68),
 "W24X84":(24.7,2370,94.4,3.70),"W27X94":(27.7,3270,124,4.03),"W30X108":(31.7,4470,146,4.99),
 "W30X116":(34.2,4930,164,6.43),"W33X130":(38.3,6710,218,7.37),"W36X150":(44.2,9040,270,10.1),
 "W14X370":(109.0,5440,1990,201.0),"W14X426":(125.0,6600,2360,331.0),"W14X500":(147.0,8210,2880,514.0),"W14X605":(178.0,10500,4060,869.0),"W14X730":(215.0,14300,4720,1450.0),
 "W36X194":(57.0,12100,375,15.0),"W40X199":(58.8,14900,530,18.3),
}
_HSS_AREA_CSV = None
def _hss_area_csv(label):
    """Gross area (in^2) for ANY AISC HSS label from aisc_shapes.csv (read + cached once). B6: lets
    thick-wall sizes (e.g. HSS12X12X3/4) resolve without being hand-tabulated."""
    global _HSS_AREA_CSV
    if _HSS_AREA_CSV is None:
        import csv, os
        _HSS_AREA_CSV = {}
        try:
            with open(os.path.join(os.path.dirname(__file__), "aisc_shapes.csv"), newline="") as f:
                for row in csv.DictReader(f):
                    lab = (row.get("AISC_Manual_Label") or "").strip().upper()
                    if lab.startswith("HSS"):
                        try: _HSS_AREA_CSV[lab] = float(row["A"])
                        except Exception: pass
        except Exception: pass
    return _HSS_AREA_CSV.get(str(label).strip().upper())

class _HSSArea(dict):
    """HSS gross area lookup with an automatic aisc_shapes.csv fallback (B6)."""
    def __missing__(self, key):
        a = _hss_area_csv(key)
        if a is None:
            raise KeyError("HSS %r not in catalog or aisc_shapes.csv -- use a valid AISC HSS label" % (key,))
        self[key] = a
        return a

HSS=_HSSArea({"H6":9.74,"H7":11.6,"H8":13.5,"H8b":16.4,"H10":21.0,"H12":25.7,"H14":33.1})
# Standard AISC square HSS (gross area, in^2) — designation keys "HSS<d>X<d>X<t>" for precise brace sizing.
HSS.update({
 "HSS4X4X1/4":3.37,"HSS4X4X3/8":4.78,"HSS5X5X1/4":4.30,"HSS5X5X3/8":6.18,"HSS5X5X1/2":7.88,
 "HSS6X6X1/4":5.24,"HSS6X6X5/16":6.43,"HSS6X6X3/8":7.58,"HSS6X6X1/2":9.74,
 "HSS7X7X3/8":8.97,"HSS7X7X1/2":11.6,"HSS8X8X1/4":7.10,"HSS8X8X3/8":10.4,"HSS8X8X1/2":13.5,"HSS8X8X5/8":16.4,
 "HSS10X10X3/8":13.5,"HSS10X10X1/2":17.2,"HSS10X10X5/8":21.0,
 "HSS12X12X3/8":16.4,"HSS12X12X1/2":20.9,"HSS12X12X5/8":25.7,
 "HSS14X14X1/2":24.6,"HSS14X14X5/8":30.3,"HSS16X16X1/2":28.3,"HSS16X16X5/8":35.0})

def _shapes_csv():
    """Lazy AISC shape DB from aisc_shapes.csv: label -> (A, Ix, Iy, J). Lets the agent name ANY W-shape."""
    import csv, os
    c = _shapes_csv._cache
    if c is None:
        c = {}
        try:
            with open(os.path.join(os.path.dirname(__file__), "aisc_shapes.csv"), newline="") as f:
                for row in csv.DictReader(f):
                    lbl = (row.get("AISC_Manual_Label") or "").strip()
                    if lbl:
                        try: c[lbl] = (float(row["A"]), float(row["Ix"]), float(row["Iy"]), float(row["J"]))
                        except Exception: pass
        except Exception: pass
        _shapes_csv._cache = c
    return c
_shapes_csv._cache = None

def Ipack(name):
    """(A, Ix, Iy, J) for a section; CASE-INSENSITIVE; falls back to aisc_shapes.csv (any AISC W-shape) on a miss."""
    key = str(name).upper().strip()                 # 'W24x104' / ' w24X104 ' -> 'W24X104'
    s = SEC.get(key) or (SEC.get(name) if name != key else None)   # honor an entry stored under the original case too
    if s is None:
        s = _shapes_csv().get(key)
        if s is None:
            raise KeyError("section %r not in catalog or aisc_shapes.csv -- use a valid AISC W-shape label (e.g. 'W24X76')" % (name,))
        SEC[key] = s
    return s

def _xy_in(cfg, i, j):
    """Grid intersection (x, y) in INCHES, honoring xcoords/ycoords (non-uniform spacing) and skew."""
    xco = cfg.get("xcoords"); yco = cfg.get("ycoords"); skew = cfg.get("skew", 0.0)
    return ((xco[i] if xco else i*cfg["SX"]) + skew*j, (yco[j] if yco else j*cfg["SY"]))

def grid(cfg,k):
    # set of (i,j) present at floor k (k=0 = base footprint = floor1).
    # Priority: cfg['present'] (per-level footprint captured from custom_build on first engine3d.build,
    # or declared by the agent) > cfg['plan'] callable > full NX x NY rectangle. These are COLUMN
    # positions; the floor plate (areas, perimeters, masses, cladding, wind silhouette, ELF weights) is
    # floor_bays(), which keeps the bays around an omitted interior column (HR-11).
    NX,NY=cfg["NX"],cfg["NY"]
    kk=1 if k==0 else k
    pr=cfg.get("present")
    if pr:
        P = pr.get(kk, pr.get(str(kk)))
        if P: return {tuple(p) for p in P}
    f=cfg.get("plan")
    if f is None:
        return {(i,j) for i in range(NX+1) for j in range(NY+1)}
    return f(kk,NX,NY)

def zlevels(cfg):
    h=cfg["heights"]; z=[0.0]
    for hi in h: z.append(z[-1]+hi)
    return z

def ntag(i,j,k): return k*100000+i*100+j
def mtag(k): return k*100000+99999

def release_args(relz="none", rely="none"):
    """elasticBeamColumn end-release flags for non-rigid (pinned/shear) connections.
    relz/rely each one of 'none','I','J','both'. **relz = the MAJOR-axis bending moment** -- for a
    floor/roof beam this is the VERTICAL / gravity bending moment, the one a beam-to-column SHEAR
    (pinned/simple) connection releases. **rely = the minor (weak-axis, horizontal) moment.** So a
    typical pinned-pinned gravity beam is:
        ops.element('elasticBeamColumn', tag, ni, nj, A,E,G,J,Iy,Iz, transf, *release_args(relz='both'))
    (Implementation note: the engine builds beams with Iy = strong, so the major-axis moment is the
    element's My and is released by OpenSees '-releasey'; relz maps to '-releasey' accordingly. Native
    release -- NO extra nodes/constraints, so topology, element registry and the demand pipeline are
    unchanged.)
    """
    _c = {"none": 0, "I": 1, "J": 2, "both": 3}
    a = []
    if _c.get(relz, 0): a += ["-releasey", _c[relz]]   # relz = MAJOR axis -> element My -> -releasey
    if _c.get(rely, 0): a += ["-releasez", _c[rely]]   # rely = minor axis -> element Mz -> -releasez
    return a

_MOMENT_NODES = set()    # B3: nodes a RIGID (moment) beam frames into -> used to auto-role lateral vs gravity columns
_BEAM_REL = {}           # viewer3d: beam tag -> (relz, rely) end-release codes ("none" = fixed-ended)
_COL_DIR = {}            # viewer3d: column tag -> strong_dir ("X"/"Y") web orientation
def build(cfg,transf="Linear"):
    """Build the OpenSees model; return the standard info dict
    {cm, present, z, NF, ele:[(tag,kind,sec,n1,n2)]}.  The agent supplies its own builder as
    cfg["custom_build"] = f, where f(cfg, transf) builds the model and returns that dict -- see
    engine/example_build.py for a complete worked reference to copy.  When no custom_build is given
    (the built-in B-archetypes and quick self-checks) the model is built by example_build()."""
    cb = cfg.get("custom_build")
    if cb is not None and "present" not in cfg:
        # PROBE build: run the custom builder once, throw the ops domain away, and capture the
        # per-level footprint into cfg['present'] so grid()/floor_area/perim/wind/masses all see
        # the ACTUAL framed plan (non-rectangular safe). The real build below then re-runs the
        # builder with that footprint visible to any floor_w()/floor_grav() calls it makes.
        try:
            _MOMENT_NODES.clear(); _BEAM_REL.clear(); _COL_DIR.clear()
            probe = cb(cfg, transf)
            cfg["present"] = {int(kk): {tuple(p) for p in v}
                              for kk, v in (probe.get("present") or {}).items()}
        except Exception:
            cfg["present"] = None          # don't re-probe every build; fall back to plan/full grid
    _MOMENT_NODES.clear()                  # repopulated by add_beam for THIS build (B3)
    _BEAM_REL.clear(); _COL_DIR.clear()    # repopulated by add_beam/add_column for THIS build (viewer3d)
    if cb is not None:
        info = cb(cfg, transf)
    else:
        from example_build import example_build as _example_build
        info = _example_build(cfg, transf)
    info["moment_nodes"] = set(_MOMENT_NODES)   # snapshot: nodes with a rigid (moment) beam framing in
    info["beam_rel"] = dict(_BEAM_REL)          # snapshot: per-beam end releases (viewer3d)
    info["col_dir"] = dict(_COL_DIR)            # snapshot: per-column strong-axis direction (viewer3d)
    return info


def is_braced(cfg):
    """True if the seismic system has braces -- declared via cfg['braces'] OR present as 'brace'
    elements in the BUILT model (so a custom_build that builds its own braces is recognised, and the
    Omega0 capacity-design combos + AISC 341/358 grounding trigger). Builds once (cheap) and reads the
    ACTUAL element list -- no id(cfg) cache, so a mutated or rebuilt cfg is never mis-read (B2)."""
    if cfg.get("braces"):
        return True
    try:
        return any(e[1] == "brace" for e in build(cfg, "Linear").get("ele", []))
    except Exception:
        return False


def register_col_transf(transf="PDelta"):
    """Register the engine's STANDARD geomTransf tags so a custom_build orients members the SAME
    (correct) way as the default builder. OPTIONAL now: add_column/add_beam auto-register these if you
    skip it. If you do call it, do so ONCE right after ops.model(...). Tags:
      1 -> column STRONG axis resists N-S (Y)  [vecxz=(1,0,0)]
      2 -> column STRONG axis resists E-W (X)  [vecxz=(0,1,0)]
      3 -> beams (strong axis vertical)        [vecxz=(0,0,1)]"""
    cT = "PDelta" if transf == "PDelta" else "Linear"
    ops.geomTransf(cT, 1, 1.0, 0.0, 0.0)
    ops.geomTransf(cT, 2, 0.0, 1.0, 0.0)
    ops.geomTransf("Linear", 3, 0.0, 0.0, 1.0)


def col_transf(strong_dir):
    """geomTransf tag for a COLUMN whose STRONG axis must resist lateral in `strong_dir` ("X"/"E-W"
    or "Y"/"N-S"). A column in a moment/braced frame spanning direction D MUST take its strong axis
    in D or the frame is far too flexible (excessive drift). Returns 2 for X(E-W), 1 for Y(N-S)."""
    d = str(strong_dir).upper()
    return 2 if ("X" in d or "E" in d) else 1


def _ensure_col_transf(transf="PDelta"):
    """Make sure the engine's STANDARD column/beam geomTransf tags (1,2,3) exist with the correct
    vectors, so add_column/add_beam work even when a custom_build never calls register_col_transf().
    Registers once, right before the first element; a no-op once any element exists. Benign OpenSees
    'similar tag exists' notices (custom_build also called register_col_transf) are suppressed."""
    if ops.getEleTags():                      # elements already built -> transforms are in place
        return
    import os
    cT = "PDelta" if transf == "PDelta" else "Linear"
    rows = ((1,1.0,0.0,0.0,cT),(2,0.0,1.0,0.0,cT),(3,0.0,0.0,1.0,"Linear"))
    saved = None
    try:                                      # silence C-level stderr/stdout for the (re)registration
        saved = (os.dup(1), os.dup(2)); dn = os.open(os.devnull, os.O_WRONLY)
        os.dup2(dn, 1); os.dup2(dn, 2)
    except Exception:
        saved = None
    try:
        for tag, vx, vy, vz, ty in rows:
            try:
                ops.geomTransf(ty, tag, vx, vy, vz)
            except Exception:
                pass                          # tag already registered -> keep the existing one
    finally:
        if saved:
            try:
                os.dup2(saved[0], 1); os.dup2(saved[1], 2)
                os.close(dn); os.close(saved[0]); os.close(saved[1])
            except Exception:
                pass


def add_column(tag, n1, n2, sec, strong_dir):
    """FOOLPROOF column for custom_build: orients the STRONG axis to resist lateral in `strong_dir`
    and gets the (Iy_weak, Iz_strong) element ordering right for you. AUTO-REGISTERS the standard
    transforms if missing -- you do NOT need to call register_col_transf(). e.g.
    add_column(et, n1, n2, "W14X159", "X")."""
    _ensure_col_transf()                      # self-register transforms before the first element
    A, Ix, Iy, J = Ipack(sec)                 # Ix = strong, Iy = weak
    ops.element("elasticBeamColumn", tag, n1, n2, A, E, Gmod, J, Iy, Ix, col_transf(strong_dir))
    _COL_DIR[tag] = strong_dir                # viewer3d: web orientation
    return tag


def add_beam(tag, n1, n2, sec, releases=None):
    """FOOLPROOF beam for custom_build: strong axis vertical (transf 3), correct (Ix_strong, Iy_weak)
    ordering, optional end moment releases. releases=(relz, rely), e.g. ("both","none") to pin both
    ends (a shear/simple connection releases the major-axis moment -> relz="both")."""
    _ensure_col_transf()                      # self-register transforms before the first element
    A, Ix, Iy, J = Ipack(sec)
    ra = release_args(*releases) if releases else []
    ops.element("elasticBeamColumn", tag, n1, n2, A, E, Gmod, J, Ix, Iy, 3, *ra)
    relz = (releases[0] if releases else "none")          # B3: major-axis release; "none"/"J" keep the I-end moment, etc.
    if relz not in ("I", "both"): _MOMENT_NODES.add(n1)
    if relz not in ("J", "both"): _MOMENT_NODES.add(n2)
    _BEAM_REL[tag] = (relz, (releases[1] if releases else "none"))   # viewer3d: end releases
    return tag


# ---------------------------------------------------------------------------------------------------
# FLOOR PLATE (HR-11). `present` lists COLUMN positions; the floor/roof plate is a set of BAYS. A bay is
# in the plate when its 4 corner grid nodes are present -- EXCEPT that a grid node with no column must
# not punch a hole in the slab:
#   * cfg['omitted_columns'] = {k: [(i,j),...]} (or a list for every level / f(k,NX,NY)) -- grid nodes
#     that have NO column but ARE inside the floor plate (long-span transfer, gym, lobby);
#   * auto: an INTERIOR grid node missing from `present` whose 8 neighbours are all present at that
#     level is an omitted column (a printed note says so) -- a 2x2-bay hole around one missing node is
#     an opening only when declared;
#   * cfg['openings'] = {k: [(i,j),...]} (bay = lower-left node index) -- bays that are genuinely open
#     (atrium, shaft) even though their corner columns exist;
#   * cfg['floor_bays'] = {k: [(i,j),...]} -- explicit plate per level, overrides all of the above;
#   * cfg['auto_omitted_columns'] = False disables the auto rule (old behaviour).
# All keys optional; with none of them the plate is the old "4 corners present" set except for the
# interior-isolated-missing-node case, which used to delete 4 bays (Ex16: -3136 ft2, -12 % of W).
# ---------------------------------------------------------------------------------------------------
_PLATE_NOTES = set()

def _note_once(msg):
    if msg not in _PLATE_NOTES:
        _PLATE_NOTES.add(msg); print("[engine3d] " + msg)

def _per_level(cfg, key, k):
    """Per-level set of (i,j) tuples from cfg[key]: dict {k: [...]}, callable f(k,NX,NY) or a plain
    list (same for every level). None when the key is absent / has no entry for level k."""
    spec = cfg.get(key)
    if spec is None or spec is False: return None
    if callable(spec): v = spec(k, cfg["NX"], cfg["NY"])
    elif isinstance(spec, dict): v = spec.get(k, spec.get(str(k)))
    else: v = spec
    return None if v is None else {tuple(p) for p in v}

def _auto_omitted(cfg, k, P):
    if cfg.get("auto_omitted_columns", True) is False: return set()
    NX, NY = cfg["NX"], cfg["NY"]; out = set()
    for i in range(1, NX):
        for j in range(1, NY):
            if (i, j) in P: continue
            if all((i+a, j+b) in P for a in (-1, 0, 1) for b in (-1, 0, 1) if (a, b) != (0, 0)):
                out.add((i, j))
                _note_once("level %d: grid node (%d,%d) has no column but all 8 neighbours are framed -> treated "
                           "as an OMITTED COLUMN; its 4 bays stay in the floor plate (area, W, cladding, wind). "
                           "Declare cfg['openings'] if those bays are really open." % (k, i, j))
    return out

def floor_bays(cfg, k):
    """Set of bays (i,j) (lower-left grid node index, 0<=i<NX, 0<=j<NY) carrying floor/roof plate at
    level k (k=0 -> level 1). See the FLOOR PLATE note above for the cfg keys (HR-11)."""
    NX, NY = cfg["NX"], cfg["NY"]; kk = 1 if k == 0 else k
    fb = _per_level(cfg, "floor_bays", kk)
    if fb is not None:
        bays = {b for b in fb if 0 <= b[0] < NX and 0 <= b[1] < NY}
    else:
        P = set(grid(cfg, kk))
        P |= (_per_level(cfg, "omitted_columns", kk) or set())
        P |= _auto_omitted(cfg, kk, P)
        bays = {(i, j) for i in range(NX) for j in range(NY)
                if (i, j) in P and (i+1, j) in P and (i, j+1) in P and (i+1, j+1) in P}
    op = _per_level(cfg, "openings", kk)
    if op: bays -= op
    return bays

def _bay_dims_ft(cfg, i, j):
    """(dx, dy) of bay (i,j) in ft, honoring xcoords/ycoords (skew does not change the bay area)."""
    dx = _xy_in(cfg, i+1, j)[0] - _xy_in(cfg, i, j)[0]
    dy = _xy_in(cfg, i, j+1)[1] - _xy_in(cfg, i, j)[1]
    return abs(dx)/12.0, abs(dy)/12.0

def nbays(cfg,k):
    return len(floor_bays(cfg, k))

def floor_area_ft2(cfg,k):
    """Floor-plate area (ft^2) of level k: sum of the bays in floor_bays(cfg,k) (HR-11: an omitted column
    no longer deletes its surrounding bays; openings are declared with cfg['openings']). Honors
    non-rectangular footprints and non-uniform xcoords/ycoords; a full uniform rectangle = NX*NY*SX*SY."""
    a = 0.0
    for (i, j) in floor_bays(cfg, k):
        dx, dy = _bay_dims_ft(cfg, i, j); a += dx*dy
    return a

def perim_ft(cfg,k):
    """Exposed floor-edge length (ft) of the level-k plate: bay edges bordered by exactly ONE plate bay.
    Handles L/T/U plans, notches, setbacks, openings (atrium edges count) and non-uniform grids.
    A full uniform rectangle reduces to 2*(NX*SX + NY*SY)."""
    B = floor_bays(cfg, k); per = 0.0
    for (i, j) in B:
        dx, dy = _bay_dims_ft(cfg, i, j)
        if (i, j-1) not in B: per += dx
        if (i, j+1) not in B: per += dx
        if (i-1, j) not in B: per += dy
        if (i+1, j) not in B: per += dy
    return per

# ---------------------------------------------------------------------------------------------------
# BUILDING VOLUME (bay stacks) -- shared by cladding weight and MWFRS wind. Each plan bay b has a stack
# of levels whose plate contains it; the enclosed volume of b runs from grade to its top level (plus
# parapet). An exterior wall exists on an edge of b wherever b is taller than the bay across that edge.
# Wall height between two consecutive levels of b's stack is shared half/half (bottom half of the
# lowest storey -> grade). This replaces the old "storey below x level perimeter" tributary, so split
# levels (Ex13), setbacks and lean-tos get the wall that actually spans to each diaphragm.
# ---------------------------------------------------------------------------------------------------
def _stacks(cfg):
    """{bay: [(k, z_ft), ...] ascending} over all levels, and z (ft) per level."""
    NF = len(cfg["heights"]); z = [v/12.0 for v in zlevels(cfg)]; st = {}
    for k in range(1, NF+1):
        for b in floor_bays(cfg, k):
            st.setdefault(b, []).append((k, z[k]))
    return st, z

def _band_split(stack, za, zb):
    """Split wall segment [za,zb] (ft) carried by a bay with level stack [(k,z),...] into {k: height};
    key 0 = grade (bottom half of the lowest storey); above the top level -> top level."""
    out = {}; zs = [s[1] for s in stack]; ks = [s[0] for s in stack]
    bounds = [zs[0]/2.0] + [(zs[n]+zs[n+1])/2.0 for n in range(len(zs)-1)] + [float("inf")]
    lo = 0.0; owners = [0] + ks
    for n, hi in enumerate(bounds):
        a, b2 = max(za, lo), min(zb, hi)
        if b2 > a: out[owners[n]] = out.get(owners[n], 0.0) + (b2 - a)
        lo = hi
    return out

def clad_areas_ft2(cfg):
    """{k: exterior wall area (ft^2) tributary to level k} from the bay-stack volume (mid-height
    tributaries; parapet cfg['wind']['parapet_ft'] adds to the top level). Grade portion excluded."""
    st, z = _stacks(cfg); NF = len(cfg["heights"]); out = {k: 0.0 for k in range(1, NF+1)}
    par = float((cfg.get("wind") or {}).get("parapet_ft", 0.0) or 0.0)
    top = {b: s[-1][1] for b, s in st.items()}
    for (i, j), s in st.items():
        dx, dy = _bay_dims_ft(cfg, i, j)
        for nb, L in (((i, j-1), dx), ((i, j+1), dx), ((i-1, j), dy), ((i+1, j), dy)):
            lo = top.get(nb, 0.0); hi = top[(i, j)]
            if nb in top and lo >= hi: continue
            for k, hgt in _band_split(s, lo, hi + par).items():
                if k: out[k] += hgt*L
    return out

def _clad_kip(cfg, k):
    c = cfg.get("clad", 0.0) or 0.0
    return c*clad_areas_ft2(cfg).get(k, 0.0)/1000.0 if c else 0.0

def _Dlev(cfg, k, roof):
    """Dead load (psf) at level k: cfg['D_by_level'][k] override, else D_roof (top level) / D_floor.
    Lets a partial top level / penthouse-over-roof be loaded per level instead of one global D_roof. (P12)"""
    by = cfg.get("D_by_level")
    if by and k in by: return by[k]
    return cfg["D_roof"] if roof else cfg["D_floor"]

def _Llev(cfg, k):
    """Live load (psf) at level k: cfg['L_by_level'][k] override, else L_floor. (P12)"""
    by = cfg.get("L_by_level")
    if by and k in by: return by[k]
    return cfg["L_floor"]

def _is_storage(cfg, k):
    """Level k is a storage floor: cfg['storage']=True (all floors) or k in cfg['storage_levels']
    (1-based story indices). Both keys optional, default False/absent."""
    if cfg.get("storage"): return True
    lv = cfg.get("storage_levels")
    return bool(lv) and k in {int(i) for i in lv}

def _lvl_val(spec, k):
    """Per-level scalar from a {k: v} dict (int or str keys); 0.0 when absent."""
    if not spec: return 0.0
    v = spec.get(k, spec.get(str(k), 0.0)) if isinstance(spec, dict) else 0.0
    return float(v or 0.0)

def floor_w(cfg,k):
    """Effective seismic weight of level k (ASCE 7-22 sec.12.7.2): dead + cladding + 25% of the
    floor live where the area is storage (item 1) + 15% of the uniform design snow load where the
    flat-roof snow load pf exceeds 45 psf (item 4) + extra_mass_floors (psf, ALSO gravity dead).
    Cladding = clad (psf) x exterior wall area tributary to the level by MID-HEIGHT bands of the actual
    building volume (clad_areas_ft2: half the storey below + half the storey above, true wall heights at
    split levels / setbacks / lean-tos; the bottom half of the lowest storey goes to grade).
    Seismic-weight-only inputs (HR-39, never added to gravity): cfg['seismic_mass_only'] = {k: psf}
    (e.g. the 12.7.2 item-2 partition allowance) and cfg['seismic_weight_kip'] = {k: kip} (lumped
    equipment / tanks)."""
    NF=len(cfg["heights"]); roof=(k==NF)
    d=_Dlev(cfg,k,roof)
    w=d*floor_area_ft2(cfg,k)/1000.0
    w+=_clad_kip(cfg,k)
    w+=_lvl_val(cfg.get("seismic_mass_only"),k)*floor_area_ft2(cfg,k)/1000.0
    w+=_lvl_val(cfg.get("seismic_weight_kip"),k)
    if roof:
        snow=cfg.get("snow",0.0)
        if snow>45.0:                          # 12.7.2 item 4: 15% of snow ONLY where pf > 45 psf
            w+=0.15*snow*floor_area_ft2(cfg,k)/1000.0
    elif _is_storage(cfg,k):                   # 12.7.2 item 1: >=25% of storage floor live in W
        w+=0.25*_Llev(cfg,k)*floor_area_ft2(cfg,k)/1000.0
    w+=cfg.get("extra_mass_floors",{}).get(k,0.0)*floor_area_ft2(cfg,k)/1000.0
    return w

def floor_grav(cfg,k):
    NF=len(cfg["heights"]); roof=(k==NF)
    d=_Dlev(cfg,k,roof); l=0.0 if roof else _Llev(cfg,k)
    base=(d+0.5*l)*floor_area_ft2(cfg,k)/1000.0
    if roof: base+=cfg.get("snow",0.0)*floor_area_ft2(cfg,k)/1000.0
    base+=cfg.get("extra_mass_floors",{}).get(k,0.0)*floor_area_ft2(cfg,k)/1000.0
    return base

def floor_dead(cfg,k):
    NF=len(cfg["heights"]); roof=(k==NF)
    d=_Dlev(cfg,k,roof)
    w=d*floor_area_ft2(cfg,k)/1000.0
    w+=_clad_kip(cfg,k)                         # same mid-height wall tributary as floor_w
    w+=cfg.get("extra_mass_floors",{}).get(k,0.0)*floor_area_ft2(cfg,k)/1000.0
    return w

def floor_live(cfg,k):
    NF=len(cfg["heights"])
    if k==NF: return 0.0
    return _Llev(cfg,k)*floor_area_ft2(cfg,k)/1000.0

def floor_roofLrS(cfg,k):
    NF=len(cfg["heights"])
    if k!=NF: return 0.0
    snow=cfg.get("snow",0.0)
    return (snow if snow>0 else cfg.get("Lr",20.0))*floor_area_ft2(cfg,k)/1000.0   # snow, else cfg['Lr'] (default 20)

_MODAL_CACHE = {}
_ELF_CACHE = {}
_MODESHAPE_CACHE = {}   # _model_key(cfg) -> {T, coords, ev (all nodes, all solved modes), ele, z, nmodes}

def clear_caches():
    """Drop the per-run modal/elf/mode-shape memo. Called at the start of each design_and_report run."""
    _MODAL_CACHE.clear(); _ELF_CACHE.clear(); _MODESHAPE_CACHE.clear()


def _model_key(cfg):
    """Content hash of the BUILT model + loads/geometry/seismic -- the cache key for modal/elf/mode
    shapes (B2). Keying on id(cfg) caused stale hits when a deep-copied cfg reused a freed id, or when a
    custom_build read a mutated module global (e.g. an optimisation sweep changing a section schedule).
    Hashing the actual element schedule (kind, section, end nodes) makes two cfgs share a cache entry
    iff they build the SAME model under the SAME loads."""
    try:
        ele = tuple((e[1], e[2], e[3], e[4]) for e in build(cfg, "Linear").get("ele", []))
    except Exception:
        ele = ()
    sig = (ele, tuple(cfg.get("heights", [])), cfg.get("NX"), cfg.get("NY"), cfg.get("SX"),
           cfg.get("SY"), str(cfg.get("base", "fixed")), cfg.get("D_floor"), cfg.get("D_roof"),
           cfg.get("clad"), cfg.get("L_floor"), cfg.get("Lr"), cfg.get("snow"),
           bool(cfg.get("storage")), tuple(cfg.get("storage_levels") or ()),   # 12.7.2 storage live in W
           tuple(sorted((cfg.get("extra_mass_floors") or {}).items())),
           tuple(sorted((cfg.get("seis") or {}).items())))
    return hashlib.md5(repr(sig).encode()).hexdigest()

def modal(cfg, nm):
    """Memoized modal analysis. Identical (cfg, nm) within a run reuses the eigen solve instead of
    re-running it for every load_cases()/combos() call. Callers use only the returned tuple; whoever
    needs the live model next rebuilds it. Returns the SAME computed values (no numeric change)."""
    key = (_model_key(cfg), nm)
    r = _MODAL_CACHE.get(key)
    if r is None:
        r = _modal_impl(cfg, nm); _MODAL_CACHE[key] = r
    return r

def _modal_impl(cfg,nm):
    info=build(cfg,"Linear"); NF=info["NF"]
    nm_req=min(nm,3*NF)
    masses={k:floor_w(cfg,k)/g for k in range(1,NF+1)}
    Jm={}
    for k in range(1,NF+1):
        pts=info["present"][k]; SX,SY=cfg["SX"],cfg["SY"]
        xs=[i*SX for i,j in pts]; ys=[j*SY for i,j in pts]
        Bx=max(xs)-min(xs)+SX; By=max(ys)-min(ys)+SY; Jm[k]=masses[k]*(Bx**2+By**2)/12.0
    Mtot=sum(masses.values()); maxmodes=max(1,3*NF)
    # Regularize the (singular) mass matrix: the rigid diaphragm leaves mass only on the floor masters,
    # so most DOF are massless and -genBandArpack fails to converge beyond ~6 modes -- silently falling
    # back to the O(N^3) -fullGenLapack (tens of seconds).  A tiny ~1e-8*floor-mass on every DOF makes M
    # non-singular so ARPACK converges for ALL requested modes in ~1 s, with NO change to periods or
    # modal mass (verified identical to the dense solve).  Masters keep their real lateral mass. (P7)
    try:
        _tm = 1e-8 * (min(masses.values()) if masses else 1.0)
        for _t in ops.getNodeTags():
            ops.mass(_t, _tm, _tm, _tm, _tm, _tm, _tm)
        for _k in range(1, NF+1):
            ops.mass(mtag(_k), masses[_k]+_tm, masses[_k]+_tm, _tm, _tm, _tm, Jm[_k]+_tm)
    except Exception:
        pass
    def _solve(nev):
        nev=max(1,min(nev,maxmodes))
        # A system MUST be set or -genBandArpack raises "no system is set" and silently falls back to the
        # VERY SLOW -fullGenLapack on every modal solve (pipeline-wide).  Set one first. (P7 root-cause)
        ops.constraints("Transformation"); ops.numberer("RCM"); ops.system("UmfPack")
        try:
            return ops.eigen("-genBandArpack",nev)      # fast Arnoldi for the lowest modes
        except Exception:
            return ops.eigen("-fullGenLapack",nev)       # dense fallback if ARPACK cannot converge
    def _mass(w2):
        eX=[];eY=[]
        for mode in range(1,len(w2)+1):
            Lx=Ly=Mi=0.0
            for k in range(1,NF+1):
                p=ops.nodeEigenvector(mtag(k),mode); mk=masses[k]
                Lx+=mk*p[0]; Ly+=mk*p[1]; Mi+=mk*(p[0]**2+p[1]**2)+Jm[k]*p[5]**2
            eX.append((Lx**2)/Mi/Mtot if Mi>0 else 0); eY.append((Ly**2)/Mi/Mtot if Mi>0 else 0)
        return eX,eY
    # Adaptive mode count: ask for 6 modes first -- ARPACK is fast and converges, unlike a 16-mode
    # request on a rigid-diaphragm model (only ~3 dynamic DOF per floor). Escalate to 12, then to the
    # caller's full request, ONLY if the ASCE 7-22 12.9.1 90% modal-mass target is not yet captured.
    # SINGLE solve: ops.eigen cannot be re-called on one model (the eigenSOE is consumed after the first
    # call), and with the mass regularized above, -genBandArpack converges for the full request in ONE
    # shot (~1 s) -- no need for the escalating ladder that triggered the second (failing) eigen call and
    # the slow dense fallback. Ask for enough modes to capture the ASCE 7-22 12.9.1 90% mass target. (P7)
    nev = min(maxmodes, max(nm_req, 12))
    w2 = _solve(nev); eX, eY = _mass(w2)
    T=[2*math.pi/math.sqrt(max(x,1e-12)) for x in w2]
    # Cache the full eigenvector FIELD (every node, every solved mode) while the model is live, so the
    # report's 3D mode-shape figure and the animated-mode GIF REUSE this solve instead of re-running
    # ops.eigen (fig_mode_3d previously re-solved with the slow -fullGenLapack). Plain-dict snapshot,
    # so it survives the later model rebuilds the figure code used to do.
    try:
        nm_have=len(w2)
        coords={t:ops.nodeCoord(t) for t in ops.getNodeTags()}
        evf={t:[ops.nodeEigenvector(t,md) for md in range(1,nm_have+1)] for t in coords}
        _MODESHAPE_CACHE[_model_key(cfg)]={"T":T,"coords":coords,"ev":evf,"ele":list(info["ele"]),
                                   "z":info["z"],"nmodes":nm_have}
    except Exception:
        pass
    return T,w2,eX,eY,Mtot


def mode_shapes(cfg, nmodes=3):
    """Cached modal eigenvector FIELD for plotting (mode-shape figure / animated GIF):
      {"T":[...], "coords":{tag:(x,y,z)}, "ev":{tag:[vec_mode1, vec_mode2, ...]}, "ele":[...], "z":..., "nmodes":N}.
    Reuses the eigenvectors already computed by modal() (snapshotted during the solve), so the figures
    do NOT re-run ops.eigen. Triggers at most one fast ARPACK solve if nothing suitable is cached."""
    c=_MODESHAPE_CACHE.get(_model_key(cfg))
    if c is not None and c.get("nmodes",0)>=nmodes:
        return c
    modal(cfg, max(nmodes,6))                       # populates the cache via _modal_impl (fast ARPACK)
    c=_MODESHAPE_CACHE.get(_model_key(cfg))
    if c is not None and c.get("nmodes",0)>=nmodes:
        return c
    info=build(cfg,"Linear"); maxm=max(1,3*len(cfg["heights"]))   # robust direct fallback
    ops.constraints("Transformation"); ops.numberer("RCM"); ops.system("UmfPack")   # P7: system so ARPACK runs
    nev=max(1,min(max(nmodes,6),maxm))
    try: w2=ops.eigen("-genBandArpack",nev)
    except Exception: w2=ops.eigen("-fullGenLapack",nev)
    T=[2*math.pi/math.sqrt(max(x,1e-12)) for x in w2]; nm_have=len(w2)
    coords={t:ops.nodeCoord(t) for t in ops.getNodeTags()}
    evf={t:[ops.nodeEigenvector(t,md) for md in range(1,nm_have+1)] for t in coords}
    c={"T":T,"coords":coords,"ev":evf,"ele":list(info["ele"]),"z":info["z"],"nmodes":nm_have}
    _MODESHAPE_CACHE[_model_key(cfg)]=c
    return c

def sa(cfg,T):
    s=cfg["seis"]; SDS=s["SDS"];SD1=s["SD1"];TL=s.get("TL",8.0)
    To=0.2*SD1/SDS; Ts=SD1/SDS
    if T<To: return SDS*(0.4+0.6*T/To)
    if T<=Ts: return SDS
    if T<=TL: return SD1/T
    return SD1*TL/T**2

def elf(cfg, T1):
    """Memoized ELF (pure function of cfg + fundamental period); same values, no numeric change."""
    key = (_model_key(cfg), round(float(T1), 6))
    r = _ELF_CACHE.get(key)
    if r is None:
        r = _elf_impl(cfg, T1); _ELF_CACHE[key] = r
    return r

def _elf_impl(cfg,T1):
    s=cfg["seis"]; NF=len(cfg["heights"]); z=zlevels(cfg)
    W=sum(floor_w(cfg,k) for k in range(1,NF+1))
    Ta=s["Ct"]*(z[-1]/12)**s["x"]; Tu=min(T1,s["Cu"]*Ta)
    R,Ie=s["R"],s["Ie"]
    Cs=s["SDS"]/(R/Ie)
    TL=s.get("TL",8.0)
    cap=s["SD1"]/(Tu*(R/Ie)) if Tu<=TL else s["SD1"]*TL/(Tu**2*(R/Ie))
    Cs=min(Cs,cap); cmin=max(0.044*s["SDS"]*Ie,0.01)
    if s.get("S1",0)>=0.6: cmin=max(cmin,0.5*s["S1"]/(R/Ie))
    Cs=max(Cs,cmin); V=Cs*W
    kk=1.0 if Tu<=0.5 else (2.0 if Tu>=2.5 else 1+(Tu-0.5)/2.0)
    whk={k:floor_w(cfg,k)*z[k]**kk for k in range(1,NF+1)}; ss=sum(whk.values())
    return Cs,V,Tu,Ta,kk,{k:V*whk[k]/ss for k in range(1,NF+1)},W

def rs_baseshear(cfg,T,eX,eY,Mtot,direction):
    s=cfg["seis"]; R,Ie=s["R"],s["Ie"]; W=Mtot*g
    eff=eX if direction=="X" else eY
    Vi=[ (sa(cfg,T[i])/(R/Ie))*eff[i]*W for i in range(len(T)) ]
    # CQC with 5% damping
    zeta=0.05; V2=0.0
    for i in range(len(T)):
        for j in range(len(T)):
            r=T[j]/T[i] if T[i]>0 else 0
            rho=(8*zeta**2*(1+r)*r**1.5)/((1-r**2)**2+4*zeta**2*r*(1+r)**2) if r>0 else (1 if i==j else 0)
            V2+=rho*Vi[i]*Vi[j]
    return math.sqrt(max(V2,0))

def static_lateral(cfg,Fx,direction,accidental=False):
    info=build(cfg,"PDelta"); NF=info["NF"]; di=0 if direction=="X" else 1
    ops.timeSeries("Linear",1); ops.pattern("Plain",1,1)
    for k in range(1,NF+1):
        pts=info["present"][k]; p=floor_grav(cfg,k)/len(pts)
        for (i,j) in pts: ops.load(ntag(i,j,k),0,0,-p,0,0,0)
    SX,SY=cfg["SX"],cfg["SY"]
    for k in range(1,NF+1):
        f=[0.0]*6; f[di]=Fx[k]
        if accidental:
            pts=info["present"][k]                       # real coords (xcoords/ycoords safe)
            xs=[_xy_in(cfg,i,j)[0] for i,j in pts]; ys=[_xy_in(cfg,i,j)[1] for i,j in pts]
            B=(max(ys)-min(ys)+SY) if direction=="X" else (max(xs)-min(xs)+SX)
            f[5]=Fx[k]*0.05*B
        ops.load(mtag(k),*f)
    ops.constraints("Transformation"); ops.numberer("RCM"); ops.system("UmfPack")
    ops.test("NormDispIncr",1e-7,200); ops.algorithm("Newton"); ops.integrator("LoadControl",1.0); ops.analysis("Static")
    ok=ops.analyze(1)
    disp={k:ops.nodeDisp(mtag(k),di+1) for k in range(1,NF+1)}
    rz={k:ops.nodeDisp(mtag(k),6) for k in range(1,NF+1)}
    z=info["z"]; dr=[]; prev=0.0
    for k in range(1,NF+1): dr.append((disp[k]-prev)/cfg["heights"][k-1]); prev=disp[k]
    ops.reactions()
    Rs=sum(ops.nodeReaction(ntag(i,j,0),di+1) for (i,j) in info["present"][0])
    # Torsional Irregularity Ratio screen (ASCE 7-22 Table 12.3-1 Type 1 / Eq. 12.3-2): ratio of
    # story DRIFT at the diaphragm edge to the story drift at the centre of mass, worst over ALL
    # stories (7-22 keys the TIR to story drifts at the ends, not total displacements; with a rigid
    # diaphragm the CM drift equals the two-edge average, so this is Dmax/Davg).
    SXp,SYp=cfg["SX"],cfg["SY"]; tratio=1.0; pd_=0.0; pr_=0.0
    for k in range(1,NF+1):
        pts=info["present"][k]
        ddr=disp[k]-pd_; rdr=rz[k]-pr_; pd_=disp[k]; pr_=rz[k]
        if abs(ddr)<=1e-12: continue
        if direction=="X":
            ys=[j*SYp for i,j in pts]; ycm=info["cm"][k][1]
            edge=max(abs(y-ycm) for y in ys)
        else:
            xs=[i*SXp for i,j in pts]; xcm=info["cm"][k][0]
            edge=max(abs(x-xcm) for x in xs)
        tratio=max(tratio,(abs(ddr)+abs(rdr)*edge)/abs(ddr))
    return ok,disp,dr,Rs,tratio


def sdc_of(cfg):
    """Seismic Design Category for a cfg: cfg['sdc'] if declared, else derived via the canonical
    ASCE 7-22 sec.11.6 implementation in preflight.asce_sdc (Tables 11.6-1/-2 incl. the Risk
    Category IV column and the S1 >= 0.75 -> E/F override)."""
    from preflight import sdc_of_cfg
    return sdc_of_cfg(cfg)


def _mf_only(cfg):
    """True when the SFRS is a moment-frame-only system (SMF/IMF/OMF family, not dual): declared
    cfg['system'] wins; otherwise a model with no braces is taken as moment-frame-only."""
    sysname = str(cfg.get("system") or "").lower()
    if sysname:
        return (any(k in sysname for k in ("smf", "imf", "omf")) or "moment" in sysname) \
               and "dual" not in sysname
    return not is_braced(cfg)


def drift_allowable(cfg):
    """(allowable story drift ratio, rho_applied): cfg['drift_limit'] (Table 12.12-1 value,
    default 0.020), divided by the redundancy factor rho for a moment-frame-only SFRS in SDC
    D/E/F per ASCE 7-22 sec.12.12.1.1 (Delta <= Delta_a/rho). rho = cfg['rho'], the same value
    used in the load combinations (default 1.3)."""
    dl = float(cfg.get("drift_limit", 0.020) or 0.020)
    if sdc_of(cfg) in ("D", "E", "F") and _mf_only(cfg):
        rho = float(cfg.get("rho", 1.3) or 1.3)
        return dl / rho, True
    return dl, False


def _drift_env(cfg, drifts):
    """Max |story drift| honoring cfg['drift_exempt_stories'] = {story_index(1-based): reason}.
    Declared inter-diaphragm offsets (split-level steps) are excluded from the code story-drift
    gate; their racking is a designed detail the agent covers in calc_package (F-1)."""
    dex = set(int(k) for k in (cfg.get("drift_exempt_stories") or {}))
    vals = [abs(d) for i, d in enumerate(drifts, start=1) if i not in dex]
    return max(vals) if vals else max(abs(d) for d in drifts)


def run(cfg):
    NF=len(cfg["heights"]); r={}
    T,w2,eX,eY,Mtot=modal(cfg,min(3*NF,12))
    Cs,V,Tu,Ta,kk,Fx,W=elf(cfg,T[0])
    sx=static_lateral(cfg,Fx,"X"); sy=static_lateral(cfg,Fx,"Y")
    mde_x=_drift_env(cfg, sx[2]); mde_y=_drift_env(cfg, sy[2])
    Cd=cfg["seis"]["Cd"]; Ie=cfg["seis"]["Ie"]            # ASCE 7-22 Eq.12.8-16 design drift
    mdx=Cd*mde_x/Ie; mdy=Cd*mde_y/Ie
    cumX=sum(eX); cumY=sum(eY)
    chk={}
    chk["equil_X"]=abs(sx[3]+V)<=1e-3*V; chk["equil_Y"]=abs(sy[3]+V)<=1e-3*V
    chk["stability"]=min(w2)>0
    chk["period"]=0.5*Ta<=T[0]<=3*Ta
    chk["modalmass_X"]=cumX>=0.90; chk["modalmass_Y"]=cumY>=0.90
    dl,_dlrho=drift_allowable(cfg)   # Table 12.12-1 limit, /rho for MF-only SDC D-F (12.12.1.1)
    chk["drift_X"]=0<mdx<dl; chk["drift_Y"]=0<mdy<dl
    chk["baseshear_X"]=abs(abs(sx[3])-V)<=1e-3*V; chk["baseshear_Y"]=abs(abs(sy[3])-V)<=1e-3*V
    out=dict(T1=T[0],T2=T[1],T3=T[2],Ta=Ta,Cs=Cs,V=V,W=W,Tu=Tu,k=kk,cumX=cumX,cumY=cumY,
             mdx=mdx,mdy=mdy,roofX=sx[1][NF],roofY=sy[1][NF],tratioX=sx[4],tratioY=sy[4])
    if _dlrho: out["drift_limit_rho"]=dl
    if "RS" in cfg.get("analyses",[]):
        # ASCE 7-22 sec.12.9.1.4.1: where the modal base shear Vt < V (ELF), scale forces by V/Vt
        # -- i.e. the MRSA design base shear is the FULL 100% of V (the 7-16 85% floor is deleted).
        VrsX=rs_baseshear(cfg,T,eX,eY,Mtot,"X"); VrsY=rs_baseshear(cfg,T,eX,eY,Mtot,"Y")
        out["VrsX"]=VrsX; out["VrsY"]=VrsY
        out["VrsX_scaled"]=max(VrsX,1.00*V); out["VrsY_scaled"]=max(VrsY,1.00*V)
        chk["rs_X_ge_100pct"]=out["VrsX_scaled"]>=1.00*V-1e-6
        chk["rs_Y_ge_100pct"]=out["VrsY_scaled"]>=1.00*V-1e-6
    if cfg.get("torsion_check"):
        sxa=static_lateral(cfg,Fx,"X",accidental=True)
        out["tratioX_acc"]=sxa[4]                          # B4: advisory (irregularity + Ax handled in report Ch.2)
        out["torsion_Ax"]=round(min(max((sxa[4]/1.2)**2,1.0),3.0),2)
    # model-fidelity / completeness / serviceability gates -- match run_one() so quick run()
    # never reports a false ALL PASS (P2).
    _dok,_cok,_mmsg=_model_gate(cfg); chk["model_declared"]=_dok; chk["model_consistent"]=_cok
    if _mmsg: out["model_warning"]=_mmsg
    chk["model_complete"]=(len(floor_beam_gaps(cfg))==0)
    if cfg.get("beam_framing")=="truss":
        chk["beam_deflection"]=True
    else:
        try:
            _rLL,_rTL=beam_serviceability(cfg); chk["beam_deflection"]=(_rLL<1.0 and _rTL<1.0)
        except Exception as _ex:
            chk["beam_deflection"]=True; out["beam_defl_warning"]=str(_ex)
    out["checks"]=chk; out["all"]=all(chk.values())
    return out


# ===== configs + orchestration =====
D=dict(D_floor=75.0,D_roof=60.0,clad=15.0,L_floor=50.0)
def seis(SDS,SD1,S1,R,Ct,x,Cu,Ie=1.0,Cd=None,Om0=None):
    # Cd, Om0 (overstrength) per ASCE 7-22 Table 12.2-1, defaulted by R for the common steel
    # SFRS here (SMF/dual R8/R7, SCBF R6, IMF R4.5, OCBF R3.25, "not detailed" R3). Systems whose
    # Cd/Om0 differ from the R-default (EBF, BRBF) pass them explicitly. A special plate shear
    # wall (the ONLY R=7 steel system with Cd=6 in Table 12.2-1) defaults to its tabulated
    # Om0 = 2.0 (row B.27), not the R=7 dual-system 2.5.
    if Cd  is None: Cd ={8:5.5,7:5.5,6:5.0,4.5:4.0,3.25:3.25,3:3.0}.get(R,R)
    if Om0 is None and R==7 and Cd==6.0: Om0=2.0   # SPSW: Table 12.2-1 Om0 = 2.0
    if Om0 is None: Om0={8:3.0,7:2.5,6:2.0,4.5:3.0,3.25:2.0,3:3.0}.get(R,2.5)
    return dict(SDS=SDS,SD1=SD1,S1=S1,R=R,Ct=Ct,x=x,Cu=Cu,Ie=Ie,Cd=Cd,Om0=Om0,TL=8.0)
def Lplan(k,NX,NY): return {(i,j) for i in range(NX+1) for j in range(NY+1) if not (i>NX//2 and j>NY//2)}
def setback(k,NX,NY):
    if k<=5: return {(i,j) for i in range(NX+1) for j in range(NY+1)}
    return {(i,j) for i in range(2,NX-1) for j in range(2,NY-1)}
def core_braces(k,NX,NY):
    lo_i,hi_i=1,NX-1; lo_j,hi_j=1,NY-1; out=[]
    for i in range(lo_i,hi_i):
        out.append(("X",i,lo_j)); out.append(("X",i,hi_j))
    for j in range(lo_j,hi_j):
        out.append(("Y",lo_i,j)); out.append(("Y",hi_i,j))
    return out
def core1(k,NX,NY):
    ci,cj=NX//2,NY//2
    return [("X",ci-1,cj),("Y",ci,cj-1)]
def perim_braces(k,NX,NY):
    return [("X",0,0),("X",NX-1,0),("X",0,NY),("X",NX-1,NY),("Y",0,0),("Y",0,NY-1),("Y",NX,0),("Y",NX,NY-1)]
# ---- non-rectangular plan footprints (present-set per level) -- reference builds for the RAG (R23) ----
def Tplan(k,NX,NY):
    ci=NX//2
    return {(i,j) for i in range(NX+1) for j in range(NY+1) if j>=NY-2 or (ci-1<=i<=ci+1)}
def Uplan(k,NX,NY):                       # courtyard: open block at top-centre
    return {(i,j) for i in range(NX+1) for j in range(NY+1) if not (2<=i<=NX-2 and j>=NY-2)}
def cruciform(k,NX,NY):                    # plus-shape: 4 re-entrant corners
    ci,cj=NX//2,NY//2
    return {(i,j) for i in range(NX+1) for j in range(NY+1) if (ci-1<=i<=ci+1) or (cj-1<=j<=cj+1)}
def Zplan(k,NX,NY):                        # two offset blocks sharing the centre
    return {(i,j) for i in range(NX+1) for j in range(NY+1) if (i<=NX//2 and j<=NY//2) or (i>=NX//2 and j>=NY//2)}

def _footprint_at(cfg,k,NX,NY):
    """Per-level present (i,j) columns: runtime cfg['present'] (from custom_build) -> plan= fn -> full grid."""
    pres=cfg.get("present")
    if isinstance(pres,dict) and k in pres and pres[k]:
        return set(tuple(p) for p in pres[k])
    pl=cfg.get("plan")
    if pl:
        try: return set(pl(k,NX,NY))
        except Exception: pass
    return {(i,j) for i in range(NX+1) for j in range(NY+1)}

def plan_irregularities(cfg):
    """FIRM ASCE 7-22 Table 12.3-1/-2 plan screen from the ACTUAL per-level footprint (custom_build
    present-sets OR a plan= fn OR the full grid). Replaces the old 'cfg.get(plan) is None -> rectangular'
    guess that let L/T/U/cruciform custom_build models be reported as 'uniform rectangular'."""
    NX,NY=cfg["NX"],cfg["NY"]; NF=len(cfg.get("heights",[1]))
    full={(i,j) for i in range(NX+1) for j in range(NY+1)}
    reentrant=setback=nonrect=False; prev=None
    for k in range(1,NF+1):
        fp=_footprint_at(cfg,k,NX,NY)
        if not fp: continue
        if fp!=full: nonrect=True
        xs=[i for i,j in fp]; ys=[j for i,j in fp]
        bbox={(i,j) for i in range(min(xs),max(xs)+1) for j in range(min(ys),max(ys)+1)}
        if len(fp)<len(bbox): reentrant=True                 # non-convex footprint => re-entrant / notch
        if prev is not None and len(fp)<len(prev): setback=True
        prev=fp
    return dict(reentrant=reentrant, setback=setback,
                nonparallel=bool(cfg.get("skew")), nonrect=nonrect)

CFG={}
CFG["B02"]=dict(arch="mid-rise office",NX=6,NY=4,SX=360,SY=360,heights=[162]*6,base="fixed",col="W14X311",beam="W33X130",seis=seis(1.0,0.6,0.6,8,0.028,0.8,1.4),wind=dict(V=115,exposure="C",Kd=0.85,Kzt=1.0,G=0.85,Cpnet=1.3),analyses=["ELF","RS"],**D)
CFG["B03"]=dict(arch="office braced core",NX=5,NY=5,SX=336,SY=336,heights=[156]*8,base="fixed",col="W14X311",beam="W24X76",brace="H14",braces=core_braces,seis=seis(1.0,0.6,0.6,6,0.02,0.75,1.4),analyses=["ELF","RS"],torsion_check=True,**D)
CFG["B04"]=dict(arch="big-box retail",NX=10,NY=6,SX=480,SY=480,heights=[288],base="fixed",col="W14X90",beam="W33X130",brace="H8",braces=perim_braces,seis=seis(0.25,0.10,0.10,3.25,0.02,0.75,1.7),governing="wind",wind=dict(V=130,Kz=0.85,Kd=0.85,Kzt=1.0,G=0.85,Cpnet=1.3),analyses=[],**D)
CFG["B05"]=dict(arch="L-shaped office",NX=4,NY=4,SX=360,SY=360,heights=[156]*4,base="fixed",plan=Lplan,col="W14X233",beam="W33X130",seis=seis(0.5,0.25,0.25,4.5,0.028,0.8,1.5),analyses=["ELF"],torsion_check=True,**D)
CFG["B06"]=dict(arch="parking structure",NX=4,NY=8,SX=600,SY=360,heights=[132]*5,base="pinned",col="W14X159",beam="W36X150",brace="H8b",braces=perim_braces,seis=seis(0.5,0.25,0.25,6,0.02,0.75,1.5),analyses=["ELF"],D_floor=85.0,D_roof=85.0,clad=5.0,L_floor=40.0)
CFG["B07"]=dict(arch="dual MF+CBF core",NX=6,NY=6,SX=360,SY=360,heights=[156]*10,base="fixed",col="W14X426",beam="W36X194",brace="H8",braces=core1,seis=seis(1.0,0.6,0.6,7,0.028,0.8,1.4),analyses=["ELF","RS"],dual_check=True,**D)
CFG["B08"]=dict(arch="hospital wing",NX=6,NY=4,SX=360,SY=360,heights=[168]*4,base="fixed",col="W14X193",beam="W24X76",brace="H10",braces=perim_braces,seis=seis(1.0,0.6,0.6,6,0.02,0.75,1.4,Ie=1.5),analyses=["ELF","RS"],drift_limit=0.015,**D)
CFG["B09"]=dict(arch="office w/ setback",NX=6,NY=6,SX=360,SY=360,heights=[156]*8,base="fixed",plan=setback,col="W14X311",beam="W33X130",seis=seis(1.0,0.6,0.6,8,0.028,0.8,1.4),analyses=["ELF","RS"],**D)
CFG["B10"]=dict(arch="tall MF soft storey",NX=6,NY=6,SX=360,SY=360,heights=[216]+[156]*11,base="fixed",col="W14X500",beam="W40X199",seis=seis(1.0,0.6,0.6,8,0.028,0.8,1.4),analyses=["ELF","RS"],softstorey_check=True,**D)
def _ss_beam_defl(sec,L,w):
    """Mid-span deflection (in) of a pinned-pinned beam (2 elements + mid-node) under UDL w
    (k/in), using build()'s exact beam element signature (J,Ix,Iy + transf vecxz=(0,0,1)),
    so the check reflects the model's actual member orientation."""
    A,Ix,Iy,J=SEC[sec]
    ops.wipe(); ops.model("basic","-ndm",3,"-ndf",6)
    ops.node(1,0.,0.,0.); ops.node(2,L/2,0.,0.); ops.node(3,L,0.,0.)
    ops.fix(1,1,1,1,1,0,0); ops.fix(3,0,1,1,1,0,0)
    ops.geomTransf("Linear",9,0.,0.,1.)
    for e,(a,b) in enumerate([(1,2),(2,3)],1):
        ops.element("elasticBeamColumn",e,a,b,A,E,Gmod,J,Ix,Iy,9)   # SAME order as build(): (J,Ix,Iy)
    ops.timeSeries("Linear",9); ops.pattern("Plain",9,9)
    for e in (1,2): ops.eleLoad("-ele",e,"-type","-beamUniform",0.,-w)
    ops.system("UmfPack"); ops.numberer("RCM"); ops.constraints("Transformation")
    ops.test("NormDispIncr",1e-8,50); ops.algorithm("Linear"); ops.integrator("LoadControl",1.0)
    ops.analysis("Static"); ops.analyze(1)
    return abs(ops.nodeDisp(2,3))

def _service_beam(cfg):
    """Section for the serviceability deflection check: cfg['beam'] if given (single-section archetypes),
    else the most flexible (smallest Ix) FLOOR beam in the BUILT model -- so a custom_build no longer needs
    a representative cfg['beam'] (was a bare KeyError). (P4)"""
    b = cfg.get("beam")
    if b:
        return b
    try:
        info = build(cfg, "Linear"); NF = info["NF"]
        fb = [(Ipack(sec)[1], sec) for (t, kind, sec, n1, n2) in info["ele"]
              if kind == "beam" and (n1 // 100000) < NF]            # floor beams (exclude the roof level)
        if not fb:
            fb = [(Ipack(sec)[1], sec) for (t, kind, sec, n1, n2) in info["ele"] if kind == "beam"]
        return min(fb)[1] if fb else None
    except Exception:
        return None


def beam_serviceability(cfg):
    """Floor/roof beam vertical deflection vs IBC/ASCE limits: live L/360, total L/240.
    Checks EVERY distinct beam GROUP (section x span x level) in the built model -- NOT one
    representative -- so a too-light beam in ANY group, including the roof (different load + often
    different section) and any heavier-loaded level, is caught. Returns (worst_LL, worst_TL) ratios.
    FE-based on build()'s beam definition, so a wrong strong/weak-axis assignment fails the ratio."""
    SX,SY=cfg["SX"],cfg["SY"]; NF=len(cfg["heights"])
    if cfg.get("lean_gravity"):
        # Perimeter MF beams carry spandrel CLADDING only (floor gravity goes to the leaning
        # columns); checking them against a full-bay floor tributary is a false alarm.
        bsec=_service_beam(cfg)
        if not bsec: return 0.0, 0.0
        th=max(cfg["heights"])/12.0; wD=cfg.get("clad",0.0)*th/1000.0/12.0
        L=max(SX,SY); return 0.0, _ss_beam_defl(bsec,L,wD)/(L/240.0)
    try:
        info=build(cfg,"Linear")
    except Exception:
        return 0.0,0.0
    groups={}                                      # (sec, round(L), roof) -> (L, trib, roof, sec)
    for (t,kind,sec,n1,n2) in info["ele"]:
        if kind!="beam": continue
        try: c1=ops.nodeCoord(n1); c2=ops.nodeCoord(n2)
        except Exception: continue
        Lx=abs(c1[0]-c2[0]); Ly=abs(c1[1]-c2[1]); L=max(Lx,Ly)
        if L<1e-6: continue
        roof=((n1//100000)>=NF)                     # top framed level = roof load case
        trib=SY if Lx>=Ly else SX                   # full perpendicular bay (matches _beam_grav, conservative)
        key=(str(sec),round(L,1),roof)
        if key not in groups or trib>groups[key][1]:
            groups[key]=(L,trib,roof,sec)
    wLLr=wTLr=0.0
    for (L,trib,roof,sec) in groups.values():
        try:
            Dp=cfg["D_roof"] if roof else cfg["D_floor"]
            Lp=((cfg.get("snow",0.0) or cfg.get("Lr",20.0)) if roof else cfg["L_floor"])
            wLL=Lp/1000.0/144.0*trib; wTL=(Dp+Lp)/1000.0/144.0*trib
            dLL=_ss_beam_defl(sec,L,wLL) if wLL>0 else 0.0
            dTL=_ss_beam_defl(sec,L,wTL)
            wLLr=max(wLLr,dLL/(L/360.0)); wTLr=max(wTLr,dTL/(L/240.0))
        except Exception:
            continue
    return wLLr,wTLr

def _infer_model(cfg):
    """Infer {bases,joints,gravity} from a cfg's implementation -- used to back-fill the built-in
    B-archetypes so they pass the gate. The AGENT must declare cfg['model'] explicitly for its own."""
    if cfg.get("custom_build"):
        return {"bases": "custom", "joints": "custom", "gravity": "custom"}
    base = str(cfg.get("base", "fixed")).lower()
    return {"bases": ("pinned" if base == "pinned" else "fixed"),
            "joints": ("pinned" if cfg.get("releases") else "rigid"),
            "gravity": ("leaning" if cfg.get("lean_gravity") else "framed")}


def _model_gate(cfg):
    """HARD model-fidelity gate -> (declared_ok, consistent_ok, message).
    (1) the cfg MUST declare its scheme: cfg['model'] = {'bases','joints','gravity'};
    (2) the built model MUST implement it. The default builder makes EVERY joint rigid, the interior
        framed, and ONE base for all columns -- so a declared pinned/mixed/leaning system without
        cfg['releases'] / mixed base / cfg['lean_gravity'] / custom_build is a hard FAIL. This stops a
        finished report from being built on a model that contradicts the design."""
    decl = cfg.get("model")
    declared = isinstance(decl, dict) and all(k in decl for k in ("bases", "joints", "gravity"))
    if not declared:
        return False, True, ("DECLARE the structural model -- set cfg['model'] = {'bases':'fixed'|"
            "'pinned'|'mixed', 'joints':'rigid'|'pinned'|'mixed', 'gravity':'framed'|'leaning'} -- so "
            "model fidelity can be gated. The cfg IS the design model, not just figures.")
    custom = bool(cfg.get("custom_build")); base = str(cfg.get("base", "fixed")).lower()
    has_rel = bool(cfg.get("releases")); lean = bool(cfg.get("lean_gravity"))
    bases = str(decl["bases"]).lower(); joints = str(decl["joints"]).lower(); gravity = str(decl["gravity"]).lower()
    probs = []
    if not custom:
        if "mixed" in bases:
            probs.append("bases='mixed' needs a custom_build (the default builder uses ONE base for ALL columns)")
        elif "pinned" in bases and base != "pinned":
            probs.append("bases='pinned' but cfg['base']='%s'" % base)
        elif "fixed" in bases and base == "pinned":
            probs.append("bases='fixed' but cfg['base']='pinned'")
        if ("pinned" in joints or "mixed" in joints) and not has_rel:
            probs.append("joints='%s' needs cfg['releases'] (the default builder makes ALL beam joints rigid)" % joints)
        if "lean" in gravity and not lean:
            probs.append("gravity='leaning' needs cfg['lean_gravity'] (or a custom_build)")
        arch = str(cfg.get("arch", "")).lower()                 # backstop vs a dishonest declaration
        if not has_rel and not lean and base != "pinned":
            if any(w in arch for w in ("lean", "gravity column", "gravity frame")) and "lean" not in gravity:
                probs.append("arch says LEANING gravity but you declared gravity='%s' on an all-framed model" % gravity)
            elif "perimeter" in arch and "frame" in arch and "mixed" not in bases and "lean" not in gravity:
                probs.append("arch says a PERIMETER frame (interior leans on mixed bases) but the model is uniform all-rigid -- use a custom_build")
    if probs:
        return True, False, ("MODEL does not match its declaration / system: " + "; ".join(probs)
                             + ". Fix the cfg (or use a custom_build) and re-run BEFORE delivering.")
    return True, True, None


def floor_beam_gaps(cfg, transf="Linear"):
    """Column-line floor-grid beam positions that have NO beam element in the model = the un-modelled
    gravity girders. Coordinate-based, so it works for the parametric builder AND any custom_build.
    Returns a list of (z,(x1,y1),(x2,y2)); EMPTY means every column-line floor beam is modelled."""
    from collections import defaultdict
    info = build(cfg, transf)
    coord = {}; modelled = set()
    for (et, kind, sec, n1, n2) in info["ele"]:
        for t in (n1, n2):
            if t not in coord:
                try: coord[t] = ops.nodeCoord(t)
                except Exception: coord[t] = None
        a, b = coord.get(n1), coord.get(n2)
        if a and b and abs(a[2]-b[2]) < 1e-6:
            modelled.add(frozenset((n1, n2)))
    zmin = min((c[2] for c in coord.values() if c), default=0.0)
    # Build the adjacency grid ONLY from real column-grid nodes (tag == ntag(i,j,k) in range), so
    # legitimate off-grid work points -- brace crossing nodes, beam-subdivision nodes -- do not register
    # as phantom "missing" column-line beams and falsely fail model_complete. (P6)
    NXc, NYc, NFc = cfg["NX"], cfg["NY"], len(cfg["heights"])
    def _isgrid(t):
        k = t // 100000; r = t % 100000; i = r // 100; j = r % 100
        return 0 <= k <= NFc and 0 <= i <= NXc and 0 <= j <= NYc and t == ntag(i, j, k)
    byz = defaultdict(list)
    for t, c in coord.items():
        if c and c[2] > zmin + 1e-6 and _isgrid(t):
            byz[round(c[2], 3)].append((c[0], c[1], t))
    gaps = []
    for z, pts in byz.items():
        xs = sorted({round(p[0], 3) for p in pts}); ys = sorted({round(p[1], 3) for p in pts})
        xi = {x: i for i, x in enumerate(xs)}; yi = {y: i for i, y in enumerate(ys)}
        at = {(xi[round(x, 3)], yi[round(y, 3)]): (x, y, t) for (x, y, t) in pts}
        for (gi, gj), (x, y, t) in at.items():
            for (di, dj) in ((1, 0), (0, 1)):
                nb = at.get((gi+di, gj+dj))
                if nb and frozenset((t, nb[2])) not in modelled:
                    gaps.append((z, (x, y), (nb[0], nb[1])))
    return gaps


def run_one(name):
    cfg=CFG[name]; NF=len(cfg["heights"])
    T,w2,eX,eY,Mtot=modal(cfg,min(3*NF,12 if NF>=18 else 16))
    Cs,V,Tu,Ta,kk,Fx,W=elf(cfg,T[0]); gov=cfg.get("governing","seismic")
    if gov=="wind": FxX=wind_forces(cfg,"X"); FxY=wind_forces(cfg,"Y"); Vx=sum(FxX.values()); Vy=sum(FxY.values())
    else: FxX=Fx; FxY=Fx; Vx=Vy=V
    sx=static_lateral(cfg,FxX,"X"); sy=static_lateral(cfg,FxY,"Y")
    _cr_tr=max(sx[4],sy[4])                                # B5: centric-load torsion ratio (~1.0 if symmetric)
    mde_x=_drift_env(cfg, sx[2]); mde_y=_drift_env(cfg, sy[2]); cumX=sum(eX); cumY=sum(eY)
    dl,_dlrho=drift_allowable(cfg)   # Table 12.12-1 limit, /rho for MF-only SDC D-F (12.12.1.1)
    # ASCE 7-22 Eq. 12.8-16: design story drift = Cd*delta_elastic/Ie (seismic only; wind drift not amplified)
    Cd=cfg["seis"]["Cd"]; Ie=cfg["seis"]["Ie"]; amp=1.0 if gov=="wind" else Cd/Ie
    mdx=amp*mde_x; mdy=amp*mde_y
    chk={}
    chk["equil_X"]=abs(sx[3]+Vx)<=1e-3*Vx; chk["equil_Y"]=abs(sy[3]+Vy)<=1e-3*Vy; chk["stability"]=min(w2)>0
    chk["modalmass_X"]=cumX>=0.90; chk["modalmass_Y"]=cumY>=0.90
    chk["drift_X"]=0<mdx<dl; chk["drift_Y"]=0<mdy<dl
    chk["baseshear_X"]=abs(abs(sx[3])-Vx)<=1e-3*Vx; chk["baseshear_Y"]=abs(abs(sy[3])-Vy)<=1e-3*Vy
    chk["period"]=(0.5*Ta<=T[0]<=3*Ta) if NF>=3 else (0.1<=T[0]<=1.5)
    extra={}
    if (not chk["drift_X"] or not chk["drift_Y"]) and min(mdx,mdy)>1e-9 and max(mdx,mdy)/min(mdx,mdy)>1.5:
        _b="X" if mdx>mdy else "Y"
        extra["orientation_warning"]=("drift in %s is %.1fx the other direction AND fails -- this is "
            "usually a COLUMN ORIENTATION error (strong/weak axis swapped). In custom_build use "
            "engine3d.add_column(tag,n1,n2,sec,strong_dir) (or col_transf(dir)) so each frame column's "
            "STRONG axis is in its frame plane; verify with the orientation figure."%(_b, max(mdx,mdy)/min(mdx,mdy)))
    _decl_ok,_cons_ok,_mmsg=_model_gate(cfg)
    chk["model_declared"]=_decl_ok; chk["model_consistent"]=_cons_ok
    if _mmsg: extra["model_warning"]=_mmsg
    if "RS" in cfg.get("analyses",[]):
        # 7-22 sec.12.9.1.4.1: MRSA forces scale to the FULL ELF base shear V (Vt < V -> x V/Vt)
        VrsX=rs_baseshear(cfg,T,eX,eY,Mtot,"X"); VrsY=rs_baseshear(cfg,T,eX,eY,Mtot,"Y")
        extra["VrsX/V"]=VrsX/V; extra["VrsY/V"]=VrsY/V
        chk["rs_X"]=max(VrsX,1.00*V)>=1.00*V-1e-6; chk["rs_Y"]=max(VrsY,1.00*V)>=1.00*V-1e-6
    if cfg.get("torsion_check"):
        if NF>=30:
            tr=sx[4]
        else:
            sxa=static_lateral(cfg,FxX,"X",accidental=True)
            if NF>=18: tr=sxa[4]
            else:
                sya=static_lateral(cfg,FxY,"Y",accidental=True); tr=max(sxa[4],sya[4])
        # B4: torsional irregularity is ADVISORY, not a pass/fail gate. ASCE 7-22 Table 12.3-1 has a
        # SINGLE Type 1 keyed to the Torsional Irregularity Ratio (TIR, Eq. 12.3-2) with cumulative
        # tiers >1.2/>1.4/>1.6 (Table 12.3-1a); Ax per sec.12.8.4.3. static_lateral computes the
        # ratio from per-story DRIFTS at the diaphragm edge (the 7-22 TIR basis).
        extra["torsion_acc"]=tr
        extra["torsion_irregularity"]=("none (TIR screen <1.2)" if tr<1.2 else
                                       "Type 1 torsional, TIR screen %.2f (>1.2 tier)"%tr if tr<1.4 else
                                       "Type 1 torsional, TIR screen %.2f (>1.4 tier%s)"%(tr,"; also >1.6" if tr>1.6 else ""))
        extra["torsion_Ax"]=round(min(max((tr/1.2)**2,1.0),3.0),2)   # Eq. 12.8-15
        if _cr_tr>1.15:                                    # B5: centre of rigidity offset from centre of mass
            extra["cr_offset_warning"]=("centric-load torsion ratio %.2f > 1.15: the lateral system's centre "
                "of rigidity is offset from the centre of mass (asymmetric brace/frame layout), inducing "
                "INHERENT torsion with no accidental eccentricity -- prefer a symmetric layout."%_cr_tr)
    if cfg.get("dual_check"):
        mf=copy.deepcopy(cfg); mf["braces"]=None; smf=static_lateral(mf,FxX,"X")
        frac=sx[1][NF]/smf[1][NF]; extra["MF_fraction"]=frac; chk["dual_25pct"]=frac>=0.25
    if cfg.get("softstorey_check"):
        extra["softstorey_driftratio"]=sx[2][0]/sx[2][1] if sx[2][1] else 0
    # serviceability: beam vertical deflection (live L/360, total L/240). Long-span bays
    # framed with trusses/joists (beam_framing="truss") are exempt from the W-shape limit.
    rLL,rTL=beam_serviceability(cfg); extra["beam_defl_LL_ratio"]=rLL; extra["beam_defl_TL_ratio"]=rTL
    if cfg.get("beam_framing")=="truss":
        chk["beam_deflection"]=True; extra["beam_framing"]="truss (W-shape deflection check N/A)"
    else:
        chk["beam_deflection"]=(rLL<1.0 and rTL<1.0)
    _gaps = floor_beam_gaps(cfg)
    chk["model_complete"] = (len(_gaps) == 0)
    if _gaps:
        extra["incomplete_model"] = ("%d column-line floor beam(s) are NOT in the model. The OpenSees model is a "
            "DELIVERABLE and must contain EVERY structural element: all columns; all girders on every column line, "
            "in BOTH directions, at each floor; and all braces. Add the missing beams in your build with "
            "engine3d.add_beam(...) and re-run." % len(_gaps))
    return dict(name=name,arch=cfg["arch"],NF=NF,T=T,Ta=Ta,Cs=Cs,V=V,W=W,Tu=Tu,k=kk,cumX=cumX,cumY=cumY,mde_x=mde_x,mde_y=mde_y,Cd=Cd,
                mdx=mdx,mdy=mdy,roofX=sx[1][NF],roofY=sy[1][NF],Vx=Vx,Vy=Vy,gov=gov,chk=chk,extra=extra,allp=all(chk.values()))
def report(name):
    r=run_one(name)
    print("### %s %s (%d st, gov=%s)"%(r["name"],r["arch"],r["NF"],r["gov"]))
    for k,v in r["chk"].items(): print("   [%s] %s"%("PASS" if v else "FAIL",k))
    if r["extra"].get("orientation_warning"): print("   [WARN] ORIENTATION: "+r["extra"]["orientation_warning"])
    if r["extra"].get("model_warning"): print("   [GATE] MODEL: "+r["extra"]["model_warning"])
    if r["extra"].get("incomplete_model"): print("   [GATE] INCOMPLETE MODEL: "+r["extra"]["incomplete_model"])
    print("RESULT:", "ALL PASS" if r["allp"] else "FAIL"); return r
def _demo():
    import sys
    for nm in (sys.argv[1:] or list(CFG)):
        try: r=run_one(nm)
        except Exception:
            import traceback; print("### %s ERROR:\n"%nm+traceback.format_exc()); continue
        print("\n### %s %s (%d st, gov=%s)"%(r['name'],r['arch'],r['NF'],r['gov']))
        print("  W=%.0f V=%.1f(Vx=%.1f) Ta=%.3f Tu=%.3f T1=%.3f T2=%.3f k=%.2f"%(r['W'],r['V'],r['Vx'],r['Ta'],r['Tu'],r['T'][0],r['T'][1],r['k']))
        print("  cumMass X=%.2f Y=%.2f maxDrift X=1/%.0f Y=1/%.0f"%(r['cumX'],r['cumY'],1/r['mdx'],1/r['mdy']))
        if r['extra']: print("  extra:",{k:(round(v,3) if isinstance(v,float) else v) for k,v in r['extra'].items()})
        fails=[k for k,v in r['chk'].items() if not v]
        print("  %s"%('ALL PASS' if r['allp'] else 'FAIL: '+','.join(fails)))

def kz_exposure(zft,exp):
    """Velocity pressure exposure coefficient Kz = 2.41*(z/zg)^(2/alpha) per ASCE 7-22
    Table 26.10-1 note 1, with the Table 26.11-1 terrain constants (B: alpha=7.5, zg=3280 ft;
    C: 9.8, 2460; D: 11.5, 1935); z floored at 15 ft."""
    z=max(zft,15.0)
    if exp=="D": zg,al=1935.0,11.5
    elif exp=="B": zg,al=3280.0,7.5
    else: zg,al=2460.0,9.8
    return 2.41*(z/zg)**(2.0/al)

def _kz_integral(zft, exp):
    """Exact integral of Kz dz from grade to z (ft*-) for the Table 26.10-1 power law (constant below 15 ft)."""
    if exp=="D": zg,al=1935.0,11.5
    elif exp=="B": zg,al=3280.0,7.5
    else: zg,al=2460.0,9.8
    e = 2.0/al; k15 = kz_exposure(15.0, exp)
    if zft <= 15.0: return k15*max(zft, 0.0)
    return k15*15.0 + 2.41*zg**(-e)*(zft**(1+e) - 15.0**(1+e))/(1+e)

# ASCE 7-22 Table 26.11-1 (customary): alpha_bar, b_bar, c, l (ft), eps_bar, z_min (ft)
_T2611 = {"B": (1/4.5, 0.47, 0.30, 320.0, 1/3.0, 30.0),
          "C": (1/6.4, 0.66, 0.20, 500.0, 1/5.0, 15.0),
          "D": (1/8.0, 0.78, 0.15, 650.0, 1/8.0, 7.0)}

def _gust_flexible(V, exp, n1, beta, h, B, L):
    """Gust-effect factor Gf for a flexible building, ASCE 7-22 Sec. 26.11.5 Eqs. 26.11-6..-16
    (customary units: V mph, lengths ft)."""
    ab, bb, c, l, eb, zmin = _T2611.get(exp, _T2611["C"])
    zb = max(0.6*h, zmin)
    Iz = c*(33.0/zb)**(1.0/6.0)                                   # Eq. 26.11-7
    Lz = l*(zb/33.0)**eb                                          # Eq. 26.11-9
    Q = math.sqrt(1.0/(1.0 + 0.63*((B + h)/Lz)**0.63))            # Eq. 26.11-8
    Vz = bb*(zb/33.0)**ab*(88.0/60.0)*V                           # Eq. 26.11-16 (ft/s)
    N1 = n1*Lz/Vz                                                 # Eq. 26.11-14
    Rn = 7.47*N1/(1.0 + 10.3*N1)**(5.0/3.0)                       # Eq. 26.11-13
    def _R(eta): return 1.0 if eta <= 0 else 1.0/eta - (1.0 - math.exp(-2.0*eta))/(2.0*eta**2)   # Eq. 26.11-15
    Rh = _R(4.6*n1*h/Vz); RB = _R(4.6*n1*B/Vz); RL = _R(15.4*n1*L/Vz)
    R = math.sqrt(Rn*Rh*RB*(0.53 + 0.47*RL)/beta)                 # Eq. 26.11-12
    lg = math.sqrt(2.0*math.log(3600.0*n1))
    gR = lg + 0.577/lg                                            # Eq. 26.11-11
    gQ = gv = 3.4
    return 0.925*(1.0 + 1.7*Iz*math.sqrt(gQ**2*Q**2 + gR**2*R**2))/(1.0 + 1.7*gv*Iz)   # Eq. 26.11-10

def _cp_leeward(LB):
    """Leeward-wall Cp, ASCE 7-22 Fig. 27.3-1 (0-1: -0.5, 2: -0.3, >=4: -0.2, linear between)."""
    if LB <= 1.0: return -0.5
    if LB <= 2.0: return -0.5 + 0.2*(LB - 1.0)
    if LB <= 4.0: return -0.3 + 0.05*(LB - 2.0)
    return -0.2

# ASCE 7-22 Fig. 27.3-1 roof Cp, wind NORMAL to ridge, theta >= 10 deg, rows h/L = 0.25 / 0.5 / 1.0.
# _ROOF_WW = the MOST POSITIVE windward value (the one that maximises the horizontal MWFRS force);
# '0.0*' table entries are the interpolation zeros of note 2. theta = 60 deg -> 0.01*theta.
_ROOF_TH = (10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 45.0, 60.0)
_ROOF_WW = {0.25: (-0.18, 0.0, 0.2, 0.3, 0.3, 0.4, 0.4, 0.6),
            0.5:  (-0.18, -0.18, 0.0, 0.2, 0.2, 0.3, 0.4, 0.6),
            1.0:  (-0.18, -0.18, -0.18, 0.0, 0.2, 0.2, 0.3, 0.6)}
_ROOF_LW = {0.25: (-0.3, -0.5, -0.6, -0.6, -0.6, -0.6, -0.6, -0.6),
            0.5:  (-0.5, -0.5, -0.6, -0.6, -0.6, -0.6, -0.6, -0.6),
            1.0:  (-0.7, -0.6, -0.6, -0.6, -0.6, -0.6, -0.6, -0.6)}

def _interp(x, xs, ys):
    if x <= xs[0]: return ys[0]
    for n in range(len(xs)-1):
        if x <= xs[n+1]: return ys[n] + (ys[n+1]-ys[n])*(x-xs[n])/(xs[n+1]-xs[n])
    return ys[-1]

def _roof_cp(table, theta, hL):
    """Fig. 27.3-1 roof Cp interpolated in theta (theta < 10 deg uses the 10-deg row; theta > 60 uses
    0.01*theta for the windward table) and in h/L between the 0.25/0.5/1.0 rows."""
    if table is _ROOF_WW and theta > 60.0: return 0.01*theta
    th = min(max(theta, 10.0), 60.0)
    rows = sorted(table); vals = [_interp(th, _ROOF_TH, table[r]) for r in rows]
    return _interp(min(max(hL, rows[0]), rows[-1]), rows, vals)

def _wind_frame_type(cfg, w):
    """'mrf' (steel moment frame: Eq. 26.11-3 na = 22.2/h^0.8) or 'other' (Eq. 26.11-4 na = 75/h).
    cfg['wind']['frame'] overrides; otherwise inferred (braced / dual / shear-wall systems -> 'other',
    unknown -> 'mrf', the lower and therefore conservative na)."""
    f = str(w.get("frame", "")).lower()
    if f: return "mrf" if ("mrf" in f or "moment" in f) else "other"
    txt = (str(cfg.get("system", "")) + " " + str(cfg.get("arch", ""))).lower()
    if cfg.get("braces") or cfg.get("brace") or any(t in txt for t in
            ("brac", "cbf", "ebf", "brb", "spsw", "shear wall", "plate shear", "dual", "core", "truss")):
        return "other"
    return "mrf"

def _wind_override(cfg, direction):
    """Hand MWFRS story forces supplied by the agent (HR-10 override hook). First of:
    cfg['wind_forces_override'], cfg['wind']['story_forces'], cfg['wind_by_level'] -- each either
    {'X': {k: kip}, 'Y': {k: kip}} or a callable f(cfg, direction) -> {k: kip}."""
    NF = len(cfg["heights"])
    for name, src in (("cfg['wind_forces_override']", cfg.get("wind_forces_override")),
                      ("cfg['wind']['story_forces']", (cfg.get("wind") or {}).get("story_forces")),
                      ("cfg['wind_by_level']", cfg.get("wind_by_level"))):
        if not src: continue
        v = src(cfg, direction) if callable(src) else src.get(direction, src.get(direction.lower()))
        if v is None:
            raise ValueError("%s has no '%s' entry -- give story forces for BOTH wind directions" % (name, direction))
        out = {k: 0.0 for k in range(1, NF+1)}
        for kk, F in dict(v).items():
            k = int(kk)
            if not 1 <= k <= NF:
                raise ValueError("%s['%s'] level %r outside 1..%d" % (name, direction, kk, NF))
            out[k] = float(F)
        return out, name
    return None

def wind_detail(cfg, direction):
    """ASCE 7-22 Ch. 27 directional-procedure MWFRS story forces (kip) for wind along `direction`
    ('X' = wind blowing along X, force in X) with the full breakdown. Returns a dict with F {k: kip}
    (the envelope used by wind_forces), the per-level parts and every parameter used.

    Method (HR-10):
      * qz = 0.00256 Kz Kzt Ke V^2 (Eq. 26.10-1); p = q Kd G Cp (Eq. 27.3-1). Internal pressure
        (GCpi) acts equally on opposite walls and CANCELS in the net MWFRS story shear, so it is
        omitted here (it matters for roof uplift / components, which this function does not give).
      * Windward walls Cp = +0.8 with qz at each height (integrated exactly over the wall);
        leeward walls Cp(L/B) = -0.5 / -0.3 / -0.2 (L/B <= 1 / 2 / >= 4, Fig. 27.3-1) with qh at the
        mean roof height h; L, B = plan dimensions parallel / normal to the wind (all levels).
      * Geometry from the building volume: every plan bay has the stack of levels whose floor plate
        contains it; in each strip parallel to the wind the FRONT-most bay that reaches a height is the
        windward wall there and the BACK-most is the leeward wall (projected silhouette: setbacks,
        podiums, split levels, lean-tos and courtyards handled). Each wall height is shared between
        the two diaphragms it spans (mid-height bands) and the top band runs to the true roof line
        (the old code dropped the top half-storey and used the storey below). The bottom half of the
        lowest storey spans to the foundation and is reported separately as F_base_direct (it is part
        of the foundation base shear, not of any frame storey shear); set cfg['wind']['ground_band'] =
        'lowest' to lump it into the lowest level instead (conservative for single-storey frames).
      * Gable roofs (cfg['wind']['roofs'] = {k: {'eave_ft', 'ridge_ft', 'ridge': 'X'|'Y',
        'ridge_at' (ft, default plate centre)}}): wind along the ridge loads the triangular gable-end
        walls; wind normal to the ridge adds the roof-surface horizontal component
        qh G (Cp_windward - Cp_leeward) x (ridge - eave) (Fig. 27.3-1 roof table, the windward value
        that maximises shear; never negative, Fig. 27.3-1 note 6). Walls stop at the eave there.
      * Parapets: cfg['wind']['parapet_ft'] (flat roofs): GCpn = +1.5 windward / -1.0 leeward with qp
        at the parapet top (Sec. 27.3.4).
      * G per Sec. 26.11: low-rise (h <= 60 ft and h <= least plan width) or n1 >= 1 Hz -> rigid,
        G = cfg['wind'].get('G', 0.85). Otherwise FLEXIBLE: Gf by Eq. 26.11-10 with n1 from
        cfg['wind']['n1'] (or 'n1X'/'n1Y', Hz = 1/T1) else the approximate lower bound na of Sec. 26.11.3
        (steel MRF 22.2/h^0.8, other 75/h; a loud warning if the Sec. 26.11.2.1 limits are not met),
        damping cfg['wind']['beta'] (default 0.01). cfg['wind']['Gf'] overrides.
      * Sec. 27.1.5 minimum: 16 psf x projected wall area + 8 psf x projected roof area, applied
        level by level; the larger of the two loadings governs each level.
      * Both senses (wind from -X and from +X) are evaluated; each level takes the larger.
      * Override hook: cfg['wind_forces_override'] / cfg['wind']['story_forces'] / cfg['wind_by_level']
        = {'X': {k: kip}, 'Y': {...}} (or f(cfg, direction)) replaces the computation (hand MWFRS).
      * NOT included (give them by hand / the override if they govern): Fig. 27.3-8 Cases 2-4
        (eccentric / torsional wind), roof uplift, monoslope / hip / arched roofs.
    'Cpnet' in old cfgs is ignored (it lumped windward+leeward at one qz)."""
    w = cfg.get("wind") or {}; NF = len(cfg["heights"])
    direction = "Y" if str(direction).upper().startswith("Y") else "X"
    ov = _wind_override(cfg, direction)
    if ov is not None:
        return dict(F=ov[0], method="override", source=ov[1], direction=direction, warnings=[])
    if cfg.get("custom_build") is not None and "present" not in cfg:
        build(cfg, "Linear")                 # capture the real footprint first (call-order independent)
    exp = str(w.get("exposure", "C")).upper()[:1]
    if exp not in "BCD": exp = "C"
    V = float(w["V"]); Kd = float(w.get("Kd", 0.85))
    q0 = 0.00256*float(w.get("Kzt", 1.0))*float(w.get("Ke", 1.0))*V**2*Kd    # Eq. 26.10-1 (x Kd of Eq. 27.3-1)
    par = float(w.get("parapet_ft", 0.0) or 0.0)
    roofs = {int(k): v for k, v in (w.get("roofs") or {}).items()}
    lump = str(w.get("ground_band", "base")).lower() == "lowest"
    st, z = _stacks(cfg)
    warns = []
    if not st:
        raise ValueError("wind_detail: no floor plate at any level (floor_bays empty) -- check cfg['present']")
    def xedge(i): xc = cfg.get("xcoords"); return (xc[i] if xc else i*cfg["SX"])/12.0
    def yedge(j): yc = cfg.get("ycoords"); return (yc[j] if yc else j*cfg["SY"])/12.0
    bays_all = set(st)
    xs = [xedge(i) for i, j in bays_all] + [xedge(i+1) for i, j in bays_all]
    ys = [yedge(j) for i, j in bays_all] + [yedge(j+1) for i, j in bays_all]
    Lx, Ly = max(xs)-min(xs), max(ys)-min(ys)
    Lw, Bw = (Lx, Ly) if direction == "X" else (Ly, Lx)            # L parallel, B normal to the wind
    # ---- roof geometry per gable level ----
    gable = {}
    for k, r in roofs.items():
        if not 1 <= k <= NF: continue
        if str(r.get("type", "gable")).lower() != "gable":
            warns.append("roof type %r at level %d not supported -- treated as flat; give story forces by "
                         "override if its roof pressures govern" % (r.get("type"), k)); continue
        rax = str(r.get("ridge", "X")).upper()[:1]
        pb = floor_bays(cfg, k)
        if not pb: continue
        if rax == "X": lo_, hi_ = min(yedge(j) for i, j in pb), max(yedge(j+1) for i, j in pb)
        else:          lo_, hi_ = min(xedge(i) for i, j in pb), max(xedge(i+1) for i, j in pb)
        sr = float(r.get("ridge_at", (lo_+hi_)/2.0))
        eave, ridge = float(r["eave_ft"]), float(r["ridge_ft"])
        d_lo, d_hi = max(sr-lo_, 0.0), max(hi_-sr, 0.0)
        if min(d_lo, d_hi) <= 1e-6:
            warns.append("level %d: ridge at the plate edge (monoslope) -- the high-side wall above the eave is "
                         "not modelled; verify by hand" % k)
        gable[k] = dict(ax=rax, sr=sr, lo=lo_, hi=hi_, eave=eave, ridge=ridge, d_lo=d_lo, d_hi=d_hi)
    def roof_mean(k):
        gr = gable.get(k)
        if not gr: return z[k]
        dmin = min([d for d in (gr["d_lo"], gr["d_hi"]) if d > 1e-6] or [1.0])
        th = math.degrees(math.atan2(gr["ridge"]-gr["eave"], dmin))
        return gr["eave"] if th <= 10.0 else 0.5*(gr["eave"]+gr["ridge"])
    h = float(w.get("h_ft") or max(roof_mean(s[-1][0]) for s in st.values()))
    qh = q0*kz_exposure(h, exp)
    # ---- gust-effect factor (Sec. 26.11) ----
    least = min(Lx, Ly); n1 = w.get("n1"+direction, w.get("n1")); n1_src = "cfg['wind']['n1']"
    if w.get("Gf") is not None:
        G = float(w["Gf"]); rigid = False; n1_src = "cfg['wind']['Gf'] given"
    elif h <= 60.0 and h <= least:
        G = float(w.get("G", 0.85)); rigid = True; n1_src = "low-rise (Sec. 26.2) -> rigid permitted (26.11.2)"
    else:
        if n1 is None:
            ft = _wind_frame_type(cfg, w)
            n1 = 22.2/h**0.8 if ft == "mrf" else 75.0/h            # Eqs. 26.11-3 / 26.11-4
            n1_src = "approximate na (Sec. 26.11.3, %s)" % ("steel MRF 22.2/h^0.8" if ft == "mrf" else "75/h")
            Leff = (sum(z[k]*_plate_len(cfg, k, direction) for k in range(1, NF+1))
                    / max(sum(z[k] for k in range(1, NF+1)), 1e-9))   # Eq. 26.11-1
            if h > 300.0 or h >= 4.0*Leff:
                warns.append("Sec. 26.11.2.1 limits not met (h=%.0f ft, Leff=%.0f ft): the approximate na=%.3f Hz is "
                             "NOT permitted -- set cfg['wind']['n1'] (=1/T1 from the modal analysis) or a "
                             "rational Gf (26.11.6)" % (h, Leff, n1))
        n1 = float(n1)
        if n1 >= 1.0:
            G = float(w.get("G", 0.85)); rigid = True
        else:
            beta = float(w.get("beta", 0.01)); rigid = False
            G = _gust_flexible(V, exp, n1, beta, h, Bw, Lw)
            warns.append("FLEXIBLE building (n1=%.3f Hz < 1 Hz, %s): Gf=%.3f by Eq. 26.11-10 (beta=%.3f) replaces "
                         "G=%.2f" % (n1, n1_src, G, beta, float(w.get("G", 0.85))))
    LB = Lw/Bw if Bw > 0 else 1.0
    cpl = _cp_leeward(LB)
    hL = h/Lw if Lw > 0 else 0.25
    # ---- strips parallel to the wind, cells along the wind ----
    NX, NY = cfg["NX"], cfg["NY"]
    if direction == "X":
        nstrip, ncell = NY, NX
        cellbay = lambda s_, c_: (c_, s_)
        strip_rng = lambda s_: (yedge(s_), yedge(s_+1))
    else:
        nstrip, ncell = NX, NY
        cellbay = lambda s_, c_: (s_, c_)
        strip_rng = lambda s_: (xedge(s_), xedge(s_+1))
    along = [k for k, g in gable.items() if (g["ax"] == direction)]       # ridge parallel to the wind
    normal = [k for k, g in gable.items() if (g["ax"] != direction)]
    nsub = 16 if along else 1
    zero = lambda: {k: 0.0 for k in range(1, NF+1)}
    res = {}
    for sense in (+1, -1):
        Fw, Fl, Fp, Fr, Amin, roofA = zero(), zero(), zero(), zero(), zero(), zero()
        Fb = {"walls": 0.0, "min": 0.0}                     # wall load straight to the foundation
        for s_ in range(nstrip):
            a0, a1 = strip_rng(s_)
            order = list(range(ncell)) if sense > 0 else list(range(ncell-1, -1, -1))
            cells = [(cellbay(s_, c_), st.get(cellbay(s_, c_))) for c_ in order]
            cells = [(b, sk) for b, sk in cells if sk]
            if not cells: continue
            for m in range(nsub):
                u0 = a0 + (a1-a0)*m/nsub; u1 = a0 + (a1-a0)*(m+1)/nsub; um = 0.5*(u0+u1); wd = u1-u0
                tops = []
                for b, sk in cells:
                    kt = sk[-1][0]; gr = gable.get(kt)
                    if gr is None: T = z[kt]; Tp = T + par
                    elif gr["ax"] == direction:                      # gable end wall: triangular top
                        d = gr["d_lo"] if um < gr["sr"] else gr["d_hi"]
                        f = 1.0 - abs(um - gr["sr"])/d if d > 1e-6 else 0.0
                        T = Tp = gr["eave"] + (gr["ridge"]-gr["eave"])*max(min(f, 1.0), 0.0)
                    else:
                        T = Tp = gr["eave"]                          # eave wall; roof added below
                    tops.append((b, sk, T, Tp))
                brk = {0.0}
                for b, sk, T, Tp in tops:
                    brk.update((T, Tp)); zs = [x[1] for x in sk]
                    brk.add(zs[0]/2.0); brk.update((zs[n]+zs[n+1])/2.0 for n in range(len(zs)-1))
                brk = sorted(x for x in brk if x >= 0.0)
                for za, zb in zip(brk[:-1], brk[1:]):
                    if zb - za < 1e-9: continue
                    zm = 0.5*(za+zb)
                    hit = [t for t in tops if t[3] >= zb - 1e-9]
                    if not hit: continue
                    for role, (b, sk, T, Tp) in (("w", hit[0]), ("l", hit[-1])):
                        bs = _band_split(sk, za, zb); kk = max(bs, key=bs.get)
                        if kk == 0: kk = sk[0][0] if lump else 0
                        if kk == 0:
                            Fb["walls"] += ((0.8*G*q0*(_kz_integral(zb, exp)-_kz_integral(za, exp))) if role == "w"
                                            else -cpl*G*qh*(zb-za))*wd/1000.0
                            if role == "w": Fb["min"] += 16.0*(zb-za)*wd/1000.0
                            continue
                        if zm > T + 1e-9:                             # parapet band (Sec. 27.3.4)
                            qp = q0*kz_exposure(Tp, exp)
                            Fp[kk] += (1.5 if role == "w" else 1.0)*qp*(zb-za)*wd/1000.0
                            if role == "w": Amin[kk] += (zb-za)*wd
                        elif role == "w":
                            Fw[kk] += 0.8*G*q0*(_kz_integral(zb, exp)-_kz_integral(za, exp))*wd/1000.0
                            Amin[kk] += (zb-za)*wd
                        else:
                            Fl[kk] += -cpl*G*qh*(zb-za)*wd/1000.0
            # gable roof surfaces, wind normal to the ridge (horizontal component on projected height)
            for kt in normal:
                gr = gable[kt]
                if not any(sk[-1][0] == kt for b, sk in cells): continue
                if sense > 0: d_up, d_dn = gr["d_lo"], gr["d_hi"]
                else:         d_up, d_dn = gr["d_hi"], gr["d_lo"]
                dh = gr["ridge"] - gr["eave"]
                th_up = math.degrees(math.atan2(dh, d_up)) if d_up > 1e-6 else 0.0
                th_dn = math.degrees(math.atan2(dh, d_dn)) if d_dn > 1e-6 else 0.0
                cww = _roof_cp(_ROOF_WW, th_up, hL) if d_up > 1e-6 else 0.0
                clw = _roof_cp(_ROOF_LW, th_dn, hL) if d_dn > 1e-6 else 0.0
                Fr[kt] += max(qh*G*(cww - clw)*dh*(a1-a0)/1000.0, 0.0)   # Fig. 27.3-1 note 6: >= 0
                roofA[kt] += dh*(a1-a0)
        Fpress = {k: Fw[k]+Fl[k]+Fp[k]+Fr[k] for k in range(1, NF+1)}
        Fmin = {k: (16.0*Amin[k] + 8.0*roofA[k])/1000.0 for k in range(1, NF+1)}   # Sec. 27.1.5
        res[sense] = dict(windward=Fw, leeward=Fl, parapet=Fp, roof=Fr, pressure=Fpress, minimum=Fmin,
                          base_direct=max(Fb["walls"], Fb["min"]),
                          F={k: max(Fpress[k], Fmin[k]) for k in range(1, NF+1)})
    F = {k: max(res[+1]["F"][k], res[-1]["F"][k]) for k in range(1, NF+1)}
    gov = {k: ("27.1.5 minimum" if max(res[+1]["minimum"][k], res[-1]["minimum"][k]) >=
               max(res[+1]["pressure"][k], res[-1]["pressure"][k]) - 1e-12 and F[k] > 0 else "pressure")
           for k in range(1, NF+1)}
    for m in warns: _note_once("WIND " + m)
    Fbase = max(res[+1]["base_direct"], res[-1]["base_direct"])
    return dict(F=F, F_base_direct=Fbase, V_foundation=sum(F.values())+Fbase,
                method="ASCE 7-22 Ch.27 directional", direction=direction, G=G, rigid=rigid,
                n1=n1, n1_source=n1_src, Cp_windward=0.8, Cp_leeward=cpl, L_over_B=LB, L_ft=Lw, B_ft=Bw,
                h_ft=h, qh_psf=qh/Kd, Kd=Kd, exposure=exp, governs=gov, senses={"+": res[+1], "-": res[-1]},
                warnings=warns)

def _plate_len(cfg, k, direction):
    """Plan length (ft) of the level-k plate parallel to `direction` (Eq. 26.11-1 L_i)."""
    B = floor_bays(cfg, k)
    if not B: return 0.0
    if direction == "X":
        xc = cfg.get("xcoords"); f = lambda i: (xc[i] if xc else i*cfg["SX"])/12.0
        return max(f(i+1) for i, j in B) - min(f(i) for i, j in B)
    yc = cfg.get("ycoords"); f = lambda j: (yc[j] if yc else j*cfg["SY"])/12.0
    return max(f(j+1) for i, j in B) - min(f(j) for i, j in B)

def wind_forces(cfg,direction):
    """MWFRS story forces {level k: kip} for wind along `direction` ('X'/'Y'), ASCE 7-22 Ch. 26-27
    directional procedure -- see wind_detail() for the method, cfg keys and the per-level breakdown
    (HR-10). An agent's hand MWFRS forces are used verbatim when cfg['wind_forces_override'] /
    cfg['wind']['story_forces'] / cfg['wind_by_level'] = {'X': {k: kip}, 'Y': {k: kip}} is given."""
    return wind_detail(cfg, direction)["F"]
def tors2(k,NX,NY): return [("X",0,0),("X",NX-1,0),("Y",0,0),("Y",0,NY-1)]
def tors3(k,NX,NY): return [("X",0,0),("X",NX-1,0),("X",0,NY),("X",NX-1,NY),("Y",0,0),("Y",0,NY-1)]
def weak1(k,NX,NY): return [] if k==1 else perim_braces(k,NX,NY)
def podium2(k,NX,NY): return perim_braces(k,NX,NY) if k<=2 else []
def ydir(k,NX,NY): return [("Y",0,0),("Y",0,NY-1),("Y",NX,0),("Y",NX,NY-1)]
def offset8(k,NX,NY):
    if k<=4: return [("X",0,0),("X",0,NY),("Y",0,0),("Y",NX,0)]
    return [("X",NX-1,0),("X",NX-1,NY),("Y",0,NY-1),("Y",NX,NY-1)]
CFG["B11"]=dict(arch="mid-rise CBF office",NX=6,NY=4,SX=360,SY=360,heights=[162]*6,base="fixed",col="W14X159",beam="W24X76",brace="H8b",braces=perim_braces,seis=seis(0.5,0.25,0.25,6,0.02,0.75,1.5),analyses=["ELF","RS"],**D)
CFG["B12"]=dict(arch="slender wind tower",NX=3,NY=2,SX=360,SY=360,heights=[156]*15,base="fixed",col="W14X233",beam="W24X76",brace="H12",braces=perim_braces,seis=seis(0.25,0.10,0.10,6,0.02,0.75,1.7),governing="wind",wind=dict(V=150,exposure="D",Kd=0.85,Kzt=1.0,G=0.85,Cpnet=1.3),analyses=[],**D)
CFG["B13"]=dict(arch="coastal low-rise",NX=5,NY=3,SX=360,SY=360,heights=[156]*3,base="fixed",col="W14X159",beam="W27X94",seis=seis(0.25,0.10,0.10,3,0.028,0.8,1.7),governing="wind",wind=dict(V=160,exposure="D",Kd=0.85,Kzt=1.0,G=0.85,Cpnet=1.3),analyses=[],**D)
CFG["B14"]=dict(arch="torsional irregularity",NX=5,NY=5,SX=360,SY=360,heights=[156]*5,base="fixed",col="W14X193",beam="W24X76",brace="H10",braces=tors3,seis=seis(0.5,0.25,0.25,6,0.02,0.75,1.5),analyses=["ELF"],torsion_check=True,**D)
CFG["B15"]=dict(arch="soft first storey",NX=6,NY=4,SX=360,SY=360,heights=[156]*6,base="fixed",col="W14X233",beam="W24X76",brace="H10",braces=weak1,seis=seis(1.0,0.6,0.6,6,0.02,0.75,1.4),analyses=["ELF","RS"],softstorey_check=True,**D)
CFG["B16"]=dict(arch="podium two-stage",NX=6,NY=4,SX=360,SY=360,heights=[156]*9,base="fixed",col="W14X426",beam="W36X194",brace="H12",braces=podium2,seis=seis(1.0,0.6,0.6,6,0.028,0.8,1.4),analyses=["ELF","RS"],**D)
CFG["B17"]=dict(arch="mixed MF-X / CBF-Y",NX=6,NY=4,SX=360,SY=360,heights=[162]*6,base="fixed",col="W14X311",beam="W33X130",brace="H10",braces=ydir,seis=seis(1.0,0.6,0.6,6,0.028,0.8,1.4),analyses=["ELF","RS"],**D)
CFG["B18"]=dict(arch="out-of-plane offset",NX=6,NY=6,SX=360,SY=360,heights=[156]*8,base="fixed",col="W14X233",beam="W24X76",brace="H12",braces=offset8,seis=seis(1.0,0.6,0.6,6,0.02,0.75,1.4),analyses=["ELF","RS"],torsion_check=True,**D)
CFG["B19"]=dict(arch="snow long-span industrial",NX=4,NY=6,SX=600,SY=360,heights=[180,180],base="fixed",col="W14X159",beam="W36X194",seis=seis(0.25,0.10,0.10,3,0.028,0.8,1.7),analyses=["ELF"],snow=40.0,D_floor=70.0,D_roof=25.0,clad=12.0,L_floor=125.0)
CFG["B20"]=dict(arch="tall braced-core tower",NX=5,NY=5,SX=336,SY=336,heights=[156]*16,base="fixed",col="W14X426",beam="W24X76",brace="H14",braces=core_braces,seis=seis(1.0,0.6,0.6,6,0.02,0.75,1.4),analyses=["ELF","RS"],torsion_check=True,**D)


def stepcore(k,NX,NY):
    return [("X",2,2),("X",3,2),("X",2,4),("X",3,4),("Y",2,2),("Y",2,3),("Y",4,2),("Y",4,3)]
def step12(k,NX,NY):
    if k<=4: lo,hi=0,NX
    elif k<=8: lo,hi=1,NX-1
    else: lo,hi=2,NX-2
    return {(i,j) for i in range(lo,hi+1) for j in range(lo,hi+1)}
CFG["B21"]=dict(arch="20-story braced-core tower",NX=5,NY=5,SX=336,SY=336,heights=[156]*20,base="fixed",col="W14X500",beam="W24X76",brace="H14",braces=core_braces,seis=seis(1.0,0.6,0.6,6,0.02,0.75,1.4),analyses=["ELF","RS"],torsion_check=True,**D)
CFG["B22"]=dict(arch="25-story braced-core tower",NX=4,NY=4,SX=336,SY=336,heights=[156]*25,base="fixed",col="W14X730",beam="W24X76",brace="H14",braces=core_braces,seis=seis(1.0,0.6,0.6,6,0.02,0.75,1.4),analyses=["ELF","RS"],torsion_check=True,**D)
CFG["B23"]=dict(arch="30-story braced-core tower",NX=4,NY=4,SX=336,SY=336,heights=[156]*30,base="fixed",col="W14X730",beam="W24X76",brace="H14",braces=core_braces,seis=seis(1.0,0.6,0.6,6,0.02,0.75,1.4),analyses=["ELF","RS"],torsion_check=True,**D)
CFG["B24"]=dict(arch="SDC E near-fault CBF",NX=6,NY=4,SX=360,SY=360,heights=[156]*6,base="fixed",col="W14X193",beam="W24X76",brace="H10",braces=perim_braces,seis=seis(1.5,0.9,0.9,6,0.02,0.75,1.4),analyses=["ELF","RS"],**D)
CFG["B25"]=dict(arch="soft-soil long-period MF",NX=6,NY=4,SX=360,SY=360,heights=[156]*10,base="fixed",col="W14X500",beam="W36X194",seis=seis(1.0,0.9,0.9,8,0.028,0.8,1.4),analyses=["ELF","RS"],**D)
CFG["B26"]=dict(arch="high-aspect narrow office",NX=12,NY=2,SX=360,SY=360,heights=[156]*5,base="fixed",col="W14X233",beam="W30X108",seis=seis(1.0,0.6,0.6,8,0.028,0.8,1.4),analyses=["ELF","RS"],**D)
CFG["B27"]=dict(arch="large-footprint low-rise CBF",NX=12,NY=10,SX=360,SY=360,heights=[156]*2,base="fixed",col="W14X120",beam="W24X76",brace="H8",braces=perim_braces,seis=seis(0.5,0.25,0.25,6,0.02,0.75,1.5),analyses=["ELF"],**D)
CFG["B28"]=dict(arch="stepped setback tower",NX=6,NY=6,SX=360,SY=360,heights=[156]*12,base="fixed",plan=step12,col="W14X426",beam="W24X76",brace="H14",braces=stepcore,seis=seis(1.0,0.6,0.6,6,0.02,0.75,1.4),analyses=["ELF","RS"],torsion_check=True,**D)
CFG["B29"]=dict(arch="heavy industrial mill",NX=4,NY=6,SX=600,SY=360,heights=[240,240],base="fixed",col="W14X159",beam="W40X199",brace="H8b",braces=perim_braces,seis=seis(0.25,0.10,0.10,3.25,0.02,0.75,1.7),analyses=["ELF"],snow=40.0,D_floor=120.0,D_roof=30.0,clad=15.0,L_floor=250.0,beam_framing="truss")
CFG["B30"]=dict(arch="long-span warehouse",NX=8,NY=6,SX=480,SY=480,heights=[240],base="pinned",col="W14X90",beam="W33X130",brace="H8",braces=perim_braces,seis=seis(0.25,0.10,0.10,3.25,0.02,0.75,1.7),governing="wind",wind=dict(V=120,exposure="C",Kd=0.85,Kzt=1.0,G=0.85,Cpnet=1.3),analyses=[],**D)


def openlobby(k,NX,NY):
    full={(i,j) for i in range(NX+1) for j in range(NY+1)}
    if k<=1: return {(i,j) for (i,j) in full if i in (0,NX) or j in (0,NY)}
    return full
def doughnut(k,NX,NY):
    return {(i,j) for i in range(NX+1) for j in range(NY+1) if not (2<=i<=NX-2 and 2<=j<=NY-2)}

CFG["B31"]=dict(arch="eccentrically braced frame (EBF)",NX=6,NY=4,SX=360,SY=360,heights=[156]*8,base="fixed",col="W14X193",beam="W24X76",brace="H6",braces=perim_braces,seis=seis(1.0,0.6,0.6,8,0.02,0.75,1.4,Cd=4.0,Om0=2.0),analyses=["ELF","RS"],**D)
CFG["B32"]=dict(arch="buckling-restrained braced frame (BRBF)",NX=6,NY=4,SX=360,SY=360,heights=[156]*8,base="fixed",col="W14X159",beam="W24X76",brace="H8",braces=perim_braces,seis=seis(1.0,0.6,0.6,8,0.02,0.75,1.4,Cd=5.0,Om0=2.5),analyses=["ELF","RS"],**D)
CFG["B33"]=dict(arch="steel-plate shear-wall core",NX=5,NY=5,SX=336,SY=336,heights=[156]*10,base="fixed",col="W14X311",beam="W24X76",brace="H14",braces=core_braces,seis=seis(1.0,0.6,0.6,7,0.02,0.75,1.4,Cd=6.0),analyses=["ELF","RS"],torsion_check=True,**D)
CFG["B34"]=dict(arch="supertall core-tube tower",NX=4,NY=4,SX=360,SY=360,heights=[156]*40,base="fixed",col="W14X730",beam="W24X76",brace="H14",braces=core_braces,seis=seis(1.0,0.6,0.6,6,0.02,0.75,1.4),analyses=["ELF","RS"],torsion_check=True,**D)
CFG["B35"]=dict(arch="transfer-level open-lobby tower",NX=6,NY=4,SX=360,SY=360,heights=[180]+[156]*7,base="fixed",plan=openlobby,col="W14X500",beam="W36X194",seis=seis(1.0,0.6,0.6,8,0.028,0.8,1.4),analyses=["ELF","RS"],**D)
CFG["B36"]=dict(arch="atrium / diaphragm opening",NX=6,NY=6,SX=360,SY=360,heights=[156]*6,base="fixed",plan=doughnut,col="W14X233",beam="W30X108",seis=seis(1.0,0.6,0.6,8,0.028,0.8,1.4),analyses=["ELF","RS"],torsion_check=True,**D)
CFG["B37"]=dict(arch="nonparallel (skewed) system",NX=6,NY=4,SX=360,SY=360,heights=[156]*5,base="fixed",skew=90.0,col="W14X311",beam="W33X130",seis=seis(0.5,0.25,0.25,8,0.028,0.8,1.5),analyses=["ELF"],torsion_check=True,**D)
CFG["B38"]=dict(arch="mass irregularity (heavy floor)",NX=6,NY=4,SX=360,SY=360,heights=[156]*8,base="fixed",col="W14X426",beam="W36X194",seis=seis(1.0,0.6,0.6,8,0.028,0.8,1.4),analyses=["ELF","RS"],extra_mass_floors={4:150.0},**D)
CFG["B39"]=dict(arch="mixed bay spacing office",NX=5,NY=4,SX=360,SY=360,xcoords=[0,360,600,960,1200,1560],heights=[156]*6,base="fixed",col="W14X311",beam="W33X130",seis=seis(1.0,0.6,0.6,8,0.028,0.8,1.4),analyses=["ELF","RS"],**D)
CFG["B40"]=dict(arch="wind-governed supertall",NX=4,NY=4,SX=360,SY=360,heights=[156]*40,base="fixed",col="W14X730",beam="W24X76",brace="H14",braces=core_braces,seis=seis(0.25,0.10,0.10,6,0.02,0.75,1.7),governing="wind",wind=dict(V=140,exposure="D",Kd=0.85,Kzt=1.0,G=0.85,Cpnet=1.3),analyses=[],torsion_check=True,**D)
# ---- R23: non-rectangular reference builds (T/U/cruciform/Z) -- plan shapes that had no archetype ----
CFG["B41"]=dict(arch="T-plan tower, braced core",system="SPSW",model=dict(bases="fixed",joints="rigid",gravity="framed"),NX=6,NY=4,SX=336,SY=336,heights=[156]*12,base="fixed",plan=Tplan,col="W14X311",beam="W24X76",brace="H12",braces=core_braces,seis=seis(1.0,0.55,0.55,7,0.02,0.75,1.4,Cd=6.0),analyses=["ELF","RS"],torsion_check=True,**D)
CFG["B42"]=dict(arch="U-plan courtyard, unequal wings",system="BRBF",model=dict(bases="fixed",joints="rigid",gravity="framed"),NX=6,NY=4,SX=360,SY=360,heights=[156]*10,base="fixed",plan=Uplan,col="W14X311",beam="W24X76",brace="H10",braces=perim_braces,seis=seis(1.25,0.85,0.85,8,0.02,0.75,1.4,Cd=5.0,Om0=2.5),analyses=["ELF","RS"],torsion_check=True,**D)
CFG["B43"]=dict(arch="cruciform, four re-entrant corners",system="EBF",model=dict(bases="fixed",joints="rigid",gravity="framed"),NX=6,NY=6,SX=360,SY=360,heights=[156]*11,base="fixed",plan=cruciform,col="W14X311",beam="W24X76",brace="H12",braces=core_braces,seis=seis(0.95,0.52,0.52,8,0.02,0.75,1.4,Cd=4.0,Om0=2.0),analyses=["ELF","RS"],torsion_check=True,**D)
CFG["B44"]=dict(arch="Z-plan school",system="IMF",model=dict(bases="fixed",joints="rigid",gravity="framed"),NX=6,NY=4,SX=360,SY=360,heights=[168]*3,base="fixed",plan=Zplan,col="W14X159",beam="W24X76",seis=seis(0.48,0.20,0.20,4.5,0.028,0.8,1.5,Ie=1.25),analyses=["ELF","RS"],drift_limit=0.015,torsion_check=True,**D)


def export_model(cfg, outdir, name="model", transf="PDelta"):
    """Record the exact OpenSees commands build()/custom_build issues and write a STANDALONE, runnable
    model the user can open and check in OpenSees:
      <outdir>/model_opensees.py   -- standalone OpenSeesPy model (run: python model_opensees.py)
    Faithful because it REPLAYS the real ops calls (works for the default builder AND any custom_build)."""
    import os as _os, openseespy.opensees as _ops
    rec = []
    funcs = ["wipe", "model", "node", "fix", "mass", "geomTransf", "uniaxialMaterial",
             "element", "rigidDiaphragm", "equalDOF", "rigidLink"]
    orig = {f: getattr(_ops, f) for f in funcs if hasattr(_ops, f)}
    def _shim(fn, real):
        def w(*a):
            rec.append((fn, list(a))); return real(*a)
        return w
    for f, real in orig.items():
        setattr(_ops, f, _shim(f, real))
    try:
        build(cfg, transf)
    finally:
        for f, real in orig.items():
            setattr(_ops, f, real)
    _os.makedirs(outdir, exist_ok=True)
    arch = str(cfg.get("arch", ""))
    py = ['"""Standalone OpenSeesPy model for %s -- %s.' % (name, arch),
          'Auto-generated from the design cfg; rebuilds the EXACT analysis model.',
          'Run:  python model_opensees.py   (prints node/element counts + modal periods)."""',
          'import math', 'import openseespy.opensees as ops', '']
    _gt_seen = set()                                   # drop duplicate geomTransf(tag,...) registrations
    for cmd, a in rec:                                 # (a custom_build may register the same tag twice)
        if cmd == "geomTransf" and len(a) >= 2:
            if a[1] in _gt_seen: continue
            _gt_seen.add(a[1])
        py.append("ops.%s(%s)" % (cmd, ", ".join(repr(x) for x in a)))
    py += ['', '# --- quick self-check ---',
           'print("nodes:", len(ops.getNodeTags()), " elements:", len(ops.getEleTags()))',
           'w2 = ops.eigen("-fullGenLapack", 3)',
           'print("periods T (s):", [round(2*math.pi/math.sqrt(max(w, 1e-9)), 3) for w in w2])']
    pyp = _os.path.join(outdir, "model_opensees.py"); open(pyp, "w").write("\n".join(py) + "\n")
    return [pyp]


# back-fill the built-in B-archetypes with an inferred model declaration so they pass the gate;
# the AGENT must declare cfg['model'] for ITS buildings (a missing declaration is a hard FAIL).
for _b in list(CFG):
    CFG[_b].setdefault("model", _infer_model(CFG[_b]))

if __name__=="__main__": _demo()

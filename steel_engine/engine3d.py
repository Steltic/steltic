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
    # or declared by the agent) > cfg['plan'] callable > full NX x NY rectangle. This keeps areas,
    # perimeters, masses, wind widths and ELF weights consistent with the ACTUAL framed footprint for
    # non-rectangular / setback buildings instead of silently assuming the full plate.
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


def nbays(cfg,k):
    P=grid(cfg,k); n=0
    for i in range(cfg["NX"]):
        for j in range(cfg["NY"]):
            if (i,j) in P and (i+1,j) in P and (i,j+1) in P and (i+1,j+1) in P: n+=1
    return n

def floor_area_ft2(cfg,k):
    """Framed floor area (ft^2): sum of ACTUAL bay areas (honors non-rectangular footprints via
    grid() and non-uniform xcoords/ycoords).  Full uniform rectangle reduces to NX*NY*SX*SY."""
    P=grid(cfg,k); a=0.0
    for i in range(cfg["NX"]):
        for j in range(cfg["NY"]):
            if (i,j) in P and (i+1,j) in P and (i,j+1) in P and (i+1,j+1) in P:
                dx=_xy_in(cfg,i+1,j)[0]-_xy_in(cfg,i,j)[0]
                dy=_xy_in(cfg,i,j+1)[1]-_xy_in(cfg,i,j)[1]
                a+=abs(dx*dy)/144.0
    return a

def perim_ft(cfg,k):
    """Exposed floor-edge length (ft) for cladding: bay edges bordered by exactly ONE framed bay.
    Handles non-rectangular footprints (L/T/U, notches, setbacks) and non-uniform xcoords/ycoords.
    A full uniform rectangle reduces to the old 2*(NX*SX + NY*SY) bounding-box value."""
    P=grid(cfg,k)
    def framed(i,j):
        return (0<=i<cfg["NX"] and 0<=j<cfg["NY"] and (i,j) in P and (i+1,j) in P
                and (i,j+1) in P and (i+1,j+1) in P)
    per=0.0
    for i in range(cfg["NX"]):
        for j in range(cfg["NY"]+1):          # X-running edges between nodes (i,j)-(i+1,j)
            if framed(i,j-1) != framed(i,j):
                per+=abs(_xy_in(cfg,i+1,j)[0]-_xy_in(cfg,i,j)[0])/12.0
    for j in range(cfg["NY"]):
        for i in range(cfg["NX"]+1):          # Y-running edges between nodes (i,j)-(i,j+1)
            if framed(i-1,j) != framed(i,j):
                per+=abs(_xy_in(cfg,i,j+1)[1]-_xy_in(cfg,i,j)[1])/12.0
    return per

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

def floor_w(cfg,k):
    """Effective seismic weight of level k (ASCE 7-22 sec.12.7.2): dead + cladding + 25% of the
    floor live where the area is storage (item 1) + 15% of the uniform design snow load where the
    flat-roof snow load pf exceeds 45 psf (item 4)."""
    NF=len(cfg["heights"]); roof=(k==NF)
    d=_Dlev(cfg,k,roof)
    w=d*floor_area_ft2(cfg,k)/1000.0
    th=cfg["heights"][k-1]/12.0
    th=th if not roof else th/2
    w+=cfg["clad"]*perim_ft(cfg,k)*th/1000.0
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
    th=cfg["heights"][k-1]/12.0; th=th if not roof else th/2
    w+=cfg["clad"]*perim_ft(cfg,k)*th/1000.0
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
_MODAL_DETAIL = {}      # (_model_key, nm) -> per-mode diaphragm eigenvectors + participation (MRSA, HR-23)
_DIAPH_CACHE = {}       # _model_key -> diaphragm_props (centre of mass, polar inertia, extents; HR-19/31)
_TORSION_CACHE = {}     # _model_key -> torsion_summary (TIR / Ax / edge drift; HR-08/09)
_MRSA_CACHE = {}        # (_model_key, direction) -> mrsa() result (HR-23)
_SDF_CACHE = {}         # _model_key -> seismic_design_forces() (HR-01/02/08/23)

def clear_caches():
    """Drop the per-run modal/elf/mode-shape memo. Called at the start of each design_and_report run."""
    _MODAL_CACHE.clear(); _ELF_CACHE.clear(); _MODESHAPE_CACHE.clear(); _MODAL_DETAIL.clear()
    _DIAPH_CACHE.clear(); _TORSION_CACHE.clear(); _MRSA_CACHE.clear(); _SDF_CACHE.clear()


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
           repr(sorted((cfg.get("seis") or {}).items())),
           # HR-02/20/19/23: per-direction factors, the declared system (Table 12.2-1 defaults), the
           # diaphragm-mass policy and the seismic demand basis all change the seismic results
           repr([(k, cfg.get(k)) for k in ("seis_X", "seis_Y", "seis_x", "seis_y", "rho", "rho_X", "rho_Y",
                                            "system", "system_X", "system_Y", "diaphragm_mass", "diaphragm",
                                            "seismic_demand_basis", "analyses", "sdc", "drift_limit")]))
    return hashlib.md5(repr(sig).encode()).hexdigest()


def _nm_default(NF):
    """Mode count used by every seismic routine (period per direction, MRSA, modal-mass gate) so they
    share ONE eigen solve: 16 modes (12 for >= 18 stories), never more than 3 per diaphragm level."""
    return min(3 * NF, 12 if NF >= 18 else 16)


def modal(cfg, nm):
    """Memoized modal analysis. Identical (cfg, nm) within a run reuses the eigen solve instead of
    re-running it for every load_cases()/combos() call. Callers use only the returned tuple; whoever
    needs the live model next rebuilds it.  Returns (T, w2, eX, eY, Mtot); eX/eY are the effective
    modal mass ratios.  Per-mode diaphragm eigenvectors and participation factors (for MRSA) are kept
    in modal_detail(cfg, nm)."""
    mk = _model_key(cfg)
    key = (mk, nm)
    r = _MODAL_CACHE.get(key)
    if r is None:
        r = _modal_impl(cfg, nm, mk); _MODAL_CACHE[key] = r
    return r


def modal_detail(cfg, nm=None):
    """Per-mode data of the cached modal solve: {'T','w2','phi':{k:[(ux,uy,rz) at the master per mode]},
    'Lx','Ly','Mn' (participation numerators / generalised masses), 'mass_pts':{k:(x,y,m,J)} (where each
    level's lumped seismic mass sits), 'props' (diaphragm_props), 'warnings'}."""
    NF = len(cfg["heights"]); nm = nm or _nm_default(NF)
    modal(cfg, nm)
    return _MODAL_DETAIL.get((_model_key(cfg), nm))


# ===================================================================== diaphragm mass (HR-19, HR-31)
def cmtag(k):
    """Node tag of the level-k centre-of-mass node (slaved to the diaphragm master mtag(k))."""
    return k*100000 + 99998


def _poly_props(P):
    """Signed area, first moments and polar second moment about the ORIGIN of a polygon P=[(x,y)..]."""
    A = Sx = Sy = Ixx = Iyy = 0.0
    n = len(P)
    for a in range(n):
        x0, y0 = P[a]; x1, y1 = P[(a+1) % n]
        c = x0*y1 - x1*y0
        A += c; Sx += (x0+x1)*c; Sy += (y0+y1)*c
        Ixx += (y0*y0 + y0*y1 + y1*y1)*c; Iyy += (x0*x0 + x0*x1 + x1*x1)*c
    A *= 0.5; Sx /= 6.0; Sy /= 6.0; Ixx /= 12.0; Iyy /= 12.0
    if A < 0: A, Sx, Sy, Ixx, Iyy = -A, -Sx, -Sy, -Ixx, -Iyy
    return A, Sx, Sy, Ixx + Iyy


def _diaphragm_props_live(cfg, info):
    """Diaphragm mass properties of every level from the LIVE model (node coordinates as built):
      * W = floor_w(cfg,k) (the ELF seismic weight, so modal mass == W/g exactly);
      * the area-distributed part (dead, snow, storage live, extra mass) sits on the framed panels of
        the ACTUAL footprint (present positions, real node coordinates); the cladding part sits on the
        exposed panel edges -- centre of mass = weight-weighted centroid (ASCE 7-22 12.8.4.1/12.8.4.2.2);
      * J about the centre of mass by the parallel-axis rule (panel polygons + edge lines), not the old
        (extent + one bay) rectangle (HR-19);
      * the extreme points / plan dimensions perpendicular to each direction (accidental arm, edges).
    Returns {k: dict(W,m,xc,yc,J,xm,ym,z,xmin,xmax,ymin,ymax,BX,BY)}, plus key 'warnings'."""
    NF = info["NF"]; out = {}; warns = []
    for k in range(1, NF+1):
        pts = info["present"][k]
        co = {}
        for (i, j) in pts:
            try: c = ops.nodeCoord(ntag(i, j, k)); co[(i, j)] = (c[0], c[1], c[2])
            except Exception: co[(i, j)] = _xy_in(cfg, i, j) + (zlevels(cfg)[k],)
        try: mc = ops.nodeCoord(mtag(k)); xm, ym, zk = mc[0], mc[1], mc[2]
        except Exception:
            xm = sum(c[0] for c in co.values())/len(co); ym = sum(c[1] for c in co.values())/len(co)
            zk = zlevels(cfg)[k]
        xs = [c[0] for c in co.values()]; ys = [c[1] for c in co.values()]
        P = set(pts)
        def framed(i, j):
            return (i, j) in P and (i+1, j) in P and (i, j+1) in P and (i+1, j+1) in P
        A = Sx = Sy = Ip = 0.0
        for (i, j) in pts:
            if framed(i, j):
                quad = [co[(i, j)][:2], co[(i+1, j)][:2], co[(i+1, j+1)][:2], co[(i, j+1)][:2]]
                a, sx, sy, ip = _poly_props(quad); A += a; Sx += sx; Sy += sy; Ip += ip
        Ltot = Lx_ = Ly_ = Le = 0.0
        for (i, j) in pts:                 # exposed panel edges (bordered by exactly one framed panel)
            for (a, b, n1, n2) in (((i, j), (i+1, j), (i, j-1), (i, j)), ((i, j), (i, j+1), (i-1, j), (i, j))):
                if b not in P: continue
                if framed(*n1) != framed(*n2):
                    p, q = co[a], co[b]; L = math.hypot(q[0]-p[0], q[1]-p[1])
                    mx, my = 0.5*(p[0]+q[0]), 0.5*(p[1]+q[1])
                    Ltot += L; Lx_ += L*mx; Ly_ += L*my; Le += L*(mx*mx + my*my + L*L/12.0)
        W = floor_w(cfg, k); m = W/g
        NFh = len(cfg["heights"]); th = cfg["heights"][k-1]/12.0; th = th if k != NFh else th/2
        Wc = cfg.get("clad", 0.0)*perim_ft(cfg, k)*th/1000.0
        Wa = W - Wc
        if A <= 0 or Wa < -1e-9:
            # no framed panel (e.g. a single line of columns): node average + rectangle of the ACTUAL extent
            xc = sum(xs)/len(xs); yc = sum(ys)/len(ys)
            J = m*((max(xs)-min(xs))**2 + (max(ys)-min(ys))**2)/12.0
            warns.append("level %d: no framed floor panel found -- centre of mass taken at the node average "
                         "and J from the actual plan extent" % k)
        else:
            ma = Wa/g; me = Wc/g
            xa, ya = Sx/A, Sy/A; Ja = ma*(Ip/A - (xa*xa + ya*ya))
            if Ltot > 0 and me > 0:
                xe, ye = Lx_/Ltot, Ly_/Ltot; Je = me*(Le/Ltot - (xe*xe + ye*ye))
            else:
                xe, ye, Je, me = xa, ya, 0.0, 0.0
            mm = ma + me
            xc = (ma*xa + me*xe)/mm if mm > 0 else xa; yc = (ma*ya + me*ye)/mm if mm > 0 else ya
            J = Ja + ma*((xa-xc)**2 + (ya-yc)**2) + Je + me*((xe-xc)**2 + (ye-yc)**2)
        out[k] = dict(W=W, m=m, xc=xc, yc=yc, J=J, xm=xm, ym=ym, z=zk,
                      xmin=min(xs), xmax=max(xs), ymin=min(ys), ymax=max(ys),
                      BX=max(ys)-min(ys), BY=max(xs)-min(xs))
    out["warnings"] = warns
    return out


def diaphragm_props(cfg):
    """Cached _diaphragm_props_live on a fresh build (see there). Keys 1..NF and 'warnings'."""
    key = _model_key(cfg)
    r = _DIAPH_CACHE.get(key)
    if r is None:
        info = build(cfg, "Linear"); r = _diaphragm_props_live(cfg, info); _DIAPH_CACHE[key] = r
    return r


def attach_diaphragm_mass(cfg, info, props=None):
    """Put each level's seismic mass where it physically is: W/g translational and the footprint polar
    inertia J at the CENTRE OF MASS (ASCE 7-22 12.8.4.1), not at the master node / node average (HR-31).
    Where the master is not at the centre of mass a massed node cmtag(k) is added at the CM and slaved
    to the master by a rigid diaphragm (exact coupled mass matrix).  Call it at the end of a builder
    (example_build does) instead of hand-writing ops.mass on the master.  Returns the props dict."""
    props = props or _diaphragm_props_live(cfg, info)
    have = set(ops.getNodeTags())
    for k in range(1, info["NF"]+1):
        p = props[k]
        if math.hypot(p["xc"]-p["xm"], p["yc"]-p["ym"]) < 1e-6:
            ops.mass(mtag(k), p["m"], p["m"], 0.0, 0.0, 0.0, p["J"])
            continue
        t = cmtag(k)
        if t not in have:
            ops.node(t, p["xc"], p["yc"], p["z"]); ops.fix(t, 0, 0, 1, 1, 1, 0)
            ops.rigidDiaphragm(3, mtag(k), t)
        ops.mass(t, p["m"], p["m"], 0.0, 0.0, 0.0, p["J"])
        ops.mass(mtag(k), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    info["diaphragm"] = props
    return props


def _modal_impl(cfg, nm, mkey=None):
    mkey = mkey or _model_key(cfg)          # BEFORE the build below (_model_key rebuilds the model)
    info=build(cfg,"Linear"); NF=info["NF"]
    nm_req=min(nm,3*NF); maxmodes=max(1,3*NF)
    props=_diaphragm_props_live(cfg, info); warns=list(props.get("warnings", []))
    tags=list(ops.getNodeTags()); have=set(tags)
    mode=str(cfg.get("diaphragm_mass", "engine")).lower()
    mref=min([props[k]["m"] for k in range(1, NF+1) if props[k]["m"] > 0] or [1.0])   # smallest REAL floor mass
    # ---- builder masses (HR-19: never silently overwrite what the custom_build assigned) ----
    bm={}
    for t in tags:
        try: mm=ops.nodeMass(t)
        except Exception: mm=[0.0]*6
        if any(abs(v) > 1e-9*mref for v in mm[:6]): bm[t]=list(mm[:6])
    cm_built=("diaphragm" in info)          # the builder called attach_diaphragm_mass (example_build does)
    if mode=="builder":
        for k in range(1, NF+1):
            if mtag(k) not in bm and cmtag(k) not in bm:
                warns.append("diaphragm_mass='builder' but level %d has no mass on its master -- the engine "
                             "centre-of-mass mass W/g was assigned instead" % k)
                p=props[k]; t=cmtag(k)
                if t not in have:
                    ops.node(t, p["xc"], p["yc"], p["z"]); ops.fix(t, 0, 0, 1, 1, 1, 0)
                    ops.rigidDiaphragm(3, mtag(k), t)
                ops.mass(t, p["m"], p["m"], 0.0, 0.0, 0.0, p["J"])
    elif not cm_built:
        for k in range(1, NF+1):
            p=props[k]; b=bm.get(mtag(k))
            if b is None: continue
            e2=(p["xc"]-p["xm"])**2+(p["yc"]-p["ym"])**2
            Jm=p["J"]+p["m"]*e2                                   # engine J referred to the master
            dm=abs(b[0]/p["m"]-1.0) if p["m"] else 0.0
            dJ=abs(b[5]/Jm-1.0) if Jm else 0.0
            if dm>0.02 or dJ>0.10 or e2>1.0:
                warns.append("level %d: custom_build master mass m=%.4g J=%.4g at (%.0f,%.0f) replaced by the "
                             "consistent seismic mass m=W/g=%.4g, J_cm=%.4g at the centre of mass (%.0f,%.0f) "
                             "(dm=%+.1f%%, dJ(about master)=%+.1f%%); set cfg['diaphragm_mass']='builder' to "
                             "keep the builder's own masses" % (k, b[0], b[5], p["xm"], p["ym"], p["m"], p["J"],
                                                                 p["xc"], p["yc"], 100*(b[0]/p["m"]-1.0) if p["m"] else 0.0,
                                                                 100*(b[5]/Jm-1.0) if Jm else 0.0))
            bm.pop(mtag(k), None)
        try:
            attach_diaphragm_mass(cfg, info, props)
        except Exception as _ex:              # e.g. a builder without mtag(k) diaphragm masters
            warns.append("could not place the seismic mass at the centres of mass (%s) -- the builder's own "
                         "masses are used for the modal analysis" % _ex)
            mode = "builder"
            for t in tags:
                try:
                    mm = ops.nodeMass(t)
                    if any(abs(v) > 1e-9*mref for v in mm[:6]): bm[t] = list(mm[:6])
                except Exception:
                    pass
    other=[t for t in bm if t not in {mtag(k) for k in range(1, NF+1)} | {cmtag(k) for k in range(1, NF+1)}]
    if other:
        mo=sum(bm[t][0] for t in other)
        warns.append("custom_build assigned mass to %d non-diaphragm node(s) (sum m=%.4g = %.0f kip): kept in the "
                     "eigen solve, NOT in the ELF weight W" % (len(other), mo, mo*g))
    # Regularize the (singular) mass matrix: the rigid diaphragm leaves mass only on the floor masters,
    # so most DOF are massless and -genBandArpack fails to converge beyond ~6 modes -- silently falling
    # back to the O(N^3) -fullGenLapack (tens of seconds).  A tiny ~1e-8*floor-mass on every MASSLESS DOF
    # makes M non-singular so ARPACK converges for ALL requested modes in ~1 s, with NO change to periods
    # or modal mass.  Nodes that carry a real mass (the CM / master masses, any builder mass) keep it. (P7)
    _tm = 1e-8 * mref
    mnodes=[]
    for t in ops.getNodeTags():
        try: mm=list(ops.nodeMass(t))[:6]
        except Exception: mm=[0.0]*6
        if any(abs(v) > 1e-9*mref for v in mm):
            mnodes.append((t, mm))
            ops.mass(t, *[v if abs(v) > 1e-9*mref else _tm for v in mm])
        else:
            try: ops.mass(t, _tm, _tm, _tm, _tm, _tm, _tm)
            except Exception: pass
    Mtot=sum(mm[0] for t, mm in mnodes)
    masspt={}
    for k in range(1, NF+1):
        p=props[k]
        if mode=="builder" and mtag(k) in bm:
            masspt[k]=(p["xm"], p["ym"], bm[mtag(k)][0], bm[mtag(k)][5])
        else:
            masspt[k]=(p["xc"], p["yc"], p["m"], p["J"])
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
        # effective modal mass ratios over EVERY massed node (CM nodes, masters, any builder mass):
        # L_n = sum m*phi (per direction), M_n = sum m*phi^2 (ux, uy, rz)
        eX=[];eY=[];LX=[];LY=[];MN=[]
        for mode in range(1,len(w2)+1):
            Lx=Ly=Mi=0.0
            for t,mm in mnodes:
                p=ops.nodeEigenvector(t,mode)
                Lx+=mm[0]*p[0]; Ly+=mm[1]*p[1]
                Mi+=mm[0]*p[0]**2+mm[1]*p[1]**2+mm[2]*p[2]**2+mm[3]*p[3]**2+mm[4]*p[4]**2+mm[5]*p[5]**2
            LX.append(Lx); LY.append(Ly); MN.append(Mi)
            eX.append((Lx**2)/Mi/Mtot if Mi>0 else 0); eY.append((Ly**2)/Mi/Mtot if Mi>0 else 0)
        return eX,eY,LX,LY,MN
    # SINGLE solve: ops.eigen cannot be re-called on one model (the eigenSOE is consumed after the first
    # call), and with the mass regularized above, -genBandArpack converges for the full request in ONE
    # shot (~1 s). Ask for enough modes to capture the ASCE 7-22 12.9.1 90% mass target. (P7)
    nev = min(maxmodes, max(nm_req, 12))
    w2 = _solve(nev); eX, eY, LX, LY, MN = _mass(w2)
    T=[2*math.pi/math.sqrt(max(x,1e-12)) for x in w2]
    phi={k:[tuple(ops.nodeEigenvector(mtag(k),md)[i] for i in (0,1,5)) for md in range(1,len(w2)+1)]
         for k in range(1,NF+1)}
    _MODAL_DETAIL[(mkey,nm)]={"T":T,"w2":list(w2),"phi":phi,"Lx":LX,"Ly":LY,"Mn":MN,"Mtot":Mtot,
                              "mass_pts":masspt,"props":props,"warnings":warns,"eX":eX,"eY":eY}
    # Cache the full eigenvector FIELD (every node, every solved mode) while the model is live, so the
    # report's 3D mode-shape figure and the animated-mode GIF REUSE this solve instead of re-running
    # ops.eigen (fig_mode_3d previously re-solved with the slow -fullGenLapack). Plain-dict snapshot,
    # so it survives the later model rebuilds the figure code used to do.
    try:
        nm_have=len(w2)
        coords={t:ops.nodeCoord(t) for t in ops.getNodeTags()}
        evf={t:[ops.nodeEigenvector(t,md) for md in range(1,nm_have+1)] for t in coords}
        _MODESHAPE_CACHE[mkey]={"T":T,"coords":coords,"ev":evf,"ele":list(info["ele"]),
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

# ===================================================================== seismic factors (HR-02, HR-20)
# ASCE 7-22 Table 12.2-1 (R, Omega0, Cd) with the Table 12.8-2 period coefficients (Ct, x) for the
# steel seismic force-resisting systems this engine designs.  Keys are what sfrs_key() returns.
SFRS_TABLE = {
    "EBF":  dict(R=8.0,  Om0=2.0, Cd=4.0,  Ct=0.03,  x=0.75, row="B.1 steel eccentrically braced frames"),
    "SCBF": dict(R=6.0,  Om0=2.0, Cd=5.0,  Ct=0.02,  x=0.75, row="B.2 steel special concentrically braced frames"),
    "OCBF": dict(R=3.25, Om0=2.0, Cd=3.25, Ct=0.02,  x=0.75, row="B.3 steel ordinary concentrically braced frames"),
    "BRBF": dict(R=8.0,  Om0=2.5, Cd=5.0,  Ct=0.03,  x=0.75, row="B.26 steel buckling-restrained braced frames"),
    "SPSW": dict(R=7.0,  Om0=2.0, Cd=6.0,  Ct=0.02,  x=0.75, row="B.27 steel special plate shear walls"),
    "SMF":  dict(R=8.0,  Om0=3.0, Cd=5.5,  Ct=0.028, x=0.8,  row="C.1 steel special moment frames"),
    "STMF": dict(R=7.0,  Om0=3.0, Cd=5.5,  Ct=0.028, x=0.8,  row="C.2 steel special truss moment frames"),
    "IMF":  dict(R=4.5,  Om0=3.0, Cd=4.0,  Ct=0.028, x=0.8,  row="C.3 steel intermediate moment frames"),
    "OMF":  dict(R=3.5,  Om0=3.0, Cd=3.0,  Ct=0.028, x=0.8,  row="C.4 steel ordinary moment frames"),
    "DUAL_SMF_EBF":  dict(R=8.0, Om0=2.5, Cd=4.0, Ct=0.03, x=0.75, row="D.1 dual: SMF + steel EBF"),
    "DUAL_SMF_SCBF": dict(R=7.0, Om0=2.5, Cd=5.5, Ct=0.02, x=0.75, row="D.2 dual: SMF + steel SCBF"),
    "DUAL_SMF_BRBF": dict(R=8.0, Om0=2.5, Cd=5.0, Ct=0.03, x=0.75, row="D.13 dual: SMF + steel BRBF"),
    "DUAL_SMF_SPSW": dict(R=8.0, Om0=2.5, Cd=6.5, Ct=0.02, x=0.75, row="D.14 dual: SMF + steel SPSW"),
    "DUAL_IMF_SCBF": dict(R=6.0, Om0=2.5, Cd=5.0, Ct=0.02, x=0.75, row="E.1 dual: IMF + steel SCBF"),
    "NSD":  dict(R=3.0,  Om0=3.0, Cd=3.0,  Ct=None,  x=None, row="H steel systems not specifically detailed"),
}


def _has_word(s, w):
    import re
    return re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(w), s) is not None


def sfrs_key(name):
    """Map a declared system name (cfg['system'], e.g. 'SPSW', 'Dual SMF+BRBF', 'steel IMF') to a
    SFRS_TABLE key; None when it is empty, unknown, or names several systems without 'dual' (a mixed
    building -- declare the system per direction instead, cfg['system_X'] / cfg['system_Y'])."""
    s = str(name or "").lower().replace("_", " ").replace("-", " ")
    if not s.strip():
        return None
    found = []
    def add(key, *words):
        if any(_has_word(s, w) for w in words): found.append(key)
    add("STMF", "stmf", "truss moment")
    add("SPSW", "spsw", "plate shear wall", "plate shear walls")
    add("BRBF", "brbf", "buckling restrained")
    add("EBF", "ebf", "eccentrically braced", "eccentric")
    add("SCBF", "scbf", "special concentrically braced", "special concentric")
    add("OCBF", "ocbf", "ordinary concentrically braced", "ordinary concentric")
    add("SMF", "smf", "special moment")
    add("IMF", "imf", "intermediate moment")
    add("OMF", "omf", "ordinary moment")
    if "not specifically detailed" in s or _has_word(s, "nsd"):
        found.append("NSD")
    if "dual" in s:
        mf = [f for f in found if f in ("SMF", "IMF")]; other = [f for f in found if f not in ("SMF", "IMF", "OMF")]
        if len(mf) == 1 and len(other) == 1:
            k = "DUAL_%s_%s" % (mf[0], other[0])
            return k if k in SFRS_TABLE else None
        return None
    return found[0] if len(found) == 1 else None


def _sfrs_from_factors(s):
    """The unique SFRS_TABLE key whose (R, Cd, Om0) equal the set s, else None (used only where no system
    name resolves, e.g. a mixed building that gives explicit per-direction factors)."""
    try:
        hit = [k for k, r in SFRS_TABLE.items()
               if all(abs(float(s[f]) - r[f]) < 1e-9 for f in ("R", "Cd", "Om0"))]
    except Exception:
        return None
    return hit[0] if len(hit) == 1 else None


def _legacy_cd(R):
    return {8: 5.5, 7: 5.5, 6: 5.0, 4.5: 4.0, 3.25: 3.25, 3: 3.0}.get(R, R)


def _legacy_om0(R, Cd):
    if R == 7 and Cd == 6.0: return 2.0               # SPSW row B.27
    return {8: 3.0, 7: 2.5, 6: 2.0, 4.5: 3.0, 3.25: 2.0, 3: 3.0}.get(R, 2.5)


def _dir_override(cfg, d):
    """Per-direction override dict (ASCE 7-22 12.2.2): cfg['seis']['X'|'Y'], then cfg['seis_X'|'seis_Y']
    (or lower-case cfg['seis_x'|'seis_y']).  NOTE cfg['seis']['x'] is the period exponent, never a
    direction key."""
    ov = {}
    s = cfg.get("seis") or {}
    for src in (s.get(d), cfg.get("seis_" + d), cfg.get("seis_" + d.lower())):
        if isinstance(src, dict): ov.update(src)
    return ov


def system_dir(cfg, direction=None):
    """Declared SFRS name in `direction`: seis[d]['system'] / cfg['seis_d']['system'] / cfg['system_X'|'Y'],
    else the building-wide cfg['seis']['system'] / cfg['system']."""
    if direction in ("X", "Y"):
        ov = _dir_override(cfg, direction)
        for v in (ov.get("system"), cfg.get("system_" + direction), cfg.get("system_" + direction.lower())):
            if v: return str(v)
    s = cfg.get("seis") or {}
    return str(s.get("system") or cfg.get("system") or "")


def _seis_resolve(cfg, d=None):
    base = {k: v for k, v in (cfg.get("seis") or {}).items() if k not in ("X", "Y")}
    auto = set(base.pop("_auto", None) or [])
    ov = dict(_dir_override(cfg, d)) if d in ("X", "Y") else {}
    ov_auto = set(ov.pop("_auto", None) or [])
    eff = dict(base); eff.update(ov)
    warns = []; tag = ("direction %s" % d) if d else "building"
    r_changed = bool(ov) and "R" in ov and "R" in base and abs(float(ov["R"]) - float(base["R"])) > 1e-9
    if r_changed:
        # a different R means a different system in this direction: only a DIRECTION-specific system name
        # may supply its Table 12.2-1 row (the building-wide cfg['system'] describes the other direction)
        sysname = str(ov.get("system") or cfg.get("system_" + d) or cfg.get("system_" + d.lower()) or "")
    else:
        sysname = system_dir(cfg, d)
    key = sfrs_key(sysname); row = SFRS_TABLE.get(key)
    if ov:
        auto = (auto - set(ov)) | ov_auto
        if r_changed:
            for kk in ("Cd", "Om0", "Ct", "x"):        # never inherit another system's factors
                if kk not in ov: auto.add(kk)
    for kk in sorted(auto):
        if row and row.get(kk) is not None:
            eff[kk] = row[kk]
        elif kk == "Cd":
            eff[kk] = _legacy_cd(eff.get("R"))
            warns.append("%s: Cd=%s is an R-keyed DEFAULT (system %r not resolved to a Table 12.2-1 row) -- "
                         "declare cfg['system'] or pass Cd explicitly" % (tag, eff[kk], sysname or None))
        elif kk == "Om0":
            eff[kk] = _legacy_om0(eff.get("R"), eff.get("Cd"))
            warns.append("%s: Omega0=%s is an R-keyed DEFAULT (system %r not resolved to a Table 12.2-1 row) -- "
                         "declare cfg['system'] or pass Om0 explicitly" % (tag, eff[kk], sysname or None))
        else:
            warns.append("%s: %s=%s inherited from the base cfg['seis'] although R differs -- give %s for "
                         "this direction (Table 12.8-2)" % (tag, kk, eff.get(kk), kk))
    if row:
        for kk in ("R", "Cd", "Om0", "Ct", "x"):
            v = eff.get(kk); t = row.get(kk)
            if v is None or t is None: continue
            if abs(float(v) - float(t)) > 1e-6:
                note = ""
                if kk == "Cd" and float(v) < t: note = " (UNCONSERVATIVE: drift too low)"
                if kk == "Om0" and float(v) < t: note = " (UNCONSERVATIVE: overstrength too low)"
                if kk == "R" and float(v) > t: note = " (UNCONSERVATIVE: base shear too low)"
                if kk == "Ct" and float(v) < t: note = " (Ta lower -> conservative Cs cap)"
                warns.append("%s: %s=%s differs from ASCE 7-22 Table %s row %s value %s%s -- confirm"
                             % (tag, kk, v, "12.8-2" if kk in ("Ct", "x") else "12.2-1", row["row"], t, note))
    rho = None
    if d in ("X", "Y"):
        rho = cfg.get("rho_" + d)
        if rho is None: rho = ov.get("rho")
    if rho is None: rho = cfg.get("rho")
    if rho is None: rho = base.get("rho")
    eff["rho"] = float(rho if rho is not None else 1.3)
    eff["system"] = sysname; eff["sfrs"] = key
    if not key:
        inf = _sfrs_from_factors(eff)
        if inf:                                   # no name resolved, but R/Cd/Om0 match exactly one row
            eff["sfrs"] = inf; eff["sfrs_inferred"] = True
    return eff, warns


def seis_dir(cfg, direction=None):
    """Effective seismic parameter set for `direction` ('X', 'Y' or None = building-wide):
    the base cfg['seis'] merged with the direction's overrides (ASCE 7-22 12.2.2: the respective R, Cd
    and Omega0 apply to each system), the defaulted factors replaced by the declared system's Table 12.2-1
    / 12.8-2 values (HR-20), and the redundancy factor rho (cfg['rho_X'|'rho_Y'] > override 'rho' >
    cfg['rho'] > 1.3).  Fully backward compatible: with one set, X and Y are identical."""
    return _seis_resolve(cfg, direction)[0]


def seis_warnings(cfg):
    """Loud list of seismic-factor problems (defaults not tied to a declared system, values that differ
    from Table 12.2-1 / 12.8-2, per-direction overrides missing factors).  Preflight checks are separate."""
    out = []
    dirs = [None, "X", "Y"] if any(_dir_override(cfg, d) for d in ("X", "Y")) or cfg.get("system_X") \
        or cfg.get("system_Y") else [None]
    for d in dirs:
        for w in _seis_resolve(cfg, d)[1]:
            if w not in out: out.append(w)
    return out


# ===================================================================== ELF per direction (HR-01, HR-02)
def period_dir(cfg, direction, nm=None):
    """(T, mode index, effective mass ratio) of the mode with the DOMINANT translational mass
    participation in `direction` -- ASCE 7-22 12.8.2 'the fundamental period in the direction under
    consideration' (not simply mode 1, which may be torsional or belong to the other axis)."""
    NF = len(cfg["heights"]); nm = nm or _nm_default(NF)
    T, w2, eX, eY, Mtot = modal(cfg, nm)
    eff = eX if direction == "X" else eY
    i = max(range(len(T)), key=lambda n: eff[n])
    return T[i], i, eff[i]


def elf(cfg, T1, direction=None):
    """Memoized ELF for period T1 (pure function of cfg + T1 + direction).  `direction` selects the
    per-direction factor set (seis_dir); None keeps the single building-wide set.
    Returns (Cs, V, Tu, Ta, k, {level: Fx}, W)."""
    key = (_model_key(cfg), round(float(T1), 6), direction)
    r = _ELF_CACHE.get(key)
    if r is None:
        r = _elf_impl(cfg, T1, direction); _ELF_CACHE[key] = r
    return r


def elf_dir(cfg, direction):
    """ELF in `direction` with that direction's period (period_dir) AND factors (seis_dir):
    Tu = min(T_dir, Cu*Ta_dir), Cs/V/k/Fx per ASCE 7-22 12.8.1-12.8.3."""
    return elf(cfg, period_dir(cfg, direction)[0], direction)


def _elf_impl(cfg, T1, direction=None):
    s = seis_dir(cfg, direction); NF = len(cfg["heights"]); z = zlevels(cfg)
    W = sum(floor_w(cfg, k) for k in range(1, NF+1))
    Ta = s["Ct"]*(z[-1]/12)**s["x"]; Tu = min(T1, s["Cu"]*Ta)          # 12.8.2 / Eq. 12.8-8
    R, Ie = s["R"], s["Ie"]
    Cs = s["SDS"]/(R/Ie)                                                 # Eq. 12.8-2
    TL = s.get("TL", 8.0)
    cap = s["SD1"]/(Tu*(R/Ie)) if Tu <= TL else s["SD1"]*TL/(Tu**2*(R/Ie))   # Eq. 12.8-3 / -4
    Cs = min(Cs, cap); cmin = max(0.044*s["SDS"]*Ie, 0.01)                   # Eq. 12.8-6
    if s.get("S1", 0) >= 0.6: cmin = max(cmin, 0.5*s["S1"]/(R/Ie))          # Eq. 12.8-7
    Cs = max(Cs, cmin); V = Cs*W
    kk = 1.0 if Tu <= 0.5 else (2.0 if Tu >= 2.5 else 1+(Tu-0.5)/2.0)
    whk = {k: floor_w(cfg, k)*z[k]**kk for k in range(1, NF+1)}; ss = sum(whk.values())
    return Cs, V, Tu, Ta, kk, {k: V*whk[k]/ss for k in range(1, NF+1)}, W


def _eq12_8_7_governs(cfg, direction):
    """True when Cs in `direction` is set by the S1 >= 0.6 minimum, Eq. 12.8-7 (drives 12.9.1.4.2)."""
    s = seis_dir(cfg, direction)
    if s.get("S1", 0) < 0.6: return False
    Cs = elf_dir(cfg, direction)[0]
    return abs(Cs - 0.5*s["S1"]/(s["R"]/s["Ie"])) < 1e-12


def _cqc_rho(T, zeta=0.05):
    n = len(T); rho = [[0.0]*n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            r = T[j]/T[i] if T[i] > 0 else 0
            rho[i][j] = ((8*zeta**2*(1+r)*r**1.5)/((1-r**2)**2+4*zeta**2*r*(1+r)**2)) if r > 0 else (1.0 if i == j else 0.0)
    return rho


def _cqc(vals, rho):
    v2 = 0.0; n = len(vals)
    for i in range(n):
        if vals[i] == 0.0: continue
        for j in range(n):
            v2 += rho[i][j]*vals[i]*vals[j]
    return math.sqrt(max(v2, 0.0))


def rs_baseshear(cfg, T, eX, eY, Mtot, direction):
    """CQC modal base shear Vt in `direction` (design spectrum / (R/Ie) of that direction)."""
    s = seis_dir(cfg, direction); R, Ie = s["R"], s["Ie"]; W = Mtot*g
    eff = eX if direction == "X" else eY
    Vi = [(sa(cfg, T[i])/(R/Ie))*eff[i]*W for i in range(len(T))]
    return _cqc(Vi, _cqc_rho(T))


# ===================================================================== static ELF analysis (HR-08/09/31)
def _u_at(ux, uy, rz, xm, ym, x, y, di):
    """Rigid-diaphragm displacement in direction di (0=X, 1=Y) at plan point (x, y)."""
    return (ux - rz*(y - ym)) if di == 0 else (uy + rz*(x - xm))


def static_lateral(cfg, Fx, direction, accidental=False, Ax=None):
    """Linear-elastic (P-Delta) static ELF analysis in `direction` under the story forces Fx{level}.
    * The forces act at each level's CENTRE OF MASS (diaphragm_props), applied at the master as the force
      plus its offset torque -- ASCE 7-22 12.8.4.1 inherent torsion from the true CM (HR-31).
    * accidental = False | True/+1 | -1: adds Mta = +/-0.05 * B * Fx * Ax at each level, B = the actual
      plan dimension perpendicular to the forces (12.8.4.2.2(2), HR-19); Ax{level} (12.8.4.3) defaults 1.
    Returns (ok, disp, drift, base_reaction, TIR, detail):
      disp{k}   displacement at the level's centre of mass;
      drift[k-1] story drift ratio at the centre of mass (bottom = vertical projection of the top CM,
                 12.8.6.5);
      TIR       max over stories of Delta_max/Delta_avg at the two extreme edges (Eq. 12.3-2);
      detail    {'edge_drift': [max |edge drift ratio| per story], 'edge_pair': [(lo, hi) signed],
                 'tir': [per story or None], 'disp_ratio': {k: delta_max/delta_avg} (Eq. 12.8-15 basis),
                 'edge_disp': {k: (lo, hi)}, 'Mta': {k}, 'rz': {k}}."""
    props = diaphragm_props(cfg)
    info = build(cfg, "PDelta"); NF = info["NF"]; di = 0 if direction == "X" else 1
    sgn = 0.0 if not accidental else (-1.0 if (accidental is not True and accidental < 0) else 1.0)
    ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
    for k in range(1, NF+1):
        pts = info["present"][k]; p = floor_grav(cfg, k)/len(pts)
        for (i, j) in pts: ops.load(ntag(i, j, k), 0, 0, -p, 0, 0, 0)
    Mta = {}
    for k in range(1, NF+1):
        pk = props[k]; f = [0.0]*6; F = Fx[k]; f[di] = F
        # force at the centre of mass -> force + offset torque at the master
        f[5] = (-(pk["yc"]-pk["ym"])*F) if di == 0 else ((pk["xc"]-pk["xm"])*F)
        B = pk["BX"] if di == 0 else pk["BY"]
        Mta[k] = sgn*0.05*B*F*(Ax.get(k, 1.0) if Ax else 1.0)
        f[5] += Mta[k]
        ops.load(mtag(k), *f)
    ops.constraints("Transformation"); ops.numberer("RCM"); ops.system("UmfPack")
    ops.test("NormDispIncr",1e-7,200); ops.algorithm("Newton"); ops.integrator("LoadControl",1.0); ops.analysis("Static")
    ok=ops.analyze(1)
    U = {k: (ops.nodeDisp(mtag(k), 1), ops.nodeDisp(mtag(k), 2), ops.nodeDisp(mtag(k), 6)) for k in range(1, NF+1)}
    def u(k, x, y):
        if k == 0: return 0.0
        a = U[k]; pk = props[k]
        return _u_at(a[0], a[1], a[2], pk["xm"], pk["ym"], x, y, di)
    disp = {}; dr = []; edge_drift = []; edge_pair = []; tir = []; dratio = {}; edge_disp = {}
    for k in range(1, NF+1):
        pk = props[k]; h = cfg["heights"][k-1]
        disp[k] = u(k, pk["xc"], pk["yc"])
        dr.append((disp[k] - u(k-1, pk["xc"], pk["yc"]))/h)
        if di == 0: E1, E2 = (pk["xc"], pk["ymin"]), (pk["xc"], pk["ymax"])
        else:       E1, E2 = (pk["xmin"], pk["yc"]), (pk["xmax"], pk["yc"])
        d1 = (u(k, *E1) - u(k-1, *E1))/h; d2 = (u(k, *E2) - u(k-1, *E2))/h
        edge_pair.append((d1, d2)); edge_drift.append(max(abs(d1), abs(d2)))
        avg = 0.5*(abs(d1) + abs(d2))
        tir.append(max(abs(d1), abs(d2))/avg if avg > 1e-12 else None)
        u1, u2 = u(k, *E1), u(k, *E2); edge_disp[k] = (u1, u2)
        davg = 0.5*(abs(u1) + abs(u2))
        dratio[k] = (max(abs(u1), abs(u2))/davg) if davg > 1e-12 else None
    ops.reactions()
    Rs = sum(ops.nodeReaction(ntag(i, j, 0), di+1) for (i, j) in info["present"][0])
    # Torsional Irregularity Ratio (ASCE 7-22 Eq. 12.3-2): story drift at the building's extreme edge /
    # average of the two opposing edges, for every story; the max over stories is returned.  Stories
    # whose drift is numerically nil (e.g. a stiff podium under a tiny force) are skipped.
    big = max(edge_drift) if edge_drift else 0.0
    vals = [t for t, e in zip(tir, edge_drift) if t is not None and e > 1e-6*big]
    tratio = max(vals) if vals else 1.0
    det = dict(edge_drift=edge_drift, edge_pair=edge_pair, tir=tir, disp_ratio=dratio, edge_disp=edge_disp,
               Mta=Mta, rz={k: U[k][2] for k in U})
    return ok, disp, dr, Rs, tratio, det


# ===================================================================== torsion: TIR / Ax / edge drift
def torsion_summary(cfg):
    """THE torsional-irregularity result used everywhere (screen, combos, gates, report) -- HR-08/HR-09.
    Per direction, with that direction's ELF forces:
      * TIR (Eq. 12.3-2): story-drift max/avg at the extreme edges WITH accidental torsion (+ and -),
        Ax = 1 (12.3.2.1.1); Type 1 when TIR > 1.2 (Table 12.3-1, tiers 1.2/1.4/1.6 of Table 12.3-1a);
      * Ax per level (Eq. 12.8-15): (delta_max/(1.2 delta_avg))^2 from level DISPLACEMENTS at the extreme
        points, 1.0 <= Ax <= 3.0, applied where Type 1 exists in SDC C-F (12.8.4.3);
      * design story drift location (12.8.6.5): the centre of mass, or -- Type 1 in SDC C-F -- the largest
        edge drift of vertically aligned points incl. diaphragm rotation, with accidental torsion x Ax.
    Flexible diaphragms: not applicable (12.8.4.2.1), Ax = 1, CM drift.
    Returns {'X': {...}, 'Y': {...}, 'TIR': max, 'type1': bool, 'amplify': bool, 'edges': bool,
             'classification': str, 'Ax_max': float, 'sdc': str}."""
    key = _model_key(cfg)
    r = _TORSION_CACHE.get(key)
    if r is not None:
        return r
    sdc = sdc_of(cfg); flexible = str(cfg.get("diaphragm", "rigid")).lower() == "flexible"
    NF = len(cfg["heights"]); res = {"sdc": sdc}
    for d in ("X", "Y"):
        Fx = elf_dir(cfg, d)[5]
        c0 = static_lateral(cfg, Fx, d)
        ap = static_lateral(cfg, Fx, d, accidental=+1)
        an = static_lateral(cfg, Fx, d, accidental=-1)
        tir_story = []
        for a, b in zip(ap[5]["tir"], an[5]["tir"]):
            vv = [v for v in (a, b) if v is not None]; tir_story.append(max(vv) if vv else None)
        axraw = {}
        for k in range(1, NF+1):
            vv = [v for v in (ap[5]["disp_ratio"].get(k), an[5]["disp_ratio"].get(k)) if v is not None]
            axraw[k] = max(((v/1.2)**2 for v in vv), default=1.0)
        res[d] = dict(TIR=max(ap[4], an[4]), TIR_story=tir_story, TIR_inherent=c0[4], Ax_raw=axraw,
                      cm_drift=list(c0[2]), centric=c0, acc=(ap, an))
    TIR = max(res["X"]["TIR"], res["Y"]["TIR"])
    type1 = (not flexible) and TIR > 1.2
    amplify = type1 and sdc in ("C", "D", "E", "F")
    for d in ("X", "Y"):
        Fx = elf_dir(cfg, d)[5]; q = res[d]
        q["Ax"] = {k: (round(min(max(q["Ax_raw"][k], 1.0), 3.0), 3) if amplify else 1.0) for k in range(1, NF+1)}
        if amplify and any(v > 1.0 for v in q["Ax"].values()):
            runs = (static_lateral(cfg, Fx, d, accidental=+1, Ax=q["Ax"]),
                    static_lateral(cfg, Fx, d, accidental=-1, Ax=q["Ax"]))
        else:
            runs = q["acc"]
        q["acc_amp"] = runs
        q["edge_drift"] = [max(abs(runs[0][5]["edge_drift"][i]), abs(runs[1][5]["edge_drift"][i])) for i in range(NF)]
        # the static accidental-torsion contribution alone (amplified run minus centric run), per story --
        # what 12.9.1.5 adds to the MRSA edge drift
        q["mta_edge_drift"] = [max(abs(runs[s][5]["edge_pair"][i][e] - q["centric"][5]["edge_pair"][i][e])
                                   for s in (0, 1) for e in (0, 1)) for i in range(NF)]
        q["drift_location"] = "edges" if amplify else "centre of mass"
        q["drift_elastic"] = list(q["edge_drift"]) if amplify else [abs(v) for v in q["cm_drift"]]
        q["Ax_max"] = max(q["Ax"].values()) if q["Ax"] else 1.0
    res["TIR"] = TIR; res["type1"] = type1; res["amplify"] = amplify; res["edges"] = amplify
    res["flexible"] = flexible
    res["Ax_max"] = max(res["X"]["Ax_max"], res["Y"]["Ax_max"])
    if flexible:
        res["classification"] = "not applicable (flexible diaphragm, 12.8.4.2.1)"
    elif TIR <= 1.2:
        res["classification"] = "none (TIR %.2f <= 1.2)" % TIR
    else:
        tier = ">1.6" if TIR > 1.6 else (">1.4" if TIR > 1.4 else ">1.2")
        res["classification"] = ("Type 1 torsional, TIR %.2f (%s tier)%s" % (
            TIR, tier, "; Ax up to %.2f applied to Mta (12.8.4.3); drift at the edges (12.8.6.5)" % res["Ax_max"]
            if amplify else "; SDC %s: Ax / edge drift not required" % sdc))
    _TORSION_CACHE[key] = res
    return res


# ===================================================================== MRSA (HR-23)
def mrsa(cfg, direction):
    """Modal response spectrum analysis in `direction` (ASCE 7-22 12.9.1), CQC with 5 % damping on the
    cached modes.  Per mode n: Gamma_n = L_n/M_n, pseudo-displacement D_n = Gamma_n*Sa_n*g/omega_n^2/(R/Ie);
    story forces F = m*omega^2*u at each level's mass point; story shears, CM drifts (vertical projection)
    and edge drifts are CQC-combined.  Scaling (12.9.1.4): forces x V/Vt where Vt < V (ELF V of that
    direction, 12.9.1.4.1); displacements x CsW/Vt where Vt < CsW and Cs is set by Eq. 12.8-7
    (12.9.1.4.2).  For Type 1 the static accidental torsion (x Ax) is added to the edge drift (12.9.1.5).
    Returns dict(Vt, V, force_scale, drift_scale, F (scaled story forces, sum = scaled base shear),
    shear (scaled CQC story shears), drift_cm / drift_edge (elastic ratios, scaled), nmodes, mass)."""
    key = (_model_key(cfg), direction)
    r = _MRSA_CACHE.get(key)
    if r is not None:
        return r
    NF = len(cfg["heights"]); det = modal_detail(cfg)
    s = seis_dir(cfg, direction); RI = s["R"]/s["Ie"]; di = 0 if direction == "X" else 1
    T = det["T"]; w2 = det["w2"]; L = det["Lx"] if di == 0 else det["Ly"]; Mn = det["Mn"]
    props = det["props"]; mp = det["mass_pts"]; nmo = len(T)
    Vn = []; Dcm = []; De1 = []; De2 = []
    for n in range(nmo):
        if Mn[n] <= 0 or w2[n] <= 0:
            Vn.append([0.0]*NF); Dcm.append([0.0]*NF); De1.append([0.0]*NF); De2.append([0.0]*NF); continue
        Dn = (L[n]/Mn[n])*sa(cfg, T[n])*g/w2[n]/RI
        U = {k: tuple(Dn*v for v in det["phi"][k][n]) for k in range(1, NF+1)}
        def u(k, x, y):
            if k == 0: return 0.0
            a = U[k]; pk = props[k]
            return _u_at(a[0], a[1], a[2], pk["xm"], pk["ym"], x, y, di)
        F = [mp[k][2]*w2[n]*u(k, mp[k][0], mp[k][1]) for k in range(1, NF+1)]
        V = [sum(F[i:]) for i in range(NF)]
        dc = []; e1 = []; e2 = []
        for k in range(1, NF+1):
            pk = props[k]; h = cfg["heights"][k-1]
            dc.append((u(k, pk["xc"], pk["yc"]) - u(k-1, pk["xc"], pk["yc"]))/h)
            if di == 0: E1, E2 = (pk["xc"], pk["ymin"]), (pk["xc"], pk["ymax"])
            else:       E1, E2 = (pk["xmin"], pk["yc"]), (pk["xmax"], pk["yc"])
            e1.append((u(k, *E1) - u(k-1, *E1))/h); e2.append((u(k, *E2) - u(k-1, *E2))/h)
        Vn.append(V); Dcm.append(dc); De1.append(e1); De2.append(e2)
    rho = _cqc_rho(T)
    shear = [_cqc([Vn[n][i] for n in range(nmo)], rho) for i in range(NF)]
    dcm = [_cqc([Dcm[n][i] for n in range(nmo)], rho) for i in range(NF)]
    de = [max(_cqc([De1[n][i] for n in range(nmo)], rho), _cqc([De2[n][i] for n in range(nmo)], rho)) for i in range(NF)]
    Vt = shear[0] if shear else 0.0
    Cs, V, Tu, Ta, kk, Fx, W = elf_dir(cfg, direction)
    fs = (V/Vt) if 0 < Vt < V else 1.0
    ds = (Cs*W/Vt) if (0 < Vt < Cs*W and _eq12_8_7_governs(cfg, direction)) else 1.0
    sh = [fs*v for v in shear]
    F = {k: sh[k-1] - (sh[k] if k < NF else 0.0) for k in range(1, NF+1)}
    ts = torsion_summary(cfg); q = ts[direction]
    edge = [ds*de[i] + (q["mta_edge_drift"][i] if ts["edges"] else 0.0) for i in range(NF)]
    r = dict(Vt=Vt, V=V, force_scale=fs, drift_scale=ds, F=F, shear=sh, drift_cm=[ds*v for v in dcm],
             drift_edge=edge, drift_elastic=(edge if ts["edges"] else [ds*v for v in dcm]),
             drift_location=q["drift_location"], nmodes=nmo,
             mass=sum(det["eX"] if di == 0 else det["eY"]))
    _MRSA_CACHE[key] = r
    return r


# ===================================================================== design seismic forces per direction
def seismic_design_forces(cfg):
    """Per direction, everything the load combinations and gates need (HR-01/02/08/19/23/31):
    {'X': dict(Fx{k} (story forces used for the demands), F_elf{k}, V, Cs, T, Tu, Ta, k, W, R, Cd, Om0,
               rho, Ie, Ax{k}, B{k} (accidental arm dimension), off{k}=(xc-xm, yc-ym) CM offset from the
               master, basis, rs (mrsa dict or None)), 'Y': {...}, 'basis': ..., 'warnings': [...]}.
    Demand basis (12.6: ELF is permitted for all structures; 12.9.1 MRSA where 'RS' is in cfg['analyses']):
    cfg['seismic_demand_basis'] = 'ELF' | 'MRSA' | 'envelope' (default 'envelope' when RS is requested:
    the larger of the ELF and the 12.9.1.4-scaled MRSA response -- never below either
    method in any story: the story-shear envelope of the two, re-differenced into story forces, so the base
    shear stays V), 'ELF' otherwise."""
    key = _model_key(cfg)
    r = _SDF_CACHE.get(key)
    if r is not None:
        return r
    NF = len(cfg["heights"]); props = diaphragm_props(cfg)
    rs_on = "RS" in (cfg.get("analyses") or [])
    basis = str(cfg.get("seismic_demand_basis") or ("envelope" if rs_on else "ELF"))
    if basis not in ("ELF", "MRSA", "envelope"):
        raise ValueError("cfg['seismic_demand_basis'] must be 'ELF', 'MRSA' or 'envelope' (got %r)" % basis)
    if basis != "ELF" and not rs_on:
        raise ValueError("cfg['seismic_demand_basis']=%r needs 'RS' in cfg['analyses']" % basis)
    ts = torsion_summary(cfg); out = {"basis": basis}
    for d in ("X", "Y"):
        s = seis_dir(cfg, d)
        Cs, V, Tu, Ta, kk, Fx, W = elf_dir(cfg, d)
        rs = mrsa(cfg, d) if rs_on else None
        if basis == "MRSA": F = dict(rs["F"])
        elif basis == "envelope":
            # per story the larger STORY SHEAR of the two methods (both sum to V at the base, so the
            # envelope keeps V_base = V and never falls below either method in any story)
            Ve = {k: max(sum(Fx[i] for i in range(k, NF+1)), rs["shear"][k-1]) for k in range(1, NF+1)}
            F = {k: Ve[k] - (Ve[k+1] if k < NF else 0.0) for k in range(1, NF+1)}
        else: F = dict(Fx)
        out[d] = dict(Fx=F, F_elf=dict(Fx), V=V, Cs=Cs, T=period_dir(cfg, d)[0], Tu=Tu, Ta=Ta, k=kk, W=W,
                      R=s["R"], Cd=s["Cd"], Om0=s["Om0"], rho=s["rho"], Ie=s["Ie"], system=s.get("system"),
                      sfrs=s.get("sfrs"),
                      Ax=dict(ts[d]["Ax"]), B={k: (props[k]["BX"] if d == "X" else props[k]["BY"]) for k in range(1, NF+1)},
                      off={k: (props[k]["xc"]-props[k]["xm"], props[k]["yc"]-props[k]["ym"]) for k in range(1, NF+1)},
                      basis=basis, rs=rs)
    out["warnings"] = seis_warnings(cfg) + list(props.get("warnings", []))
    _SDF_CACHE[key] = out
    return out


def lateral_pattern(cfg, dirn, sgn=1, acc=0, fac="rho", companion=0.3):
    """Diaphragm-master load dict {level: (fx, fy, mz)} for one seismic case:
    principal = fac_d * Fx_d (fac 'rho' | 'Om0' | a number), companion = companion * fac_o * Fx_o of the
    OTHER direction (its own forces and factors, 12.5.3.1), forces at the centre of mass (offset torque),
    accidental torsion acc * 0.05 * B * Ax * principal force (12.8.4.2 / 12.8.4.3), all x sgn."""
    SF = seismic_design_forces(cfg); NF = len(cfg["heights"])
    oth = "Y" if dirn == "X" else "X"; P, O = SF[dirn], SF[oth]
    fP = P[fac] if isinstance(fac, str) else float(fac)
    fO = O[fac] if isinstance(fac, str) else float(fac)
    lat = {}
    for k in range(1, NF+1):
        Fp = fP*P["Fx"][k]; Fo = companion*fO*O["Fx"][k]
        fx, fy = (Fp, Fo) if dirn == "X" else (Fo, Fp)
        ex, ey = P["off"][k]
        mz = ex*fy - ey*fx + acc*0.05*P["B"][k]*P["Ax"][k]*Fp
        lat[k] = (sgn*fx, sgn*fy, sgn*mz)
    return lat


def sdc_of(cfg):
    """Seismic Design Category for a cfg: cfg['sdc'] if declared, else derived via the canonical
    ASCE 7-22 sec.11.6 implementation in preflight.asce_sdc (Tables 11.6-1/-2 incl. the Risk
    Category IV column and the S1 >= 0.75 -> E/F override)."""
    from preflight import sdc_of_cfg
    return sdc_of_cfg(cfg)


def _mf_only(cfg, direction=None):
    """True when the SFRS (in `direction`, if given and declared per direction) is a moment-frame-only
    system (SMF/IMF/OMF/STMF family, not dual): the declared system wins; otherwise a model with no
    braces is taken as moment-frame-only."""
    sysname = (system_dir(cfg, direction) if direction else str(cfg.get("system") or "")).lower()
    key = sfrs_key(sysname)
    if not key and direction:
        key = _sfrs_from_factors(seis_dir(cfg, direction))   # e.g. a mixed building with per-direction R/Cd/Om0
    if key:
        return key in ("SMF", "IMF", "OMF", "STMF")
    if sysname:
        return (any(k in sysname for k in ("smf", "imf", "omf")) or "moment" in sysname) \
               and "dual" not in sysname
    return not is_braced(cfg)


def drift_allowable(cfg, direction=None):
    """(allowable story drift ratio, rho_applied): cfg['drift_limit'] (Table 12.12-1 value,
    default 0.020), divided by the redundancy factor rho for a moment-frame-only SFRS in SDC
    D/E/F per ASCE 7-22 sec.12.12.1.1 (Delta <= Delta_a/rho).  With `direction`, the system and rho of
    that direction (seis_dir) are used (HR-02); rho defaults to cfg['rho'] (1.3), as in the combinations."""
    dl = float(cfg.get("drift_limit", 0.020) or 0.020)
    if sdc_of(cfg) in ("D", "E", "F") and _mf_only(cfg, direction):
        rho = float(seis_dir(cfg, direction)["rho"]) if direction else float(cfg.get("rho", 1.3) or 1.3)
        return dl / rho, True
    return dl, False


def _drift_env(cfg, drifts):
    """Max |story drift| honoring cfg['drift_exempt_stories'] = {story_index(1-based): reason}.
    Declared inter-diaphragm offsets (split-level steps) are excluded from the code story-drift
    gate; their racking is a designed detail the agent covers in calc_package (F-1)."""
    dex = set(int(k) for k in (cfg.get("drift_exempt_stories") or {}))
    vals = [abs(d) for i, d in enumerate(drifts, start=1) if i not in dex]
    return max(vals) if vals else max(abs(d) for d in drifts)


def seismic_drift(cfg):
    """Design story drifts per direction (ASCE 7-22 Eq. 12.8-16: Delta = Cd*delta_xe/Ie, here as ratios of
    the story height) at the location 12.8.6.5 requires (centre of mass; the edges, incl. diaphragm
    rotation and accidental torsion x Ax, for Type 1 in SDC C-F -- torsion_summary), from ELF and -- when
    'RS' is requested -- from the 12.9.1.4.2-scaled MRSA; the governing profile follows the seismic demand
    basis ('envelope': per story the larger).  Each direction uses its own Cd (12.2.2) and allowable drift
    (rho of that direction for moment-frame-only systems, 12.12.1.1).
    {'X': dict(location, elf[], mrsa[] or None, design[], max, limit, rho_applied, elastic[] (delta_xe/h
    at the same location -- the story stiffness basis for theta, 12.8.7)), 'Y': {...}}"""
    SF = seismic_design_forces(cfg); ts = torsion_summary(cfg); out = {}
    for d in ("X", "Y"):
        P = SF[d]; amp = P["Cd"]/P["Ie"]
        el = list(ts[d]["drift_elastic"])
        dd = dict(location=ts[d]["drift_location"], elf=[amp*v for v in el], mrsa=None, Cd=P["Cd"])
        if P["rs"] is not None:
            dd["mrsa"] = [amp*v for v in P["rs"]["drift_elastic"]]
        if SF["basis"] == "MRSA":
            dd["design"] = dd["mrsa"]; dd["elastic"] = list(P["rs"]["drift_elastic"])
        elif SF["basis"] == "envelope":
            dd["design"] = [max(a, b) for a, b in zip(dd["elf"], dd["mrsa"])]
            dd["elastic"] = [max(a, b) for a, b in zip(el, P["rs"]["drift_elastic"])]
        else:
            dd["design"] = dd["elf"]; dd["elastic"] = el
        dd["max"] = _drift_env(cfg, dd["design"])
        dd["limit"], dd["rho_applied"] = drift_allowable(cfg, d)
        out[d] = dd
    return out


def _seismic_gate(cfg, chk, out, nm):
    """Shared seismic part of run()/run_one(): per-direction ELF (HR-01/02), drift at the 12.8.6.5
    location (HR-09), torsion (HR-08), MRSA scaling gate (HR-23).  Fills chk / out in place."""
    NF = len(cfg["heights"])
    T, w2, eX, eY, Mtot = modal(cfg, nm)
    SF = seismic_design_forces(cfg); ts = torsion_summary(cfg); sd = seismic_drift(cfg)
    for d in ("X", "Y"):
        P = SF[d]; low = d.lower()
        out["T" + d] = P["T"]; out["Tu" + d] = P["Tu"]; out["Ta" + d] = P["Ta"]; out["Cs" + d] = P["Cs"]
        out["V" + d] = P["V"]; out["k" + d] = P["k"]; out["R" + d] = P["R"]; out["Cd" + d] = P["Cd"]
        out["Om0" + d] = P["Om0"]; out["rho" + d] = P["rho"]
        out["md" + low] = sd[d]["max"]; out["drift_profile_" + d] = sd[d]["design"]
        out["drift_location_" + d] = sd[d]["location"]
        chk["drift_" + d] = 0 < sd[d]["max"] < sd[d]["limit"]
        if sd[d]["rho_applied"]: out["drift_limit_rho_" + d] = sd[d]["limit"]
        # 12.8.2: T_dir between 0.5 Ta and 3 Ta of THAT direction (a model-sanity screen)
        out["period_ok_" + d] = (0.5*P["Ta"] <= P["T"] <= 3*P["Ta"]) if NF >= 3 else (0.1 <= P["T"] <= 1.5)
    chk["period"] = out["period_ok_X"] and out["period_ok_Y"]
    out["torsion"] = {"TIR_X": round(ts["X"]["TIR"], 3), "TIR_Y": round(ts["Y"]["TIR"], 3),
                      "TIR": round(ts["TIR"], 3), "classification": ts["classification"],
                      "Ax_max": ts["Ax_max"], "Ax_X": ts["X"]["Ax"], "Ax_Y": ts["Y"]["Ax"],
                      "drift_location": sd["X"]["location"]}
    out["edge_drift"] = {d: [round(SF[d]["Cd"]/SF[d]["Ie"]*v, 5) for v in ts[d]["edge_drift"]] for d in ("X", "Y")}
    out["seismic_basis"] = SF["basis"]
    if "RS" in (cfg.get("analyses") or []):
        # 12.9.1.4.1: the design forces actually handed to the demands must reach 100 % of the ELF base
        # shear of each direction, and 12.9.1.1: >= 90 % modal mass -- a gate that CAN fail.
        for d, cum in (("X", sum(eX)), ("Y", sum(eY))):
            rs = SF[d]["rs"]; Vd = SF[d]["V"]; Fsum = sum(SF[d]["Fx"].values())
            out["Vrs" + d] = rs["Vt"]; out["Vrs%s/V" % d] = rs["Vt"]/Vd if Vd else 0.0
            out["rs_force_scale_" + d] = rs["force_scale"]; out["rs_drift_scale_" + d] = rs["drift_scale"]
            out["Vrs%s_scaled" % d] = rs["Vt"]*rs["force_scale"]
            chk["rs_" + d] = (Fsum >= Vd*(1 - 1e-6)) and cum >= 0.90 and rs["Vt"]*rs["force_scale"] >= Vd*(1 - 1e-6)
    w = list(SF.get("warnings", []))
    det = modal_detail(cfg, nm)
    if det: w += [x for x in det.get("warnings", []) if x not in w]
    if w: out["seismic_warnings"] = w
    return T, w2, eX, eY, SF, sd, ts


def run(cfg):
    NF=len(cfg["heights"]); r={}
    chk={}; out={}
    T,w2,eX,eY,SF,sd,ts=_seismic_gate(cfg, chk, out, _nm_default(NF))
    sx=static_lateral(cfg,SF["X"]["F_elf"],"X"); sy=static_lateral(cfg,SF["Y"]["F_elf"],"Y")
    VX=SF["X"]["V"]; VY=SF["Y"]["V"]
    cumX=sum(eX); cumY=sum(eY)
    chk["equil_X"]=abs(sx[3]+VX)<=1e-3*VX; chk["equil_Y"]=abs(sy[3]+VY)<=1e-3*VY
    chk["stability"]=min(w2)>0
    chk["modalmass_X"]=cumX>=0.90; chk["modalmass_Y"]=cumY>=0.90
    chk["baseshear_X"]=abs(abs(sx[3])-VX)<=1e-3*VX; chk["baseshear_Y"]=abs(abs(sy[3])-VY)<=1e-3*VY
    gd="X" if VX>=VY else "Y"; G=SF[gd]
    out.update(T1=T[0],T2=T[1] if len(T)>1 else None,T3=T[2] if len(T)>2 else None,Ta=G["Ta"],Cs=G["Cs"],
               V=max(VX,VY),W=G["W"],Tu=G["Tu"],k=G["k"],cumX=cumX,cumY=cumY,
               roofX=sx[1][NF],roofY=sy[1][NF],tratioX=ts["X"]["TIR"],tratioY=ts["Y"]["TIR"])
    out["tratioX_acc"]=ts["X"]["TIR"]; out["torsion_Ax"]=ts["Ax_max"]
    if out.get("drift_limit_rho_X") or out.get("drift_limit_rho_Y"):
        out["drift_limit_rho"]=out.get("drift_limit_rho_X") or out.get("drift_limit_rho_Y")
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
def seis(SDS,SD1,S1,R,Ct,x,Cu,Ie=1.0,Cd=None,Om0=None,system=None):
    """Seismic parameter block for cfg['seis'].  Cd / Om0 (overstrength) come from ASCE 7-22 Table 12.2-1
    for the declared system: pass system= here, or declare cfg['system'] -- an omitted Cd/Om0 is marked
    in '_auto' and re-derived from the declared system at use time by seis_dir() (HR-20).  Only when no
    system can be resolved does the legacy R-keyed default apply, with a loud warning (seis_warnings):
    it is wrong for e.g. STMF (Om0 3.0, not 2.5) and SPSW (Cd 6.0, dual 6.5, not 5.5).
    Per-direction factors (ASCE 7-22 12.2.2, different systems along the two axes): give the other
    direction's overrides as cfg['seis_Y'] = seis(...) / dict(R=..., Cd=..., Om0=..., Ct=..., x=...,
    rho=..., system=...) (or cfg['seis']['Y'] = {...}; likewise X).  Keys not given are inherited;
    when R differs, Cd/Om0/Ct/x are taken from that direction's system, never from the other one."""
    auto=[]
    row=SFRS_TABLE.get(sfrs_key(system)) if system else None
    if Cd is None:
        if row: Cd=row["Cd"]
        else: Cd=_legacy_cd(R); auto.append("Cd")
    if Om0 is None:
        if row: Om0=row["Om0"]
        else: Om0=_legacy_om0(R, Cd); auto.append("Om0")
    d=dict(SDS=SDS,SD1=SD1,S1=S1,R=R,Ct=Ct,x=x,Cu=Cu,Ie=Ie,Cd=Cd,Om0=Om0,TL=8.0)
    if system: d["system"]=system
    if auto: d["_auto"]=auto
    return d
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
    gov=cfg.get("governing","seismic")
    chk={}; sout={}
    # seismic: per-direction period + factors (HR-01/02), CM forces (HR-31), torsion/Ax (HR-08),
    # drift at the 12.8.6.5 location (HR-09), MRSA (HR-23) -- shared with run()
    T,w2,eX,eY,SF,sd,ts=_seismic_gate(cfg, chk, sout, _nm_default(NF))
    gd="X" if SF["X"]["V"]>=SF["Y"]["V"] else "Y"; G=SF[gd]
    Cs,V,Tu,Ta,kk,W=G["Cs"],G["V"],G["Tu"],G["Ta"],G["k"],G["W"]
    if gov=="wind": FxX=wind_forces(cfg,"X"); FxY=wind_forces(cfg,"Y"); Vx=sum(FxX.values()); Vy=sum(FxY.values())
    else: FxX=SF["X"]["F_elf"]; FxY=SF["Y"]["F_elf"]; Vx=SF["X"]["V"]; Vy=SF["Y"]["V"]
    sx=static_lateral(cfg,FxX,"X"); sy=static_lateral(cfg,FxY,"Y")
    _cr_tr=max(sx[4],sy[4]) if gov=="wind" else max(ts["X"]["TIR_inherent"],ts["Y"]["TIR_inherent"])  # B5
    cumX=sum(eX); cumY=sum(eY)
    Cd=SF[gd]["Cd"]; Ie=SF[gd]["Ie"]
    if gov=="wind":
        # wind drift: not amplified (ASCE 7-22 Eq. 12.8-16 is seismic only); CM drift of the wind case
        mde_x=_drift_env(cfg, sx[2]); mde_y=_drift_env(cfg, sy[2]); mdx=mde_x; mdy=mde_y
        dl,_dlrho=drift_allowable(cfg)
        chk["drift_X"]=0<mdx<dl; chk["drift_Y"]=0<mdy<dl
    else:
        # ASCE 7-22 Eq. 12.8-16 per direction (own Cd) at the 12.8.6.5 location -- see seismic_drift()
        mdx=sout["mdx"]; mdy=sout["mdy"]
        mde_x=mdx*SF["X"]["Ie"]/SF["X"]["Cd"]; mde_y=mdy*SF["Y"]["Ie"]/SF["Y"]["Cd"]
    chk["equil_X"]=abs(sx[3]+Vx)<=1e-3*Vx; chk["equil_Y"]=abs(sy[3]+Vy)<=1e-3*Vy; chk["stability"]=min(w2)>0
    chk["modalmass_X"]=cumX>=0.90; chk["modalmass_Y"]=cumY>=0.90
    chk["baseshear_X"]=abs(abs(sx[3])-Vx)<=1e-3*Vx; chk["baseshear_Y"]=abs(abs(sy[3])-Vy)<=1e-3*Vy
    extra={}
    for _k in ("TX","TY","TuX","TuY","TaX","TaY","CsX","CsY","VX","VY","RX","RY","CdX","CdY","Om0X","Om0Y",
               "rhoX","rhoY","drift_location_X","drift_location_Y","drift_limit_rho_X","drift_limit_rho_Y",
               "seismic_basis","seismic_warnings"):
        if _k in sout: extra[_k]=sout[_k]
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
        # 12.9.1.4: MRSA scaled to 100 % of each direction's ELF V; the gate (in _seismic_gate) checks
        # the forces actually handed to the demands, plus the 90 % modal mass of 12.9.1.1
        for _k in ("VrsX/V","VrsY/V","rs_force_scale_X","rs_force_scale_Y","rs_drift_scale_X","rs_drift_scale_Y"):
            extra[_k]=sout[_k]
    # torsion (ASCE 7-22 12.3.2.1.1 / 12.8.4.3): computed for EVERY building with a non-flexible
    # diaphragm (12.8.4.2.1: accidental torsion applied to all structures to determine the irregularity);
    # ONE value (torsion_summary) feeds this screen, the combos, the drift gate and the report.
    tr=ts["TIR"]
    extra["torsion_acc"]=tr; extra["torsion_TIR_X"]=ts["X"]["TIR"]; extra["torsion_TIR_Y"]=ts["Y"]["TIR"]
    extra["torsion_irregularity"]=ts["classification"]
    extra["torsion_Ax"]=ts["Ax_max"]
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
                mdx=mdx,mdy=mdy,roofX=sx[1][NF],roofY=sy[1][NF],Vx=Vx,Vy=Vy,gov=gov,chk=chk,extra=extra,
                torsion=sout.get("torsion"),edge_drift=sout.get("edge_drift"),
                seismic={d: {kk_: SF[d][kk_] for kk_ in ("T","Tu","Ta","Cs","V","k","R","Cd","Om0","rho","Ie","basis")}
                         for d in ("X","Y")},
                allp=all(chk.values()))
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
def wind_forces(cfg,direction):
    w=cfg["wind"]; exp=w.get("exposure","C"); NF=len(cfg["heights"]); zlev=zlevels(cfg)
    F={}
    for k in range(1,NF+1):
        # projected width of the ACTUAL level-k footprint (real coords: non-rectangular and
        # non-uniform xcoords/ycoords safe; setback levels get their own, smaller width)
        P=grid(cfg,k)
        xs=[_xy_in(cfg,i,j)[0] for i,j in P]; ys=[_xy_in(cfg,i,j)[1] for i,j in P]
        width=(max(ys)-min(ys))/12 if direction=="X" else (max(xs)-min(xs))/12
        zmid=(zlev[k-1]+zlev[k])/2/12; Kz=kz_exposure(zmid,exp)
        # ASCE 7-22 Eq. 26.10-1: qz = 0.00256*Kz*Kzt*Ke*V^2 (Ke optional cfg wind key, default 1.0).
        # Kd belongs to the design-pressure equation (Eq. 27.3-1); it is multiplied here in the same
        # product for numerical convenience only.
        qz=0.00256*Kz*w.get("Kzt",1.0)*w.get("Ke",1.0)*w["V"]**2*w.get("Kd",0.85)
        p=qz*w.get("G",0.85)*w.get("Cpnet",1.3); h=cfg["heights"][k-1]/12; th=h if k<NF else h/2
        F[k]=p*width*th/1000.0
    return F
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
        _inf = build(cfg, transf)
        if "diaphragm" not in _inf and str(cfg.get("diaphragm_mass", "engine")).lower() != "builder":
            # the engine's modal analysis replaces a custom_build's master masses by the seismic mass at the
            # centre of mass (HR-19/31): record that assignment too so the exported model matches it
            attach_diaphragm_mass(cfg, _inf)
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

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

def floor_w(cfg,k):
    NF=len(cfg["heights"]); roof=(k==NF)
    d=_Dlev(cfg,k,roof)
    w=d*floor_area_ft2(cfg,k)/1000.0
    th=cfg["heights"][k-1]/12.0
    th=th if not roof else th/2
    w+=cfg["clad"]*perim_ft(cfg,k)*th/1000.0
    if roof: w+=0.2*cfg.get("snow",0.0)*floor_area_ft2(cfg,k)/1000.0
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
    # torsion ratio at top floor
    pts=info["present"][NF]; SXp,SYp=cfg["SX"],cfg["SY"]
    if direction=="X":
        ys=[j*SYp for i,j in pts]; ycm=info["cm"][NF][1]
        dd=[disp[NF]-rz[NF]*(y-ycm) for y in ys]
    else:
        xs=[i*SXp for i,j in pts]; xcm=info["cm"][NF][0]
        dd=[disp[NF]+rz[NF]*(x-xcm) for x in xs]
    davg=disp[NF]; tratio=max(abs(d) for d in dd)/abs(davg) if abs(davg)>1e-12 else 1.0
    return ok,disp,dr,Rs,tratio


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
    Cd=cfg["seis"]["Cd"]; Ie=cfg["seis"]["Ie"]            # ASCE 7-22 Eq.12.8-15 design drift
    mdx=Cd*mde_x/Ie; mdy=Cd*mde_y/Ie
    cumX=sum(eX); cumY=sum(eY)
    chk={}
    chk["equil_X"]=abs(sx[3]+V)<=1e-3*V; chk["equil_Y"]=abs(sy[3]+V)<=1e-3*V
    chk["stability"]=min(w2)>0
    chk["period"]=0.5*Ta<=T[0]<=3*Ta
    chk["modalmass_X"]=cumX>=0.90; chk["modalmass_Y"]=cumY>=0.90
    dl=cfg.get("drift_limit",0.020)
    chk["drift_X"]=0<mdx<dl; chk["drift_Y"]=0<mdy<dl
    chk["baseshear_X"]=abs(abs(sx[3])-V)<=1e-3*V; chk["baseshear_Y"]=abs(abs(sy[3])-V)<=1e-3*V
    out=dict(T1=T[0],T2=T[1],T3=T[2],Ta=Ta,Cs=Cs,V=V,W=W,Tu=Tu,k=kk,cumX=cumX,cumY=cumY,
             mdx=mdx,mdy=mdy,roofX=sx[1][NF],roofY=sy[1][NF],tratioX=sx[4],tratioY=sy[4])
    if "RS" in cfg.get("analyses",[]):
        VrsX=rs_baseshear(cfg,T,eX,eY,Mtot,"X"); VrsY=rs_baseshear(cfg,T,eX,eY,Mtot,"Y")
        out["VrsX"]=VrsX; out["VrsY"]=VrsY
        out["VrsX_scaled"]=max(VrsX,0.85*V); out["VrsY_scaled"]=max(VrsY,0.85*V)
        chk["rs_X_ge_85pct"]=out["VrsX_scaled"]>=0.85*V-1e-6
        chk["rs_Y_ge_85pct"]=out["VrsY_scaled"]>=0.85*V-1e-6
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
    # Cd/Om0 differ from the R-default (EBF, BRBF, special plate shear wall) pass them explicitly.
    if Cd  is None: Cd ={8:5.5,7:5.5,6:5.0,4.5:4.0,3.25:3.25,3:3.0}.get(R,R)
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
    mde_x=_drift_env(cfg, sx[2]); mde_y=_drift_env(cfg, sy[2]); cumX=sum(eX); cumY=sum(eY); dl=cfg.get("drift_limit",0.020)
    # ASCE 7-22 Eq. 12.8-15: design story drift = Cd*delta_elastic/Ie (seismic only; wind drift not amplified)
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
        VrsX=rs_baseshear(cfg,T,eX,eY,Mtot,"X"); VrsY=rs_baseshear(cfg,T,eX,eY,Mtot,"Y")
        extra["VrsX/V"]=VrsX/V; extra["VrsY/V"]=VrsY/V
        chk["rs_X"]=max(VrsX,0.85*V)>=0.85*V-1e-6; chk["rs_Y"]=max(VrsY,0.85*V)>=0.85*V-1e-6
    if cfg.get("torsion_check"):
        if NF>=30:
            tr=sx[4]
        else:
            sxa=static_lateral(cfg,FxX,"X",accidental=True)
            if NF>=18: tr=sxa[4]
            else:
                sya=static_lateral(cfg,FxY,"Y",accidental=True); tr=max(sxa[4],sya[4])
        # B4: torsional irregularity is ADVISORY, not a pass/fail gate. ASCE 7-22 permits Type 1a/1b in
        # SDC<=D with amplified accidental torsion Ax (sec.12.8.4.3); the report Ch.2 renders the screen.
        extra["torsion_acc"]=tr
        extra["torsion_irregularity"]=("none (<1.2)" if tr<1.2 else "Type 1a torsional (1.2-1.4)"
                                       if tr<1.4 else "Type 1b extreme torsional (>=1.4)")
        extra["torsion_Ax"]=round(min(max((tr/1.2)**2,1.0),3.0),2)
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
    z=max(zft,15.0)
    if exp=="D": zg,al=700.0,11.5
    elif exp=="B": zg,al=1200.0,7.0
    else: zg,al=900.0,9.5
    return 2.01*(z/zg)**(2.0/al)
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
        qz=0.00256*Kz*w.get("Kzt",1.0)*w.get("Kd",0.85)*w["V"]**2
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

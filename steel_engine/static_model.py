"""
static_model.py -- the STATIC building model (the second model).

The dynamic model (engine3d.build) LUMPS each floor's gravity at the joints -- correct for mass /
period / seismic base shear, but the beam ELEMENTS then carry ~0 gravity force, so internal force
diagrams cannot be drawn from it. This static model instead DISTRIBUTES the floor pressures onto the
beams as their true two-way (45-degree) tributary line loads -- triangular on the short bay edge,
trapezoidal on the long edge -- by sub-dividing every beam into `nseg` sub-elements with real
intermediate nodes and applying the exact stepped load. Every ASCE 7-22 LRFD combination is then
analysed statically with P-Delta, so N / V / M are correct everywhere for the force diagrams and the
gravity member demands. The dynamic model is untouched.

Geometry, sections, orientation, base fixity, diaphragm, per-member releases and the lateral story
forces are all replicated from engine3d so the two models are the same structure.
"""
import math
import openseespy.opensees as ops
import engine3d as eng
from engine3d import ntag, mtag, grid, zlevels, Ipack

EMOD = eng.E
GMOD = eng.Gmod

_SUB0 = 9_000_000          # base tag for intermediate beam nodes / sub-elements


def export_static_model(cfg, outdir, name="model", nseg=6):
    """Write a STANDALONE, runnable STATIC model (model_static.py) -- the SECOND model used for the force
    diagrams (beams sub-divided with the true two-way tributary gravity). Replays the exact OpenSees calls
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
          'The SECOND (force-diagram) model: beams sub-divided into sub-elements carrying the true two-way',
          'tributary gravity, so internal N / V / M are correct everywhere. Auto-generated; replays the exact',
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


def _staticize_custom(cfg, transf="PDelta", nseg=10):
    """Distribute gravity over an AGENT-BUILT (custom_build) model. We RECORD every OpenSees call the
    custom_build makes, then REPLAY it with the beams sub-divided into `nseg` elements so the true
    two-way tributary line loads produce correct internal-force diagrams -- the same treatment the
    parametric model gets. Relies on the custom_build conventions (ntag/mtag, beams on transf 3,
    release_args, returns the info dict with per-element kind+section)."""
    rec = {k: [] for k in ("node", "fix", "geomTransf", "element", "rigidDiaphragm", "mass", "uniaxialMaterial")}
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

    ops.wipe(); ops.model("basic", "-ndm", 3, "-ndf", 6)
    coord = {}
    for a in rec["node"]: ops.node(*a); coord[a[0]] = (a[1], a[2], a[3])
    for a in rec["fix"]: ops.fix(*a)
    _gt_seen = set()                       # a custom_build may register the SAME transf tag twice
    for a in rec["geomTransf"]:            # (explicit register_col_transf + add_*/_ensure auto-register);
        if a[1] in _gt_seen: continue      # the live build swallows the dup, but replay must not re-add it
        _gt_seen.add(a[1]); ops.geomTransf(*a)
    for a in rec["uniaxialMaterial"]: ops.uniaxialMaterial(*a)

    def dec(t): r = t % 100000; return r // 100, r % 100, t // 100000
    einfos = info.get("ele", [])
    sub_node = _SUB0; sub_ele = _SUB0; beams = []; cols = []; braces = []; dia_extra = {}
    for idx, a in enumerate(rec["element"]):
        kind = einfos[idx][1] if idx < len(einfos) else None
        sec = einfos[idx][2] if idx < len(einfos) else None
        if a[0] == "elasticBeamColumn" and kind == "beam":
            n1, n2 = a[2], a[3]; props = a[4:10]; ttag = a[10]; relz, rely = _parse_rel(a[11:])
            (x1, y1, z1) = coord[n1]; (x2, y2, z2) = coord[n2]
            L = ((x2-x1)**2 + (y2-y1)**2 + (z2-z1)**2) ** 0.5
            chain = [n1]
            for sgi in range(1, nseg):
                t = sgi/float(nseg); nd = sub_node; sub_node += 1
                ops.node(nd, x1+(x2-x1)*t, y1+(y2-y1)*t, z1+(z2-z1)*t)
                dia_extra.setdefault(round(z1, 6), []).append(nd); chain.append(nd)
            chain.append(n2); segs = []
            for sgi in range(nseg):
                ra = []
                if sgi == 0: ra += eng.release_args(*_end_rel(relz, rely, "I"))
                if sgi == nseg-1: ra += eng.release_args(*_end_rel(relz, rely, "J"))
                te = sub_ele; sub_ele += 1
                ops.element("elasticBeamColumn", te, chain[sgi], chain[sgi+1], *props, ttag, *ra); segs.append(te)
            i, j, k = dec(n1); dirn = "X" if abs(x2-x1) >= abs(y2-y1) else "Y"
            beams.append({"i": i, "j": j, "k": k, "dir": dirn, "L": L, "A": n1, "B": n2,
                          "nodes": chain, "segs": segs, "sec": sec, "relz": relz, "rely": rely})
        else:
            ops.element(*a)
            if kind == "col":
                i, j, k = dec(a[2]); cols.append({"tag": a[1], "sec": sec, "n1": a[2], "n2": a[3],
                                                  "i": i, "j": j, "k": k, "axis": "col"})
            elif kind == "brace":
                braces.append({"tag": a[1], "sec": sec, "n1": a[2], "n2": a[3]})
    for a in rec["rigidDiaphragm"]:
        master = a[1]; slaves = list(a[2:])
        mz = round(coord[master][2], 6) if master in coord else None
        ops.rigidDiaphragm(a[0], master, *(slaves + dia_extra.get(mz, [])))

    NF = len(cfg["heights"]); NX, NY = cfg["NX"], cfg["NY"]; present = {}
    for t, (x, y, zz) in coord.items():
        i, j, k = dec(t)
        if 0 <= i <= NX and 0 <= j <= NY: present.setdefault(k, set()).add((i, j))
    for k in range(NF+1): present.setdefault(k, set())
    bases = {}
    for a in rec["fix"]:
        if len(a) >= 7:
            i, j, k = dec(a[0])
            if k == 0 and 0 <= i <= NX and 0 <= j <= NY:
                bases[(i, j)] = "fixed" if a[4] == 1 else "pinned"   # a[4] = rotational restraint rx
    return {"cm": info.get("cm", {}), "present": present, "z": zlevels(cfg), "NF": NF,
            "cols": cols, "beams": beams, "braces": braces, "bases": bases}

def build_static(cfg, transf="PDelta", nseg=10):
    """Build the static model. Beams are sub-divided into `nseg` sub-elements with real intermediate
    nodes; columns and braces stay single elements. Returns a dict describing the model so loads can
    be applied and per-member diagrams reassembled."""
    if cfg.get("custom_build"):
        return _staticize_custom(cfg, transf, nseg)
    ops.wipe(); ops.model("basic", "-ndm", 3, "-ndf", 6)
    NX, NY = cfg["NX"], cfg["NY"]; SX, SY = cfg["SX"], cfg["SY"]
    z = zlevels(cfg); NF = len(cfg["heights"])
    present = {k: grid(cfg, k) for k in range(NF+1)}

    # primary (column-line) nodes
    for k in range(NF+1):
        for (i, j) in present[k]:
            x, y = _XY(cfg, i, j); ops.node(ntag(i, j, k), x, y, z[k])
    base = cfg.get("base", "fixed")
    for (i, j) in present[0]:
        if base == "pinned": ops.fix(ntag(i, j, 0), 1, 1, 1, 0, 0, 0)
        else:                ops.fix(ntag(i, j, 0), 1, 1, 1, 1, 1, 1)

    # diaphragm masters
    cm = {}
    for k in range(1, NF+1):
        pts = present[k]
        cx = sum(_XY(cfg, i, j)[0] for i, j in pts)/len(pts)
        cy = sum(_XY(cfg, i, j)[1] for i, j in pts)/len(pts)
        cm[k] = (cx, cy); ops.node(mtag(k), cx, cy, z[k]); ops.fix(mtag(k), 0, 0, 1, 1, 1, 0)

    cT = "PDelta" if transf == "PDelta" else "Linear"
    ops.geomTransf(cT, 1, 1.0, 0.0, 0.0); ops.geomTransf(cT, 2, 0.0, 1.0, 0.0)
    ops.geomTransf("Linear", 3, 0.0, 0.0, 1.0)
    cA, cIx, cIy, cJ = Ipack(cfg["col"]); bA, bIx, bIy, bJ = Ipack(cfg["beam"])
    relf = cfg.get("releases")

    et = 1; cols = []; beams = []
    sub_node = _SUB0; sub_ele = _SUB0
    dia_extra = {k: [] for k in range(1, NF+1)}   # interior beam nodes to add to each diaphragm

    # columns (single elements, exactly as the dynamic model)
    for i in range(NX+1):
        for j in range(NY+1):
            tt = 2 if (i == 0 or i == NX) else 1
            for k in range(NF):
                if (i, j) in present[k] and (i, j) in present[k+1]:
                    ops.element("elasticBeamColumn", et, ntag(i, j, k), ntag(i, j, k+1),
                                cA, EMOD, GMOD, cJ, cIy, cIx, tt)
                    cols.append({"tag": et, "sec": cfg["col"], "n1": ntag(i, j, k), "n2": ntag(i, j, k+1),
                                 "i": i, "j": j, "k": k, "axis": "col"})
                    et += 1

    def _add_beam(i, j, k, dirn, A, B):
        nonlocal et, sub_node, sub_ele
        xa, ya = ops.nodeCoord(A)[0], ops.nodeCoord(A)[1]
        xb, yb = ops.nodeCoord(B)[0], ops.nodeCoord(B)[1]
        zk = z[k]; L = math.hypot(xb-xa, yb-ya)
        relz, rely = (relf(i, j, k, dirn) if relf else ("none", "none"))
        # interior nodes
        chain = [A]
        for s in range(1, nseg):
            t = s/float(nseg)
            nd = sub_node; sub_node += 1
            ops.node(nd, xa+(xb-xa)*t, ya+(yb-ya)*t, zk)
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
                ra2 = eng.release_args(rj_z, rj_y)
                ra = ra + ra2
            tag = sub_ele; sub_ele += 1
            ops.element("elasticBeamColumn", tag, n1, n2, bA, EMOD, GMOD, bJ, bIx, bIy, 3, *ra)
            seg_tags.append(tag)
        beams.append({"sec": cfg["beam"], "i": i, "j": j, "k": k, "dir": dirn, "L": L,
                      "A": A, "B": B, "nodes": chain, "segs": seg_tags})

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
        ops.uniaxialMaterial("Elastic", 1, EMOD); brA = eng.HSS[cfg["brace"]]
        for k in range(1, NF+1):
            for (dirn, i, j) in cfg["braces"](k, NX, NY):
                a = (i, j); b = (i+1, j) if dirn == "X" else (i, j+1)
                if a in present[k-1] and b in present[k]:
                    n1, n2 = ntag(a[0], a[1], k-1), ntag(b[0], b[1], k)
                    ops.element("Truss", et, n1, n2, brA, 1)
                    braces.append({"tag": et, "sec": cfg.get("brace"), "n1": n1, "n2": n2}); et += 1
                if a in present[k] and b in present[k-1]:
                    n1, n2 = ntag(a[0], a[1], k), ntag(b[0], b[1], k-1)
                    ops.element("Truss", et, n1, n2, brA, 1)
                    braces.append({"tag": et, "sec": cfg.get("brace"), "n1": n1, "n2": n2}); et += 1

    # rigid diaphragm (corner nodes + interior beam nodes), no mass (static)
    for k in range(1, NF+1):
        sl = [ntag(i, j, k) for (i, j) in present[k]] + dia_extra[k]
        ops.rigidDiaphragm(3, mtag(k), *sl)

    return {"cm": cm, "present": present, "z": z, "NF": NF,
            "cols": cols, "beams": beams, "braces": braces}


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


def apply_gravity(cfg, model, fD, fL, fLr):
    """Apply the true two-way tributary gravity (kip/in, local z down) to every beam sub-element,
    plus cladding line load on perimeter beams. Returns the total applied vertical load (kip)."""
    NF = model["NF"]; SX, SY = cfg["SX"], cfg["SY"]
    heights = cfg["heights"]; clad = cfg.get("clad", 0.0)
    extra = cfg.get("extra_mass_floors", {})
    total = 0.0
    for b in model["beams"]:
        i, j, k, dirn, L = b["i"], b["j"], b["k"], b["dir"], b["L"]
        if not (1 <= k <= NF):
            continue                        # non-grid beam (brace apex / custom node) -> no floor tributary gravity
        roof = (k == NF)
        pD = (cfg["D_roof"] if roof else cfg["D_floor"]) + extra.get(k, 0.0)
        pL = 0.0 if roof else cfg["L_floor"]
        pLr = (cfg.get("snow") or 20.0) if roof else 0.0
        p = fD*pD + fL*pL + fLr*pLr                       # psf
        nb = _bays_adjacent(model["present"].get(k, set()), i, j, dirn)
        other = SY if dirn == "X" else SX                 # perpendicular bay dim (in)
        wcap = other/2.0
        # cladding (dead only) on perimeter beams (bounding a single bay)
        th = heights[k-1]/12.0; th = th/2.0 if roof else th
        wclad = fD*clad*th/12000.0 if (clad and nb == 1) else 0.0   # kip/in
        segs = b["segs"]
        for s, tag in enumerate(segs):
            s0 = L*s/len(segs); s1 = L*(s+1)/len(segs); smid = 0.5*(s0+s1)
            width_in = min(smid, L-smid, wcap)            # tributary half-width at smid (in)
            w = nb * p * (width_in/12.0) / 12000.0 + wclad  # psf*ft -> kip/in
            ops.eleLoad("-ele", tag, "-type", "-beamUniform", 0.0, -w, 0.0)
            total += w*(s1-s0)
    return total


def apply_lateral(lateral):
    for k, (fx, fy, mz) in lateral.items():
        ops.load(mtag(k), fx, fy, 0.0, 0.0, 0.0, mz)


def _solve():
    ops.constraints("Transformation"); ops.numberer("RCM"); ops.system("UmfPack")
    ops.test("NormDispIncr", 1e-7, 200); ops.algorithm("Newton")
    ops.integrator("LoadControl", 1.0); ops.analysis("Static")
    return ops.analyze(1)


def run_combo(cfg, fD, fL, fLr, lateral, transf="PDelta", nseg=10):
    """Build + analyse ONE LRFD combination statically with P-Delta. Returns (model, applied_kip, ok)."""
    model = build_static(cfg, transf, nseg)
    ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
    applied = apply_gravity(cfg, model, fD, fL, fLr)
    apply_lateral(lateral)
    ok = _solve()
    return model, applied, ok


# ====================================================================================
# DEMAND ENVELOPE on the single distributed static model  (replaces design_post.run_case
# for the gravity-correct column axial / base reactions).  Beam gravity moment follows the
# cfg["floor_system"] convention: "one-way" (default, composite steel deck->fillers->girder ->
# conservative w*L^2/8 on the girder) or "two-way" (slab 45-deg tributary -> the solved moment).
# Two-key DISK cache (jobs/<name>/design/): gravity envelope keyed sectionless (size-invariant when
# the gravity path is determinate) so it is computed ONCE and reused across resizes; seismic envelope
# keyed on the LATERAL members' sections + mass, so it is reused while only gravity members change.
# ====================================================================================
import hashlib as _hashlib, json as _json, os as _os

def _one_way_grav(cfg, b, fD, fL, fLr):
    """One-way girder gravity moment/shear (w*L^2/8, w*L/2) over the full perpendicular-bay tributary
    -- the realistic design moment for a one-way composite floor (deck -> filler beams -> girder)."""
    k = b["k"]; NF = len(cfg["heights"]); roof = (k >= NF); L = b["L"]
    trib = cfg["SY"] if b["dir"] == "X" else cfg["SX"]
    Dp = cfg["D_roof"] if roof else cfg["D_floor"]; Lp = 0.0 if roof else cfg["L_floor"]
    LrS = ((cfg.get("snow", 0.0) or cfg.get("Lr", 20.0)) if roof else 0.0)
    w = (fD*Dp + fL*Lp + fLr*LrS)/1000.0/144.0*trib
    return w*L*L/8.0, w*L/2.0

def _member_kinds(model):
    """fset(corner nodes) -> ('col'|'beam'|'brace', section)."""
    kinds = {}
    for c in model["cols"]:   kinds[frozenset((c["n1"], c["n2"]))] = ("col", c.get("sec"))
    for b in model["beams"]:  kinds[frozenset((b["A"], b["B"]))]   = ("beam", b.get("sec"))
    for b in model["braces"]: kinds[frozenset((b["n1"], b["n2"]))] = ("brace", b.get("sec"))
    return kinds

def _extract(model, cfg, fD, fL, fLr, floor_system):
    """Per-PARENT-member demands {fset(corner nodes): (N, Mz, My, V)} from the solved static model."""
    out = {}
    for c in model["cols"]:
        bf = ops.basicForce(c["tag"]); N = bf[0]
        m_z = max(abs(bf[1]), abs(bf[2])); m_y = max(abs(bf[3]), abs(bf[4]))
        L = max(1e-6, abs(ops.nodeCoord(c["n2"])[2]-ops.nodeCoord(c["n1"])[2]))
        V = max((abs(bf[1])+abs(bf[2]))/L, (abs(bf[3])+abs(bf[4]))/L)
        out[frozenset((c["n1"], c["n2"]))] = (N, m_z, m_y, V)             # column strong axis = local z
    for b in model["braces"]:
        out[frozenset((b["n1"], b["n2"]))] = (ops.basicForce(b["tag"])[0], 0.0, 0.0, 0.0)
    for b in model["beams"]:
        Nb = Mmaj = Mmin = 0.0
        for t in b["segs"]:                                # envelope moment along the real diagram
            f = ops.basicForce(t)
            Nb = max(Nb, abs(f[0]))
            mz = max(abs(f[1]), abs(f[2])); my = max(abs(f[3]), abs(f[4]))
            Mmaj = max(Mmaj, max(mz, my)); Mmin = max(Mmin, min(mz, my))
        Mg, Vg = _one_way_grav(cfg, b, fD, fL, fLr)
        if floor_system != "two-way":
            Mmaj = max(Mmaj, Mg)                            # one-way girder design moment (conservative)
        out[frozenset((b["A"], b["B"]))] = (Nb, Mmaj, Mmin, Vg)   # Vg = simple-span design shear (wL/2)
    return out

def _solve_case(cfg, case, nseg, floor_system):
    label, fD, fL, fLr, lat, col_only = case
    model = build_static(cfg, "PDelta", nseg)
    ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
    apply_gravity(cfg, model, fD, fL, fLr); apply_lateral(lat); _solve()
    return _extract(model, cfg, fD, fL, fLr, floor_system), _member_kinds(model)

def _merge(env, kinds, res, label, col_only):
    """Fold one case's per-member result into the running envelope (respecting col_only)."""
    for fs, (N, Mz, My, V) in res.items():
        kind = kinds.get(fs, ("beam", None))[0]
        if col_only and kind != "col":
            continue
        e = env.setdefault(fs, dict(comp=0.0, tens=0.0, Mz=0.0, My=0.0, V=0.0, combo="", score=-1.0))
        e["comp"] = max(e["comp"], max(-N, 0.0)); e["tens"] = max(e["tens"], max(N, 0.0))
        e["Mz"] = max(e["Mz"], abs(Mz)); e["My"] = max(e["My"], abs(My)); e["V"] = max(e["V"], abs(V))
        sc = abs(N) if kind in ("col", "brace") else abs(Mz)
        if sc > e["score"]: e["score"] = sc; e["combo"] = label

def _grav_key(cfg, nseg, floor_system, determinate, sec_sig):
    base = dict(NX=cfg["NX"], NY=cfg["NY"], SX=cfg["SX"], SY=cfg["SY"], H=tuple(cfg["heights"]),
                base=cfg.get("base", "fixed"), D=cfg.get("D_floor"), Dr=cfg.get("D_roof"),
                clad=cfg.get("clad"), L=cfg.get("L_floor"), Lr=cfg.get("Lr"), snow=cfg.get("snow"),
                xtra=tuple(sorted((cfg.get("extra_mass_floors") or {}).items())),
                fs=floor_system, nseg=nseg)
    if not determinate:                  # indeterminate (moment-frame) gravity -> size-dependent
        base["sec"] = sec_sig
    return "G" + _hashlib.md5(repr(sorted(base.items())).encode()).hexdigest()

def _seis_key(cfg, nseg, floor_system, lat_sig):
    base = dict(NX=cfg["NX"], NY=cfg["NY"], SX=cfg["SX"], SY=cfg["SY"], H=tuple(cfg["heights"]),
                base=cfg.get("base", "fixed"), seis=tuple(sorted((cfg.get("seis") or {}).items())),
                gov=cfg.get("governing"), wind=bool(cfg.get("wind")), rho=cfg.get("rho"),
                D=cfg.get("D_floor"), Dr=cfg.get("D_roof"), clad=cfg.get("clad"),
                fs=floor_system, nseg=nseg, lat=lat_sig)
    return "S" + _hashlib.md5(repr(sorted(base.items())).encode()).hexdigest()

def _cache_load(cache_dir, key):
    if not cache_dir: return None
    p = _os.path.join(cache_dir, "_demand_cache.json")
    try:
        d = _json.load(open(p))
        if d.get("key") == key:
            return {frozenset(int(x) for x in k.split("|")): v for k, v in d["env"].items()}
    except Exception:
        pass
    return None

def _cache_save(cache_dir, key, env):
    if not cache_dir: return
    try:
        _os.makedirs(cache_dir, exist_ok=True)
        ser = {"|".join(str(n) for n in fs): v for fs, v in env.items()}
        _json.dump({"key": key, "env": ser}, open(_os.path.join(cache_dir, "_demand_cache.json"), "w"))
    except Exception:
        pass

def demand_envelope(cfg, cases, nseg=6, floor_system=None, determinate=True,
                    sec_sig="", lat_sig="", cache_dir=None):
    """Per-member DEMAND envelope from the SINGLE distributed static model, with the two-key disk
    cache. Returns (env, kinds): env[fset] = {comp,tens,Mz,My,V,combo}; kinds[fset] = (kind, sec).
    fset = frozenset of the member's two corner-grid nodes (matches engine3d.build element nodes)."""
    floor_system = (floor_system or cfg.get("floor_system") or "one-way")
    grav = [c for c in cases if not c[4]]            # lateral dict empty -> pure gravity
    seis = [c for c in cases if c[4]]
    kinds = _member_kinds(build_static(cfg, "Linear", 1))     # cheap topology map (nseg=1)
    # ---- gravity envelope (size-invariant when determinate): disk cache, compute ONCE ----
    gkey = _grav_key(cfg, nseg, floor_system, determinate, sec_sig)
    genv = _cache_load(cache_dir, gkey + "|grav")
    if genv is None:
        genv = {}
        for case in grav:
            res, _k = _solve_case(cfg, case, nseg, floor_system); _merge(genv, _k, res, case[0], case[5])
        _cache_save(cache_dir, gkey + "|grav", genv)
    # ---- seismic/wind envelope (keyed on lateral sections + mass): disk cache ----
    skey = _seis_key(cfg, nseg, floor_system, lat_sig)
    senv = _cache_load(cache_dir, skey + "|seis")
    if senv is None:
        senv = {}
        for case in seis:
            res, _k = _solve_case(cfg, case, nseg, floor_system); _merge(senv, _k, res, case[0], case[5])
        _cache_save(cache_dir, skey + "|seis", senv)
    # ---- merge the two envelopes per member ----
    env = {}
    for src in (genv, senv):
        for fs, e in src.items():
            d = env.setdefault(fs, dict(comp=0.0, tens=0.0, Mz=0.0, My=0.0, V=0.0, combo="", score=-1.0))
            for q in ("comp", "tens", "Mz", "My", "V"): d[q] = max(d[q], e[q])
            if e.get("score", -1.0) > d["score"]: d["score"] = e["score"]; d["combo"] = e["combo"]
    return env, kinds

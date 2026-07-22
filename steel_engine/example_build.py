"""
example_build.py  --  REFERENCE model builder (read this, then write your own).

You build the OpenSees model yourself, every run, by setting

    cfg["custom_build"] = my_build          # def my_build(cfg, transf): ...

`my_build` must build the model and RETURN the standard info dict

    {"cm": {k:(x,y)}, "present": {k:set((i,j))}, "z":[...], "NF":int,
     "ele":[(tag, kind, sec, n1, n2), ...]}      # kind in {"col","beam","brace"}

so the rest of the pipeline (demands, figures, report, static diagrams) runs on YOUR
model unchanged.  This file is a COMPLETE, working example for an ordinary rectangular
frame -- copy its structure and adapt the section groups, joints and geometry to your
building.  It is also what the engine falls back to when no custom_build is given.

KEY RULES this example follows (follow them too):
  * BEAMS IN BOTH DIRECTIONS, EVERY LEVEL.  A floor that has girders in X but not Y (or
    vice-versa) is an incomplete model and fails the model_complete gate.
  * VARIED SECTIONS.  Real buildings never use one column and one beam size everywhere.
    Drive sections from groups (story-group x exterior/interior for columns; roof/floor
    and bay for beams) -- here via the optional cfg callables `col_sec(i,j,k,perim)` and
    `beam_sec(i,j,k,dirn)`, with a single-section fallback.
  * RIGID vs PINNED joints are an explicit choice.  A moment connection is rigid; a shear
    (simple) connection releases the beam's MAJOR-axis moment -> add_beam(..., releases=
    ("both","none")).  Drive it from the optional callable `releases(i,j,k,dirn)`.
  * ORIENTATION IS AUTOMATIC.  Use engine3d.add_column / add_beam -- they register the
    correct geomTransf tags for you.  Do NOT hand-write ops.geomTransf(...).  A frame
    column's STRONG axis must lie in its frame's plane (strong_dir "X" for an E-W frame,
    "Y" for N-S).
"""
import openseespy.opensees as ops
import engine3d as eng


def _col_sec(cfg, i, j, k, perim):
    f = cfg.get("col_sec")
    return f(i, j, k, perim) if callable(f) else cfg["col"]     # fallback: one column section


def _beam_sec(cfg, i, j, k, dirn):
    f = cfg.get("beam_sec")
    return f(i, j, k, dirn) if callable(f) else cfg["beam"]     # fallback: one beam section


def _PLACEHOLDER_strong_dir(i, j, NX, NY):
    """!!! GENERIC PLACEHOLDER for the space-frame archetypes ONLY -- do NOT copy this line verbatim. !!!
    It orients the i=0/NX edge columns strong in X and all others strong in Y. A PERIMETER moment frame
    needs the OPPOSITE on at least one pair of edges: the A/H edge lines (x=0, x=NX) that resist the N-S/Y
    direction take strong_dir "Y"; the 1/6 edge lines (y=0, y=NY) that resist E-W/X take "X". Set strong_dir
    from YOUR RESOLVED FRAMING orientation check -- a wrong axis passes every other gate yet doubles drift.
    A custom_build may instead pass cfg["col_strong"] = f(i,j,NX,NY)."""
    return "X" if (i == 0 or i == NX) else "Y"


def example_build(cfg, transf="PDelta"):
    ops.wipe(); ops.model("basic", "-ndm", 3, "-ndf", 6)
    NX, NY = cfg["NX"], cfg["NY"]; SX, SY = cfg["SX"], cfg["SY"]
    z = eng.zlevels(cfg); NF = len(cfg["heights"])
    xco = cfg.get("xcoords"); yco = cfg.get("ycoords"); skew = cfg.get("skew", 0.0)
    def XY(i, j):                                               # plan coords (supports uneven bays / skew)
        return ((xco[i] if xco else i*SX) + skew*j, (yco[j] if yco else j*SY))

    # ---- nodes (present[k] is the set of grid points that exist at level k) ----
    present = {k: eng.grid(cfg, k) for k in range(NF+1)}
    for k in range(NF+1):
        for (i, j) in present[k]:
            x, y = XY(i, j); ops.node(eng.ntag(i, j, k), x, y, z[k])

    # ---- column bases ----
    base = cfg.get("base", "fixed")
    for (i, j) in present[0]:
        if base == "pinned": ops.fix(eng.ntag(i, j, 0), 1, 1, 1, 0, 0, 0)
        else:                ops.fix(eng.ntag(i, j, 0), 1, 1, 1, 1, 1, 1)

    # ---- one rigid-diaphragm master per floor, at the floor centroid ----
    cm = {}
    for k in range(1, NF+1):
        pts = present[k]
        cx = sum(XY(i, j)[0] for i, j in pts)/len(pts); cy = sum(XY(i, j)[1] for i, j in pts)/len(pts)
        cm[k] = (cx, cy); ops.node(eng.mtag(k), cx, cy, z[k]); ops.fix(eng.mtag(k), 0, 0, 1, 1, 1, 0)

    et = 1; eles = []
    relf = cfg.get("releases")                                  # optional f(i,j,k,dirn)->(relz,rely)

    # ---- columns (strong axis chosen per frame; add_column auto-registers transforms) ----
    for i in range(NX+1):
        for j in range(NY+1):
            perim = (i in (0, NX) or j in (0, NY))
            _cs = cfg.get("col_strong")                        # custom_build override; else the LOUD placeholder
            sd = _cs(i, j, NX, NY) if callable(_cs) else _PLACEHOLDER_strong_dir(i, j, NX, NY)
            for k in range(NF):
                if (i, j) in present[k] and (i, j) in present[k+1]:
                    sec = _col_sec(cfg, i, j, k+1, perim)
                    eng.add_column(et, eng.ntag(i, j, k), eng.ntag(i, j, k+1), sec, sd)
                    eles.append((et, "col", sec, eng.ntag(i, j, k), eng.ntag(i, j, k+1))); et += 1

    # ---- girders in BOTH directions on EVERY level ----
    for k in range(1, NF+1):
        P = present[k]
        for j in range(NY+1):                                   # X-direction girders
            for i in range(NX):
                if (i, j) in P and (i+1, j) in P:
                    sec = _beam_sec(cfg, i, j, k, "X"); rel = relf(i, j, k, "X") if relf else None
                    eng.add_beam(et, eng.ntag(i, j, k), eng.ntag(i+1, j, k), sec, releases=rel)
                    eles.append((et, "beam", sec, eng.ntag(i, j, k), eng.ntag(i+1, j, k))); et += 1
        for i in range(NX+1):                                   # Y-direction girders
            for j in range(NY):
                if (i, j) in P and (i, j+1) in P:
                    sec = _beam_sec(cfg, i, j, k, "Y"); rel = relf(i, j, k, "Y") if relf else None
                    eng.add_beam(et, eng.ntag(i, j, k), eng.ntag(i, j+1, k), sec, releases=rel)
                    eles.append((et, "beam", sec, eng.ntag(i, j, k), eng.ntag(i, j+1, k))); et += 1

    # ---- braces (optional; single concentric diagonal per braced bay/story) ----
    if cfg.get("braces"):
        ops.uniaxialMaterial("Elastic", 1, eng.E); brA = eng.HSS[cfg["brace"]]
        for k in range(1, NF+1):
            for (dirn, i, j) in cfg["braces"](k, NX, NY):
                a = (i, j); b = (i+1, j) if dirn == "X" else (i, j+1)
                if a in present[k-1] and b in present[k]:
                    ops.element("Truss", et, eng.ntag(*a, k-1), eng.ntag(*b, k), brA, 1)
                    eles.append((et, "brace", cfg["brace"], eng.ntag(*a, k-1), eng.ntag(*b, k))); et += 1
                if a in present[k] and b in present[k-1]:
                    ops.element("Truss", et, eng.ntag(*a, k), eng.ntag(*b, k-1), brA, 1)
                    eles.append((et, "brace", cfg["brace"], eng.ntag(*a, k), eng.ntag(*b, k-1))); et += 1

    # ---- rigid diaphragm + floor mass ----
    info = {"cm": cm, "present": present, "z": z, "NF": NF, "ele": eles}
    for k in range(1, NF+1):
        sl = [eng.ntag(i, j, k) for (i, j) in present[k]]
        ops.rigidDiaphragm(3, eng.mtag(k), *sl)
        w = eng.floor_w(cfg, k); m = w/eng.g
        pts = present[k]; xs = [XY(i, j)[0] for i, j in pts]; ys = [XY(i, j)[1] for i, j in pts]
        Bx = max(xs)-min(xs)+SX; By = max(ys)-min(ys)+SY
        ops.mass(eng.mtag(k), m, m, 0.0, 0.0, 0.0, m*(Bx**2+By**2)/12.0)
    return info

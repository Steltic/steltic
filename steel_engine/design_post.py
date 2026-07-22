"""
design_post.py  --  ANALYSIS / DEMAND extraction only. NO coded AISC 360 capacities.

This module deliberately contains NO AISC 360 member-design equations. The framework computes
structural DEMANDS (the OpenSees model, the ASCE 7 loads, the second-order P-Delta analysis);
the AISC 360-22 capacity checks (compression E3, tension D2, flexure F2-F6, shear G2,
beam-column interaction H1, the App.8 B2 amplifier, and the AISC 341 SCWB / Omega0 capacity
design) are NOT coded anywhere. The design agent must query the AISC 360 / 341 RAG, derive the
governing limit-state equation for each member itself, compute the capacity and D/C, cite the
clause, and write its own calc_package.json. There is no oracle to fall back on.

What this module provides:
  * run_case(cfg, fD, fL, fLr, lateral) -- analyse ONE factored load combination through proper
    P-Delta and return per-element DEMANDS {tag: (N, Mz, My, V)}. Pure structural analysis.
  * _beam_grav(...) -- the beam gravity span moment/shear added to the joint-lumped model
    (analysis bookkeeping, not a code check).

The ASCE 7-22 combination set + the per-member demand envelope live in repo-root
design_pipeline.py; the report scaffold lives in report.py. Neither computes a member capacity.
"""
import os, sys, math, csv, json
import openseespy.opensees as ops
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine3d as E
import sections as S


# ---------- beam gravity span moment/shear (analysis bookkeeping, NOT a code check) ----------
def _beam_grav(cfg, n1, n2, fD, fL, fLr):
    """Beam gravity span moment & shear (w*L^2/8, w*L/2) over the tributary bay, kip-in/kip.
    The engine lumps floor load at the nodes (good for drift/period), so beam gravity flexure
    must be added analytically to the per-member demand."""
    a = ops.nodeCoord(n1); b = ops.nodeCoord(n2); NF = len(cfg["heights"])
    k = n1 // 100000; roof = (k == NF)
    Lx = abs(a[0]-b[0]); Ly = abs(a[1]-b[1]); L = max(Lx, Ly)
    if cfg.get("lean_gravity"):
        # Perimeter lateral frame with gravity-only interior that LEANS on the frame
        # (AISC Design Example III-1 idealization). The full-bay floor gravity is carried by the
        # interior gravity columns (the joint-lumped nodal loads / leaning-column P-Delta path in
        # run_case), so the lateral-frame beams carry only the spandrel CLADDING line load.
        th = cfg["heights"][k-1]/12.0; th = th if not roof else th/2.0   # ft of wall tributary
        w = fD * cfg.get("clad", 0.0) * th / 1000.0 / 12.0               # kip/in (dead only)
        return w*L*L/8.0, w*L/2.0
    trib = cfg["SY"] if Lx >= Ly else cfg["SX"]
    Dp = cfg["D_roof"] if roof else cfg["D_floor"]
    Lp = 0.0 if roof else cfg["L_floor"]
    LrSp = (cfg.get("snow", 0.0) if cfg.get("snow", 0.0) > 0 else 20.0) if roof else 0.0
    w = (fD*Dp + fL*Lp + fLr*LrSp) / 1000.0 / 144.0 * trib     # kip/in
    return w*L*L/8.0, w*L/2.0


def run_case(cfg, fD, fL, fLr, lateral):
    """Analyse ONE factored load combination (supplied by the caller) under proper P-Delta and
    return per-element DEMANDS {tag: (N, Mz, My, V)}. The caller chooses the factors and the
    lateral pattern (ASCE 7-22 combinations: Ev in fD, rho, Omega0, 100/30, accidental torsion);
    this runs the actual nonlinear analysis, so no invalid superposition of factored results.
        fD, fL, fLr : dead / live / roof-live(or snow) load factors for THIS combination.
        lateral     : dict floor-> (fx, fy, mz) at the diaphragm master, scaled from the engine's
                      elementary patterns (E.elf seismic / E.wind_forces wind)."""
    info = E.build(cfg, "PDelta"); NF = info["NF"]; pres = info["present"]
    ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
    for k in range(1, NF+1):
        npts = len(pres[k])
        p = (fD*E.floor_dead(cfg, k) + fL*E.floor_live(cfg, k) + fLr*E.floor_roofLrS(cfg, k))/npts
        for (i, j) in pres[k]:
            ops.load(E.ntag(i, j, k), 0, 0, -p, 0, 0, 0)
    for k, (fx, fy, mz) in lateral.items():
        ops.load(E.mtag(k), fx, fy, 0, 0, 0, mz)
    ops.constraints("Transformation"); ops.numberer("RCM"); ops.system("UmfPack")
    ops.test("NormDispIncr", 1e-7, 200); ops.algorithm("Newton")
    ops.integrator("LoadControl", 1.0); ops.analysis("Static")
    ops.analyze(1)
    out = {}
    for (t, kind, sec, n1, n2) in info["ele"]:
        bf = ops.basicForce(t)
        if kind == "brace":
            out[t] = (bf[0], 0.0, 0.0, 0.0)            # (N, Mz, My, V); tension +
        else:
            c1, c2 = ops.nodeCoord(n1), ops.nodeCoord(n2)
            L = math.dist(c1, c2)
            N = bf[0]
            m_z = max(abs(bf[1]), abs(bf[2])); m_y = max(abs(bf[3]), abs(bf[4]))
            V = max((abs(bf[1])+abs(bf[2]))/L, (abs(bf[3])+abs(bf[4]))/L)
            if kind == "beam":
                # beam vertical/in-plane bending is the STRONG-axis demand (local-y basic moment);
                # add the gravity span moment (the model lumps floor gravity at the joints).
                Mg, Vg = _beam_grav(cfg, n1, n2, fD, fL, fLr)
                Mz = max(m_z, m_y) + Mg; My = min(m_z, m_y); V = max(V, Vg)
            else:                                   # column: strong axis = local z
                Mz = m_z; My = m_y
            out[t] = (N, Mz, My, V)
    return out, info

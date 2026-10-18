"""Static demand model + ASCE 7-22 combinations (HR-03/04/05/06/07/13/17/18/27/29/30/35/36).

Small hand-checkable models; run from the repo root:
    PYTHONPATH=$PWD:$PWD/steel_engine python -m pytest tests/test_static_demands.py -q
"""
import copy, math, os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "steel_engine")); sys.path.insert(0, ROOT)

import pytest  # noqa: E402

ops = pytest.importorskip("openseespy.opensees")
import engine3d as E  # noqa: E402
import static_model as SM  # noqa: E402
import design_pipeline as DP  # noqa: E402

SEIS = E.seis(1.0, 0.6, 0.6, 6, 0.02, 0.75, 1.4, Ie=1.0, Cd=5.0, Om0=2.0)


def _cfg(**kw):
    c = dict(arch="test", NX=2, NY=2, SX=360.0, SY=300.0, heights=[168.0, 168.0], base="fixed",
             col="W14X90", beam="W24X76", D_floor=80.0, D_roof=60.0, L_floor=50.0, Lr=20.0, clad=0.0,
             seis=copy.deepcopy(SEIS), rho=1.0, floor_system="two-way",
             releases=lambda i, j, k, d: ("both", "none"), dam=False)
    c.update(kw)
    return c


def _area(cfg, k):
    return E.floor_area_ft2(cfg, k)


# ------------------------------------------------------------------ HR-03 loads = seismic-weight loads
def test_gravity_matches_seismic_weight_and_lower_roofs():
    # 3 levels; level 3 only covers the bays with j == 0 -> the j == 1 bays of level 2 are a LOWER ROOF
    def plan(k, NX, NY):
        if k <= 2:
            return {(i, j) for i in range(NX + 1) for j in range(NY + 1)}
        return {(i, j) for i in range(NX + 1) for j in range(2)}
    cfg = _cfg(heights=[168.0] * 3, plan=plan, D_by_level={1: 95.0}, L_by_level={1: 125.0}, Lr=30.0,
               extra_mass_floors={2: 10.0}, clad=15.0)
    audit = SM.gravity_audit(cfg)
    for k, (st, w) in audit.items():
        assert st == pytest.approx(w, rel=1e-6), (k, st, w)          # dead incl. D_by_level/extra/cladding
    model = SM.build_static(cfg, "Linear", 4)
    roof = {k: sum(1 for (kk, i, j), b in model["bays"].items() if kk == k and b["roof"]) for k in (1, 2, 3)}
    assert roof == {1: 0, 2: 2, 3: 2}                                 # lower roof detected geometrically
    def total(fD, fL, fLr):
        SM._reset(); ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
        return SM.apply_case_gravity(cfg, model, fD, fL, fLr)["by_level"]
    live = total(0.0, 1.0, SM.RoofFactors())
    bay = 360.0 * 300.0 / 144.0
    assert live[1] == pytest.approx(125.0 * 4 * bay / 1000.0, rel=1e-9)     # L_by_level honoured
    assert live[2] == pytest.approx(50.0 * 2 * bay / 1000.0, rel=1e-9)      # only the covered bays
    roofl = total(0.0, 0.0, SM.RoofFactors(Lr=1.0, cfg=cfg))
    assert roofl[2] == pytest.approx(30.0 * 2 * bay / 1000.0, rel=1e-9)     # cfg['Lr'] on the lower roof
    assert roofl[3] == pytest.approx(30.0 * 2 * bay / 1000.0, rel=1e-9)
    assert 1 not in roofl


# ------------------------------------------------------------------ HR-13 / HR-29 split spans, tag map
def _split_build(order_reversed=False, unlisted=False):
    """2 x 1 bays, one storey; X beam (0,0)-(1,0) split at an off-grid node at 0.3 L. Pinned beams."""
    def cb(cfg, transf="PDelta"):
        ops.wipe(); ops.model("basic", "-ndm", 3, "-ndf", 6)
        z = E.zlevels(cfg); pres = {k: {(i, j) for i in range(3) for j in range(2)} for k in (0, 1)}
        for k in (0, 1):
            for (i, j) in pres[k]:
                ops.node(E.ntag(i, j, k), i * 360.0, j * 300.0, z[k])
        for (i, j) in pres[0]:
            ops.fix(E.ntag(i, j, 0), 1, 1, 1, 1, 1, 1)
        ops.node(E.mtag(1), 360.0, 150.0, z[1]); ops.fix(E.mtag(1), 0, 0, 1, 1, 1, 0)
        eles = []; et = 1
        for (i, j) in sorted(pres[0]):
            E.add_column(et, E.ntag(i, j, 0), E.ntag(i, j, 1), "W14X90", "X")
            eles.append((et, "col", "W14X90", E.ntag(i, j, 0), E.ntag(i, j, 1))); et += 1
        mid = 100000 + 80000
        ops.node(mid, 0.3 * 360.0, 0.0, z[1])
        P = ("both", "none")
        E.add_beam(et, E.ntag(0, 0, 1), mid, "W24X76", ("I", "none")); eles.append((et, "beam", "W24X76", E.ntag(0, 0, 1), mid)); et += 1
        E.add_beam(et, E.ntag(1, 0, 1), mid, "W21X62", ("I", "none")); eles.append((et, "beam", "W21X62", E.ntag(1, 0, 1), mid)); et += 1
        for (a, b) in (((1, 0), (2, 0)), ((0, 1), (1, 1)), ((1, 1), (2, 1)), ((0, 0), (0, 1)), ((1, 0), (1, 1)), ((2, 0), (2, 1))):
            E.add_beam(et, E.ntag(*a, 1), E.ntag(*b, 1), "W24X76", P)
            eles.append((et, "beam", "W24X76", E.ntag(*a, 1), E.ntag(*b, 1))); et += 1
        ops.rigidDiaphragm(3, E.mtag(1), *[E.ntag(i, j, 1) for (i, j) in pres[1]])
        if order_reversed:
            eles = eles[::-1]
        if unlisted:
            eles = eles[:-1]
        return {"cm": {1: (360.0, 150.0)}, "present": pres, "z": z, "NF": 1, "ele": eles}
    return cb


def test_split_beam_gets_full_tributary_and_tags_map_by_tag():
    cfg = _cfg(NX=2, NY=1, heights=[168.0], custom_build=_split_build(order_reversed=True))
    model = SM.build_static(cfg, "Linear", 4)
    split = [b for b in model["beams"] if b["parent"] == (1, "X", 0, 0)]
    assert len(split) == 2                                          # both pieces on grid span (0,0)-(1,0)
    secs = sorted(b["sec"] for b in split)
    assert secs == ["W21X62", "W24X76"]                            # HR-29: by tag, not list position
    assert all(c["sec"] == "W14X90" for c in model["cols"])
    SM._reset(); ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
    g = SM.apply_case_gravity(cfg, model, 1.0, 0.0, SM.RoofFactors())
    assert g["total"] == pytest.approx(60.0 * 2 * 360 * 300 / 144000.0, rel=1e-9)   # full roof area
    # the two pieces carry exactly what the unsplit span carries: triangle of height 150 in each side
    w0 = 60.0 / 144000.0
    exp_span = w0 * SM._tri_int(0.0, 360.0, 360.0, 150.0)
    got = sum(SM._piece_int(pc, 0.0, 360.0, 360.0) for pc, _o in
              SM._case_pieces(cfg, model, 1.0, 0.0, SM.RoofFactors())["span"][(1, "X", 0, 0)])
    assert got == pytest.approx(exp_span, rel=1e-9)


def test_unlisted_element_is_warned_not_mislabelled():
    cfg = _cfg(NX=2, NY=1, heights=[168.0], custom_build=_split_build(unlisted=True))
    model = SM.build_static(cfg, "Linear", 2)
    assert any("does not list" in w for w in model["warnings"])
    assert len(model["cols"]) == 6


# ------------------------------------------------------------------ HR-04 / HR-35 solved V, signed N
def test_beam_shear_is_solved_and_axial_keeps_sign():
    cfg = _cfg(NX=1, NY=1, heights=[144.0], releases=lambda i, j, k, d: ("none", "none"), D_roof=0.0, Lr=0.0)
    model = SM.build_static(cfg, "Linear", 4)
    SM._reset(); ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
    SM.apply_lateral({1: (100.0, 0.0, 0.0)}); SM._solve()
    res = SM._extract(model)
    # X beams of the two X-frames: shear from frame action, nothing from gravity (zero loads)
    xb = [b for b in model["beams"] if b["dir"] == "X"]
    for b in xb:
        r = res[frozenset((b["A"], b["B"]))]
        lf = ops.eleResponse(b["segs"][0], "localForce")
        assert r["V"] == pytest.approx(max(abs(lf[2]), abs(lf[8]), abs(lf[1]), abs(lf[7])), rel=1e-9)
        assert r["V"] > 1.0
    # signed axial: push one beam end node horizontally in a model without diaphragm
    def cb(cfg, transf="PDelta"):
        ops.wipe(); ops.model("basic", "-ndm", 3, "-ndf", 6)
        ops.node(E.ntag(0, 0, 1), 0.0, 0.0, 144.0); ops.node(E.ntag(1, 0, 1), 360.0, 0.0, 144.0)
        ops.fix(E.ntag(1, 0, 1), 1, 1, 1, 1, 1, 1); ops.fix(E.ntag(0, 0, 1), 0, 1, 1, 1, 1, 1)
        E.add_beam(1, E.ntag(0, 0, 1), E.ntag(1, 0, 1), "W24X76")
        return {"cm": {}, "present": {0: set(), 1: {(0, 0), (1, 0)}}, "z": [0, 144.0], "NF": 1,
                "ele": [(1, "beam", "W24X76", E.ntag(0, 0, 1), E.ntag(1, 0, 1))]}
    c2 = _cfg(NX=1, NY=0, heights=[144.0], custom_build=cb)
    m2 = SM.build_static(c2, "Linear", 3)
    SM._reset(); ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
    ops.load(E.ntag(0, 0, 1), 50.0, 0, 0, 0, 0, 0); SM._solve()
    r = SM._extract(m2)[frozenset((E.ntag(0, 0, 1), E.ntag(1, 0, 1)))]
    assert r["Nc"] == pytest.approx(50.0, rel=1e-6) and r["Nt"] == 0.0     # compression, not tension


# ------------------------------------------------------------------ HR-27 one-way tributary
def test_one_way_deck_span_distribution():
    # 1 bay 30 ft (X) x 25 ft (Y), deck spans X with virtual infill at 10 ft: Y beams carry 5 ft, the X
    # girders carry two point loads P = w*10ft*25ft/2 at the third points.
    cfg = _cfg(NX=1, NY=1, SX=360.0, SY=300.0, heights=[144.0], D_roof=100.0, Lr=0.0,
               floor_system="one-way", deck_span="X", infill_spacing=120.0)
    model = SM.build_static(cfg, "Linear", 6)
    pcs = SM._case_pieces(cfg, model, 1.0, 0.0, SM.RoofFactors())
    w = 100.0 / 144000.0
    yb = pcs["span"][(1, "Y", 0, 0)]
    assert len(yb) == 1 and yb[0][0][0] == "uni" and yb[0][0][1] == pytest.approx(w * 60.0)
    xg = pcs["span"][(1, "X", 0, 0)]
    pts = sorted(pc[1] for pc, _o in xg if pc[0] == "pt")
    assert pts == pytest.approx([120.0, 240.0])
    P = w * 120.0 * 300.0 / 2.0
    assert all(pc[2] == pytest.approx(P) for pc, _o in xg if pc[0] == "pt")
    SM._reset(); ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
    g = SM.apply_case_gravity(cfg, model, 1.0, 0.0, SM.RoofFactors())
    assert g["total"] == pytest.approx(100.0 * 360 * 300 / 144000.0, rel=1e-9)
    SM._solve()
    res = SM._extract(model)
    gb = next(b for b in model["beams"] if b["parent"] == (1, "X", 0, 0))
    assert res[frozenset((gb["A"], gb["B"]))]["Mz"] == pytest.approx(P * 120.0, rel=2e-3)   # P*L/3
    assert res[frozenset((gb["A"], gb["B"]))]["V"] == pytest.approx(P, rel=2e-3)


def test_default_one_way_girder_floor_uses_half_bay_per_side():
    # deck span undeclared: interior girder floor w = p*(SY/2 + SY/2), perimeter girder w = p*SY/2
    cfg = _cfg(NX=1, NY=2, heights=[144.0], D_roof=100.0, Lr=0.0, floor_system="one-way")
    model = SM.build_static(cfg, "Linear", 6)
    spec, _ = SM._floor_spec(cfg, model)
    pcs = SM._case_pieces(cfg, model, 1.0, 0.0, SM.RoofFactors())
    fl = SM._floors(cfg, model, pcs, spec)
    w = 100.0 / 144000.0
    for b in model["beams"]:
        if b["dir"] != "X":
            continue
        nb = 2 if b["j"] == 1 else 1
        Mg, Vg = fl[id(b)]
        assert Mg == pytest.approx(w * nb * 150.0 * 360.0 ** 2 / 8.0, rel=1e-3)
        assert Vg == pytest.approx(w * nb * 150.0 * 360.0 / 2.0, rel=2e-2)


# ------------------------------------------------------------------ HR-05 / HR-06 / HR-30 combinations
def test_combinations_roof_branches_live_factor_orthogonal_and_omega0():
    cfg = _cfg(snow=30.0, Lr=20.0, L_by_level={1: 125.0}, rho=1.3,
               wind=dict(V=115, exposure="C", Kd=0.85, G=0.85, Cpnet=1.3),
               releases=lambda i, j, k, d: ("none", "none"))
    cases = DP.combos(cfg)
    labels = [c[0] for c in cases]
    assert "1.2D+1.6L+0.5Lr" in labels and "1.2D+1.6L+0.3S" in labels
    assert any(l.startswith("1.2D+1.6Lr+") for l in labels) and any(l.startswith("1.2D+1.0S+") for l in labels)
    assert any("+0.5WX+" in l for l in labels)                                     # combo 3a with 0.5W
    c3 = next(c for c in cases if c[0].startswith("1.2D+1.6Lr+"))
    assert isinstance(c3[2], SM.LiveFactor) and c3[2].hi == 1.0
    assert SM._live_factor(c3[2], cfg, 1) == 1.0 and SM._live_factor(c3[2], cfg, 2) == 0.5   # Lo > 100 psf
    assert c3[3].Lr == 1.6 and c3[3].S == 0.0
    # legacy float = equivalent factor on 'snow' (top roof): 1.6*20/30
    assert float(c3[3]) == pytest.approx(1.6 * 20.0 / 30.0)
    seis = [c for c in cases if getattr(c, "kind", "") == "seismic" and "rhoEX+t+" in c[0]]
    fy = sorted({round(c[4][2][1], 6) for c in seis})
    assert fy[0] < 0 < fy[-1]                                                       # HR-30 both signs
    om = [c for c in cases if getattr(c, "kind", "") == "omega0"]
    assert om and all(c[5] and c.applies == {"col"} for c in om)                    # HR-06 moment frame
    up = [c for c in cases if getattr(c, "uplift", None)]
    assert up and all(c[0].startswith("0.9D+1.0W") for c in up)


# ------------------------------------------------------------------ HR-07 capacity-limited
def _chevron_build(cfg, transf="PDelta"):
    """1 bay x 1 bay, 1 storey; inverted-V SCBF in the X bay at j = 0 (HSS6X6X1/2), apex split beam."""
    ops.wipe(); ops.model("basic", "-ndm", 3, "-ndf", 6)
    z = E.zlevels(cfg); pres = {k: {(i, j) for i in range(2) for j in range(2)} for k in (0, 1)}
    for k in (0, 1):
        for (i, j) in pres[k]:
            ops.node(E.ntag(i, j, k), i * 360.0, j * 300.0, z[k])
    for (i, j) in pres[0]:
        ops.fix(E.ntag(i, j, 0), 1, 1, 1, 0, 0, 0)
    ops.node(E.mtag(1), 180.0, 150.0, z[1]); ops.fix(E.mtag(1), 0, 0, 1, 1, 1, 0)
    eles = []; et = 1
    for (i, j) in sorted(pres[0]):
        E.add_column(et, E.ntag(i, j, 0), E.ntag(i, j, 1), "W14X90", "X")
        eles.append((et, "col", "W14X90", E.ntag(i, j, 0), E.ntag(i, j, 1))); et += 1
    ap = 180000
    ops.node(ap, 180.0, 0.0, z[1])
    E.add_beam(et, E.ntag(0, 0, 1), ap, "W24X76", ("I", "none")); eles.append((et, "beam", "W24X76", E.ntag(0, 0, 1), ap)); et += 1
    E.add_beam(et, E.ntag(1, 0, 1), ap, "W24X76", ("I", "none")); eles.append((et, "beam", "W24X76", E.ntag(1, 0, 1), ap)); et += 1
    for (a, b) in (((0, 1), (1, 1)), ((0, 0), (0, 1)), ((1, 0), (1, 1))):
        E.add_beam(et, E.ntag(*a, 1), E.ntag(*b, 1), "W24X76", ("both", "none"))
        eles.append((et, "beam", "W24X76", E.ntag(*a, 1), E.ntag(*b, 1))); et += 1
    ops.uniaxialMaterial("Elastic", 1, E.E)
    A = E.HSS["HSS6X6X1/2"]
    for (i, j) in ((0, 0), (1, 0)):
        ops.element("Truss", et, E.ntag(i, j, 0), ap, A, 1); eles.append((et, "brace", "HSS6X6X1/2", E.ntag(i, j, 0), ap)); et += 1
    # Y direction: one more chevron-free single diagonal on grid 0 so the model is stable in Y
    ops.element("Truss", et, E.ntag(0, 0, 0), E.ntag(0, 1, 1), A, 1); eles.append((et, "brace", "HSS6X6X1/2", E.ntag(0, 0, 0), E.ntag(0, 1, 1))); et += 1
    ops.element("Truss", et, E.ntag(1, 1, 0), E.ntag(1, 0, 1), A, 1); eles.append((et, "brace", "HSS6X6X1/2", E.ntag(1, 1, 0), E.ntag(1, 0, 1))); et += 1
    ops.rigidDiaphragm(3, E.mtag(1), *[E.ntag(i, j, 1) for (i, j) in pres[1]])
    return {"cm": {1: (180.0, 150.0)}, "present": pres, "z": z, "NF": 1, "ele": eles}


def test_scbf_expected_strengths_and_chevron_unbalanced_load():
    cfg = _cfg(NX=1, NY=1, heights=[156.0], custom_build=_chevron_build, system="SCBF", D_roof=0.0, Lr=0.0)
    model = SM.build_static(cfg, "Linear", 4)
    br = next(b for b in model["braces"] if b["n2"] == 180000)
    cap = SM.brace_capacity(cfg, model, br)
    row = SM._shape("HSS6X6X1/2")
    L = math.hypot(180.0, 156.0)
    T = 1.3 * 50.0 * row["A"]
    r = min(row["rx"], row["ry"]); Fe = math.pi ** 2 * 29000.0 / (L / r) ** 2
    Fne = 0.658 ** (65.0 / Fe) * 65.0 if 65.0 / Fe <= 2.25 else 0.877 * Fe
    C = min(T, Fne * row["A"] / 0.877)
    assert cap["T"] == pytest.approx(T) and cap["C"] == pytest.approx(C) and cap["Cpb"] == pytest.approx(0.3 * C)
    # Ecl (b): unbalanced vertical (T - 0.3C) sin(theta) at the apex of a 30-ft simply supported beam
    pat = {1: (100.0, 0.0, 0.0)}; paty = {1: (0.0, 100.0, 0.0)}
    case = SM.Case("Ecl", 0.0, 0.0, SM.RoofFactors(), {}, True, kind="ecl",
                   ecl=dict(sx=1, sy=1, variant="b", patterns=(pat, paty), Om0=2.0))
    info = {}
    env, kinds = SM.demand_envelope(cfg, [case], nseg=4, info_out=info)
    # the only NOT EVALUATED item: the Ecl strut axial of the two Y beams (8, 9) whose single diagonals end
    # at diaphragm-slaved nodes (VERIFY: AISC 341-22 F2.3 'struts'); the chevron beams (free apex) are evaluated
    assert len(info["not_evaluated"]) == 1 and "Ecl axial force in 2 braced-bay beam(s)" in info["not_evaluated"][0]
    assert "element" not in info["not_evaluated"][0]          # no internal element ids in the published note
    sin = 156.0 / L
    Pu = (T - 0.3 * C) * sin
    beam = next(fs for fs, kv in kinds.items() if kv[0] == "beam" and 180000 in fs)
    assert env[beam]["Mz"] == pytest.approx(Pu * 360.0 / 4.0, rel=0.02)          # P L / 4
    assert env[beam]["V"] == pytest.approx(Pu / 2.0, rel=0.02)
    # beam axial = horizontal components delivered at the apex into the (restrained) diaphragm
    assert max(env[beam]["comp"], env[beam]["tens"]) > 0.5 * (T + 0.3 * C) * 180.0 / L


def test_ebf_link_and_brb_strengths():
    row = SM._shape("W18X76")
    Vc, note = SM._link_capacity({}, {"sec": "W18X76", "L": 36.0})
    Vp = 0.6 * 50 * (row["d"] - 2 * row["tf"]) * row["tw"]; Mp = 50 * row["Zx"]
    assert Vc == pytest.approx(1.25 * 1.1 * min(Vp, 2 * Mp / 36.0))
    cfg = {"system": "BRBF", "brb": {"Fysc": 38.0, "Ry": 1.1, "beta": 1.15, "omega": 1.5}}
    m = {"coord": {1: (0.0, 0.0, 0.0), 2: (300.0, 0.0, 150.0)}}
    cap = SM.brace_capacity(cfg, m, {"sec": "BRB-Asc6.5", "n1": 1, "n2": 2, "area": 9.1})
    assert cap["T"] == pytest.approx(1.5 * 1.1 * 38.0 * 6.5) and cap["C"] == pytest.approx(1.15 * cap["T"])
    cap2 = SM.brace_capacity({"system": "BRBF"}, m, {"sec": "BRBX-6", "n1": 1, "n2": 2})
    assert "ASSUMED" in cap2["note"]                                                # defaults are loud


# ------------------------------------------------------------------ HR-18 Direct Analysis
def test_dam_stiffness_and_notional_base_shear():
    cfg = _cfg(NX=1, NY=1, heights=[144.0], D_roof=100.0, Lr=0.0, dam=True)
    m = SM.build_static(cfg, "PDelta", 2, stiff=0.8)
    Es = {round(a[5], 3) for (n, a) in m["sig"] if n == "element" and a[0] == "elasticBeamColumn"}
    assert Es == {round(0.8 * E.E, 3)}
    case = SM.Case("1.4D", 1.4, 0.0, SM.RoofFactors(), {}, False)
    reac = {}; info = {}
    SM.demand_envelope(cfg, [case], nseg=2, reactions_out=reac, info_out=info)
    W = 1.4 * 100.0 * 360 * 300 / 144000.0
    assert info["dam"]["notional_gravity_only"] == pytest.approx(0.003)
    # gravity-only combination: the only horizontal load is Ni = 0.003*Yi -> base shear envelope
    assert sum(r["Vx"] for r in reac.values()) == pytest.approx(0.003 * W, rel=0.05)
    assert "+Ni(" in next(iter(reac.values()))["V_combo"]


# ------------------------------------------------------------------ HR-17 base reactions / uplift
def test_base_reactions_enveloped_with_roof_uplift():
    cfg = _cfg(NX=1, NY=1, heights=[240.0], D_roof=12.0, Lr=20.0,
               wind=dict(V=150, exposure="C", Kd=0.85, G=0.85, Cpnet=1.3), base="pinned")
    cases = [c for c in DP.combos(cfg) if getattr(c, "kind", "") in ("gravity", "wind")]
    reac = {}
    SM.demand_envelope(cfg, cases, nseg=2, reactions_out=reac)
    assert max(r["uplift"] for r in reac.values()) > 0.0                    # net uplift from roof suction
    assert any("roof uplift" in r["uplift_combo"] for r in reac.values())
    assert max(r["V"] for r in reac.values()) > 0.0


# ------------------------------------------------------------------ HR-36 cache
def test_demand_cache_hits_only_on_identical_inputs():
    cfg = _cfg(NX=1, NY=1, heights=[144.0])
    cases = [SM.Case("1.2D+1.6L+0.5Lr", 1.2, 1.6, SM.RoofFactors(Lr=0.5, cfg=cfg), {}, False)]
    with tempfile.TemporaryDirectory() as d:
        i1 = {}; e1, _ = SM.demand_envelope(cfg, cases, nseg=2, cache_dir=d, info_out=i1)
        i2 = {}; e2, _ = SM.demand_envelope(cfg, cases, nseg=2, cache_dir=d, info_out=i2)
        assert i2.get("cache") == "hit" and i1.get("cache") is None
        assert {k: v["Mz"] for k, v in e1.items()} == pytest.approx({k: v["Mz"] for k, v in e2.items()})
        cfg2 = dict(cfg, D_roof=cfg["D_roof"] + 10.0)
        i3 = {}; e3, _ = SM.demand_envelope(cfg2, cases, nseg=2, cache_dir=d, info_out=i3)
        assert i3.get("cache") is None
        assert max(v["Mz"] for v in e3.values()) > max(v["Mz"] for v in e1.values())


def test_default_one_way_rigid_beam_floor_adds_girder_load_effect():
    # rigid-ended beams, deck span undeclared: demand = solved + simple-span effect of the extra load the
    # half-bay girder rule puts on the beam (uniform w*c instead of the 45-degree trapezoid)
    cfg = _cfg(NX=1, NY=1, heights=[144.0], D_roof=100.0, Lr=0.0, floor_system="one-way",
               releases=lambda i, j, k, d: ("none", "none"))
    case = SM.Case("1.4D", 1.4, 0.0, SM.RoofFactors(), {}, False)
    env, kinds = SM.demand_envelope(cfg, [case], nseg=6)
    model, _, _ = SM.run_combo(cfg, 1.4, 0.0, SM.RoofFactors(), {}, transf="PDelta", nseg=6)
    res = SM._extract(model)
    w = 1.4 * 100.0 / 144000.0
    for b in model["beams"]:
        fs = frozenset((b["A"], b["B"]))
        L = b["L"]; c = (300.0 if b["dir"] == "X" else 360.0) / 2.0
        xs, Mg, Vg, _ = SM._simple_span([(("uni", w * c), "g")], L)
        _x, Ma, Va, _ = SM._simple_span([(("tri", w, c), "a")], L)
        dM = max(g - a for g, a in zip(Mg, Ma))
        assert dM > 0
        assert env[fs]["Mz"] == pytest.approx(res[fs]["Mz"] + dM, rel=1e-6)
        assert env[fs]["Mz"] < w * c * L * L / 8.0 * 1.2                     # not a scaled-up solve


def test_roof_suction_coefficients_fig_27_3_1():
    cfg = _cfg(wind=dict(V=115, exposure="C"))
    # h/L <= 0.5: -0.9 within h, -0.5 to 2h, -0.3 beyond
    assert SM._roof_cp(cfg, 10.0, 20.0, 100.0) == -0.9
    assert SM._roof_cp(cfg, 30.0, 20.0, 100.0) == -0.5
    assert SM._roof_cp(cfg, 50.0, 20.0, 100.0) == -0.3
    # h/L >= 1.0: -1.3 within h/2, -0.7 beyond; linear in between
    assert SM._roof_cp(cfg, 5.0, 20.0, 20.0) == -1.3
    assert SM._roof_cp(cfg, 15.0, 20.0, 30.0) == pytest.approx(2 / 3 * -0.9 + 1 / 3 * -0.7)
    qh, h = SM._wind_qh(cfg)
    Kz = E.kz_exposure(h, "C")
    assert qh == pytest.approx(0.00256 * Kz * 115.0 ** 2 * 0.85)


def test_ocbf_v_beam_unbalanced_load_f1_4a():
    cfg = _cfg(NX=1, NY=1, heights=[156.0], custom_build=_chevron_build, system="OCBF", D_roof=0.0, Lr=0.0)
    model = SM.build_static(cfg, "Linear", 4)
    row = SM._shape("HSS6X6X1/2")
    L = math.hypot(180.0, 156.0)
    T = 1.3 * 50.0 * row["A"]                                   # Om0*E (large pattern) > RyFyAg -> RyFyAg
    r = min(row["rx"], row["ry"]); Fe = math.pi ** 2 * 29000.0 / (L / r) ** 2
    Fn = 0.658 ** (50.0 / Fe) * 50.0 if 50.0 / Fe <= 2.25 else 0.877 * Fe
    C = 0.3 * Fn * row["A"]                                     # 0.3 Pn with Fy (not RyFy)
    pat = {1: (5000.0, 0.0, 0.0)}; paty = {1: (0.0, 5000.0, 0.0)}
    case = SM.Case("OCBF V", 0.0, 0.0, SM.RoofFactors(), {}, True, kind="ecl_ocbf", applies={"beam"},
                   ecl=dict(sx=1, sy=1, variant="a", patterns=(pat, paty), Om0=2.0))
    info = {}
    env, kinds = SM.demand_envelope(cfg, [case], nseg=4, info_out=info)
    beam = next(fs for fs, kv in kinds.items() if kv[0] == "beam" and 180000 in fs)
    Pu = (T - C) * 156.0 / L
    assert env[beam]["Mz"] == pytest.approx(Pu * 360.0 / 4.0, rel=0.02)
    other = [fs for fs, kv in kinds.items() if kv[0] == "beam" and 180000 not in fs]
    assert all(env.get(fs, {}).get("Mz", 0.0) == 0.0 for fs in other)     # only the V-braced beam


def test_service_deflection_from_solved_model():
    # deck spans X, no infill: each Y edge beam carries p * SX/2 -> 5 w L^4 / (384 E I), simple span
    cfg = _cfg(NX=1, NY=1, SX=360.0, SY=300.0, heights=[144.0], D_roof=50.0, Lr=20.0,
               floor_system="one-way", deck_span="X")
    sd = SM.service_deflections(cfg)
    Ix = E.Ipack("W24X76")[1]
    for (k, d, i, j), v in sd.items():
        if d != "Y":
            continue
        wT = (50.0 + 20.0) * 180.0 / 144000.0; wL = 20.0 * 180.0 / 144000.0
        assert v["TL"] == pytest.approx(5 * wT * 300.0 ** 4 / (384 * E.E * Ix), rel=0.02)
        assert v["LL"] == pytest.approx(5 * wL * 300.0 ** 4 / (384 * E.E * Ix), rel=0.02)
    assert SM._ss_mid_defl([(("uni", 1.0), "")], 480.0) == pytest.approx(5 * 480.0 ** 4 / 384, rel=1e-4)


def test_ecl_strut_axial_on_a_slaved_braced_bay_beam_is_reported_not_evaluated():
    """VERIFY (independent review): with the diaphragm restrained in the Ecl analysis, a braced-bay beam whose
    ends are slaved to the rigid diaphragm shows ~0 axial although AISC 341-22 F2.3 requires the strut to be
    designed for Ecl -- the package must say NOT EVALUATED instead of delivering the ~0 silently."""
    cfg = _cfg(NX=1, NY=1, heights=[156.0], custom_build=_chevron_build, system="SCBF", D_roof=0.0, Lr=0.0)
    pat = {1: (100.0, 0.0, 0.0)}; paty = {1: (0.0, 100.0, 0.0)}
    case = SM.Case("Ecl", 0.0, 0.0, SM.RoofFactors(), {}, True, kind="ecl",
                   ecl=dict(sx=1, sy=1, variant="a", patterns=(pat, paty), Om0=2.0))
    info = {}
    env, kinds = SM.demand_envelope(cfg, [case], nseg=4, info_out=info)
    model = SM.build_static(cfg, "Linear", 4)
    yb = [b for b in model["beams"] if b["etag"] in (8, 9)]
    assert len(yb) == 2
    # the Y brace at expected tension RyFyAg delivers a large horizontal force at the top of these beams ...
    row = SM._shape("HSS6X6X1/2"); T = 1.3 * 50.0 * row["A"]
    for b in yb:
        e = env[frozenset((b["A"], b["B"]))]
        assert max(e["comp"], e["tens"]) < 0.01 * T * 300.0 / math.hypot(300.0, 156.0)   # ... yet ~0 axial here
    ne = " ".join(info["not_evaluated"])
    # listed under not_evaluated (the report prints it as "Not evaluated by the analysis"), never silent
    assert info["not_evaluated"] and "~0 Ecl axial" in ne and "2 braced-bay beam(s)" in ne and "F2.3" in ne
    # no braces of an identified capacity-limited system -> no strut note
    cfg2 = _cfg(NX=1, NY=1, heights=[156.0], custom_build=_chevron_build, system="OCBF", D_roof=0.0, Lr=0.0)
    info2 = {}
    SM.demand_envelope(cfg2, [case], nseg=4, info_out=info2)
    assert not any("Ecl axial force" in x for x in info2["not_evaluated"])


def test_scbf_expected_compression_length_never_exceeds_the_work_point_length_rules():
    """VERIFY (independent review), AISC 341-22 F2.3: 'the brace length used for the determination of Fne shall
    not exceed the distance from brace end to brace end'. Crossing X-brace diagonals use half the work-point
    length; cfg['brace_length'] / cfg['brace_length_factor'] give the end-to-end length; the work-point default
    is flagged as underestimating C."""
    row = SM._shape("HSS5X5X1/2"); A = row["A"]; r = min(row["rx"], row["ry"])
    coord = {1: (0.0, 0.0, 0.0), 2: (480.0, 0.0, 180.0), 3: (0.0, 0.0, 180.0), 4: (480.0, 0.0, 0.0)}
    b1 = {"sec": "HSS5X5X1/2", "n1": 1, "n2": 2}; b2 = {"sec": "HSS5X5X1/2", "n1": 3, "n2": 4}
    m = {"coord": coord, "braces": [b1, b2]}
    Lwp = math.hypot(480.0, 180.0)

    def C_hand(L):
        Fe = math.pi ** 2 * 29000.0 / (L / r) ** 2; RyFy = 1.3 * 50.0
        Fne = 0.658 ** (RyFy / Fe) * RyFy if RyFy / Fe <= 2.25 else 0.877 * Fe
        return min(RyFy * A, Fne * A / 0.877)
    cap = SM.brace_capacity({"system": "SCBF"}, m, b1)                     # X-brace: half length
    assert cap["L"] == pytest.approx(Lwp / 2) and cap["C"] == pytest.approx(C_hand(Lwp / 2), rel=1e-3)
    assert not cap["L_default"] and cap["C"] > 3.0 * C_hand(Lwp)          # the full WP length was ~4x low here
    cap = SM.brace_capacity({"system": "SCBF", "brace_length": {"HSS5X5X1/2": 200.0}}, m, b1)
    assert cap["L"] == 200.0 and cap["C"] == pytest.approx(C_hand(200.0), rel=1e-3)
    cap = SM.brace_capacity({"system": "SCBF", "brace_length_factor": 0.8}, {"coord": coord, "braces": [b1]}, b1)
    assert cap["L"] == pytest.approx(0.8 * Lwp)
    cap = SM.brace_capacity({"system": "SCBF"}, {"coord": coord, "braces": [b1]}, b1)   # single diagonal
    assert cap["L"] == pytest.approx(Lwp) and cap["L_default"] and "UNDERESTIMATED" in cap["note"]
    # the chevron model's braces share the apex (no crossing): the work-point default is reported in the notes
    cfg = _cfg(NX=1, NY=1, heights=[156.0], custom_build=_chevron_build, system="SCBF", D_roof=0.0, Lr=0.0)
    model = SM.build_static(cfg, "Linear", 4)
    plan = SM.ecl_plan(cfg, model, {"X": {"brace": {}, "beam": {}}, "Y": {"brace": {}, "beam": {}}})
    assert any("WORK-POINT length" in n and "F2.3" in n for n in plan["notes"])

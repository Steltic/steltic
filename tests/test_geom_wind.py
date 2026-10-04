"""HR-10 (MWFRS wind), HR-11 (floor plate vs omitted columns), HR-12 (model_complete with subdivided
girders) and the HR-39 seismic-weight-only inputs. Hand calculations are written out independently of the
engine (numerical integration of Kz, Fig. 27.3-1 values typed from the standard).
Run: PYTHONPATH=.:steel_engine python -m pytest tests/test_geom_wind.py -q"""
import math, os, sys
import pytest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "steel_engine")); sys.path.insert(0, ROOT)

import engine3d as E  # noqa: E402


# ----------------------------------------------------------------------------- hand-calc helpers
def kz(z, exp="C"):
    zg, al = {"B": (3280.0, 7.5), "C": (2460.0, 9.8), "D": (1935.0, 11.5)}[exp]
    return 2.41*(max(z, 15.0)/zg)**(2.0/al)


def q(z, V, Kd=0.85, exp="C"):
    return 0.00256*kz(z, exp)*V**2*Kd               # psf, Kd folded in (Eq. 27.3-1)


def iq(a, b, V, exp="C", n=4000):                  # integral of q dz (midpoint rule, independent of engine)
    h = (b-a)/n
    return sum(q(a+(m+0.5)*h, V, exp=exp) for m in range(n))*h


def base_cfg(**kw):
    c = dict(NX=2, NY=2, SX=360.0, SY=360.0, heights=[144.0]*4, D_floor=80.0, D_roof=30.0, L_floor=50.0,
             clad=0.0, wind=dict(V=115.0, exposure="C", Kd=0.85, G=0.85, Kzt=1.0, Ke=1.0))
    c.update(kw); return c


# ----------------------------------------------------------------------------- HR-10 wind
def test_leeward_cp_by_L_over_B_fig_27_3_1():
    for LB, cp in ((0.5, -0.5), (1.0, -0.5), (1.5, -0.4), (2.0, -0.3), (3.0, -0.25), (4.0, -0.2), (6.0, -0.2)):
        assert E._cp_leeward(LB) == pytest.approx(cp)


def test_rectangular_lowrise_windward_qz_leeward_qh_full_top_halfstorey():
    """60x60 ft, 4 x 12 ft, h=48 ft (low-rise, rigid G=0.85), L/B=1 -> Cp_leeward=-0.5.
    Mid-height bands: L1 6-18, L2 18-30, L3 30-42, roof 42-48 (the top half-storey is kept); 0-6 ft -> foundation."""
    c = base_cfg(); V = 115.0; G = 0.85; B = 60.0
    d = E.wind_detail(c, "X")
    assert d["rigid"] and d["G"] == pytest.approx(0.85) and d["Cp_leeward"] == pytest.approx(-0.5)
    bands = {1: (6, 18), 2: (18, 30), 3: (30, 42), 4: (42, 48)}
    for k, (a, b) in bands.items():
        hand = (0.8*G*iq(a, b, V) + 0.5*G*q(48, V)*(b-a))*B/1000.0
        assert d["F"][k] == pytest.approx(hand, rel=2e-3), k
    base = (0.8*G*iq(0, 6, V) + 0.5*G*q(48, V)*6)*B/1000.0
    assert d["F_base_direct"] == pytest.approx(base, rel=2e-3)
    # the old engine (one q(z_mid) x G x Cpnet 1.3, roof h/2) under-predicted the frame shear here
    assert sum(d["F"].values()) > 1.05*sum(0.00256*kz(z)*V**2*0.85*0.85*1.3*B*t/1000.0
                                           for z, t in ((6, 12), (18, 12), (30, 12), (42, 6)))
    # 'lowest' lumps the foundation band into level 1
    c2 = base_cfg(); c2["wind"]["ground_band"] = "lowest"
    assert E.wind_forces(c2, "X")[1] == pytest.approx(d["F"][1] + base, rel=2e-3)


def test_minimum_16psf_governs_low_wind_speed():
    c = base_cfg(); c["wind"]["V"] = 60.0
    d = E.wind_detail(c, "Y")
    for k, t in {1: 12.0, 2: 12.0, 3: 12.0, 4: 6.0}.items():
        assert d["F"][k] == pytest.approx(16.0*60.0*t/1000.0, rel=1e-9)
        assert d["governs"][k] == "27.1.5 minimum"


def _split_cfg():
    """Two wings: A = bays i 0..1 with diaphragms at 10 and 30 ft (levels 1, 3); B = bays i 2..3 at 20 ft
    (level 2). 120 x 60 ft overall, h = 30 ft (low-rise, rigid)."""
    def plan(k, NX, NY):
        return {(i, j) for i in range(NX+1) for j in range(NY+1) if (i <= 2 if k in (1, 3) else i >= 2)}
    return base_cfg(NX=4, NY=2, SX=360.0, SY=360.0, heights=[120.0]*3, plan=plan)


def test_split_level_wings_get_their_own_wall_heights():
    """Y wind (strips along X): each wing is its own wall. Old engine: L1 60ft x 10ft, L2 60x10, L3 60x5."""
    c = _split_cfg(); V = 115.0; G = 0.85; qh = q(30, V)
    d = E.wind_detail(c, "Y")
    assert d["Cp_leeward"] == pytest.approx(-0.5) and d["h_ft"] == pytest.approx(30.0)
    hand = {1: (0.8*G*iq(5, 20, V) + 0.5*G*qh*15)*60/1000.0,
            2: (0.8*G*iq(10, 20, V) + 0.5*G*qh*10)*60/1000.0,
            3: (0.8*G*iq(20, 30, V) + 0.5*G*qh*10)*60/1000.0}
    for k in hand:
        assert d["F"][k] == pytest.approx(hand[k], rel=2e-3), k


def test_split_level_silhouette_x_wind_envelopes_both_senses():
    """X wind across the step: from -X wing A is windward over its full height and wing B's east wall is
    leeward up to 20 ft; from +X the roles swap. Each level takes the larger sense; L/B = 2 -> Cp = -0.3."""
    c = _split_cfg(); V = 115.0; G = 0.85; qh = q(30, V); W = 60.0
    d = E.wind_detail(c, "X")
    assert d["Cp_leeward"] == pytest.approx(-0.3)
    L1 = max(0.8*G*iq(5, 20, V), 0.3*G*qh*15)*W/1000.0
    L2 = max(0.3*G*qh*10, 0.8*G*iq(10, 20, V))*W/1000.0
    L3 = (0.8*G*iq(20, 30, V) + 0.3*G*qh*10)*W/1000.0
    for k, v in {1: L1, 2: L2, 3: L3}.items():
        assert d["F"][k] == pytest.approx(v, rel=2e-3), k


def _gable_cfg():
    c = base_cfg(NX=2, NY=1, SX=720.0, SY=720.0, heights=[300.0])          # 120 x 60 ft, diaphragm at 25 ft
    c["wind"]["roofs"] = {1: dict(eave_ft=20.0, ridge_ft=30.0, ridge="X")}  # ridge along X at y = 30 ft
    return c


def test_gable_along_ridge_loads_the_triangular_end_walls():
    c = _gable_cfg(); V = 115.0; G = 0.85; qh = q(25.0, V)                 # theta = 18.4 deg -> h = mean = 25
    d = E.wind_detail(c, "X")
    assert d["h_ft"] == pytest.approx(25.0) and d["Cp_leeward"] == pytest.approx(-0.3)   # L/B = 120/60
    n = 600; hand = 0.0
    for m in range(n):
        y = (m+0.5)*60.0/n; T = 20.0 + 10.0*(1.0 - abs(y-30.0)/30.0)
        hand += (0.8*G*iq(12.5, T, V, n=200) + 0.3*G*qh*(T-12.5))*(60.0/n)
    assert d["F"][1] == pytest.approx(hand/1000.0, rel=5e-3)


def test_gable_normal_to_ridge_adds_roof_horizontal_component():
    """Walls stop at the 20-ft eave; roof: theta=18.43 deg, h/L=25/60. Fig. 27.3-1 (typed by hand):
    windward  h/L 0.25: 15deg 0.0, 20deg 0.2 ; h/L 0.5: 15deg -0.18, 20deg 0.0  -> 0.0083
    leeward   -0.5 at 15deg, -0.6 at 20deg (both rows)                          -> -0.5687"""
    c = _gable_cfg(); V = 115.0; G = 0.85; qh = q(25.0, V)
    t = math.degrees(math.atan(10.0/30.0)); f = (t-15.0)/5.0
    r25 = 0.0 + 0.2*f; r50 = -0.18 + 0.18*f; hL = 25.0/60.0
    cww = r25 + (r50-r25)*(hL-0.25)/0.25; clw = -0.5 - 0.1*f
    assert cww == pytest.approx(0.0083, abs=2e-4) and clw == pytest.approx(-0.5687, abs=2e-4)
    d = E.wind_detail(c, "Y")
    walls = (0.8*G*iq(12.5, 20.0, V) + 0.5*G*qh*7.5)*120.0
    roof = qh*G*(cww-clw)*10.0*120.0
    assert d["F"][1] == pytest.approx((walls+roof)/1000.0, rel=2e-3)


def test_flexible_gust_factor_matches_commentary_table_c26_11_1():
    # ASCE 7-22 Table C26.11-1: h=600, B=D=100 ft, Exposure B, V=115 mph, n1=0.2 Hz, beta=0.01 -> Gf = 1.162
    assert E._gust_flexible(115.0, "B", 0.2, 0.01, 600.0, 100.0, 100.0) == pytest.approx(1.162, abs=1e-3)


def test_flexible_building_uses_gf_and_rigid_keeps_085(capsys):
    c = base_cfg(heights=[156.0]*12, system="SMF")                  # h = 156 ft, not low-rise
    na = 22.2/156.0**0.8                                             # Eq. 26.11-3 (steel MRF)
    d = E.wind_detail(c, "X")
    assert not d["rigid"] and d["n1"] == pytest.approx(na)
    assert d["G"] == pytest.approx(E._gust_flexible(115.0, "C", na, 0.01, 156.0, 60.0, 60.0))
    assert "FLEXIBLE" in capsys.readouterr().out
    c["wind"]["n1"] = 1.2                                            # analysed n1 >= 1 Hz -> rigid
    assert E.wind_detail(c, "X")["G"] == pytest.approx(0.85)


def test_override_hooks_return_hand_forces_verbatim():
    c = base_cfg()
    c["wind_forces_override"] = {"X": {1: 5.0, "4": 2.0}, "Y": {2: 7.0}}
    assert E.wind_forces(c, "X") == {1: 5.0, 2: 0.0, 3: 0.0, 4: 2.0}
    assert E.wind_detail(c, "Y")["method"] == "override"
    c2 = base_cfg(); c2["wind"]["story_forces"] = {"X": {1: 1.0}}
    assert E.wind_forces(c2, "X")[1] == 1.0
    with pytest.raises(ValueError):
        E.wind_forces(c2, "Y")                                       # missing direction -> loud
    c3 = base_cfg(wind_by_level=lambda cfg, d: {1: 3.0 if d == "X" else 4.0})
    assert E.wind_forces(c3, "Y")[1] == 4.0


# ----------------------------------------------------------------------------- HR-11 floor plate
def _plate_cfg(**kw):
    c = dict(NX=3, NY=3, SX=360.0, SY=360.0, heights=[144.0], D_floor=80.0, D_roof=40.0, L_floor=50.0, clad=0.0)
    c.update(kw); return c


FULL = {(i, j) for i in range(4) for j in range(4)}


def test_omitted_interior_column_keeps_its_bays(capsys):
    c = _plate_cfg(present={1: FULL - {(1, 1)}})
    assert E.floor_area_ft2(c, 1) == pytest.approx(9*900.0)
    assert E.perim_ft(c, 1) == pytest.approx(4*90.0)
    assert "OMITTED COLUMN" in capsys.readouterr().out
    assert E.floor_w(c, 1) == pytest.approx(40.0*8100.0/1000.0)


def test_openings_and_old_behaviour_switch():
    c = _plate_cfg(present={1: FULL - {(1, 1)}}, openings={1: [(0, 0)]})
    assert E.floor_area_ft2(c, 1) == pytest.approx(8*900.0)
    c = _plate_cfg(present={1: FULL}, openings=[(1, 1)])                     # 1-bay atrium, all columns present
    assert E.nbays(c, 1) == 8 and E.perim_ft(c, 1) == pytest.approx(4*90.0 + 4*30.0)
    c = _plate_cfg(present={1: FULL - {(1, 1)}}, auto_omitted_columns=False)
    assert E.floor_area_ft2(c, 1) == pytest.approx(5*900.0)


def test_edge_node_is_a_notch_unless_declared_omitted():
    c = _plate_cfg(present={1: FULL - {(1, 0)}})
    assert E.nbays(c, 1) == 7
    c["omitted_columns"] = {1: [(1, 0)]}
    assert E.nbays(c, 1) == 9


@pytest.mark.parametrize("plan,NX,NY", [(E.Lplan, 4, 4), (E.doughnut, 6, 6), (E.Uplan, 6, 4), (E.openlobby, 6, 4)])
def test_plan_archetypes_area_unchanged(plan, NX, NY):
    c = _plate_cfg(NX=NX, NY=NY, heights=[144.0]*2, plan=plan)
    for k in (1, 2):
        P = plan(k, NX, NY)
        old = sum(900.0 for i in range(NX) for j in range(NY)
                  if {(i, j), (i+1, j), (i, j+1), (i+1, j+1)} <= P)
        assert E.floor_area_ft2(c, k) == pytest.approx(old)


# ----------------------------------------------------------------------------- cladding (mid-height bands)
def test_cladding_uses_half_storey_above_and_below():
    c = _plate_cfg(NX=2, NY=1, heights=[180.0, 144.0, 144.0], clad=15.0, D_floor=0.0, D_roof=0.0)
    per = 2*(60.0+30.0)
    for k, t in {1: (15+12)/2.0, 2: 12.0, 3: 6.0}.items():
        assert E.floor_w(c, k) == pytest.approx(15.0*per*t/1000.0), k


def test_cladding_setback_walls_go_to_the_levels_they_span():
    def plan(k, NX, NY):
        return {(i, j) for i in range(NX+1) for j in range(NY+1) if k == 1 or i <= 1}
    c = _plate_cfg(NX=2, NY=1, heights=[120.0, 120.0], clad=10.0, D_floor=0.0, D_roof=0.0, plan=plan)
    A = E.clad_areas_ft2(c)
    assert A[1] == pytest.approx(1500.0) and A[2] == pytest.approx(600.0)


# ----------------------------------------------------------------------------- HR-39 seismic-weight-only inputs
def test_seismic_weight_only_keys_do_not_touch_gravity():
    c = _plate_cfg(heights=[144.0, 144.0])
    w0, g0, d0 = E.floor_w(c, 1), E.floor_grav(c, 1), E.floor_dead(c, 1)
    c["seismic_mass_only"] = {1: 10.0}; c["seismic_weight_kip"] = {"1": 25.0}
    assert E.floor_w(c, 1) == pytest.approx(w0 + 10.0*8100.0/1000.0 + 25.0)
    assert E.floor_grav(c, 1) == pytest.approx(g0) and E.floor_dead(c, 1) == pytest.approx(d0)


# ----------------------------------------------------------------------------- HR-12 floor_beam_gaps
def _frame_builder(variant):
    import openseespy.opensees as ops

    def cb(cfg, transf="Linear"):
        ops.wipe(); ops.model("basic", "-ndm", 3, "-ndf", 6)
        SX, SY, z = cfg["SX"], cfg["SY"], E.zlevels(cfg)
        P = {(i, j) for i in range(3) for j in range(2)}
        for k in (0, 1):
            for (i, j) in P:
                ops.node(E.ntag(i, j, k), i*SX, j*SY, z[k])
        for (i, j) in P:
            ops.fix(E.ntag(i, j, 0), 1, 1, 1, 1, 1, 1)
        ele = []; et = 1
        for (i, j) in sorted(P):
            E.add_column(et, E.ntag(i, j, 0), E.ntag(i, j, 1), "W14X90", "X")
            ele.append((et, "col", "W14X90", E.ntag(i, j, 0), E.ntag(i, j, 1))); et += 1

        def beam(n1, n2):
            nonlocal et
            E.add_beam(et, n1, n2, "W18X50"); ele.append((et, "beam", "W18X50", n1, n2)); et += 1
        mid = 1*100000 + 9000 + 1                       # off-grid work point (i >= 90)
        off = 24.0 if variant == "offline" else 0.0
        ops.node(mid, 0.5*SX, off, z[1])
        beam(E.ntag(0, 0, 1), mid)                      # (0,0)-(1,0) girder in two halves
        if variant != "missing_half":
            beam(mid, E.ntag(1, 0, 1))
        if variant == "three_pieces":                   # extra collinear subdivision node
            mid2 = 1*100000 + 9000 + 2; ops.node(mid2, 1.5*SX, 0.0, z[1])
            beam(E.ntag(1, 0, 1), mid2); beam(mid2, E.ntag(2, 0, 1))
        else:
            beam(E.ntag(1, 0, 1), E.ntag(2, 0, 1))
        for i in range(2):
            beam(E.ntag(i, 1, 1), E.ntag(i+1, 1, 1))
        for i in range(3):
            beam(E.ntag(i, 0, 1), E.ntag(i, 1, 1))
        return {"cm": {1: (SX, 0.5*SY)}, "present": {0: P, 1: P}, "z": z, "NF": 1, "ele": ele}
    return cb


@pytest.mark.parametrize("variant,ngaps", [("split", 0), ("three_pieces", 0), ("missing_half", 1), ("offline", 1)])
def test_floor_beam_gaps_follow_collinear_chains(variant, ngaps):
    c = _plate_cfg(NX=2, NY=1, SX=240.0, SY=240.0, custom_build=_frame_builder(variant))
    assert len(E.floor_beam_gaps(c)) == ngaps

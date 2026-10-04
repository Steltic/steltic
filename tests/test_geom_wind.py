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

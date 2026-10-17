"""US-practice fixes, October 2026 (second round): Omega0 column cases only where AISC 341 applies,
ASCE 7-22 4.7 live load reduction per member, simple-shear connection slots, story stiffness V/Delta.

    PYTHONPATH=$PWD:$PWD/steel_engine python -m pytest tests/test_us_practice_2026_10b.py -q
"""
import copy, math, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "steel_engine")); sys.path.insert(0, ROOT)

import pytest  # noqa: E402

ops = pytest.importorskip("openseespy.opensees")
import engine3d as E  # noqa: E402
import static_model as SM  # noqa: E402
import design_pipeline as DP  # noqa: E402


def _cfg(R=3.0, Om0=3.0, Cd=3.0, braced=True, **kw):
    seis = E.seis(0.20, 0.09, 0.09, R, 0.02, 0.75, 1.7, Ie=1.0, Cd=Cd, Om0=Om0)
    c = dict(arch="test", NX=2, NY=2, SX=360.0, SY=300.0, heights=[180.0, 156.0, 156.0], base="pinned",
             col="W14X90", beam="W24X76", D_floor=75.0, D_roof=75.0, L_floor=65.0, Lr=20.0, clad=0.0,
             seis=seis, rho=1.0, floor_system="two-way",
             system=("steel system not specifically detailed for seismic resistance" if R <= 3 else "SCBF"),
             releases=lambda i, j, k, d: ("both", "none"), dam=False)
    if braced:
        c["brace"] = "HSS6X6X1/2"
        c["braces"] = lambda k, NX, NY: [("X", 0, 0), ("Y", 0, 0)]
    c.update(kw)
    return c


def test_r3_system_has_no_omega0_column_cases_and_r6_keeps_them():
    cases = DP.combos(_cfg(R=3.0))
    assert not [c for c in cases if getattr(c, "kind", "") == "omega0"]
    assert DP.omega0_column_dirs(_cfg(R=3.0)) == []
    assert not DP.aisc341_applies(_cfg(R=3.0), "X")
    cases6 = DP.combos(_cfg(R=6.0, Om0=2.0, Cd=5.0))
    om = [c for c in cases6 if getattr(c, "kind", "") == "omega0"]
    assert om and all(c.applies == {"col"} for c in om)
    # explicit opt-in (e.g. ASCE 7-22 12.3.3.4 supports of discontinuous frames)
    assert DP.omega0_column_dirs(_cfg(R=3.0, omega0_columns=True)) == ["X", "Y"]


def test_live_load_reduction_factor_eq_4_7_1_and_limits():
    assert SM.llr_factor(4.0, 5250.0, 7) == pytest.approx(0.40)                 # 0.353 -> 0.40 floor
    assert SM.llr_factor(2.0, 500.0, 1) == pytest.approx(0.25 + 15 / math.sqrt(1000.0))
    assert SM.llr_factor(2.0, 150.0, 1) == 1.0                                  # KLL AT < 400 ft2
    assert SM.llr_factor(4.0, 1500.0, 1) == pytest.approx(0.50)                 # one floor: >= 0.50
    assert SM.llr_factor(4.0, 400.0, 2) == pytest.approx(0.625)


def test_llr_reduces_interior_column_axial_by_hand_value():
    """2 x 2 bays (30 x 25 ft), 3 levels: interior column at the base supports two floors of 750 ft2 each.
    Reducible 50 psf, 15 psf partitions not reduced; 1.2D + 1.6L: reduction = 1.6 x 50 psf x 1500 ft2 x (1 - RF)."""
    base = _cfg(R=3.0, braced=False)
    cases = [SM.Case("1.2D+1.6L", 1.2, 1.6, SM.RoofFactors(), {}, False)]
    env0, kinds = SM.demand_envelope(base, cases, nseg=4)
    llr = dict(base, live_load_reduction={"unreduced_psf": 15.0})
    env1, _k = SM.demand_envelope(llr, cases, nseg=4)
    inner = [fs for fs, (k, s) in kinds.items() if k == "col" and
             any((n % 100000) // 100 == 1 and n % 100 == 1 and n // 100000 == 0 for n in fs)]
    assert inner
    fs = inner[0]
    AT = 2 * 750.0
    rf = SM.llr_factor(4.0, AT, 2)
    expect = 1.6 * 0.050 * AT * (1 - rf)
    assert env0[fs]["comp"] - env1[fs]["comp"] == pytest.approx(expect, rel=0.02)
    assert env1[fs]["llr"]["RF"] == pytest.approx(rf, abs=1e-3)
    assert env1[fs]["llr"]["AT_ft2"] == pytest.approx(AT, rel=0.01)


def test_story_stiffness_ratio_uses_story_drift_not_drift_ratio():
    src = open(os.path.join(ROOT, "steel_engine", "design_pipeline.py")).read()
    assert "dr[k - 1] * hs[k - 1]" in src

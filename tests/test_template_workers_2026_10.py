"""October 2026 fix-to-A campaign: report template items found while fixing the examples.

- Collectors are beams (never "Lateral-system columns"); every collector is named in Chapter 2.
- Chapter 10 base rows print the D2.6a design tension; the member LLR note prints only where a reduction is taken
  (either key set); the deflection table lists the groups behind the summary ratios; a specified camber replaces
  the suggestion; the governing hazard compares rho V with W; the Chapter 11 base intro cites AISC 341 only where
  the base records use it; the modal-mass check uses the engine's mode count; base-shear shares count every support.

    PYTHONPATH=$PWD:$PWD/steel_engine python -m pytest tests/test_template_workers_2026_10.py -q
"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "steel_engine")); sys.path.insert(0, ROOT)

import pytest  # noqa: E402

pytest.importorskip("engine3d")
import report as R  # noqa: E402

SRC = open(os.path.join(ROOT, "steel_engine", "report.py"), encoding="utf-8").read()


def test_collectors_are_beams():
    assert R._role_of("collector-Y-floor-W21X55", {"kind": "beam"}) == "floor"
    assert R._role_of("x", {"role": "collector"}) == "floor"
    assert R._role_of("lateral_col-W14X132", {}) == "lateral_col"
    assert R._role_of("col-W14X90", {}) == "lateral_col"
    assert R._role_of("gravity_col-W14X61", {}) == "gravity_col"


def test_base_row_prints_design_tension():
    c = {"id": "conn-base-x", "type": "fixed (moment) column base",
         "demand": {"P_comp_kip": 193.1, "uplift_kip": 54.2, "T_design_kip": 71.6, "V_kip": 47.7}}
    row = R._base_record_row(c)
    assert "design tension 71.6 kip (AISC 341-22 D2.6a)" in " ".join(map(str, row))


def test_specified_camber_from_cfg_or_member():
    cfg = {"composite_floor": {"camber_in": {"W24X55": 0.75}}}
    pkg = {"members": [{"inputs": {"section": "W40X249"}, "composite": {"camber_in": 1.25}}]}
    assert R._specified_camber(cfg, pkg, "W24X55") == 0.75
    assert R._specified_camber(cfg, pkg, "W40X249") == 1.25
    assert R._specified_camber(cfg, pkg, "W18X35") == 0.0


def test_rho_of_per_direction():
    assert R._rho_of({"rho_X": 1.3}, "X") == pytest.approx(1.3)
    seis = {"SDS": 1.0, "SD1": 0.6, "S1": 0.6, "R": 8, "Ie": 1.0}
    assert R._rho_of({"rho": 1.3, "seis": seis}, "Y") == pytest.approx(1.3)


def test_source_wording_and_rules():
    assert "min(3*NF, 16)" not in SRC and "E._nm_default(NF)" in SRC
    assert "if abs(c[2]) > 1.0: continue" not in SRC
    assert "coll[:6]" not in SRC
    assert ", new / altered" not in SRC
    assert "Ponding is addressed under the rain basis." not in SRC
    assert "headed studs or end plate with" not in SRC
    assert "cantilever action of" in SRC and "&rho;V" in SRC


def test_base_shear_shares_count_only_translational_supports():
    """R24g B1: the rigid-diaphragm masters (fixed in Z and the rotations) carry minus the story force as a
    'reaction'; counting them doubled the total and halved every printed share. Only supports restrained in the
    direction's translation enter the base shear (hillside footings at any elevation still do)."""
    i = SRC.index("planes = sorted((sf or {}).get(\"lines\", {}).get(d, {}) or {})")
    body = SRC[i:i + 900]
    assert "for n in ops.getFixedNodes():" in body and "if di not in ops.getFixedDOFs(n):" in body
    assert body.index("getFixedDOFs") < body.index("share[p] = share.get(p, 0.0) + rx")


def test_getfixeddofs_distinguishes_masters_from_supports():
    import openseespy.opensees as ops
    ops.wipe(); ops.model("basic", "-ndm", 3, "-ndf", 6)
    ops.node(1, 0.0, 0.0, 0.0); ops.node(2, 0.0, 0.0, 120.0)
    ops.fix(1, 1, 1, 1, 1, 1, 1); ops.fix(2, 0, 0, 1, 1, 1, 0)      # a support and a rigid-diaphragm master
    assert 1 in ops.getFixedDOFs(1) and 1 not in ops.getFixedDOFs(2)
    ops.wipe()

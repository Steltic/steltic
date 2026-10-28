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


def test_composite_disclaimer_only_without_composite_members():
    """R24c / R24d: a roof-stud scope note ('strength checked on the bare steel') printed 'composite action is not
    relied upon ... designed as bare steel' under composite floor beams. The disclaimer needs an explicit
    not-relied scope AND no member with composite design values."""
    pkg = {"composite_design": {"scope": "floors composite; the roof framing is composite for serviceability "
                                         "(its strength is checked on the bare steel, conservatively)"},
           "members": [{"id": "floor-W24X55", "inputs": {"section": "W24X55"},
                        "composite": {"studs_total": 32, "I_LB_in4": 2444.0, "pct": 0.45}}]}
    html = R._composite_section(pkg) if hasattr(R, "_composite_section") else ""
    src = SRC[SRC.index("not_relied = bool("):]
    assert "if not_relied and not rows:" in src
    assert "bare[- ]steel|not used" not in SRC.split("not_relied = bool(")[1][:200]
    assert "not relied upon for strength or deflection" not in html


def test_modal_table_lists_every_mode_used():
    """R24e / R24f / R24g: the Chapter 5 table stopped at 12 of the 16 modes, so its cumulative mass disagreed with
    the quoted modal-mass value."""
    assert "for m in range(min(len(T), 12)):" not in SRC and "for m in range(len(T)):" in SRC


def test_wind_row_names_the_tier_cp():
    D = {"L_over_B": 0.8, "Cp_leeward": -0.5, "Cp_leeward_tiers": [(0.8, -0.5), (2.0, -0.3)]}
    t = R._lb_cp_txt(D)
    assert "0.80, -0.50 (plan)" in t and "2.00, -0.30 above a setback" in t
    assert R._lb_cp_txt({"L_over_B": 1.0, "Cp_leeward": -0.5, "Cp_leeward_tiers": [(1.0, -0.5)]}) == "1.00, -0.50"


def test_single_pair_ratio_on_interaction_named_rows():
    import consistency as C
    k = {"limit_state": "construction stage, wet concrete + deck + 20 psf construction live; flexure",
         "cited": "AISC 360-22 F2.1", "Mu_kipft": 102.8, "phiMn_kipft": 249.4, "DC": 0.412}
    html = R._checks_table([k], C)
    assert "0.412" in html and html.count("&mdash;") == 0 and "reconcile" not in html
    k2 = dict(k, DC=0.70)                      # an interaction whose D/C is not this pair's ratio stays a dash
    html2 = R._checks_table([k2], C)
    assert "&mdash;" in html2 and "reconcile" not in html2


def test_deck_sentence_carries_12335_increase():
    assert "25%% increase of &sect;12.3.3.5 (%s) on the deck connections" in SRC
    assert '_QA["t12335"] = sorted(set(trig["12.3.3.5"])) if sdc in ("D", "E", "F") else []' in SRC

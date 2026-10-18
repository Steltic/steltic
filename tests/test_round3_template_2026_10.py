"""October 2026, round-3 review: template fixes.

- The printed H1-1 ratio uses a square box / square HSS / round section's single phi*Mn about both axes, and its
  label names the moment terms it actually uses.
- The irregularity preamble mentions NOT EVALUATED items only when one is marked so.
- The composite summary table reads the partial-composite ratio of a member's composite record ('pct').

    PYTHONPATH=$PWD:$PWD/steel_engine python -m pytest tests/test_round3_template_2026_10.py -q
"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "steel_engine")); sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # noqa: E402

E = pytest.importorskip("engine3d")
import report as R  # noqa: E402
import consistency as C  # noqa: E402
from test_report_hr import _rect  # noqa: E402


def _box(section="BOX28X4", cap=None):
    return {"id": "lateral_col-" + section, "inputs": {"section": section, "P_comp_kip": 11523.0,
                                                      "Mz_kipin": 1869.0 * 12, "My_kipin": 1832.0 * 12, "V_kip": 109.0},
            "capacity": cap or {"phiPn_kip": 16558.0, "phiMn_kipft": 13080.0, "phiVn_kip": 4320.0}, "DC": 0.967}


def test_h1_square_box_uses_phiMn_about_both_axes():
    rows = {q: r for q, _d, _c, r in C.envelope_ratios(_box())}
    expect = 11523.0 / 16558.0 + 8.0 / 9.0 * (1869.0 + 1832.0) / 13080.0
    assert rows["H1"] == pytest.approx(expect, rel=1e-6)          # 0.967, the recorded check
    assert C.h1_moment_terms(_box())[0] == ["Mx", "My"]


def test_h1_wide_flange_without_minor_strength_omits_and_says_so():
    m = _box("W14X257", {"phiPn_kip": 3000.0, "phiMn_kipft": 1500.0})
    terms = C.h1_moment_terms(m)[0]
    assert terms == ["Mx"]
    html = R._member_calc_block(m)
    assert "omits that term" in html and "from the printed P, M<sub>x</sub> and strengths" in html
    html = R._member_calc_block(_box())
    assert "from the printed P, M<sub>x</sub>, M<sub>y</sub> and strengths" in html and "omits" not in html


def test_irregularity_preamble_mentions_not_evaluated_only_when_present():
    cfg = _rect(); R._INV_CACHE.clear()
    runs = {"exempt": set(), "sdc": "D"}
    for d in ("X", "Y"):
        runs[d] = {"k": [200.0] * 4, "drift_cm": [0.01] * 4, "tir": [1.0] * 4, "ax": [1.0] * 4}
    cfg["irregularity_statements"] = {"H3": "the same slab at every level"}
    html = R._irregularity_section(cfg, runs, R._sfrs(cfg))
    body = html[html.find("<table"):]
    intro = html[:html.find("<table")] if "<table" in html else html
    if "NOT EVALUATED" in body:
        assert "Items marked NOT EVALUATED" in intro
    else:
        assert "Items marked NOT EVALUATED" not in intro


def test_composite_table_reads_pct_from_composite_record():
    pkg = {"members": [{"id": "floor-W18X35", "inputs": {"section": "W18X35"},
                        "capacity": {"phiMn_composite_kipft": 390.5, "I_LB_in4": 1036.0},
                        "composite": {"studs_total": 18, "Qn_kip": 17.23, "pct": 0.3, "I_LB_in4": 1036.0}}]}
    html = R._composite_section(pkg)
    row = html[html.find("floor-W18X35"):]
    row = row[:row.find("</tr>")]
    assert "30%" in row and "&mdash;" in row          # pct now printed; camber / wet stage still absent


def test_wind_drift_governing_value_heads_the_section_and_scorecard():
    from test_report_us_practice import _cfg as _ucfg
    w = dict(V=115.0, exposure="C")
    base = _ucfg(wind=w, wind_service={"V": 76.0, "MRI": 10})
    html0 = R._wind_drift_section(base)
    cm = R._QA["wind_drift"][0]
    gov = dict(base, wind_drift_governing={"drift": cm * 1.5, "where": "Case 2 (Fig. 27.3-8) at the plan edge"})
    html = R._wind_drift_section(gov)
    assert R._QA["wind_drift"][0] == pytest.approx(cm * 1.5)
    assert "Case 2 (Fig. 27.3-8) at the plan edge" in html and "%.3f%%" % (cm * 150) in html
    low = dict(base, wind_drift_governing={"drift": cm * 0.5, "where": "Case 3 at the corner"})
    html = R._wind_drift_section(low)
    assert R._QA["wind_drift"][0] == pytest.approx(cm) and "does not govern" in html
    plain = dict(_ucfg(wind=w), wind_drift_governing={"drift_pct": 1.0, "where": "Case 2 at Grid 1"})
    html = R._wind_drift_section(plain)
    assert "Worst wind drift 1.000%" in html and R._QA["wind_drift"][0] == pytest.approx(0.01)
    assert "Worst service wind drift" in html0


def test_suggested_camber_only_from_three_quarter_inch():
    import inspect
    src = inspect.getsource(R._deflection_section)
    assert "Suggested camber" in src and "camber >= 0.75" in src


def test_diaphragm_fpx_evaluated_in_both_directions():
    cfg = _rect(); R._INV_CACHE.clear()
    NF = len(cfg["heights"])
    FxX = {k: 10.0 * k for k in range(1, NF + 1)}
    FxY = {k: 14.0 * k for k in range(1, NF + 1)}
    html = R._diaphragm_section(cfg, (FxX, FxY))
    assert "Eq. 12.10-1, X (kip)" in html and "Eq. 12.10-1, Y (kip)" in html
    one = R._diaphragm_section(cfg, (FxX, dict(FxX)))      # same forces both ways: one column, as before
    assert "Eq. 12.10-1, Y (kip)" not in one and "Eq. 12.10-1 (kip)" in one


def test_member_self_weight_enters_W_only():
    cfg = _rect(); R._INV_CACHE.clear()
    w0 = E.floor_w(cfg, 1); d0 = E.floor_dead(cfg, 1)
    sw = E.member_self_weight_by_level(cfg)
    assert sw and all(v > 0 for v in sw.values())
    cfg2 = dict(cfg, seismic_weight_members_kip=sw)
    assert E.floor_w(cfg2, 1) == pytest.approx(w0 + sw.get(1, 0.0))
    assert E.floor_dead(cfg2, 1) == pytest.approx(d0)          # gravity dead load unchanged
    rows = R._gravity_loads_table(cfg2) if hasattr(R, "_gravity_loads_table") else ""
    if rows:
        assert "member self-weight not in the floor dead load" in rows


def test_report_passes_each_direction_elf_forces_to_the_diaphragm_table():
    import inspect
    src = inspect.getsource(R.build_report)
    i = src.find("_diaphragm_section(cfg, _fxd")
    assert i > 0 and "seismic_design_forces(cfg)" in src[max(0, i - 600):i]
    cfg = _rect(); R._INV_CACHE.clear()
    sdf = E.seismic_design_forces(cfg)
    fxd = tuple({int(k): float(v) for k, v in sdf[d]["F_elf"].items()} for d in ("X", "Y"))
    html = R._diaphragm_section(cfg, fxd)
    assert "Diaphragm" in html or "F<sub>px</sub>" in html


def test_lb_range_prints_a_dash_not_an_escaped_entity():
    m = {"id": "floor-W27X94", "inputs": {"section": "W27X94", "kind": "beam", "Lb_in": 120.0},
         "checks": [{"limit_state": "flexure, deck-braced top flange", "Lb_in": 0.0},
                    {"limit_state": "flexure, construction stage", "Lb_in": 370.0}]}
    html = R._member_calc_block(m)
    assert "per limit state (0–370)" in html
    assert "&amp;ndash;" not in html


def test_column_rows_follow_the_column_connection_record():
    cfg = _rect(); R._INV_CACHE.clear()
    sf = R._sfrs(cfg)
    def col(mid, sec):
        return {"id": mid, "inputs": {"section": sec, "kind": "col", "P_comp_kip": 300.0, "V_kip": 10.0}}
    pkg = {"members": [col("lateral_col-W24X117", "W24X117"), col("gravity_col-W14X90", "W14X90"),
                       col("lateral_col-W14X176", "W14X176")],
           "connections": [
               {"id": "conn-lateral_col-W24X117", "type": "no column splice: one-piece 38-ft crane column; brackets"},
               {"id": "conn-gravity_col-W14X90", "type": "column top: cap plate and girder seats (single-story column)",
                "design_basis": "AISC 360-22 J10 and J4; AISC Manual Part 14"}]}
    html = R._connection_demands(cfg, pkg, sf=sf, ctx={"a341": True}) or ""
    assert "W24X117" not in html                                   # one-piece column: no splice row
    row = html[html.find("W14X90"):]; row = row[:row.find("</tr>")]
    assert "column top: cap plate and girder seats" in row and "J10" in row and "splice" not in row
    row = html[html.find("W14X176"):]; row = row[:row.find("</tr>")]
    assert "column splice" in row                                  # no record: the generic splice row stays


def test_splice_rows_name_the_recorded_splice_and_weld_basis():
    cfg = _rect(); R._INV_CACHE.clear()
    sf = R._sfrs(cfg)
    def col(mid, sec):
        return {"id": mid, "inputs": {"section": sec, "kind": "col", "P_comp_kip": 900.0, "V_kip": 20.0}}
    pkg = {"members": [col("lateral_col-W14X370", "W14X370"), col("gravity_col-W14X132", "W14X132")],
           "connections": [
               {"id": "conn-lateral_col-W14X370", "type": "SMF column splice, CJP groove welded (demand critical), 4 ft above L6: x"},
               {"id": "conn-gravity_col-W14X132", "type": "gravity column splice, 4 ft above the floor (milled bearing; plates)"}]}
    html = R._connection_demands(cfg, pkg, sf=sf, ctx={"a341": True}) or ""
    row = html[html.find("W14X370"):]; row = row[:row.find("</tr>")]
    assert "SMF column splice, CJP groove welded" in row and "J2" in row and "J1.4" not in row
    row = html[html.find("W14X132"):]; row = row[:row.find("</tr>")]
    assert "gravity column splice, 4 ft above the floor" in row and "J1.4" in row


def test_member_weight_split_by_end_nodes():
    z = [0.0, 192.0, 372.0]                       # base, office roof (16 ft), hall roof (31 ft)
    # a 31-ft hall column with no node at the office roof: half to the hall roof, half to grade
    assert E._split_member_weight(z, 2, [(0.0, 372.0, 10.0)]) == {2: 5.0}
    # story-by-story columns: the same as splitting each story's weight between its two levels
    out = E._split_member_weight(z, 2, [(0.0, 192.0, 4.0), (192.0, 372.0, 6.0)])
    assert out == {1: 5.0, 2: 3.0}
    # an intermediate node shares its half between the bounding levels by height
    out = E._split_member_weight(z, 2, [(96.0, 192.0, 2.0)])
    assert out == {1: 1.5}


def test_extra_floor_mass_is_printed_with_its_basis():
    cfg = _rect(); R._INV_CACHE.clear()
    NF = len(cfg["heights"])
    cfg2 = dict(cfg, extra_mass_floors={k: 1.2 for k in range(1, NF + 1)},
                extra_mass_basis="6 in slab edge beyond the edge members")
    html = R._gravity_loads_table(cfg2)
    assert "Additional dead load by level" in html and "1.2 psf on levels" in html
    assert "6 in slab edge beyond the edge members" in html
    assert "Additional dead load by level" not in R._gravity_loads_table(cfg)


def test_combination_table_prints_code_roof_factors():
    import static_model as SM
    cfg = {"Lr": 20.0, "rain": 24.8, "snow": 0.0}
    cases = [("1.2D + 1.6R + 0.5L", 1.2, 0.5, SM.RoofFactors(R=1.6, cfg=cfg), None),
             ("1.2D + 1.6Lr + 0.5L", 1.2, 0.5, SM.RoofFactors(Lr=1.6, cfg=cfg), None),
             ("1.4D", 1.4, 0.0, SM.RoofFactors(cfg=cfg), None),
             ("legacy", 1.2, 1.6, 0.5, None)]
    html = R._combo_table(cases)
    assert "1.60R" in html and "1.60L<sub>r</sub>" in html and "1.98" not in html
    assert "0.50" in html                                      # a plain float factor still prints

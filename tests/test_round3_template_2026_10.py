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

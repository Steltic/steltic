"""October 2026: Table 12.3-1 Type 3 -- the story-to-story change in effective diaphragm stiffness is the engineer's
evaluation, declared with cfg['irregularity_statements']['H3'] (the open-area part stays automated).

    PYTHONPATH=$PWD:$PWD/steel_engine python -m pytest tests/test_h3_statement_2026_10.py -q
"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "steel_engine")); sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # noqa: E402

E = pytest.importorskip("engine3d")
import report as R  # noqa: E402
from test_report_hr import _rect  # noqa: E402


def _runs():
    runs = {"exempt": set(), "sdc": "D"}
    for d in ("X", "Y"):
        runs[d] = {"k": [200.0] * 4, "drift_cm": [0.01] * 4, "tir": [1.0] * 4, "ax": [1.0] * 4}
    return runs


def _h3_row(html):
    i = html.find("Horizontal 3")
    assert i >= 0
    return html[i:html.find("</tr>", i)]


def test_h3_default_keeps_the_open_area_screen():
    cfg = _rect(); R._INV_CACHE.clear()
    row = _h3_row(R._irregularity_section(cfg, _runs(), R._sfrs(cfg)))
    assert "story-to-story change in effective diaphragm stiffness" in row


def test_h3_declared_not_present_and_present():
    cfg = _rect(); R._INV_CACHE.clear()
    cfg["irregularity_statements"] = {"H3": "the same 3-1/4 in. LW concrete on 3 in. deck at every level"}
    html = R._irregularity_section(cfg, _runs(), R._sfrs(cfg))
    row = _h3_row(html)
    assert "Not present (engineer&#x27;s evaluation)" in row or "Not present (engineer's evaluation)" in row
    assert "LW concrete" in row and "NOT EVALUATED" not in row
    cfg["irregularity_statements"] = {"H3": {"present": True, "text": "the atrium floors lose more than half their stiffness"}}
    html = R._irregularity_section(cfg, _runs(), R._sfrs(cfg))
    row = _h3_row(html)
    assert "Type 3 DIAPHRAGM DISCONTINUITY: YES (engineer" in row
    j = html.find("12.3.3.5")
    assert j >= 0 and "H3" in html[j:html.find("</tr>", j)]


def test_h3_open_area_over_25pct_stays_yes():
    cfg = _rect(NX=4, NY=4); R._INV_CACHE.clear()
    cfg["plan"] = lambda k, NX, NY: {(i, j) for i in range(NX + 1) for j in range(NY + 1)}
    cfg["diaphragm_openings"] = {2: 0.30 * 16 * 30 * 30}
    cfg["irregularity_statements"] = {"H3": "uniform slab"}
    row = _h3_row(R._irregularity_section(cfg, _runs(), R._sfrs(cfg)))
    assert "Type 3 DIAPHRAGM DISCONTINUITY: YES" in row and "uniform slab" in row

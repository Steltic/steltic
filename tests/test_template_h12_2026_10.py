"""October 2026 final review (H12): template wording and dimensional limits.

- Dimensional / detailing limits (Lp <= T, Part 10 single-plate ductility thickness, plate within a coped web) print as
  limits and never head a record, the connection table or the scorecard: the headline is the worst strength check.
- "(+1 more check)" is singular.
- Chapter 11 hands overturning and sliding stability to the foundation engineer.
- The MRSA section states the 11.4.5.2 two-period spectrum basis (11.4.5 Exception 2).

    PYTHONPATH=$PWD:$PWD/steel_engine python -m pytest tests/test_template_h12_2026_10.py -q
"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "steel_engine")); sys.path.insert(0, ROOT)

import pytest  # noqa: E402

pytest.importorskip("engine3d")
import report as R  # noqa: E402

SRC = open(os.path.join(ROOT, "steel_engine", "report.py"), encoding="utf-8").read()


def _ck(ls, dem, cap, **kw):
    d = {"limit_state": ls, "cited": "AISC 360-22", "Ru_kip": dem, "phiRn_kip": cap, "DC": round(dem / cap, 3)}
    d.update(kw)
    return d


def _fit(Lp, T, tag=True):
    d = {"limit_state": f"plate length within the beam's flat web depth: Lp = {Lp:g} in <= T = {T:g} in (W21X50)",
         "cited": "AISC Manual Part 10", "demand": Lp, "capacity": T, "units": "in", "DC": round(Lp / T, 3)}
    if tag:
        d["detailing_limit"] = True
    return d


def test_detailing_limit_by_tag_or_label():
    assert R._is_detailing_limit(_fit(18, 18.375))
    assert R._is_detailing_limit(_fit(18, 18.375, tag=False))
    assert R._is_detailing_limit({"limit_state": "single-plate rotational ductility, conventional configuration (5 bolts)"})
    assert not R._is_detailing_limit(_ck("bolt group shear J3.7", 50, 80))


def test_governing_dc_sets_aside_a_fit_ratio_headline():
    rec = {"id": "conn-x", "DC": 0.98, "checks": [_ck("bolt group shear J3.7", 70.0, 78.8), _fit(18, 18.375)]}
    dc, gk, lim = R._governing_dc(rec)
    assert abs(dc - 0.888) < 1e-3 and gk["limit_state"].startswith("bolt group") and lim


def test_governing_dc_keeps_a_strength_headline_above_the_checks():
    rec = {"id": "conn-y", "DC": 0.95, "checks": [_ck("weld J2.4", 40.0, 50.0), _fit(9, 15.5)]}
    assert R._governing_dc(rec)[0] == 0.95          # not a limit ratio: the recorded headline stands


def test_connection_table_counts_and_headlines():
    pkg = {"connections": [{"id": "conn-x", "type": "single plate", "demand": "V = 70 kip", "DC": 0.98,
                            "checks": [_ck("bolt group shear J3.7", 70.0, 78.8), _fit(18, 18.375)]}]}
    html = R._connection_summary(pkg)
    assert "(+1 more check)" in html and "more checks)" not in html
    assert "bolt group shear J3.7" in html and "0.98" not in html


def test_chapter11_and_spectrum_wording():
    assert "confirmed against the geotechnical capacities" not in SRC
    assert "to be confirmed by the foundation engineer with these" in SRC
    assert "two-period spectrum of &sect;11.4.5.2" in SRC and "&sect;11.4.5 Exception 2" in SRC

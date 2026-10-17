"""October 2026 re-run fixes: coupled composite plate shear walls (ASCE 7-22 Table 12.2-1 B.28 / D.15,
AISC 341-22 H8) in the three system parsers, and the Chapter 10 connection type of moment-connected
floor / roof beams taken from the connection schedule.

    PYTHONPATH=$PWD:$PWD/steel_engine python -m pytest tests/test_ccpsw_mf_conn_2026_10.py -q
"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "steel_engine")); sys.path.insert(0, ROOT)

import pytest  # noqa: E402

E = pytest.importorskip("engine3d")
import preflight as PF  # noqa: E402
import report as RP  # noqa: E402


def test_sfrs_key_coupled_composite_walls():
    assert E.sfrs_key("dual: SMF + steel and concrete coupled composite plate shear walls") == "DUAL_SMF_CCPSW"
    r = E.SFRS_TABLE["DUAL_SMF_CCPSW"]
    assert (r["R"], r["Om0"], r["Cd"]) == (8.0, 2.5, 5.5)
    assert E.sfrs_key("coupled composite plate shear walls (CC-PSW/CF)") == "CCPSW"
    assert E.sfrs_key("steel and concrete composite plate shear walls") == "CPSW"
    assert E.sfrs_key("SPSW") == "SPSW"
    assert E.sfrs_key("dual: SMF + SPSW") == "DUAL_SMF_SPSW"


def test_preflight_parse_coupled_composite_walls():
    ps = PF.parse_system("dual system: SMF + steel and concrete coupled composite plate shear walls")
    assert ps["kind"] == "dual" and ps["rows"] == ["dual:smf+cc-psw"]
    ps = PF.parse_system("CC-PSW/CF")
    assert ps["rows"] == ["cc-psw"]
    assert PF.parse_system("C-PSW")["rows"] == ["c-psw"]


def test_report_families_coupled_vs_uncoupled():
    assert RP._families("dual: SMF + coupled composite plate shear walls on grids B and C") == ["SMF", "CC-PSW"]
    assert RP._families("steel and concrete composite plate shear walls") == ["C-PSW"]
    assert RP.FAM_341["CC-PSW"] == "H8"


def test_chapter10_moment_beam_from_connection_schedule():
    cfg = dict(system="SMF", heights=[180.0, 156.0])
    sf = dict(families=["SMF"], dirs={"X": ["SMF"], "Y": ["SMF"]}, has_mf=True, has_br=False)
    pkg = {"members": [
        {"id": "floor-W30X132", "inputs": {"kind": "beam", "section": "W30X132", "role": "floor",
                                            "V_kip": 61.3, "Mz_kipin": 8066.0}},
        {"id": "floor-W24X55", "inputs": {"kind": "beam", "section": "W24X55", "role": "floor", "V_kip": 52.4}}],
        "connections": [
        {"id": "conn-floor-W30X132", "type": "SMF reduced beam section (RBS) moment connection, AISC 358-22"},
        {"id": "conn-floor-W24X55", "type": "single-plate shear connection -- gravity beam ends"}]}
    html = RP._connection_demands(cfg, pkg, sf=sf, ctx={"a341": True})
    html = html if isinstance(html, str) else str(html)
    i30, i24 = html.find("W30X132"), html.find("W24X55")
    assert i30 >= 0 and i24 >= 0
    seg30 = html[i30:html.find("</tr>", i30)]
    seg24 = html[i24:html.find("</tr>", i24)]
    assert "moment connection" in seg30
    assert "moment connection" not in seg24

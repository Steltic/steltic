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


def test_deflection_rows_say_which_group(monkeypatch):
    g1 = dict(sec="W24X62", L=336.0, trib=None, roof=False, dLL=0.30, dTL=0.93, rLL=0.330, rTL=0.664)
    g2 = dict(sec="W24X62", L=336.0, trib=None, roof=False, dLL=0.30, dTL=0.84, rLL=0.333, rTL=0.600)
    monkeypatch.setattr(R.E, "beam_serviceability_detail", lambda cfg: {"groups": [g1, g2], "LL": 0.333, "TL": 0.664})
    html = R._deflection_section({"heights": [168.0], "D_floor": 50.0})
    assert "W24X62 (worst total)" in html and "W24X62 (worst live)" in html


def test_short_list_overflow_prints_in_full():
    rows = [{"level": i, "V_kip": float(i)} for i in range(41)]
    html = R._record_html({"rows": rows})
    assert "further row" not in html
    html2 = R._record_html({"rows": [{"level": i, "V_kip": float(i)} for i in range(55)]})
    assert "15 further rows" in html2


def test_single_angle_member_states_its_r_basis():
    m = {"id": "vessel-leg-diagonal-L4X4X1/2", "section": "L4X4X1/2",
         "inputs": {"kind": "brace", "role": "floor", "section": "L4X4X1/2", "length_in": 92.5},
         "checks": [{"limit_state": "compression, single angle", "cited": "AISC 360-22 E5 (Eq. E5-1)",
                     "Pu_kip": 30.0, "phiPn_kip": 40.0, "DC": 0.75}], "DC": 0.75}
    html = R._member_calc_block(m)
    assert "not found in the AISC Shapes Database" not in html and "single angle" in html and "E5" in html


def test_non_sfrs_brace_kind_uses_its_connection_record_and_358_for_spsw():
    assert "not an SFRS brace connection" in SRC
    assert 'any(f in ("SMF", "IMF", "SPSW") for f in _fams)' in SRC
    assert 're.search(r"AISC[\\s_-]*358", _ct)' in SRC


def test_modal_mass_one_decimal_and_grid_names():
    html = R._modal_mass_check([0.7, 0.2, 0.096], [0.8, 0.198])
    assert "99.6%" in html and "100%" not in html
    R._QA.clear(); R._QA["grid_names"] = {"x": ["1", "2", "3"], "y": ["A", "B"]}
    assert R._grid_txt(201) == "(3, B)"
    R._QA.clear()
    assert R._grid_txt(201) == "(2, 1)"
    assert "re-logged at the last fill" not in SRC


def test_ocbf_v_beam_cases_can_be_declared_absent():
    import design_pipeline as DP
    src = open(DP.__file__, encoding="utf-8").read()
    assert 'cfg.get("ocbf_v_beams", True) is not False' in src


def test_web_strip_and_rod_members_state_their_basis():
    for sec, txt in (("WEB3/16", "tension-field strips"), ("ROD1", "tension-only rod")):
        m = {"id": "brace-" + sec, "section": sec, "inputs": {"kind": "brace", "section": sec, "length_in": 240.0},
             "checks": [{"limit_state": "tension yielding", "cited": "AISC 360-22 D2", "Tu_kip": 10.0,
                         "phiTn_kip": 20.0, "DC": 0.5}], "DC": 0.5}
        html = R._member_calc_block(m)
        assert txt in html and "not found in the AISC Shapes Database" not in html


def test_deflection_tie_tags_each_ratio_once(monkeypatch):
    g1 = dict(sec="W24X62", L=336.0, trib=None, roof=False, dLL=0.30, dTL=0.93, rLL=0.3330000000001, rTL=0.664)
    g2 = dict(sec="W24X62", L=336.0, trib=None, roof=False, dLL=0.30, dTL=0.84, rLL=0.3330000000002, rTL=0.600)
    monkeypatch.setattr(R.E, "beam_serviceability_detail", lambda cfg: {"groups": [g1, g2], "LL": 0.333, "TL": 0.664})
    html = R._deflection_section({"heights": [168.0], "D_floor": 50.0})
    assert html.count("worst live") == 1 + html.count("worst live ratio") and "W24X62 (worst total)" in html


def test_identical_deflection_rows_merge(monkeypatch):
    g1 = dict(sec="W24X55", L=360.0, trib=None, roof=False, dLL=0.40, dTL=0.90, rLL=0.40, rTL=0.60)
    g2 = dict(g1, rLL=0.40000000001)                       # same printed numbers, another group
    g3 = dict(sec="W21X44", L=300.0, trib=None, roof=False, dLL=0.10, dTL=0.30, rLL=0.12, rTL=0.24)
    monkeypatch.setattr(R.E, "beam_serviceability_detail", lambda cfg: {"groups": [g1, g2, g3], "LL": 0.4, "TL": 0.6})
    html = R._deflection_section({"heights": [168.0], "D_floor": 50.0})
    assert html.count("<td>W24X55") == 1


def test_ch10_brace_branches_and_ch6_governing_text():
    assert 'transfer[- ]truss|truss diagonal' in SRC
    assert '_brole not in ("brace", "sfrs") or re.search(r"plan bracing|horizontal truss", _bty, re.I)' in SRC
    assert 'f"{cx:.1f}% / {cy:.1f}%"' in SRC and '"%.1f%%" % (100.0 * float(q.get("mass", 0) or 0))' in SRC
    assert 'ls_txt = gchk.get("limit_state")' in SRC


def test_roof_deflection_per_level_and_r_table():
    cfg = {"roof_deflection": {1: "nonplaster", "default": "plaster"}}
    assert R.E.defl_limits(cfg, True, 1) == (240.0, 180.0) and R.E.defl_limits(cfg, True, 2) == (360.0, 240.0)
    assert R.E.defl_limits({"roof_deflection": "none"}, True) == (180.0, 120.0)
    txt = R._roof_defl_txt(cfg)
    assert "level 1 roof supporting a nonplaster ceiling: L/240 and L/180" in txt and "other roof members" in txt
    assert R._r_table({"system": "OCBF, ASCE 7-22 Table 15.4-1 nonbuilding"}) == "Table 15.4-1"
    assert R._r_table({"system": "SCBF"}) == "Table 12.2-1"


def test_occupied_top_roof_live_in_theta_px():
    import static_model as SM
    src = open(R.E.__file__, encoding="utf-8").read()
    assert "_SM.occupied_roof_live_psf(cfg, k)" in src
    sm = open(SM.__file__, encoding="utf-8").read()
    assert "e.g. %s" not in sm.split("not on a grid span")[0][-400:] and "carry no floor load in the analysis model" in sm


def test_uplift_sentence_names_the_design_tension():
    pkg = {"connections": [{"id": "conn-base-A1", "type": "exposed base plate, anchor rods",
                            "demand": {"T_design_kip": 71.6}}]}
    import types
    R._base_reactions_orig = R._base_reactions
    try:
        R._base_reactions = lambda p: {101: {"uplift": 54.2, "uplift_combo": "0.9D-E"}}
        html = R._uplift_summary(pkg)
    finally:
        R._base_reactions = R._base_reactions_orig
    assert "54.2" in html and "71.6" in html


def test_capacity_dict_labels_design_values():
    m = {"id": "floor-W21X50", "inputs": {"kind": "beam", "section": "W21X50"},
         "capacity": {"phiMn_kipft": 412.0, "n_studs": 24, "camber_in": 0.75}, "DC": 0.8}
    html = R._member_calc_block(m)
    assert "design value" in html and "available strength" in html


def test_ch10_lists_joint_records_and_notch_clauses():
    assert '_mid = "conn-" + str(m.get("id", "")).lower() + "-"' in SRC
    assert "(AISC 360-22 A3.1d, A3.1e)" in SRC and "A3.1c, A3.1d" not in SRC


def test_type4_cites_12_10_1_1_only_and_4a_names_every_direction():
    assert "12.10.1.1/12.10.3.3" not in SRC
    assert "every direction with a story weaker than the story above" in SRC


def test_spsw_ch10_basis_and_splice_system_clause_and_ecl_scope():
    # SPSW web strips (brace elements) print the web-to-boundary record and the F5.6c basis, not "Ch. J"
    assert '"SPSW" in fams and _bre and re.search(r"web plate", _bty, re.I)' in SRC
    assert "web expected tension R<sub>y</sub>F<sub>y</sub>t at &alpha; (AISC 341-22 F5.6c)" in SRC
    # Ch. 10 splice rows name the system splice clause of the record (E3.6g / F2.6d / F3.6d / F4.6d)
    assert r'E3\.6g|F2\.6d|F3\.6d|F4\.6d' in SRC
    # capacity-limited cases are not described as governing columns only
    assert "other SFRS members, connections and bases by their capacity-design records" in SRC


def test_r26c_scorecard_record_deflection_chords_uplift_gravity_note():
    # scorecard beam deflection also reports the design-record deflection rows
    pkg = {"members": [{"id": "infill-W27X84", "checks": [
        {"limit_state": "total deflection: wet load on the bare beam", "DC": 0.69},
        {"limit_state": "story drift ratio", "DC": 0.95}]}]}
    assert R._record_deflection_max(pkg) == (0.69, "infill-W27X84")
    assert R._record_deflection_max({"members": []}) is None
    assert "design-record deflection rows up to %.2f (%s)" in SRC
    # rigid-diaphragm chord clause, capacity-limited uplift wording and the design-basis gravity note come from cfg
    assert 'cfg.get("diaphragm_chords")' in SRC
    assert '"net uplift, from the capacity-limited combination," if _ucl else "analysis net uplift"' in SRC
    assert 'cfg.get("design_basis_gravity_note")' in SRC

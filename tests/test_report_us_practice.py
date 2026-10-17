"""US-practice calc-package regressions (branch fix/report-us-practice-2026-10).

Publication text and number formatting of report.py, the consistency guards (printed envelope vs capacity,
AISC 360-22 H1-1a / H1-1b from the printed terms, stale overstrength combinations, single-plate bolt-group
eccentricity, module verdicts, ASCE 7-16 equation numbers, brace r), the governing brace r of sections.py,
and the per-direction seismic / service-wind / R <= 3 combination text.

Run from the repo root:  PYTHONPATH=$PWD:$PWD/steel_engine python -m pytest tests -q"""
import os, re, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "steel_engine")); sys.path.insert(0, ROOT)

import pytest  # noqa: E402
import engine3d as E  # noqa: E402
import design_pipeline as DP  # noqa: E402
import report as R  # noqa: E402
import consistency as C  # noqa: E402
import sections as S  # noqa: E402

MODEL = dict(bases="fixed", joints="rigid", gravity="framed")


def _cfg(**kw):
    c = dict(arch="test frame", NX=3, NY=1, SX=360.0, SY=360.0, heights=[156.0] * 3, base="fixed",
             col="W14X120", beam="W24X55", seis=E.seis(1.0, 0.6, 0.6, 8, 0.028, 0.8, 1.4),
             system="SMF", sdc="D", rho=1.3, analyses=["ELF"], model=dict(MODEL), **E.D)
    c.update(kw)
    E.clear_caches()
    return c


def _r3(**kw):
    """R = 3 'not specifically detailed' braced building at low seismicity (SDC B)."""
    return _cfg(seis=E.seis(0.25, 0.10, 0.05, 3, 0.02, 0.75, 1.7),
                system="steel systems not specifically detailed for seismic resistance", sdc="B", rho=1.0, **kw)


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# ------------------------------------------------------------------ one number formatter / feet-inch
def test_number_formatter_and_feet_inch_helpers():
    assert R._n(None) == "&mdash;" and R._n("None") == "&mdash;" and R._n(float("nan")) == "&mdash;"
    assert R._n(1214.5) == "1,215"                                   # round half up, thousands separator
    assert R._n(8024) == "8,024" and R._n(8024.0) == "8,024"         # int and float alike
    assert R._n(247.28) == "247.3" and R._n(12.0) == "12" and R._n(0.0308) == "0.0308"
    assert R._n(0.7126, dc=True) == "0.713" and R._n(0.8, dc=True) == "0.80"
    assert R._ft(150) == "12.5" and R._ft(270) == "22.5"             # no round-half-even trap
    assert R._ftin(150) == "12'-6\"" and R._ftin(180) == "15'-0\"" and R._ftin(162) == "13'-6\""
    s = R._fmt({"P_kip": 12.0, "note": None, "x": "", "Mz_kipin": 1200.0})
    assert s == "P = 12 kip; M<sub>x</sub> = 100 kip-ft"             # kip-in printed in kip-ft, empties omitted


def test_combination_labels_are_typeset_consistently():
    lab = R._combo_label("(1.2+0.2SDS)D+rhoEX+t+/0.3Y-+0.5L+0.15S")
    assert lab == "(1.2+0.2S<sub>DS</sub>)D+&rho;E<sub>X</sub>+t+/0.3Y-+0.5L+0.15S"
    assert "&Omega;<sub>0</sub>E<sub>Y</sub>" in R._combo_label("(0.9-0.2SDS)D+Om0*EY-/0.3X+ [col]")
    assert R._combo_label("1.2D+1.6Lr+0.5WX+") == "1.2D+1.6L<sub>r</sub>+0.5W<sub>X</sub>+"
    assert R._combo_label("1.2D+1.6L+0.5Lr +Ni(X+)").endswith("+N<sub>i</sub>(X+)")
    assert R._combo_label(None) == "&mdash;"
    tab = R._combo_table([("(1.2+0.2SDS)D+rhoEX+t+/0.3Y-+0.5L", 1.32, 0.5, 0.0, {1: (1.0, 0.0)}, False)])
    assert "rhoE" not in tab and "&rho;E<sub>X</sub>" in tab


# ------------------------------------------------------------------ publication text
def test_publication_text_drops_harness_wording():
    t = R._pub("Walking per DG11 (Cj 2.0). DG11 is NOT in the RAG corpus -- method applied from memory "
               "(declared fallback, not verified against the corpus).")
    assert "RAG" not in t and "corpus" not in t and "memory" not in t and t.startswith("Walking per DG11 (Cj 2.0).")
    assert "cfg" not in R._pub("set cfg['wind']['V'] = 100") and "the declared wind / V input" in R._pub("cfg['wind']['V']")
    assert R._pub("Direct Analysis Method, AISC 360-22 C2 (see 'dam')") == "Direct Analysis Method, AISC 360-22 C2"
    assert R._pub("example I.1 (steel_design_examples)") == "example I.1 (AISC Design Examples)"
    assert R._pub("b/t <= 0.3 sqrt(E/Fy)") == "b/t ≤ 0.3 √(E/Fy)"
    assert R._pub("camber per (brief)") == "camber per (project criteria)"
    assert "framework" not in R._pub("framework collector axial").lower()
    iss = R._pub_issue("[connection conn-base] no envelope (demand_analysis.base_reactions) -- set {'P': 1}")
    assert "demand_analysis" not in iss and "{" not in iss and iss.startswith("[connection conn-base] no envelope")


def test_design_credit_line_only_when_recorded():
    assert R._design_credit({}) == "" and R._design_credit({"design_agent": {}}) == ""
    pkg = {"design_agent": {"model": "Claude Opus 5.5", "provider": "Anthropic", "date": "2026-10-17"}}
    assert R._design_credit(pkg) == "Design prepared with Steltic; design agent: Claude Opus 5.5 (Anthropic), 2026-10-17"
    assert R._design_credit({"design_agent": "Claude Opus 5.5"}) == "Design prepared with Steltic; design agent: Claude Opus 5.5"


def test_record_tables_are_never_nested_in_cells():
    rec = {"basis": "AISC 360-22 Ch. I", "T_model": {"X": 1.36, "Y": 1.259},
           "members": {"infill beam W21X55": {"section": "W21X55", "span_ft": 40, "b_eff_in": 120, "SumQn_kip": 417,
                                              "studs_total": 52, "camber_in": 0.75, "PNA": "top flange",
                                              "vibration": {"fn_hz": 4.19, "ap_over_g_pct": 0.48}}}}
    html = R._record_html(rec)
    assert not re.search(r"<td>\s*<table", html)
    assert "X = 1.36; Y = 1.259" in html                             # small nested record written inline
    assert "Members &mdash; infill beam W21X55" in html              # larger one as its own titled table


# ------------------------------------------------------------------ AISC 360-22 H1-1a / H1-1b (coordinator request)
def test_h1_1b_uses_half_the_axial_term():
    def run(ch):
        return C._entry_issues("member", {"id": "m", "checks": [ch]})
    b = {"limit_state": "H1-1b axial + flexure", "Pr_kip": 100.0, "Pc_kip": 1000.0, "Mrx_kipft": 200.0,
         "Mcx_kipft": 400.0, "DC": 0.55, "cited": "AISC 360-22 H1.1"}           # 100/(2x1000) + 200/400 = 0.55
    assert not run(b)
    assert any("Eq. H1-1b from its own printed terms = 0.550" in i for i in run(dict(b, DC=0.65)))
    assert not run(dict(b, limit_state="H1-1 combined"))                       # Pr/Pc = 0.1 < 0.2 -> H1-1b
    assert not run({"limit_state": "H1-1b axial", "Pr_kip": 116.0, "Pc_kip": 1000.0, "DC": 0.058, "cited": "H1"})
    a = {"limit_state": "H1-1a axial + flexure", "Pr_kip": 494.1, "Pc_kip": 1211.5, "DC": 0.712, "cited": "H1"}
    assert not run(a)                                                          # H1-1a: Pr/Pc alone
    # the unconservative form for the axial ratio is flagged (H1.1: H1-1b only for Pr/Pc < 0.2)
    bb = {"limit_state": "H1-1b", "Pr_kip": 400.0, "Pc_kip": 1000.0, "Mrx_kipft": 100.0, "Mcx_kipft": 400.0,
          "DC": 0.45, "cited": "H1.1"}
    assert any("requires Eq. H1-1a" in i for i in run(bb))


# ------------------------------------------------------------------ printed envelope vs capacity / check demand
def _member(cap, check_cap, check_dem=158.0, ls="F2-1 flexure", dc=0.79):
    return {"id": "roof-b", "inputs": {"section": "W24X55", "Mz_kipin": 305 * 12.0, "V_kip": 30.0},
            "capacity": {"phiMn_kipft": cap}, "DC": dc, "limit_state": "F2 flexure", "cited": "F2",
            "checks": [{"limit_state": ls, "Mu_kipft": check_dem, "phiMn_kipft": check_cap, "DC": check_dem / check_cap,
                        "cited": "F2.1"}]}


def test_envelope_rules_flag_ng_and_low_check_demands():
    out = C.entry_findings("member", _member(200.0, 200.0))
    assert any("= 1.525 > 1.0 (NG" in i for i in out)                          # 305 / 200, non-waivable form
    assert any("uses Mu_kipft = 158 < the printed analysis envelope 305" in i for i in out)
    ok = C.entry_findings("member", _member(400.0, 400.0, check_dem=305.0, dc=0.7625))
    assert not ok
    sub = C.entry_findings("member", _member(400.0, 400.0, ls="F2-1 flexure (subgroup: interior spans)", dc=0.395))
    assert not any("uses Mu_kipft" in i for i in sub)                         # stated basis accepted
    assert any("> headline D/C" in i for i in sub)                             # but the headline must cover it


def test_stale_overstrength_combination_and_guards():
    m = {"id": "c", "inputs": {"governing_combo": "(1.2+0.2SDS)D+Om0*EY+/0.3X++0.5L [col]"}}
    assert C.stale_combo_issue("member", m, {"1.4D"})
    assert not C.stale_combo_issue("member", m, None)
    tab = {"id": "c1", "type": "single-plate shear connection",
           "checks": [{"limit_state": "J3.6 bolt shear", "Vu_kip": 20.0, "phiRn_kip": 40.0, "DC": 0.5}]}
    assert C._shear_tab_issues({"connections": [tab]})
    tab["checks"][0]["limit_state"] = "J3.6 bolt shear, eccentric bolt group e = a/2 (Manual Part 10)"
    assert not C._shear_tab_issues({"connections": [tab]})
    ddm = {"ddm_analysis": {"verdict": "FAIL", "combinations": [{"label": "1.4D", "check": "phi_s*lambda_u = 0.95 FAIL"}]}}
    assert any("verdict FAIL" in i for i in C._module_verdict_issues(ddm))
    assert not C._module_verdict_issues({"ddm_analysis": {"verdict": "PASS"}})


def test_asce7_16_equation_numbers_are_flagged():
    rec = {"loads_hand_check": {"Cs_eqs": "12.8-2 SDS/(R/Ie) = 0.0633; 12.8-3 SD1/(T R/Ie) = 0.0308 governs; "
                                          "12.8-5 min 0.044 SDS Ie = 0.0167", "Ta": "Ta = Ct hn^x (Eq. 12.8-7)"}}
    out = C._asce722_eq_issues(rec)
    assert len(out) == 4 and all("ASCE 7-16 number" in i for i in out)
    ok = {"x": "Cs = SDS/(R/Ie) = 0.0633 (Eq. 12.8-3); SD1/(T R/Ie) = 0.0308 (Eq. 12.8-4); Ta = Ct hn^x (Eq. 12.8-8)"}
    assert not C._asce722_eq_issues(ok)


# ------------------------------------------------------------------ governing brace r
def test_brace_r_is_the_least_radius_and_never_guessed():
    assert S.brace_r("HSS8X4X1/2") == pytest.approx(1.56)                      # ry, not rx = 2.71
    assert S.brace_r("HSS5X5X3/8") == pytest.approx(1.87)                      # Shapes Database v16.0
    assert S.brace_r("HSS6.625X.280") == pytest.approx(2.25)                   # Manual label of a round HSS
    assert S.brace_r("W8X31") == pytest.approx(2.02)
    assert all(S.brace_r(k) > 0 for k in E.HSS)                                # every engine brace key resolves
    for bad in ("W99X999", "L4X4X1/2"):                                        # unknown / single angle (rz)
        with pytest.raises(KeyError):
            S.brace_r(bad)
    pkg = {"members": [{"id": "b1", "inputs": {"kind": "brace", "section": "HSS8X4X1/2", "r": 2.71}},
                       {"id": "b2", "inputs": {"kind": "brace", "section": "HSS8X4X1/2", "r": 1.56}}]}
    out = C._brace_r_issues(pkg)
    assert len(out) == 1 and "[member b1]" in out[0] and "1.56" in out[0]


# ------------------------------------------------------------------ DDM: seismic combinations are information only
def test_ddm_seismic_combinations_are_not_printed_as_pass():
    pkg = {"ddm_analysis": {"verdict": "PASS", "combinations": [
        {"label": "1.4D", "kind": "gravity", "lambda_u": 2.0, "phi_s": 0.8, "check": "phi_s*lambda_u = 1.600 PASS"},
        {"label": "(1.2+0.2SDS)D+rhoEX+t+/0.3Y++0.5L", "kind": "seismic", "lambda_u": 2.1, "phi_s": None,
         "check": "n/a"}]}}
    html = R._ddm_summary(pkg)
    assert "information only" in html and "(1 combination checked)" in html
    assert "1 combination (seismic) without an established system resistance factor" in html
    assert html.count(">PASS<") == 1


# ------------------------------------------------------------------ Chapter 1 / 3 / 4 / 8 content
def test_hazard_table_snow_rain_and_tornado():
    c = _cfg()
    t = _text(R._design_basis_codes(c, c["seis"]))
    assert "Snow load (ASCE 7-22 Ch. 7) not applicable at site" in t
    assert "Rain load (ASCE 7-22 Ch. 8) not applicable at site" in t and "Tornado" not in t
    c3 = _cfg(risk_category="III", seis=E.seis(1.0, 0.6, 0.6, 8, 0.028, 0.8, 1.4, Ie=1.25), snow=25.0)
    t3 = _text(R._design_basis_codes(c3, c3["seis"]))
    assert "Tornado loads (ASCE 7-22 Ch. 32)" in t3 and "design roof snow load S = 25 psf" in t3
    assert "not evaluated in this package (roof drainage design by others)" in t3
    tr = _text(R._design_basis_codes(_r3(), _r3()["seis"]))
    assert "seismic provisions: ductile detailing" not in tr and "AISC 358" not in tr
    assert "AISC 341-22 does not apply" in tr


def test_seismic_section_is_per_direction_and_rederivable():
    c = _cfg(NX=3, NY=2, system_X="SMF", seis_Y=dict(R=6, Cd=5.0, Om0=2.0, Ct=0.02, x=0.75, system="SCBF"),
             rho_X=1.0, rho_Y=1.3)
    T, w2, eX, eY, Mt = E.modal(c, 6)
    html = R._seismic_loads_section(c, T, eX, eY)
    m = re.findall(r"V<sub>([XY])</sub> = ([0-9.]+) &times; ([0-9,]+) = ([0-9,.]+) kip", html)
    assert {d for d, *_ in m} == {"X", "Y"}
    for _d, cs, w, v in m:                                                     # V = Cs W from the printed numbers
        assert float(cs) * float(w.replace(",", "")) == pytest.approx(float(v.replace(",", "")), rel=1e-3)
    assert "Eq. 12.8-12, 12.8-13" in html


def test_sfrs_recognises_the_not_detailed_system_per_direction():
    nsd = "steel systems not specifically detailed for seismic resistance"
    sf = R._sfrs(_r3(system_X=nsd, system_Y=nsd))
    assert sf["declared"] and sf["recognised"]
    for d in ("X", "Y"):
        lab = R._sys_label(sf, d)
        assert lab.startswith("steel system not specifically detailed for seismic resistance")
        assert "not declared" not in lab and "?" not in lab


def test_mrsa_section_renders_scaling_and_story_shears():
    c = _cfg(analyses=["ELF", "RS"])
    T, w2, eX, eY, Mt = E.modal(c, 6)
    t = _text(R._seismic_loads_section(c, T, eX, eY))
    assert "Modal response spectrum analysis (&sect;12.9.1)" in t and "CQC combination, 5% damping" in t
    assert "force scale V/V t" in t and "scaled MRSA X (kip)" in t


def test_r3_combinations_carry_no_overstrength():
    c = _r3()
    cases = DP.combos(c)
    assert not any("Om0" in x[0] for x in cases)
    r6 = _cfg(system="SCBF", seis=E.seis(0.5, 0.3, 0.2, 6, 0.02, 0.75, 1.4), sdc="D", rho=1.0)
    assert any("Om0" in x[0] for x in DP.combos(r6))                            # AISC 341 D1.4a columns: unchanged
    notes = _text(R._combo_notes(c, cases, {}))
    assert "No overstrength" in notes and "AISC 341 does not apply" in notes
    assert "[col]" not in R._combo_legend(c, cases)


def test_service_wind_drift_uses_the_appendix_cc_basis():
    w = dict(V=115.0, exposure="C")
    html = R._wind_drift_section(_cfg(wind=w, wind_service={"V": 76.0, "MRI": 10}))
    t = _text(html)
    assert "Fig. CC.2-2" in t and "Eq. CC.2-3" in t and "information only" in t
    plain = _text(R._wind_drift_section(_cfg(wind=w)))
    assert "conservative for a serviceability check" in plain


def test_base_reaction_chapter_lists_the_uplift_envelope():
    pkg = {"demand_analysis": {"base_reactions": {
        "100000": {"P": 410.0, "P_combo": "1.2D+1.6L+0.5Lr", "uplift": 0.0, "V": 12.0, "V_combo": "1.2D+1.0WX+"},
        "100103": {"P": 250.0, "P_combo": "1.2D+1.6L+0.5Lr", "uplift": 96.5,
                   "uplift_combo": "(0.9-0.2SDS)D+rhoEX+t+/0.3Y+", "V": 40.0, "V_combo": "(0.9-0.2SDS)D+rhoEX+t+/0.3Y+"}}}}
    t = _text(R._base_reactions_section(_cfg(), pkg))
    assert "96.5 kip" in t and "(0.9&minus;0.2S DS )D+&rho;E X +t+/0.3Y+" in t

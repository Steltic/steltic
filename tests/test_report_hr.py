"""Report / gate regressions for HR-22, HR-24, HR-25, HR-26, HR-28, HR-38 and HUB-09.

Run from the repo root:  PYTHONPATH=$PWD:$PWD/steel_engine python -m pytest tests -q
The OpenSees models used here are small built-in archetypes (4 stories, a few seconds in total)."""
import copy, math, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "steel_engine")); sys.path.insert(0, ROOT)

import pytest  # noqa: E402
import engine3d as E  # noqa: E402
import report as R  # noqa: E402


def _b05(**kw):
    """4-story L-plan moment frame (built-in archetype B05), rigid joints, full-grid example build."""
    c = copy.deepcopy({k: v for k, v in E.CFG["B05"].items()})
    c["model"] = {"bases": "fixed", "joints": "rigid", "gravity": "framed"}
    c.update(kw)
    return c


def _rect(**kw):
    c = _b05(**kw); c.pop("plan", None); return c


# ------------------------------------------------------------------ HR-22 RBS drift factor
def test_rbs_factor_interpolates_per_aisc358_5_7_step1():
    assert E.rbs_drift_factor({}, "X") == (1.0, "no RBS declared")
    assert E.rbs_drift_factor({"rbs": True}, "X")[0] == pytest.approx(1.10)
    # c = 0.25 bbf -> 50 % flange reduction -> 1.10 ; c = 0.20 bbf -> 40 % -> 1.08 ; c = 0.10 bbf -> 1.04
    assert E.rbs_drift_factor({"rbs": {"c_over_bf": 0.25}}, "X")[0] == pytest.approx(1.10)
    assert E.rbs_drift_factor({"rbs": {"c_over_bf": 0.20}}, "Y")[0] == pytest.approx(1.08)
    assert E.rbs_drift_factor({"rbs": {"flange_reduction": 0.20}}, "Y")[0] == pytest.approx(1.04)
    assert E.rbs_drift_factor({"rbs": {"c_over_bf": 0.25, "dirs": ["Y"]}}, "X")[0] == 1.0
    assert E.rbs_drift_factor({"rbs_drift_factor": {"X": 1.07, "Y": 1.0}}, "X")[0] == pytest.approx(1.07)
    assert E.rbs_drift_factor({"rbs_drift_factor": 0.9}, "X")[0] == 1.0          # never reduces drift


def test_rbs_named_in_text_applies_only_to_the_moment_frame_direction():
    cfg = {"system": "mixed: SMF in X (Grids A, D) + SCBF in Y", "arch": "office, RBS connections"}
    assert E.rbs_drift_factor(cfg, "X")[0] == pytest.approx(1.10)
    assert E.rbs_drift_factor(cfg, "Y")[0] == 1.0
    assert E.rbs_drift_factor({"system": "SMF", "arch": "perimeter SMF (RBS)"}, "Y")[0] == pytest.approx(1.10)


def test_drift_gate_and_report_apply_the_same_rbs_factor():
    base = _rect(); rbs = _rect(rbs={"c_over_bf": 0.25})
    E.clear_caches(); r0 = E.run(base); E.clear_caches(); r1 = E.run(rbs)
    assert r1["mdx"] / r0["mdx"] == pytest.approx(1.10, rel=1e-6)
    assert r1["mdy"] / r0["mdy"] == pytest.approx(1.10, rel=1e-6)
    T, w2, eX, eY, Mt = E.modal(rbs, 6); Fx = E.elf(rbs, T[0])[5]
    R._INV_CACHE.clear(); runs = R._lateral_runs(rbs, Fx)
    # the report's governing design drift equals the gate's (same routine, same factor)
    assert runs["X"]["design_max"] == pytest.approx(r1["mdx"], rel=1e-6)
    assert runs["Y"]["design_max"] == pytest.approx(r1["mdy"], rel=1e-6)


# ------------------------------------------------------------------ HR-24 theta
def _fake_runs(cfg, dxe_in, V, Cd=5.5, Ie=1.0, exempt=()):
    NF = len(cfg["heights"]); h = cfg["heights"]
    r = {"V": V, "dxe": [dxe_in[s] / h[s] for s in range(NF)], "Cd": Cd, "Ie": Ie}
    return {"X": r, "Y": dict(r), "exempt": set(exempt)}


def test_theta_hand_calc_unfactored_px_and_floor_of_0_10():
    cfg = _rect(); NF = 4
    dxe = [0.60, 0.55, 0.45, 0.30]; V = [400.0, 350.0, 250.0, 120.0]
    runs = _fake_runs(cfg, dxe, V, Cd=5.5)
    rows, tmax, beta = R._theta_rows(cfg, runs, "X")
    # Px: 1.0 D (incl. cladding) + 0.5 (0.4 L0) on the framed area, no factor > 1.0 (12.8.6.1 / 12.8.7)
    A = E.floor_area_ft2(cfg, 1)
    P = {k: E.floor_dead(cfg, k) + (0.5 * 0.4 * cfg["L_floor"] * A / 1000.0 if k < NF else 0.0) for k in range(1, NF + 1)}
    Px1 = sum(P.values())
    assert rows[0]["Px"] == pytest.approx(Px1)
    raw = Px1 * 0.60 / (400.0 * cfg["heights"][0])
    assert rows[0]["raw"] == pytest.approx(raw)
    assert rows[0]["theta"] == pytest.approx(raw / (1 + raw))            # P-Delta analysis: / (1 + theta)
    assert rows[0]["Delta"] == pytest.approx(0.60 * 5.5)
    assert tmax == pytest.approx(0.10) and beta == 1.0                    # 0.5/5.5 = 0.091 -> floor 0.10


def test_theta_max_cap_and_beta_lower_bound():
    cfg = _rect(); runs = _fake_runs(cfg, [0.1] * 4, [100.0] * 4, Cd=4.0)
    assert R._theta_rows(cfg, runs, "X")[1] == pytest.approx(0.125)       # 0.5/(1.0*4.0)
    runs = _fake_runs(cfg, [0.1] * 4, [100.0] * 4, Cd=1.5)
    assert R._theta_rows(cfg, runs, "X")[1] == pytest.approx(0.25)        # capped
    cfg2 = _rect(theta_beta=0.2); cfg2["seis"]["Om0"] = 2.5                # beta >= 1.25/Om0 = 0.5
    # an explicit Om0 (hr-seismic HR-20: a value still listed in '_auto' is re-derived from the system)
    cfg2["seis"]["_auto"] = [k for k in (cfg2["seis"].get("_auto") or []) if k != "Om0"]
    runs = _fake_runs(cfg2, [0.1] * 4, [100.0] * 4, Cd=4.0)
    t, tm, b = R._theta_rows(cfg2, runs, "X")
    assert b == pytest.approx(0.5) and tm == pytest.approx(0.25)


# ------------------------------------------------------------------ HR-25 irregularities
def test_reentrant_needs_both_projections_over_20pct():
    big = _rect(NX=10, NY=10)
    big["plan"] = lambda k, NX, NY: {(i, j) for i in range(NX + 1) for j in range(NY + 1) if not (i == NX and j == NY)}
    p = E.plan_irregularities(big)                                          # 1x1 notch on 10x10 = 10 %
    assert p["reentrant_any"] and not p["reentrant"]
    assert E.plan_irregularities(_b05())["reentrant"]                       # L-plan 50 % x 50 %


def test_interior_opening_over_25pct_and_level_groups():
    c = _rect(NX=4, NY=4)
    c["plan"] = lambda k, NX, NY: {(i, j) for i in range(NX + 1) for j in range(NY + 1)}
    c["diaphragm_openings"] = {2: 0.30 * 16 * 30 * 30}                     # 30 % of 16 bays of 30x30 ft
    lv = E.plan_irregularity_levels(c)
    assert lv["opening"] and lv["levels"][2]["open_ratio"] > 0.25 and lv["levels"][1]["open_ratio"] == 0
    # split level: two wings at offset levels form an L only when screened together
    w = _rect(NX=4, NY=4, heights=[156.0, 24.0, 132.0, 156.0])
    A = {(i, j) for i in range(0, 3) for j in range(0, 5)}; B = {(i, j) for i in range(2, 5) for j in range(0, 3)}
    w["plan"] = lambda k, NX, NY: A if k in (1, 3) else B
    assert not E.plan_irregularities(w)["reentrant"]
    w["level_groups"] = [[1, 2], [3, 4]]
    assert E.plan_irregularities(w)["reentrant"]


def test_discontinuity_detection_out_of_plane_and_in_plane():
    sf = {"lines": {"X": {0.0: {s: [(0.0, 360.0)] for s in (1, 2, 3, 4)},
                          720.0: {s: [(720.0, 1080.0)] for s in (3, 4)},            # terminates above story 2
                          1440.0: {1: [(0.0, 360.0)], 2: [(360.0, 720.0)]}},       # shifts in its own plane
                    "Y": {}}}
    term, inpl = R._discontinuities({}, sf)
    assert ("X", 720.0, 3) in term and ("X", 1440.0, 2) in inpl and len(term) == 1


def test_irregularity_section_prints_numbers_or_not_evaluated():
    cfg = _b05(); R._INV_CACHE.clear()
    T, w2, eX, eY, Mt = E.modal(cfg, 6); Fx = E.elf(cfg, T[0])[5]
    runs = R._lateral_runs(cfg, Fx)
    html = R._irregularity_section(cfg, runs, R._sfrs(cfg))
    assert "Type 2 RE-ENTRANT: YES" in html and "50%" in html
    assert "NOT EVALUATED" in html and "story strengths" in html            # weak story needs strengths
    assert "Horizontal 4 &mdash; Out-of-plane offset | None" not in html
    assert "Not present: every SFRS plane is continuous" in html
    # weak story evaluated when strengths are given (7-22 Table 12.3-2 Type 4a/4b)
    cfg2 = _b05(story_strength={"X": [500, 900, 800, 600], "Y": [900, 900, 800, 600]})
    html2 = R._irregularity_section(cfg2, runs, R._sfrs(cfg2))
    assert "Type 4b EXTREME WEAK STORY: YES" in html2
    # ASCE 7-22 Type 4a triggers below 100 % of the story above (7-16 Type 5a used 80 %)
    cfg3 = _b05(story_strength={"X": [900, 1000, 1000, 1000], "Y": [1000, 1000, 1000, 1000]})
    html3 = R._irregularity_section(cfg3, runs, R._sfrs(cfg3))
    assert "Type 4a WEAK STORY: YES" in html3 and "90%" in html3
    cfg4 = _b05(story_strength={"X": [1000, 1000, 900, 800], "Y": [1000, 1000, 1000, 1000]})
    html4 = R._irregularity_section(cfg4, runs, R._sfrs(cfg4))
    assert "WEAK STORY: YES" not in html4 and "Not present: lowest story strength ratio" in html4
    # SDC E/F: Type 4a at >= 80 % is permitted by the 12.3.3.1 Exception, below 80 % it is prohibited
    assert "12.3.3.1 applies in SDC E and F" in html3 or "12.3.3.1 Exception" in html3


def test_soft_story_ratio_uses_story_stiffness():
    cfg = _rect(); runs = {"exempt": set(), "sdc": "D"}
    k = [100.0, 200.0, 200.0, 200.0]                                       # story 1 at 50 % -> Type 1b
    for d in ("X", "Y"):
        runs[d] = {"k": k, "drift_cm": [0.02, 0.01, 0.01, 0.01], "tir": [1.0] * 4, "ax": [1.0] * 4}
    R._INV_CACHE.clear()
    html = R._irregularity_section(cfg, runs, R._sfrs(cfg))
    assert "Type 1b EXTREME SOFT STORY: YES" in html and "0.50" in html
    for d in ("X", "Y"):
        runs[d]["drift_cm"] = [0.01, 0.01, 0.01, 0.01]                    # Exception 1: no story > 130 % above
    assert "Exception 1" in R._irregularity_section(cfg, runs, R._sfrs(cfg))


# ------------------------------------------------------------------ HR-26 / HUB-09 system description
def test_system_parsed_per_direction_and_no_moment_frame_default():
    assert R._families_by_dir("mixed per direction: SMF in X (Grids A, D) + SCBF in Y (Grids 1, 4, 7)") == \
        {"X": ["SMF"], "Y": ["SCBF"]}
    assert R._families("dual SMF+BRBF") == ["SMF", "BRBF"]
    assert R._families("STMF (steel special truss moment frame)") == ["STMF"]
    cfg = _rect(system="EBF"); R._INV_CACHE.clear()
    sf = R._sfrs(cfg)                                                       # model has no braces -> loud warning
    assert sf["dirs"]["X"] == ["EBF"] and sf["warnings"]
    html = R._horizontal_distribution(cfg, {1: 10.0, 2: 20.0, 3: 30.0, 4: 40.0}, None, None, sf)
    assert "two moment frames" not in html and "eccentrically braced frame" in html


@pytest.mark.parametrize("system,need", [("SMF", True), ("IMF", True), ("dual SMF+BRBF", True),
                                         ("mixed: SMF in X + SCBF in Y", True), ("STMF", False), ("SCBF", False)])
def test_grounding_requires_a358_for_smf_imf_and_dual(system, need):
    cfg = _rect(system=system); cfg["seis"]["R"] = 8; R._INV_CACHE.clear()
    html = R._grounding_check(cfg, "__no_such_job__", {})
    row = [r for r in html.split("<tr>") if "AISC 358" in r][0]
    assert ("<td>required</td>" in row) == need


# ------------------------------------------------------------------ HR-28 beam deflection gate
def test_beam_deflection_lowercase_label_and_unknown_section():
    assert E._ss_beam_defl("w24x76", 360.0, 0.1) == pytest.approx(E._ss_beam_defl("W24X76", 360.0, 0.1))
    cfg = _rect(); cfg["beam"] = "w33x130"
    rLL, rTL = E.beam_serviceability(cfg)
    assert math.isfinite(rLL) and math.isfinite(rTL)
    bad = _rect(beam="W99X999")
    d = E.beam_serviceability_detail(bad)
    assert d["error"] or d["skipped"]
    assert math.isinf(d["LL"]) and math.isinf(d["TL"])                     # FAILS, never a silent pass
    html = R._deflection_section(bad, {})
    assert "FAIL" in html and "NOT EVALUATED" in html or "check not evaluated" in html


def test_report_deflection_table_is_the_gate():
    cfg = _rect(); det = E.beam_serviceability_detail(cfg)
    html = R._deflection_section(cfg, {})
    gate = "PASS" if det["LL"] < 1 and det["TL"] < 1 else "FAIL"
    assert ("Beam deflection check: %s" % gate) in html
    pkg = {"members": [{"id": "b", "serviceability": {"ok": gate == "FAIL"}}]}
    assert "Reconcile" in R._deflection_section(cfg, pkg)


# ------------------------------------------------------------------ HR-38 tools
def test_geometry_figure_without_cfg_col_and_beam(tmp_path):
    import plot_model as PM
    from example_build import example_build
    inner = _rect()
    cfg = {k: v for k, v in inner.items() if k not in ("col", "beam")}
    cfg["custom_build"] = lambda c, t: example_build(inner, t)
    E.CFG["__T_custom__"] = cfg
    try:
        p = PM.geometry("__T_custom__", str(tmp_path))
        assert os.path.exists(p)
        bx = PM._base_nodes(E.build(cfg, "Linear"))
        assert bx and all(b[3] == "fixed" for b in bx)
    finally:
        E.CFG.pop("__T_custom__", None)


def test_agent_strings_are_escaped():
    html = R._member_calc_block({"id": "<script>x</script>", "inputs": {"section": "W24X76", "kind": "beam"},
                                 "limit_state": "a<b", "DC": 0.5, "cited": "F2"})
    assert "<script>" not in html and "&lt;script&gt;" in html and "a&lt;b" in html


def test_build_transf_is_honoured_by_add_column():
    import openseespy.opensees as ops
    def tip(tr):
        E._BUILD_TRANSF = tr
        ops.wipe(); ops.model("basic", "-ndm", 3, "-ndf", 6)
        ops.node(1, 0.0, 0.0, 0.0); ops.node(2, 0.0, 0.0, 300.0); ops.fix(1, 1, 1, 1, 1, 1, 1)
        E.add_column(1, 1, 2, "W14X90", "X")
        ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1); ops.load(2, 5.0, 0, -600.0, 0, 0, 0)
        ops.constraints("Plain"); ops.numberer("RCM"); ops.system("UmfPack"); ops.test("NormDispIncr", 1e-9, 50)
        ops.algorithm("Newton"); ops.integrator("LoadControl", 1.0); ops.analysis("Static"); ops.analyze(1)
        return ops.nodeDisp(2, 1)
    assert tip("PDelta") > 1.05 * tip("Linear")
    E.build(_rect(), "Linear"); assert E._BUILD_TRANSF == "Linear"
    E.build(_rect(), "PDelta"); assert E._BUILD_TRANSF == "PDelta"


def test_grid_level0_uses_the_custom_base_footprint():
    c = _rect(); c["present"] = {0: {(0, 0), (1, 0)}, 1: {(0, 0), (1, 0), (2, 0)}}
    assert E.grid(c, 0) == {(0, 0), (1, 0)} and E.grid(c, 1) == {(0, 0), (1, 0), (2, 0)}
    c["present"] = {1: {(0, 0), (1, 0)}}
    assert E.grid(c, 0) == {(0, 0), (1, 0)}

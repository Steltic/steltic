"""HR-14/15/16/20/21/33/34/40 regression tests: Table 12.2-1 system / height limits and the SDC
derivation (preflight), the R/Cd/Om0/Ct/x checks, consistency heuristics, the built-model gate,
the dual-system MF share, the stability coefficient theta and the app-side completion gate.
Run from the repo root:  PYTHONPATH=$PWD:$PWD/steel_engine python -m pytest tests -q"""
import copy, hashlib, json, os, pathlib, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "steel_engine")); sys.path.insert(0, ROOT)

import pytest  # noqa: E402
import preflight as P  # noqa: E402
import consistency as C  # noqa: E402


def _cfg(system, heights_ft, SDS=1.0, SD1=0.6, S1=0.5, Ie=1.0, **kw):
    row = P.SYSTEMS.get(P.parse_system(system)["rows"][0]) if P.parse_system(system)["rows"] else None
    s = dict(SDS=SDS, SD1=SD1, S1=S1, Ie=Ie)
    if row:
        s.update(R=row["R"], Cd=row["Cd"], Om0=row["Om0"], Ct=row["Ct_x"][0], x=row["Ct_x"][1])
    c = dict(system=system, heights=[h * 12.0 for h in heights_ft], NX=3, NY=3, SX=360.0, SY=360.0, rho=1.3,
             model={"bases": "fixed", "joints": "rigid", "gravity": "framed"}, seis=s, D_floor=80.0, D_roof=60.0,
             clad=15.0)
    c.update(kw)
    return c


def _err(cfg):
    return [m for s, m in P.check(cfg) if s == "ERROR"]


def _warn(cfg):
    return [m for s, m in P.check(cfg) if s == "WARN"]


# ------------------------------------------------------------------ HR-15: SDC + height limits
def test_sdc_override_lower_than_derived_is_an_error_and_ignored():
    # Ex16: SDS 0.48, SD1 0.20, RC III -> Table 11.6-2 gives D; the brief said C
    c = _cfg("IMF", [12, 12, 12], SDS=0.48, SD1=0.20, S1=0.13, Ie=1.25, sdc="C")
    assert any("cfg['sdc']='C'" in m and "SDC D" in m for m in _err(c))
    assert P.sdc_of_cfg(c) == "D"                                    # the engine never uses the lower SDC
    c["sdc"] = "E"
    assert P.sdc_of_cfg(c) == "E" and any("MORE severe" in m for m in _warn(c))


def test_sdc_11_6_exception_needs_all_four_conditions():
    c = _cfg("SCBF", [10, 10], SDS=0.48, SD1=0.20, S1=0.13, sdc="C")
    assert P.sdc_assessment(c)["required"] == "D"
    c[P.SDC_EXC_KEY] = {"T_drift": 0.30, "Cs_eq_12_8_2": True}      # Ta = 0.02*20^0.75 = 0.189 < 0.8Ts = 0.333
    a = P.sdc_assessment(c)
    assert a["exception_ok"] and a["required"] == "C" and not any("cfg['sdc']" in m for m in _err(c))
    c[P.SDC_EXC_KEY] = {"T_drift": 0.50, "Cs_eq_12_8_2": True}      # T_drift >= Ts = 0.417 -> condition 2 fails
    assert P.sdc_assessment(c)["required"] == "D"


def test_imf_height_limit_applies_in_sdc_d_regardless_of_sds():
    c = _cfg("IMF", [12, 12, 12], SDS=0.48, SD1=0.20, S1=0.13, Ie=1.25)     # Ex16: 36 ft, SDS < 0.50
    assert any("36.0 ft exceeds" in m and "intermediate moment frame" in m for m in _err(c))
    assert not any("exceeds" in m for m in _err(_cfg("IMF", [12, 11], SDS=0.48, SD1=0.20, S1=0.13, Ie=1.25)))


def test_imf_single_story_exception_and_sdc_e_f_not_permitted():
    c = _cfg("IMF", [40], SDS=1.0, SD1=0.6, S1=0.5, D_roof=15.0, clad=10.0)
    assert any("exceeds" in m for m in _err(c))
    c[P.H_EXC_KEY] = {"clause": "12.2.5.7.1(a)", "roof_dead_psf": 15, "wall_dead_psf": 10}
    assert not _err(c) or not any("intermediate" in m for m in _err(c))
    assert any("permitted to h_n=65" in m for m in _warn(c))
    c[P.H_EXC_KEY]["roof_dead_psf"] = 25                                   # roof DL > 20 psf -> no exception
    assert any("intermediate" in m for m in _err(c))
    e = _cfg("IMF", [12, 12], SDS=1.0, SD1=0.6, S1=0.8)                    # S1 >= 0.75 -> SDC E
    assert any("NOT PERMITTED in SDC E" in m for m in _err(e))
    assert any("NOT PERMITTED in SDC F" in m for m in _err(_cfg("STMF", [14] * 4, S1=0.8, Ie=1.5)))


def test_sdc_f_limits_and_every_subsystem_of_a_mixed_declaration():
    assert any("100 ft" in m for m in _err(_cfg("SCBF", [12] * 10, S1=0.8, Ie=1.5)))          # 120 ft SCBF, SDC F
    assert any("100 ft" in m for m in _err(_cfg("BRBF", [12] * 10, S1=0.8, Ie=1.5)))
    assert not any("exceeds" in m for m in _err(_cfg("dual SMF+BRBF", [12] * 10, S1=0.8, Ie=1.5)))
    m = _cfg("mixed SMF (X) + SCBF (Y)", [13] * 13)                                          # 169 ft, SDC D
    assert P.parse_system(m)["kind"] == "mixed"
    assert any("special concentrically" in x and "160 ft" in x for x in _err(m))
    assert any("NOT PERMITTED" in x for x in _err(_cfg("dual IMF+SCBF", [12] * 4, S1=0.8)))   # E1 row, SDC E


def test_12_2_5_4_increase_clears_only_with_numeric_conditions():
    c = _cfg("SCBF", [18, 18] + [12.5] * 10)                                                # Ex20: 161 ft, SDC D
    assert any("161.0 ft exceeds" in m and "12.2.5.4" in m for m in _err(c))
    c[P.H_INC_KEY] = {"TIR_max": 1.31, "max_plane_share_X": 0.55, "max_plane_share_Y": 0.50}
    assert not any("exceeds" in m for m in _err(c)) and any("12.2.5.4" in m for m in _warn(c))
    c[P.H_INC_KEY]["max_plane_share_X"] = 0.65                                             # condition 2 fails
    assert any("> 60%" in m for m in _err(c))
    c[P.H_INC_KEY] = {"TIR_max": 1.45, "max_plane_share_X": 0.5, "max_plane_share_Y": 0.5}  # condition 1 fails
    assert any("> 1.4" in m for m in _err(c))
    assert any("beyond even" in m for m in _err(_cfg("SCBF", [12.5] * 20)))                 # 250 ft > 240 ft
    c[P.H_INC_KEY] = {"TIR_max": 1.3, "max_plane_share_X": 1.99, "max_plane_share_Y": 0.5}   # Ex20 Grid C: 199 %
    assert any("199% > 60%" in m for m in _err(c))
    c[P.H_INC_KEY] = {"TIR_max": 1.3, "max_plane_share_X_pct": 55, "max_plane_share_Y_pct": 50}
    assert not any("exceeds" in m for m in _err(c))


def test_consistency_height_issue_clears_with_package_evidence(tmp_path):
    cfg = _cfg("SCBF", [18, 18] + [12.5] * 10)
    pkg = {"members": [], "connections": [{"id": "c", "limit_state": "x", "cited": "J3", "DC": 0.5}],
           "capacity_design": {"brace_expected_strength": {"Ry": 1.4, "Pt_kip": 300}}}
    assert any("exceeds the Table 12.2-1" in i for i in C._height_limit_issues(cfg, pkg))
    pkg["capacity_design"]["height_limit"] = {"TIR_max": 1.31, "max_plane_share_X": 0.55, "max_plane_share_Y": 0.5,
                                              "TIR_basis": "Eq. 12.3-2 with accidental torsion"}
    assert not C._height_limit_issues(cfg, pkg)
    pkg["capacity_design"]["height_limit"] = {"12.2.5.4_condition_1": "TIR = 1.31 <= 1.4 -> met"}   # prose only
    assert any("exceeds" in i for i in C._height_limit_issues(cfg, pkg))


# ------------------------------------------------------------------ HR-20: R, Cd, Om0, Ct, x
def test_factor_mismatches_against_table_12_2_1_and_12_8_2():
    brbf = _cfg("BRBF", [13] * 3)
    assert not [m for m in _err(brbf) if "Table 12" in m]
    brbf["seis"].update(Cd=5.5, Om0=3.0)                                    # engine R-keyed defaults for R=8
    errs = _err(brbf)
    assert any("Cd=5.5" in m and "conservative" in m for m in errs) and any("Om0=3" in m for m in errs)
    brbf[P.SEIS_REASON_KEY] = "brief prescribes Cd 5.5 and Om0 3.0 for the owner's criteria"
    assert not [m for m in _err(brbf) if "Table 12" in m] and any("kept per" in m for m in _warn(brbf))
    stmf = _cfg("STMF", [14] * 8); stmf["seis"]["Om0"] = 2.5                # R=7 default: unconservative
    assert any("Om0=2.5" in m and "UNCONSERVATIVE" in m for m in _err(stmf))
    spsw = _cfg("SPSW", [13] * 6); spsw["seis"]["Cd"] = 5.5
    assert any("Cd=5.5" in m and "UNCONSERVATIVE" in m for m in _err(spsw))
    b2 = _cfg("BRBF", [13] * 3); b2["seis"].update(Ct=0.02, x=0.75)         # 'all other' Ct for BRBF: lower Ta
    assert any("Ct=0.02" in m and "conservative" in m for m in _err(b2))
    smf = _cfg("SMF", [13] * 3); smf["seis"].update(Ct=0.02, x=0.75)        # permitted MF row alternative
    assert not [m for m in _err(smf) if "Ct=" in m]
    smf["seis"].update(Ct=0.03, x=0.8)                                      # larger Ta -> unconservative
    assert any("Ct=0.03" in m and "UNCONSERVATIVE" in m for m in _err(smf))
    flex = _cfg("SCBF", [13] * 3, diaphragm="flexible"); flex["seis"]["Om0"] = 2.0
    assert not [m for m in _err(flex) if "Om0" in m]                         # 2.0 floor of the 0.5 reduction


def test_mixed_system_envelope_and_per_direction_blocks():
    m = _cfg("mixed SMF X + SCBF Y", [13] * 3)
    m["seis"].update(R=8, Cd=5.5, Om0=3.0)                                   # SMF factors for BOTH directions
    assert any("R=8" in x and "UNCONSERVATIVE" in x for x in _err(m))
    m["seis_Y"] = dict(m["seis"], R=6, Cd=5.0, Om0=2.0, Ct=0.02, x=0.75)
    assert not [x for x in _err(m) if "R=" in x] and any("BOTH directions" in x for x in _warn(m))


# ------------------------------------------------------------------ HR-40: rho
def test_rho_defaults_are_reported():
    c = _cfg("SCBF", [13] * 3, SDS=0.4, SD1=0.15, S1=0.1); c.pop("rho")
    assert P.sdc_of_cfg(c) == "C" and any("12.3.4.1" in m and "rho=1.0" in m for m in _warn(c))
    c["rho"] = 1.15
    assert any("rho = 1.0 or 1.3" in m for m in _err(c))


def test_mixed_system_is_not_moment_frame_only():
    assert P.mf_only_system({"system": "SMF"}) and P.mf_only_system({"system": "IMF both directions"})
    assert not P.mf_only_system({"system": "mixed per direction: SMF in X + SCBF in Y"})
    assert not P.mf_only_system({"system": "dual SMF+BRBF"})
    ex31 = "Steel systems not specifically detailed for seismic resistance (R=3): Y = gable portal moment frames (OMF-type)"
    assert P.parse_system(ex31)["rows"] == ["nsd"]
    ex33 = _cfg("IMF (Y, transverse) + OCBF (X, longitudinal)", [38], SDS=0.42, SD1=0.17, S1=0.10)
    ex33["seis"].update(R=3.25, Cd=3.25, Om0=3.0, Ct=0.02, x=0.75)          # Ex33: Cd of the OCBF used for the IMF too
    assert any("Cd=3.25" in m and "UNCONSERVATIVE" in m for m in _err(ex33))


# ------------------------------------------------------------------ HR-33: consistency heuristics
def test_demand_capacity_matched_by_key_semantics_not_order():
    a = {"limit_state": "bolt shear", "phi": 0.75, "Ru_kip": 50.0, "phiRn_kip": 100.0, "DC": 0.5, "cited": "J3"}
    b = dict(reversed(list(a.items())))
    one = lambda chk: C._entry_issues("connection", {"id": "c", "checks": [chk]})
    assert not one(a) and not one(b)
    bad = dict(a, DC=0.3)
    assert any("!= demand/capacity" in i for i in one(bad)) and one(bad) == one(dict(reversed(list(bad.items()))))
    strut = {"limit_state": "x", "strut_length_in": 120.0, "capacity_kip": 200.0, "DC": 0.4, "cited": "E3"}
    assert not C._entry_issues("member", {"id": "m", "checks": [strut], "cited": "E3"})   # 'strut' is no demand


def test_interaction_and_small_dc_tolerance():
    h1 = {"limit_state": "H1-1a axial + flexure", "Pr_kip": 494.1, "Pc_kip": 1211.5, "DC": 0.712, "cited": "H1"}
    assert not C._entry_issues("member", {"id": "m", "checks": [h1]})
    h1_bad = dict(h1, DC=0.30)                                               # below its own axial term 0.408
    assert any("< its own" in i for i in C._entry_issues("member", {"id": "m", "checks": [h1_bad]}))
    small = {"limit_state": "weld", "Ru": 1.0, "phiRn": 70.0, "DC": 0.01, "cited": "J2"}  # 0.0143 vs 0.01
    assert not C._entry_issues("connection", {"id": "c", "checks": [small]})


def test_numeric_comparisons_are_not_symbolic():
    pkg = {"capacity_design": {"a": "b/t = 7.2 <= 0.3 sqrt(E/Fy) = 7.22 OK", "b": "TIR 1.31 <= 1.4",
                               "c": "Ic >= 0.0031 tw h^4/L"}}
    out = C._named_not_computed_issues(pkg)
    assert len(out) == 1 and "Ic >= 0.0031" in out[0]


def test_system_checks_need_numbers_not_keywords():
    cfg = {"system": "SMF"}
    prose = {"capacity_design": {"notes": "SCWB and panel zone checked per E3.4a / E3.6e"}}
    assert len(C._system_checks_issues(cfg, prose)) == 2
    num = {"capacity_design": {"SCWB": {"ratio": 1.21, "cite": "AISC 341-22 E3.4a"},
                               "panel_zone": {"Ru_kip": 600, "phiRn_kip": 700, "DC": 0.86}}}
    assert not C._system_checks_issues(cfg, num)
    assert any("continuity" in i for i in C._system_checks_issues({"system": "IMF"}, num))   # IMF list, no SCWB
    dual = C._system_checks_issues({"system": "dual SMF+BRBF"}, num)
    assert any("adjusted brace" in i for i in dual) and any("25 %" in i for i in dual)


def test_seeded_text_neither_triggers_nor_clears():
    seeded = {"id": "collector-irregularity-lines", "type": "collector / drag strut (SEEDED - REQUIRED)",
              "design_basis": "SEEDED because the screen found reentrant: collectors on the re-entrant/setback/"
                              "transfer lines are a REQUIRED deliverable.", "DC": None}
    pkg = {"connections": [seeded], "framework_screen": {"note": "FRAMEWORK-COMPUTED irregularity screen"}}
    assert "transfer" not in C._agent_blob(pkg)
    cfg = {"arch": "drawing of a stepped school", "system": "SCBF"}
    assert not any("multi-wing" in i for i in C._consultancy_issues(cfg, {"members": []}))
    assert any("multi-wing" in i for i in C._consultancy_issues({"arch": "two wings"}, {"members": []}))


def test_composite_needs_computed_items():
    cfg = {"floor_system": "one-way composite"}
    words = {"capacity_design": {"note": "studs, camber and the wet stage per Ch. I; scope as noted"}}
    assert any("COMPOSITE" in i for i in C._consultancy_issues(cfg, words))
    nums = {"composite_design": {"studs": "3/4 in studs, Qn = 17.2 kip", "camber": "no camber: 0.55 in < 3/4 in",
                                 "wet_stage": {"Mu_kipft": 360, "DC": 0.48}}}
    assert not any("COMPOSITE" in i for i in C._consultancy_issues(cfg, nums))


def test_r3_note_clears_with_numeric_governing_hazard():
    cfg = {"seis": {"R": 3.0}, "system": "steel not specifically detailed"}
    assert any("R=3.00" in i for i in C._design_basis_issues(cfg, None, {"capacity_design": {"x": "wind governs"}}))
    pkg = {"governing_hazard": {"X": {"wind_kip": 76, "seismic_kip": 140}, "Y": {"wind_kip": 127, "seismic_kip": 140}}}
    assert not any("R=3.00" in i for i in C._design_basis_issues(cfg, None, pkg))


def test_consistency_records_its_verdict_with_hashes(tmp_path):
    jd = tmp_path / "B"; (jd / "design").mkdir(parents=True)
    (jd / "cfg.py").write_text("cfg = dict(system='SMF', heights=[156.0], seis=dict(SDS=0.1, SD1=0.05, R=8, Cd=5.5, Om0=3, Ie=1.0))\n")
    pkg = {"members": [{"id": "m", "limit_state": "F2", "cited": "F2", "DC": 0.5}],
           "connections": [{"id": "c", "limit_state": "J3", "cited": "J3", "DC": 0.5}],
           "capacity_design": {"SCWB": {"ratio": 1.3}, "panel_zone": {"DC": 0.5}}}
    (jd / "design" / "calc_package.json").write_text(json.dumps(pkg))
    issues = C.check("B", root=str(jd), verbose=False)
    rec = json.loads((jd / "design" / C.RESULT_FILE).read_text())
    assert rec["n_issues"] == len(issues)
    assert rec["calc_package_sha256"] == hashlib.sha256((jd / "design" / "calc_package.json").read_bytes()).hexdigest()
    assert rec["cfg_sha256"] == hashlib.sha256((jd / "cfg.py").read_bytes()).hexdigest()


# ------------------------------------------------------------------ HR-14: completion gate
class _WS:
    def __init__(self, jd): self.jd = jd
    def _job_dir(self): return self.jd


def _job(tmp_path, checks=None, extra=None, issues=(), pkg_extra=None):
    jd = tmp_path / "J"; (jd / "design").mkdir(parents=True, exist_ok=True)
    (jd / "cfg.py").write_text("cfg = {}\n")
    pkg = {"members": [{"id": "m", "DC": 0.8, "cited": "AISC 360-22 F2"}],
           "connections": [{"id": "c", "DC": 0.7, "cited": "AISC 360-22 J3"}]}
    pkg.update(pkg_extra or {})
    cp = jd / "design" / "calc_package.json"; cp.write_text(json.dumps(pkg))
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    ch = {"drift_X": True, "drift_Y": True, "model_declared": True, "model_consistent": True,
          "model_complete": True, "beam_deflection": True, "stability_theta": True}
    ch.update(checks or {})
    ex = {"theta": {"max": 0.05, "theta_max": 0.1}}; ex.update(extra or {})
    (jd / "design" / "pipeline_results.json").write_text(json.dumps(
        {"cfg_sha256": sha(jd / "cfg.py"), "checks": ch, "extra": ex}))
    (jd / "design" / "consistency_result.json").write_text(json.dumps(
        {"issues": list(issues), "calc_package_sha256": sha(cp), "cfg_sha256": sha(jd / "cfg.py")}))
    return jd


def _gate(jd):
    from steltic.agent import _completion_gate
    return _completion_gate(_WS(jd))


def test_gate_clean_and_missing_files(tmp_path):
    jd = _job(tmp_path)
    assert _gate(jd) == []
    (jd / "design" / "consistency_result.json").unlink()
    assert any("consistency.check(name) has not been run" in p for p in _gate(jd))
    (jd / "design" / "pipeline_results.json").unlink()
    assert any("pipeline_results.json is missing" in p for p in _gate(jd))


def test_gate_fails_sanity_and_waivers(tmp_path):
    p = _gate(_job(tmp_path, checks={"drift_Y": False}))
    assert any("drift_Y FAILED" in x and "not waivable" in x for x in p)
    jd = _job(tmp_path, checks={"beam_deflection": False})
    assert any("beam_deflection FAILED" in x for x in _gate(jd))
    jd = _job(tmp_path, checks={"beam_deflection": False},
              pkg_extra={"gate_waivers": {"beam_deflection": "long-span roof trusses by joist supplier (SJI)"}})
    assert _gate(jd) == []
    jd = _job(tmp_path, checks={"beam_deflection": False}, pkg_extra={"gate_waivers": {"beam_deflection": "n/a"}})
    assert any("beam_deflection FAILED" in x for x in _gate(jd))                    # no real justification
    p = _gate(_job(tmp_path, extra={"theta": {"max": 0.13, "theta_max": 0.1}}))
    assert any("theta = 0.130 > theta_max" in x for x in p)
    p = _gate(_job(tmp_path, extra={"theta": {"error": "x"}}))
    assert any("NOT evaluated" in x for x in p)


def test_gate_dual_cleared_by_numeric_record(tmp_path):
    assert any("dual_25pct" in x for x in _gate(_job(tmp_path, checks={"dual_25pct": False})))
    rec = {"capacity_design": {"dual_25pct": {"mf_only_0.25V": [{"member": "SMF-col", "DC": 0.57}]}}}
    assert _gate(_job(tmp_path, checks={"dual_25pct": False}, pkg_extra=rec)) == []


def test_gate_stale_and_consistency_issues(tmp_path):
    jd = _job(tmp_path)
    (jd / "cfg.py").write_text("cfg = {'changed': 1}\n")
    p = _gate(jd)
    assert any("STALE" in x for x in p) and any("OLDER calc_package.json / cfg.py" in x for x in p)
    iss = "[capacity_design] 'x' is a symbolic REQUIREMENT, not a COMPUTED check -- substitute"
    assert any(x.startswith("consistency: [capacity_design]") for x in _gate(_job(tmp_path, issues=[iss])))
    w = {"gate_waivers": {"consistency: [capacity_design] 'x'": "a stated supplier criterion, not a check"}}
    assert _gate(_job(tmp_path, issues=[iss], pkg_extra=w)) == []
    hl = "h_n=161.0 ft exceeds the Table 12.2-1 limit of 160 ft for steel SCBF in SDC D"
    w2 = {"gate_waivers": {"consistency: h_n=161.0": "close enough to 160 ft for the owner"}}
    assert any("not waivable" in x for x in _gate(_job(tmp_path, issues=[hl], pkg_extra=w2)))


def test_gate_string_dc_and_waiver_rules(tmp_path):
    jd = _job(tmp_path, pkg_extra={"members": [{"id": "m", "checks": [{"DC": "1.07 (NG)", "cited": "F2"}], "DC": 0.5,
                                                 "cited": "F2"}]})
    assert any("D/C = 1.070" in x for x in _gate(jd))
    jd = _job(tmp_path, pkg_extra={"members": [{"id": "m", "waived": "n/a"}]})
    assert any("without a real justification" in x for x in _gate(jd))
    jd = _job(tmp_path, pkg_extra={"members": [{"id": "m", "waived": "existing member outside the retrofit scope"}]})
    assert any("must cite" in x for x in _gate(jd))


# ------------------------------------------------------------------ HR-14/16/21: engine
E = pytest.importorskip("engine3d")
ops = E.ops


def _frame(base_fix=(1, 1, 1, 1, 1, 1), rel=None, brace=False):
    """2-story, 1x1-bay frame custom_build (300 in bays, 144 in stories)."""
    def cb(cfg, transf="PDelta"):
        ops.wipe(); ops.model("basic", "-ndm", 3, "-ndf", 6)
        z = E.zlevels(cfg); NF = len(cfg["heights"]); pts = {(i, j) for i in (0, 1) for j in (0, 1)}
        for k in range(NF + 1):
            for (i, j) in pts:
                ops.node(E.ntag(i, j, k), i * 300.0, j * 300.0, z[k])
        for (i, j) in pts:
            ops.fix(E.ntag(i, j, 0), *base_fix)
        cm = {}; ele = []; t = 1
        for k in range(1, NF + 1):
            cm[k] = (150.0, 150.0); ops.node(E.mtag(k), 150.0, 150.0, z[k]); ops.fix(E.mtag(k), 0, 0, 1, 1, 1, 0)
        for (i, j) in sorted(pts):
            for k in range(NF):
                E.add_column(t, E.ntag(i, j, k), E.ntag(i, j, k + 1), "W14X90", "X" if j == 0 else "Y")
                ele.append((t, "col", "W14X90", E.ntag(i, j, k), E.ntag(i, j, k + 1))); t += 1
        for k in range(1, NF + 1):
            for a, b in (((0, 0), (1, 0)), ((0, 1), (1, 1)), ((0, 0), (0, 1)), ((1, 0), (1, 1))):
                E.add_beam(t, E.ntag(*a, k), E.ntag(*b, k), "W24X76", releases=rel)
                ele.append((t, "beam", "W24X76", E.ntag(*a, k), E.ntag(*b, k))); t += 1
            if brace:
                ops.uniaxialMaterial("Elastic", 7, E.E) if k == 1 else None
                for (a, b) in (((0, 0), (1, 0)), ((0, 0), (0, 1))):
                    ops.element("Truss", t, E.ntag(*a, k - 1), E.ntag(*b, k), 4.0, 7)
                    ele.append((t, "brace", "HSS6X6X1/4", E.ntag(*a, k - 1), E.ntag(*b, k))); t += 1
            ops.rigidDiaphragm(3, E.mtag(k), *[E.ntag(i, j, k) for (i, j) in pts])
            m = E.floor_w(cfg, k) / E.g
            ops.mass(E.mtag(k), m, m, 0, 0, 0, m * 2 * 300.0 ** 2 / 12)
        return {"cm": cm, "present": {k: set(pts) for k in range(NF + 1)}, "z": z, "NF": NF, "ele": ele}
    return cb


def _ecfg(**kw):
    c = dict(arch="test", NX=1, NY=1, SX=300.0, SY=300.0, heights=[144.0, 144.0], base="fixed",
             seis=E.seis(1.0, 0.6, 0.5, 8, 0.028, 0.8, 1.4, Cd=5.5, Om0=3.0), D_floor=80.0, D_roof=60.0,
             clad=15.0, L_floor=50.0, system="SMF")
    c.update(kw)
    return c


def test_model_gate_reads_the_built_model():
    # the reproduced HR-21 case: declared pinned / rigid / leaning on an all-fixed custom_build
    c = _ecfg(custom_build=_frame(), model={"bases": "pinned", "joints": "rigid", "gravity": "leaning"})
    dok, cok, msg = E._model_gate(c)
    assert dok and not cok and "bases='pinned'" in msg and "gravity='leaning'" in msg
    c = _ecfg(custom_build=_frame(), model={"bases": "fixed", "joints": "rigid", "gravity": "framed"})
    assert E._model_gate(c)[1]
    c = _ecfg(custom_build=_frame(base_fix=(1, 1, 1, 0, 0, 0), rel=("both", "none")),
              model={"bases": "fixed", "joints": "rigid", "gravity": "framed"})
    dok, cok, msg = E._model_gate(c)
    assert not cok and "built column bases are pinned" in msg and "joints='rigid'" in msg
    c["model"] = {"bases": "pinned", "joints": "pinned", "gravity": "leaning"}
    assert E._model_gate(c)[1]
    c["model"] = {"bases": "mixed", "joints": "mixed", "gravity": "leaning"}
    msg = E._model_gate(c)[2]
    assert "bases='mixed'" in msg and "joints='mixed'" in msg


def test_theta_formula(monkeypatch):
    c = _ecfg(custom_build=_frame())
    Fx = {1: 10.0, 2: 20.0}
    monkeypatch.setattr(E, "_edge_story_drifts", lambda cfg, F, d: (None, [0.004, 0.002]))
    th = E.stability_theta(c, Fx, Fx)
    P1 = sum(E.floor_dead(c, k) + 0.5 * 0.4 * E.floor_live(c, k) for k in (1, 2))
    raw = P1 * 0.004 / 30.0
    assert th["X"][0] == pytest.approx(round(raw / (1 + raw), 4), abs=1e-4)
    assert th["theta_max"] == pytest.approx(max(min(0.5 / 5.5, 0.25), 0.10))        # 0.0909 -> 0.10 floor
    monkeypatch.setattr(E, "_edge_story_drifts", lambda cfg, F, d: (None, [0.2, 0.002]))
    assert not E.stability_theta(c, Fx, Fx)["ok"]


def test_dual_share_from_the_analysed_custom_build():
    c = _ecfg(custom_build=_frame(brace=True), system="dual SMF+SCBF",
              seis=E.seis(1.0, 0.6, 0.5, 7, 0.02, 0.75, 1.4, Cd=5.5, Om0=2.5))
    Fx = {1: 20.0, 2: 40.0}
    d = E.dual_mf_share(c, Fx, Fx)
    assert d["n_brace_elements"] == 4 and 0.0 < d["min_share"] < 1.0
    info = E.build(c, "PDelta"); bt = {e[0] for e in info["ele"] if e[1] == "brace"}
    E.static_lateral(c, Fx, "X"); tot, br = E._story_split(info, c, 0, bt)
    assert abs(abs(tot[0]) - 60.0) < 0.5 and abs(abs(tot[1]) - 40.0) < 0.5         # story shears recovered
    assert d["mf_only"]["X"]["converged"] and d["mf_only"]["forces_0.25E"]["by_element"]
    c2 = _ecfg(custom_build=_frame(brace=False), system="dual SMF+SCBF")
    assert E.dual_mf_share(c2, Fx, Fx)["min_share"] is None                         # no braces -> not computable

"""Integration tests for the review-fixes merge (hr-seismic x hr-report x hr-gates x hr-geomwind x
hr-demands): one drift (incl. the AISC 358 RBS factor) for the gate, theta and the report; one theta
formula; cache keys that see the floor-plate / seismic-weight keys; static dead == seismic-weight dead
with the mid-height cladding; per-direction factors inside the hr-demands combinations."""
import copy, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "steel_engine")); sys.path.insert(0, ROOT)

import pytest  # noqa: E402
import engine3d as E  # noqa: E402
import design_pipeline as DP  # noqa: E402
import static_model as SM  # noqa: E402

MODEL = dict(bases="fixed", joints="rigid", gravity="framed")


def _cfg(**kw):
    c = dict(arch="test frame", NX=3, NY=2, SX=360.0, SY=360.0, heights=[156.0]*3, base="fixed",
             col="W14X120", beam="W24X55", seis=E.seis(1.0, 0.6, 0.6, 8, 0.028, 0.8, 1.4),
             system="SMF", sdc="D", rho=1.3, analyses=["ELF"], model=dict(MODEL), **E.D)
    c.update(kw)
    E.clear_caches()
    return c


def test_rbs_factor_is_in_the_one_drift_gate_theta_and_report_share():
    c0 = _cfg(); sd0 = E.seismic_drift(c0)
    c1 = _cfg(rbs_drift_factor=1.1); sd1 = E.seismic_drift(c1)
    for d in ("X", "Y"):
        assert sd1[d]["rbs"][0] == pytest.approx(1.1) and sd0[d]["rbs"][0] == 1.0
        assert sd1[d]["max"] == pytest.approx(1.1*sd0[d]["max"])
        assert sd1[d]["elastic"] == [pytest.approx(1.1*v) for v in sd0[d]["elastic"]]
    # theta: the gate rows use the RBS-amplified elastic drift
    t0 = E.stability_theta(c0); t1 = E.stability_theta(c1)
    raw0 = E.theta_rows(c0, "X")[0][0]["raw"]; raw1 = E.theta_rows(c1, "X")[0][0]["raw"]
    assert raw1 == pytest.approx(1.1*raw0)
    assert t1["X"][0] > t0["X"][0]
    # the report's Ch. 7 theta rows == the gate's rows (one formula, one drift)
    import report as R
    runs = R._lateral_runs(c1, None)
    for d in ("X", "Y"):
        assert runs[d]["design_max"] == pytest.approx(sd1[d]["max"])        # report drift == gate drift
        if True:
            rr, tm, b = R._theta_rows(c1, runs, d)
            er, etm, eb = E.theta_rows(c1, d)
            assert tm == pytest.approx(etm) and b == pytest.approx(eb)
            assert [x["theta"] for x in rr] == [pytest.approx(x["theta"]) for x in er]
            assert max(x["theta"] for x in rr) == pytest.approx(max(v for v in t1[d] if v is not None), abs=1e-4)


def test_theta_gate_reports_the_governing_direction_limit():
    c = _cfg(seis_Y=dict(R=6, Cd=5.0, Om0=2.0, Ct=0.02, x=0.75, system="SCBF"))
    th = E.stability_theta(c)
    assert th["theta_max_X"] == pytest.approx(max(min(0.5/E.seis_dir(c, "X")["Cd"], 0.25), 0.10), abs=1e-4)
    assert th["theta_max_Y"] == pytest.approx(max(min(0.5/5.0, 0.25), 0.10), abs=1e-4)
    gd = max(("X", "Y"), key=lambda d: max(v for v in th[d] if v is not None)/th["theta_max_" + d])
    assert th["theta_max"] == th["theta_max_" + gd]
    assert th["ok"] == all(max(v for v in th[d] if v is not None) <= th["theta_max_" + d] for d in ("X", "Y"))


def test_model_and_demand_cache_keys_see_floor_plate_and_seismic_weight_keys():
    base = _cfg()
    k0 = E._model_key(base)
    for kw in (dict(openings={2: [(1, 0)]}), dict(seismic_mass_only={1: 10.0}), dict(seismic_weight_kip={2: 50.0}),
               dict(omitted_columns={1: [(1, 1)]}), dict(wind=dict(V=110, exposure="C", parapet_ft=4.0))):
        c = copy.deepcopy(base); c.update(kw)
        assert E._model_key(c) != k0, kw
    # callables hash as "<fn>" in the demand key: the RESOLVED plate is part of it
    c1 = copy.deepcopy(base); c1["openings"] = lambda k, NX, NY: []
    c2 = copy.deepcopy(base); c2["openings"] = lambda k, NX, NY: [(1, 0)]
    m = SM.build_static(c1, "Linear", 1)
    assert SM._full_key(c1, m, [], 1, "one-way", None) != SM._full_key(c2, m, [], 1, "one-way", None)


def test_static_dead_equals_seismic_dead_with_mid_height_cladding_on_a_setback():
    def plan(k, NX, NY):
        if k <= 2:
            return {(i, j) for i in range(NX + 1) for j in range(NY + 1)}
        return {(i, j) for i in range(NX + 1) for j in range(2)}
    c = _cfg(heights=[168.0, 144.0, 144.0], plan=plan, clad=20.0)
    c["wind"] = dict(V=110, exposure="C", parapet_ft=3.0)
    for k, (st, w) in SM.gravity_audit(c).items():
        assert st == pytest.approx(w, rel=1e-6), (k, st, w)
    # the diaphragm cladding mass is exactly the cladding in W
    tot = sum(E._clad_kip(c, k) for k in range(1, 4))
    assert tot == pytest.approx(20.0*sum(E.clad_areas_ft2(c).values())/1000.0)


def test_combinations_use_per_direction_factors_and_flip_the_companion_consistently():
    c = _cfg(seis_Y=dict(R=6, Cd=5.0, Om0=2.0, Ct=0.02, x=0.75, system="SCBF"), rho_X=1.0, rho_Y=1.3)
    SF = E.seismic_design_forces(c)
    cases = DP.combos(c)
    # principal X with the orthogonal 30 % of Y in both signs; mz recomputed for the flipped companion
    lab_p = [x for x in cases if x[0].startswith("(1.2+0.2SDS)D+rhoEX+t+/0.3Y+")][0]
    lab_m = [x for x in cases if x[0].startswith("(1.2+0.2SDS)D+rhoEX+t+/0.3Y-")][0]
    assert lab_p[4] == E.lateral_pattern(c, "X", 1, 1, "rho", companion=0.3)
    assert lab_m[4] == E.lateral_pattern(c, "X", 1, 1, "rho", companion=-0.3)
    for k in range(1, 4):
        assert lab_p[4][k][0] == pytest.approx(SF["X"]["rho"]*SF["X"]["Fx"][k])
        assert lab_m[4][k][1] == pytest.approx(-0.3*SF["Y"]["rho"]*SF["Y"]["Fx"][k])
    # Omega0 column cases: each direction's own Omega0
    om = [x for x in cases if "Om0*EY+" in x[0] and x[0].startswith("(1.2")][0]
    assert om[4][1][1] == pytest.approx(SF["Y"]["Om0"]*SF["Y"]["Fx"][1])
    assert SF["Y"]["Om0"] == 2.0
    # collector statics use each direction's ELF forces and Omega0
    col = DP.collector_extras(c)
    assert col["Y"][1][1] == pytest.approx(2.0) and col["X"][1][1] == pytest.approx(SF["X"]["Om0"])


def test_consistency_requires_the_rbs_declaration_the_package_designs():
    import consistency as C
    pkg = {"connections": [{"id": "conn-beam-W24X55", "type": "RBS moment connection (AISC 358 Ch. 5)"}]}
    c = _cfg()
    assert any("RBS" in x for x in C._rbs_declaration_issues(c, pkg))
    c["rbs"] = {"c_over_bf": 0.25}
    assert C._rbs_declaration_issues(c, pkg) == []
    assert C._rbs_declaration_issues(_cfg(), {"connections": [{"id": "x", "type": "WUF-W"}]}) == []

"""hr-seismic regression tests (HR-01, 02, 08, 09, 19, 20, 23, 31): ELF per direction, per-direction
seismic factors, Table 12.2-1 defaults, torsion (TIR/Ax/edge drift), diaphragm mass at the centre of mass
and the scaled MRSA.  Small 3-storey models; run with `python -m pytest tests -q` from the repo root."""
import copy, math, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "steel_engine")); sys.path.insert(0, ROOT)

import pytest  # noqa: E402
import openseespy.opensees as ops  # noqa: E402
import engine3d as E  # noqa: E402
import design_pipeline as DP  # noqa: E402

MODEL = dict(bases="fixed", joints="rigid", gravity="framed")


def _cfg(**kw):
    c = dict(arch="test frame", NX=3, NY=1, SX=360.0, SY=360.0, heights=[156.0]*3, base="fixed",
             col="W14X120", beam="W24X55", seis=E.seis(1.0, 0.6, 0.6, 8, 0.028, 0.8, 1.4),
             system="SMF", sdc="D", rho=1.3, analyses=["ELF"], model=dict(MODEL), **E.D)
    c.update(kw)
    E.clear_caches()
    return c


def _hand_elf(c, T, s):
    """ASCE 7-22 12.8.1-12.8.3 by hand for period T and factor set s."""
    NF = len(c["heights"]); z = E.zlevels(c); hn = z[-1]/12.0
    Ta = s["Ct"]*hn**s["x"]; Tu = min(T, s["Cu"]*Ta); RI = s["R"]/s["Ie"]
    Cs = min(s["SDS"]/RI, s["SD1"]/(Tu*RI))
    Cs = max(Cs, 0.044*s["SDS"]*s["Ie"], 0.01, (0.5*s["S1"]/RI) if s["S1"] >= 0.6 else 0.0)
    W = sum(E.floor_w(c, k) for k in range(1, NF+1))
    return Cs, Cs*W, Tu, Ta


# ------------------------------------------------------------------ HR-01: period per direction
def test_elf_uses_dominant_mode_period_of_each_direction():
    c = _cfg(NX=4, NY=1, seis=E.seis(1.0, 0.6, 0.6, 8, 0.028, 0.8, 2.5))
    NF = 3
    T, w2, eX, eY, Mt = E.modal(c, E._nm_default(NF))
    iX = max(range(len(T)), key=lambda n: eX[n]); iY = max(range(len(T)), key=lambda n: eY[n])
    assert iX != iY
    assert E.period_dir(c, "X")[0] == T[iX] and E.period_dir(c, "Y")[0] == T[iY]
    for d, i in (("X", iX), ("Y", iY)):
        Cs, V, Tu, Ta, kk, Fx, W = E.elf_dir(c, d)
        Csh, Vh, Tuh, Tah = _hand_elf(c, T[i], E.seis_dir(c, d))
        assert Tu == pytest.approx(Tuh) and Cs == pytest.approx(Csh) and V == pytest.approx(Vh)
        assert sum(Fx.values()) == pytest.approx(V)
    # the two directions really differ here (the old engine used T[0] for both)
    assert E.elf_dir(c, "X")[1] != pytest.approx(E.elf_dir(c, "Y")[1], rel=1e-3)
    # the run gates report each direction's own period and base shear
    E.CFG["_t_hr01"] = c
    r = E.run_one("_t_hr01")
    assert r["extra"]["TX"] == pytest.approx(T[iX]) and r["extra"]["TY"] == pytest.approx(T[iY])
    assert r["Vx"] == pytest.approx(E.elf_dir(c, "X")[1]) and r["Vy"] == pytest.approx(E.elf_dir(c, "Y")[1])
    assert r["chk"]["equil_X"] and r["chk"]["equil_Y"] and r["chk"]["baseshear_Y"]


# ------------------------------------------------------------------ HR-02: per-direction factors
def test_per_direction_factors_drive_elf_drift_and_combinations():
    c = _cfg(NX=3, NY=2, system_X="SMF",
             seis_Y=dict(R=6, Cd=5.0, Om0=2.0, Ct=0.02, x=0.75, system="SCBF"), rho_X=1.0, rho_Y=1.3)
    sx, sy = E.seis_dir(c, "X"), E.seis_dir(c, "Y")
    assert (sx["R"], sx["Cd"], sx["Om0"], sx["rho"]) == (8, 5.5, 3.0, 1.0)
    assert (sy["R"], sy["Cd"], sy["Om0"], sy["rho"], sy["Ct"]) == (6, 5.0, 2.0, 1.3, 0.02)
    VX = E.elf_dir(c, "X")[1]; VY = E.elf_dir(c, "Y")[1]
    TY = E.period_dir(c, "Y")[0]
    assert VY == pytest.approx(_hand_elf(c, TY, sy)[1])
    cases = DP.combos(c)
    ex = [x for x in cases if "rhoEX+t+" in x[0] and x[0].startswith("(1.2")][0][4]
    ey = [x for x in cases if "rhoEY+t+" in x[0] and x[0].startswith("(1.2")][0][4]
    assert sum(v[0] for v in ex.values()) == pytest.approx(1.0*VX)          # principal X, rho_X
    assert sum(v[1] for v in ex.values()) == pytest.approx(0.3*1.3*VY)      # companion = 30 % of the Y system
    assert sum(v[1] for v in ey.values()) == pytest.approx(1.3*VY)
    assert sum(v[0] for v in ey.values()) == pytest.approx(0.3*1.0*VX)
    om = E.lateral_pattern(c, "Y", fac="Om0")
    assert sum(v[1] for v in om.values()) == pytest.approx(2.0*VY) and sum(v[0] for v in om.values()) == pytest.approx(0.3*3.0*VX)
    # drift: each direction its own Cd (Eq. 12.8-16)
    E.CFG["_t_hr02"] = c
    r = E.run_one("_t_hr02")
    ts = E.torsion_summary(c)
    assert r["extra"]["CdY"] == 5.0 and r["extra"]["CdX"] == 5.5
    assert r["mdy"] == pytest.approx(5.0/1.0*max(ts["Y"]["drift_elastic"]))
    # 12.12.1.1: only the moment-frame direction divides the limit by rho
    assert r["extra"].get("drift_limit_rho_X") == pytest.approx(0.02/1.0) and "drift_limit_rho_Y" not in r["extra"]


def test_single_factor_set_is_unchanged_in_both_directions():
    c = _cfg()
    sx, sy, sb = E.seis_dir(c, "X"), E.seis_dir(c, "Y"), E.seis_dir(c)
    assert sx == sy == sb
    for k in ("R", "Cd", "Om0", "Ct", "x", "Cu", "Ie", "SDS", "SD1"):
        assert sx[k] == c["seis"][k]
    assert sx["rho"] == 1.3


def test_override_that_changes_R_never_inherits_the_other_systems_factors():
    c = _cfg(seis_Y=dict(R=6, system="SCBF"))
    sy = E.seis_dir(c, "Y")
    assert (sy["Cd"], sy["Om0"], sy["Ct"], sy["x"]) == (5.0, 2.0, 0.02, 0.75)
    c = _cfg(seis_Y=dict(R=6))                      # no system for Y: legacy default, LOUDLY
    w = " ".join(E.seis_warnings(c))
    assert "R-keyed DEFAULT" in w and "Ct" in w


# ------------------------------------------------------------------ HR-20: Table 12.2-1 defaults
def test_defaults_follow_the_declared_system():
    assert E.seis(1, .6, .6, 7, .028, .8, 1.4, system="STMF")["Om0"] == 3.0          # was 2.5 by R
    c = _cfg(system="SPSW", seis=E.seis(1.0, 0.6, 0.6, 7, 0.02, 0.75, 1.4))
    s = E.seis_dir(c); assert (s["Cd"], s["Om0"]) == (6.0, 2.0) and not E.seis_warnings(c)
    c = _cfg(system="Dual SMF + SPSW", seis=E.seis(1.0, 0.6, 0.6, 8, 0.02, 0.75, 1.4))
    s = E.seis_dir(c); assert (s["Cd"], s["Om0"]) == (6.5, 2.5)
    c = _cfg(system="STMF", seis=E.seis(1.0, 0.6, 0.6, 7, 0.028, 0.8, 1.4))
    assert E.seis_dir(c)["Om0"] == 3.0
    c = _cfg(system="IMF", seis=E.seis(1.0, 0.6, 0.6, 4.5, 0.028, 0.8, 1.4, Cd=3.5, Om0=3.0))
    assert any("Cd=3.5 differs" in w and "UNCONSERVATIVE" in w for w in E.seis_warnings(c))
    c = _cfg(system=None, seis=E.seis(1.0, 0.6, 0.6, 8, 0.028, 0.8, 1.4))
    assert any("R-keyed DEFAULT" in w for w in E.seis_warnings(c))
    assert E.sfrs_key("Dual_SMF_BRBF") == "DUAL_SMF_BRBF" and E.sfrs_key("OCBF") == "OCBF"
    assert E.sfrs_key("steel special plate shear walls") == "SPSW"
    assert E.sfrs_key("mixed: SMF in X + SCBF in Y") is None


# ------------------------------------------------------------------ HR-19 / HR-31: diaphragm mass
def test_rectangle_polar_inertia_uses_the_actual_extent():
    c = _cfg(NX=3, NY=2, clad=0.0)
    p = E.diaphragm_props(c)[1]
    assert (p["xc"], p["yc"]) == (pytest.approx(540.0), pytest.approx(360.0))
    assert p["J"] == pytest.approx(p["m"]*(1080.0**2 + 720.0**2)/12.0, rel=1e-9)   # not (extent + 1 bay)
    assert p["m"] == pytest.approx(E.floor_w(c, 1)/E.g)
    assert (p["BX"], p["BY"]) == (720.0, 1080.0)


def test_lplan_mass_at_centre_of_mass_not_node_average():
    c = _cfg(NX=2, NY=2, plan=E.Lplan, clad=0.0)
    p = E.diaphragm_props(c)[1]
    assert (p["xc"], p["yc"]) == (pytest.approx(300.0), pytest.approx(300.0))          # 3-panel centroid
    assert p["J"] == pytest.approx(p["m"]*79200.0, rel=1e-9)                            # parallel-axis rule
    assert (p["xm"], p["ym"]) == (pytest.approx(315.0), pytest.approx(315.0))           # node average (8 nodes)
    E.build(c, "Linear")
    assert ops.nodeCoord(E.cmtag(1))[:2] == [pytest.approx(300.0), pytest.approx(300.0)]
    assert ops.nodeMass(E.cmtag(1))[0] == pytest.approx(p["m"]) and ops.nodeMass(E.mtag(1))[0] == 0.0
    # an ELF force at the CM = force + offset torque at the master
    lat = E.lateral_pattern(c, "X", 1, 0, 1.0, companion=0.0)
    F = E.elf_dir(c, "X")[5]
    assert lat[1][2] == pytest.approx(-(300.0 - 315.0)*F[1])


def _wrong_mass_build(cfg, transf="PDelta"):
    from example_build import example_build
    info = example_build(cfg, transf); info.pop("diaphragm", None)
    for k in range(1, info["NF"]+1):
        m = E.floor_w(cfg, k)/E.g
        ops.mass(E.mtag(k), m, m, 0, 0, 0, 2.0*m*(1080.0**2 + 720.0**2)/12.0)        # J doubled on purpose
    return info


def test_custom_build_mass_is_respected_or_replaced_loudly():
    c = _cfg(NX=3, NY=2, clad=0.0, custom_build=_wrong_mass_build)
    T, w2, eX, eY, _ = E.modal(c, 9)
    det = E.modal_detail(c, 9)
    assert any("replaced by the consistent seismic mass" in w for w in det["warnings"])
    it = min(range(3), key=lambda n: eX[n] + eY[n])                                       # torsional mode
    c2 = dict(c, diaphragm_mass="builder"); E.clear_caches()
    T2, w22, eX2, eY2, _ = E.modal(c2, 9)
    it2 = min(range(3), key=lambda n: eX2[n] + eY2[n])
    assert T2[it2]/T[it] == pytest.approx(math.sqrt(2.0), rel=0.03)                       # builder J kept


# ------------------------------------------------------------------ HR-08 / HR-09: torsion
def _tors_cfg(**kw):
    return _cfg(NX=3, NY=3, brace="H10", braces=E.tors3, system="SCBF", seis=E.seis(0.5, 0.25, 0.25, 6, 0.02, 0.75, 1.5),
                col="W14X193", beam="W24X76", **kw)


def test_torsion_tir_ax_applied_and_consistent():
    c = _tors_cfg()
    ts = E.torsion_summary(c)
    assert ts["type1"] and ts["amplify"] and ts["TIR"] > 1.2
    for d in ("X", "Y"):
        q = ts[d]
        for k in range(1, 4):
            r = [run[5]["edge_disp"][k] for run in q["acc"]]
            raw = max((max(abs(a), abs(b))/(1.2*0.5*(abs(a)+abs(b))))**2 for a, b in r)
            assert q["Ax"][k] == pytest.approx(min(max(raw, 1.0), 3.0), abs=1e-3)
    d = "Y" if ts["Y"]["TIR"] >= ts["X"]["TIR"] else "X"
    assert max(ts[d]["Ax"].values()) > 1.0
    # Ax multiplies the accidental moment in the design load cases (12.8.4.3)
    SF = E.seismic_design_forces(c); P = SF[d]
    lp = E.lateral_pattern(c, d, 1, 1, "rho"); l0 = E.lateral_pattern(c, d, 1, 0, "rho")
    for k in range(1, 4):
        assert lp[k][2] - l0[k][2] == pytest.approx(P["rho"]*0.05*P["B"][k]*P["Ax"][k]*P["Fx"][k])
    cases = DP.combos(c)
    lab = [x for x in cases if ("rhoE%s+t+" % d) in x[0] and x[0].startswith("(1.2")][0]
    assert lab[4][1][2] == pytest.approx(lp[1][2])
    # ONE TIR / Ax value everywhere: engine gate == torsion_summary
    E.CFG["_t_hr08"] = c
    r = E.run_one("_t_hr08")
    assert r["extra"]["torsion_acc"] == ts["TIR"] and r["extra"]["torsion_Ax"] == ts["Ax_max"]


def test_type1_drift_is_taken_at_the_edges():
    c = _tors_cfg()
    ts = E.torsion_summary(c)
    E.CFG["_t_hr09"] = c
    r = E.run_one("_t_hr09")
    for d, md in (("X", r["mdx"]), ("Y", r["mdy"])):
        s = E.seis_dir(c, d)
        assert ts[d]["drift_location"] == "edges"
        assert md == pytest.approx(s["Cd"]/s["Ie"]*max(ts[d]["edge_drift"]))
        assert max(ts[d]["edge_drift"]) >= max(abs(v) for v in ts[d]["cm_drift"]) - 1e-12
        assert r["edge_drift"][d] == [pytest.approx(s["Cd"]/s["Ie"]*v, abs=1e-5) for v in ts[d]["edge_drift"]]
    # SDC B: no Ax, drift at the centre of mass. (hr-gates HR-15: a declared SDC lower than the one
    # derived from SDS/SD1 is overridden, so the SDC-B case needs SDC-B spectral values.)
    c = _tors_cfg(sdc="B")
    c["seis"] = E.seis(0.25, 0.10, 0.10, 6, 0.02, 0.75, 1.5)
    E.clear_caches()
    assert E.sdc_of(c) == "B"
    ts = E.torsion_summary(c)
    assert not ts["amplify"] and ts["Ax_max"] == 1.0 and ts["X"]["drift_location"] == "centre of mass"


# ------------------------------------------------------------------ HR-23: MRSA
def test_mrsa_scaling_and_meaningful_gate():
    c = _cfg(NX=3, NY=2, analyses=["ELF", "RS"])
    NF = 3; T, w2, eX, eY, Mt = E.modal(c, E._nm_default(NF))
    for d in ("X", "Y"):
        rs = E.mrsa(c, d); V = E.elf_dir(c, d)[1]
        assert rs["Vt"] == pytest.approx(E.rs_baseshear(c, T, eX, eY, Mt, d), rel=1e-6)   # two independent paths
        assert rs["force_scale"] == pytest.approx(max(1.0, V/rs["Vt"]))
        assert sum(rs["F"].values()) == pytest.approx(max(V, rs["Vt"]))                 # 12.9.1.4.1
        SF = E.seismic_design_forces(c)
        assert SF["basis"] == "envelope"
        Ve = [sum(SF[d]["Fx"][i] for i in range(k, NF+1)) for k in range(1, NF+1)]
        Vel = [sum(SF[d]["F_elf"][i] for i in range(k, NF+1)) for k in range(1, NF+1)]
        for k in range(NF):
            assert Ve[k] >= Vel[k] - 1e-9 and Ve[k] >= rs["shear"][k] - 1e-9
    E.CFG["_t_hr23"] = c
    r = E.run_one("_t_hr23")
    assert r["chk"]["rs_X"] and r["chk"]["rs_Y"]
    # the gate checks the forces actually handed to the demands -> it CAN fail now
    SF = E.seismic_design_forces(c)
    SF["X"]["Fx"] = {k: 0.9*v for k, v in SF["X"]["Fx"].items()}
    r = E.run_one("_t_hr23")
    assert not r["chk"]["rs_X"] and r["chk"]["rs_Y"]
    # MRSA-only basis: demands follow the scaled MRSA distribution (base shear = V)
    c2 = _cfg(NX=3, NY=2, analyses=["ELF", "RS"], seismic_demand_basis="MRSA")
    SF2 = E.seismic_design_forces(c2)
    assert sum(SF2["X"]["Fx"].values()) == pytest.approx(E.elf_dir(c2, "X")[1])


def test_mrsa_drift_scaling_where_eq_12_8_7_governs():
    c = _cfg(NX=3, NY=2, analyses=["ELF", "RS"], seis=E.seis(1.0, 0.2, 0.9, 8, 0.028, 0.8, 1.4))
    for d in ("X", "Y"):
        Cs, V, Tu, Ta, kk, Fx, W = E.elf_dir(c, d)
        assert Cs == pytest.approx(0.5*0.9/8)                                              # Eq. 12.8-7
        rs = E.mrsa(c, d)
        assert rs["Vt"] < Cs*W and rs["drift_scale"] == pytest.approx(Cs*W/rs["Vt"])        # 12.9.1.4.2

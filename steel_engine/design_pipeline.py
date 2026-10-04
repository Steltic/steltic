"""
design_pipeline.py  --  ASCE 7-22 LRFD combinations + per-member DEMAND envelope. NO capacities.

This builds the turnkey ASCE 7-22 §2.3 LRFD combination set (combos) and runs every combination
through second-order P-Delta to envelope the per-member DEMANDS (design). It writes the demand
package (member_schedule.csv, member_demands.md, calc_package.json, connection_demands.csv,
design_report.md).

It does **NOT** compute any AISC 360 member capacity or D/C — there is no coded capacity anywhere
in this repo. The design agent must query the AISC 360 / 341 RAG, derive each governing
limit-state equation itself (compression E3, tension D2, flexure F2-F6, shear G2,
beam-column interaction H1, the App.8 B2 amplifier, the AISC 341 SCWB / Omega0 capacity-design
check), compute the capacity and D/C, cite the clause, and fill them into calc_package.json.

Run:  python design_pipeline.py B02
"""
import os, sys, math, csv, json
import openseespy.opensees as ops
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine"))
import engine3d as E
import sections as S
from design_post import run_case   # analysis/demand extraction only (no capacities)
import static_model as SM

# ---------- build the LRFD combination set ----------
_PRINC = {"Lr": 1.6, "S": 1.0, "R": 1.6}     # ASCE 7-22 2.3.1 combo 3a principal roof factors
_COMP = {"Lr": 0.5, "S": 0.3, "R": 0.5}      # combos 2a / 4a companion roof factors


def _roof_branches(cfg, fac):
    """Roof variable-load branches present (Lr always unless 0; S / R when declared at any level) with the
    branches whose factored roof pressure is <= another branch's at EVERY level dropped (identical load
    shape, so a dominated branch cannot govern any member). Returns [(name, factor)]."""
    NF = len(cfg["heights"])
    vec = {}
    for b in ("Lr", "S", "R"):
        v = [fac[b] * SM.roof_psf(cfg, k)[("Lr", "S", "R").index(b)] for k in range(1, NF + 1)]
        if any(x > 0 for x in v):
            vec[b] = v
    if not vec:
        return [("Lr", fac["Lr"])]
    keep = []
    for b, v in vec.items():
        dom = any(o != b and all(x >= y for x, y in zip(vo, v)) and (vo != v or list(vec).index(o) < list(vec).index(b))
                  for o, vo in vec.items())
        if not dom:
            keep.append((b, fac[b]))
    return keep


def _rf(cfg, b=None, f=0.0):
    return SM.RoofFactors(cfg=cfg, **({b: f} if b else {}))


def _live_label(cfg):
    """Companion live label: 0.5L, 1.0L, or 0.5L with the 1.0L levels listed (2.3.1 Exc. 1 per level)."""
    NF = len(cfg["heights"])
    lv = [k for k in range(1, NF + 1) if SM.live_psf(cfg, k) > 0]
    full = [k for k in lv if SM.live_is_full(cfg, k)]
    if lv and len(full) == len(lv):
        return "1.0L"
    if full:
        return "0.5L(1.0L@L%s)" % ",".join(str(k) for k in full)
    return "0.5L"


def _ecl_systems(cfg):
    """AISC 341 systems of the braces in the built model (SM.brace_system) -> set; plus R."""
    out = set()
    try:
        info = E.build(cfg, "Linear")
        for (t, kind, sec, n1, n2) in info.get("ele", []):
            if kind == "brace":
                out.add(SM.brace_system(cfg, {"sec": sec, "n1": n1, "n2": n2}))
    except Exception:
        pass
    return out


def combos(cfg):
    """ASCE 7-22 LRFD combinations (2.3.1 / 2.3.6) as SM.Case objects (legacy 6-tuples
    (label, fD, fL, fLr, lateral, col_only) with extra attributes, see static_model.Case):
      1a 1.4D;  2a 1.2D+1.6L+(0.5Lr | 0.3S | 0.5R);  3a 1.2D+(1.6Lr | 1.0S | 1.6R)+(L | 0.5W);
      4a 1.2D+1.0W+L+(0.5Lr | 0.3S | 0.5R);  5a 0.9D+1.0W (+ MWFRS roof suction, Fig. 27.3-1);
      6  (1.2+0.2SDS)D + rho*E + L + 0.15S;  7 (0.9-0.2SDS)D + rho*E   -- 100/30 with BOTH orthogonal signs
         (12.5.3/12.5.4, HR-30) and +/- accidental torsion;
      Omega0 (12.4.3) column cases for every R > 3 SFRS and every braced frame (AISC 341-22 D1.4a(b), HR-06);
      capacity-limited Ecl cases (12.4.3.2; AISC 341-22 F2.3 / F3.3 / F4.3 / F5.3, HR-07) and OCBF V-beam
         cases (F1.4a(a)), analysed by static_model.demand_envelope.
    Companion live factor per level: 0.5 where Lo <= 100 psf, 1.0 where Lo > 100 psf / garage / assembly
    (2.3.1 Exc. 1, 2.3.6 Exc. 1). Roof branches present: Lr (cfg['Lr'], default 20), S (snow), R (rain);
    a branch dominated at every level by another is omitted for the wind-companion combos."""
    s = cfg["seis"]; SDS = s["SDS"]
    NF = len(cfg["heights"])
    # Seismic forces and factors PER DIRECTION (engine3d.seismic_design_forces): each direction's own
    # period (HR-01) and R/Cd/Omega0/rho (ASCE 7-22 12.2.2, HR-02), forces at the centre of mass (HR-31),
    # accidental torsion 0.05*B*Ax with the actual plan dimension and Ax of 12.8.4.3 (HR-08/19), and the
    # 12.9.1.4-scaled MRSA story forces when 'RS' is requested (HR-23).
    SF = E.seismic_design_forces(cfg)
    Ev = 0.2*SDS
    C = SM.Case
    Lf = SM.LiveFactor(0.5, 1.0); LL = _live_label(cfg)
    Om0 = {d: float(SF[d]["Om0"]) for d in ("X", "Y")}      # per direction (Ecl OCBF min(Om0 E, RyFyAg))
    snow = any(SM.roof_psf(cfg, k)[1] > 0 for k in range(1, NF + 1))
    allb = [b for b in ("Lr", "S", "R")
            if any(SM.roof_psf(cfg, k)[("Lr", "S", "R").index(b)] > 0 for k in range(1, NF + 1))] or ["Lr"]
    cases = []
    cases.append(C("1.4D", 1.4, 0.0, _rf(cfg), {}, False))
    for b in allb:                                                   # 2a
        cases.append(C("1.2D+1.6L+%.1f%s" % (_COMP[b], b), 1.2, 1.6, _rf(cfg, b, _COMP[b]), {}, False))
    for b in allb:                                                   # 3a with (L)
        cases.append(C("1.2D+%.1f%s+%s" % (_PRINC[b], b, LL), 1.2, Lf, _rf(cfg, b, _PRINC[b]), {}, False))
    def Elat(dirn, sgn, acc, fac):
        # fac = "rho" | "Om0" -> that factor of EACH direction (principal and 30 % companion use their own
        # direction's forces and factor); see engine3d.lateral_pattern
        return E.lateral_pattern(cfg, dirn, sgn, acc, fac)
    # 100/30 (12.5.3 / 12.5.4): the orthogonal 30 % is applied with BOTH signs (HR-30); the torsional
    # moment depends only on the principal component, so flipping the orthogonal one is exact.
    # cfg['orthogonal_combination'] = False drops the 30 % (12.5.2 / 12.5.3 where not required).
    orth = cfg.get("orthogonal_combination", True)
    osgs = (1, -1) if orth else (1,)
    def Elat(dirn, sgn, acc, fac, osg=1):
        # the orthogonal 30 % (the OTHER direction's forces x its own rho / Omega0, at its CM) with sign
        # osg relative to the principal; the CM-offset torque is recomputed for that sign (hr-seismic
        # lateral_pattern), so flipping the companion stays consistent with the mass location
        return E.lateral_pattern(cfg, dirn, sgn, acc, fac, companion=(0.3*osg if orth else 0.0))
    def olab(dirn, osg):
        return ("/0.3%s%s" % ("Y" if dirn == "X" else "X", "+" if osg > 0 else "-")) if orth else ""
    fS = _rf(cfg, "S", 0.15) if snow else _rf(cfg)                   # 2.3.6 combo 6 companion 0.15S
    _sS = "+0.15S" if snow else ""
    # standard seismic (rho E), both dirs, +/-, +/- accidental torsion, +/- orthogonal 30 %
    for dirn in ("X", "Y"):
        for sgn in (1, -1):
            for acc in (1, -1):
                for osg in osgs:
                    tg = "%s%st%s%s" % (dirn, '+' if sgn > 0 else '-', '+' if acc > 0 else '-', olab(dirn, osg))
                    cases.append(C("(1.2+0.2SDS)D+rhoE%s+%s%s" % (tg, LL, _sS), 1.2+Ev, Lf, fS,
                                   Elat(dirn, sgn, acc, "rho", osg), False, kind="seismic"))
                    cases.append(C("(0.9-0.2SDS)D+rhoE%s" % tg, 0.9-Ev, 0.0, _rf(cfg),
                                   Elat(dirn, sgn, acc, "rho", osg), False, kind="seismic"))
    # Omega0 (ASCE 7-22 12.4.3, 2.3.6 combos 6/7 with Emh): axial demand on the COLUMNS of every SFRS --
    # AISC 341-22 D1.4a(b) applies to moment frames, braced frames and walls alike (HR-06); braced R <= 3
    # frames keep them as before. No accidental torsion (Emh = Omega0 * QE).
    R = max(float(SF[d]["R"] or 0) for d in ("X", "Y"))   # an R > 3 SFRS in either direction
    if E.is_braced(cfg) or R > 3.0:
        for dirn in ("X", "Y"):
            for sgn in (1, -1):
                for osg in osgs:
                    tg = "%s%s%s" % (dirn, '+' if sgn > 0 else '-', olab(dirn, osg))
                    cases.append(C("(1.2+0.2SDS)D+Om0*E%s+%s%s [col]" % (tg, LL, _sS), 1.2+Ev, Lf, fS,
                                   Elat(dirn, sgn, 0, "Om0", osg), True, kind="omega0", applies={"col"}))
                    cases.append(C("(0.9-0.2SDS)D+Om0*E%s [col]" % tg, 0.9-Ev, 0.0, _rf(cfg),
                                   Elat(dirn, sgn, 0, "Om0", osg), True, kind="omega0", applies={"col"}))
    # capacity-limited seismic load effect Ecl (ASCE 7-22 12.4.3.2 substitutes Ecl for Emh in 2.3.6) for
    # the AISC 341 braced systems present; analysed by static_model with the yielding elements replaced by
    # their expected / adjusted strengths, yielding in both directions simultaneously (D1.4a, F2.3, F3.3,
    # F4.3, F5.3). Labelled separately from the Omega0 cases.
    if R > 3.0 or cfg.get("ecl_force"):
        systems = _ecl_systems(cfg)
        patX = {k: (SF["X"]["Fx"][k], 0.0, 0.0) for k in range(1, NF + 1)}   # each direction's own forces
        patY = {k: (0.0, SF["Y"]["Fx"][k], 0.0) for k in range(1, NF + 1)}
        main = sorted(x for x in systems if x in ("SCBF", "BRBF", "EBF", "SPSW"))
        if main:
            clause = "/".join({"SCBF": "F2.3", "BRBF": "F4.3", "EBF": "F3.3", "SPSW": "F5.3"}[x] for x in main)
            variants = ("a", "b") if "SCBF" in main else ("a",)
            for sx in (1, -1):
                for sy in (1, -1):
                    for var in variants:
                        tg = "X%sY%s" % ('+' if sx > 0 else '-', '+' if sy > 0 else '-')
                        vt = "(%s)" % var if len(variants) > 1 else ""
                        ec = dict(sx=sx, sy=sy, variant=var, patterns=(patX, patY), Om0=Om0)
                        cases.append(C("(1.2+0.2SDS)D+Ecl[%s]%s+%s%s [capacity-limited, AISC 341-22 %s]"
                                       % (tg, vt, LL, _sS, clause), 1.2+Ev, Lf, fS, {}, True, kind="ecl", ecl=ec))
                        cases.append(C("(0.9-0.2SDS)D+Ecl[%s]%s [capacity-limited, AISC 341-22 %s]"
                                       % (tg, vt, clause), 0.9-Ev, 0.0, _rf(cfg), {}, True, kind="ecl", ecl=ec))
        if "OCBF" in systems:
            for sx in (1, -1):
                for sy in (1, -1):
                    tg = "X%sY%s" % ('+' if sx > 0 else '-', '+' if sy > 0 else '-')
                    ec = dict(sx=sx, sy=sy, variant="a", patterns=(patX, patY), Om0=Om0)
                    cases.append(C("(1.2+0.2SDS)D+E[OCBF V-beam %s]+%s%s [AISC 341-22 F1.4a(a), beams]"
                                   % (tg, LL, _sS), 1.2+Ev, Lf, fS, {}, True, kind="ecl_ocbf", ecl=ec,
                                   applies={"beam"}))
                    cases.append(C("(0.9-0.2SDS)D+E[OCBF V-beam %s] [AISC 341-22 F1.4a(a), beams]" % tg,
                                   0.9-Ev, 0.0, _rf(cfg), {}, True, kind="ecl_ocbf", ecl=ec, applies={"beam"}))
    if cfg.get("wind"):
        pr = _roof_branches(cfg, _PRINC); co = _roof_branches(cfg, _COMP)
        for dirn in ("X", "Y"):
            wf = E.wind_forces(cfg, dirn)
            for sgn in (1, -1):
                sg = '+' if sgn > 0 else '-'
                latW = {k: (sgn*wf[k], 0.0, 0.0) if dirn == "X" else (0.0, sgn*wf[k], 0.0) for k in wf}
                lat05 = {k: tuple(0.5*v for v in fv) for k, fv in latW.items()}
                for b, f in pr:                                      # 3a with (0.5W)
                    cases.append(C("1.2D+%.1f%s+0.5W%s%s" % (f, b, dirn, sg), 1.2, 0.0, _rf(cfg, b, f),
                                   lat05, False, kind="wind"))
                for b, f in co:                                      # 4a
                    cases.append(C("1.2D+1.0W%s%s+%s+%.1f%s" % (dirn, sg, LL, f, b), 1.2, Lf, _rf(cfg, b, f),
                                   latW, False, kind="wind"))
                upl = (dirn, sgn) if (cfg["wind"].get("roof_uplift", True) is not False) else None
                cases.append(C("0.9D+1.0W%s%s%s" % (dirn, sg, " (roof uplift)" if upl else ""), 0.9, 0.0,
                               _rf(cfg), latW, False, kind="wind", uplift=upl))   # 5a
    return cases


def collector_extras(cfg, cases=None):
    """ASCE 7-22 12.10.2.1(a) collector demands (SDC C-F): Omega0 x the axial that the ELF story forces
    induce in the beams of each SFRS line when the diaphragm delivers the line force uniformly along the
    line (static_model.collector_forces). Items (b)/(c) (Fpx of Eqs. 12.10-1/-2) and the 12.3.3.5 25 %
    increase are covered by the seeded collector slot / the agent. Returns None outside SDC C-F."""
    try:
        sdc = E.sdc_of(cfg)
    except Exception:
        sdc = None
    if sdc not in ("C", "D", "E", "F"):
        return None
    NF = len(cfg["heights"])
    SF = E.seismic_design_forces(cfg)          # each direction's own ELF forces and Omega0 (HR-01/02)
    out = {}
    for d in ("X", "Y"):
        Fx = SF[d].get("F_elf") or SF[d]["Fx"]; Om0 = float(SF[d]["Om0"] or 2.0)
        pat = {k: ((Fx[k], 0.0, 0.0) if d == "X" else (0.0, Fx[k], 0.0)) for k in range(1, NF + 1)}
        out[d] = (pat, {k: Om0 for k in range(1, NF + 1)},
                  "Om0*E%s collector/strut axial (ASCE 7-22 12.10.2.1(a), diaphragm-delivery statics)" % d)
    return out


# ---------- main prescriptive design (operator/oracle) ----------

# ---------- DEMAND envelope (analysis only; NO AISC 360 capacities) ----------
def design(name, outdir=None):
    """Run the ASCE 7-22 LRFD combinations through P-Delta and write the per-member DEMAND
    envelope + connection demands. NO capacities / D-C are computed -- the design agent derives
    every AISC 360 / 341 check from the RAG and fills them into calc_package.json."""
    cfg = E.CFG[name]
    base = os.path.dirname(os.path.abspath(__file__))
    outdir = outdir or os.path.join(base, "buildings", name, "design")
    os.makedirs(outdir, exist_ok=True)
    cases = combos(cfg)

    info0 = E.build(cfg, "PDelta")
    reg = {t: (kind, sec, n1, n2) for (t, kind, sec, n1, n2) in info0["ele"]}
    length = {t: math.dist(ops.nodeCoord(n1), ops.nodeCoord(n2)) for t, (k, s, n1, n2) in reg.items()}
    # expected / adjusted brace strengths for the brace-connection slots (AISC 341-22 F1.6a, F2.6c, F4.6c)
    brace_cap = {}
    _bco = {"coord": {}, "braces": []}
    for t, (k, sec, n1, n2) in reg.items():                 # every brace: X-brace crossings (static_model._brace_length)
        if k == "brace":
            for n in (n1, n2):
                _bco["coord"][n] = tuple(ops.nodeCoord(n))
            _bco["braces"].append({"sec": sec, "n1": n1, "n2": n2, "etag": t})
    for t, (k, sec, n1, n2) in reg.items():
        if k == "brace" and sec not in brace_cap:
            try:
                bc = SM.brace_capacity(cfg, _bco, next(b for b in _bco["braces"] if b["etag"] == t))
            except Exception as ex:
                bc = {"err": str(ex)}
            if bc.get("T"):
                brace_cap[sec] = {"expected_tension_kip": round(bc["T"], 1),
                                  "expected_compression_kip": round(bc["C"], 1),
                                  "system": bc.get("sys"), "basis": bc.get("note", "")}

    prop = {}
    def P(sec):
        if sec not in prop:
            try: prop[sec] = S.props(sec, SEC=E.SEC)
            except Exception: prop[sec] = None
        return prop[sec]

    # ---- DEMANDS from the distributed static model (static_model.demand_envelope): gravity applied bay
    #      by bay with the seismic-weight loads (HR-03/13/27), Direct Analysis stiffness + notional loads
    #      (HR-18), solved beam shear (HR-04), signed beam axial (HR-35), Omega0 / capacity-limited cases
    #      (HR-06/07), collector statics (12.10.2.1) and the base-reaction envelope over ALL cases (HR-17).
    #      The disk cache is reused only on a complete-key match (HR-36). Results are keyed by the
    #      frozenset of the member's end nodes and mapped onto the dynamic-model element tags. ----
    reac_env = {}; dinfo = {}
    _col = collector_extras(cfg)
    _extras = {"collector": _col} if _col else None
    senv, _kinds = SM.demand_envelope(cfg, cases, nseg=int(cfg.get("demand_nseg", 6)),
                                      floor_system=cfg.get("floor_system", "one-way"),
                                      cache_dir=outdir, extras=_extras,
                                      reactions_out=reac_env, info_out=dinfo)
    env = {t: dict(comp=0.0, tens=0.0, Mz=0.0, My=0.0, V=0.0, combo="", Vcombo="", Tcombo="") for t in reg}
    score = {t: -1.0 for t in reg}
    missing = []
    for t in reg:
        se = senv.get(frozenset((reg[t][2], reg[t][3])))
        if se:
            env[t] = dict(comp=se["comp"], tens=se["tens"], Mz=se["Mz"], My=se["My"], V=se["V"],
                          combo=se["combo"], Vcombo=se.get("Vcombo", ""), Tcombo=se.get("Tcombo", ""),
                          collector=se.get("collector", 0.0), collector_combo=se.get("collector_combo", ""))
        else:
            missing.append(t)
        score[t] = (max(env[t]["comp"], env[t]["tens"]) if reg[t][0] in ("col", "brace") else env[t]["Mz"])
    _R = max(float(E.seis_dir(cfg, d).get("R", 0) or 0) for d in ("X", "Y"))   # per-direction R (12.2.2)
    if _R > 3.0 and E.is_braced(cfg):
        _sy = _ecl_systems(cfg)
        if None in _sy:
            dinfo.setdefault("not_evaluated", []).append(
                "capacity-limited (Ecl) demands NOT EVALUATED for braces whose AISC 341 system is not "
                "identified -- declare cfg['system'] (SCBF / OCBF / BRBF / EBF / SPSW) or cfg['brace_system']")
    if _R > 3.0 and "STMF" in SM._sys_tokens(cfg):
        dinfo.setdefault("not_evaluated", []).append(
            "STMF special-segment capacity-limited analysis (AISC 341-22 E4.3) NOT EVALUATED -- the "
            "agent must derive the expected special-segment strengths and the resulting demands")
    if missing:
        dinfo.setdefault("warnings", []).append(
            "%d element(s) of the dynamic model have no static-model demand (end nodes not matched), e.g. %s"
            % (len(missing), ", ".join(str(t) for t in missing[:5])))

    # ---- member_schedule.csv (every element: DEMANDS only) ----
    with open(os.path.join(outdir, "member_schedule.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ele_tag", "member", "section", "length_in", "P_comp_kip", "P_tens_kip",
                    "Mx_kipft", "My_kipft", "V_kip", "governing_combo", "V_governing_combo",
                    "P_tens_governing_combo", "collector_axial_kip"])
        for t in sorted(reg):
            kind, sec, n1, n2 = reg[t]; e = env[t]
            w.writerow([t, kind, sec, round(length[t], 1), round(e["comp"], 1), round(e["tens"], 1),
                        round(e["Mz"]/12, 1), round(e["My"]/12, 1), round(e["V"], 1), e["combo"],
                        e.get("Vcombo", ""), e.get("Tcombo", ""), round(e.get("collector", 0.0) or 0.0, 1)])

    # ---- member_demands.md (summary by type; capacities are the AGENT's job) ----
    # group by (kind, section, ROLE), with the group demand = max of EVERY component over ALL members
    # in the group (not one max-moment representative) so axial-governed members are not understated,
    # and auto-tag a role so the report places members without the agent re-deriving it (P3).
    braced = E.is_braced(cfg); NFlev = len(cfg["heights"])
    brace_lines = set()
    for (t, kind, sec, n1, n2) in info0["ele"]:
        if kind == "brace":
            for nd in (n1, n2): brace_lines.add(((nd % 100000) // 100, nd % 100))
    # B3: a column is LATERAL if a brace OR a rigid (moment) beam frames into it; otherwise GRAVITY.
    # moment_nodes (from the build) carries the nodes a non-released beam connects to, so perimeter
    # moment-frame columns auto-tag lateral_col and interior gravity columns auto-tag gravity_col -- the
    # agent no longer has to relabel interiors by hand.
    moment_lines = {((nd % 100000) // 100, nd % 100) for nd in info0.get("moment_nodes", set())}
    lateral_lines = brace_lines | moment_lines
    def _role(kind, n1, n2):
        if kind == "brace": return "brace"
        if kind == "beam":  return "roof" if (n1 // 100000) >= NFlev else "floor"
        ij = ((n1 % 100000) // 100, n1 % 100)                  # column line (i,j)
        return "lateral_col" if ij in lateral_lines else "gravity_col"
    role_of = {t: _role(reg[t][0], reg[t][2], reg[t][3]) for t in reg}
    by = {}
    for t in reg:
        kind, sec, n1, n2 = reg[t]; by.setdefault((kind, sec, role_of[t]), []).append(t)
    genv = {}
    for key, tags in by.items():
        gg = dict(comp=0.0, tens=0.0, Mz=0.0, My=0.0, V=0.0, Vcombo="", Tcombo="", collector=0.0)
        for t in tags:
            e = env[t]
            if e["V"] > gg["V"]: gg["Vcombo"] = e.get("Vcombo", "")
            if e["tens"] > gg["tens"]: gg["Tcombo"] = e.get("Tcombo", "")
            for kk in ("comp", "tens", "Mz", "My", "V"): gg[kk] = max(gg[kk], e[kk])
            gg["collector"] = max(gg["collector"], e.get("collector", 0.0) or 0.0)
        tg = max(tags, key=lambda t: score[t])
        gg["combo"] = env[tg]["combo"]; gg["L"] = max(length[t] for t in tags); genv[key] = gg
    with open(os.path.join(outdir, "member_demands.md"), "w") as f:
        f.write("# %s - member DEMAND envelope (ASCE 7-22 LRFD, second-order P-Delta)\n\n" % name)
        nk = {}
        for c in cases:
            nk[getattr(c, "kind", "gravity")] = nk.get(getattr(c, "kind", "gravity"), 0) + 1
        f.write("Load combinations: %d (ASCE 7-22 2.3.1 / 2.3.6: gravity with every roof branch (Lr / S / R) "
                "and the per-level companion live factor; seismic with Ev, rho, 100/30 with both orthogonal "
                "signs, +/-accidental torsion; Omega0 [col] for every SFRS; capacity-limited Ecl where an AISC "
                "341 braced system is present%s) -- %s. Each is run as a factored second-order case on the "
                "static model and enveloped per element.\n\n"
                % (len(cases), "; wind incl. 0.5W companion and 0.9D+1.0W roof uplift" if cfg.get("wind") else "",
                   ", ".join("%s %d" % kv for kv in sorted(nk.items()))))
        _dam = dinfo.get("dam")
        if _dam:
            f.write("**Analysis basis: Direct Analysis Method (AISC 360-22 Ch. C)** -- stiffness x%.2f on all "
                    "members (C2.3(a)), tau_b = 1.0 (%s), notional loads Ni = %.3f*Yi in gravity-only "
                    "combinations (4 directions) and %.3f*Yi added to lateral combinations (C2.2b; max "
                    "second-/first-order drift ratio %.2f %s 1.7). Use K = 1 (C3). Periods and drift use "
                    "nominal stiffness.\n\n"
                    % (_dam["stiffness_factor"], "C2.3(c) 0.001*Yi add-on" if _dam["tau_b"] == "notional"
                       else "alpha*Pr/Pns checked", _dam["notional_gravity_only"], _dam["notional_lateral_cases"],
                       _dam["drift_ratio_2nd_1st"], ">" if _dam["drift_ratio_2nd_1st"] > 1.7 else "<="))
        else:
            f.write("**Analysis basis: nominal-stiffness P-Delta (cfg['dam'] = False)** -- NOT the Direct "
                    "Analysis Method: use the effective length method (App. 7) with K from the alignment "
                    "charts, or enable cfg['dam'].\n\n")
        for _ne in dinfo.get("not_evaluated", []):
            f.write("> **NOT EVALUATED:** %s\n\n" % _ne)
        for _nt in dinfo.get("notes", []):
            f.write("> Note: %s\n\n" % _nt)
        for _wn in dinfo.get("warnings", []):
            f.write("> **WARNING:** %s\n\n" % _wn)
        f.write("> **Capacities and D/C are NOT computed here.** The framework provides demands only; "
                "the design agent derives each AISC 360-22 limit-state capacity (compression E3, tension "
                "D2, flexure F2-F6, shear G2, beam-column interaction H1), the App.8 B2 amplifier, and "
                "the AISC 341 SCWB / Omega0 column check from the RAG, computes D/C, cites the clause, and "
                "records them in calc_package.json.\n\n")
        f.write("| member type | section | n | governing combo | P_comp | P_tens | Mx(k-ft) | My | V | V combo |\n")
        f.write("|---|---|---:|---|---:|---:|---:|---:|---:|---|\n")
        for key, tags in sorted(by.items()):
            kind, sec, role = key; g = genv[key]
            f.write("| %s | %s | %d | %s | %.0f | %.0f | %.0f | %.0f | %.0f | %s |\n"
                    % (role, sec, len(tags), g["combo"], g["comp"], g["tens"], g["Mz"]/12, g["My"]/12, g["V"],
                       g.get("Vcombo", "")))
        if reac_env:
            f.write("\n## Column base reactions (envelope over ALL combinations incl. Omega0 / Ecl)\n\n")
            f.write("| base node | P_comp (kip) | combo | uplift (kip) | combo | V (kip) | combo | M (k-ft) |\n")
            f.write("|---|---:|---|---:|---|---:|---|---:|\n")
            for n in sorted(reac_env):
                r = reac_env[n]
                f.write("| %s | %.0f | %s | %.0f | %s | %.0f | %s | %.0f |\n"
                        % (n, r["P"], r["P_combo"], r["uplift"], r["uplift_combo"], r["V"], r["V_combo"], r["M"]/12))

    # ---- calc_package.json (DEMANDS only; agent adds limit_state / cited / capacity / DC) ----
    pkg = {"building": name, "code": "AISC 360-22 LRFD",
           "note": "Framework provides DEMANDS only. The agent must derive every capacity and D/C "
                   "from the AISC 360/341 RAG and add 'limit_state', 'cited', 'capacity', and 'DC' to "
                   "each member and connection.", "members": [], "connections": []}
    for key, tags in sorted(by.items()):
        kind, sec, role = key; g = genv[key]; L = g["L"]
        inp = {"kind": kind, "role": role, "section": sec, "length_in": round(L, 1)}
        p = P(sec)
        if kind == "brace":
            inp.update(A=(E.HSS.get(sec) if hasattr(E, "HSS") else None), r=S.brace_r(sec))
        elif p:
            Lb_eff = min(L, 0.095*p["ry"]*29000.0/50.0) if kind == "beam" else L
            inp.update(Lb_in=round(Lb_eff, 1), A=p["A"], Ix=p["Ix"], Iy=p["Iy"], J=p["J"], Zx=p["Zx"],
                       Zy=round(p["Zy"], 1), Sx=round(p["Sx"], 1), Sy=round(p["Sy"], 1),
                       rx=round(p["rx"], 3), ry=round(p["ry"], 3),
                       Aw=round(p["Aw"], 2) if p.get("Aw") else None,
                       ho=round(p["ho"], 2) if p.get("ho") else None,
                       rts=round(p["rts"], 3) if p.get("rts") else None)
        inp.update(P_comp_kip=round(g["comp"], 2), P_tens_kip=round(g["tens"], 2),
                   Mz_kipin=round(g["Mz"], 1), My_kipin=round(g["My"], 1), V_kip=round(g["V"], 2),
                   governing_combo=g["combo"], V_governing_combo=g.get("Vcombo", ""),
                   P_tens_governing_combo=g.get("Tcombo", ""))
        if g.get("collector"):
            inp.update(collector_axial_kip=round(g["collector"], 1),
                       collector_basis="ASCE 7-22 12.10.2.1 (Omega0 / Fpx), diaphragm-delivery statics")
        if kind == "brace" and brace_cap.get(sec):
            inp.update(expected_strength=brace_cap[sec])
        pkg["members"].append({"id": "%s-%s" % (role, sec), "inputs": inp,
                               "limit_state": None, "cited": None, "capacity": {}, "DC": None})
    # ---- connections[] : one design slot per governing member type + column base (DEMANDS only;
    #      the agent designs each connection in place and fills limit_state/cited/capacity/DC) ----
    for key, tags in sorted(by.items()):
        kind, sec, role = key; e = genv[key]
        if kind == "beam":
            ctype = "beam-to-column (shear; + moment if MF)"
            dem = {"V_kip": round(e["V"], 1), "M_kipft": round(e["Mz"]/12, 1)}
            if max(e["comp"], e["tens"]) > 0.5:
                dem.update(P_comp_kip=round(e["comp"], 1), P_tens_kip=round(e["tens"], 1))
            basis = "AISC 360 Ch.J (J2 welds / J3 bolts / J4 block shear); MF per AISC 358 / 341"
        elif kind == "brace":
            ctype = "brace-to-gusset"
            dem = {"axial_kip": round(max(e["comp"], e["tens"]), 1)}
            bc = brace_cap.get(sec)
            if bc:
                dem.update({k: v for k, v in bc.items() if k.endswith("_kip")})
                basis = ("AISC 360 Ch.J gusset/weld; AISC 341-22 required strength from the expected / "
                         "adjusted brace strength (%s)" % bc.get("basis", ""))
            else:
                basis = "AISC 360 Ch.J gusset/weld; seismic expected strength RyFyAg per AISC 341"
        else:
            ctype = "column splice"
            dem = {"P_kip": round(e["comp"], 1), "P_comp_kip": round(e["comp"], 1),
                   "P_tens_kip": round(e["tens"], 1), "V_kip": round(e["V"], 1),
                   "M_kipft": round(e["Mz"]/12, 1), "My_kipft": round(e["My"]/12, 1),
                   "tension_combo": e.get("Tcombo", ""), "shear_combo": e.get("Vcombo", "")}
            basis = ("AISC 360 J1.4 splice; AISC 341-22 D2.5 (tension/shear incl. Omega0 / Ecl cases); "
                     "envelope over all combinations")
        pkg["connections"].append({"id": "conn-%s-%s" % (role, sec), "type": ctype, "section": sec,
                                   "demand": dem, "design_basis": basis,
                                   "limit_state": None, "cited": None, "capacity": {}, "DC": None})
    # ---- column BASE slots from the base-reaction envelope over ALL combinations (HR-17) ----
    base_of = {}
    for t, (kind, sec, n1, n2) in reg.items():
        if kind == "col":
            for nd in (n1, n2):
                if nd in reac_env:
                    base_of.setdefault((role_of[t], sec), []).append(nd)
    for (role, sec), nds in sorted(base_of.items()):
        rr = [reac_env[n] for n in nds]
        rP = max(rr, key=lambda r: r["P"]); rU = max(rr, key=lambda r: r["uplift"])
        rV = max(rr, key=lambda r: r["V"]); rM = max(rr, key=lambda r: r["M"])
        pkg["connections"].append({
            "id": "conn-base-%s-%s" % (role, sec), "type": "column base plate / anchorage", "section": sec,
            "demand": {"P_comp_kip": round(rP["P"], 1), "P_comp_combo": rP["P_combo"],
                       "uplift_kip": round(rU["uplift"], 1), "uplift_combo": rU["uplift_combo"],
                       "V_kip": round(rV["V"], 1), "Vx_kip": round(max(r["Vx"] for r in rr), 1),
                       "Vy_kip": round(max(r["Vy"] for r in rr), 1), "V_combo": rV["V_combo"],
                       "M_kipft": round(rM["M"] / 12, 1), "M_combo": rM["M_combo"], "n_bases": len(nds)},
            "design_basis": "AISC 360 J8-J9 base plate + ACI 318 Ch.17 anchorage; AISC 341-22 D2.6 "
                            "(reactions enveloped over ALL combinations incl. 0.9D+1.0W roof uplift, Omega0 "
                            "and capacity-limited cases)",
            "limit_state": None, "cited": None, "capacity": {}, "DC": None})
    # ---- SEEDED COLLECTOR SLOTS + FRAMEWORK IRREGULARITY SCREEN (hardening #3/#9) ----
    # When the footprint screen finds a re-entrant corner or setback, seed a collector design slot
    # with the diaphragm-force demand so the package CANNOT silently omit it (weak-LLM miss #1).
    # Also write the framework-computed irregularity screen (story-stiffness ratios + torsion
    # ratio + classification) so the agent only has to RESPOND to it, not derive it.
    try:
        pir = E.plan_irregularities(cfg)
    except Exception:
        pir = {}
    try:
        NFq = len(cfg["heights"])
        SFq = E.seismic_design_forces(cfg)          # per-direction forces + factors (HR-01/02/23)
        wlev = {k: E.floor_w(cfg, k) for k in range(1, NFq + 1)}
        SDSq = float(cfg["seis"].get("SDS", 1.0)); Ieq = float(cfg["seis"].get("Ie", 1.0))
        Fpx_d = {}
        for dq in ("X", "Y"):
            Fxq = SFq[dq]["Fx"]; Fpx_d[dq] = {}
            for k in range(1, NFq + 1):
                num = sum(Fxq[i] for i in range(k, NFq + 1)); den = sum(wlev[i] for i in range(k, NFq + 1))
                Fpx_d[dq][k] = min(max(num / den * wlev[k], 0.2 * SDSq * Ieq * wlev[k]), 0.4 * SDSq * Ieq * wlev[k])
        Fpx = {k: max(Fpx_d["X"][k], Fpx_d["Y"][k]) for k in range(1, NFq + 1)}
        Fp_max = max(Fpx.values())
        # story-stiffness soft-story screen (each direction with ITS OWN ELF forces) + the ONE torsion
        # result (engine3d.torsion_summary: TIR with accidental torsion at the actual edges, Ax from
        # displacements, Eq. 12.3-2 / 12.8-15) -- computed BEFORE the collector seeding so a Type 1
        # torsional irregularity (TIR > 1.2) can trigger the 12.3.3.5 25% increase there
        sxq = E.static_lateral(cfg, SFq["X"]["F_elf"], "X"); syq = E.static_lateral(cfg, SFq["Y"]["F_elf"], "Y")
        def _kratio(s_, Fxq):
            dr = s_[2]
            Vst = [sum(Fxq[i] for i in range(k, NFq + 1)) for k in range(1, NFq + 1)]
            K = [abs(Vst[k - 1] / dr[k - 1]) if abs(dr[k - 1]) > 1e-12 else 1e9 for k in range(1, NFq + 1)]
            r1 = K[0] / K[1] if NFq >= 2 else 9.9
            r3 = K[0] / (sum(K[1:4]) / max(len(K[1:4]), 1)) if NFq >= 4 else r1
            return round(r1, 2), round(r3, 2)
        kx, kx3 = _kratio(sxq, SFq["X"]["F_elf"]); ky, ky3 = _kratio(syq, SFq["Y"]["F_elf"])
        cls = ("none" if min(kx, ky) >= 0.70 and min(kx3, ky3) >= 0.80 else
               ("Type 1b EXTREME soft story (PROHIBITED SDC E/F, 12.3.3.1)"
                if min(kx, ky) < 0.60 or min(kx3, ky3) < 0.70 else "Type 1a soft story"))
        TSq = E.torsion_summary(cfg)
        tr = TSq["TIR"]
        # 7-22 Table 12.3-1/-1a: SINGLE Type 1 torsional irregularity keyed to the TIR with
        # cumulative tiers >1.2 / >1.4 / >1.6 (no 1a/1b split, no SDC E/F prohibition).
        tcls = TSq["classification"]
        Ax = TSq["Ax_max"]
        # 12.3.3.5 (SDC D-F): 25% diaphragm-force increase for horizontal Type 1 (TIR>1.2),
        # 2 (re-entrant), 3, 4 (out-of-plane offset) or vertical Type 3. Type 4 is not auto-screened
        # here -- declare it via the collector design if present.
        # Table 12.3-1 Type 1 is defined on drifts INCLUDING accidental torsion (Ax = 1.0): torsion_summary's
        # TIR already is that value (hr-seismic HR-08), so it triggers the collector seed (HR-34)
        tors_trig = tr > 1.2
        if pir.get("reentrant") or pir.get("setback") or tors_trig:
            # HR-34 collector demand. ASCE 7-22 12.10.2.1 (SDC C-F): collectors and their connections
            # resist the MAX of (a) Om0 x ELF/MRSA forces, (b) Om0 x Fpx (Eq. 12.10-1, with its
            # 12.10-2/-3 limits) and (c) the 2.3.6 combinations with Fpx = 0.2 SDS Ie wpx (Eq. 12.10-2).
            # The 12.3.3.5 25 % increase (SDC D-F, horizontal Type 1/2/3/4, vertical Type 3) is NOT
            # stacked on overstrength forces (12.3.3.5 EXCEPTION) -- it can only govern item (c).
            # SDC A/B: 12.10.2.1 does not apply -> Fpx without Om0 (12.3.3.5 is SDC D-F only).
            Om0q = max(float(SFq["X"]["Om0"]), float(SFq["Y"]["Om0"]))   # 12.10.2.1, per-direction Om0
            try:
                sdcq = E.sdc_of(cfg)
            except Exception:
                sdcq = "D"
            kmax = max(Fpx, key=lambda k: Fpx[k])
            Fpx_min = 0.2 * SDSq * Ieq * wlev[kmax]
            inc = 1.25 if (sdcq in ("D", "E", "F") and (pir.get("reentrant") or pir.get("setback") or tors_trig)) else 1.0
            if sdcq in ("C", "D", "E", "F"):
                Pb, Pc = Om0q * Fp_max, inc * Fpx_min
                Pgov = max(Pb, Pc); rule = ("12.10.2.1(b) Om0 x Fpx" if Pb >= Pc else
                                            "12.10.2.1(c) Eq. 12.10-2 x %.2f (12.3.3.5)" % inc)
            else:
                Pb, Pc, Pgov, rule = None, None, Fp_max, "SDC %s: Fpx (12.10.1.1), no overstrength" % sdcq
            share = 0.5
            pkg["connections"].append({
                "id": "collector-irregularity-lines", "type": "collector / drag strut (SEEDED - REQUIRED)",
                "demand": {"Fpx_max_kip": round(Fp_max, 0), "Om0": Om0q, "SDC": sdcq,
                           "P_12_10_2_1_b_kip": None if Pb is None else round(Pb, 0),
                           "P_12_10_2_1_c_kip": None if Pc is None else round(Pc, 0),
                           "increase_12_3_3_5": inc,
                           "governing": rule,
                           "line_share_assumed": share,
                           "P_basis_kip": round(share * Pgov, 0)},
                "design_basis": "SEEDED because the screen found %s: collectors on the "
                                "re-entrant/setback/transfer lines are a REQUIRED deliverable. Design "
                                "for the MAX of ASCE 7-22 12.10.2.1 (a) Om0 x the ELF/MRSA collector force from "
                                "your diaphragm model, (b) Om0 x Fpx and (c) Fpx(Eq. 12.10-2)%s -- the 25%% "
                                "increase of 12.3.3.5 is NOT applied on top of Om0 (12.3.3.5 Exception). "
                                "P_basis assumes %.0f%% of the level force per collector line: replace that "
                                "share with your diaphragm geometry; fill limit_state/cited/capacity/DC like "
                                "any other connection." % (
                                    "/".join(k for k, on in (("reentrant", pir.get("reentrant")),
                                                             ("setback", pir.get("setback")),
                                                             ("torsional TIR>1.2 (with accidental torsion)", tors_trig)) if on),
                                    " x 1.25 (12.3.3.5)" if inc > 1.0 else "", 100 * share),
                "limit_state": None, "cited": None, "capacity": {}, "DC": None})
        pkg["framework_screen"] = {
            "note": "FRAMEWORK-COMPUTED irregularity screen -- the agent RESPONDS to these (classify "
                    "consequences, apply rho/Ax/25% collector increases as required); do not re-derive.",
            "plan": {k: bool(v) for k, v in pir.items()},
            "soft_story": {"K1_over_K2": {"X": kx, "Y": ky}, "K1_over_avg3": {"X": kx3, "Y": ky3},
                           "classification": cls, "cite": "ASCE 7-22 Table 12.3-2 (computed)"},
            "torsion": {"ratio_max": round(tr, 2), "TIR_X": round(TSq["X"]["TIR"], 3),
                        "TIR_Y": round(TSq["Y"]["TIR"], 3), "classification": tcls, "Ax": Ax,
                        "Ax_by_level": {d: {k: v for k, v in TSq[d]["Ax"].items()} for d in ("X", "Y")},
                        "Ax_applied_to_Mta": bool(TSq["amplify"]),
                        "drift_location": TSq["X"]["drift_location"],
                        "cite": "ASCE 7-22 Eq. 12.3-2 / Table 12.3-1 / 12.8.4.3 Eq. 12.8-15 / 12.8.6.5 (computed)"},
            "seismic_by_direction": {d: {kk: (round(v, 4) if isinstance(v, float) else v)
                                         for kk, v in SFq[d].items() if kk in
                                         ("T", "Tu", "Ta", "Cs", "V", "k", "R", "Cd", "Om0", "rho", "Ie", "system", "sfrs", "basis")}
                                     for d in ("X", "Y")},
            "seismic_demand_basis": SFq["basis"],
            "mrsa": {d: ({"Vt_kip": round(SFq[d]["rs"]["Vt"], 1), "V_elf_kip": round(SFq[d]["V"], 1),
                          "force_scale_12_9_1_4_1": round(SFq[d]["rs"]["force_scale"], 4),
                          "drift_scale_12_9_1_4_2": round(SFq[d]["rs"]["drift_scale"], 4),
                          "story_forces_scaled_kip": {k: round(v, 1) for k, v in SFq[d]["rs"]["F"].items()}}
                         if SFq[d]["rs"] else None) for d in ("X", "Y")},
            "seismic_warnings": SFq.get("warnings", []),
            "Fpx_kip_by_level": {k: round(v, 0) for k, v in Fpx.items()},
            "Fpx_kip_by_level_dir": {d: {k: round(v, 0) for k, v in Fpx_d[d].items()} for d in ("X", "Y")},
        }
    except Exception as _se:
        pkg["framework_screen"] = {"error": "screen failed: %s" % _se}
    pkg["demand_analysis"] = {
        "note": "How the framework demands were produced (static_model.demand_envelope). Items under "
                "'not_evaluated' are NOT in the demands -- the agent must provide them.",
        "analysis": ("Direct Analysis Method, AISC 360-22 C2 (see 'dam')" if dinfo.get("dam") else
                     "nominal-stiffness second-order (cfg['dam'] = False): NOT the Direct Analysis Method"),
        "dam": dinfo.get("dam"), "floor_load_modes": dinfo.get("floor_modes"),
        "gravity_audit_dead_kip": {"note": "per level: (static model, seismic weight W) dead load",
                                   "levels": dinfo.get("gravity_audit_dead_kip")},
        "capacity_limited": dinfo.get("ecl"), "collector_lines": dinfo.get("collector"),
        "not_evaluated": dinfo.get("not_evaluated", []), "notes": dinfo.get("notes", []),
        "warnings": dinfo.get("warnings", [])[:40], "solves": dinfo.get("solves"),
        "cases": len(cases)}
    _cp = os.path.join(outdir, "calc_package.json")
    try:                                            # never silently destroy the agent's filled package
        if os.path.exists(_cp):
            _old = json.load(open(_cp))
            _filled = any(m.get("limit_state") or m.get("DC") is not None for m in _old.get("members", [])) \
                   or any(c.get("DC") is not None or c.get("checks") for c in _old.get("connections", []))
            if _filled:
                import shutil as _sh; _sh.copy(_cp, _cp + ".filled.bak")
                print("[design] WARNING: existing calc_package.json had agent capacities -> backed up to "
                      "calc_package.json.filled.bak before overwriting with fresh demands. Do NOT re-run "
                      "design_and_report to make a report; re-render with report.build_report "
                      "(which preserves your capacities).")
    except Exception:
        pass
    json.dump(pkg, open(_cp, "w"), indent=1)

    # ---- connection_demands.csv (demands + the limit-state checklist the agent sizes) ----
    with open(os.path.join(outdir, "connection_demands.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["connection", "member_tag", "type", "demand_kip_or_kipft", "note"])
        for t in sorted(reg):
            kind, sec, n1, n2 = reg[t]; e = env[t]
            if kind == "beam":
                w.writerow(["beam-end @ %s/%s" % (n1, n2), t, "shear (+moment if MF)",
                            "V=%.1f kip, M=%.1f kip-ft" % (e["V"], e["Mz"]/12),
                            "size per AISC 360 Ch.J (agent derives bolt/weld/plate from RAG); MF per A358"])
            elif kind == "brace":
                w.writerow(["brace @ %s/%s" % (n1, n2), t, "axial",
                            "P=%.1f kip" % max(e["comp"], e["tens"]),
                            "gusset/weld per AISC 360 Ch.J; seismic capacity-design RyFyAg per AISC 341 (agent)"])
        # base reactions: envelope over ALL combinations (gravity, wind incl. roof uplift, rho E, Omega0,
        # Ecl) from the static demand model -- not one gravity combination (HR-17)
        for n in sorted(reac_env):
            r = reac_env[n]; i, j = (n % 100000) // 100, n % 100
            w.writerow(["column base @ grid(%s,%s)" % (i, j), n, "base plate/anchorage",
                        "P=%.1f kip [%s]; uplift=%.1f kip [%s]; V=%.1f (Vx=%.1f, Vy=%.1f) kip [%s]; M=%.1f kip-ft [%s]"
                        % (r["P"], r["P_combo"], r["uplift"], r["uplift_combo"], r["V"], r["Vx"], r["Vy"],
                           r["V_combo"], r["M"] / 12, r["M_combo"]),
                        "base plate/anchor rods per AISC 360 J8/J9 (agent); anchorage ACI 318 Ch.17; AISC 341-22 D2.6"])

    # ---- optional opsvis figures ----
    figs = []
    try:
        import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
        import opsvis as opsv
        E.build(cfg, "Linear")
        opsv.plot_model(node_labels=0, element_labels=0); plt.savefig(os.path.join(outdir, "fig_model.png"), dpi=140); plt.close()
        _l, _fd, _fl, _flr, _lat, _co = cases[1]; run_case(cfg, _fd, _fl, _flr, _lat)
        opsv.plot_defo(sfac=30); plt.savefig(os.path.join(outdir, "fig_deformed.png"), dpi=140); plt.close()
        figs = ["fig_model.png", "fig_deformed.png"]
    except Exception as ex:
        with open(os.path.join(outdir, "figures_note.txt"), "w") as f:
            f.write("opsvis/matplotlib not available here; reviewer figures come from plot_model.py.\n(%s)\n" % ex)

    # ---- design_report.md ----
    with open(os.path.join(outdir, "design_report.md"), "w") as f:
        f.write("# %s - demand summary\n\n" % name)
        _sx, _sy = E.seis_dir(cfg, "X"), E.seis_dir(cfg, "Y")
        f.write("Archetype: %s; %d storeys; system %s; R=%s, Ie=%s.\n\n"
                % (cfg.get("arch", ""), len(cfg["heights"]),
                   cfg.get("system") or "NOT DECLARED (set cfg['system']; HR-26)",
                   _sx["R"] if _sx["R"] == _sy["R"] else "%s (X) / %s (Y)" % (_sx["R"], _sy["R"]),
                   cfg["seis"]["Ie"]))
        f.write("- Load combinations run: **%d** (LRFD, second-order each; %s).\n"
                % (len(cases), "Direct Analysis Method, AISC 360-22 C2" if dinfo.get("dam") else
                   "nominal stiffness -- not DAM"))
        for _ne in dinfo.get("not_evaluated", []):
            f.write("- **NOT EVALUATED:** %s\n" % _ne)
        f.write("- Members enveloped: **%d** elements.\n" % len(reg))
        f.write("- **Capacities / D-C: derived by the agent from the AISC 360/341 RAG** (not computed by the framework).\n\n")
        f.write("Files: member_schedule.csv (per-element demands), member_demands.md (summary by type), "
                "connection_demands.csv (+ checklist), calc_package.json (demands; agent fills capacities).\n")
    print("[%s] %d combos, %d members -> DEMAND envelope written (capacities = agent/RAG)" % (name, len(cases), len(reg)))
    print("  output: %s" % outdir)
    return {"members": len(reg), "combos": len(cases), "outdir": outdir}   # B8: truthy -> pipeline.demands_written is True

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

# ---------- build the LRFD combination set ----------
def combos(cfg):
    s = cfg["seis"]; SDS = s["SDS"]; rho = cfg.get("rho", 1.3)
    NF = len(cfg["heights"]); Bx = cfg["NX"]*cfg["SX"]; By = cfg["NY"]*cfg["SY"]
    T, *_ = E.modal(cfg, min(3*NF, 12)); _, V, Tu, Ta, kk, Fx, W = E.elf(cfg, T[0])
    Ev = 0.2*SDS
    # ASCE 7-22 §2.3.1 Exception 1 / §2.3.6 Exception 1: companion live factor 0.5 only where
    # Lo <= 100 psf AND the occupancy is not a garage or place of public assembly (else 1.0).
    # cfg['garage'] / cfg['public_assembly'] are optional, default False.
    Lf = 1.0 if (cfg.get("L_floor", 50.0) > 100.0 or cfg.get("garage", False)
                 or cfg.get("public_assembly", False)) else 0.5
    Om0 = s.get("Om0", 2.0)
    # roof load is snow when declared (E.floor_roofLrS returns S, else Lr). ASCE 7-22 factors:
    # principal 1.0S / 1.6Lr (combo 3a); companion 0.3S / 0.5Lr (combos 2a/4a); seismic 0.15S (§2.3.6).
    snow = float(cfg.get("snow", 0) or 0)
    rl = "S" if snow > 0 else "Lr"
    fR_pr, fR_co = (1.0, 0.3) if snow > 0 else (1.6, 0.5)
    # cases are 6-tuples: (label, fD, fL, fLr, lateral, col_only). col_only cases (Om0 overstrength)
    # are applied to columns only (capacity-protected members).
    cases = []
    cases.append(("1.4D", 1.4, 0.0, 0.0, {}, False))
    cases.append((f"1.2D+1.6L+{fR_co}{rl}", 1.2, 1.6, fR_co, {}, False))
    cases.append((f"1.2D+{fR_pr}{rl}+{Lf}L", 1.2, Lf, fR_pr, {}, False))  # ASCE 7-22 combo 3a: 1.6Lr or
                                                                 # 1.0S governs roof-controlled members
    def Elat(dirn, sgn, acc, fac):
        lat = {}
        for k in range(1, NF+1):
            if dirn == "X": fx, fy = Fx[k], 0.3*Fx[k]; B = By
            else:           fx, fy = 0.3*Fx[k], Fx[k]; B = Bx
            mz = acc*0.05*B*(fx if dirn == "X" else fy)
            lat[k] = (fac*sgn*fx, fac*sgn*fy, fac*sgn*mz)
        return lat
    fS = 0.15 if snow > 0 else 0.0                      # §2.3.6 combo 6 companion snow = 0.15S
    _sS = "+0.15S" if snow > 0 else ""
    # standard seismic (rho E), both dirs, +/-, +/- accidental torsion
    for dirn in ("X", "Y"):
        for sgn in (1, -1):
            for acc in (1, -1):
                cases.append((f"(1.2+0.2SDS)D+rhoE{dirn}{'+' if sgn>0 else '-'}t{'+' if acc>0 else '-'}+{Lf}L{_sS}",
                              1.2+Ev, Lf, fS, Elat(dirn, sgn, acc, rho), False))
                cases.append((f"(0.9-0.2SDS)D+rhoE{dirn}{'+' if sgn>0 else '-'}t{'+' if acc>0 else '-'}",
                              0.9-Ev, 0.0, 0.0, Elat(dirn, sgn, acc, rho), False))
    # AISC 341 capacity design (simplified): braced-frame COLUMNS designed for the overstrength
    # seismic load Em = Om0*QE (ASCE 7-22 §12.4.3.2). No accidental torsion needed here.
    # E.is_braced() detects braces from cfg['braces'] OR brace elements in a custom_build model (P1).
    if E.is_braced(cfg):
        for dirn in ("X", "Y"):
            for sgn in (1, -1):
                cases.append((f"(1.2+0.2SDS)D+Om0*E{dirn}{'+' if sgn>0 else '-'}+{Lf}L{_sS} [col]",
                              1.2+Ev, Lf, fS, Elat(dirn, sgn, 0, Om0), True))
                cases.append((f"(0.9-0.2SDS)D+Om0*E{dirn}{'+' if sgn>0 else '-'} [col]",
                              0.9-Ev, 0.0, 0.0, Elat(dirn, sgn, 0, Om0), True))
    if cfg.get("wind"):
        for dirn in ("X", "Y"):
            wf = E.wind_forces(cfg, dirn)
            for sgn in (1, -1):
                latW = {k: (sgn*wf[k], 0.0, 0.0) if dirn == "X" else (0.0, sgn*wf[k], 0.0) for k in wf}
                cases.append((f"1.2D+1.0W{dirn}{'+' if sgn>0 else '-'}+{Lf}L+{fR_co}{rl}",
                              1.2, Lf, fR_co, latW, False))
                cases.append((f"0.9D+1.0W{dirn}{'+' if sgn>0 else '-'}", 0.9, 0.0, 0.0, latW, False))
    return cases

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

    prop = {}
    def P(sec):
        if sec not in prop:
            try: prop[sec] = S.props(sec, SEC=E.SEC)
            except Exception: prop[sec] = None
        return prop[sec]

    # ---- DEMANDS from the SINGLE distributed static model (gravity-correct column axial / base
    #      reactions; one-way vs two-way girder moment per cfg["floor_system"]). Two-key DISK cache:
    #      gravity solved ONCE (size-invariant when the gravity path is determinate / pinned joints),
    #      seismic keyed on the LATERAL members' sections so it is reused while only gravity members
    #      change. Keyed by frozenset of corner nodes -> mapped onto the dynamic-model element tags. ----
    import static_model as SM
    brace_lines = set(); 
    for (t, kind, sec, n1, n2) in info0["ele"]:
        if kind == "brace":
            for nd in (n1, n2): brace_lines.add(((nd % 100000)//100, nd % 100))
    moment_lines = {((nd % 100000)//100, nd % 100) for nd in info0.get("moment_nodes", set())}
    lat_lines = brace_lines | moment_lines
    def _is_lat(t):
        k, sec, n1, n2 = reg[t]
        return True if k == "brace" else (((n1 % 100000)//100, n1 % 100) in lat_lines)
    sec_sig = repr(sorted(reg[t][1] for t in reg))                            # full schedule (indeterminate gravity)
    lat_sig = repr(sorted(reg[t][1] for t in reg if _is_lat(t)))             # lateral-system sections (seismic key)
    determinate = str((cfg.get("model") or {}).get("joints", "")).lower() == "pinned"
    senv, _kinds = SM.demand_envelope(cfg, cases, nseg=int(cfg.get("demand_nseg", 6)),
                                      floor_system=cfg.get("floor_system", "one-way"),
                                      determinate=determinate, sec_sig=sec_sig, lat_sig=lat_sig,
                                      cache_dir=outdir)
    env = {t: dict(comp=0.0, tens=0.0, Mz=0.0, My=0.0, V=0.0, combo="") for t in reg}
    score = {t: -1.0 for t in reg}
    for t in reg:
        se = senv.get(frozenset((reg[t][2], reg[t][3])))
        if se:
            env[t] = dict(comp=se["comp"], tens=se["tens"], Mz=se["Mz"], My=se["My"], V=se["V"], combo=se["combo"])
        score[t] = (max(env[t]["comp"], env[t]["tens"]) if reg[t][0] in ("col", "brace") else env[t]["Mz"])

    # ---- member_schedule.csv (every element: DEMANDS only) ----
    with open(os.path.join(outdir, "member_schedule.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ele_tag", "member", "section", "length_in", "P_comp_kip", "P_tens_kip",
                    "Mx_kipft", "My_kipft", "V_kip", "governing_combo"])
        for t in sorted(reg):
            kind, sec, n1, n2 = reg[t]; e = env[t]
            w.writerow([t, kind, sec, round(length[t], 1), round(e["comp"], 1), round(e["tens"], 1),
                        round(e["Mz"]/12, 1), round(e["My"]/12, 1), round(e["V"], 1), e["combo"]])

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
        gg = dict(comp=0.0, tens=0.0, Mz=0.0, My=0.0, V=0.0)
        for t in tags:
            e = env[t]
            for kk in ("comp", "tens", "Mz", "My", "V"): gg[kk] = max(gg[kk], e[kk])
        tg = max(tags, key=lambda t: score[t])
        gg["combo"] = env[tg]["combo"]; gg["L"] = max(length[t] for t in tags); genv[key] = gg
    with open(os.path.join(outdir, "member_demands.md"), "w") as f:
        f.write("# %s - member DEMAND envelope (ASCE 7-22 LRFD, second-order P-Delta)\n\n" % name)
        f.write("Load combinations: %d (gravity; seismic w/ Ev, rho, 100/30, +/-accidental torsion, "
                "Omega0 [col]%s). Each run as a factored P-Delta case; demands enveloped per element.\n\n"
                % (len(cases), ", wind" if cfg.get("wind") else ""))
        f.write("> **Capacities and D/C are NOT computed here.** The framework provides demands only; "
                "the design agent derives each AISC 360-22 limit-state capacity (compression E3, tension "
                "D2, flexure F2-F6, shear G2, beam-column interaction H1), the App.8 B2 amplifier, and "
                "the AISC 341 SCWB / Omega0 column check from the RAG, computes D/C, cites the clause, and "
                "records them in calc_package.json.\n\n")
        f.write("| member type | section | n | governing combo | P_comp | P_tens | Mx(k-ft) | My | V |\n")
        f.write("|---|---|---:|---|---:|---:|---:|---:|---:|\n")
        for key, tags in sorted(by.items()):
            kind, sec, role = key; g = genv[key]
            f.write("| %s | %s | %d | %s | %.0f | %.0f | %.0f | %.0f | %.0f |\n"
                    % (role, sec, len(tags), g["combo"], g["comp"], g["tens"], g["Mz"]/12, g["My"]/12, g["V"]))

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
                   governing_combo=g["combo"])
        pkg["members"].append({"id": "%s-%s" % (role, sec), "inputs": inp,
                               "limit_state": None, "cited": None, "capacity": {}, "DC": None})
    # ---- connections[] : one design slot per governing member type + column base (DEMANDS only;
    #      the agent designs each connection in place and fills limit_state/cited/capacity/DC) ----
    for key, tags in sorted(by.items()):
        kind, sec, role = key; e = genv[key]
        if kind == "beam":
            ctype = "beam-to-column (shear; + moment if MF)"
            dem = {"V_kip": round(e["V"], 1), "M_kipft": round(e["Mz"]/12, 1)}
            basis = "AISC 360 Ch.J (J2 welds / J3 bolts / J4 block shear); MF per AISC 358 / 341"
        elif kind == "brace":
            ctype = "brace-to-gusset"
            dem = {"axial_kip": round(max(e["comp"], e["tens"]), 1)}
            basis = "AISC 360 Ch.J gusset/weld; seismic expected strength RyFyAg per AISC 341"
        else:
            ctype = "column splice / base plate"
            dem = {"P_kip": round(e["comp"], 1), "M_kipft": round(e["Mz"]/12, 1)}
            basis = "AISC 360 J1.4 splice / J8-J9 base plate + ACI 318 Ch.17 anchorage"
        pkg["connections"].append({"id": "conn-%s-%s" % (role, sec), "type": ctype, "section": sec,
                                   "demand": dem, "design_basis": basis,
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
        Tq, w2q, eXq, eYq, Mtq = E.modal(cfg, min(3 * NFq, 12))
        Csq, Vq, Tuq, Taq, kq, Fxq, Wq = E.elf(cfg, Tq[0])
        wlev = {k: E.floor_w(cfg, k) for k in range(1, NFq + 1)}
        SDSq = float(cfg["seis"].get("SDS", 1.0)); Ieq = float(cfg["seis"].get("Ie", 1.0))
        Fpx = {}
        for k in range(1, NFq + 1):
            num = sum(Fxq[i] for i in range(k, NFq + 1)); den = sum(wlev[i] for i in range(k, NFq + 1))
            Fpx[k] = min(max(num / den * wlev[k], 0.2 * SDSq * Ieq * wlev[k]), 0.4 * SDSq * Ieq * wlev[k])
        Fp_max = max(Fpx.values())
        # story-stiffness soft-story screen (both directions) + torsion ratio -- computed BEFORE the
        # collector seeding so a Type 1 torsional irregularity (TIR screen > 1.2) can trigger the
        # 12.3.3.5 25% increase there
        sxq = E.static_lateral(cfg, Fxq, "X"); syq = E.static_lateral(cfg, Fxq, "Y")
        def _kratio(s_):
            dr = s_[2]
            Vst = [sum(Fxq[i] for i in range(k, NFq + 1)) for k in range(1, NFq + 1)]
            K = [abs(Vst[k - 1] / dr[k - 1]) if abs(dr[k - 1]) > 1e-12 else 1e9 for k in range(1, NFq + 1)]
            r1 = K[0] / K[1] if NFq >= 2 else 9.9
            r3 = K[0] / (sum(K[1:4]) / max(len(K[1:4]), 1)) if NFq >= 4 else r1
            return round(r1, 2), round(r3, 2)
        kx, kx3 = _kratio(sxq); ky, ky3 = _kratio(syq)
        cls = ("none" if min(kx, ky) >= 0.70 and min(kx3, ky3) >= 0.80 else
               ("Type 1b EXTREME soft story (PROHIBITED SDC E/F, 12.3.3.1)"
                if min(kx, ky) < 0.60 or min(kx3, ky3) < 0.70 else "Type 1a soft story"))
        tr = max(sxq[4], syq[4])
        # 7-22 Table 12.3-1/-1a: SINGLE Type 1 torsional irregularity keyed to the TIR with
        # cumulative tiers >1.2 / >1.4 / >1.6 (no 1a/1b split, no SDC E/F prohibition). The engine
        # ratio is a per-story DRIFT-based screen (the Eq. 12.3-2 TIR basis; static_lateral).
        tcls = ("none" if tr <= 1.2 else
                ("Type 1 torsional, TIR screen %.2f (>1.6 tier)" % tr if tr > 1.6 else
                 "Type 1 torsional, TIR screen %.2f (>1.4 tier)" % tr if tr > 1.4 else
                 "Type 1 torsional, TIR screen %.2f (>1.2 tier)" % tr))
        Ax = round(min((tr / 1.2) ** 2, 3.0), 2) if tr > 1.2 else 1.0
        # 12.3.3.5 (SDC D-F): 25% diaphragm-force increase for horizontal Type 1 (TIR>1.2),
        # 2 (re-entrant), 3, 4 (out-of-plane offset) or vertical Type 3. Type 4 is not auto-screened
        # here -- declare it via the collector design if present.
        tors_trig = tr > 1.2
        if pir.get("reentrant") or pir.get("setback") or tors_trig:
            Om0q = float(cfg["seis"].get("Om0", 2.5) or 2.5)
            bump = 1.25 if (pir.get("reentrant") or tors_trig) else 1.0
            pkg["connections"].append({
                "id": "collector-irregularity-lines", "type": "collector / drag strut (SEEDED - REQUIRED)",
                "demand": {"Fpx_max_kip": round(Fp_max, 0), "Om0": Om0q,
                           "increase_12_3_3_5": bump,
                           "P_basis_kip": round(bump * Om0q * Fp_max * 0.5, 0)},
                "design_basis": "SEEDED because the screen found %s: collectors on the "
                                "re-entrant/setback/transfer lines are a REQUIRED deliverable. Design "
                                "with the OVERSTRENGTH combinations (ASCE 7-22 12.10.2.1)%s. Refine the "
                                "line share from your diaphragm geometry; fill limit_state/cited/"
                                "capacity/DC like any other connection." % (
                                    "/".join(k for k, on in (("reentrant", pir.get("reentrant")),
                                                             ("setback", pir.get("setback")),
                                                             ("torsional TIR>1.2", tors_trig)) if on),
                                    " + the 25%% increase (12.3.3.5: Type 1 torsional / Type 2 "
                                    "re-entrant / Type 4)" if bump > 1.0 else ""),
                "limit_state": None, "cited": None, "capacity": {}, "DC": None})
        pkg["framework_screen"] = {
            "note": "FRAMEWORK-COMPUTED irregularity screen -- the agent RESPONDS to these (classify "
                    "consequences, apply rho/Ax/25% collector increases as required); do not re-derive.",
            "plan": {k: bool(v) for k, v in pir.items()},
            "soft_story": {"K1_over_K2": {"X": kx, "Y": ky}, "K1_over_avg3": {"X": kx3, "Y": ky3},
                           "classification": cls, "cite": "ASCE 7-22 Table 12.3-2 (computed)"},
            "torsion": {"ratio_max": round(tr, 2), "classification": tcls, "Ax": Ax,
                        "cite": "ASCE 7-22 Table 12.3-1 / 12.8.4.3 (computed)"},
            "Fpx_kip_by_level": {k: round(v, 0) for k, v in Fpx.items()},
        }
    except Exception as _se:
        pkg["framework_screen"] = {"error": "screen failed: %s" % _se}
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
        _l, _fd, _fl, _flr, _lat, _co = cases[1]; res, info = run_case(cfg, _fd, _fl, _flr, _lat); ops.reactions()
        for (i, j) in info["present"][0]:
            R = [ops.nodeReaction(E.ntag(i, j, 0), d) for d in (1, 2, 3, 4, 5, 6)]
            w.writerow(["column base @ grid(%s,%s)" % (i, j), E.ntag(i, j, 0), "base plate/anchorage",
                        "P=%.1f kip, Vx=%.1f, Vy=%.1f, M=%.1f kip-ft" % (R[2], R[0], R[1], max(abs(R[3]), abs(R[4]))/12),
                        "base plate/anchor rods per AISC 360 J8/J9 (agent); anchorage ACI 318 Ch.17"])

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
        f.write("Archetype: %s; %d storeys; system %s; R=%s, Ie=%s.\n\n"
                % (cfg["arch"], len(cfg["heights"]), "CBF/dual" if cfg.get("braces") else "moment frame",
                   cfg["seis"]["R"], cfg["seis"]["Ie"]))
        f.write("- Load combinations run: **%d** (LRFD, P-Delta each).\n" % len(cases))
        f.write("- Members enveloped: **%d** elements.\n" % len(reg))
        f.write("- **Capacities / D-C: derived by the agent from the AISC 360/341 RAG** (not computed by the framework).\n\n")
        f.write("Files: member_schedule.csv (per-element demands), member_demands.md (summary by type), "
                "connection_demands.csv (+ checklist), calc_package.json (demands; agent fills capacities).\n")
    print("[%s] %d combos, %d members -> DEMAND envelope written (capacities = agent/RAG)" % (name, len(cases), len(reg)))
    print("  output: %s" % outdir)
    return {"members": len(reg), "combos": len(cases), "outdir": outdir}   # B8: truthy -> pipeline.demands_written is True

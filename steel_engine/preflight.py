"""preflight.py -- R22 pre-analysis cfg linter. Cheap, engine-free checks run BEFORE the first
OpenSees solve so a mis-declared cfg is caught in seconds, not after a full pipeline run.
Returns a list of (severity, message); severity in {"ERROR","WARN"}. Non-blocking by design --
pipeline.design_and_report prints the findings and puts them in its return dict.

Also hosts the CANONICAL Seismic Design Category function asce_sdc() (ASCE 7-22 sec.11.6) --
engine-free so engine3d.py and report.py both import THIS implementation instead of keeping
divergent copies."""
import re


def asce_sdc(SDS, SD1, S1=0.0, risk_cat="II"):
    """Seismic Design Category per ASCE 7-22 sec.11.6: the WORSE of Table 11.6-1 (SDS) and
    Table 11.6-2 (SD1), with the Risk Category IV column, after the S1 override:
    S1 >= 0.75 -> SDC E for Risk Category I-III, F for Risk Category IV."""
    rc4 = str(risk_cat).strip().upper() in ("IV", "4")
    if S1 is not None and float(S1) >= 0.75:
        return "F" if rc4 else "E"
    def _tab(x, rows):                       # rows: (upper_bound, SDC for RC I-III, SDC for RC IV)
        for thr, c123, c4 in rows:
            if float(x) < thr:
                return c4 if rc4 else c123
        return "D"                           # top row of both tables: D for every Risk Category
    c1 = _tab(SDS, [(0.167, "A", "A"), (0.33, "B", "C"), (0.50, "C", "D")])   # Table 11.6-1
    c2 = _tab(SD1, [(0.067, "A", "A"), (0.133, "B", "C"), (0.20, "C", "D")])  # Table 11.6-2
    return max(c1, c2)


def risk_cat_from_Ie(Ie):
    """Risk Category inferred from the seismic importance factor (Table 1.5-2)."""
    Ie = float(Ie or 1.0)
    return "IV" if Ie >= 1.5 else ("III" if Ie >= 1.25 else "II")


def risk_cat_of_cfg(cfg):
    """Risk Category: an explicit cfg['risk_category'] (or 'risk_cat') wins, else inferred from Ie."""
    for k in ("risk_category", "risk_cat"):
        v = str(cfg.get(k) or "").strip().upper()
        if v in ("I", "II", "III", "IV", "1", "2", "3", "4"):
            return {"1": "I", "2": "II", "3": "III", "4": "IV"}.get(v, v)
    return risk_cat_from_Ie((cfg.get("seis") or {}).get("Ie", 1.0))


def _sdc_tables_only_11_6_1(SDS, risk_cat):
    rc4 = str(risk_cat).strip().upper() in ("IV", "4")
    for thr, c123, c4 in [(0.167, "A", "A"), (0.33, "B", "C"), (0.50, "C", "D")]:
        if float(SDS) < thr:
            return c4 if rc4 else c123
    return "D"


SDC_EXC_KEY = "sdc_11_6_exception"


def sdc_assessment(cfg):
    """ASCE 7-22 sec.11.6 SDC assessment of a cfg (engine-free).
    Returns dict(derived, table1_only, exception_ok, exception_why, declared, effective, rc).
      derived     -- worse of Tables 11.6-1/-2 after the S1 >= 0.75 override (asce_sdc);
      table1_only -- Table 11.6-1 alone (only usable under the 11.6 exception);
      exception_ok-- True when ALL four 11.6 exception conditions are shown: (1) Ta < 0.8Ts is
                     computed here from Ct*hn^x; (2) the period used for drift < Ts and (3) Cs from
                     Eq. 12.8-2 must be DECLARED in cfg['sdc_11_6_exception'] = {'T_drift': s,
                     'Cs_eq_12_8_2': True}; (4) rigid diaphragm, or a declared
                     'max_vertical_element_spacing_ft' <= 40;
      effective   -- what every engine gate uses: the declared cfg['sdc'] when it is the SAME or
                     MORE severe than the derived value, otherwise the derived value (a declared
                     lower SDC never silently lowers rho / drift / height limits)."""
    s = cfg.get("seis") or {}
    SDS = float(s.get("SDS", 0) or 0); SD1 = float(s.get("SD1", 0) or 0); S1 = float(s.get("S1", 0) or 0)
    rc = risk_cat_of_cfg(cfg)
    derived = asce_sdc(SDS, SD1, S1, rc)
    t1 = _sdc_tables_only_11_6_1(SDS, rc) if S1 < 0.75 else derived
    exc_ok, why = False, []
    ex = cfg.get(SDC_EXC_KEY)
    if S1 < 0.75 and t1 < derived and isinstance(ex, dict):
        H = [float(h) for h in (cfg.get("heights") or []) if isinstance(h, (int, float))]
        hn = sum(H) / 12.0
        Ts = SD1 / SDS if SDS > 0 else 0.0
        try:
            Ta = float(s.get("Ct")) * hn ** float(s.get("x"))
        except (TypeError, ValueError):
            Ta = None
        c1 = Ta is not None and Ta < 0.8 * Ts
        why.append("(1) Ta=%s vs 0.8Ts=%.3f s -> %s" % ("%.3f" % Ta if Ta is not None else "?", 0.8 * Ts,
                                                          "OK" if c1 else "NOT MET"))
        try:
            Td = float(ex.get("T_drift"))
        except (TypeError, ValueError):
            Td = None
        c2 = Td is not None and Td < Ts
        why.append("(2) declared T_drift=%s vs Ts=%.3f s -> %s" % (Td, Ts, "OK" if c2 else "NOT MET/NOT DECLARED"))
        c3 = bool(ex.get("Cs_eq_12_8_2"))
        why.append("(3) Cs from Eq. 12.8-2 %s" % ("declared" if c3 else "NOT DECLARED"))
        dia = str(cfg.get("diaphragm", "rigid")).lower()
        try:
            sp = float(ex.get("max_vertical_element_spacing_ft"))
        except (TypeError, ValueError):
            sp = None
        c4 = dia == "rigid" or (sp is not None and sp <= 40.0)
        why.append("(4) diaphragm %s%s -> %s" % (dia, "" if sp is None else ", spacing %.0f ft" % sp,
                                                   "OK" if c4 else "NOT MET"))
        exc_ok = c1 and c2 and c3 and c4
    req = t1 if exc_ok else derived
    decl = str(cfg.get("sdc") or "").strip().upper() or None
    eff = decl if (decl and decl in "ABCDEF" and len(decl) == 1 and decl >= req) else req
    return dict(derived=derived, table1_only=t1, exception_ok=exc_ok, exception_why=why,
                declared=decl, effective=eff, rc=rc, required=req)


def sdc_of_cfg(cfg):
    """SDC used by every engine gate (ASCE 7-22 sec.11.6). A declared cfg['sdc'] is honoured only
    when it is the same as or MORE severe than the SDC derived from SDS/SD1/S1/Risk Category
    (Tables 11.6-1/-2, or 11.6-1 alone when the 11.6 exception is demonstrated in
    cfg['sdc_11_6_exception']); a LOWER declared SDC is overridden by the derived one and preflight
    reports it as an ERROR (it would otherwise silently drop rho, the drift/rho limit and the
    Table 12.2-1 height limits)."""
    return sdc_assessment(cfg)["effective"]


# ---------------------------------------------------------------------------------------------
# ASCE 7-22 sec.16.1.2 drift relief -- cfg['drift_relief_16_1_2']
#
# "Where a nonlinear response history analysis (Chapter 16) is performed, ... the drift limits of
# sec.12.12.1 need not apply" -- for Risk Category I, II and III only. The Nonlinear module (SNL)
# writes this block when it hands a Chapter-16-verified design back for a lighter, drift-relaxed
# re-design; the agent copies it verbatim into cfg and sets cfg['drift_limit'] to 'linear_target'.
# The relief never applies to Risk Category IV (Ie >= 1.5), and the relaxed linear target may never
# exceed the Chapter 16 mean-drift limit itself. Every finding here is mirrored in consistency.py.
# ---------------------------------------------------------------------------------------------
RELIEF_KEY = "drift_relief_16_1_2"
RELIEF_REQUIRED = ("nlrha_mean_drift", "nlrha_limit", "linear_target")


def drift_relief(cfg):
    """The cfg['drift_relief_16_1_2'] block as a dict, or None when absent."""
    r = cfg.get(RELIEF_KEY) if isinstance(cfg, dict) else None
    return r if isinstance(r, dict) and r else None


def relief_active(cfg):
    """True when a well-formed 16.1.2 relief block is present AND the Risk Category permits it."""
    r = drift_relief(cfg)
    if r is None:
        return False
    Ie = float((cfg.get("seis") or {}).get("Ie", 1.0) or 1.0)
    if Ie >= 1.5:
        return False
    try:
        return all(float(r.get(k)) > 0 for k in RELIEF_REQUIRED)
    except (TypeError, ValueError):
        return False


def relief_findings(cfg):
    """(severity, message) findings for a cfg that carries the 16.1.2 relief block."""
    out = []
    r = drift_relief(cfg)
    if r is None:
        return out
    s = cfg.get("seis") or {}
    Ie = float(s.get("Ie", 1.0) or 1.0)
    dl = float(cfg.get("drift_limit", 0.020) or 0.020)
    if Ie >= 1.5:
        out.append(("ERROR", "cfg['%s'] present but Ie=%.2f (Risk Category IV): ASCE 7-22 16.1.2 keeps the "
                             "12.12.1 drift limits for RC IV -- remove the relief block and design to Table "
                             "12.12-1 (0.010)" % (RELIEF_KEY, Ie)))
        return out
    missing = [k for k in RELIEF_REQUIRED if not isinstance(r.get(k), (int, float)) or float(r.get(k)) <= 0]
    if missing:
        out.append(("ERROR", "cfg['%s'] is missing %s -- the relief must record the Chapter 16 result it rests "
                             "on (SNL writes these; do not invent them)" % (RELIEF_KEY, missing)))
        return out
    lim16 = float(r["nlrha_limit"]); tgt = float(r["linear_target"]); mean16 = float(r["nlrha_mean_drift"])
    if dl > lim16 + 1e-9:
        out.append(("ERROR", "drift_limit=%.4f exceeds the Chapter 16 mean-drift limit %.4f recorded in cfg['%s'] "
                             "-- a linear target above the 16.4.1.2 limit is not a relief, it is a mistake"
                             % (dl, lim16, RELIEF_KEY)))
    if abs(dl - tgt) > 1e-6:
        out.append(("WARN", "drift_limit=%.4f differs from the relief's linear_target=%.4f -- set cfg['drift_limit'] "
                            "to the target the Nonlinear module derived, or record why it changed" % (dl, tgt)))
    if mean16 > lim16:
        out.append(("ERROR", "cfg['%s'] records a Chapter 16 mean drift %.4f ABOVE its limit %.4f -- the relief "
                             "rests on an analysis that did not pass 16.4.1.2" % (RELIEF_KEY, mean16, lim16)))
    out.append(("WARN", "ASCE 7-22 16.1.2 drift relief in force (Risk Category %s): Table 12.12-1 need not apply; "
                        "linear design target %.4f from the Chapter 16 result (mean MCE_R drift %.4f vs %.4f). "
                        "The final design must be re-verified by a Chapter 16 analysis before issue."
                        % (risk_cat_from_Ie(Ie), tgt, mean16, lim16)))
    return out

# ---------------------------------------------------------------------------------------------
# ASCE 7-22 Table 12.2-1 (steel rows) + Table 12.8-2 -- system factors and structural height limits
#
# lim: structural height limit h_n (ft) per Seismic Design Category; None = NL (not limited),
#      0.0 = NP (not permitted). SDC A uses the SDC B column. Ct_x: Table 12.8-2 (Ct, x).
# inc: the system is one of those listed in 12.2.5.4 (increase 160->240 ft in SDC D/E, 100->160 ft
#      in SDC F when TIR <= 1.4 and no plane resists > 60 % of the seismic force per direction).
# exc: the clause family that may permit an otherwise NP / exceeded height (single-story etc.).
# ---------------------------------------------------------------------------------------------
NL, NP = None, 0.0
SYSTEMS = {
    "ebf":  dict(name="steel eccentrically braced frame", R=8.0, Om0=2.0, Cd=4.0,
                 lim=dict(B=NL, C=NL, D=160.0, E=160.0, F=100.0), Ct_x=(0.03, 0.75), inc=True),
    "scbf": dict(name="steel special concentrically braced frame", R=6.0, Om0=2.0, Cd=5.0,
                 lim=dict(B=NL, C=NL, D=160.0, E=160.0, F=100.0), Ct_x=(0.02, 0.75), inc=True),
    "ocbf": dict(name="steel ordinary concentrically braced frame", R=3.25, Om0=2.0, Cd=3.25,
                 lim=dict(B=NL, C=NL, D=35.0, E=35.0, F=NP), Ct_x=(0.02, 0.75), exc="ocbf_j"),
    "brbf": dict(name="steel buckling-restrained braced frame", R=8.0, Om0=2.5, Cd=5.0,
                 lim=dict(B=NL, C=NL, D=160.0, E=160.0, F=100.0), Ct_x=(0.03, 0.75), inc=True),
    "spsw": dict(name="steel special plate shear wall", R=7.0, Om0=2.0, Cd=6.0,
                 lim=dict(B=NL, C=NL, D=160.0, E=160.0, F=100.0), Ct_x=(0.02, 0.75), inc=True),
    "c-psw": dict(name="steel and concrete composite plate shear wall", R=6.5, Om0=2.5, Cd=5.5,
                  lim=dict(B=NL, C=NL, D=160.0, E=160.0, F=100.0), Ct_x=(0.02, 0.75)),
    "cc-psw": dict(name="steel and concrete coupled composite plate shear wall", R=8.0, Om0=2.5, Cd=5.5,
                   lim=dict(B=NL, C=NL, D=160.0, E=160.0, F=100.0), Ct_x=(0.02, 0.75), inc=True),
    "smf":  dict(name="steel special moment frame", R=8.0, Om0=3.0, Cd=5.5,
                 lim=dict(B=NL, C=NL, D=NL, E=NL, F=NL), Ct_x=(0.028, 0.8), mf=True),
    "stmf": dict(name="steel special truss moment frame", R=7.0, Om0=3.0, Cd=5.5,
                 lim=dict(B=NL, C=NL, D=160.0, E=100.0, F=NP), Ct_x=(0.028, 0.8), mf=True),
    "imf":  dict(name="steel intermediate moment frame", R=4.5, Om0=3.0, Cd=4.0,
                 lim=dict(B=NL, C=NL, D=35.0, E=NP, F=NP), Ct_x=(0.028, 0.8), mf=True, exc="imf"),
    "omf":  dict(name="steel ordinary moment frame", R=3.5, Om0=3.0, Cd=3.0,
                 lim=dict(B=NL, C=NL, D=NP, E=NP, F=NP), Ct_x=(0.028, 0.8), mf=True, exc="omf"),
    "nsd":  dict(name="steel system not specifically detailed for seismic resistance", R=3.0, Om0=3.0,
                 Cd=3.0, lim=dict(B=NL, C=NL, D=NP, E=NP, F=NP), Ct_x=(0.02, 0.75)),
    # dual systems (12.2.5.1): moment frames resist >= 25 % of the design seismic forces
    "dual:smf+ebf":  dict(name="dual: SMF + steel EBF", R=8.0, Om0=2.5, Cd=4.0,
                          lim=dict(B=NL, C=NL, D=NL, E=NL, F=NL), Ct_x=(0.03, 0.75), dual=True),
    "dual:smf+scbf": dict(name="dual: SMF + steel SCBF", R=7.0, Om0=2.5, Cd=5.5,
                          lim=dict(B=NL, C=NL, D=NL, E=NL, F=NL), Ct_x=(0.02, 0.75), dual=True),
    "dual:smf+brbf": dict(name="dual: SMF + steel BRBF", R=8.0, Om0=2.5, Cd=5.0,
                          lim=dict(B=NL, C=NL, D=NL, E=NL, F=NL), Ct_x=(0.03, 0.75), dual=True),
    "dual:smf+spsw": dict(name="dual: SMF + steel SPSW", R=8.0, Om0=2.5, Cd=6.5,
                          lim=dict(B=NL, C=NL, D=NL, E=NL, F=NL), Ct_x=(0.02, 0.75), dual=True),
    "dual:smf+cc-psw": dict(name="dual: SMF + steel and concrete coupled composite plate shear walls", R=8.0,
                            Om0=2.5, Cd=5.5, lim=dict(B=NL, C=NL, D=NL, E=NL, F=NL), Ct_x=(0.02, 0.75),
                            dual=True),
    "dual:imf+scbf": dict(name="dual: IMF + steel SCBF", R=6.0, Om0=2.5, Cd=5.0,
                          lim=dict(B=NL, C=NL, D=35.0, E=NP, F=NP), Ct_x=(0.02, 0.75), dual=True),
}
_LONG = (("special truss moment", "stmf"), ("special moment", "smf"), ("intermediate moment", "imf"),
         ("ordinary moment", "omf"), ("special concentrically", "scbf"), ("ordinary concentrically", "ocbf"),
         ("eccentrically", "ebf"), ("buckling-restrained", "brbf"), ("buckling restrained", "brbf"),
         ("special plate shear", "spsw"), ("coupled composite plate shear", "cc-psw"),
         ("composite plate shear", "c-psw"),
         ("not specifically detailed", "nsd"))
_ABBR = re.compile(r"(?<![a-z])(stmf|smf|imf|omf|scbf|ocbf|ebf|brbf|spsw|cc-?psw|c-?psw)(?![a-z])")
_BRACED = ("ebf", "scbf", "ocbf", "brbf", "spsw", "c-psw", "cc-psw")
H_INC_KEY = "height_increase_12_2_5_4"
H_EXC_KEY = "height_limit_exception"


def parse_system(cfg_or_name):
    """Classify cfg['system'] against Table 12.2-1. Returns dict(kind, rows, parts, text) with
    kind in {'single','dual','mixed','unknown','none'}: 'rows' are the SYSTEMS keys whose limits
    and factors apply ('dual' -> the dual row; 'mixed' (12.2.2 per-direction or 12.2.3 vertical
    combination) -> EVERY component row), 'parts' the individual system tokens found."""
    name = cfg_or_name.get("system") if isinstance(cfg_or_name, dict) else cfg_or_name
    txt = str(name or "").lower()
    if not txt.strip():
        return dict(kind="none", rows=[], parts=[], text=txt)
    found = []
    for pat, key in _LONG:
        if pat in txt and key not in found:
            found.append(key)
    for m in _ABBR.finditer(txt):
        g_ = m.group(1)
        key = "cc-psw" if g_.startswith("cc") else ("c-psw" if g_.startswith("c") else g_)
        if key not in found:
            found.append(key)
    if "cc-psw" in found and "c-psw" in found:  # 'coupled composite plate shear' also reads as C-PSW
        found.remove("c-psw")
    if any(k in txt for k in ("not detailed", "r=3", "r = 3")) and "nsd" not in found:
        found.append("nsd")
    if not found:
        return dict(kind="unknown", rows=[], parts=[], text=txt)
    if "nsd" in found:                   # an explicit 'not specifically detailed' system governs the row;
        return dict(kind="single", rows=["nsd"], parts=found, text=txt)   # 'OMF-type' etc. is description
    if "dual" in txt:
        mfs = [k for k in found if k in ("smf", "imf")]
        brs = [k for k in found if k in _BRACED]
        if len(mfs) == 1 and len(brs) == 1 and ("dual:%s+%s" % (mfs[0], brs[0])) in SYSTEMS:
            return dict(kind="dual", rows=["dual:%s+%s" % (mfs[0], brs[0])], parts=found, text=txt)
        return dict(kind="unknown", rows=[], parts=found, text=txt)
    if len(found) == 1:
        return dict(kind="single", rows=found, parts=found, text=txt)
    return dict(kind="mixed", rows=found, parts=found, text=txt)


def mf_only_system(cfg):
    """True when EVERY system in cfg['system'] is a moment frame (SMF/IMF/OMF/STMF family); a dual
    or a mixed moment-frame + braced declaration is NOT moment-frame-only (12.12.1.1 applies only
    in a direction resisted solely by moment frames)."""
    ps = parse_system(cfg)
    if ps["kind"] in ("single", "mixed"):
        return all(SYSTEMS[k].get("mf") for k in ps["rows"])
    txt = ps["text"]
    return ("moment" in txt and "dual" not in txt and not any(b in txt for b in ("brac", "wall")))


def _hn_ft(cfg):
    H = [float(h) for h in (cfg.get("heights") or []) if isinstance(h, (int, float))]
    return sum(H) / 12.0, len(H)


def _num_or_none(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def increase_12_2_5_4(cfg, pkg_evidence=None):
    """Evaluate the ASCE 7-22 12.2.5.4 increased height limit for this cfg.
    Evidence comes from cfg['height_increase_12_2_5_4'] (or, in consistency.check, from
    calc_package capacity_design['height_limit']) = {'TIR_max': x, 'max_plane_share_X': a,
    'max_plane_share_Y': b} -- NUMBERS; shares are FRACTIONS of the direction's total seismic force
    resisted by the most-loaded plane, neglecting accidental torsion (a value > 1 is a plane carrying
    more than the story force, e.g. with reversed shear on other lines); 'max_plane_share_X_pct'
    takes a percentage. Returns (ok, why)."""
    ev = pkg_evidence if isinstance(pkg_evidence, dict) else cfg.get(H_INC_KEY)
    if not isinstance(ev, dict):
        return False, ("no 12.2.5.4 evidence: record calc_package capacity_design['height_limit'] (or "
                       "cfg['%s']) = {'TIR_max': <Eq. 12.3-2 TIR>, 'max_plane_share_X': <largest share of the "
                       "X seismic force resisted by any one plane>, 'max_plane_share_Y': ...} as NUMBERS "
                       "(accidental torsion neglected)" % H_INC_KEY)
    ps = parse_system(cfg)
    if ps["kind"] not in ("single", "mixed") or not all(SYSTEMS[k].get("inc") for k in ps["rows"]):
        return False, ("12.2.5.4 applies only when the SFRS is limited to steel EBF / SCBF / BRBF / SPSW "
                       "(or coupled C-PSW / RC walls); system '%s' is not" % ps["text"])
    tir = _num_or_none(ev.get("TIR_max", ev.get("TIR")))
    def _share(d):                       # a FRACTION (0.55); '<key>_pct' carries a percentage (55)
        v = _num_or_none(ev.get("max_plane_share_" + d))
        if v is None:
            v = _num_or_none(ev.get("max_plane_share_%s_pct" % d))
            v = None if v is None else v / 100.0
        return v
    sx = _share("X"); sy = _share("Y")
    probs = []
    if tir is None:
        probs.append("TIR_max not given")
    elif tir > 1.4 + 1e-9:
        probs.append("TIR_max=%.2f > 1.4 (condition 1)" % tir)
    for d, v in (("X", sx), ("Y", sy)):
        if v is None:
            probs.append("max_plane_share_%s not given" % d)
        else:
            if v > 0.60 + 1e-9:
                probs.append("plane share %s = %.0f%% > 60%% (condition 2)" % (d, v * 100))
    if probs:
        return False, "12.2.5.4 conditions not shown: " + "; ".join(probs)
    return True, "12.2.5.4 conditions shown (TIR %.2f <= 1.4; plane share X %.0f%%, Y %.0f%% <= 60%%)" % (
        tir, sx * 100, sy * 100)


def _height_exception(cfg, key, sdc, hn, nst):
    """Single-story / light-frame exceptions of 12.2.5.6 (OMF), 12.2.5.7 (IMF) and Table 12.2-1
    footnote j (OCBF). Needs cfg['height_limit_exception'] = {'clause': ..., 'roof_dead_psf': ..,
    'wall_dead_psf': .., optional 'light_frame': True, 'floor_dead_psf': .., 'equipment_enclosure':
    True}. Returns (permitted_limit_ft or None, why)."""
    ex = cfg.get(H_EXC_KEY)
    fam = SYSTEMS[key].get("exc")
    if not fam:
        return None, ""
    if not isinstance(ex, dict):
        if nst > 1 and (fam == "ocbf_j" or (fam == "imf" and sdc == "D") or (fam == "omf" and sdc == "F")):
            return None, ""                              # only single-story exceptions exist here
        return None, ("if a %s exception applies, declare cfg['%s'] = {'clause': '12.2.5.%s', "
                      "'roof_dead_psf': .., 'wall_dead_psf': ..%s}"
                      % ("single-story" if nst == 1 else "light-frame", H_EXC_KEY,
                         "7" if fam == "imf" else "6", "" if nst == 1 else ", 'light_frame': True, 'floor_dead_psf': .."))
    roof = max(x for x in (_num_or_none(ex.get("roof_dead_psf")), _num_or_none(cfg.get("D_roof")), 0.0)
               if x is not None)
    wall = max(x for x in (_num_or_none(ex.get("wall_dead_psf")), _num_or_none(cfg.get("clad")), 0.0)
               if x is not None)
    floor = max(x for x in (_num_or_none(ex.get("floor_dead_psf")),
                            _num_or_none(cfg.get("D_floor")) if nst > 1 else None, 0.0) if x is not None)
    why = "roof DL %.0f psf, wall DL %.0f psf, %d story(ies)" % (roof, wall, nst)
    if fam == "ocbf_j":                                  # Table 12.2-1 footnote j
        if nst == 1 and roof <= 20.0:
            return 60.0, "Table 12.2-1 footnote j (single-story, roof DL <= 20 psf): " + why
        return None, "footnote j needs a single story and roof DL <= 20 psf (%s)" % why
    if nst == 1 and ex.get("equipment_enclosure") and roof <= 20.0 and wall <= 20.0 and sdc in ("D", "E") \
            and fam in ("imf", "omf"):
        return float("inf"), "single-story equipment enclosure exception (roof+equipment <= 20 psf): " + why
    if nst == 1 and roof <= 20.0 and wall <= 20.0:
        return 65.0, ("12.2.5.%s single-story (a) (roof DL <= 20 psf, wall DL <= 20 psf): "
                      % ("7" if fam == "imf" else "6")) + why
    if ex.get("light_frame") and roof <= 35.0 and floor <= 35.0 and wall <= 20.0 and \
            not (fam == "imf" and sdc == "D") and not (fam == "omf" and sdc == "F"):
        return 35.0, "light-frame construction (b) (roof/floor DL <= 35 psf, wall DL <= 20 psf): " + why
    return None, "no 12.2.5.6/12.2.5.7 exception condition met (%s)" % why


def system_limit_findings(cfg, pkg_height_evidence=None):
    """(severity, message) findings: Table 12.2-1 structural height limits / NP systems for EVERY
    system in cfg['system'] in the SDC of the building (sdc_of_cfg), with the 12.2.5.4 increase and
    the single-story / light-frame exceptions only where their conditions are DECLARED and met."""
    out = []
    ps = parse_system(cfg)
    hn, nst = _hn_ft(cfg)
    if ps["kind"] == "none":
        return out
    if ps["kind"] == "unknown":
        out.append(("WARN", "cfg['system']='%s' is not recognised against the steel rows of Table 12.2-1 -- "
                            "height limits and R/Cd/Om0/Ct/x are NOT checked; use the exact system name "
                            "(SMF, IMF, OMF, STMF, SCBF, OCBF, EBF, BRBF, SPSW, 'dual SMF+BRBF', ...)"
                    % ps["text"]))
        return out
    sdc = sdc_of_cfg(cfg)
    col = "B" if sdc in ("A", "B") else sdc
    for key in ps["rows"]:
        row = SYSTEMS[key]
        lim = row["lim"].get(col)
        if lim is None or not hn:
            continue
        if lim > 0 and hn <= lim + 0.05:
            continue
        # exceeded or NP: try the 12.2.5.4 increase, then the single-story exceptions
        if row.get("inc") and lim > 0 and col in ("D", "E", "F"):
            inc_lim = 240.0 if col in ("D", "E") else 160.0
            ok, why = increase_12_2_5_4(cfg, pkg_height_evidence)
            if ok and hn <= inc_lim + 0.05:
                out.append(("WARN", "h_n=%.1f ft > %.0f ft (Table 12.2-1, %s, SDC %s) -- permitted up to %.0f ft by "
                                    "ASCE 7-22 12.2.5.4: %s. Keep the evidence current with the final design."
                            % (hn, lim, row["name"], sdc, inc_lim, why)))
                continue
            out.append(("ERROR", "h_n=%.1f ft exceeds the Table 12.2-1 limit of %.0f ft for %s in SDC %s%s -- %s; "
                                 "otherwise re-select the system (dual system / 12.2.1.1)"
                        % (hn, lim, row["name"], sdc,
                           (" (12.2.5.4 allows up to %.0f ft)" % inc_lim) if hn <= inc_lim + 0.05 else "",
                           why if hn <= inc_lim + 0.05 else "beyond even the 12.2.5.4 increase")))
            continue
        elim, why = _height_exception(cfg, key, col, hn, nst)
        if elim is not None and hn <= elim + 0.05:
            out.append(("WARN", "%s in SDC %s: %s -- permitted to h_n=%s ft (h_n=%.1f ft). Keep the load "
                                "limits true in the final design." % (row["name"], sdc, why,
                                "unlimited" if elim == float("inf") else "%.0f" % elim, hn)))
            continue
        if lim == NP:
            out.append(("ERROR", "%s is NOT PERMITTED in SDC %s (Table 12.2-1%s)%s -- re-select the system"
                        % (row["name"], sdc,
                           {"imf": "; 12.2.5.7", "omf": "; 12.2.5.6", "ocbf_j": " footnote j"}.get(row.get("exc"), ""),
                           ("; " + why) if why else "")))
        else:
            out.append(("ERROR", "h_n=%.1f ft exceeds the Table 12.2-1 limit of %.0f ft for %s in SDC %s%s -- "
                                 "re-select the system (or a dual system / 12.2.1.1)"
                        % (hn, lim, row["name"], sdc,
                           {"imf": " (12.2.5.7.1(b): 35 ft)", "ocbf_j": ""}.get(row.get("exc"), "")
                           + (("; " + why) if why else ""))))
    return out


SEIS_REASON_KEY = "seis_factor_basis"


def factor_findings(cfg):
    """(severity, message) findings: cfg['seis'] R, Cd, Om0, Ct, x against the declared system's
    Table 12.2-1 / Table 12.8-2 values. A mismatch is an ERROR; a mismatch that is CONSERVATIVE
    (smaller R, larger Cd/Om0, smaller Ta) is downgraded to a WARN when cfg['seis_factor_basis']
    records why. Om0 may be reduced by 0.5 (not below 2.0) for a flexible diaphragm (Table 12.2-1
    footnote). For a MIXED system the engine applies cfg['seis'] to BOTH directions, so it must be
    the conservative envelope of the components unless per-direction cfg['seis_X'] / cfg['seis_Y']
    blocks carry each direction's own factors."""
    out = []
    ps = parse_system(cfg)
    s = cfg.get("seis") or {}
    if ps["kind"] not in ("single", "dual", "mixed") or not s:
        return out
    rows = [SYSTEMS[k] for k in ps["rows"]]
    hn, _ = _hn_ft(cfg)
    flex = str(cfg.get("diaphragm", "rigid")).lower() == "flexible"
    reason = str(cfg.get(SEIS_REASON_KEY) or "").strip()
    per_dir = [d for d in ("seis_X", "seis_Y") if isinstance(cfg.get(d), dict)]

    def _exp(rws):            # conservative envelope of one or more rows
        return dict(R=min(r["R"] for r in rws), Cd=max(r["Cd"] for r in rws), Om0=max(r["Om0"] for r in rws),
                    Om0_min=max(max(r["Om0"] - 0.5, 2.0) if flex else r["Om0"] for r in rws),
                    Ta=min(r["Ct_x"][0] * hn ** r["Ct_x"][1] for r in rws) if hn else None,
                    Ct_x=[r["Ct_x"] for r in rws], name=" / ".join(r["name"] for r in rws))

    def _cmp(sv, exp, label, mixed_note=""):
        f = []
        def flag(cons, msg):
            if cons and len(reason) >= 15:
                f.append(("WARN", msg + " -- conservative; kept per cfg['%s']: %s" % (SEIS_REASON_KEY, reason[:120])))
            else:
                f.append(("ERROR", msg + (" -- conservative, but set the Table value or record why in "
                                          "cfg['%s']" % SEIS_REASON_KEY if cons else " -- UNCONSERVATIVE")
                          + mixed_note))
        R = _num_or_none(sv.get("R")); Cd = _num_or_none(sv.get("Cd")); Om0 = _num_or_none(sv.get("Om0"))
        if R is not None and abs(R - exp["R"]) > 1e-6:
            flag(R < exp["R"], "%s R=%g but Table 12.2-1 gives R=%g for %s" % (label, R, exp["R"], exp["name"]))
        if Cd is not None and abs(Cd - exp["Cd"]) > 1e-6:
            flag(Cd > exp["Cd"], "%s Cd=%g but Table 12.2-1 gives Cd=%g for %s" % (label, Cd, exp["Cd"], exp["name"]))
        if Om0 is None:
            f.append(("WARN", "%s Om0 not declared -- set Om0=%g (Table 12.2-1, %s) explicitly; the engine "
                              "default is keyed to R, not to the system" % (label, exp["Om0"], exp["name"])))
        elif Om0 < exp["Om0_min"] - 1e-6:
            flag(False, "%s Om0=%g but Table 12.2-1 gives Om0=%g for %s%s"
                 % (label, Om0, exp["Om0"], exp["name"], " (>= %g with the flexible-diaphragm reduction)"
                    % exp["Om0_min"] if flex else ""))
        elif Om0 > exp["Om0"] + 1e-6:
            flag(True, "%s Om0=%g but Table 12.2-1 gives Om0=%g for %s" % (label, Om0, exp["Om0"], exp["name"]))
        Ct = _num_or_none(sv.get("Ct")); x = _num_or_none(sv.get("x"))
        if Ct is not None and x is not None and exp["Ta"] and (Ct, x) not in [tuple(c) for c in exp["Ct_x"]]:
            Ta = Ct * hn ** x
            mfrow = all(c == (0.028, 0.8) for c in exp["Ct_x"])
            if mfrow and (Ct, x) == (0.02, 0.75):
                pass        # Table 12.8-2 'all other' row: permitted for MF adjoined by rigid components (lower Ta)
            else:
                flag(Ta <= exp["Ta"] + 1e-9,
                     "%s Ct=%g, x=%g (Ta=%.3f s) but Table 12.8-2 gives Ct, x = %s for %s (Ta=%.3f s)"
                     % (label, Ct, x, Ta, " or ".join("%g, %g" % c for c in exp["Ct_x"]), exp["name"], exp["Ta"]))
        return f

    if ps["kind"] in ("single", "dual"):
        out += _cmp(s, _exp(rows), "cfg['seis']")
        return out
    # mixed: per-direction blocks (if any) must each match one component; cfg['seis'] is what the
    # engine applies to BOTH directions -> it must be the envelope unless per-direction blocks exist
    for d in per_dir:
        sv = cfg[d]
        best = None
        for r in rows:
            f = _cmp(sv, _exp([r]), "cfg['%s']" % d)
            if not any(sev == "ERROR" for sev, _ in f):
                best = f; break
        if best is None:
            out.append(("ERROR", "cfg['%s'] (R=%s, Cd=%s, Om0=%s) matches none of the mixed system's rows (%s)"
                        % (d, sv.get("R"), sv.get("Cd"), sv.get("Om0"), ", ".join(r["name"] for r in rows))))
        else:
            out += best
    env = _exp(rows)
    if per_dir:
        f = [m for m in _cmp(s, env, "cfg['seis']") if m[0] == "ERROR"]
        if f:
            out.append(("WARN", "mixed system: the engine applies cfg['seis'] (R=%s, Cd=%s, Om0=%s) to BOTH "
                                "directions; it is not the conservative envelope (R=%g, Cd=%g, Om0=%g) -- confirm "
                                "the %s per-direction demands/drifts were injected from %s (12.2.2)"
                        % (s.get("R"), s.get("Cd"), s.get("Om0"), env["R"], env["Cd"], env["Om0"],
                           "/".join(per_dir), "/".join(per_dir))))
    else:
        out += _cmp(s, env, "cfg['seis']",
                    " (mixed system: the engine applies ONE factor set to BOTH directions, so it must be the "
                    "envelope -- smallest R, largest Cd/Om0 -- or declare cfg['seis_X'] / cfg['seis_Y'])")
    return out


def rho_findings(cfg):
    """Redundancy factor declarations (ASCE 7-22 12.3.4). The engine default is rho = 1.3 whatever
    the SDC; 12.3.4.1 sets rho = 1.0 in SDC B and C, and 12.3.4.2 allows 1.0 in SDC D-F only where
    its conditions are met."""
    out = []
    sdc = sdc_of_cfg(cfg)
    rho = cfg.get("rho")
    if rho is None:
        if sdc in ("A", "B", "C"):
            out.append(("WARN", "cfg['rho'] not declared: the engine uses rho=1.0 per ASCE 7-22 12.3.4.1 (SDC %s) "
                                "-- declare cfg['rho'] to record it" % sdc))
        else:
            out.append(("WARN", "cfg['rho'] not declared: the engine uses rho=1.3 (SDC %s). rho=1.0 is permitted "
                                "only where 12.3.4.2 (a) or (b) is met -- declare cfg['rho'] explicitly" % sdc))
        return out
    r = _num_or_none(rho)
    if r is None or not (abs(r - 1.0) < 1e-6 or abs(r - 1.3) < 1e-6):
        out.append(("ERROR", "cfg['rho']=%r -- ASCE 7-22 12.3.4 gives rho = 1.0 or 1.3 only" % (rho,)))
    elif abs(r - 1.0) < 1e-6 and sdc in ("D", "E", "F") and not str(cfg.get("rho_basis") or "").strip():
        out.append(("WARN", "cfg['rho']=1.0 in SDC %s: valid only where 12.3.4.2 (a) or (b) is met -- record "
                            "which condition in cfg['rho_basis'] (and in the report)" % sdc))
    return out


def sdc_findings(cfg):
    """A declared cfg['sdc'] that disagrees with Tables 11.6-1/-2 (11.6)."""
    out = []
    a = sdc_assessment(cfg)
    s = cfg.get("seis") or {}
    if not s:
        return out
    basis = "SDS=%s, SD1=%s, S1=%s, Risk Category %s" % (s.get("SDS"), s.get("SD1"), s.get("S1", 0), a["rc"])
    if a["declared"] and a["declared"] not in ("A", "B", "C", "D", "E", "F"):
        out.append(("ERROR", "cfg['sdc']=%r is not an SDC letter A-F" % cfg.get("sdc")))
    elif a["declared"] and a["declared"] < a["required"]:
        out.append(("ERROR", "cfg['sdc']='%s' but %s gives SDC %s (ASCE 7-22 11.6, Tables 11.6-1/-2%s)%s -- the "
                             "engine uses SDC %s; correct cfg['sdc'] (a lower SDC would drop rho, the drift/rho "
                             "limit and the Table 12.2-1 height limits)"
                    % (a["declared"], basis, a["required"],
                       "; 11.6 exception -> Table 11.6-1 alone" if a["exception_ok"] else "",
                       ("; 11.6 exception not shown: " + "; ".join(a["exception_why"])) if a["exception_why"]
                       and not a["exception_ok"] else "", a["required"])))
    elif a["declared"] and a["declared"] > a["required"]:
        out.append(("WARN", "cfg['sdc']='%s' is MORE severe than the SDC %s from %s -- kept (conservative)"
                    % (a["declared"], a["required"], basis)))
    if a["exception_ok"] and a["table1_only"] < a["derived"]:
        out.append(("WARN", "11.6 exception applied: SDC %s from Table 11.6-1 alone (Table 11.6-2 would give %s): %s"
                    % (a["table1_only"], a["derived"], "; ".join(a["exception_why"]))))
    return out


def check(cfg):
    out = []
    say = lambda sev, msg: out.append((sev, msg))
    if not isinstance(cfg, dict):
        return [("ERROR", "cfg is not a dict")]
    # ---- units ----
    H = [float(h) for h in (cfg.get("heights") or []) if isinstance(h, (int, float))]
    if not H:
        say("ERROR", "cfg['heights'] missing/empty")
    _dex0 = set(int(k) for k in (cfg.get("drift_exempt_stories") or {}))
    _small = [(i, h) for i, h in enumerate(H, start=1) if h < 72]
    _undeclared = [(i, h) for i, h in _small if i not in _dex0]
    if _undeclared:
        say("ERROR", "story height < 6 ft found (%s in, story %s): heights look like FEET -- engine "
                     "units are INCHES (13 ft story = 156). If a small inter-level offset is "
                     "INTENTIONAL (split-level), declare it in cfg['drift_exempt_stories'] with a reason."
                     % (_undeclared[0][1], _undeclared[0][0]))
    elif _small:
        say("WARN", "sub-6-ft story height(s) at %s are DECLARED inter-diaphragm offsets "
                    "(drift_exempt_stories) -- OK; design the step transfer detail"
                    % [i for i, _ in _small])
    for k in ("SX", "SY"):
        v = cfg.get(k)
        if isinstance(v, (int, float)) and 0 < v < 60:
            say("ERROR", "%s=%g in is < 5 ft: bay spacing looks like FEET (engine uses inches)" % (k, v))
    # ---- seismic block ----
    s = cfg.get("seis") or {}
    for k in ("SDS", "SD1", "R", "Cd", "Ie"):
        if k not in s:
            say("ERROR", "cfg['seis'] missing '%s'" % k)
    sysname = str(cfg.get("system") or "").lower()
    if not sysname:
        say("ERROR", "cfg['system'] not declared (consistency.check will FAIL) -- set the exact SFRS")
    R = float(s.get("R") or 0)
    # ---- SDC (11.6), Table 12.2-1 height / system limits, R/Cd/Om0 (12.2-1), Ct/x (12.8-2), rho ----
    for sev, msg in sdc_findings(cfg) + system_limit_findings(cfg) + factor_findings(cfg) + rho_findings(cfg):
        say(sev, msg)
    # ---- Risk-Category drift limit ----
    Ie = float(s.get("Ie", 1.0) or 1.0)
    dl = float(cfg.get("drift_limit", 0.020) or 0.020)
    relief = drift_relief(cfg)
    if relief is not None:
        for sev, msg in relief_findings(cfg):
            say(sev, msg)
    if Ie >= 1.5 and dl > 0.0101:
        say("ERROR", "Ie=%.2f (RC IV) but drift_limit=%.3f -- Table 12.12-1 requires 0.010" % (Ie, dl))
    elif 1.2 <= Ie < 1.5 and dl > 0.0151 and not relief_active(cfg):
        say("ERROR", "Ie=%.2f (RC III) but drift_limit=%.3f -- Table 12.12-1 requires 0.015" % (Ie, dl))
    # moment-frame-only SFRS in SDC D-F: allowable drift is Delta_a/rho (ASCE 7-22 sec.12.12.1.1);
    # the engine applies the division in its drift gates -- flag it so the reduced target is expected
    _mf_only = mf_only_system(cfg)
    if _mf_only and sdc_of_cfg(cfg) in ("D", "E", "F"):
        _rho = float(cfg.get("rho", 1.3 if sdc_of_cfg(cfg) in ("D", "E", "F") else 1.0) or 1.3)
        say("WARN", "moment-frame-only SFRS in SDC %s: allowable story drift is drift_limit/rho = "
                    "%.4f/%.2f = %.4f (12.12.1.1) -- the engine drift gates apply this division"
                    % (sdc_of_cfg(cfg), dl, _rho, dl / _rho))
    # ---- analyses vs R=3 ----
    if R and R <= 3.0 and "341" in str(cfg.get("system", "")):
        say("WARN", "R<=3: AISC 341 does NOT apply -- design to AISC 360 only and prove wind-vs-seismic")
    # ---- model declaration ----
    md = cfg.get("model")
    if not (isinstance(md, dict) and {"bases", "joints", "gravity"} <= set(md)):
        say("ERROR", "cfg['model'] = {'bases','joints','gravity'} declaration missing (HARD GATE "
                     "model_declared will FAIL)")
    # ---- diaphragm / split-level declarations (F-1) ----
    dia = cfg.get("diaphragm", "rigid")
    if dia not in ("rigid", "flexible", "semi-rigid"):
        say("ERROR", "cfg['diaphragm'] must be 'rigid' | 'flexible' | 'semi-rigid' (got %r)" % (dia,))
    dex = cfg.get("drift_exempt_stories") or {}
    if dex:
        for k, why in dict(dex).items():
            if not str(why).strip():
                say("ERROR", "drift_exempt_stories[%s] has no reason -- each exemption must carry a "
                             "one-line justification (e.g. 'split-level inter-diaphragm offset, step "
                             "ties designed')" % k)
        say("WARN", "drift gate will SKIP stories %s (declared inter-diaphragm offsets) -- their "
                    "racking must be addressed as a designed detail in calc_package"
                    % sorted(dict(dex).keys()))
    # ---- load sanity ----
    for k in ("D_floor", "L_floor"):
        v = cfg.get(k)
        if isinstance(v, (int, float)) and v > 400:
            say("WARN", "%s=%g psf is unusually high -- confirm units (psf)" % (k, v))
    # ---- 12.7.2 item 2: partition weight in W where a partition allowance (4.3.2) is carried ----
    _llr = cfg.get("live_load_reduction")
    _part = (isinstance(_llr, dict) and float(_llr.get("unreduced_psf", _llr.get("partition_psf", 0)) or 0) > 0) \
        or float(cfg.get("partition_psf", 0) or 0) > 0
    if _part and not cfg.get("seismic_mass_only") and not cfg.get("partitions_in_dead"):
        say("WARN", "a partition live-load allowance is declared (ASCE 7-22 4.3.2) but no partition weight is in "
                    "the seismic weight: 12.7.2 item 2 requires the actual partition weight or >= 10 psf of floor "
                    "area in W -- set cfg['seismic_mass_only'] = {level: 10.0} (or cfg['partitions_in_dead'] = True "
                    "where D already includes them)")
    _Lf = cfg.get("L_floor")
    if isinstance(_Lf, (int, float)) and _Lf >= 125 and not cfg.get("storage") \
            and not cfg.get("storage_levels"):
        say("WARN", "floor live suggests storage occupancy -- 12.7.2 requires >=25%% of storage live "
                    "in W; set cfg['storage'] (all floors) or cfg['storage_levels'] (L_floor=%g psf)" % _Lf)
    # ---- Tier A/B consultancy guards (EDGE_CASE_SWEEP) ----
    arch = (str(cfg.get("arch", "")) + " " + str(cfg.get("system", ""))).lower()
    if any(k in arch for k in ("gable", "pitch", "slope", "monoslope", "sloped")):
        say("WARN", "A2 sloped roof keywords in cfg: the engine models FLAT levels only -- model at the "
                    "mean roof height, then HAND-CHECK unbalanced/sliding snow (ASCE 7-22 7.6/7.9), eave "
                    "drift, and rafter thrust; state the idealization")
    if any(k in arch for k in ("platform", "rack", "vessel", "tank", "bin", "silo")):
        say("WARN", "B2 nonbuilding-structure keywords in cfg: Ch. 15 (NOT Ch. 12) R-values and "
                    "detailing apply -- confirm the system row in Table 15.4-1/2 before using any "
                    "building R")
    try:
        LX = float(cfg.get("NX", 0)) * float(cfg.get("SX", 0)) / 12.0
        LY = float(cfg.get("NY", 0)) * float(cfg.get("SY", 0)) / 12.0
        if max(LX, LY) > 300.0:
            say("WARN", "B5 plan dimension %.0f ft > ~300 ft jointless: record the expansion/thermal "
                        "decision in calc_package (joint located, or thermal force statement)" % max(LX, LY))
    except Exception:
        pass
    for k, v in dict(cfg.get("extra_mass_floors") or {}).items():
        try:
            if abs(float(v)) > float(cfg.get("D_floor", 100) or 100):
                say("WARN", "extra_mass_floors[%s]=%g psf exceeds |D_floor| -- confirm the sign/magnitude" % (k, v))
        except Exception:
            pass
    return out



def render(res):
    if not res:
        return "[preflight] R22: no findings -- cfg passes the pre-analysis checks"
    lines = ["[preflight] R22 cfg linter: %d finding(s) -- fix ERRORs BEFORE trusting any analysis:" % len(res)]
    for sev, msg in res:
        lines.append("  [%s] %s" % (sev, msg))
    return "\n".join(lines)

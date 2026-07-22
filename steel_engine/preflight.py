"""preflight.py -- R22 pre-analysis cfg linter. Cheap, engine-free checks run BEFORE the first
OpenSees solve so a mis-declared cfg is caught in seconds, not after a full pipeline run.
Returns a list of (severity, message); severity in {"ERROR","WARN"}. Non-blocking by design --
pipeline.design_and_report prints the findings and puts them in its return dict."""

# ASCE 7-22 Table 12.2-1 anchor values for the common steel SFRS (R, Cd, Om0, SDC-D height ft)
_SYS = {
    "smf":  (8.0, 5.5, 3.0, None), "imf": (4.5, 4.0, 3.0, 35.0),
    "ebf":  (8.0, 4.0, 2.0, 160.0), "brbf": (8.0, 5.0, 2.5, 160.0),
    "scbf": (6.0, 5.0, 2.0, 160.0), "ocbf": (3.25, 3.25, 2.0, 35.0),
    "spsw": (7.0, 6.0, 2.0, 160.0), "dual": (None, None, None, None),
    "c-psw": (6.5, 5.5, 2.5, 160.0), "stmf": (7.0, 5.5, 3.0, 160.0),
}


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
    for key, (r0, cd0, om0, hlim) in _SYS.items():
        if key in sysname and key != "dual" and r0 is not None:
            if R and abs(R - r0) > 0.51 and "dual" not in sysname:
                say("WARN", "cfg['seis'] R=%.2f but system '%s' is normally R=%.2f (Table 12.2-1) -- "
                            "confirm the brief" % (R, cfg.get("system"), r0))
            if H and hlim and float(s.get("SDS", 0) or 0) >= 0.50:
                hn = sum(H) / 12.0
                if hn > hlim + 0.5:
                    say("WARN", "h_n=%.0f ft exceeds the ~%.0f ft SDC-D Table 12.2-1 limit for '%s' -- "
                                "FLAG and resolve (12.2.5.4 increase / dual system / 12.2.1.1)"
                                % (hn, hlim, key.upper()))
            break
    # ---- Risk-Category drift limit ----
    Ie = float(s.get("Ie", 1.0) or 1.0)
    dl = float(cfg.get("drift_limit", 0.020) or 0.020)
    if Ie >= 1.5 and dl > 0.0101:
        say("ERROR", "Ie=%.2f (RC IV) but drift_limit=%.3f -- Table 12.12-1 requires 0.010" % (Ie, dl))
    elif 1.2 <= Ie < 1.5 and dl > 0.0151:
        say("ERROR", "Ie=%.2f (RC III) but drift_limit=%.3f -- Table 12.12-1 requires 0.015" % (Ie, dl))
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

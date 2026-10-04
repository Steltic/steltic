"""
consistency.py -- numerical SELF-CONSISTENCY check for a finished design.

Loads <root>/<name>/design/calc_package.json (or a passed-in dict) and flags INTERNAL
inconsistencies before the report is finalised -- the class of bug where two scripts compute the
same quantity differently and the two are never reconciled (e.g. a 1/4" plate at D/C 1.00 in one
script vs a 9/16" plate at D/C 0.77 in another, both left in the package).

For every member AND connection it checks:
  * completeness -- a limit state, a D/C and a cited clause exist (top-level OR inside a `checks`
                    list -- connections legitimately carry these per limit-state);
  * bound        -- no D/C (anywhere) exceeds 1.0;
  * governing    -- the headline D/C equals the MAX of the per-limit-state check D/Cs (this is what
                    catches a headline 0.77 sitting on top of a check that is really 1.00);
  * recompute    -- where a demand and a capacity are both recoverable (numeric fields, or an
                    "Ru=.. -> phiRn=.." style demand string), D/C == demand / capacity to tolerance;
  * one-value    -- a labelled+unit quantity (plate thickness, D/C, ...) that appears more than once
                    in the entry's text must carry ONE value; conflicting numbers are flagged.

check(name) prints a PASS/FAIL summary and returns the list of issue strings ([] == consistent).
Never raises -- a malformed package is itself reported as an issue.
"""
import os, json, re

TOL = 0.04   # 4% relative tolerance on recomputed ratios


def _here_repo():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # repo root (engine/..)


def _num(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        m = re.search(r"-?\d+(?:\.\d+)?", x.replace(",", ""))
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                return None
    return None


def _dc_num(x):
    """Strict D/C reader. Accepts a number, or a string that STARTS with a number ('0.93',
    '0.93 (governs)'). A qualitative status like 'OK (a=.625b_f ...)', 'PASS', 'N/A' returns None
    (a pass/status, NOT a ratio) -- so dimensional/geometry checks are never mis-read as a D/C."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        m = re.match(r"\s*(-?\d*\.?\d+)", x)   # anchored at the START, not search-anywhere
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
    return None


ABS_TOL = 0.006   # absolute floor: a D/C reported to 2 decimals carries up to +/-0.005 rounding


def _close(a, b, tol=TOL):
    """Relative tolerance TOL, with an ABSOLUTE floor ABS_TOL so tiny ratios (D/C 0.01 reported
    vs 0.014 recomputed) are not flagged as irreconcilable (HR-33)."""
    if a is None or b is None:
        return True
    return abs(a - b) <= max(tol * max(abs(a), abs(b)), ABS_TOL)


# ---- explicit demand / capacity schema (HR-33) -------------------------------------------------
# A check's demand and capacity are matched by KEY SEMANTICS, never by substring or dict order:
# the key is normalised (lower-case, non-alphanumerics dropped, a trailing unit suffix removed) and
# must EQUAL one of the names below. Pairs are matched by quantity: Pu<->phiPn, Mu<->phiMn,
# Vu<->phiVn, Tu<->phiTn, Ru<->phiRn, demand<->capacity. 'phi' alone (a resistance factor), 'strut',
# 'truss', 'input', ... can no longer be read as a demand or a capacity.
_UNIT_SUFFIX = ("kipft", "kipin", "kips", "kip", "kft", "kin", "ksi", "in2", "in3", "in4", "in", "ft", "k")
_DEM_KEYS = {"pu": "P", "pr": "P", "mu": "Mx", "mr": "Mx", "mux": "Mx", "mrx": "Mx", "muy": "My", "mry": "My",
             "vu": "V", "vr": "V", "tu": "T", "ru": "R", "demand": "*", "required": "*",
             "requiredstrength": "*", "demandratio": "ratio"}
_CAP_KEYS = {"phipn": "P", "phipc": "P", "pc": "P", "phimn": "Mx", "phimnx": "Mx", "mc": "Mx", "mcx": "Mx",
             "phimny": "My", "mcy": "My", "phivn": "V", "vc": "V", "phitn": "T", "phirn": "R", "rc": "R",
             "capacity": "*", "available": "*", "availablestrength": "*", "designstrength": "*",
             "capacitylimit": "ratio", "ratiolimit": "ratio"}
_INTERACTION = re.compile(r"\bh1|h2-1|interaction|combined|\+|beam.?column", re.I)


def _norm_key(k):
    s = re.sub(r"[^a-z0-9]", "", str(k).lower().replace("φ", "phi"))
    for u in _UNIT_SUFFIX:
        if len(s) > len(u) + 1 and s.endswith(u) and (s[:-len(u)] in _DEM_KEYS or s[:-len(u)] in _CAP_KEYS):
            return s[:-len(u)]
    return s


def _demand_capacity_pairs(d):
    """[(qty, demand, capacity)] recoverable from a check dict by key semantics (see above)."""
    if not isinstance(d, dict):
        return []
    dem, cap = {}, {}
    for k, v in d.items():
        n = _num(v) if not isinstance(v, (dict, list)) else None
        if n is None:
            continue
        nk = _norm_key(k)
        if nk in _DEM_KEYS:
            dem.setdefault(_DEM_KEYS[nk], n)
        elif nk in _CAP_KEYS:
            cap.setdefault(_CAP_KEYS[nk], n)
    if "ratio" in dem and "ratio" in cap:                         # explicit demand_ratio / capacity_limit
        return [("ratio", dem["ratio"], cap["ratio"])]
    pairs = [(q, dem[q], cap[q]) for q in sorted(dem) if q in cap and q not in ("*", "ratio")]
    if "*" in dem and "*" in cap:
        pairs.append(("*", dem["*"], cap["*"]))
    elif not pairs and len(dem) == 1 and len(cap) == 1 and "ratio" not in dem and "ratio" not in cap:
        pairs.append(("*", list(dem.values())[0], list(cap.values())[0]))   # e.g. Ru with 'capacity'
    return pairs


def _find(d, *subs):
    """Kept for backward compatibility: the value of the first key whose NORMALISED name equals one
    of `subs` (exact semantic match, independent of dict order); None otherwise."""
    if not isinstance(d, dict):
        return None
    want = set(re.sub(r"[^a-z0-9]", "", s.lower()) for s in subs)
    for k, v in d.items():
        if _norm_key(k) in want:
            n = _num(v)
            if n is not None:
                return n
    return None


def _demand_capacity_from_string(s):
    """Parse a demand string like 'Ru=788 kip; phiRn=601 (web) -> 1023 kip (with 9/16" doubler)'
    into (demand, capacity). demand = the Ru/required value; capacity = the FINAL (largest, post-
    reinforcement) phiRn / value after an arrow. Returns (dem, cap) or (None, None)."""
    if not isinstance(s, str):
        return None, None
    low = s.lower()
    dem = None
    m = re.search(r"(?<![a-z])(?:ru|required|demand|mu|pu|vu)\s*[=:]?\s*(-?\d+(?:\.\d+)?)", low)
    if m:
        dem = float(m.group(1))
    cap = None
    # prefer a value after an arrow (final, reinforced capacity), else after phiRn/phi Rn
    arrow = re.findall(r"(?:->|→)\s*(-?\d+(?:\.\d+)?)", low)
    if arrow:
        cap = float(arrow[-1])
    else:
        m = re.search(r"(?<![a-z])(?:phirn|φrn|phi\s*rn|capacity)\s*[=:]?\s*(-?\d+(?:\.\d+)?)", low)
        if m:
            cap = float(m.group(1))
    return dem, cap


_LABEL_NUM = re.compile(r"([A-Za-z][A-Za-z _/\.-]{1,26}?)\s*[=:]?\s*(-?\d+(?:/\d+)?(?:\.\d+)?)\s*"
                        r"(in\.?|inch|ksi|kips?|k-?ft|k-?in|mm)\b", re.I)


def _gather_text(obj, acc):
    if isinstance(obj, str):
        acc.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _gather_text(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _gather_text(v, acc)


def _frac(tok):
    if "/" in tok:
        try:
            a, b = tok.split("/"); return float(a) / float(b)
        except Exception:
            return None
    return _num(tok)


def _one_value_issues(kind, cid, entry):
    texts = []
    _gather_text(entry, texts)
    seen = {}
    for t in texts:
        for m in _LABEL_NUM.finditer(t):
            label = re.sub(r"\s+", " ", m.group(1).strip().lower())
            label = re.sub(r"^(the|a|an|use|using|with|of|for|one|each)\s+", "", label)
            if len(label) < 3:
                continue
            unit = m.group(3).lower().replace(".", "").rstrip("s")
            val = _frac(m.group(2))
            if val is None:
                continue
            key = (label, unit)
            seen.setdefault(key, [])
            if all(abs(val - v) / max(abs(val), abs(v), 1e-9) > 0.02 for v in seen[key]):
                seen[key].append(val)
    out = []
    for (label, unit), vals in seen.items():
        if len(vals) > 1:
            out.append(f"[{kind} {cid}] quantity '{label}' ({unit}) appears as "
                       f"{', '.join(str(v) for v in vals)} -- one value only; reconcile")
    return out


def _resize_hint(sec):
    """Next-heavier section in the same W-depth family (hardening #6): a concrete fix for a
    D/C > 1.0 member so a weak agent iterates instead of shipping NG."""
    try:
        import re as _re, csv as _csv, os as _os
        m = _re.match(r"(W\d+)X(\d+(?:\.\d+)?)", str(sec or "").upper())
        if not m:
            return ""
        fam, wt = m.group(1), float(m.group(2))
        path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "aisc_shapes.csv")
        wts = sorted(float(r["AISC_Manual_Label"].upper().split("X")[1])
                     for r in _csv.DictReader(open(path))
                     if r["AISC_Manual_Label"].upper().startswith(fam + "X"))
        nxt = [w for w in wts if w > wt + 0.1]
        return (" -> try %sX%g (next size up, same depth family)" % (fam, int(nxt[0]) if float(nxt[0]).is_integer() else nxt[0])) if nxt else \
               (" -> %s is the heaviest %s: use a BUILT-UP section and FLAG it" % (sec, fam))
    except Exception:
        return ""


def _entry_issues(kind, entry):
    out = []
    cid = entry.get("id", "?")
    checks = entry.get("checks") if isinstance(entry.get("checks"), list) else []

    # collect per-limit-state D/Cs (recomputing from numeric demand/capacity where available)
    check_dcs = []
    for c in checks:
        if not isinstance(c, dict):
            continue
        cls = c.get("limit_state", c.get("name", "check"))
        cdc = _dc_num(c.get("DC"))
        pairs = [(q, d_, c_) for q, d_, c_ in _demand_capacity_pairs(c) if c_ not in (None, 0.0)]
        inter = bool(_INTERACTION.search(str(cls))) and not (pairs and pairs[0][0] == "ratio")
        if len(pairs) == 1 and not inter:         # one demand/capacity pair -> D/C must equal it
            _q, dem, cap = pairs[0]
            rec = dem / cap
            if cdc is not None and not _close(rec, cdc):
                out.append(f"[{kind} {cid}] check '{cls}': D/C {cdc:.3f} != demand/capacity "
                           f"{dem:.3g}/{cap:.3g} = {rec:.3f} -- reconcile")
            elif cdc is None:
                cdc = rec
        elif pairs:                               # interaction (e.g. Pu/phiPn + Mu/phiMn): D/C >= each term
            worst = max(pairs, key=lambda t: t[1] / t[2])
            rec = worst[1] / worst[2]
            if cdc is not None and cdc < rec and not _close(rec, cdc):
                out.append(f"[{kind} {cid}] check '{cls}': D/C {cdc:.3f} < its own {worst[0]} term "
                           f"{worst[1]:.3g}/{worst[2]:.3g} = {rec:.3f} -- reconcile")
            elif cdc is None:
                cdc = rec
        if cdc is not None:
            check_dcs.append((cls, cdc))

    top_dc = _dc_num(entry.get("DC"))
    ls_present = bool(entry.get("limit_state")) or any(
        isinstance(c, dict) and c.get("limit_state") for c in checks)
    all_dcs = ([top_dc] if top_dc is not None else []) + [d for _, d in check_dcs]

    if not ls_present:
        out.append(f"[{kind} {cid}] no limit_state given (top-level or in a check)")
    if not entry.get("cited") and not any(isinstance(c, dict) and c.get("cited") for c in checks):
        out.append(f"[{kind} {cid}] no cited AISC clause")
    if not all_dcs:
        out.append(f"[{kind} {cid}] no D/C reported (top-level or in a check)")
    else:
        worst = max(all_dcs)
        if worst > 1.0 + 1e-9:
            if entry.get("waived"):
                pass   # explicitly waived with an engineering justification (completion-gate convention) --
                       # e.g. an EXISTING member in a retrofit/addition job carried as a retrofit-scope item
            else:
                _sec_ = (entry.get("inputs") or {}).get("section") or entry.get("section")
                out.append(f"[{kind} {cid}] worst D/C = {worst:.3f} > 1.0 (NG -- resize/redesign)"
                           + _resize_hint(_sec_))
        if top_dc is not None and check_dcs:
            wcls, wd = max(check_dcs, key=lambda t: t[1])
            if not _close(wd, top_dc):
                out.append(f"[{kind} {cid}] headline D/C {top_dc:.3f} != worst limit-state D/C "
                           f"{wd:.3f} ('{wcls}') -- the two were never reconciled")

    # recompute from a free-text demand string (Ru=.. ; phiRn=.. -> CAP)
    dem, cap = _demand_capacity_from_string(entry.get("demand"))
    if dem is not None and cap not in (None, 0.0) and all_dcs:
        rec = dem / cap
        if not _close(rec, max(all_dcs)):
            out.append(f"[{kind} {cid}] demand/capacity {dem:.3g}/{cap:.3g} = {rec:.3f} "
                       f"!= reported D/C {max(all_dcs):.3f} -- reconcile")

    out += _one_value_issues(kind, cid, entry)
    return out


def _completeness_issues(pkg):
    """Reserved for commonly-omitted member CATEGORIES. The old infill/filler-beam heuristic
    was removed: secondary filler beams are needed only when the deck cannot span girder-to-
    girder, which the engineer decides per project, not a blanket requirement."""
    return []


def _isnum(x):
    try:
        float(x); return True
    except Exception:
        return False


# ---- pipeline-seeded text is NOT agent evidence (HR-33) ----------------------------------------
# design_pipeline.design() seeds calc_package.json with notes, design-basis templates, the
# framework irregularity screen and the collector slot text. Keyword guards must neither be
# TRIGGERED nor CLEARED by that text -- only by what the agent wrote.
_SEEDED_PREFIXES = ("SEEDED because", "Framework provides DEMANDS only", "FRAMEWORK-COMPUTED",
                    "AISC 360 Ch.J (J2 welds", "AISC 360 Ch.J gusset/weld", "AISC 360 J1.4 splice",
                    "collector / drag strut (SEEDED", "beam-to-column (shear; + moment if MF)",
                    "brace-to-gusset", "column splice / base plate")
_SEEDED_KEYS = ("framework_screen", "pipeline_seed")


def _is_seeded_text(s):
    return isinstance(s, str) and s.strip().startswith(_SEEDED_PREFIXES)


def _agent_texts(obj, acc=None):
    """All string values (and dict KEYS) the agent authored -- seeded strings / blocks skipped."""
    acc = [] if acc is None else acc
    if isinstance(obj, str):
        if not _is_seeded_text(obj):
            acc.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if str(k) in _SEEDED_KEYS or (str(k) == "note" and _is_seeded_text(v)):
                continue
            acc.append(str(k))
            _agent_texts(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _agent_texts(v, acc)
    return acc


def _agent_blob(pkg):
    return " ".join(_agent_texts(pkg)).lower() if isinstance(pkg, dict) else ""


_NUM_RESULT = re.compile(r"(?:=|≤|≥|<=|>=|<|>|~)\s*-?\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:kips?|k-?ft|kip-?ft|"
                         r"kip-?in|k-?in|in\.?|ksi|psf|plf|ft|%|rad|in\^?[234])(?![a-z])", re.I)
_CLAUSE_KEYS = ("cite", "cited", "clause", "basis", "method", "rule", "note", "notes", "reference", "ref")


def _has_numeric_result(obj, _key=""):
    """True when obj carries a COMPUTED number: a JSON number (not a bool) under a non-citation key,
    or a string with a value after '=' / a comparator or a number with an engineering unit.
    Clause numbers ('AISC 341-22 E3.4a', 'Eq. 12.8-2') do not count."""
    if isinstance(obj, bool) or obj is None:
        return False
    if isinstance(obj, (int, float)):
        return str(_key).lower() not in _CLAUSE_KEYS
    if isinstance(obj, str):
        return bool(_NUM_RESULT.search(obj))
    if isinstance(obj, dict):
        return any(_has_numeric_result(v, k) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_has_numeric_result(v, _key) for v in obj)
    return False


def _numeric_entries(tree, pattern, key_only=False):
    """Entries (key, value) anywhere in `tree` whose KEY -- or, for a dict value, its own text
    (unless key_only) -- matches `pattern` (regex, case-insensitive) AND whose value carries a
    computed number."""
    rx = re.compile(pattern, re.I)
    hits = []
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if str(k) in _SEEDED_KEYS:
                    continue
                txt = str(k) + (" " + " ".join(_agent_texts(v)) if isinstance(v, (dict, list))
                                and not key_only else "")
                if rx.search(txt) and _has_numeric_result(v, k):
                    hits.append((k, v))
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(tree)
    return hits


def _load_cfg(root, name):
    """Best-effort load of the building cfg dict from <root>/cfg.py so geometry/units can be sanity-checked."""
    p = os.path.join(root, "cfg.py")
    if not os.path.exists(p):
        return None
    try:
        # Compile the SOURCE directly (not via importlib) so a stale cached .pyc on a coarse-mtime jobs
        # mount can never shadow the just-edited cfg.py -- the class of bug where an edit looks ignored. (P13)
        import types as _types
        src = open(p, encoding="utf-8").read()
        m = _types.ModuleType("cfg_chk_" + name); m.__file__ = p
        exec(compile(src, p, "exec"), m.__dict__)
        return getattr(m, "cfg", None)
    except Exception:
        return None


def _geometry_issues(cfg):
    """Catch the classic feet-entered-as-inches slip: the engine uses INCHES everywhere, so a story height of
    '13' is 13 inches (~1 ft), not 13 ft. Flag implausible story heights and bay spacings."""
    out = []
    if not isinstance(cfg, dict):
        return out
    H = [float(h) for h in (cfg.get("heights") or []) if _isnum(h)]
    _dex = set(int(k) for k in (cfg.get("drift_exempt_stories") or {}))
    small = [h for i, h in enumerate(H, start=1) if h < 72 and i not in _dex]   # declared offsets OK
    if small:
        out.append("story heights look like FEET, not inches (%s) -- the engine uses INCHES: a 13 ft story is 156, "
                   "not 13. Multiply every height by 12 and re-run design_and_report." % ", ".join("%g" % h for h in small[:10]))
    tall = [h for h in H if h > 720]            # > 60 ft -- gross error / wrong units (legit tall atria are < ~50 ft)
    if tall:
        out.append("story height(s) over 60 ft (%s in) -- confirm the units are inches." % ", ".join("%g" % h for h in tall[:10]))
    for key, lab in (("SX", "X-bay"), ("SY", "Y-bay")):
        v = cfg.get(key)
        if _isnum(v) and 0 < float(v) < 60:     # < 5 ft bay -- almost certainly feet
            out.append("%s spacing %s=%g in is implausibly small -- the engine uses INCHES (a 20 ft bay is 240)." % (lab, key, float(v)))
    return out



def _design_basis_issues(cfg, name=None, pkg=None):
    """R1/R2/R8/R12/R14/R16 design-basis gates (SDC-aware so SDC B/C is not over-flagged)."""
    out=[]
    if not isinstance(cfg,dict): return out
    s=cfg.get("seis") or {}
    SDS=float(s.get("SDS",0) or 0); Ie=float(s.get("Ie",1.0) or 1.0); R=s.get("R")
    sdc_high = SDS>=0.50                       # proxy for SDC D/E/F (RC I-III); ELF permitted in B/C
    if not cfg.get("system"):
        out.append("declare cfg['system'] = the EXACT SFRS from the brief (e.g. 'SPSW','EBF','SMF','dual SMF+SCBF') -- "
                   "the report otherwise INFERS it from R, which is ambiguous (R=8 is EBF/BRBF/dual)")
    try:
        import engine3d as _E
        cfgr=_E.CFG.get(name) if name else None
        pir=_E.plan_irregularities(cfgr or cfg)
    except Exception:
        pir={"reentrant":False,"setback":False,"nonparallel":False}
    if not any(pir.values()) and isinstance(pkg,dict):
        # live screen needs the BUILT footprint; fall back to the flags the pipeline recorded
        _sp=(pkg.get("framework_screen") or {}).get("plan") or {}
        if any(_sp.get(k) for k in ("reentrant","setback","nonparallel")):
            pir={k: bool(_sp.get(k)) for k in ("reentrant","setback","nonparallel")}
    # NOTE: ASCE 7-22 deleted the 7-16 Table 12.6-1 analysis-procedure matrix; sec.12.6 permits ELF
    # for ALL structures, so an irregular SDC D+ building no longer FAILS for lacking 'RS'. The
    # MRSA-recommended advisory is rendered in the report (Ch.2 irregularity screen) instead.
    dl=float(cfg.get("drift_limit",0.020) or 0.020)
    _mfrho=(" (for a moment-frame-only SFRS in SDC D-F the allowable is further divided by rho, "
            "12.12.1.1 -- the engine drift gates apply this)")
    # ASCE 7-22 16.1.2: a Chapter 16 analysis relaxes the 12.12.1 limits for RC I-III (never IV).
    # preflight.relief_findings is the single source of the rules; its ERRORs are consistency flags.
    _relief_on=False
    try:
        from preflight import relief_findings as _rf, relief_active as _ra
        for _sev,_msg in _rf(cfg):
            if _sev=="ERROR": out.append("16.1.2 drift relief: "+_msg)
        _relief_on=_ra(cfg)
    except Exception:
        pass
    if Ie>=1.5 and dl>0.0101:
        out.append("Risk Category IV (Ie=%.2f): allowable story drift is 0.010 h_sx (Table 12.12-1) -- set cfg['drift_limit']=0.010"%Ie+_mfrho)
    elif Ie>=1.25 and dl>0.0151 and not _relief_on:
        out.append("Risk Category III (Ie=%.2f): allowable story drift is 0.015 h_sx (Table 12.12-1) -- set cfg['drift_limit']=0.015"%Ie+_mfrho)
    if R is not None and float(R)<=3.0:
        # clearable (ported from steltic_cfs, made NUMERIC): the note asks the agent to confirm which
        # hazard governs each direction -- a pkg['governing_hazard'] block with the wind and seismic
        # base shears (numbers) clears it; a bare 'wind governs' phrase does not.
        _gh = pkg.get("governing_hazard") if isinstance(pkg, dict) else None
        _gh_ok = isinstance(_gh, (dict, list)) and len(_numeric_entries(_gh, r"wind", True)) > 0 \
            and len(_numeric_entries(_gh, r"seism|(^|_)e(q|h)?(_|$)", True)) > 0
        if not _gh_ok:
            out.append("R=%.2f is a 'not specifically detailed for seismic' system -- AISC 341 does NOT apply (no SCWB / "
                       "capacity design); design members & connections to AISC 360 only, and CONFIRM whether wind or seismic "
                       "governs each direction: record calc_package['governing_hazard'] = {'X': {'wind_kip': .., "
                       "'seismic_kip': ..}, 'Y': {...}} (numbers) to clear this note"%float(R))
    H=[float(h) for h in (cfg.get("heights") or []) if _isnum(h)]
    if len(H)>=2 and sdc_high:
        hr=max(max(H[k]/H[k-1],H[k-1]/H[k]) for k in range(1,len(H)))
        _screened = isinstance(pkg,dict) and (
            isinstance((pkg.get("framework_screen") or {}).get("soft_story"), dict)
            or "classification" in str((pkg.get("capacity_design") or {}).get("vertical_irregularity","")))
        if hr>=1.3 and not _screened:
            out.append("story-height ratio %.2f suggests a SOFT/WEAK story -- COMPUTE the story stiffness AND strength "
                       "ratios and classify (7-22 Vert 1a/1b stiffness, 4a/4b strength); extreme soft story (1b) and "
                       "weak stories (4a/4b) are PROHIBITED in SDC E/F only (12.3.3.1)"%hr)
        elif hr>=1.3 and _screened:
            _ss=(pkg.get("framework_screen") or {}).get("soft_story") or {}
            try:
                from preflight import sdc_of_cfg as _sdcf; _sdc_ef=_sdcf(cfg) in ("E","F")
            except Exception:
                _sdc_ef=float(s.get("S1",0) or 0)>=0.75
            if "EXTREME" in str(_ss.get("classification","")) and _sdc_ef:
                out.append("framework screen classifies an EXTREME soft story (Type 1b) -- PROHIBITED in SDC E/F "
                           "(12.3.3.1): stiffen the story or document why the classification does not govern")
    _dia = str(cfg.get("diaphragm","rigid")).lower()
    if _dia in ("flexible","semi-rigid") and isinstance(pkg,dict):
        _blob2=_cd_blob(pkg)+" "+json.dumps(pkg).lower()
        if "tributary" not in _blob2:
            out.append("cfg['diaphragm']='%s' declared but calc_package never distributes lateral force by "
                       "TRIBUTARY AREA -- for a flexible diaphragm the braced-line shears, deck shear (plf), "
                       "chords and collectors must be designed from the tributary model (ASCE 7-22 12.3.1)"%_dia)
    _dex = cfg.get("drift_exempt_stories") or {}
    if _dex and isinstance(pkg,dict):
        _blob3=json.dumps(pkg).lower()
        if not any(w in _blob3 for w in ("step","split-level","inter-diaphragm","offset")):
            out.append("drift_exempt_stories declared (%s) but calc_package has no step/split-level transfer "
                       "detail -- the exempted inter-diaphragm racking must be a DESIGNED detail (shear "
                       "transfer + the shared columns checked across the offset)"%sorted(_dex))
    if (pir.get("reentrant") or pir.get("setback")) and isinstance(pkg,dict):
        conns=pkg.get("connections") or []
        # the SEEDED (empty) collector slot is not a design: a collector entry only counts once it
        # carries a D/C, per-limit-state checks or an explicit waiver (HR-33)
        has_coll=any(isinstance(c,dict) and "collector" in (str(c.get("id",""))+str(c.get("type",""))).lower()
                     and (c.get("DC") is not None or c.get("checks") or c.get("waived")) for c in conns)
        if not has_coll:
            out.append("re-entrant/setback present but NO designed collector in calc_package -- collectors are a REQUIRED "
                       "deliverable (design with Omega_0 on the re-entrant/transfer lines, 12.10.2.1; fill the seeded "
                       "slot's limit_state/cited/capacity/DC), not 'delegated to the drawings'")
    return out


# ---- per-system REQUIRED capacity-design checks (AISC 341-22) -- NUMERIC presence (HR-33) -------
# Each requirement is met only by a capacity_design entry whose key (or own text) names the check
# AND that carries a COMPUTED number (see _has_numeric_result); a keyword in prose no longer clears
# it. Keyed by preflight.SYSTEMS rows: a dual declaration gets its MF + braced lists + the dual row,
# a mixed declaration gets every component's list. AISC 341-22 E2 (IMF) and E4 (STMF) have no SCWB.
_SYS_REQUIRED = {
    "smf":  [("SCWB ratio sum M*pc / sum M*pb > 1.0 (AISC 341-22 E3.4a)", r"scwb|strong.?column|e3\.4a"),
             ("panel-zone shear / doubler (E3.6e)", r"panel|doubler")],
    "imf":  [("panel-zone shear (AISC 360-22 J10.6 / 341-22 E2.6e)", r"panel|doubler"),
             ("continuity plates (E2.6f)", r"continuity"),
             ("column axial strength with overstrength (D1.4a)", r"overstrength|omega|om0|ω|d1\.4a")],
    "omf":  [("column axial strength with overstrength (D1.4a) / OMF connection (E1.6b)",
              r"overstrength|omega|om0|ω|d1\.4a|e1\.6")],
    "stmf": [("special-segment shear strength / expected vertical shear Ve (E4.5)",
              r"special.?segment|expected.?(vertical.?)?shear|e4\.5|(^|_)ve(_|$)")],
    "scbf": [("brace expected strength RyFyAg / min(RyFyAg, FneAg/0.877) (F2.3)",
              r"expected|ryfy|1\.1ry|f2\.3|capacity.?limited")],
    "ocbf": [("connections / columns for expected strength or overstrength (F1.4a, F1.6)",
              r"expected|ryfy|overstrength|omega|om0|ω|f1\.[46]")],
    "ebf":  [("link length e and Vp / Mp (F3.5b)", r"link"),
             ("link rotation angle <= 0.08 / 0.02 rad (F3.4a)", r"rotation|gamma|γ"),
             ("outside-link members for the adjusted link shear 1.25RyVn (F3.3)", r"1\.25|expected|outside|capacity.?limited")],
    "brbf": [("adjusted brace strengths beta*omega*Ry*Pysc (F4.2a)", r"adjusted|beta|omega|β|ω")],
    "spsw": [("VBE stiffness Ic >= 0.0031 tw h^4 / L (F5.4a)", r"vbe|(^|_)ic(_|$)|0\.003"),
             ("web-plate expected tension yield RyFy (F5.3)", r"tension|web.?plate|ryfy")],
    "c-psw": [],
    "nsd":  [],
    "dual": [("moment frames capable of resisting >= 25 % of the design seismic forces (ASCE 7-22 12.2.5.1)",
              r"dual|25.?(pct|%|percent)|mf.?only|independent")],
}

def _cd_blob(pkg):
    """All text in capacity_design -- dict KEYS + string values + numbers -- so a computed check stored with
    descriptive keys (Ic_provided: 1350) is detected, not just free-text notes."""
    cd=pkg.get("capacity_design") if isinstance(pkg,dict) else None
    parts=[]
    def walk(o):
        if isinstance(o,dict):
            for k,v in o.items(): parts.append(str(k)); walk(v)
        elif isinstance(o,list):
            for v in o: walk(v)
        else: parts.append(str(o))
    walk(cd if cd is not None else {})
    return " ".join(parts).lower()

_CMP = re.compile(r">=|<=|≥|≤")

def _named_not_computed_issues(pkg):
    """A capacity_design string that states a REQUIREMENT symbolically ('e <= 1.6Mp/Vp',
    'Ic >= 0.0031 tw h^4/L') instead of a computed check. A string whose comparison has a NUMBER on
    BOTH sides ('b/t = 7.2 <= 0.3 sqrt(E/Fy) = 7.22', 'TIR 1.31 <= 1.4') is a computed check and is
    no longer flagged (HR-33)."""
    out=[]; cd=pkg.get("capacity_design") if isinstance(pkg,dict) else None
    if cd is None: return out
    txt=[]; _gather_text(cd,txt)
    for t in txt:
        if not isinstance(t,str) or _is_seeded_text(t) or not _CMP.search(t): continue
        computed=True
        for clause in re.split(r"[;\n]|,\s(?=[A-Za-z])", t):
            m=_CMP.search(clause)
            if not m: continue
            left, right = clause[:m.start()], clause[m.end():]
            if re.search(r"\d", left) and re.search(r"\d", right):
                continue                                   # value vs limit, both numeric -> computed
            bare=set(v.lower() for v in re.findall(r"(?<![A-Za-z0-9])[A-Za-z](?![A-Za-z0-9])", clause))
            if len(bare) >= 2:                             # >= 2 unresolved single-letter symbols
                computed=False; break
        if not computed:
            out.append("[capacity_design] '%s' is a symbolic REQUIREMENT, not a COMPUTED check -- substitute the section's numbers and give value vs limit + D/C"%t.strip()[:80])
    return out

def _system_rows(cfg):
    try:
        from preflight import parse_system as _ps
    except Exception:
        return []
    ps=_ps(cfg)
    if ps["kind"]=="dual":
        mf=[k for k in ps["parts"] if k in ("smf","imf")]; br=[k for k in ps["parts"] if k not in ("smf","imf")]
        return mf+br+["dual"]
    return list(ps["rows"])

def _system_checks_issues(cfg, pkg):
    out=[]
    if not isinstance(cfg,dict) or not cfg.get("system") or not isinstance(pkg,dict): return out
    cd=pkg.get("capacity_design")
    for key in _system_rows(cfg):
        for label,pat in _SYS_REQUIRED.get(key,[]):
            if not _numeric_entries(cd if cd is not None else {}, pat):
                out.append("system '%s' requires check: %s -- no capacity_design entry with COMPUTED numbers for it "
                           "(add it under a descriptive key, with the values, limit and D/C)"%(cfg.get("system"),label))
    return out

def _height_limit_evidence(pkg):
    """capacity_design['height_limit'] -> the dict preflight.increase_12_2_5_4 reads (TIR_max,
    max_plane_share_X/Y as NUMBERS), or None. A framework torsion screen above 1.4 is only overridden
    by an agent TIR when the evidence also states its 'TIR_basis' (Eq. 12.3-2 computation)."""
    cd=pkg.get("capacity_design") if isinstance(pkg,dict) else None
    hl=cd.get("height_limit") if isinstance(cd,dict) else None
    if not isinstance(hl,dict): return None
    ev={}
    for k,v in hl.items():
        nk=re.sub(r"[^a-z0-9]","",str(k).lower())
        if isinstance(v,bool) or not isinstance(v,(int,float)): continue      # NUMBERS only
        if nk in ("tirmax","tir"): ev["TIR_max"]=float(v)
        if nk in ("maxplanesharex","planesharex"): ev["max_plane_share_X"]=float(v)
        if nk in ("maxplanesharey","planesharey"): ev["max_plane_share_Y"]=float(v)
        if nk in ("maxplanesharexpct","planesharexpct"): ev["max_plane_share_X"]=float(v)/100.0
        if nk in ("maxplaneshareypct","planeshareypct"): ev["max_plane_share_Y"]=float(v)/100.0
    if not ev: return None
    try:
        scr=float(((pkg.get("framework_screen") or {}).get("torsion") or {}).get("ratio_max"))
    except (TypeError,ValueError):
        scr=None
    if scr is not None and scr>1.4 and isinstance(ev.get("TIR_max"),float) and ev["TIR_max"]<=1.4 \
            and not str(hl.get("TIR_basis") or "").strip():
        ev["TIR_max"]=scr          # the screen says > 1.4 and the agent did not show its own TIR basis
    return ev

def _height_limit_issues(cfg, pkg=None):
    """ASCE 7-22 Table 12.2-1 / 12.2.5.4-7 system + height limits and the 11.6 SDC and Table
    12.2-1 / 12.8-2 factor checks -- the ERROR findings of preflight (single source of the rules).
    The 12.2.5.4 increase clears with NUMERIC evidence in capacity_design['height_limit'] =
    {'TIR_max': .., 'max_plane_share_X': .., 'max_plane_share_Y': ..} (or cfg['height_increase_12_2_5_4'])."""
    out=[]
    if not isinstance(cfg,dict): return out
    try:
        import preflight as _PF
    except Exception as e:
        return ["system/height-limit checks could not run (preflight import failed: %s)"%e]
    for sev,msg in (_PF.sdc_findings(cfg) + _PF.system_limit_findings(cfg, _height_limit_evidence(pkg))
                    + _PF.factor_findings(cfg)):
        if sev=="ERROR": out.append(msg)
    return out

def _transfer_issues(cfg, name, pkg):
    out=[]
    try:
        import engine3d as _E
        pir=_E.plan_irregularities(_E.CFG.get(name) or cfg)
    except Exception:
        return out
    if pir.get("setback") and isinstance(pkg,dict):
        blob=" ".join(_agent_texts([pkg.get("capacity_design"), pkg.get("connections")])).lower()
        if "transfer" not in blob and "backstay" not in blob:
            out.append("footprint SETBACK detected -- design the TRANSFER/backstay diaphragm chords/collectors and supporting members for Omega_0 (7-22 12.3.3.4, discontinuous-element support) and report the backstay force")
    return out

def _nonparallel_issues(cfg, pkg):
    out=[]
    if cfg.get("skew") and isinstance(pkg,dict):
        if "biaxial" not in _cd_blob(pkg):
            out.append("nonparallel/skewed frame -- resolve stiffness into BOTH principal directions and apply BIAXIAL SCWB/interaction at the skewed columns (not evident in capacity_design)")
    return out



def _consultancy_issues(cfg, pkg):
    """Tier A/B real-world guards (EDGE_CASE_SWEEP): each WARN clears when the package addresses
    the topic, so they are reconcilable by DOING the check, not by boilerplate. Only AGENT-authored
    text counts (the pipeline's seeded notes / screen never trigger or clear a guard)."""
    out = []
    if not isinstance(cfg, dict) or not isinstance(pkg, dict):
        return out
    blob = _agent_blob(pkg)
    arch = (str(cfg.get("arch", "")) + " " + str(cfg.get("system", ""))).lower()
    mem = pkg.get("members") or []
    # A3 ponding: long-span flat roof beams and no ponding statement
    roof_long = any(isinstance(m, dict) and (m.get("inputs") or {}).get("role") == "roof"
                    and float((m.get("inputs") or {}).get("length_in") or 0) >= 480 for m in mem)
    if roof_long and "ponding" not in blob:
        out.append("long-span (>=40 ft) roof framing and NO ponding evaluation in calc_package -- "
                   "check ponding stability/impounded rain (AISC 360-22 B3.10 / ASCE 7-22 Ch. 8: "
                   "roof slope + secondary drainage head) and record it")
    # A6 footfall vibration: long floor spans or vibration-sensitive occupancy
    floor_long = any(isinstance(m, dict) and (m.get("inputs") or {}).get("role") == "floor"
                     and float((m.get("inputs") or {}).get("length_in") or 0) >= 480 for m in mem)
    sens = any(k in arch for k in ("lab", "laborator", "hospital", "gym", "assembly", "vibration"))
    if (floor_long or sens) and "vibration" not in blob:
        out.append("footfall VIBRATION serviceability not addressed (long floor spans and/or "
                   "vibration-sensitive occupancy) -- do the AISC Design Guide 11 screen (fn, a_peak "
                   "vs occupancy limit) and record it")
    # A1 composite floors: the cfg declares a composite floor system -> the package must carry the
    # Ch. I essentials as COMPUTED values (studs + camber + the unshored wet-concrete stage), OR an
    # explicit scope statement in composite_design['scope'] that composite action is not relied upon.
    fs = (str(cfg.get("floor_system", "")).lower() + " " + arch + " "
          + str(cfg.get("notes", "")).lower())   # notes too: agents park the brief's composite line there
    if "composite" in fs:
        tree = {k: v for k, v in pkg.items() if k not in _SEEDED_KEYS and k != "note"}
        need = [(r"stud|(^|_)qn", "stud strength/schedule (I8.2a) with Qn / number of studs"),
                (r"camber", "camber decision with the wet-concrete deflection (even 'no camber: 0.55 in < 3/4 in')"),
                (r"wet|unshored|construction", "unshored wet-concrete stage check (Mu vs phiMn, deflection)")]
        missing = [what for pat, what in need if not _numeric_entries(tree, pat, key_only=True)]
        cdz = pkg.get("composite_design") if isinstance(pkg.get("composite_design"), dict) else {}
        sc = str(cdz.get("scope") or pkg.get("composite_scope") or "").lower()
        scoped = len(sc) >= 60 and any(k in sc for k in ("not relied", "excluded", "bare steel", "bare-steel",
                                                          "lower bound", "lower-bound", "delegat"))
        if missing and not scoped:
            out.append("COMPOSITE floor system declared and the package lacks COMPUTED: " + "; ".join(missing) +
                       " -- design the composite floor per AISC 360-22 Ch. I (see COMPOSITE_I3.md: "
                       "b_eff I3.1a, studs I8.2a, partial composite I3.2a, camber rule, I_LB deflection) under "
                       "calc_package['composite_design'], or state in composite_design['scope'] that composite "
                       "action is not relied upon (and why)")
    # A8 seismic joint / pounding: multi-wing keywords and no joint decision ('wing' as a WORD --
    # 'drawing', 'showing' no longer trigger it)
    if (re.search(r"\b(twin|two[- ]towers?|wings?)\b", arch)) and \
            not any(k in blob for k in ("seismic joint", "seismic_joint", "pounding", "joint width", "no seismic joint")):
        out.append("multi-wing/tower configuration and NO seismic-joint decision recorded -- either "
                   "size the joint (sum of Cd-amplified drifts, ASCE 7-22 12.12.3 + pounding check) "
                   "or record why the wings are intentionally connected (with the interaction designed)")
    # B6 snow drift at steps/parapets
    stepish = any(k in arch for k in ("step", "setback", "parapet", "penthouse", "tier", "wedding"))
    if float(cfg.get("snow", 0) or 0) > 0 and stepish and "drift" not in blob.replace("drift_", ""):
        out.append("snow present with roof steps/parapets/setbacks and no DRIFT surcharge in the "
                   "package (ASCE 7-22 7.7/7.8) -- add the drift check to the step-adjacent members")
    # B8 delegated-design register
    if any(k in blob for k in ("joist", "sji", " deck", "brb", "stair", "curtain wall")) and \
            "delegat" not in blob:
        out.append("delegated-design components referenced (joists/deck/BRBs/stairs/cladding) but no "
                   "'delegated_design' register in capacity_design -- list each delegated item, the "
                   "design criteria handed off, and the interface forces")
    return out


RESULT_FILE = "consistency_result.json"


def _sha256(path):
    try:
        import hashlib
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return None


def _record_result(root, name, issues):
    """Persist the verdict to <root>/design/consistency_result.json with the SHA-256 of the
    calc_package.json and cfg.py it was computed from, so the app-side completion gate (which never
    executes cfg.py) can tell a CURRENT clean check from a stale or missing one (HR-14)."""
    try:
        import datetime
        d = os.path.join(root, "design")
        if not os.path.isdir(d):
            return
        rec = {"name": name, "time": datetime.datetime.now().isoformat(timespec="seconds"),
               "result": "PASS" if not issues else "FAIL", "n_issues": len(issues), "issues": issues,
               "calc_package_sha256": _sha256(os.path.join(d, "calc_package.json")),
               "cfg_sha256": _sha256(os.path.join(root, "cfg.py"))}
        with open(os.path.join(d, RESULT_FILE), "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1)
    except Exception:
        pass                                   # recording must never break the check itself


def check(name, root=None, pkg=None, verbose=True):
    """Run the self-consistency check. Returns a list of issue strings ([] == consistent).
    The verdict is also written to design/consistency_result.json (with the hashes of the package
    and cfg.py it judged) whenever the package checked IS the one on disk -- the completion gate
    reads that file."""
    issues = []
    _from_disk = pkg is None
    if pkg is None:
        if root is None:
            base = os.environ.get("STEEL_BUILDER_JOBS") or _here_repo()
            root = os.path.join(base, name)
        path = os.path.join(root, "design", "calc_package.json")
        if not os.path.exists(path):
            issues.append(f"calc_package.json not found at {path} -- run design_and_report first")
            if verbose:
                _print(name, issues)
            return issues
        try:
            pkg = json.load(open(path, encoding="utf-8"))
        except Exception as ex:
            issues.append(f"calc_package.json is not valid JSON: {ex}")
            if verbose:
                _print(name, issues)
            return issues

    members = pkg.get("members") or []
    conns = pkg.get("connections") or []
    if not isinstance(conns, list) or len(conns) == 0:
        issues.append("connections list is empty -- connections are a REQUIRED deliverable "
                      "(design them in place; demands-and-basis alone is incomplete)")
    for m in members:
        if isinstance(m, dict):
            issues += _entry_issues("member", m)
    for c in conns:
        if isinstance(c, dict):
            issues += _entry_issues("connection", c)

    _root = root if root else os.path.join(os.environ.get("STEEL_BUILDER_JOBS") or _here_repo(), name)
    if not os.path.exists(os.path.join(_root, "cfg.py")):
        issues.append("jobs/%s/cfg.py not found -- write the building's cfg (including any custom_build) to cfg.py "
                      "FIRST and keep it; the saved OpenSees model must be reproducible and editable for later studies." % name)
    _dcfg = _load_cfg(_root, name)
    issues += _geometry_issues(_dcfg)                       # units/geometry sanity (story heights in ft, etc.)
    issues += _design_basis_issues(_dcfg, name, pkg)        # R1/R2/R8/R12/R14/R16
    issues += _height_limit_issues(_dcfg, pkg)              # R19 SDC / system / height limits / factors
    issues += _transfer_issues(_dcfg, name, pkg)            # R7/R15 transfer/backstay
    issues += _nonparallel_issues(_dcfg, pkg)               # R10 skewed frame
    issues += _consultancy_issues(_dcfg, pkg)               # Tier A/B real-world guards
    issues += _named_not_computed_issues(pkg)               # R9 named-not-computed
    issues += _system_checks_issues(_dcfg, pkg)             # R9 per-system required checks
    issues += _completeness_issues(pkg)
    _same = _from_disk
    if not _same:
        try:
            _same = json.load(open(os.path.join(_root, "design", "calc_package.json"), encoding="utf-8")) == pkg
        except Exception:
            _same = False
    if _same:
        _record_result(_root, name, issues)
    if verbose:
        _print(name, issues)
    return issues


def _print(name, issues):
    print("[consistency] %s: %d issue(s)" % (name, len(issues)))
    for i in issues:
        print("   -", i)
    print("RESULT:", "PASS (self-consistent)" if not issues else "FAIL (%d to reconcile)" % len(issues))


if __name__ == "__main__":
    import sys
    for nm in (sys.argv[1:] or ["B02"]):
        check(nm)

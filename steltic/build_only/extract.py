"""An HR job -> a building description, as if the user had designed it.

Used to re-enter the HR test buildings as user designs (the build-only acceptance test: the NL results of the
re-entered building must match the gold solution of the original). The model is built in a child process with
the job's own cfg.py; nodes, elements, sections, releases, column orientations and supports are read from the
OpenSees domain and the engine's registries -- exactly what the design's member schedule says -- and the basis,
loads and details from the executed cfg. Whatever the description cannot carry is listed in
desc["_extract_notes"] (e.g. a node off every level).
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

from . import schema as S

ENGINE_DIR = pathlib.Path(os.environ.get("STELTIC_ENGINE_DIR") or (pathlib.Path(__file__).resolve().parents[2] / "steel_engine"))

_CHILD = r'''
import json, math, runpy, sys
import openseespy.opensees as ops
import engine3d as eng
_ARGS = {}
_el = ops.element
def _rec(*a):
    try:
        _ARGS[int(a[1])] = [str(x) for x in a]
    except Exception:
        pass
    return _el(*a)
ops.element = _rec
_RD = {}
_rd = ops.rigidDiaphragm
def _recrd(*a):
    try:
        _RD.setdefault(int(a[1]), []).extend(int(x) for x in a[2:])
    except Exception:
        pass
    return _rd(*a)
ops.rigidDiaphragm = _recrd
ns = runpy.run_path("cfg.py")
cfg = ns["cfg"]
info = eng.build(cfg, "Linear")
nodes = {t: ops.nodeCoord(t) for t in ops.getNodeTags()}
fixed = {}
for t in ops.getFixedNodes():
    fixed[t] = list(ops.getFixedDOFs(t))
eles = []
for (tag, kind, sec, n1, n2) in info["ele"]:
    rec = dict(tag=tag, kind=kind, sec=sec, n1=n1, n2=n2)
    if kind == "col":
        rec["strong"] = eng._COL_DIR.get(tag)
        a = _ARGS.get(tag) or []
        ry = a[a.index("-releasey") + 1] if "-releasey" in a else "0"
        rz = a[a.index("-releasez") + 1] if "-releasez" in a else "0"
        rec["crel"] = [ry, rz]
    if kind == "beam":
        rec["rel"] = list(eng._BEAM_REL.get(tag, ("none", "none")))
    if kind == "brace":
        try:
            rec["A"] = ops.eleResponse(tag, "A") or None
        except Exception:
            rec["A"] = None
    eles.append(rec)
listed = {e[0] for e in info["ele"]}
other = [t for t in ops.getEleTags() if t not in listed]
sd = {d: eng.seis_dir(cfg, d) for d in ("X", "Y")}
def js(o):
    if isinstance(o, dict): return {str(k): js(v) for k, v in o.items() if not callable(v)}
    if isinstance(o, (list, tuple, set)): return [js(v) for v in o]
    if isinstance(o, (int, float, str, bool)) or o is None: return o
    return str(o)
z = eng.zlevels(cfg)
import preflight
rc = preflight._risk_category(cfg) if hasattr(preflight, "_risk_category") else cfg.get("risk_category")
slaved = sorted({t for v in _RD.values() for t in v})
out = dict(nodes=nodes, fixed=fixed, eles=eles, slaved=slaved, other_eles=other, z=z, NX=cfg["NX"], NY=cfg["NY"],
           grid_x=[eng._xy_in(cfg, i, 0)[0] for i in range(cfg["NX"] + 1)],
           grid_y=[eng._xy_in(cfg, 0, j)[1] for j in range(cfg["NY"] + 1)],
           seis=js(cfg.get("seis")), sd=js(sd), cfg=js({k: v for k, v in cfg.items() if k not in ("custom_build",)}), rc=rc,
           floor_area={k: eng.floor_area_ft2(cfg, k) for k in range(1, len(cfg["heights"]) + 1)},
           W={k: eng.floor_w(cfg, k) for k in range(1, len(cfg["heights"]) + 1)})
print("@@JSON@@" + json.dumps(out, default=str))
'''


def _child(job_dir, python=None):
    env = {**os.environ, "PYTHONPATH": str(ENGINE_DIR), "MPLBACKEND": "Agg", "PYTHONDONTWRITEBYTECODE": "1",
           "STEEL_BUILDER_JOBS": str(pathlib.Path(job_dir).parent)}
    p = subprocess.run([python or sys.executable, "-c", _CHILD], cwd=str(job_dir), env=env, capture_output=True, text=True,
                       timeout=900)
    line = next((l for l in (p.stdout or "").splitlines() if l.startswith("@@JSON@@")), None)
    if not line:
        raise RuntimeError("could not build %s: %s" % (job_dir, (p.stderr or p.stdout)[-2000:]))
    return json.loads(line[8:])


def _lab(i, prefix):
    return "%s%d" % (prefix, i + 1)


def extract(job_dir, python=None) -> dict:
    job_dir = pathlib.Path(job_dir)
    R = _child(job_dir, python)
    cfg = R["cfg"]
    notes = []
    d = S.new_description("hr_job", str(job_dir / "cfg.py"))
    d["project"] = {"name": job_dir.name, "code": "ASCE 7-22", "notes": "re-entered from the HR job %s" % job_dir.name}
    FT = 12.0
    gx = [x / FT for x in R["grid_x"]]; gy = [y / FT for y in R["grid_y"]]
    d["grid"] = {"x": [{"label": _lab(i, "X"), "coord_ft": round(v, 6)} for i, v in enumerate(gx)],
                 "y": [{"label": _lab(j, "Y"), "coord_ft": round(v, 6)} for j, v in enumerate(gy)]}
    z = R["z"]
    d["levels"] = [{"name": "Base" if k == 0 else "L%d" % k, "elev_ft": round(zz / FT, 6)} for k, zz in enumerate(z)]
    lvname = lambda k: d["levels"][k]["name"]
    nodes = {int(t): c for t, c in R["nodes"].items()}

    def pt(t):
        x, y, zz = nodes[t]
        xf, yf = x / FT, y / FT
        px = next((g["label"] for g in d["grid"]["x"] if abs(g["coord_ft"] - xf) < 1e-4), round(xf, 6))
        py = next((g["label"] for g in d["grid"]["y"] if abs(g["coord_ft"] - yf) < 1e-4), round(yf, 6))
        k = next((k for k, v in enumerate(z) if abs(v - zz) < 1e-3), None)
        return px, py, k

    for e in R["eles"]:
        a, b = pt(e["n1"]), pt(e["n2"])
        if a[2] is None or b[2] is None:
            notes.append("element %s (%s): an end is off every level -- left out" % (e["tag"], e["kind"]))
            d.setdefault("_unsupported", []).append("%s %s (%s): an end at z = %.1f ft is not on a level -- version 1 "
                                                    "places member ends on levels only" % (e["kind"], e["tag"], e["sec"],
                                                    (nodes[e["n1"]][2] if a[2] is None else nodes[e["n2"]][2]) / FT))
            continue
        if e["kind"] == "col" and (a[0], a[1]) != (b[0], b[1]):
            d.setdefault("_unsupported", []).append("column %s (%s): inclined (its ends are at different plan points) -- "
                                                    "version 1 models vertical columns only" % (e["tag"], e["sec"]))
            continue
        if e["kind"] == "col":
            lo, hi = (a, b) if a[2] < b[2] else (b, a)
            col = dict(id="C%d" % e["tag"], x=lo[0], y=lo[1], base_level=lvname(lo[2]), top_level=lvname(hi[2]),
                       section=e["sec"], strong_axis=str(e.get("strong") or "X").upper()[:1])
            ry, rz = (e.get("crel") or ["0", "0"])
            if ry != rz:
                d.setdefault("_unsupported", []).append("column %s: a release about one axis only (%s/%s)" % (e["tag"], ry, rz))
            elif ry != "0":
                flip = a[2] > b[2]                              # element I end at the top
                code = {"1": "J" if flip else "I", "2": "I" if flip else "J", "3": "both"}[ry]
                col["release"] = {"I": "base", "J": "top", "both": "both"}[code]
            d["columns"].append(col)
        elif e["kind"] == "beam":
            relz = (e.get("rel") or ["none"])[0]
            d["beams"].append(dict(id="B%d" % e["tag"], level=lvname(a[2]), i={"x": a[0], "y": a[1]}, j={"x": b[0], "y": b[1]},
                                   section=e["sec"], release={"none": "none", "I": "I", "J": "J", "both": "both"}.get(relz, "none")))
            if a[2] != b[2]:
                notes.append("beam %s is sloped (levels %s/%s)" % (e["tag"], a[2], b[2]))
        elif e["kind"] == "brace":
            rec = dict(id="D%d" % e["tag"], i={"x": a[0], "y": a[1], "level": lvname(a[2])},
                       j={"x": b[0], "y": b[1], "level": lvname(b[2])})
            sec = str(e["sec"])
            if sec.upper().startswith("BRB"):
                asc = ((cfg.get("brb") or {}).get("Asc") or {}).get(sec)
                rec["brb"] = {"Asc_in2": asc}
            else:
                rec["section"] = sec
            d["braces"].append(rec)
    bl = cfg.get("brace_length") if isinstance(cfg.get("brace_length"), dict) else {}
    blf = cfg.get("brace_length_factor")
    for br, e in zip(d["braces"], [e for e in R["eles"] if e["kind"] == "brace" and pt(e["n1"])[2] is not None and pt(e["n2"])[2] is not None]):
        Lwp = sum((a - b) ** 2 for a, b in zip(nodes[e["n1"]], nodes[e["n2"]])) ** 0.5
        if str(e["sec"]) in bl and Lwp > 0:
            br["length_factor"] = round(float(bl[str(e["sec"])]) / Lwp, 6)
        elif isinstance(blf, (int, float)):
            br["length_factor"] = float(blf)
    if R["other_eles"]:
        notes.append("%d element(s) outside info['ele'] (links, springs) are not carried: %s" % (len(R["other_eles"]), R["other_eles"][:10]))
    # EBF links
    links = {tuple(sorted(map(int, p))) for p in (cfg.get("ebf_links") or [])}
    if links:
        for bm, e in zip([b for b in d["beams"]], [e for e in R["eles"] if e["kind"] == "beam"]):
            if tuple(sorted((e["n1"], e["n2"]))) in links:
                bm["link"] = True
    # supports
    base = [t for t in nodes if int(t) // 100000 == 0]
    fx = {int(t): v for t, v in R["fixed"].items()}
    kinds = set()
    at = []
    for t in base:
        f = fx.get(t)
        if not f:
            continue
        kind = "fixed" if {4, 5, 6} <= set(f) else "pinned"
        kinds.add(kind)
        px, py, _ = pt(t)
        at.append({"x": px, "y": py, "fixity": kind})
    d["supports"] = {"default": next(iter(kinds))} if len(kinds) == 1 else {"default": None, "at": at}
    d["diaphragm"] = str(cfg.get("diaphragm") or "rigid")
    slaved = set(R.get("slaved") or [])
    used = {e[k] for e in R["eles"] for k in ("n1", "n2")}
    linkn = {n for p in links for n in p}
    ex = []
    for t in sorted(used):
        k = t // 100000
        if k == 0 or t % 100000 >= 99998 or t in slaved or t in linkn:
            continue
        px, py, kk = pt(t)
        if kk is not None:
            ex.append({"x": px, "y": py, "level": lvname(kk)})
    if ex:
        d["diaphragm_exclude"] = ex
    if links:
        d["details"] = d.get("details") or {}
        d["_link_in_diaph"] = any(n in slaved for n in linkn)
    # seismic
    se = R["seis"] or {}
    sd = R["sd"]
    sysk = {}
    for dr in ("X", "Y"):
        k = S.norm_system(sd[dr].get("system") or cfg.get("system_" + dr) or cfg.get("system"))
        sysk[dr] = k if isinstance(k, str) else (sd[dr].get("system") or cfg.get("system"))
    d["seismic"] = {"SDS": se.get("SDS"), "SD1": se.get("SD1"), "S1": se.get("S1"), "TL": se.get("TL", 8.0),
                    "Ie": se.get("Ie"), "risk_category": str(R.get("rc") or cfg.get("risk_category") or {1.0: "II", 1.25: "III", 1.5: "IV"}.get(se.get("Ie"), "II")),
                    "site_class": cfg.get("site_class") or "D",
                    "system": {dr: (sysk[dr] if isinstance(sysk[dr], str) else None) for dr in ("X", "Y")},
                    "R": {dr: sd[dr].get("R") for dr in ("X", "Y")}, "Cd": {dr: sd[dr].get("Cd") for dr in ("X", "Y")},
                    "Om0": {dr: sd[dr].get("Om0") for dr in ("X", "Y")}, "rho": {dr: sd[dr].get("rho") for dr in ("X", "Y")}}
    an = [str(x).upper() for x in (cfg.get("analyses") or ["ELF"])] if isinstance(cfg.get("analyses"), (list, tuple)) else [str(cfg.get("analyses") or "ELF").upper()]
    d["seismic"]["analysis"] = "MRSA" if any(x in ("RS", "MRSA", "RSA") for x in an) else "ELF"
    if not cfg.get("site_class"):
        notes.append("site class not in cfg: D assumed for the re-entry")
    # loads
    NF = len(z) - 1
    D = {"floor": cfg.get("D_floor"), "roof": cfg.get("D_roof")}
    for k, v in (cfg.get("D_by_level") or {}).items():
        D[lvname(int(k))] = v
    L = {"floor": cfg.get("L_floor", 0.0)}
    for k, v in (cfg.get("L_by_level") or {}).items():
        L[lvname(int(k))] = v
    d["loads"] = {"D_psf": D, "L_psf": L, "Lr_psf": cfg.get("Lr", 20.0), "snow_psf": cfg.get("snow", 0.0),
                  "clad_psf": cfg.get("clad", 0.0),
                  "floor_system": "one-way" if str(cfg.get("floor_system") or "").startswith("one") else "two-way"}
    if d["loads"]["floor_system"] == "one-way":
        ds = cfg.get("deck_span")
        d["loads"]["deck_span"] = ds if ds in ("X", "Y") else "X"
    for ck, dk in (("extra_mass_floors", "extra_dead_psf"), ("seismic_mass_only", "seismic_only_psf"),
                   ("seismic_weight_kip", "seismic_weight_kip")):
        if cfg.get(ck):
            d["loads"][dk] = {lvname(int(k)): v for k, v in cfg[ck].items()}
    op = cfg.get("openings")
    if isinstance(op, dict):
        d["openings"] = [{"level": lvname(int(k)), "x": d["grid"]["x"][int(i)]["label"], "y": d["grid"]["y"][int(j)]["label"]}
                         for k, bays in op.items() for (i, j) in (bays or []) if int(k) < len(z)]
    w0 = cfg.get("wind") if isinstance(cfg.get("wind"), dict) else {}
    d["loads"]["parapet_ft"] = float(w0.get("parapet_ft") or 0.0)
    if isinstance(cfg.get("infill_spacing"), (int, float)):
        d["loads"]["infill_spacing_in"] = float(cfg["infill_spacing"])
    elif cfg.get("infill_spacing"):
        notes.append("infill_spacing given per level / direction (%s): not carried" % (cfg["infill_spacing"],))
    if cfg.get("storage_levels"):
        d["loads"]["storage_levels"] = [lvname(int(k)) for k in cfg["storage_levels"]]
    # materials / details
    mats = cfg.get("materials") or {}
    d["materials"] = {"W": "A992", "HSS": "A500 Gr C"}
    if cfg.get("existing_structure"):
        ex = cfg["existing_structure"] if isinstance(cfg["existing_structure"], dict) else {}
        d["materials"].update(existing=True, existing_info={"year": ex.get("year") or "1960s", "grade": ex.get("grade")})
        g = str(ex.get("grade") or "")
        if "A36" in g.upper():
            d["materials"]["W"] = "A36"
    bm = cfg.get("brace_material") if isinstance(cfg.get("brace_material"), dict) else {}
    if bm.get("spec"):
        g = S.norm_grade(str(bm["spec"]).replace("ASTM", ""))
        if g and "HSS" in S.GRADES[g]["types"]:
            d["materials"]["HSS"] = g
    if cfg.get("brb"):
        b = cfg["brb"]
        d["materials"]["brb"] = {"Fysc_ksi": b.get("Fysc"), "KF": b.get("KF", 1.0), "beta": b.get("beta"), "omega": b.get("omega")}
        kf = float(b.get("KF", 1.0) or 1.0)
        for br in d["braces"]:
            if br.get("brb") and br["brb"].get("Asc_in2") is None:
                notes.append("brace %s: BRB Asc not found in cfg['brb']['Asc']" % br["id"])
    rbs = (cfg.get("rbs") or {})
    try:
        calc = json.loads((job_dir / "design" / "calc_package.json").read_text())
    except (OSError, ValueError):
        calc = {}
    rbs_sec = {}
    for m in calc.get("members") or []:
        g = (m.get("inputs") or {}).get("RBS")
        if isinstance(g, dict) and all(g.get(k) for k in ("a_in", "b_in", "c_in")):
            rbs_sec[S.norm_section((m.get("inputs") or {}).get("section"))] = {k: float(g[k]) for k in ("a_in", "b_in", "c_in")}
    # RBS cuts recorded on connection records (HR writes "RBS a = 6.56 in, b = 22.65 in, c = 2.625 in" on the
    # beam-to-column connection of the moment frame) -- the same records the Nonlinear module reads
    import re as _re
    _geo = {k: _re.compile(r"(?<![A-Za-z_'])%s\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*(?:in\b|\"|$|[,;)\s])" % k) for k in ("a", "b", "c")}
    _name = _re.compile(r"\bRBS\b|reduced[\s_-]*beam[\s_-]*section|358(?:-\d+)?\s*(?:ch(?:apter|\.)?\s*5\b)", _re.I)
    for cn in calc.get("connections") or []:
        if not isinstance(cn, dict) or not _name.search(" ".join(str(cn.get(k) or "") for k in ("type", "id", "components"))):
            continue
        txt = str(cn.get("components") or "")
        g = {}
        for k, rx in _geo.items():
            mo = rx.search(txt)
            if mo:
                g[k + "_in"] = float(mo.group(1))
        sec = S.norm_section(cn.get("section"))
        if len(g) == 3 and g["c_in"] > 0 and sec and sec not in rbs_sec:
            rbs_sec[sec] = g
    if rbs or rbs_sec:
        for b in d["beams"]:
            if b["release"] != "both" and S.norm_section(b["section"]) in rbs_sec:
                b["rbs"] = rbs_sec[S.norm_section(b["section"])]
        if rbs and not rbs_sec:
            notes.append("cfg declares RBS but calc_package records no cut geometry: RBS not carried")
    lb = {}
    for m in calc.get("members") or []:
        inp = m.get("inputs") or {}
        try:
            v = float(inp.get("Lb_in") or 0.0)
        except (TypeError, ValueError):
            v = 0.0
        s_ = S.norm_section(inp.get("section"))
        if s_ and v > 0 and v > lb.get(s_, 0.0):
            lb[s_] = v
    bbr = (calc.get("capacity_design") or {}).get("beam_bracing")
    bb_in = float(bbr["Lb_provided_in"]) if isinstance(bbr, dict) and bbr.get("Lb_provided_in") else None
    pzr = (calc.get("capacity_design") or {}).get("panel_zone")
    pz = pzr.get("by_joint") if isinstance(pzr, dict) else None
    d["details"] = {"doublers": [] if not pz else None}
    if lb:
        d["details"]["beam_Lb_in"] = lb
    if bb_in:
        d["details"]["beam_bracing_in"] = bb_in
    if d.pop("_link_in_diaph", None) is not None or links:
        d["details"]["link_ends_in_diaphragm"] = any(n in slaved for n in linkn)
    if pz and any(float((j or {}).get("doubler_in", (j or {}).get("doubler_t_in")) or 0) > 0 for j in pz if isinstance(j, dict)):
        # per-joint records that name the grid joint "(i,j)" and the level "L<k>" are carried; records grouped
        # by position class ("L3 interior ...") cannot be placed on a joint and are reported
        import re as _re
        dbl, lost = {}, 0
        for j in pz:
            if not isinstance(j, dict):
                continue
            t = float(j.get("doubler_in", j.get("doubler_t_in")) or 0.0)
            if t <= 0:
                continue
            lab = str(j.get("joint") or "")
            mo = _re.search(r"\((\d+)\s*,\s*(\d+)\)", lab)
            lv = j.get("level")
            if lv is None:
                ml = _re.search(r"\bL(\d+)\b", lab)
                lv = int(ml.group(1)) if ml else None
            if not mo or lv is None or int(lv) >= len(z):
                lost += 1
                continue
            key = (int(mo.group(1)), int(mo.group(2)), int(lv))
            dbl[key] = max(t, dbl.get(key, 0.0))
        d["details"]["doublers"] = [{"level": lvname(k), "x": d["grid"]["x"][i]["label"], "y": d["grid"]["y"][jj]["label"], "t_in": t}
                                    for (i, jj, k), t in sorted(dbl.items())]
        if lost:
            notes.append("%d HR panel_zone.by_joint record(s) with a doubler are grouped by position, not by joint: not carried" % lost)
    elif pz:
        d["details"]["doublers"] = []                         # records exist and every doubler is zero: none
    w = cfg.get("wind")
    if isinstance(w, dict) and w.get("V"):
        gc = float(w.get("GCpi", 0.18) or 0.18)
        d["wind"] = {"V_mph": w["V"], "exposure": w.get("exposure", "C"), "Kzt": w.get("Kzt", 1.0),
                     "enclosure": "partially enclosed" if gc > 0.3 else "enclosed"}
    try:
        pr = json.loads((job_dir / "design" / "pipeline_results.json").read_text())
        sm = pr.get("summary") or {}
        T = sm.get("T") or []
        d["user_results"] = {"T1_s": {"X": T[0] if T else None, "Y": T[1] if len(T) > 1 else None},
                             "V_kip": {"X": sm.get("V"), "Y": sm.get("V")}, "W_kip": sm.get("W"),
                             "drift_pct": {"X": 100 * sm["mdx"] if sm.get("mdx") else None, "Y": 100 * sm["mdy"] if sm.get("mdy") else None}}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    d["_extract_notes"] = notes
    d["_hr_reference"] = {"floor_area_ft2": R["floor_area"], "W_by_level": R["W"], "floor_bays": cfg.get("floor_bays")}
    return d

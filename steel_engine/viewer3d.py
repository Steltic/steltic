"""viewer3d.py -- interactive 3D structural viewer (three.js), opened from the report's
"View model" button (standalone tab; no iframe embed).

  standalone_html(cfg, name, root) -> full-page viewer HTML string
  report_section(cfg, name, root)  -> writes <root>/viewer_3d.html and returns the orange
                                      "View model" button linking to it (new tab).

Data sources (no NEW eigen solves -- adds a few seconds at most to a report build):
  * geometry / sections / mode shapes : engine3d.mode_shapes() cached eigen snapshot
  * end releases / column web dirs    : engine3d.build() info (beam_rel / col_dir snapshots)
  * member demands + governing combos : <root>/design/member_schedule.csv
  * capacities                        : sections.props() from the AISC Shapes DB
  * loads: dynamic-model gravity (level totals at grid nodes, as static_lateral applies them),
    static-model true tributary gravity per beam segment (apply_gravity math), wind story
    forces (engine3d.wind_forces) and ELF seismic story forces (engine3d.elf)
  * LLM model used: "_model=<x>" line in <root>/execution_log.md

D/C is a screening check (AISC 360 E3 with K=1, phi*Mp bending without LTB, H1 interaction);
the calc package is the check of record.  Vendored engine: vendor/three.min.js (r128, MIT).
"""
import os, re, csv, json, math, html as _html

_DIR = os.path.dirname(os.path.abspath(__file__))
_ES = 29000.0
ORANGE = "#ff8a3d"   # matches frontend/styles.css accent


# --------------------------------------------------------------- capacities
def _sec_props(sec):
    import sections as S
    try:
        p = S.props(sec)
        if p and p.get("A"):
            return p
    except Exception:
        pass
    try:
        import engine3d as E, sections as S2
        return {"A": E.HSS[sec], "ry": S2.brace_r(sec), "Zx": None, "Zy": None}
    except Exception:
        return None


def _dc(kind, sec, L_in, Pc_k, Pt_k, Mx_kin, My_kin, Fy):
    """Screening D/C: AISC E3 compression (K=1), phi*Fy*Z bending (no LTB), H1 interaction."""
    p = _sec_props(sec)
    if not p:
        return None
    A = p["A"]
    ry = p.get("ry") or 2.5
    slend = max(L_in / max(ry, 1e-6), 1.0)
    Fe = math.pi ** 2 * _ES / slend ** 2
    Fcr = (0.658 ** (Fy / Fe)) * Fy if Fy / Fe <= 2.25 else 0.877 * Fe
    pPn_c, pPn_t = 0.9 * Fcr * A, 0.9 * Fy * A
    if kind == "brace" or not p.get("Zx"):
        return max(Pc_k / pPn_c, Pt_k / pPn_t)
    pMnx = 0.9 * Fy * p["Zx"]
    pMny = 0.9 * Fy * p["Zy"] if p.get("Zy") else None
    P, Pcap = (Pc_k, pPn_c) if Pc_k >= Pt_k else (Pt_k, pPn_t)
    pr = P / Pcap
    mterm = Mx_kin / pMnx + ((My_kin / pMny) if pMny else 0.0)
    return pr + 8.0 / 9.0 * mterm if pr >= 0.2 else pr / 2.0 + mterm


def _demands(root):
    path = os.path.join(root, "design", "member_schedule.csv")
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                out[int(row["ele_tag"])] = row
            except (KeyError, ValueError):
                continue
    return out


def _model_used(root):
    """LLM model recorded by the run harness: '_model=<x>' in execution_log.md."""
    try:
        txt = open(os.path.join(root, "execution_log.md"), encoding="utf-8",
                   errors="replace").read(4000)
        m = re.search(r"_model=([^\s|]+)", txt)
        return m.group(1) if m else None
    except Exception:
        return None


# --------------------------------------------------------------- loads
def _static_gravity(cfg, coords):
    """Per-beam per-segment tributary w (kip/in) for D and L, replaying apply_gravity's math
    on the build_static model (subdivided beams, true two-way tributary)."""
    import engine3d as E, static_model as SM
    m = SM.build_static(cfg)
    NF = m["NF"]; SX, SY = cfg["SX"], cfg["SY"]
    heights = cfg["heights"]; clad = cfg.get("clad", 0.0)
    extra = cfg.get("extra_mass_floors", {})
    D, L = [], []
    totD = totL = 0.0
    for b in m["beams"]:
        i, j, k, dirn, Ln = b["i"], b["j"], b["k"], b["dir"], b["L"]
        if not (1 <= k <= NF):
            continue
        n1 = E.ntag(i, j, k)
        n2 = E.ntag(i + 1, j, k) if dirn == "X" else E.ntag(i, j + 1, k)
        c1, c2 = coords.get(n1), coords.get(n2)
        if c1 is None or c2 is None:
            continue
        roof = (k == NF)
        pD = (cfg["D_roof"] if roof else cfg["D_floor"]) + extra.get(k, 0.0)
        pL = 0.0 if roof else cfg["L_floor"]
        nb = SM._bays_adjacent(m["present"].get(k, set()), i, j, dirn)
        other = SY if dirn == "X" else SX
        wcap = other / 2.0
        th = heights[k - 1] / 12.0; th = th / 2.0 if roof else th
        wclad = clad * th / 12000.0 if (clad and nb == 1) else 0.0
        nseg = len(b["segs"])
        wD, wL = [], []
        for s in range(nseg):
            smid = Ln * (s + 0.5) / nseg
            width_in = min(smid, Ln - smid, wcap)
            base = nb * (width_in / 12.0) / 12000.0        # psf -> kip/in per psf
            wD.append(round(pD * base + wclad, 6))
            wL.append(round(pL * base, 6))
            totD += wD[-1] * Ln / nseg; totL += wL[-1] * Ln / nseg
        seg = [round(c1[0], 1), round(c1[1], 1), round(c1[2], 1),
               round(c2[0], 1), round(c2[1], 1), round(c2[2], 1)]
        if any(wD):
            D.append(seg + [wD])
        if any(wL):
            L.append(seg + [wL])
    return {"D": {"beams": D, "total": round(totD, 0)},
            "L": {"beams": L, "total": round(totL, 0)}}


def _loads(cfg, info, coords, T1):
    import engine3d as E
    out = {}
    NF = len(cfg["heights"]); z = E.zlevels(cfg)
    present = info.get("present") or {}
    # ---- dynamic model gravity: level totals spread over the grid points
    ptsD, ptsL, totD, totL = [], [], 0.0, 0.0
    for k in range(1, NF + 1):
        pts = list(present.get(k) or [])
        if not pts:
            continue
        area = E.floor_area_ft2(cfg, k)
        Dk = E.floor_w(cfg, k)
        Lk = ((cfg.get("Lr") or 20.0) if k == NF else cfg.get("L_floor", 50.0)) * area / 1000.0
        pD, pL = Dk / len(pts), Lk / len(pts)
        totD += Dk; totL += Lk
        for (i, j) in pts:
            c = coords.get(E.ntag(i, j, k))
            if c:
                ptsD.append([round(c[0], 1), round(c[1], 1), round(c[2], 1), round(pD, 2)])
                ptsL.append([round(c[0], 1), round(c[1], 1), round(c[2], 1), round(pL, 2)])
    out["DLdyn"] = {"pts": ptsD, "total": round(totD, 0)}
    out["LLdyn"] = {"pts": ptsL, "total": round(totL, 0)}
    # ---- wind story forces (both directions)
    try:
        FX = E.wind_forces(cfg, "X"); FY = E.wind_forces(cfg, "Y")
        out["wind"] = {"z": [z[k] for k in range(1, NF + 1)],
                       "X": [round(FX[k], 1) for k in range(1, NF + 1)],
                       "Y": [round(FY[k], 1) for k in range(1, NF + 1)]}
    except Exception:
        pass
    # ---- ELF seismic story forces
    try:
        Cs, V, Tu, Ta, kk, Fk, Wt = E.elf(cfg, T1)
        out["seis"] = {"z": [z[k] for k in range(1, NF + 1)],
                       "F": [round(Fk[k], 1) for k in range(1, NF + 1)],
                       "V": round(V, 1), "Cs": round(Cs, 4)}
    except Exception:
        pass
    # ---- static model true tributary gravity (built LAST: build_static wipes the ops domain)
    try:
        out["stat"] = _static_gravity(cfg, coords)
    except Exception:
        out["stat"] = None
    return out


# --------------------------------------------------------------- data build
def _viewer_data(cfg, name, root):
    import engine3d as E
    ms = E.mode_shapes(cfg, 6)
    info = E.build(cfg, "Linear")           # fresh build: beam_rel / col_dir / present snapshots
    beam_rel = info.get("beam_rel") or {}
    col_dir = info.get("col_dir") or {}
    moment_nodes = info.get("moment_nodes") or set()
    coords = {t: tuple(c[:3]) for t, c in ms["coords"].items()}
    nmodes = min(6, ms.get("nmodes", len(ms["T"])))
    kindmap = {"col": "column", "beam": "beam", "brace": "brace", "column": "column"}

    used = set()
    elements = []
    dem = _demands(root)
    Fy = float(cfg.get("Fy", 50.0))
    for (tag, kind, sec, n1, n2) in ms["ele"]:
        if n1 not in coords or n2 not in coords:
            continue
        used.update((n1, n2))
        x1, y1, z1 = coords[n1]; x2, y2, z2 = coords[n2]
        L = math.dist((x1, y1, z1), (x2, y2, z2))
        rec = {"tag": tag, "type": kindmap.get(kind, "beam"), "sec": sec,
               "n": [n1, n2], "L": round(L, 1), "util": None, "combo": None, "f": {}}
        if rec["type"] == "beam":
            rel = beam_rel.get(tag)
            if rel:
                rec["fe"] = 1 if rel[0] == "none" else 0
                rec["rel"] = {"none": "fixed-fixed", "both": "pinned-pinned",
                              "I": "pinned-fixed", "J": "fixed-pinned"}.get(rel[0], rel[0])
            else:                                     # fallback: node-level moment snapshot
                rec["fe"] = 1 if (n1 in moment_nodes and n2 in moment_nodes) else 0
        elif rec["type"] == "column":
            wd = col_dir.get(tag)
            if wd in ("X", "Y"):
                rec["wd"] = wd
        d = dem.get(tag)
        if d:
            Pc = float(d.get("P_comp_kip") or 0); Pt = float(d.get("P_tens_kip") or 0)
            Mx = float(d.get("Mx_kipft") or 0);   My = float(d.get("My_kipft") or 0)
            u = _dc("col" if rec["type"] == "column" else rec["type"],
                    sec, L, Pc, Pt, Mx * 12.0, My * 12.0, Fy)
            if u is not None:
                rec["util"] = round(u, 3)
            rec["combo"] = d.get("governing_combo")
            if rec["type"] == "brace":
                rec["f"] = {"N": round(-Pc if Pc >= Pt else Pt, 1)}
            else:
                rec["f"] = {"P": round(max(Pc, Pt), 1), "Mx": round(Mx, 1),
                            "My": round(My, 1), "V": round(float(d.get("V_kip") or 0), 1)}
        elements.append(rec)

    zs = sorted({round(coords[t][2], 1) for t in used})
    levels = list(zs)
    fixed = [t for t in used if abs(coords[t][2] - levels[0]) < 1e-6]

    # mode shapes; patch rigid-diaphragm slaves from their master
    masters = [t for t in coords if t not in used]
    modes = []
    for m in range(nmodes):
        shape = {t: list(ms["ev"][t][m][:3]) for t in used}
        for mt in masters:
            evm = ms["ev"].get(mt)
            if not evm or len(evm[m]) < 6:
                continue
            xm, ym, zm = coords[mt]
            vx, vy, rz = evm[m][0], evm[m][1], evm[m][5]
            for t in used:
                x, y, zc = coords[t]
                if abs(zc - zm) < 1e-6 and abs(shape[t][0]) + abs(shape[t][1]) < 1e-12:
                    shape[t][0] = vx - rz * (y - ym)
                    shape[t][1] = vy + rz * (x - xm)
        mx = max(max(abs(c) for c in s) for s in shape.values()) or 1.0
        modes.append({"T": round(ms["T"][m], 4),
                      "shape": {str(t): [round(c / mx, 4) for c in s] for t, s in shape.items()}})

    # slabs: one translucent quad per grid bay whose 4 corners have nodes
    slabs = []
    for z in levels[1:]:
        pts = {(round(coords[t][0], 1), round(coords[t][1], 1)) for t in used
               if abs(coords[t][2] - z) < 1e-6}
        xs = sorted({p[0] for p in pts}); ys = sorted({p[1] for p in pts})
        for a in range(len(xs) - 1):
            for b in range(len(ys) - 1):
                if all(p in pts for p in [(xs[a], ys[b]), (xs[a + 1], ys[b]),
                                          (xs[a], ys[b + 1]), (xs[a + 1], ys[b + 1])]):
                    slabs.append([xs[a], xs[a + 1], ys[b], ys[b + 1], z])

    loads = _loads(cfg, info, coords, ms["T"][0])

    base = str((cfg.get("model") or {}).get("bases") or cfg.get("base") or "pinned").lower()
    support_label = "Supports (%s)" % ("fixed" if "fix" in base else "pinned")
    model_used = _model_used(root)

    have_dem = sum(1 for e in elements if e["util"] is not None)
    umax = max((e["util"] for e in elements if e["util"] is not None), default=None)
    nfe = sum(1 for e in elements if e.get("fe"))
    stats = [["Nodes / elements", "%d / %d" % (len(used), len(elements))],
             ["Periods T1-T3", " / ".join("%.3f" % t for t in ms["T"][:3]) + " s"],
             ["Members with demands", "%d of %d" % (have_dem, len(elements))],
             ["Fixed-ended beams", str(nfe)]]
    if umax is not None:
        stats.append(["Max D/C (screening)", "%.2f" % umax])
    if loads.get("seis"):
        stats.append(["Seismic base shear V", "%.0f k (Cs=%.3f)" %
                      (loads["seis"]["V"], loads["seis"]["Cs"])])
    if loads.get("wind"):
        stats.append(["Wind base shear X / Y", "%.0f / %.0f k" %
                      (sum(loads["wind"]["X"]), sum(loads["wind"]["Y"]))])

    return {
        "meta": {
            "title": "%s — %s" % (name, cfg.get("arch", "")),
            "subtitle": "%d nodes · %d elements · T₁=%.3f s · units: kip, in" %
                        (len(used), len(elements), ms["T"][0]),
            "model_line": ("designed with model: %s" % model_used) if model_used else "",
            "support_label": support_label,
            "levels": levels, "stats": stats,
            "demand_src": ("demands: design/member_schedule.csv (governing combos)" if dem
                           else "no member_schedule.csv found — run the design pipeline"),
            "caveat": ("Demands are the pipeline's governing-combo envelopes from "
                       "design/member_schedule.csv; D/C here is a screening check (AISC 360 E3 "
                       "with K=1, phi*Mp bending with no LTB reduction, H1 interaction). "
                       "The calc package is the check of record. North = +Y (report convention). "
                       "Drag = orbit, scroll = zoom, right-drag = pan, click a member for details."),
        },
        "nodes": {str(t): [round(c, 1) for c in coords[t]] for t in used},
        "fixed": fixed, "elements": elements, "modes": modes, "slabs": slabs,
        "loads": loads,
    }


# --------------------------------------------------------------- html assembly
def standalone_html(cfg, name, root=None):
    root = root or os.getcwd()
    data = _viewer_data(cfg, name, root)
    tpl = open(os.path.join(_DIR, "viewer3d_template.html"), encoding="utf-8").read()
    three = open(os.path.join(_DIR, "vendor", "three.min.js"), encoding="utf-8").read()
    return (tpl.replace("__TITLE__", _html.escape(str(name)))
               .replace("__THREE__", three)
               .replace("__DATA__", json.dumps(data, separators=(",", ":"))))


def report_section(cfg, name, root):
    """Write <root>/viewer_3d.html and return an orange 'View model' button (opens a new tab)."""
    doc = standalone_html(cfg, name, root)
    with open(os.path.join(root, "viewer_3d.html"), "w", encoding="utf-8") as f:
        f.write(doc)
    return ("<p><a href='viewer_3d.html' target='_blank' rel='noopener' style=\""
            "display:inline-block;background:%s;color:#fff;font-weight:600;"
            "padding:9px 22px;border-radius:8px;text-decoration:none\">"
            "View model</a> <span class='note' style='margin-left:8px'>interactive 3D viewer "
            "(geometry, mode shapes, loads, member D&#8725;C) &mdash; opens in a new tab</span></p>"
            % ORANGE)

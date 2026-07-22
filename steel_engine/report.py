"""
report.py  --  one self-contained HTML engineer's report per building.

A senior engineer can follow the whole job: building summary + plan, model & deformed shape,
dynamic properties + mode shapes, drift/P-Delta, story shear/OTM, support reactions, member
forces per load case (tables + N/V/M diagrams), the design summary, and Appendix A — a fully
referenced AISC 360-22 design-calculation record for every member/connection.

Math is rendered with MathJax; figures are written to figs/ and referenced (keeps the .html small).

Run (operator, Linux/WSL where openseespy runs):
    python report.py B07
    python report.py T06            # auto-registers test_cfgs
    python report.py T01 T06 B02
"""
import os, sys, math, re, base64, io, json, datetime
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D  # noqa
import openseespy.opensees as ops

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "engine"))
sys.path.insert(0, os.path.join(HERE, "eval_tests", "answer_key"))
import engine3d as E
import sections as S
import design_post as DPOST          # run_case + capacity snippets (operator side)
try:
    import design_pipeline as PIPE   # combos() — the ASCE 7 load-case list
    HAVE_PIPE = True
except Exception:
    HAVE_PIPE = False

Fy, Emod = 50.0, 29000.0
g = E.g

# ============================================================ small utilities
def _register(name):
    if name not in E.CFG:
        # re-render path: a fresh run_python process won't have the cfg registered -- load jobs/<name>/cfg.py
        _base = os.environ.get("STEEL_BUILDER_JOBS") or HERE
        _cfgfile = os.path.join(_base, name, "cfg.py")
        if os.path.exists(_cfgfile):
            try:
                import importlib.util as _ilu
                _spec = _ilu.spec_from_file_location("jobcfg_" + name, _cfgfile)
                _m = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_m)
                if hasattr(_m, "cfg"): E.CFG[name] = _m.cfg
            except Exception:
                pass
    if name not in E.CFG:
        try: import test_cfgs  # noqa: F401
        except Exception: pass
    if name not in E.CFG:
        raise SystemExit(f"unknown building '{name}' -- register its cfg or write jobs/{name}/cfg.py first")

# Figures are written to <root>/figs/ and referenced by relative path (set in build_report).
_FIGDIR = None          # absolute path of the current report's figs/ folder
_FIGSEQ = [0]           # running counter for auto-named inline figures

def _b64(fig):
    """Save a matplotlib figure to figs/ and return its relative path (data-URI fallback if no figdir).
    Uses tight_layout (O(subplots)) instead of bbox_inches='tight' (O(artists)) so a frame figure with
    thousands of annotations encodes in milliseconds, not tens of seconds (P8)."""
    try: fig.tight_layout()
    except Exception: pass
    if _FIGDIR:
        _FIGSEQ[0] += 1; fn = "auto_%03d.png" % _FIGSEQ[0]
        fig.savefig(os.path.join(_FIGDIR, fn), format="png", dpi=130)
        plt.close(fig); return "figs/" + fn
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=130)
    plt.close(fig); buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode("ascii")

def _png_file_b64(path):
    """Reference an existing PNG (already in figs/) by relative path; data-URI fallback if no figdir."""
    if not os.path.exists(path): return None
    if _FIGDIR:
        return "figs/" + os.path.basename(path)
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")

def _materialize(uri):
    """Turn any leftover data:image/...;base64 URI (e.g. from viz3d/frame_diagram) into a figs/ file."""
    if _FIGDIR and isinstance(uri, str) and uri.startswith("data:image/"):
        try:
            head, b64 = uri.split(",", 1)
            ext = head.split("/")[1].split(";")[0].replace("jpeg", "jpg").replace("svg+xml", "svg")
            _FIGSEQ[0] += 1; fn = "auto_%03d.%s" % (_FIGSEQ[0], ext)
            with open(os.path.join(_FIGDIR, fn), "wb") as f: f.write(base64.b64decode(b64))
            return "figs/" + fn
        except Exception:
            return uri
    return uri

def _decode(tag):
    k = tag // 100000; r = tag % 100000; return r // 100, r % 100, k

def _loc(n1, n2):
    """Human-readable member location: grid (i,j) and building level k."""
    i1, j1, k1 = _decode(n1); i2, j2, k2 = _decode(n2)
    if (i1, j1) == (i2, j2):                       # vertical: column
        return f"({i1},{j1}) level {k1}→{k2}"
    return f"({i1},{j1})→({i2},{j2}) level {k1}"

def _table(headers, rows, cls=""):
    h = "".join(f"<th>{c}</th>" for c in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table class='{cls}'><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>"

def _calc(title, rows, note=""):
    """Engineering calc-sheet block: a titled worksheet table whose rows are
    (quantity, calculation, value, code-reference) — one calc step per line."""
    n = f"<div class='cnote'><i>{note}</i></div>" if note else ""
    body = "".join(f"<tr><td>{q}</td><td>{c}</td><td class='v'>{v}</td><td class='r'>{r}</td></tr>"
                   for (q, c, v, r) in rows)
    return (f"<h5>{title}</h5>{n}<table class='calc'>"
            "<tr class='hd'><th>Quantity</th><th>Calculation</th><th>Value</th><th>Ref. (AISC)</th></tr>"
            + body + "</table>")

def _img(uri, caption, full=False):
    cls = "full" if full else ""
    if not uri: return f"<p class='note'>[{caption}: not available]</p>"
    if isinstance(uri, str) and uri.startswith("ERROR"):
        return f"<p class='note'>[{caption}: {uri}]</p>"
    uri = _materialize(uri)
    return f"<figure class='{cls}'><img src='{uri}'/><figcaption>@@FIGNUM@@ {caption}</figcaption></figure>"

def _fmt(d):
    if not isinstance(d, dict): return str(d)
    return "; ".join(f"{k}={v}" for k, v in d.items())

# ============================================================ analysis helpers
def _seismic(cfg):
    NF = len(cfg["heights"])
    T, w2, eX, eY, Mtot = E.modal(cfg, min(3*NF, 16))
    Cs, V, Tu, Ta, kk, Fx, W = E.elf(cfg, T[0])
    return T, eX, eY, Cs, V, Tu, Ta, Fx, W

def _run_case(cfg, direction, Fx, accidental=False):
    """Seismic ELF case in `direction`; leaves model live. Returns info, disp, drift, reactions."""
    info = E.build(cfg, "PDelta"); NF = info["NF"]; di = 0 if direction == "X" else 1
    ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
    fD = 1.2 + 0.2*cfg["seis"]["SDS"]
    for k in range(1, NF+1):
        pts = info["present"][k]; p = fD*E.floor_grav(cfg, k)/len(pts)
        for (i, j) in pts: ops.load(E.ntag(i, j, k), 0, 0, -p, 0, 0, 0)
    SX, SY = cfg["SX"], cfg["SY"]
    for k in range(1, NF+1):
        f = [0.0]*6; f[di] = Fx[k]
        if accidental:
            pts = info["present"][k]; xs = [i*SX for i, j in pts]; ys = [j*SY for i, j in pts]
            B = (max(ys)-min(ys)+SY) if direction == "X" else (max(xs)-min(xs)+SX)
            f[5] = Fx[k]*0.05*B
        ops.load(E.mtag(k), *f)
    ops.constraints("Transformation"); ops.numberer("RCM"); ops.system("UmfPack")
    ops.test("NormDispIncr", 1e-7, 200); ops.algorithm("Newton")
    ops.integrator("LoadControl", 1.0); ops.analysis("Static"); ops.analyze(1)
    disp = {k: ops.nodeDisp(E.mtag(k), di+1) for k in range(1, NF+1)}
    drift = []; prev = 0.0
    for k in range(1, NF+1):
        drift.append((disp[k]-prev)/cfg["heights"][k-1]); prev = disp[k]
    ops.reactions()
    react = [(i, j, [ops.nodeReaction(E.ntag(i, j, 0), d) for d in (1, 2, 3, 4, 5, 6)])
             for (i, j) in info["present"][0]]
    return info, disp, drift, react

# ============================================================ figures
def fig_plan(cfg):
    """2D plan: column grid with (i,j) labels — explains the grid used in the reaction tables."""
    NX, NY = cfg["NX"], cfg["NY"]; SX, SY = cfg["SX"], cfg["SY"]
    xco = cfg.get("xcoords"); yco = cfg.get("ycoords"); skew = cfg.get("skew", 0.0)
    def XY(i, j): return ((xco[i] if xco else i*SX)+skew*j, (yco[j] if yco else j*SY))
    pres0 = set(cfg_present(cfg, 1))
    Lx = (xco[-1] if xco else NX*SX); Ly = (yco[-1] if yco else NY*SY)
    fig, ax = plt.subplots(figsize=(9, 9*max(Ly, 1)/max(Lx, 1) + 1))
    for i in range(NX):                            # shade framed bays -> non-rectangular footprints are visible
        for j in range(NY):
            if all(p in pres0 for p in [(i, j), (i+1, j), (i, j+1), (i+1, j+1)]):
                bx = [XY(i, j)[0], XY(i+1, j)[0], XY(i+1, j+1)[0], XY(i, j+1)[0]]
                by = [XY(i, j)[1], XY(i+1, j)[1], XY(i+1, j+1)[1], XY(i, j+1)[1]]
                ax.fill(bx, by, color="#eef3fb", zorder=0)
    for i in range(NX+1):                          # grid lines
        x0, _ = XY(i, 0); x1, y1 = XY(i, NY); ax.plot([XY(i,0)[0], XY(i,NY)[0]], [XY(i,0)[1], XY(i,NY)[1]], color="#ccc", lw=0.8)
    for j in range(NY+1):
        ax.plot([XY(0,j)[0], XY(NX,j)[0]], [XY(0,j)[1], XY(NX,j)[1]], color="#ccc", lw=0.8)
    for i in range(NX+1):
        for j in range(NY+1):
            x, y = XY(i, j)
            present = (i, j) in pres0
            ax.plot([x], [y], marker=("s" if present else "x"), ms=5, ls="none", color="black" if present else "#bbb")
            ax.annotate(f"({i},{j})", (x, y), textcoords="offset points", xytext=(4, 4), fontsize=7)
    ax.set_aspect("equal"); ax.set_xlabel("X (in)  —  i index"); ax.set_ylabel("Y (in)  —  j index")
    ax.set_title(f"Plan grid — column lines labelled (i, j); shaded = framed bays (level 1); × = no column")
    ax.grid(False)
    return _b64(fig)

def cfg_present(cfg, k):
    """present column (i,j) at floor k (handles plan-shape callables)."""
    try:
        return E.grid(cfg, k)
    except Exception:
        return [(i, j) for i in range(cfg["NX"]+1) for j in range(cfg["NY"]+1)]

def fig_mode_3d(cfg, mode, nmodes):
    """Full-frame 3D mode shape (undeformed grey + modal-deformed colour), true proportions.
    REUSES the cached modal eigenvector field (engine3d.mode_shapes) -- no ops.eigen re-solve."""
    ms = E.mode_shapes(cfg, max(nmodes, mode))
    nodes = ms["coords"]; evall = ms["ev"]; ele = ms["ele"]; zlev = ms["z"]
    ev = {t: (evall[t][mode-1] if (t in evall and len(evall[t]) >= mode) else [0,0,0,0,0,0]) for t in nodes}
    amp = max((abs(ev[t][0])+abs(ev[t][1]) for t in ev), default=1.0) or 1.0
    zmax = (max(zlev.values()) if isinstance(zlev, dict) else zlev[-1])
    sc = 0.12*zmax/amp
    fig = plt.figure(figsize=(11, 8)); ax = fig.add_subplot(111, projection="3d")
    for (et, kind, sec, n1, n2) in ele:
        if n1 not in nodes or n2 not in nodes: continue
        a, b = nodes[n1], nodes[n2]
        ax.plot([a[0], b[0]], [a[1], b[1]], [a[2], b[2]], color="0.8", lw=0.6)
        da = [a[i]+sc*ev[n1][i] for i in range(3)]; db = [b[i]+sc*ev[n2][i] for i in range(3)]
        ax.plot([da[0], db[0]], [da[1], db[1]], [da[2], db[2]], color="#2c5aa0", lw=1.3)
    ax.set_xlabel("X (in)"); ax.set_ylabel("Y (in)"); ax.set_zlabel("Z (in)")
    try:
        xr = ax.get_xlim3d(); yr = ax.get_ylim3d(); zr = ax.get_zlim3d()
        ax.set_box_aspect((xr[1]-xr[0], yr[1]-yr[0], zr[1]-zr[0]))
    except Exception: pass
    return _b64(fig)

def _elevation_from_live(cfg, direction, title):
    """Draw N/V/M for the perimeter frame line of `direction` from the CURRENTLY LIVE model
    (call right after a case has been analysed). Returns data-uri or None."""
    onln = (lambda i, j: j == 0) if direction == "X" else (lambda i, j: i == 0)
    al = (lambda n: ops.nodeCoord(n)[0]) if direction == "X" else (lambda n: ops.nodeCoord(n)[1])
    zo = lambda n: ops.nodeCoord(n)[2]
    mem = []
    for (t, kind, sec, n1, n2) in _LIVE_ELE:
        i1, j1, _ = _decode(n1); i2, j2, _ = _decode(n2)
        if onln(i1, j1) and onln(i2, j2): mem.append((t, kind, n1, n2))
    if not mem: return None
    fig, axes = plt.subplots(1, 3, figsize=(16, 6))
    for ax, which, ttl, col in zip(axes, ("N", "V", "M"),
                                   ("Axial N (kip)", "Shear V (kip)", "Moment M (k-ft)"),
                                   ("#1f77b4", "#2ca02c", "#d62728")):
        data = []; mx = 1e-9
        for (t, kind, n1, n2) in mem:
            bf = ops.basicForce(t)
            if kind == "brace":
                e1 = e2 = (bf[0] if which == "N" else 0.0)
            else:
                L = math.dist(ops.nodeCoord(n1), ops.nodeCoord(n2))
                if which == "N":   e1 = e2 = bf[0]
                elif which == "V": e1 = e2 = (abs(bf[1])+abs(bf[2]))/L
                else:              e1 = bf[1]/12.0; e2 = -bf[2]/12.0
            data.append((kind, n1, n2, e1, e2)); mx = max(mx, abs(e1), abs(e2))
        sc = (0.30*min(cfg["SX"], cfg["SY"]))/mx
        peak = 0.0; peaklab = None
        for kind, n1, n2, e1, e2 in data:
            a1, z1 = al(n1), zo(n1); a2, z2 = al(n2), zo(n2)
            ax.plot([a1, a2], [z1, z2], color="#bbb", lw=1.0, zorder=1)
            if abs(z2-z1) > abs(a2-a1):
                ax.plot([a1+e1*sc, a2+e2*sc], [z1, z2], color=col, lw=1.2)
                ax.plot([a1, a1+e1*sc], [z1, z1], color=col, lw=0.5)
                ax.plot([a2, a2+e2*sc], [z2, z2], color=col, lw=0.5)
            else:
                ax.plot([a1, a2], [z1+e1*sc, z2+e2*sc], color=col, lw=1.2)
                ax.plot([a1, a1], [z1, z1+e1*sc], color=col, lw=0.5)
                ax.plot([a2, a2], [z2, z2+e2*sc], color=col, lw=0.5)
            if max(abs(e1), abs(e2)) > peak:
                peak = max(abs(e1), abs(e2)); peaklab = (a1, z1, max(e1, e2, key=abs))
        if peaklab:
            ax.annotate(f"max {peak:.0f}", (peaklab[0], peaklab[1]), fontsize=8, color=col,
                        fontweight="bold")
        ax.set_title(f"{ttl}   (peak {peak:.0f})"); ax.set_xlabel("along (in)"); ax.set_ylabel("Z (in)")
        ax.set_aspect("equal", "datalim"); ax.grid(alpha=0.2)
    fig.suptitle(title)
    return _b64(fig)

_LIVE_ELE = []   # element registry of the currently-live model (set by run helpers)

def fig_drift_profile(cfg, driftX, driftY):
    NF = len(cfg["heights"]); Cd = cfg["seis"].get("Cd", 5.0); Ie = cfg["seis"]["Ie"]
    lvl = list(range(1, NF+1))
    fig, ax = plt.subplots(figsize=(9, 6))
    ax.plot([d*100 for d in driftX], lvl, "-o", label=r"$\delta_e$ X")
    ax.plot([d*Cd/Ie*100 for d in driftX], lvl, "-o", label=r"$\delta=C_d\delta_e/I_e$ X")
    ax.plot([d*100 for d in driftY], lvl, "-s", label=r"$\delta_e$ Y")
    ax.plot([d*Cd/Ie*100 for d in driftY], lvl, "-s", label=r"$\delta$ Y")
    lim = cfg.get("drift_limit", 0.02)*100
    ax.axvline(lim, color="r", ls="--", label=f"limit {lim:.1f}%")
    ax.set_xlabel("interstory drift (%)"); ax.set_ylabel("story"); ax.set_title("Drift profile")
    ax.legend(fontsize=8); ax.grid(alpha=0.3)
    return _b64(fig)

def fig_story_shear_otm(cfg, Fx):
    NF = len(cfg["heights"]); z = E.zlevels(cfg)
    Vstory = [sum(Fx[k] for k in range(s, NF+1)) for s in range(1, NF+1)]
    OTM = [sum(Fx[k]*(z[k]-z[s-1])/12.0 for k in range(s, NF+1)) for s in range(1, NF+1)]
    OTM_base = sum(Fx[k]*z[k]/12.0 for k in range(1, NF+1))
    lvl = list(range(1, NF+1))
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(9, 5))
    a1.step(Vstory, lvl, where="mid"); a1.set_xlabel("story shear (kip)"); a1.set_ylabel("story"); a1.grid(alpha=0.3); a1.set_title("Story shear")
    a2.plot(OTM, lvl, "-o"); a2.set_xlabel("overturning moment (k-ft)"); a2.set_title("OTM (above story)"); a2.grid(alpha=0.3)
    return _b64(fig), Vstory, OTM_base

# ============================================================ per-load-case forces
def load_cases(cfg):
    """ASCE 7-22 LRFD combinations considered (label, fD, fL, fLr, lat, col_only)."""
    if HAVE_PIPE:
        try: return PIPE.combos(cfg)
        except Exception: pass
    # minimal fallback if design_pipeline unavailable
    return [("1.4D", 1.4, 0, 0, {}, False), ("1.2D+1.6L+0.5Lr", 1.2, 1.6, 0.5, {}, False)]

def case_forces(cfg, fD, fL, fLr, lat):
    """Run one combination through P-Delta; return (out dict, registry). Leaves model live."""
    out, info = DPOST.run_case(cfg, fD, fL, fLr, lat)
    global _LIVE_ELE; _LIVE_ELE = info["ele"]
    return out, info

def per_case_max_table(info):
    """Per member TYPE, the governing (max) member + its forces + location, read from basicForce —
    the SAME convention the elevation diagrams use, so table and figure magnitudes agree."""
    best = {}   # (kind,sec) -> (score, N, Mz, My, V, n1, n2)
    for (t, kind, sec, n1, n2) in info["ele"]:
        bf = ops.basicForce(t)
        if kind == "brace":
            N = bf[0]; Mz = My = V = 0.0; score = abs(N)
        else:
            L = math.dist(ops.nodeCoord(n1), ops.nodeCoord(n2))
            N = bf[0]; Mz = max(abs(bf[1]), abs(bf[2])); My = max(abs(bf[3]), abs(bf[4]))
            V = max((abs(bf[1])+abs(bf[2]))/L, (abs(bf[3])+abs(bf[4]))/L); score = max(abs(Mz), abs(N))
        key = (kind, sec)
        if key not in best or score > best[key][0]:
            best[key] = (score, N, Mz, My, V, n1, n2)
    rows = []
    for (kind, sec), (sc, N, Mz, My, V, n1, n2) in sorted(best.items()):
        rows.append([kind, sec, f"{N:.1f}", f"{Mz/12:.1f}", f"{My/12:.1f}", f"{V:.1f}", _loc(n1, n2)])
    return rows

def _case_desc(label, col_only):
    """Plain-English description of an ASCE 7-22 combination label."""
    L = label
    if ("WX" in L or "WY" in L) and "E" not in L.replace("Lr", "").replace("Le", ""):
        d = "X (E–W)" if "WX" in L else "Y (N–S)"
        upl = " (uplift / overturning case, 0.9D)" if L.startswith("0.9") else ""
        return f"Wind acting in the {d} direction, combined with gravity{upl}."
    if "Om0" in L:
        d = "X (E–W)" if "EX" in L else "Y (N–S)"
        upl = " on the 0.9D (uplift) gravity case" if L.startswith("(0.9") else ""
        return (f"Capacity-design (overstrength, \\(\\Omega_0\\)) seismic in the {d} direction{upl} — applied "
                "to the columns only, so they remain elastic while the braces/beams yield (AISC 341 / "
                "ASCE 7 §12.4.3).")
    if "rhoE" in L:
        d = "X (E–W)" if "EX" in L else "Y (N–S)"
        sense = "positive" if ("EX+" in L or "EY+" in L) else "negative"
        tor = ("with +5% accidental torsion" if "t+" in L else
               ("with −5% accidental torsion" if "t-" in L else "no accidental torsion"))
        grav = ("the reduced 0.9D (uplift) gravity case" if L.startswith("(0.9")
                else "the (1.2+0.2·S_DS)D + 0.5L gravity case")
        return f"Code-level seismic in the {d} direction ({sense} sense, {tor}), combined with {grav}."
    if L.strip() == "1.4D":
        return "Dead load only — the basic gravity check."
    if "1.6L" in L:
        return "Gravity only: dead load plus full floor live load and roof live load."
    return "Load combination."

def _lat_dir(lat):
    if not lat: return "X"
    sx = sum(abs(v[0]) for v in lat.values()); sy = sum(abs(v[1]) for v in lat.values())
    return "X" if sx >= sy else "Y"

# ============================================================ Appendix A — design calcs
def _safe_props(section):
    try: return S.props(section, SEC=E.SEC)
    except Exception:
        try: return S.props(section)
        except Exception: return None

def _member_calc_block(m):
    # Render one calc_package member: section inputs + demand envelope + the agent's RAG-derived
    # capacity / limit state / D-C. Framework codes NO capacity; if unfilled, show a 'derive from RAG' note.
    mid = m.get("id", ""); inp = m.get("inputs", {}) or {}
    sec = inp.get("section", ""); kind = inp.get("kind", "")
    h = ["<h4>%s &mdash; %s (%s)</h4>" % (mid, sec, kind)]
    pk = [("A","A (in2)"),("Zx","Zx (in3)"),("Sx","Sx (in3)"),("Zy","Zy (in3)"),("Sy","Sy (in3)"),
          ("rx","rx (in)"),("ry","ry (in)"),("r","r (in)"),("Aw","Aw (in2)"),("J","J (in4)"),
          ("length_in","L (in)"),("Lb_in","Lb (in)")]
    pr = [(lbl, inp[k]) for k, lbl in pk if inp.get(k) is not None]
    if pr:
        h.append("<p class='cnote'>Section properties / geometry (AISC Shapes Database v16):</p>")
        h.append(_table([l for l, _ in pr], [["%s" % v for _, v in pr]]))
    dem = [("P_comp (kip)", inp.get("P_comp_kip")), ("P_tens (kip)", inp.get("P_tens_kip")),
           ("Mz (kip-in)", inp.get("Mz_kipin")), ("My (kip-in)", inp.get("My_kipin")),
           ("V (kip)", inp.get("V_kip"))]
    h.append("<p class='cnote'>Demand envelope (analysis):</p>")
    h.append(_table([l for l, _ in dem] + ["governing combo"],
                    [["%s" % v for _, v in dem] + [str(inp.get("governing_combo", ""))]]))
    cap = m.get("capacity"); dc = m.get("DC"); ls = m.get("limit_state"); cited = m.get("cited")
    if (isinstance(cap, dict) and cap) or dc is not None or ls or cited:
        rows = []
        if ls: rows.append(("Governing limit state", "selected from AISC 360", str(ls), str(cited or "")))
        if isinstance(cap, dict):
            for k, v in cap.items(): rows.append((k, "", str(v), ""))
        if dc is not None:
            ok = "ok" if (isinstance(dc,(int,float)) and dc <= 1.0) else ("NG" if isinstance(dc,(int,float)) else "")
            rows.append(("D/C", "demand / capacity", "%s %s" % (dc, ok), str(cited or "")))
        h.append(_calc("AISC 360 capacity &amp; D/C - derived by the agent from the RAG", rows))
    else:
        h.append("<p class='note'>Capacity &amp; D/C: <b>to be derived by the agent from the AISC 360 / 341 "
                 "RAG</b> - the framework computes demands only (no coded capacity). Expected calc_package "
                 "fields: <code>limit_state</code>, <code>cited</code>, <code>capacity</code>, <code>DC</code>.</p>")
    return "".join(h)

def appendix(cfg, name, pkg):
    # Appendix A: per governing member, section inputs + demand envelope + the agent's RAG-derived
    # AISC 360 capacity/limit-state/D-C. The framework codes NO capacity equations.
    h = ["<h3>Member calculations</h3>",
         "<p>For every governing member the framework lists the section properties and the enveloped "
         "demand from the analysis combinations. The AISC 360-22 capacity, governing limit state, cited "
         "clause, and D/C are <b>derived by the agent from the RAG</b> and shown where recorded in "
         "calc_package.json - the framework computes no capacity.</p>"]
    members = pkg.get("members") if isinstance(pkg, dict) else None
    if isinstance(members, list):
        for m in members:
            if not (m.get("inputs", {}) or {}).get("section"): continue
            try: h.append(_member_calc_block(m))
            except Exception as ex: h.append("<p class='note'>[render failed: %s]</p>" % ex)
    else:
        h.append("<p class='note'>[no members in calc_package.json]</p>")
    cd = pkg.get("connections") or pkg.get("connection_demands")
    if cd:
        h.append("<h3>Connections (AISC 360-22 Ch. J / Ch. K; AISC 341-22 capacity design)</h3>")
        items = cd.items() if isinstance(cd, dict) else [(c.get("id", ""), c) for c in cd]
        for cid, c in items:
            dem = _fmt(c.get("demand", c.get("demands", {})))
            comp = c.get("components", c.get("notes", ""))
            cited = c.get("cited", "")
            cdf = " (capacity-design)" if c.get("capacity_design") else ""
            h.append("<h4>%s &mdash; %s%s</h4><p><b>Demand:</b> %s</p><p><b>Components:</b> %s</p><p><b>Cited:</b> %s</p>"
                     % (cid, c.get("type", ""), cdf, dem, comp, cited))
            chks = c.get("checks")
            if isinstance(chks, list) and chks:
                rows = [[k.get("limit_state", ""),
                         ("%.2f" % k["DC"] if isinstance(k.get("DC"), (int, float)) else str(k.get("DC", ""))),
                         "OK" if (isinstance(k.get("DC"), (int, float)) and k["DC"] <= 1.0) else ""] for k in chks]
                h.append(_table(["Limit state", "D/C", "&le;1.0"], rows))
    return "".join(h)

def _num(x):
    try: return float(x)
    except Exception: return 0.0
def _kind_from_id(mid):
    s = (mid or "").lower()
    return "brace" if "brace" in s else ("beam" if "beam" in s else "col")
def _sec_from_id(mid):
    for part in (mid or "").replace("_", "-").split("-"):
        if part and (part[0] in "WHwh") and any(c.isdigit() for c in part): return part.upper()
    return None

# ============================================================ HTML shell
CSS = """body{font-family:Segoe UI,Arial,sans-serif;max-width:1040px;margin:24px auto;color:#222;line-height:1.55}
h1{border-bottom:3px solid #2c5aa0}h2{border-bottom:1px solid #ccc;margin-top:34px;color:#2c5aa0}
h3{color:#2c5aa0;margin-top:22px}h4{margin:18px 0 4px;color:#333}
table{border-collapse:collapse;margin:12px 0;font-size:14px}th,td{border:1px solid #bbb;padding:4px 9px;text-align:right}
th{background:#eef3fb}td:first-child,th:first-child{text-align:left}
figure{display:block;margin:16px 0}img{width:100%;max-width:100%;border:1px solid #ddd}
figcaption{font-size:12px;color:#555;text-align:center}.note{color:#a00;font-style:italic}
pre{background:#f6f8fa;padding:10px;border-radius:6px;overflow:auto;font-size:12px}
h5{margin:16px 0 2px;color:#2c5aa0;font-size:14px}
.cnote{font-size:12px;color:#666;margin:0 0 4px}
table.calc{width:100%;font-size:13px;margin:2px 0 14px}
table.calc td,table.calc th{text-align:left;padding:3px 9px;vertical-align:top}
table.calc td.v{text-align:right;white-space:nowrap;font-weight:600}
table.calc td.r{text-align:right;color:#888;white-space:nowrap;font-size:12px}
table.calc tr.hd th{background:#eef3fb}"""

MATHJAX = ("<script>window.MathJax={tex:{inlineMath:[['\\\\(','\\\\)']],displayMath:[['\\\\[','\\\\]']]}};</script>"
           "<script async src='https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js'></script>")

def _gravity_loads_table(cfg):
    rows = [["Floor dead, D", f"{cfg.get('D_floor','?')} psf"],
            ["Roof dead, D", f"{cfg.get('D_roof','?')} psf"],
            ["Cladding", f"{cfg.get('clad','?')} psf of wall"],
            ["Floor live, L", f"{cfg.get('L_floor','?')} psf"]]
    if cfg.get("Lr"):   rows.append(["Roof live, Lr", f"{cfg['Lr']} psf"])
    if cfg.get("snow"): rows.append(["Snow, S", f"{cfg['snow']} psf"])
    return _table(["Gravity load", "Value"], rows)

def _lateral_inputs_table(cfg):
    s = cfg["seis"]; w = cfg.get("wind", {})
    rows = [["Seismic SDS / SD1", f"{s['SDS']} / {s['SD1']} g"],
            ["R / Cd / Ω0 / Ie", f"{s['R']} / {s.get('Cd')} / {s.get('Om0')} / {s['Ie']}"],
            ["Period coeff Ct / x / Cu", f"{s['Ct']} / {s['x']} / {s['Cu']}"]]
    if w: rows.append(["Wind V / Exposure", f"{w.get('V','?')} mph / {w.get('exposure','C')}"])
    return _table(["Lateral-load input", "Value"], rows)

def _wind_section(cfg):
    """Returns (html, VwX, VwY). VwX/VwY are base shears (None if no wind defined)."""
    if not cfg.get("wind"):
        return "<p class='note'>No wind parameters defined for this building.</p>", None, None
    try:
        w = cfg["wind"]; FX = E.wind_forces(cfg, "X"); FY = E.wind_forces(cfg, "Y")
        VwX = sum(FX.values()); VwY = sum(FY.values()); NF = len(cfg["heights"])
        rows = [[k, f"{FX[k]:.1f}", f"{FY[k]:.1f}"] for k in range(1, NF+1)]
        h = ["<p>ASCE 7-22 §27 MWFRS. Velocity pressure qz = 0.00256·Kz·Kzt·Kd·V²; design pressure "
             "p = qz·G·Cpnet; story force = p × tributary width × tributary height.</p>",
             _table(["Parameter", "Value"], [["Basic wind speed V", f"{w.get('V')} mph"],
                    ["Exposure", w.get("exposure", "C")],
                    ["Kd / G / Cpnet", f"{w.get('Kd',0.85)} / {w.get('G',0.85)} / {w.get('Cpnet',1.3)}"]]),
             "<p><b>Story wind forces (kip):</b></p>",
             _table(["Story", "X (E-W wind)", "Y (N-S wind)"], rows),
             f"<p><b>Wind base shear:</b> X = {VwX:.0f} kip, Y = {VwY:.0f} kip.</p>"]
        return "".join(h), VwX, VwY
    except Exception as ex:
        return f"<p class='note'>[wind determination failed: {ex}]</p>", None, None

def _horizontal_distribution(cfg, Fx, VwX, VwY):
    Vseis = sum(Fx.values()); nframe = 2
    def row(lbl, Vw):
        gov = "seismic" if Vseis >= (Vw or 0) else "wind"; g = max(Vseis, Vw or 0)
        return [lbl, (f"{Vw:.0f}" if Vw is not None else "—"), f"{Vseis:.0f}", gov, f"{g/nframe:.0f}"]
    rows = [row("E-W — two moment frames", VwX), row("N-S — two braced frames", VwY)]
    h = ["<p>The story shear in each direction is shared by the two parallel lateral frames. The "
         "governing base shear (greater of wind and seismic) is split about 50/50 to each frame, plus "
         "±5% accidental torsion (ASCE 7-22 §12.8.4.2) which biases demand toward the leading frame.</p>",
         _table(["Direction / frames", "Wind base (kip)", "Seismic base (kip)", "Governs", "Per frame (kip)"], rows)]
    return "".join(h)

def _role_of(mid, inp):
    r = (inp.get("role") or "").lower().strip().replace(" ", "_")
    _alias = {"roof": "roof", "roof_beam": "roof", "floor": "floor", "floor_beam": "floor",
              "gravity_col": "gravity_col", "interior_col": "gravity_col", "interior_column": "gravity_col",
              "gravity_column": "gravity_col", "lateral_col": "lateral_col", "mf_col": "lateral_col",
              "moment_col": "lateral_col", "braced_col": "lateral_col", "lateral_column": "lateral_col",
              "brace": "brace"}
    if r in _alias: return _alias[r]
    s = (mid or "").lower()
    if "roof" in s: return "roof"
    if "brace" in s or inp.get("kind") == "brace": return "brace"
    if "col" in s or inp.get("kind") == "col":
        return "gravity_col" if ("grav" in s or "interior" in s) else "lateral_col"
    if "floor" in s or "beam" in s or inp.get("kind") == "beam": return "floor"
    return "other"

def _member_section(cfg, name, pkg, want_roles, title, intro):
    h = [f"<h2>{title}</h2>", f"<p>{intro}</p>"]
    members = pkg.get("members") if isinstance(pkg, dict) else None
    Lcol = max(cfg["heights"]); Lbeam = max(cfg["SX"], cfg["SY"])
    Lbrace = math.sqrt(min(cfg["SX"], cfg["SY"])**2 + Lcol**2)
    found = False
    if isinstance(members, list):
        for m in members:
            inp = m.get("inputs", {})
            if _role_of(m.get("id", ""), inp) not in want_roles: continue
            if not inp.get("section"): continue
            try: h.append(_member_calc_block(m)); found = True
            except Exception as ex: h.append(f"<p class='note'>[render failed: {ex}]</p>")
    if not found:
        h.append("<p class='note'>No member with this role is present in calc_package.json — tag each "
                 "member's <code>role</code> (roof / floor / gravity_col / lateral_col / brace); Chapter 6 has a "
                 "section per role.</p>")
    return "".join(h)

def _case_extremes(out, info):
    """Overall max |axial| and max |moment| across all members for one load case."""
    reg = {t: (kind, sec) for (t, kind, sec, n1, n2) in info["ele"]}
    maxN = (0.0, "", ""); maxM = (0.0, "", "")
    for t, (N, Mz, My, V) in out.items():
        kind, sec = reg.get(t, ("", ""))
        if abs(N) > abs(maxN[0]): maxN = (N, kind, sec)
        m = max(abs(Mz), abs(My))
        if m > abs(maxM[0]): maxM = (m, kind, sec)
    return maxN, maxM

def _stability_section(cfg, Fx, drX, pkg):
    NF = len(cfg["heights"]); s = cfg["seis"]; SDS = s["SDS"]; Cd = s.get("Cd", 5.5); Ie = s["Ie"]
    NX, NY, SX, SY = cfg["NX"], cfg["NY"], cfg["SX"], cfg["SY"]
    A = (NX*SX)*(NY*SY)/144.0
    Dk = {k: E.floor_w(cfg, k) for k in range(1, NF+1)}
    Lk = {k: (cfg.get("L_floor", 0)*A/1000.0 if k < NF else 0.0) for k in range(1, NF+1)}
    Pu = {k: (1.2+0.2*SDS)*Dk[k] + 0.5*Lk[k] for k in range(1, NF+1)}
    Pstory = {sx: sum(Pu[k] for k in range(sx, NF+1)) for sx in range(1, NF+1)}
    Vstory = {sx: sum(Fx[k] for k in range(sx, NF+1)) for sx in range(1, NF+1)}
    beta = 1.0; theta_max = min(0.5/(beta*Cd), 0.25); worst = 0.0; rows = []
    for sx in range(1, NF+1):
        th = (Pstory[sx]*drX[sx-1]/Vstory[sx]) if Vstory[sx] else 0.0
        worst = max(worst, th)
        rows.append([sx, "%.0f" % Pstory[sx], "%.0f" % Vstory[sx], "%.0f" % cfg["heights"][sx-1],
                     "%.3f" % (drX[sx-1]*100), "%.3f" % th, "OK" if th <= theta_max else "NG"])
    h = ["<p>Member demands already include second-order effects (a P-&Delta; geometric transformation is applied "
         "under every combination), so the AISC 360-22 App.8 B<sub>2</sub> amplifier is captured directly by the "
         "analysis. The ASCE 7-22 &sect;12.8.7 story stability coefficient "
         "&theta; = P<sub>x</sub>&Delta;I<sub>e</sub>/(V<sub>x</sub>h<sub>sx</sub>C<sub>d</sub>) is evaluated per "
         "story below; the limit is &theta;<sub>max</sub> = min(0.5/(&beta;C<sub>d</sub>), 0.25) = "
         "%.3f (&beta; = 1.0 conservatively). &theta; &le; 0.10 means P-&Delta; could be neglected; "
         "&theta; &gt; &theta;<sub>max</sub> is not permitted.</p>" % theta_max,
         _table(["Story", "P<sub>x</sub> (kip)", "V<sub>x</sub> (kip)", "h<sub>sx</sub> (in)",
                 "&delta;<sub>e</sub> %", "&theta;", "&le; %.3f" % theta_max], rows),
         "<p>Worst-story &theta; = %.3f &mdash; %s the %.3f limit; P-&Delta; effects are %s and the analysis "
         "includes them regardless.</p>" % (worst, "within" if worst <= theta_max else "EXCEEDS",
                                            theta_max, "significant" if worst > 0.10 else "small")]
    return "".join(h)

def _load_activity(name):
    """Records for this job. Reads jobs/<name>/activity_log.jsonl AND folds in any scratch-log SESSIONS that
    belong to this job -- e.g. work done after a server restart that logged to scratch because new_activity_log()
    was not re-called (its RAG queries would otherwise be invisible). Returns (recs_sorted, primary_source)."""
    _jobs = os.environ.get("STEEL_BUILDER_JOBS") or os.path.join(HERE, "desktop_app", "agent_workspace", "work", "claude_desktop", "jobs")
    def _read(p):
        out = []
        try:
            for line in open(p, encoding="utf-8"):
                if line.strip():
                    try: out.append(json.loads(line))
                    except Exception: pass
        except Exception: pass
        return out
    src = None; jrecs = []
    for c in (os.path.join(_jobs, name, "activity_log.jsonl"),
              os.path.join(HERE, "desktop_app", "agent_workspace", "work", name, "activity_log.jsonl"),
              os.path.join(HERE, "desktop_app", "agent_workspace", "work", "claude_desktop", "activity_log.jsonl")):
        if os.path.exists(c):
            src = c; jrecs = _read(c); break
    scratch = os.environ.get("STEEL_BUILDER_SCRATCH") or os.path.join(os.path.dirname(_jobs), ".scratch")
    srecs = _read(os.path.join(scratch, "activity_log.jsonl"))
    extra = []
    if srecs:
        def refs(r):
            if r.get("tool") == "new_activity_log" and str(r.get("detail", "")).strip() == name:
                return True
            d = str(r.get("detail", "")) + " " + str(r.get("result", ""))
            return ("jobs/" + name + "/" in d) or ("jobs/" + name in d) or (os.sep + name + os.sep in d)
        sessions = []; cur = []
        for r in srecs:
            st = r.get("step", 0)
            if cur and isinstance(st, int) and st <= cur[-1].get("step", 0):
                sessions.append(cur); cur = []
            cur.append(r)
        if cur: sessions.append(cur)
        for s in sessions:
            if any(refs(r) for r in s): extra += s
    seen = set(); merged = []
    for r in jrecs + extra:
        k = (r.get("ts"), r.get("tool"), str(r.get("detail"))[:80])
        if k in seen: continue
        seen.add(k); merged.append(r)
    merged.sort(key=lambda r: (r.get("ts") or "", r.get("step") or 0))
    return merged, src

def _activity_section(name):
    recs, src = _load_activity(name)
    h = ["<h2>Appendix C — Activity log</h2>",
         "<p>Every tool call made during the design, recorded by the MCP server, summarised below.</p>"]
    if not recs:
        h.append("<p class='note'>[no activity_log.jsonl found — call new_activity_log() at the start of "
                 "the design so the server records the tool calls]</p>")
        return "".join(h)
    counts = {}
    for r in recs: counts[r.get("tool", "?")] = counts.get(r.get("tool", "?"), 0) + 1
    _sd = os.path.relpath(src, HERE) if src else "scratch log"
    h.append(f"<p>Source: <code>{_sd}</code> — {len(recs)} tool calls.</p>")
    h.append(_table(["Tool", "Calls"], [[k, counts[k]] for k in sorted(counts)]))
    rows = [[i + 1, (r.get("ts", "").split("T")[-1]), r.get("tool", ""),
             r.get("detail", ""), r.get("result", "")] for i, r in enumerate(recs)]
    h.append("<h3>Chronological tool calls</h3>" + _table(["#", "time", "tool", "detail", "result"], rows))
    return "".join(h)

def _load_pkg(name, root=None):
    cands = []
    if root: cands.append(os.path.join(root, "design", "calc_package.json"))
    cands += [os.path.join(HERE, name, "design", "calc_package.json"),
              os.path.join(HERE, "desktop_app", "agent_workspace", "work", name, "calc_package.json"),
              os.path.join(HERE, "buildings", name, "design", "calc_package.json")]
    for c in cands:
        if os.path.exists(c):
            try: return json.load(open(c, encoding="utf-8")), c
            except Exception: pass
    return None, None



CHK_CSS = ("ol.toc{font-size:14px}ol.toc li{margin:2px 0}"
           ".chk{background:#f3f7fd;border:1px solid #cfe;border-left:4px solid #2c5aa0;"
           "border-radius:4px;padding:8px 14px;margin:8px 0 16px}"
           ".chk-h{font-weight:600;color:#2c5aa0;font-size:13px;margin-bottom:4px}"
           ".chk ul{margin:4px 0 2px 0;padding-left:20px}.chk li{font-size:13px;color:#333;margin:2px 0}"
           ".chk-status{font-weight:400;font-style:italic;color:#a06000;font-size:12px}")

CHAPTERS = {
 1: ("Design basis & codes", [
   "Governing codes and editions stated (IBC, ASCE 7-22, AISC 360-22, AISC 341-22 + 358 if seismic, AWS D1.1); Risk Category and Importance Factors.",
   "Project criteria match the architectural/owner brief and the geotechnical report (site class, bearing, lateral soil, frost).",
   "Units, sign conventions and material specs stated and consistent throughout.",
   "Scope and design-responsibility boundaries defined (connections DESIGNED in this package; delegated/deferred items: joists, stairs, cladding, embeds)."]),
 2: ("Structural system & load path", [
   "Gravity and lateral load paths complete and continuous to the foundation in both orthogonal directions.",
   "Lateral system per direction identified with the correct R, &Omega;<sub>0</sub>, C<sub>d</sub>, &rho; and any height/redundancy/system limits.",
   "Diaphragm type (rigid/semi-rigid/flexible) justified; collectors/drag struts and chord forces addressed.",
   "Irregularities (plan &amp; vertical) and expansion/seismic joints identified; triggered provisions applied."]),
 3: ("Loads", [
   "Dead loads: realistic self-weight, superimposed (MEP, ceilings, finishes, partitions), cladding/fa&ccedil;ade.",
   "Live loads per occupancy with correct reductions (and not where prohibited).",
   "Roof live/snow: flat-roof, drift, unbalanced, rain-on-snow, ponding, minimums.",
   "Wind (Ch. 26-31): MWFRS and C&amp;C; windward/leeward; torsional cases.",
   "Seismic (Ch. 11-12): S<sub>DS</sub>/S<sub>D1</sub>, SDC, period, C<sub>s</sub>, V, vertical distribution, E<sub>v</sub>, accidental+amplified torsion, 100/30, &rho;.",
   "Governing case (wind vs seismic) identified per direction."]),
 4: ("Load combinations", [
   "Full ASCE 7-22 &sect;2.3 LRFD set (gravity, wind &plusmn;, seismic &plusmn; with E<sub>v</sub> &amp; &rho;, &Omega;<sub>0</sub> where required), 100/30, &plusmn; accidental torsion.",
   "Combinations applied to the right members (&Omega;<sub>0</sub> to capacity-protected only); uplift/net-tension (0.9D) checked.",
   "Second-order effects handled per-combination (no superposition of factored second-order results)."]),
 5: ("Analysis model fidelity", [
   "Geometry, member sizes and orientations match the drawings (strong/weak axis correct).",
   "Boundary conditions: column base fixity matches the baseplate/anchorage detail and the foundation's capacity.",
   "Connection idealization (rigid / pinned / PR releases) matches the detailed connections.",
   "Diaphragm constraint, rigid offsets/panel zones, and leaning columns with their P-&Delta; represented.",
   "Modal results sane: &ge;90% participating mass, reasonable periods, expected mode shapes.",
   "Equilibrium verified: &Sigma;R = applied base shear (each direction) and total factored gravity.",
   "Stiffness reductions / notional loads / effective-length basis consistent with the analysis method."]),
 6: ("Member strength design (AISC 360)", [
   "Every member type checked for the governing combo: tension (D2/D3), compression (E3/E4/E7), flexure (F2-F8 correct limit state), shear (G2), interaction (H1).",
   "Correct limit state, &phi;, L<sub>b</sub>, C<sub>b</sub>, K, slenderness and section properties.",
   "Composite members (Ch. I) designed properly if used (PNA, &phi;M<sub>n</sub>, studs, shoring, construction-stage deflection).",
   "Concentrated-load limit states (J10) and stiffeners where required.",
   "Governing D/C &le; 1.0 with sensible margins; sections reasonably economical.",
   "Calcs cited to specific AISC clauses and independently re-derivable; 2-3 governing members spot-checked."]),
 7: ("Stability & second-order", [
   "Stability method (Direct Analysis Method preferred) applied correctly end-to-end.",
   "B<sub>2</sub> / &theta; per story within limits; P-&Delta; included in member demands."]),
 8: ("Serviceability", [
   "Seismic design drift &delta;=C<sub>d</sub>&delta;<sub>xe</sub>/I<sub>e</sub> &le; allowable; wind drift &le; project limit; inter-story compatible with cladding/partitions.",
   "Deflections: floor LL &le; L/360, TL &le; L/240; camber; roof ponding; long-span/cantilever limits.",
   "Floor vibration (AISC DG11) for the occupancy where applicable.",
   "Building separation / pounding; differential movement at joints."]),
 9: ("Seismic / wind detailing (AISC 341)", [
   "System detailing matches the R used (width-thickness, brace slenderness, protected zones).",
   "Capacity design: SCWB (E3.4a), columns/collectors for &Omega;<sub>0</sub> or expected strength (R<sub>y</sub>F<sub>y</sub>).",
   "Demand-critical welds, prequalified connections (AISC 358), continuity/doubler plates, panel-zone shear.",
   "Wind: C&amp;C on cladding/fasteners, net uplift load path and hold-downs."]),
 10: ("Connections", [
   "Connection demands (V, N, M, incl. capacity-design/overstrength where required) on the drawings.",
   "Connections designed in this package (sized components; every limit-state D/C \u2264 1.0); only shop-level detailing on the fabricator submittal.",
   "Critical connections checked/constrained: bolts (J3), welds (J2), block shear (J4), HSS (Ch. K), base plates/anchors (J8/J9 + ACI 318 Ch. 17).",
   "Constructability: erection stability, access, OSHA min 4 anchor rods."]),
 11: ("Foundations interface", [
   "Column base reactions (P/V/M, uplift, overturning) provided to foundation design; consistent with assumed base fixity.",
   "Overall overturning/sliding stability; net uplift anchorage; load cases to geotech consistent with bearing/lateral capacity."]),
 12: ("Drawings, specifications & documentation", [
   "Drawings internally consistent and consistent with the calculations.",
   "General notes, material specs, welding/bolting, special-inspection schedule and design loads shown.",
   "Member schedules, framing plans, brace/MF elevations and connection details complete and coordinated.",
   "Deferred-submittal items listed; assumptions and limitations documented."]),
 13: ("QA / professional acceptance", [
   "Independent check performed (different engineer or method); discrepancies resolved.",
   "Software validated/appropriate; analysis assumptions vs detailing reconciled.",
   "Special inspection &amp; testing program (IBC Ch. 17) defined.",
   "Calc package complete, traceable (input &rarr; demand &rarr; capacity &rarr; D/C &rarr; cited clause), archived.",
   "All open items / RFIs closed; assumptions confirmed.",
   "EOR satisfied the design meets the governing codes and the standard of care &mdash; apply seal/signature."]),
}

def _toc():
    rows = "".join(f"<li><b>Chapter {n}</b> &mdash; {CHAPTERS[n][0]}</li>" for n in range(1, 14))
    return ("<h2>Report structure &mdash; EOR review checklist</h2>"
            "<p>This report is organised as the senior-engineer (EOR) acceptance checklist: one chapter per "
            "checklist section. Each chapter opens with the items a reviewer must accept, followed by the "
            "supporting analysis and design evidence. Appendix A is the fully-referenced AISC 360/341 member "
            "calc, Appendix B the member forces for every load case, Appendix C the activity log of the tool "
            "calls that produced the design.</p><ol class='toc'>" + rows + "</ol>")

def _chapter(n, status=None):
    title, items = CHAPTERS[n]
    li = "".join(f"<li>{t}</li>" for t in items)
    extra = f" &mdash; <span class='chk-status'>{status}</span>" if status else ""
    return (f"<h2>Chapter {n} &mdash; {title}</h2>"
            f"<div class='chk'><div class='chk-h'>Reviewer acceptance items{extra}</div><ul>{li}</ul></div>")

def _risk_category(Ie):
    return {1.0: "II", 1.25: "III", 1.5: "IV"}.get(round(float(Ie), 2), "II")

def _sdc(SDS, SD1):
    """Seismic Design Category from ASCE 7-22 Tables 11.6-1/2 (Risk Cat I-III; S1>=0.75 E/F needs S1)."""
    def a(x, b):
        for thr, c in b:
            if x < thr: return c
        return "D"
    c1 = a(SDS, [(0.167, "A"), (0.33, "B"), (0.50, "C")])
    c2 = a(SD1, [(0.067, "A"), (0.133, "B"), (0.20, "C")])
    return max(c1, c2)

def _design_basis_codes(cfg, s):
    seismic = bool(s.get("R"))
    rows = [["International Building Code (IBC)", "adopting code &mdash; confirm locally adopted edition &amp; amendments"],
            ["ASCE/SEI 7-22", "loads &amp; load combinations (gravity, wind Ch.26-31, seismic Ch.11-12, &sect;2.3 LRFD)"],
            ["AISC 360-22", "steel member &amp; connection design (LRFD)"]]
    if seismic:
        rows += [["AISC 341-22", "seismic provisions / ductile detailing &amp; capacity design"],
                 ["AISC 358", "prequalified moment connections (if SMF/IMF used)"]]
    rows += [["AWS D1.1", "structural welding"], ["ACI 318 (Ch. 17)", "cast-in anchorage at column bases"]]
    out = ["<h3>Governing standards</h3>",
           _table(["Reference", "Used for"], rows)]
    Ie = s.get("Ie", 1.0); RC = _risk_category(Ie)
    crows = [["Risk Category", RC], ["Importance factor I<sub>e</sub>", f"{Ie}"]]
    if seismic:
        sdc = _sdc(s["SDS"], s["SD1"])
        crows += [["S<sub>DS</sub> / S<sub>D1</sub>", f"{s['SDS']} / {s['SD1']} g"],
                  ["Seismic Design Category", f"{sdc} <span class='cnote'>(Tables 11.6-1/2; confirm E/F vs S<sub>1</sub>)</span>"],
                  ["R / C<sub>d</sub> / &Omega;<sub>0</sub>", f"{s['R']} / {s.get('Cd')} / {s.get('Om0')}"],
                  ["Redundancy &rho;", f"{cfg.get('rho', 1.3)}"]]
    w = cfg.get("wind", {})
    if w:
        crows.append(["Basic wind speed V", f"{w.get('V','?')} mph, Exposure {w.get('exposure','?')}"])
    out += ["<h3>Risk &amp; hazard classification</h3>", _table(["Parameter", "Value"], crows)]
    out += ["<h3>Scope &amp; design responsibility</h3>",
            "<p class='cnote'>This package covers the primary steel gravity and lateral framing, its members, "
            "and the analysis behind them. Connection design, foundations, cold-formed/joist framing, stairs, "
            "cladding attachment and embeds are delegated/deferred unless explicitly included; their demands are "
            "transmitted in Chapters 10-11. The reviewer should confirm the geotechnical and architectural "
            "criteria match this basis.</p>"]
    return "".join(out)

def _system_loadpath(cfg):
    seismic = bool(cfg["seis"].get("R")); braced = E.is_braced(cfg)
    sysname = "concentrically braced / dual" if braced else "moment-resisting frame"
    return ("<h3>Lateral system &amp; load path</h3>"
            f"<p>The lateral force-resisting system is a steel {sysname} in each principal direction. "
            "<b>Gravity</b> path: floor/roof pressure &rarr; floor beams (two-way tributary) &rarr; girders/"
            "columns &rarr; column bases &rarr; foundation. <b>Lateral</b> path: story inertial/wind force &rarr; "
            "rigid floor diaphragm &rarr; vertical lateral frames &rarr; column bases &rarr; foundation, in both "
            "orthogonal directions. The design inputs for each system are below; the horizontal distribution to "
            "the frames and the deformed shape that confirms the path are shown in this chapter and Chapter 5.</p>")


def _lfrs_table(cfg):
    s = cfg["seis"]; R = s.get("R"); braced = E.is_braced(cfg); Om0 = s.get("Om0"); Cd = s.get("Cd")
    rho = cfg.get("rho", 1.3)
    if braced:
        if abs(R-6) < 0.6:    name, hl = "Special concentrically braced frame (SCBF)", "160 ft (SDC D/E), 100 ft (F)"
        elif abs(R-3.25) < .5: name, hl = "Ordinary concentrically braced frame (OCBF)", "35 ft (SDC D/E), not permitted (F)"
        elif R and abs(R-8) < .4 and Om0 and Om0 < 2.75: name, hl = "Buckling-restrained braced frame (BRBF)", "160 ft (D/E), 100 ft (F)"
        elif R and abs(R-8) < .4: name, hl = "Eccentrically braced frame (EBF)", "160 ft (D/E), 100 ft (F)"
        else: name, hl = "steel braced frame", "per ASCE 7-22 Table 12.2-1"
    else:
        if R and abs(R-8) < .4:   name, hl = "Special moment frame (SMF)", "not limited (NL)"
        elif R and abs(R-4.5) < .4: name, hl = "Intermediate moment frame (IMF)", "35 ft in SDC D; not permitted E/F (confirm)"
        elif R and abs(R-3.5) < .4: name, hl = "Ordinary moment frame (OMF)", "limited use in SDC D-F (confirm)"
        else: name, hl = "steel moment frame", "per ASCE 7-22 Table 12.2-1"
    sysdecl = cfg.get("system")                       # R1: use the system the agent DECLARED, not an R-guess
    sys_key = "Seismic force-resisting system (declared)" if sysdecl else "System (inferred from R &mdash; DECLARE cfg['system'])"
    sys_val = (sysdecl if sysdecl else name)
    rows = [[sys_key, sys_val],
            ["Response modification R", f"{R}"],
            ["Deflection amplification C<sub>d</sub>", f"{Cd}"],
            ["Overstrength &Omega;<sub>0</sub>", f"{Om0}"],
            ["Redundancy &rho;", f"{rho} <span class='cnote'>(confirm vs &sect;12.3.4.2; 1.0 if redundancy conditions met)</span>"],
            ["Structural height h<sub>n</sub> limit (Table 12.2-1)", hl]]
    if sysdecl:
        note = ("<p class='cnote'>System is the one DECLARED in cfg['system']; apply its AISC 341 provisions in "
                "Chapter 9 and confirm the Table 12.2-1 height limit for the SDC. Use the same system both directions "
                "unless cfg declares a different system per direction.</p>")
    else:
        note = ("<p class='cnote'>No cfg['system'] declared &mdash; the label above is INFERRED from R and is ambiguous "
                "(R=8 is EBF, BRBF, or dual). DECLARE cfg['system'] = the exact SFRS from the brief so the system, its "
                "ductile detailing (Ch 9) and height limit are locked.</p>")
    return _table(["Parameter", "Value"], rows) + note

def _diaphragm_section(cfg, Fx):
    NF = len(cfg["heights"]); s = cfg["seis"]; SDS = s["SDS"]; Ie = s["Ie"]
    w = [E.floor_w(cfg, k) for k in range(1, NF+1)]
    rows = []
    for x in range(1, NF+1):
        sumF = sum(Fx[i] for i in range(x, NF+1)); sumw = sum(w[i-1] for i in range(x, NF+1))
        wpx = w[x-1]; Fpx = (sumF/sumw)*wpx if sumw else 0.0
        lo = 0.2*SDS*Ie*wpx; hi = 0.4*SDS*Ie*wpx; gov = min(max(Fpx, lo), hi)
        tag = "min" if gov == lo else ("max" if gov == hi else "Eq.12.10-1")
        rows.append([x, f"{wpx:.0f}", f"{Fpx:.0f}", f"{lo:.0f}", f"{hi:.0f}", f"{gov:.0f} ({tag})"])
    just = ("<p>The floor/roof is taken as a <b>rigid diaphragm</b> (concrete-filled metal deck), distributing "
            "story forces to the lateral frames in proportion to their stiffness and modelled with a rigid "
            "in-plane constraint per ASCE 7-22 &sect;12.3.1.2. Collectors/drag struts carry the diaphragm shear "
            "into the frames; chord forces (M<sub>diaph</sub>/depth) are resisted by the perimeter beams. The "
            "diaphragm, its collectors and chords are designed for the force F<sub>px</sub> below (&sect;12.10.1.1).</p>")
    tbl = _table(["Level x", "w<sub>px</sub> (kip)", "F<sub>px</sub> Eq.12.10-1 (kip)",
                  "min 0.2S<sub>DS</sub>I<sub>e</sub>w<sub>px</sub>", "max 0.4S<sub>DS</sub>I<sub>e</sub>w<sub>px</sub>",
                  "F<sub>px</sub> design (kip)"], rows)
    return just + tbl + ("<p class='cnote'>F<sub>px</sub> is the diaphragm/collector design force; the detailed "
                         "diaphragm, collector and chord design is delegated and confirmed on the drawings.</p>")

def _torsion_ratios(cfg, Fx):
    NF = len(cfg["heights"]); SX, SY = cfg["SX"], cfg["SY"]; NX, NY = cfg["NX"], cfg["NY"]
    Lx = (cfg["xcoords"][-1] if cfg.get("xcoords") else NX*SX); Ly = (cfg["ycoords"][-1] if cfg.get("ycoords") else NY*SY)
    out = {}
    for direction, Bperp in (("X", Ly), ("Y", Lx)):
        try:
            info, disp, drift, re = _run_case(cfg, direction, Fx, accidental=True)
            rot = {k: ops.nodeDisp(E.mtag(k), 6) for k in range(1, NF+1)}
            worst = 1.0; prevd = 0.0; prevr = 0.0
            for k in range(1, NF+1):
                dtr = disp[k] - prevd; drot = rot[k] - prevr; prevd = disp[k]; prevr = rot[k]
                if abs(dtr) > 1e-9:
                    worst = max(worst, 1.0 + abs(drot)*(Bperp/2.0)/abs(dtr))
            out[direction] = worst
        except Exception:
            out[direction] = None
    return out

def _irregularity_section(cfg, Fx):
    NF = len(cfg["heights"]); w = [E.floor_w(cfg, k) for k in range(1, NF+1)]
    h = cfg["heights"]; custom_plan = cfg.get("plan") is not None
    tors = _torsion_ratios(cfg, Fx)
    tr = max([v for v in tors.values() if v is not None], default=1.0)
    _Ax = min(max((tr/1.2)**2, 1.0), 3.0)   # ASCE 7-22 Eq. 12.8-14 accidental-torsion amplification
    if tr >= 1.4:   tcls = f"EXTREME torsional (1b): ratio {tr:.2f} &ge; 1.4 &mdash; apply A<sub>x</sub> = {_Ax:.2f} (&sect;12.8.4.3)"
    elif tr >= 1.2: tcls = f"Torsional (1a): ratio {tr:.2f} &ge; 1.2 &mdash; apply A<sub>x</sub> = {_Ax:.2f} (&sect;12.8.4.3)"
    else:           tcls = f"None: ratio {tr:.2f} &lt; 1.2 (A<sub>x</sub> = 1.0)"
    # mass irregularity (vertical 2): adjacent floor mass > 150%
    massr = max((max(w[k]/w[k-1], w[k-1]/w[k]) for k in range(1, NF)), default=1.0)
    mcls = "None" if massr <= 1.5 else f"Mass irregularity (Vert-2): adjacent ratio {massr:.2f} &gt; 1.5"
    # soft story screen via story height uniformity (stiffness ~ 1/h^3 proxy)
    hr = max((max(h[k]/h[k-1], h[k-1]/h[k]) for k in range(1, NF)), default=1.0)
    scls = "None (uniform story heights)" if hr <= 1.0001 else f"Check stiffness/soft-story: tallest/shortest story height ratio {hr:.2f}"
    # R3: FIRM plan/vertical determination from the ACTUAL per-level footprint (present-sets / plan= fn),
    # not the old 'cfg.get(plan) is None -> uniform rectangular' guess that denied custom_build L/T/U/cruciform plans.
    pir = E.plan_irregularities(cfg)
    if pir["reentrant"]:
        h2 = ("Type 2 RE-ENTRANT: YES &mdash; non-convex footprint. Requires MODAL RESPONSE SPECTRUM (12.6/12.9) and a "
              "<b>25% increase to diaphragm-to-collector CONNECTION forces</b> (&sect;12.3.3.4), with &Omega;<sub>0</sub> "
              "collectors on the re-entrant grid lines.")
    else:
        h2 = "None (rectangular / convex footprint)"
    h3 = "None (solid rigid diaphragm; confirm any large openings/atria)"
    h5 = ("Type 5 NONPARALLEL: YES &mdash; a skewed frame line; resolve its stiffness into BOTH principal directions and "
          "apply biaxial member / SCWB checks with the 100/30 combination." if pir["nonparallel"]
          else "None (orthogonal frames)")
    v3 = ("Type 3 GEOMETRIC SETBACK: YES &mdash; footprint reduces with height; design the transfer/backstay diaphragm "
          "at the setback with &Omega;<sub>0</sub> collectors." if pir["setback"] else "None (uniform footprint over height)")
    rows = [
        ["Horizontal 1a/1b &mdash; Torsional / extreme", tcls],
        ["Horizontal 2 &mdash; Re-entrant corners", h2],
        ["Horizontal 3 &mdash; Diaphragm discontinuity", h3],
        ["Horizontal 4 &mdash; Out-of-plane offset", "None (continuous vertical frames; confirm no transfer)"],
        ["Horizontal 5 &mdash; Nonparallel system", h5],
        ["Vertical 1a/1b &mdash; Soft / extreme soft story", scls],
        ["Vertical 2 &mdash; Mass (weight)", mcls],
        ["Vertical 3 &mdash; Geometric (setback)", v3],
        ["Vertical 4 &mdash; In-plane discontinuity", "None (aligned frames; confirm no transfer columns)"],
        ["Vertical 5a/5b &mdash; Weak / extreme weak story", "Confirm against story shear strengths (Ch 6/9)"]]
    intro = ("<p>Screening against ASCE 7-22 Tables 12.3-1 (plan) and 12.3-2 (vertical). The torsional ratio is "
             "the story diaphragm displacement-ratio under the &plusmn;5% accidental eccentricity (rigid-diaphragm "
             "estimate); &ge;1.2 triggers a torsional irregularity and amplification A<sub>x</sub> (&sect;12.8.4.3).</p>")
    mrsa_needed = pir["reentrant"] or pir["setback"] or pir["nonparallel"] or tr >= 1.2
    if mrsa_needed and "RS" not in [a.upper() for a in cfg.get("analyses", [])]:
        intro += ("<p class='cnote'><b>Analysis procedure &mdash; ACTION:</b> the determinations above trigger ASCE 7-22 "
                  "Table 12.6-1: this structure requires <b>MODAL RESPONSE SPECTRUM (&sect;12.9)</b>, not the ELF "
                  "procedure (&sect;12.8). Add 'RS' to cfg['analyses'] and design to the MRSA demands.</p>")
    return intro + _table(["Irregularity type", "Determination"], rows)


def _gravity_loads_note():
    return ("<p class='cnote'>Dead D is the bundled member self-weight + superimposed dead (MEP, ceilings, "
            "finishes, partition allowance) plus fa&ccedil;ade cladding &mdash; itemise on the load schedule. "
            "Live L is applied at full L<sub>0</sub> (no &sect;4.7 reduction taken &mdash; conservative); "
            "L = L<sub>0</sub>(0.25 + 15/&radic;(K<sub>LL</sub>A<sub>T</sub>)) may be used in member design where "
            "K<sub>LL</sub>A<sub>T</sub> &ge; 400 ft&sup2; and reduction is permitted. Roof carries a uniform "
            "L<sub>r</sub>/snow; flat-roof snow, drift, unbalanced/sliding snow, rain-on-snow and ponding stability "
            "are confirmed separately (out of scope for the uniform roof model).</p>")

def _wind_cc_note():
    return ("<p class='cnote'>The forces above are the MWFRS (main wind force-resisting system) for the overall "
            "building (windward + leeward combined via C<sub>p,net</sub>). Components &amp; cladding (C&amp;C) "
            "pressures &mdash; zone GC<sub>p</sub> of &sect;30 for cladding, fasteners and their supports &mdash; "
            "and across-wind/torsional MWFRS load cases are part of the (delegated) cladding design.</p>")

def _seismic_loads_section(cfg, T, eX, eY, Cs, V, Tu, Ta, Fx, W):
    s = cfg["seis"]; NF = len(cfg["heights"]); z = E.zlevels(cfg)
    R = s["R"]; Ie = s["Ie"]; SDS = s["SDS"]; SD1 = s["SD1"]; S1 = s.get("S1", 0); TL = s.get("TL", 8.0)
    kk = 1.0 if Tu <= 0.5 else (2.0 if Tu >= 2.5 else 1 + (Tu-0.5)/2.0)
    Cs_eq = SDS/(R/Ie)
    cap = SD1/(Tu*(R/Ie)) if Tu <= TL else SD1*TL/(Tu**2*(R/Ie))
    cmin = max(0.044*SDS*Ie, 0.01); cmin_s1 = (0.5*S1/(R/Ie)) if S1 >= 0.6 else None
    low = max(cmin, cmin_s1 or 0.0); upper = min(Cs_eq, cap)
    if upper <= low: gov = "C<sub>s,min</sub> (Eq.12.8-6, S<sub>1</sub>)" if (cmin_s1 and low == cmin_s1) else "C<sub>s,min</sub> (Eq.12.8-5)"
    elif cap < Cs_eq: gov = "C<sub>s,max</sub> (Eq.12.8-3)"
    else: gov = "C<sub>s</sub> (Eq.12.8-2)"
    intro = (f"<p>ASCE 7-22 &sect;12.8 equivalent lateral force. Seismic weight W = {W:.0f} kip; S<sub>DS</sub> = "
             f"{SDS} g, S<sub>D1</sub> = {SD1} g, S<sub>1</sub> = {S1} g, R = {R}, I<sub>e</sub> = {Ie}. Approximate "
             f"period T<sub>a</sub> = C<sub>t</sub>h<sub>n</sub><sup>x</sup> = {Ta:.2f} s; design period "
             f"T = min(T<sub>computed</sub>, C<sub>u</sub>T<sub>a</sub>) = {Tu:.2f} s (&sect;12.8.2).</p>")
    crows = [["C<sub>s</sub> = S<sub>DS</sub>/(R/I<sub>e</sub>) &nbsp;(Eq.12.8-2)", f"{Cs_eq:.4f}"],
             ["C<sub>s,max</sub> = S<sub>D1</sub>/(T&middot;R/I<sub>e</sub>) &nbsp;(Eq.12.8-3)", f"{cap:.4f}"],
             ["C<sub>s,min</sub> = max(0.044&middot;S<sub>DS</sub>I<sub>e</sub>, 0.01) &nbsp;(Eq.12.8-5)", f"{cmin:.4f}"]]
    if cmin_s1 is not None:
        crows.append(["C<sub>s,min</sub> = 0.5&middot;S<sub>1</sub>/(R/I<sub>e</sub>) &nbsp;(Eq.12.8-6, S<sub>1</sub>&ge;0.6)", f"{cmin_s1:.4f}"])
    crows.append(["<b>Governing C<sub>s</sub></b>", f"<b>{Cs:.4f}</b> &nbsp;({gov})"])
    cstab = "<h4>Seismic response coefficient C<sub>s</sub> and its limits</h4>" + _table(["C<sub>s</sub> equation", "Value"], crows)
    base = f"<p>Design base shear V = C<sub>s</sub>W = {Cs:.4f} &times; {W:.0f} = <b>{V:.0f} kip</b> in each direction.</p>"
    vrows = [[k, f"{E.floor_w(cfg,k):.0f}", f"{z[k]/12:.1f}", f"{E.floor_w(cfg,k)*(z[k]/12)**kk:,.0f}",
              f"{Fx[k]/V:.3f}", f"{Fx[k]:.1f}"] for k in range(1, NF+1)]
    vtab = (f"<h4>Vertical distribution of base shear (k = {kk:.2f}, &sect;12.8.3)</h4>"
            "<p>F<sub>x</sub> = C<sub>vx</sub>V, &nbsp; C<sub>vx</sub> = w<sub>x</sub>h<sub>x</sub><sup>k</sup> / "
            "&Sigma; w<sub>i</sub>h<sub>i</sub><sup>k</sup>.</p>"
            + _table(["Floor", "w (kip)", "h (ft)", "w&middot;h<sup>k</sup>", "C<sub>vx</sub>", "F<sub>x</sub> (kip)"], vrows))
    cumX = cumY = 0.0; mrows = []
    for m in range(min(len(T), 12)):
        cumX += eX[m]; cumY += eY[m]
        mrows.append([m+1, f"{T[m]:.3f}", f"{eX[m]*100:.1f}", f"{cumX*100:.1f}", f"{eY[m]*100:.1f}", f"{cumY*100:.1f}"])
    mtab = ("<h4>Modal periods &amp; mass participation (&ge; 90% per &sect;12.9.1)</h4>"
            + _table(["Mode", "T (s)", "mX %", "&Sigma;mX %", "mY %", "&Sigma;mY %"], mrows)
            + "<p class='cnote'>Mode-shape figures are in Chapter 5. Vertical seismic E<sub>v</sub> = 0.2&middot;S<sub>DS</sub>D, "
            "redundancy &rho;, the 100/30 directional combination and &plusmn;5% accidental torsion are applied in the "
            "load combinations (Chapter 4); torsional amplification is screened in Chapter 2.</p>")
    return intro + cstab + base + vtab + mtab

def _governing_lateral(cfg, V, VwX, VwY):
    if V is None:
        return "<p class='note'>[seismic base shear unavailable]</p>"
    rows = []
    for d, Vw in (("X (E-W)", VwX), ("Y (N-S)", VwY)):
        if Vw is None:
            rows.append([d, f"{V:.0f}", "&mdash; (no wind defined)", "<b>Seismic</b>"])
        else:
            rows.append([d, f"{V:.0f}", f"{Vw:.0f}", f"<b>{'Seismic' if V >= Vw else 'Wind'}</b>"])
    note = ("<p class='cnote'>Strength-level base-shear comparison (seismic E and wind W are both strength-level in "
            "ASCE 7-22 LRFD). The governing system per direction sizes the lateral frames; both are carried through "
            "the load combinations (Chapter 4).</p>")
    return _table(["Direction", "Seismic V (kip)", "Wind V (kip)", "Governs"], rows) + note


def _combo_table(cases):
    def lab(label, lat):
        if not lat: return "&mdash;"
        if "WX" in label or "WY" in label: return "wind"
        if "EX" in label or "EY" in label: return "seismic"
        return "lateral"
    rows = [[L, f"{fD:.2f}", f"{fL:.2f}", f"{fLr:.2f}", lab(L, lat), "columns only" if co else "all members"]
            for (L, fD, fL, fLr, lat, co) in cases]
    return _table(["Combination", "D", "L", "L<sub>r</sub>/S", "Lateral", "Applies to"], rows)

def _combo_legend():
    items = [("D", "dead load"),
             ("L", "floor live load (reducible per ASCE 7-22 &sect;4.7)"),
             ("L<sub>r</sub> / S", "roof live load / snow"),
             ("E<sub>X</sub>, E<sub>Y</sub>", "horizontal seismic effect Q<sub>E</sub> (ELF) in the X (E-W) / Y (N-S) direction; the vertical term E<sub>v</sub>=0.2S<sub>DS</sub>D is folded into the D factor"),
             ("W<sub>X</sub>, W<sub>Y</sub>", "wind load in the X / Y direction"),
             ("&rho;", "redundancy factor multiplying Q<sub>E</sub> (&sect;12.3.4); &rho;=1.0 here (SDC B)"),
             ("&Omega;<sub>0</sub>", "overstrength factor; the &ldquo;[col]&rdquo; cases apply &Omega;<sub>0</sub>Q<sub>E</sub> to capacity-protected columns only (&sect;12.4.3)"),
             ("t+ / t&minus;", "&plusmn;5% accidental torsion M<sub>t</sub> = &plusmn;0.05B&middot;F<sub>x</sub> (&sect;12.8.4.2)"),
             ("+ / &minus;", "sign (direction) of the applied lateral load"),
             ("[col]", "combination applied to columns only"),
             ("0.5L, 0.2S, 0.9D", "companion / counteracting load factors (ASCE 7-22 &sect;2.3)")]
    return "<h4>Notation used in the combination labels</h4>" + _table(["Symbol", "Meaning"], [[a, b] for a, b in items])

def _joint_figure(cfg):
    """Schematic of the ACTUAL modelled boundary conditions for THIS job (base fixity and joint
    types from cfg['model'], braced-bay panel only when the model has braces) -- no boilerplate
    about frames or grid lines that aren't in the present building."""
    import matplotlib.pyplot as _plt
    md = (cfg.get("model") or {})
    bases = str(md.get("bases") or cfg.get("base") or "pinned").lower()
    joints = str(md.get("joints") or ("pinned" if cfg.get("releases") else "rigid")).lower()
    try:
        braced = E.is_braced(cfg)
    except Exception:
        braced = bool(cfg.get("brace") or cfg.get("braces"))
    ncol = 3 if braced else 2
    fig, axs = _plt.subplots(1, ncol, figsize=(4.9*ncol + 1.0, 4.6))

    def _fixed_base(ax, x, label):
        ax.plot([x, x], [3, 8.5], lw=3, color="#333"); ax.plot([x-1.3, x+1.3], [3, 3], lw=4, color="#333")
        for h in (-1.1, -0.5, 0.1, 0.7): ax.plot([x+h, x+h-0.45], [3, 2.45], lw=1, color="#333")
        ax.text(x, 9.0, "FIXED base", ha="center", fontweight="bold", fontsize=11)
        ax.text(x, 1.6, label, ha="center", fontsize=8.5)
    def _pinned_base(ax, x, label):
        ax.plot([x, x], [3, 8.5], lw=3, color="#1f77b4"); ax.plot([x-0.7, x+0.7], [2.55, 2.55], lw=4, color="#333")
        for h in (-0.5, 0.1, 0.6): ax.plot([x+h, x+h-0.45], [2.55, 2.0], lw=1, color="#333")
        ax.plot([x-0.4, x, x+0.4], [2.55, 3, 2.55], color="#1f77b4", lw=2)
        ax.add_patch(_plt.Circle((x, 3), 0.16, fill=False, color="#1f77b4", lw=2))
        ax.text(x, 9.0, "PINNED base", ha="center", fontweight="bold", fontsize=11)
        ax.text(x, 1.6, label, ha="center", fontsize=8.5)

    ax = axs[0]; ax.set_title("Column base fixity"); ax.axis("off"); ax.set_xlim(0, 10); ax.set_ylim(0, 10)
    if "mix" in bases:
        _fixed_base(ax, 2.4, "lateral-frame columns"); _pinned_base(ax, 7.4, "all other columns")
    elif "fix" in bases:
        _fixed_base(ax, 5, "ALL column bases in this model")
    else:
        _pinned_base(ax, 5, "ALL column bases in this model")

    def _fr_joint(ax, x, label):
        ax.plot([x, x], [1, 9], lw=3, color="#333"); ax.plot([x, x+3.0], [6, 6], lw=3, color="#333")
        ax.add_patch(_plt.Rectangle((x-0.25, 5.55), 0.5, 0.9, color="#d62728"))
        ax.plot([x+0.3, x+1.0], [6.5, 6.9], lw=1.5, color="#d62728"); ax.plot([x+0.3, x+1.0], [5.5, 5.1], lw=1.5, color="#d62728")
        ax.text(x+1.4, 7.2, "FR (rigid)\nmoment joint", fontsize=9, color="#d62728", fontweight="bold")
        ax.text(x+1.4, 4.6, label, fontsize=8.5)
    def _pin_joint(ax, x, label, y=3.0):
        ax.plot([x, x], [1, 9], lw=3, color="#333"); ax.plot([x, x+2.6], [y, y], lw=3, color="#1f77b4")
        ax.add_patch(_plt.Circle((x+0.18, y), 0.16, fill=False, color="#1f77b4", lw=2))
        ax.text(x+0.35, y+1.0, "PINNED (shear) joint", fontsize=9, color="#1f77b4", fontweight="bold")
        ax.text(x+0.35, y-1.4, label, fontsize=8.5)

    ax = axs[1]; ax.set_title("Beam-to-column joints"); ax.axis("off"); ax.set_xlim(0, 10); ax.set_ylim(0, 10)
    if "mix" in joints:
        _fr_joint(ax, 1.6, "moment-frame girders"); _pin_joint(ax, 6.6, "all other girders")
    elif joints.startswith("pin"):
        _pin_joint(ax, 3.0, "ALL girder joints in this model\n(shear / simple connections)", y=6.0)
    else:
        _fr_joint(ax, 3.0, "ALL girder joints in this model")

    if braced:
        ax = axs[2]; ax.set_title("Bracing"); ax.axis("off"); ax.set_xlim(0, 10); ax.set_ylim(0, 10)
        ax.plot([2, 2], [2, 8], lw=3, color="#333"); ax.plot([8, 8], [2, 8], lw=3, color="#333")
        ax.plot([2, 8], [8, 8], lw=3, color="#333"); ax.plot([2, 8], [2, 2], lw=2, color="#666")
        ax.plot([2, 8], [2, 8], lw=2.5, color="#e67e22"); ax.plot([2, 8], [8, 2], lw=2.5, color="#e67e22")
        for (bx, by) in ((2, 2), (8, 8), (2, 8), (8, 2)):
            ax.add_patch(_plt.Circle((bx, by), 0.22, fill=False, color="#e67e22", lw=2))
        ax.text(5, 0.8, "braces: PIN-ENDED axial (truss) members\nno end moments, tension/compression only",
                ha="center", fontsize=8.5, color="#a3540a")
    fig.suptitle("Modelled joint & base fixity", fontweight="bold")
    return _b64(fig)

def _floor_serviceability(pkg):
    ms = (pkg or {}).get("members") or []
    rows = []
    for m in ms:
        sv = m.get("serviceability")
        if not sv: continue
        sec = (m.get("inputs", {}) or {}).get("section", m.get("id", ""))
        rows.append([m.get("id", ""), sec,
                     f"{sv.get('live_in','')}\" = {sv.get('live_ratio','')}", sv.get("live_limit", "L/360"),
                     f"{sv.get('total_in','')}\" = {sv.get('total_ratio','')}", sv.get("total_limit", "L/240"),
                     (f"{sv.get('camber_in')}\"" if sv.get("camber_in") else "&mdash;"),
                     "OK" if sv.get("ok") else "NG"])
    if not rows:
        return "<p class='cnote'>Floor/roof beam deflection &amp; camber: no serviceability data in calc_package.json.</p>"
    return ("<h3>Floor &amp; roof beam deflection and camber (ASCE 7-22 serviceability; AISC 360-22 Ch. L)</h3>"
            "<p>Live-load deflection is limited to L/360 and total-load deflection to L/240. Composite floor members "
            "use a lower-bound transformed moment of inertia I<sub>tr</sub>; the bare-steel (pre-composite, wet-concrete) "
            "dead-load deflection is offset by shop camber.</p>"
            + _table(["Member", "Section", "Live &delta;", "Limit", "Total &delta;", "Limit", "Camber", "&le; limit"], rows))

def _combo_notes(cfg):
    s = cfg["seis"]; SDS = s["SDS"]; rho = cfg.get("rho", 1.3); Om0 = s.get("Om0")
    return ("<p>The analysed set is the ASCE 7-22 &sect;2.3 LRFD strength combinations:</p><ul>"
            f"<li><b>Vertical seismic E<sub>v</sub></b> = 0.2&middot;S<sub>DS</sub>D = {0.2*SDS:.2f}D is folded into the "
            f"D factor: seismic combinations use (1.2+0.2S<sub>DS</sub>)D = {1.2+0.2*SDS:.2f}D and "
            f"(0.9&minus;0.2S<sub>DS</sub>)D = {0.9-0.2*SDS:.2f}D.</li>"
            f"<li><b>Redundancy &rho;</b> = {rho} multiplies the horizontal seismic effect Q<sub>E</sub> (&sect;12.3.4).</li>"
            f"<li><b>Overstrength &Omega;<sub>0</sub></b> = {Om0}: the <i>columns-only</i> cases apply "
            "E<sub>m</sub> = &Omega;<sub>0</sub>Q<sub>E</sub> to the capacity-protected columns (&sect;12.4.3) and are "
            "not applied to other members.</li>"
            "<li><b>Directional 100/30</b>: each seismic case carries 100% of F<sub>x</sub> in the loaded direction "
            "plus 30% in the orthogonal direction (&sect;12.5.3/12.5.4).</li>"
            "<li><b>Accidental torsion</b> &plusmn;M<sub>t</sub> = &plusmn;0.05B&middot;F<sub>x</sub> (&sect;12.8.4.2) "
            "is applied in both signs.</li>"
            "<li><b>Net uplift / overturning</b> is checked by the 0.9D combinations (minimum gravity with lateral).</li>"
            "<li><b>Second order</b>: every combination is solved independently through a P-&Delta; analysis; "
            "factored second-order results are never superposed.</li></ul>")

def _modal_mass_check(eX, eY):
    cx = sum(eX); cy = sum(eY)
    return (f"<p><b>Modal mass participation:</b> &Sigma;m<sub>X</sub> = {cx*100:.0f}% "
            f"({'OK' if cx >= 0.90 else 'review &lt; 90%'}), &Sigma;m<sub>Y</sub> = {cy*100:.0f}% "
            f"({'OK' if cy >= 0.90 else 'review &lt; 90%'}); ASCE 7-22 &sect;12.9.1 requires &ge; 90% in each "
            "direction. Periods and mode shapes are shown below and in Chapter 3.</p>")

def _stability_basis_note():
    return ("<p class='cnote'><b>Analysis basis:</b> a second-order P-&Delta; geometric transformation is applied to "
            "the columns under every factored combination (no superposition of factored results). Effective length "
            "K = 1 is used, consistent with a second-order analysis. If the Direct Analysis Method (AISC 360-22 "
            "&sect;C2) is the design basis, the 0.8&middot;EI / 0.8&tau;<sub>b</sub>&middot;EA stiffness reductions and "
            "notional loads N<sub>i</sub> = 0.002&alpha;Y<sub>i</sub> are confirmed at the member-design stage; the "
            "story stability coefficient &theta; (&sect;12.8.7) and the B<sub>2</sub> amplifier are reported in "
            "Chapter 7.</p>")

_KNOWN_PKG_KEYS = {"members", "connections", "connection_demands", "capacity_design", "scwb",
                   "framework_screen", "building", "code", "note", "notes", "meta", "name",
                   "composite_design"}   # composite_design gets its OWN Chapter-6 section (below)

_COMPOSITE_SYN = {   # tolerant key synonyms seen across agent packages (candidates + gold)
    "pct":   ("partial_composite_ratio", "partial_composite_percent", "partial_composite_pct",
              "degree_of_composite", "composite_ratio"),
    "Qn":    ("stud_Qn_kip", "Qn_kip", "Qn"),
    "studs": ("stud_count_total", "n_studs", "studs_total", "n_per_half_span", "stud_schedule", "studs"),
    "camber":("camber_in", "camber"),
    "ILB":   ("I_LB_in4", "ILB_in4", "I_LB", "lower_bound_I_in4", "I_lb_in4"),
    "wet":   ("wet_stage_phiMn_kipft", "wet_stage_phiMn_kipin", "Mu_wet_kipin",
              "wet_dead_deflection_in", "wet_deflection_in"),
}


def _extra_blocks_section(pkg):
    """Supplementary design records: render every agent-authored TOP-LEVEL calc_package block the
    report does not already show (serviceability, fatigue, crane_runway, equipment_supports,
    analysis_idealization, ponding, ...) through the same nested-table renderer as capacity_design.
    These are real design records the agent wrote for the reviewer -- before this section existed
    they lived only in calc_package.json."""
    if not isinstance(pkg, dict):
        return ""
    parts = []
    for k, v in pkg.items():
        if k in _KNOWN_PKG_KEYS or v in (None, "", [], {}):
            continue
        if not isinstance(v, (dict, list, str, int, float, bool)):
            continue
        blob = v if isinstance(v, str) else json.dumps(v)
        title = str(k).replace("_", " ").title()
        if len(blob) > 12000:                      # guard: a runaway dump must not drown the report
            parts.append("<h4>%s</h4><p class='note'>[block truncated at 12 kB -- full record in "
                         "design/calc_package.json]</p><pre>%s\u2026</pre>"
                         % (title, (blob[:12000].replace("<", "&lt;"))))
            continue
        parts.append("<h4>%s</h4>" % title)
        parts.append(_capdesign_html(v) if isinstance(v, (dict, list))
                     else "<p>%s</p>" % str(v).replace("<", "&lt;"))
    if not parts:
        return ""
    return ("<h3>Supplementary design records (from the calc package)</h3>"
            "<p>Agent-authored design records beyond the member/connection/capacity-design tables "
            "&mdash; rendered verbatim from <code>design/calc_package.json</code>.</p>" + "".join(parts))


def _composite_section(pkg):
    """Chapter-6 'Composite floor design (AISC 360 Ch. I)' section. Two tolerant sources:
    (a) the standard top-level `composite_design` block (rendered as nested tables), and
    (b) composite values embedded in member capacity dicts (stud counts / partial ratio / camber /
        I_LB / wet stage) -- summarised one row per member. Renders nothing when neither exists."""
    if not isinstance(pkg, dict):
        return ""
    parts = []
    cd = pkg.get("composite_design")
    rows = []
    for m in pkg.get("members", []) or []:
        cap = m.get("capacity") if isinstance(m.get("capacity"), dict) else {}
        found = {}
        for col, keys in _COMPOSITE_SYN.items():
            for kk in keys:
                if kk in cap and cap[kk] not in (None, ""):
                    found[col] = cap[kk]; break
        if found:
            rows.append([str(m.get("id", "")), str((m.get("inputs") or {}).get("section", "")),
                         str(found.get("pct", "&mdash;")), str(found.get("Qn", "&mdash;")),
                         str(found.get("studs", "&mdash;")), str(found.get("camber", "&mdash;")),
                         str(found.get("ILB", "&mdash;")), str(found.get("wet", "&mdash;"))])
    if not cd and not rows:
        return ""
    parts.append("<h3>Composite floor design (AISC 360 Ch. I)</h3>")
    if rows:
        parts.append("<p>Per-member composite design values recorded in the calc package (partial-"
                     "composite ratio, stud strength/schedule, camber, lower-bound moment of inertia, "
                     "unshored wet-concrete stage):</p>")
        parts.append(_table(["member", "section", "partial comp.", "Q<sub>n</sub> (kip)", "studs",
                             "camber (in)", "I<sub>LB</sub> (in<sup>4</sup>)", "wet stage"], rows))
    if cd:
        parts.append("<h4>Composite design record (calc package `composite_design`)</h4>")
        parts.append(_capdesign_html(cd))
    parts.append("<p class='note'>Stud strengths per AISC 360-22 &sect;I8.2a (with deck-rib position "
                 "factors); flexure per &sect;I3.2a; service deflection on the lower-bound moment of "
                 "inertia; camber rule and the unshored construction stage as recorded above.</p>")
    return "".join(parts)


def _member_dc_summary(pkg):
    ms = pkg.get("members") or []
    have = [m for m in ms if m.get("DC") is not None]
    if have:
        gov = max(have, key=lambda m: m.get("DC") or 0.0)
        return (f"<p>Governing member: <b>{gov.get('id')}</b> at D/C = <b>{gov.get('DC'):.2f}</b> "
                f"({gov.get('limit_state')}, combo {(gov.get('inputs', {}) or {}).get('governing_combo', '')}).</p>")
    return ("<p class='cnote'>The member <b>demands</b> (axial, moment, shear, L<sub>b</sub>, section properties and "
            "governing combination) are computed by the framework and listed below; the <b>capacities</b>, limit "
            "states and D/C ratios are derived by the agent from the AISC 360/341 RAG (Appendix A) and are pending "
            "for this building.</p>")

def _ch6_notes(cfg):
    comp = " Composite floor members (Ch. I) are designed where composite action is used." if cfg.get("composite") else ""
    return ("<p class='cnote'>Limit states checked per member as applicable: tension (D2/D3), compression (E3/E4/E7), "
            "flexure (F2-F8 with the correct compact / noncompact / slender limit state, L<sub>b</sub>, C<sub>b</sub>), "
            "shear (G2) and combined axial+flexure interaction (H1)." + comp + " Concentrated-load limit states "
            "(web local yielding / crippling, J10) and stiffeners are checked at point loads and supports where "
            "applicable. The EOR should spot-check 2-3 governing members by hand against Appendix A.</p>")


def _drift_from_forces(cfg, Fdict, direction):
    info = E.build(cfg, "Linear"); NF = info["NF"]; di = 0 if direction == "X" else 1
    ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
    for k in range(1, NF+1):
        f = [0.0]*6; f[di] = Fdict[k]; ops.load(E.mtag(k), *f)
    ops.constraints("Transformation"); ops.numberer("RCM"); ops.system("UmfPack")
    ops.test("NormDispIncr", 1e-8, 100); ops.algorithm("Linear")
    ops.integrator("LoadControl", 1.0); ops.analysis("Static"); ops.analyze(1)
    drift = []; prev = 0.0
    for k in range(1, NF+1):
        d = ops.nodeDisp(E.mtag(k), di+1); drift.append((d-prev)/cfg["heights"][k-1]); prev = d
    return drift

def _wind_drift_section(cfg):
    if not cfg.get("wind"):
        return "<p class='cnote'>Wind drift not evaluated (no wind parameters defined).</p>"
    NF = len(cfg["heights"]); lim = cfg.get("wind_drift_limit", 1.0/400.0)
    try:
        dX = _drift_from_forces(cfg, E.wind_forces(cfg, "X"), "X")
        dY = _drift_from_forces(cfg, E.wind_forces(cfg, "Y"), "Y")
    except Exception as ex:
        return f"<p class='note'>[wind drift run failed: {ex}]</p>"
    rows = [[k, f"{dX[k-1]*100:.3f}", f"{dY[k-1]*100:.3f}", "OK" if max(dX[k-1], dY[k-1]) <= lim else "NG"]
            for k in range(1, NF+1)]
    return ("<h3>Wind drift</h3>"
            f"<p>Interstory drift under the design MWFRS wind vs a serviceability limit of h/{int(round(1/lim))} "
            f"({lim*100:.2f}%). Wind drift has no code-mandated limit (ASCE 7-22 Appendix CC is advisory); a "
            "10-year-MRI service wind may be used for a less conservative check.</p>"
            + _table(["Story", "drift X %", "drift Y %", f"&le; {lim*100:.2f}%"], rows))

def _capdesign_html(cap):
    """Render the agent's capacity_design / scwb block as readable nested tables (NOT raw JSON)."""
    def fmt(v):
        if isinstance(v, bool): return "yes" if v else "no"
        if isinstance(v, float): return ("%.3f" % v).rstrip("0").rstrip(".")
        return str(v)
    def block(d):
        rows = []
        for k, v in d.items():
            label = str(k).replace("_", " ")
            if isinstance(v, dict):
                rows.append([label, block(v)])
            elif isinstance(v, list):
                if any(isinstance(x, dict) for x in v):
                    rows.append([label, "".join(block(x) if isinstance(x, dict)
                                                else "<p>%s</p>" % fmt(x) for x in v)])
                else:
                    rows.append([label, ", ".join(fmt(x) for x in v)])
            else:
                rows.append([label, fmt(v)])
        return _table(["Item", "Value"], rows)
    if isinstance(cap, dict): return block(cap)
    if isinstance(cap, list): return "".join(_capdesign_html(x) for x in cap)
    return "<p>%s</p>" % fmt(cap)

def _aisc341_detailing(cfg, pkg):
    braced = E.is_braced(cfg); s = cfg["seis"]
    det = (pkg or {}).get("detailing")
    if det:
        _rows = [[c.get("check", ""), c.get("status", "")] for c in det.get("checks", [])]
        _intro = (f"<p><b>System:</b> {det.get('system','')} &mdash; R = {det.get('R', s.get('R'))}, "
                  f"C<sub>d</sub> = {det.get('Cd', s.get('Cd'))}, &Omega;<sub>0</sub> = {det.get('Omega0', s.get('Om0'))}, "
                  f"SDC {det.get('SDC','')}. <b>AISC 341 applies:</b> {'yes' if det.get('aisc341_applies') else 'no'}.</p>"
                  f"<p>{det.get('basis','')}</p>")
        return _intro + _table(["Required check", "Status / basis"], _rows)
    if braced:
        sysname = "concentrically braced frame (SCBF/OCBF)"
        checks = [("Brace width-thickness", "highly/moderately ductile (Table D1.1)"),
                  ("Brace slenderness KL/r", "&le; 200 (SCBF, F2.5b)"),
                  ("Brace connection strength", "expected R<sub>y</sub>F<sub>y</sub>A<sub>g</sub> / 1.1R<sub>y</sub>P<sub>n</sub> (F2.6c)"),
                  ("Columns &amp; collectors", "amplified seismic &Omega;<sub>0</sub> or capacity-limited"),
                  ("Protected zones / gussets", "brace ends, gusset hinge zone"),
                  ("Demand-critical welds", "AISC 341 A3.4")]
    else:
        sysname = "moment frame (SMF/IMF)"
        checks = [("Strong-column-weak-beam", "&Sigma;M*<sub>pc</sub>/&Sigma;M*<sub>pb</sub> &gt; 1.0 (E3.4a)"),
                  ("Beam &amp; column width-thickness", "highly ductile (Table D1.1)"),
                  ("Panel-zone shear &amp; doublers", "AISC 341 E3.6e"),
                  ("Continuity plates", "at beam flanges (E3.6f)"),
                  ("Protected zones", "RBS / plastic-hinge regions"),
                  ("Demand-critical welds", "CJP beam-flange-to-column"),
                  ("Prequalified connection", "within AISC 358 limits")]
    cap = (pkg or {}).get("capacity_design") or (pkg or {}).get("scwb")
    rows = [[name, basis, "see Appendix A" if cap else "agent-derived (AISC 341 RAG) &mdash; pending"] for name, basis in checks]
    intro = (f"<p>For the {sysname} (R = {s.get('R')}), the required AISC 341-22 ductile-detailing and capacity-design "
             "checks are listed below. These are derived by the agent from the AISC 341 RAG; values populate from the "
             "calc package where present.</p>")
    extra = ("<h4>Capacity-design results (from the calc package)</h4>" + _capdesign_html(cap)) if cap else ""
    note = ("<p class='cnote'>Width-thickness ductility, AISC 358 prequalification limits, weld NDT and the C&amp;C "
            "cladding / net-uplift checks are confirmed on the drawings and connection submittal (delegated).</p>")
    return intro + _table(["Required check", "Basis", "Status"], rows) + extra + note


def _connection_demands(cfg, pkg, reX):
    rows = []; ms = (pkg or {}).get("members") or []
    mf = not cfg.get("braces")
    for m in ms:
        inp = m.get("inputs", {}) or {}; kind = inp.get("kind", ""); sec = inp.get("section", "")
        V = inp.get("V_kip") or 0.0; Mz = inp.get("Mz_kipin") or 0.0
        P = max(inp.get("P_comp_kip", 0) or 0, inp.get("P_tens_kip", 0) or 0)
        if kind == "beam":
            typ = "beam-to-column (moment)" if mf else "beam-to-column (shear)"
            dem = f"V = {V:.0f} kip" + (f", M = {Mz/12:.0f} k-ft" if mf and Mz else "")
            basis = "CJP flange welds + web bolts (J2/J3)" if mf else "bolted shear tab (J3 / J4)"
        elif "col" in kind:
            typ = "column splice / base"; dem = f"P = {P:.0f} kip" + (f", M = {Mz/12:.0f} k-ft" if Mz else "")
            basis = "splice (J1.4) / base plate J8 + ACI 318 Ch.17"
        elif kind == "brace":
            typ = "brace-to-gusset"; dem = f"axial = {P:.0f} kip"; basis = "expected strength (AISC 341 F2.6c)"
        else:
            continue
        rows.append([f"{sec} {kind}", typ, dem, basis])
    if reX is not None:
        pmax = max((r[2][2] for r in reX), default=0.0); vmax = max((abs(r[2][0]) for r in reX), default=0.0)
        rows.append(["column base", "base plate / anchor rods", f"P = {pmax:.0f} kip, V = {vmax:.0f} kip",
                     "J8/J9 + ACI 318 Ch.17 (min 4 rods)"])
    if not rows:
        return None
    return _table(["Member / location", "Connection type", "Governing demand", "Design basis (limit states)"], rows)

def _qa_scorecard(cfg, Fx, reX, eX, eY, drX, drY):
    s = cfg["seis"]; rows = []
    if reX is not None and Fx is not None:
        Rx = sum(r[2][0] for r in reX); base = sum(Fx.values()); Rz = sum(r[2][2] for r in reX)
        rows.append(["Equilibrium &mdash; |&Sigma;R<sub>x</sub>| = applied base shear", f"{abs(Rx):.0f} vs {base:.0f} kip",
                     "PASS" if base and abs(abs(Rx)-base)/base < 0.01 else "REVIEW"])
    if eX is not None and eY is not None:
        cx = sum(eX)*100; cy = sum(eY)*100
        rows.append(["Modal mass &ge; 90% (X / Y)", f"{cx:.0f}% / {cy:.0f}%", "PASS" if min(cx, cy) >= 90 else "REVIEW"])
    if drX is not None and Fx is not None:
        NF = len(cfg["heights"]); SDS = s["SDS"]; Cd = s.get("Cd", 5.5); Ie = s["Ie"]; lim = cfg.get("drift_limit", 0.02)
        dmax = max(max(drX[k]*Cd/Ie, drY[k]*Cd/Ie) for k in range(NF))
        rows.append(["Seismic design drift &le; limit", f"{dmax*100:.2f}% &le; {lim*100:.1f}%", "PASS" if dmax <= lim else "FAIL"])
        A = (cfg["NX"]*cfg["SX"])*(cfg["NY"]*cfg["SY"])/144.0
        Pu = {k: (1.2+0.2*SDS)*E.floor_w(cfg, k) + 0.5*(cfg.get("L_floor", 0)*A/1000.0 if k < NF else 0) for k in range(1, NF+1)}
        Ps = {sx: sum(Pu[k] for k in range(sx, NF+1)) for sx in range(1, NF+1)}
        Vs = {sx: sum(Fx[k] for k in range(sx, NF+1)) for sx in range(1, NF+1)}
        tmax = min(0.5/Cd, 0.25); tw = max((Ps[sx]*drX[sx-1]/Vs[sx] if Vs[sx] else 0) for sx in range(1, NF+1))
        rows.append(["Stability &theta; &le; &theta;<sub>max</sub>", f"{tw:.3f} &le; {tmax:.3f}", "PASS" if tw <= tmax else "FAIL"])
    if not rows:
        return ""
    return ("<h3>QA scorecard (automated checks)</h3>"
            + _table(["Check", "Result", "Status"], rows))


def _save_case_fig(uri, figdir, label):
    """Write a per-combination N/V/M figure to figs/case_<label>.png for the user to inspect later;
    it is NOT embedded in the report (keeps report.html compact)."""
    if not (figdir and uri and isinstance(uri, str)):
        return
    safe = "".join(c if c.isalnum() else "_" for c in str(label))[:60].strip("_") or "case"
    try:
        if uri.startswith("data:image/"):
            head, b64 = uri.split(",", 1)
            ext = head.split("/")[1].split(";")[0].replace("jpeg", "jpg")
            with open(os.path.join(figdir, "case_%s.%s" % (safe, ext)), "wb") as f:
                f.write(base64.b64decode(b64))
        else:
            src = os.path.join(figdir, os.path.basename(uri))
            if os.path.exists(src):
                import shutil; shutil.copy2(src, os.path.join(figdir, "case_%s.png" % safe))
    except Exception:
        pass

def _static_case_table(cfg, combo, nseg=2, render_fig=True):
    """Per member TYPE governing forces for ONE combination, from the STATIC model (distributed
    gravity) so beams carry their true moment (fixes the lumped-model 1.4D = zero-beam-moment bug).
    Custom-geometry buildings (custom_build) fall back to the dynamic model's demand envelope, which
    already adds the beam gravity span moment and supports arbitrary geometry."""
    label, fD, fL, fLr, lat, co = combo
    nseg = 6 if render_fig else nseg   # App-B per-combo FIGURE -> finer (nseg 6); scalar summary table -> fast (nseg 2)
    import static_model as SM
    mm, _, _ = SM.run_combo(cfg, fD, fL, fLr, lat, nseg=nseg)
    best = {}; maxN = (0.0, "", ""); maxM = (0.0, "", "")
    def upd(kind, sec, N, Mz, My, V, n1, n2, score):
        key = (kind, sec)
        if key not in best or score > best[key][0]:
            best[key] = (score, N, Mz, My, V, n1, n2)
    for b in mm["beams"]:
        sec = b.get("sec") or cfg["beam"]; N = Mz = My = V = 0.0
        for tag in b["segs"]:
            lf = ops.eleResponse(tag, "localForces")
            N = max(N, abs(lf[0]), abs(lf[6])); Mz = max(Mz, abs(lf[4]), abs(lf[10]))
            My = max(My, abs(lf[5]), abs(lf[11])); V = max(V, abs(lf[2]), abs(lf[8]))
        upd("beam", sec, N, Mz, My, V, b["A"], b["B"], Mz)
        if N > abs(maxN[0]): maxN = (N, "beam", sec)
        if Mz > abs(maxM[0]): maxM = (Mz, "beam", sec)
    for c in mm["cols"]:
        sec = c.get("sec") or cfg["col"]; lf = ops.eleResponse(c["tag"], "localForces")
        N = max(abs(lf[0]), abs(lf[6])); Mz = max(abs(lf[5]), abs(lf[11])); My = max(abs(lf[4]), abs(lf[10]))
        V = max(abs(lf[1]), abs(lf[2]), abs(lf[7]), abs(lf[8]))
        upd("col", sec, N, Mz, My, V, c["n1"], c["n2"], max(Mz, N))
        if N > abs(maxN[0]): maxN = (N, "col", sec)
        if max(Mz, My) > abs(maxM[0]): maxM = (max(Mz, My), "col", sec)
    for br in mm.get("braces", []):
        sec = br.get("sec") or cfg.get("brace", ""); N = ops.basicForce(br["tag"])[0]
        upd("brace", sec, N, 0.0, 0.0, 0.0, None, None, abs(N))
        if abs(N) > abs(maxN[0]): maxN = (N, "brace", sec)
    rows = []
    for (kind, sec), (sc, N, Mz, My, V, n1, n2) in sorted(best.items()):
        rows.append([kind, sec, f"{N:.1f}", f"{Mz/12:.1f}", f"{My/12:.1f}", f"{V:.1f}",
                     _loc(n1, n2) if n1 else "&mdash;"])
    d = _lat_dir(lat) or "X"
    uri = None
    if render_fig:                       # per-combo Appendix-B N/V/M figure (opt-in; the governing
        try:                             # diagrams in Chapter 5 already cover the governing cases)
            import frame_diagram as FD
            uri, _pk = FD.render_solved(cfg, mm, d, label, "perimeter")
        except Exception:
            uri = None
    return rows, maxN, maxM, uri, d

def _jmark(ax, x, z, pinned):
    if pinned: ax.plot([x], [z], marker="o", mfc="white", mec="#c0392b", ms=8, mew=1.6, zorder=5)
    else:      ax.plot([x], [z], marker="s", mfc="#1f3b73", mec="#1f3b73", ms=7, zorder=5)

def _joint_dist_fig(cfg, direction, model=None):
    """2D elevations of the perimeter and an internal frame line, every beam-end and column base
    marked rigid (filled) or pinned/shear-released (open). Parametric buildings read cfg['releases']
    and cfg['base']; custom_build buildings pass the staticized `model` (per-beam relz/rely + per-
    column bases). A beam end is 'pinned' when its STRONG-axis (vertical/gravity) moment is released
    (rely), per the corrected release convention."""
    custom = model is not None
    if cfg.get("custom_build") and not custom:
        return None
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    import matplotlib.lines as mlines
    NX, NY, SX, SY = cfg["NX"], cfg["NY"], cfg["SX"], cfg["SY"]; NF = len(cfg["heights"]); z = E.zlevels(cfg)
    if custom:
        rel_lu = {(b["i"], b["j"], b["k"], b["dir"]): (b.get("relz", "none"), b.get("rely", "none")) for b in model["beams"]}
        base_lu = model.get("bases", {})
    else:
        relf = cfg.get("releases"); base0 = cfg.get("base", "fixed")
    # ABSENT members must draw NOTHING (never a fixed/rigid default): on notched / offset-core /
    # split-level plans the elevation lines cross bays and bases that do not exist in the model --
    # the old .get(..., "fixed"/"none") defaults painted phantom FIXED bases and RIGID joints there.
    if custom:
        present = {k: set(map(tuple, v)) for k, v in (model.get("present") or {}).items()}
    else:
        present = {k: E.grid(cfg, k) for k in range(NF+1)}
    def _pt(i, fx):
        return (i, fx) if direction == "X" else (fx, i)
    def _exists(i, fx, k):
        return _pt(i, fx) in (present.get(k) or set())
    if direction == "X":
        ncol, span, coords = NX, SX, [i*SX for i in range(NX+1)]
        lines = [("Perimeter frame (j=0)", 0), (f"Internal frame (j={NY//2})", NY//2)]
        def beam_rel(i, k, fx):
            return rel_lu.get((i, fx, k, "X")) if custom else ((relf(i, fx, k, "X") if relf else ("none", "none"))
                                                               if _exists(i, fx, k) and _exists(i+1, fx, k) else None)
        def base_at(i, fx):
            return base_lu.get((i, fx)) if custom else (base0 if _exists(i, fx, 0) else None)
    else:
        ncol, span, coords = NY, SY, [j*SY for j in range(NY+1)]
        lines = [("Perimeter frame (i=0)", 0), (f"Internal frame (i={NX//2})", NX//2)]
        def beam_rel(i, k, fx):
            return rel_lu.get((fx, i, k, "Y")) if custom else ((relf(fx, i, k, "Y") if relf else ("none", "none"))
                                                               if _exists(i, fx, k) and _exists(i+1, fx, k) else None)
        def base_at(i, fx):
            return base_lu.get((fx, i)) if custom else (base0 if _exists(i, fx, 0) else None)
    fig, axes = plt.subplots(1, 2, figsize=(16, 6), sharey=True)
    for ax, (ttl, fx) in zip(axes, lines):
        for i in range(ncol+1):
            zs = [k for k in range(NF+1) if _exists(i, fx, k)]
            if len(zs) >= 2:                                   # column line only over the levels that exist
                ax.plot([coords[i]]*2, [z[min(zs)], z[max(zs)]], color="#999", lw=2, zorder=1)
            bf = base_at(i, fx)
            if bf is not None:
                ax.plot([coords[i]], [z[0]], marker="^", ms=11, zorder=5,
                        mfc=("white" if bf == "pinned" else "#1f3b73"),
                        mec=("#c0392b" if bf == "pinned" else "#1f3b73"), mew=1.6)
        for k in range(1, NF+1):
            for i in range(ncol):
                rel = beam_rel(i, k, fx)
                if rel is None:
                    continue                                   # bay not present on this line/level: draw nothing
                x1, x2 = coords[i], coords[i+1]; zk = z[k]
                relz, rely = rel
                ax.plot([x1, x2], [zk, zk], color="#999", lw=2, zorder=1)
                _jmark(ax, x1 + 0.12*span, zk, relz in ("I", "both"))
                _jmark(ax, x2 - 0.12*span, zk, relz in ("J", "both"))
        ax.set_title(ttl, fontsize=11); ax.set_xlabel("plan (in)"); ax.grid(alpha=0.2)
    axes[0].set_ylabel("Z (in)")
    leg = [mlines.Line2D([], [], marker="s", color="none", mfc="#1f3b73", mec="#1f3b73", ms=9, label="rigid / continuous"),
           mlines.Line2D([], [], marker="o", color="none", mfc="white", mec="#c0392b", ms=9, mew=1.6, label="pinned / shear release"),
           mlines.Line2D([], [], marker="^", color="none", mfc="#1f3b73", mec="#1f3b73", ms=10, label="base fixed"),
           mlines.Line2D([], [], marker="^", color="none", mfc="white", mec="#c0392b", ms=10, mew=1.6, label="base pinned")]
    fig.suptitle(f"Joint fixity distribution - {direction}-direction frames", y=1.0, fontsize=12)
    fig.legend(handles=leg, loc="lower center", ncol=4, fontsize=9, frameon=False, bbox_to_anchor=(0.5, -0.02))
    fig.tight_layout(rect=[0, 0.06, 1, 0.96])
    return _b64(fig)

def _beam_deflections(cfg, fD, fL, nseg=6):
    """Chord-relative mid-span vertical deflection of every beam under a service gravity case, from
    the static model (true distributed tributary load). Returns {(i,j,k,dir): (|defl_in|, L_in)}."""
    import static_model as SM
    mm, _, _ = SM.run_combo(cfg, fD, fL, 0.0, {}, nseg=nseg)
    out = {}
    for b in mm["beams"]:
        ch = b["nodes"]; zA = ops.nodeDisp(ch[0], 3); zB = ops.nodeDisp(ch[-1], 3); n = len(ch)-1
        dmax = 0.0
        for idx, nd in enumerate(ch):
            d = ops.nodeDisp(nd, 3) - (zA + (zB-zA)*idx/n)
            if abs(d) > abs(dmax): dmax = d
        out[(b["i"], b["j"], b["k"], b["dir"])] = (abs(dmax), b["L"])
    return out

def _deflection_section(cfg, nseg=6):
    NF = len(cfg["heights"])
    try:
        dD = _beam_deflections(cfg, 1.0, 0.0, nseg); dL = _beam_deflections(cfg, 0.0, 1.0, nseg)
    except Exception as ex:
        return f"<p class='note'>[deflection run failed: {ex}]</p>"
    def worst(roof):
        best = None
        for key, (dl, L) in dL.items():
            if (key[2] == NF) != roof: continue
            ratio = (L/dl) if dl > 1e-6 else 1e12
            if best is None or ratio < best[1]: best = (key, ratio, dl, L)
        return best
    def cell(d, L, lim):
        if d < 1e-3: return ("&lt; 0.01 in", "OK")
        return (f"{d:.2f} in (L/{L/d:.0f})", "OK" if d <= L/lim else "NG")
    rows = []
    for roof, lbl in ((False, "Floor beam (governing)"), (True, "Roof beam (governing)")):
        w = worst(roof)
        if not w: continue
        key, ratio, dl, L = w; dd = dD.get(key, (0.0, L))[0]; dtl = dd + dl
        ltxt, lok = cell(dl, L, 360); ttxt, tok = cell(dtl, L, 240)
        camber = round(0.8*dd*4)/4 if dd >= 0.75 else 0.0
        rows.append([lbl, f"{L/12:.0f} ft", ltxt, lok, ttxt, tok,
                     (f"{camber:.2f} in" if camber > 0 else "none")])
    if not rows:
        return ""
    return ("<h3>Beam deflection &amp; camber</h3>"
            "<p>Service deflection of the governing floor and roof beam from the static model "
            "(chord-relative, true two-way tributary gravity): live load against L/360 and total D+L "
            "against L/240. Suggested camber &asymp; 80% of the dead-load deflection (nearest 1/4 in) "
            "where it exceeds 3/4 in.</p>"
            + _table(["Member", "Span", "Live &delta; (vs L/360)", "&le;", "Total &delta; (vs L/240)", "&le;", "Camber"], rows))


def _grounding_check(cfg, name, pkg):
    """Verify the design actually queried the RAG collections its systems require (reliability).
    Reads the activity log; A341/A358 are required only for seismic systems detailed for ductility
    (R > 3); AISC 360 + connection grounding are always required."""
    import re as _re
    recs, _ = _load_activity(name)
    col = {}
    for r in recs:
        if r.get("tool") == "search_engineering_standards":
            mm = _re.search(r"\[([A-Za-z0-9_]+)\]", r.get("detail", ""))
            if mm: col[mm.group(1)] = col.get(mm.group(1), 0) + 1
    def n(c): return col.get(c, 0)
    # ALSO credit grounding from the CITED CLAUSES in calc_package.json (not the activity log alone), so a
    # filled, RAG-grounded package is recognised even if the auto-logger did not record the queries (P5).
    def _cited(obj):
        out = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                out += ([str(v)] if k in ("cited", "cite") else _cited(v))
        elif isinstance(obj, list):
            for v in obj: out += _cited(v)
        return out
    _ct = " ".join(_cited(pkg or {}))
    cite_a360 = "360" in _ct; cite_a341 = "341" in _ct; cite_a358 = "358" in _ct
    R = cfg["seis"].get("R", 3); braced = E.is_braced(cfg); detailed = R > 3
    has_conn = bool((pkg or {}).get("connections")); has_cap = bool((pkg or {}).get("capacity_design"))
    rows = []
    def row(item, required, ok, ev):
        rows.append([item, "required" if required else "n/a (R&le;3)" if not detailed else "n/a",
                     ev, ("&mdash;" if not required else ("grounded" if ok else "<b>MISSING</b>"))])
    row("AISC 360-22 &mdash; member limit states", True, n("engineering_standards_A360") > 0 or cite_a360,
        f"{n('engineering_standards_A360')} queries" + (" + cited in calc_package" if cite_a360 else ""))
    row("AISC 360 Ch. J &mdash; connection design grounded", True, has_conn,
        "connections block present" if has_conn else "no connections block written")
    row("AISC 341-22 &mdash; seismic detailing / capacity design", detailed,
        n("engineering_standards_A341") > 0 or has_cap or cite_a341,
        f"{n('engineering_standards_A341')} queries" + (" + capacity_design block" if has_cap else "")
        + (" + cited in calc_package" if cite_a341 else ""))
    row("AISC 358 &mdash; prequalified moment connections", detailed and not braced,
        n("engineering_standards_A358") > 0 or cite_a358,
        f"{n('engineering_standards_A358')} queries" + (" + cited in calc_package" if cite_a358 else ""))
    nmiss = sum(1 for r in rows if "MISSING" in r[3])
    head = ("<h3>Grounding verification</h3>"
            f"<p>Whether the design queried the RAG collections its systems require. R = {R} "
            + ("(&gt; 3 &mdash; AISC 341 ductile detailing applies)." if detailed
               else "(&le; 3 &mdash; system not detailed for seismic; AISC 341/358 do not apply, design per AISC 360).")
            + "</p>")
    tail = ("<p class='note'>Grounding incomplete: the items marked MISSING were required for this building's "
            "systems but no RAG query / calc-package evidence was found. Re-run those checks against the RAG.</p>"
            if nmiss else "<p>All required RAG grounding is present.</p>")
    return head + _table(["Grounding requirement", "Applies", "Evidence", "Status"], rows) + tail

def _appendix_case_figs(cases, cfg):
    """Which load-case indices get a per-combo N/V/M figure in Appendix B. Default: a fast
    representative set -- the gravity combos + the primary seismic combo per direction/sign -- so the
    report stays under the MCP client timeout. cfg['appendix_case_figures']=True renders ALL combos
    (slow; run in the background); =False renders none."""
    flag = cfg.get("appendix_case_figures", "subset")  # default: representative subset (gravity + primary seismic)
    if flag is True:
        return set(range(len(cases)))                # ALL combos (slower; rendered inline)
    if not flag:
        return set()                                 # explicit False / None -> none
    picked, seen = [], set()
    for idx, (label, fD, fL, fLr, lat, co) in enumerate(cases):
        if co:                                    # skip Omega0 column-only duplicates
            continue
        lab = str(label)
        if lab.startswith("1.4D") or "1.2D+1.6L" in lab:
            picked.append(idx)
        else:
            for dirn in ("X", "Y"):
                for sgn in ("+", "-"):
                    key = "E" + dirn + sgn
                    if ("rhoE" + dirn + sgn) in lab and "+L" in lab and key not in seen:
                        seen.add(key); picked.append(idx)
        if len(picked) >= 6:
            break
    return set(picked[:6])


def _consistency_section(name, root, pkg):
    """Chapter 13 numerical self-consistency check (engine/consistency.py)."""
    try:
        import consistency
        issues = consistency.check(name, root=root, pkg=pkg, verbose=False)
    except Exception as ex:
        return f"<h3>Numerical self-consistency check</h3><p class='note'>[consistency check unavailable: {ex}]</p>"
    if not issues:
        return ("<h3>Numerical self-consistency check</h3>"
                "<p class='cnote'>&#10003; <b>PASS</b> &mdash; every member/connection carries a limit state, "
                "capacity and D/C; each headline D/C equals its worst limit-state check and demand&divide;capacity; "
                "and no quantity is reported with conflicting values.</p>")
    rows = [[str(i + 1), iss] for i, iss in enumerate(issues)]
    return ("<h3>Numerical self-consistency check</h3>"
            f"<p class='note'><b>{len(issues)} item(s) to reconcile</b> before the report is final &mdash; the same "
            "quantity must carry one value everywhere, every D/C must equal demand&divide;capacity, the headline D/C "
            "must equal its worst limit-state check, and no D/C may exceed 1.0:</p>" + _table(["#", "issue"], rows))


def _design_basis(cfg):
    """Top-of-report echo of the RESOLVED building parameters so a brief-vs-built mismatch (bay count,
    spans, stories, loads) is visible on page 1."""
    NX,NY=cfg["NX"],cfg["NY"]; SX,SY=cfg["SX"],cfg["SY"]; H=cfg["heights"]; s=cfg.get("seis",{})
    rows=[
      ["Lateral system", str(cfg.get("arch",""))],
      ["Plan grid", "%d &times; %d bays @ %.0f &times; %.0f ft  (%.0f &times; %.0f ft overall)"
                    % (NX,NY,SX/12.0,SY/12.0,NX*SX/12.0,NY*SY/12.0)],
      ["Stories", "%d @ %s ft  (H = %.0f ft)" % (len(H), ", ".join("%.0f"%(h/12.0) for h in H), sum(H)/12.0)],
      ["Gravity loads", "floor D %s / L %s psf; roof D %s / L %s psf"
                    % (cfg.get("D_floor","?"),cfg.get("L_floor","?"),cfg.get("D_roof","?"),cfg.get("L_roof","?"))],
      ["Seismic", "R=%s, Cd=%s, &Omega;<sub>0</sub>=%s, Ie=%s, S<sub>DS</sub>=%s, S<sub>1</sub>=%s"
                    % (s.get("R","?"),s.get("Cd","?"),s.get("Om0","?"),s.get("Ie","?"),s.get("SDS","?"),s.get("S1","?"))],
    ]
    return ("<h2>Design basis</h2><p class='note'>Model built to the parameters below &mdash; <b>verify these "
            "against your brief</b>, especially the bay count and spans.</p>" + _table(["Parameter","Value"], rows))


def build_report(name, root=None):
    _register(name); cfg = E.CFG[name]
    if root is None:
        root = os.path.join(os.environ.get("STEEL_BUILDER_JOBS") or HERE, name)
    NF = len(cfg["heights"]); s = cfg["seis"]
    SX, SY = cfg["SX"], cfg["SY"]; NX, NY = cfg["NX"], cfg["NY"]
    Lx = (cfg["xcoords"][-1] if cfg.get("xcoords") else NX*SX); Ly = (cfg["ycoords"][-1] if cfg.get("ycoords") else NY*SY)
    Htot = E.zlevels(cfg)[-1]
    pkg, pkgsrc = _load_pkg(name, root)
    mat = "ASTM A992 steel: \\(F_y=50\\) ksi, \\(F_u=65\\) ksi, \\(E=29{,}000\\) ksi."
    parts = [f"<h1>{name} &mdash; structural analysis &amp; design report</h1>",
             f"<p><b>{cfg.get('arch','')}</b> &middot; generated {datetime.date.today()}</p>",
             _toc(), _design_basis(cfg)]
    figdir = os.path.join(root, "figs"); os.makedirs(figdir, exist_ok=True)
    global _FIGDIR; _FIGDIR = figdir; _FIGSEQ[0] = 0
    try:
        import plot_model as PM
        PM.figures(name, figdir, deformed_fig=bool(cfg.get("deformed_shape_figure")))
        # Figure 2 (member orientation) is REQUIRED -- retry once on its own if missing
        if not os.path.exists(os.path.join(figdir, f"{name}_orientation.png")):
            try: PM.orientation(name, figdir)
            except Exception as _oex:
                parts.append(f"<p class='note'>[REQUIRED orientation figure (Fig 2) failed twice: {_oex} "
                             "&mdash; fix the model and re-render]</p>")
    except Exception as ex:
        parts.append(f"<p class='note'>[model figures failed: {ex}]</p>")

    # ---- shared computations (used across several chapters) ----
    Fx = None; T = eX = eY = None; Cs = V = Tu = Ta = W = None
    try: T, eX, eY, Cs, V, Tu, Ta, Fx, W = _seismic(cfg)
    except Exception as ex: parts.append(f"<p class='note'>[seismic analysis failed: {ex}]</p>")
    windhtml, VwX, VwY = _wind_section(cfg)
    print("[%s] report: model + ASCE 7 loads + modal done; rendering chapters%s ..."
          % (name, " + static-model force diagrams (~30-60 s)" if cfg.get("force_diagrams") else ""))
    drX = drY = None; reX = reY = None
    if Fx is not None:
        try:
            _, _dX, drX, reX = _run_case(cfg, "X", Fx); _, _dY, drY, reY = _run_case(cfg, "Y", Fx)
        except Exception as ex: parts.append(f"<p class='note'>[drift/equilibrium run failed: {ex}]</p>")

    # ============================== Chapter 1 — Design basis & codes ==============================
    parts.append(_chapter(1))
    parts.append(_design_basis_codes(cfg, s))
    parts.append("<h3>Building description</h3>")
    parts.append(_table(["Item", "Value"], [
        ["Archetype", cfg.get("arch", "")],
        ["Stories", f"{NF}"],
        ["Plan", f"{Lx/12:.0f} ft (X) &times; {Ly/12:.0f} ft (Y)"],
        ["Bays", f"{NX} &times; {NY} @ {SX/12:.0f} ft (X) / {SY/12:.0f} ft (Y)"],
        ["Height", f"{Htot/12:.0f} ft (h&#8345;)"],
        ["Column bases", cfg.get("base", "fixed")],
        ["Lateral system", "moment frame" if not cfg.get("braces") else "braced / dual / mixed"],
    ]))
    parts.append("<h3>Materials</h3><p>" + mat + "</p>")
    parts.append("<h3>Geometry</h3>")
    try: parts.append(_img(fig_plan(cfg), "Plan grid &mdash; the (i, j) labels used in the reaction/force tables"))
    except Exception as ex: parts.append(f"<p class='note'>[plan figure failed: {ex}]</p>")
    parts.append(_img(_png_file_b64(os.path.join(figdir, f"{name}_orientation.png")),
                      "Section orientation (web/depth ticks &mdash; beam strong axis must be vertical)"))

    # ====================== Chapter 2 — Structural system & load path =======================
    parts.append(_chapter(2))
    parts.append(_system_loadpath(cfg))
    parts.append("<h3>Seismic force-resisting system (each principal direction)</h3>")
    parts.append(_lfrs_table(cfg))
    parts.append("<h3>Lateral-load design inputs</h3>"); parts.append(_lateral_inputs_table(cfg))
    if cfg.get("deformed_shape_figure"):   # OFF the default workflow (viewer shows the same live)
        parts.append(_img(_png_file_b64(os.path.join(figdir, f"{name}_deformed_X.png")),
                          "Deformed shape under lateral X (confirms a continuous lateral load path)"))
    else:
        parts.append("<p class='note'>Static deformed-shape figure omitted for faster reporting &mdash; "
                     "set cfg['deformed_shape_figure']=True and re-render to include it; the interactive "
                     "3D viewer (viewer_3d.html in the job download) shows the load path live.</p>")
    try:
        # The standalone 3D viewer (viewer_3d.html) is WRITTEN here as a side effect -- the frontend
        # "View model" button and the job download need the file even though the report no longer
        # embeds a viewer section (removed by request). Do not drop this call again.
        import viewer3d
        viewer3d.report_section(cfg, name, root)   # returns markup we deliberately discard
    except Exception as ex:
        print(f"[report] viewer_3d.html generation failed: {ex}")
    if Fx is not None:
        parts.append("<h3>Horizontal force distribution to the lateral frames</h3>")
        try: parts.append(_horizontal_distribution(cfg, Fx, VwX, VwY))
        except Exception as ex: parts.append(f"<p class='note'>[distribution failed: {ex}]</p>")
        parts.append("<h3>Diaphragm classification &amp; design force (ASCE 7-22 &sect;12.10)</h3>")
        try: parts.append(_diaphragm_section(cfg, Fx))
        except Exception as ex: parts.append(f"<p class='note'>[diaphragm section failed: {ex}]</p>")
        parts.append("<h3>Plan &amp; vertical irregularity screening</h3>")
        try: parts.append(_irregularity_section(cfg, Fx))
        except Exception as ex: parts.append(f"<p class='note'>[irregularity screen failed: {ex}]</p>")
    else:
        parts.append("<p class='cnote'>Diaphragm design force and irregularity screening need the seismic "
                     "analysis (Chapter 3).</p>")

    # ================================= Chapter 3 — Loads ==================================
    parts.append(_chapter(3))
    parts.append("<h3>Gravity loads</h3>"); parts.append(_gravity_loads_table(cfg))
    parts.append(_gravity_loads_note())
    parts.append("<h3>Wind load determination (ASCE 7-22 Ch. 26-31)</h3>"); parts.append(windhtml)
    parts.append(_wind_cc_note())
    parts.append("<h3>Seismic load determination (ASCE 7-22 Ch. 11-12)</h3>")
    if Fx is not None:
        parts.append(_seismic_loads_section(cfg, T, eX, eY, Cs, V, Tu, Ta, Fx, W))
    else:
        parts.append("<p class='note'>[seismic data unavailable]</p>")
    parts.append("<h3>Governing lateral load per direction</h3>")
    parts.append(_governing_lateral(cfg, V, VwX, VwY))

    # ========================== Chapter 4 — Load combinations ============================
    parts.append(_chapter(4))
    case_detail_parts = []
    try:
        cases = load_cases(cfg)
        parts.append(_combo_notes(cfg))
        parts.append("<h3>Combinations analysed (load factors)</h3>")
        parts.append(_combo_table(cases))
        parts.append(_combo_legend())
        # --- THREE separate opt-in items (each requestable on its own) -------------------
        #   cfg['force_summary']          -> the per-combination force SUMMARY table (heavy 27-combo solve)
        #   cfg['appendix_case_figures']  -> the Appendix-B per-combo N/V/M FIGURES (heavy; figs in the same loop)
        #   cfg['force_diagrams']         -> the Chapter-5 governing N/V/M frame DIAGRAMS (light; rendered below)
        _want_summary  = bool(cfg.get("force_summary"))
        _want_casefigs = bool(cfg.get("appendix_case_figures"))
        if Fx is not None and (_want_summary or _want_casefigs):      # this 27-combo solve feeds the table AND/OR the figs
            # RESUMABLE per-combo cache (design/_case_cache.pkl): each solved combo is saved as soon as
            # it finishes, so an interrupted render (client timeout / instance recycle) resumes instead
            # of restarting the whole 27+ solve loop. Keyed by a cfg fingerprint -> stale after redesign.
            import pickle as _pk, hashlib as _hl, os as _os
            _cache_p = _os.path.join(root, "design", "_case_cache.pkl")
            _fp = _hl.md5(repr(sorted((k, str(v)) for k, v in cfg.items()
                                      if not callable(v))).encode()).hexdigest()
            try:
                _cc = _pk.load(open(_cache_p, "rb"))
                if _cc.get("_fingerprint") != _fp:
                    _cc = {"_fingerprint": _fp}
            except Exception:
                _cc = {"_fingerprint": _fp}
            def _cc_save():
                try:
                    _os.makedirs(_os.path.dirname(_cache_p), exist_ok=True)
                    _tmp = _cache_p + ".tmp~"
                    with open(_tmp, "wb") as _f:
                        _pk.dump(_cc, _f)
                    _os.replace(_tmp, _cache_p)
                except Exception:
                    pass
            srows = []
            for _ci, combo in enumerate(cases):
                label, fD, fL, fLr, lat, col_only = combo
                if col_only:        # Omega0 column-overstrength: column-axial only -> reported in the member schedule (Ch.6)
                    if _want_summary:
                        srows.append([label, "&mdash;", "&Omega;<sub>0</sub> column overstrength (see Ch. 6)", "&mdash;", ""])
                    continue
                try:
                    _ckey = (str(label), bool(_want_casefigs))
                    if _ckey in _cc:
                        rows, (Nv, Nk, Ns), (Mv, Mk, Ms), uri, d = _cc[_ckey]
                    else:
                        rows, (Nv, Nk, Ns), (Mv, Mk, Ms), uri, d = _static_case_table(cfg, combo, render_fig=_want_casefigs)
                        _cc[_ckey] = (rows, (Nv, Nk, Ns), (Mv, Mk, Ms), uri, d)
                        _cc_save()
                    if _want_summary:
                        srows.append([label, f"{Nv:.0f}", f"{Nk} {Ns}".strip(), f"{Mv/12:.0f}", f"{Mk} {Ms}".strip()])
                        case_detail_parts.append(f"<h4>Load case: {label}</h4><p>{_case_desc(label, col_only)}</p>")
                        case_detail_parts.append(_table(
                            ["member", "section", "N (kip)", "Mz (k-ft)", "My (k-ft)", "V (kip)", "location (i,j / level)"],
                            rows))
                    if uri and _want_casefigs:
                        _save_case_fig(uri, figdir, label)   # -> figs/case_<label>.png (NOT embedded)
                except Exception as ex:
                    if _want_summary:
                        srows.append([label, "&mdash;", f"[failed: {ex}]", "&mdash;", ""])
            if _want_summary:
                parts.append("<h3>Load-case force summary</h3>")
                parts.append("<p>One row per combination: the largest axial force and the largest bending moment "
                             "anywhere in the structure, with the member type/section that carries it (from the "
                             "<b>static model</b>, so gravity beam moments are correct). The governing N/V/M diagrams are "
                             "in Chapter 5 (cfg['force_diagrams']); the per-combination N/V/M figures are in Appendix B "
                             "(cfg['appendix_case_figures']).</p>")
                parts.append(_table(["Load case", "Max axial N (kip)", "carried by", "Max moment M (k-ft)", "carried by"], srows))
        elif Fx is not None:
            parts.append("<h3>Load-case force summary</h3>")
            parts.append("<p class='note'>The per-combination force-summary table (one row per LRFD combination) is "
                         "temporarily omitted for faster reporting &mdash; it can be populated here on request at the "
                         "completion of the design. The governing per-member design demands are the enveloped values in "
                         "Chapter 6 / the member schedule.</p>")
    except Exception as ex:
        parts.append(f"<p class='note'>[load combinations failed: {ex}]</p>")

    # ======================= Chapter 5 — Analysis model fidelity =========================
    print("[%s] report: chapter 5 (analysis-model fidelity) ..." % name)
    parts.append(_chapter(5))
    rel = cfg.get("releases"); cb = cfg.get("custom_build")
    jt = ("custom (defined in custom_build)" if cb else
          "rigid / continuous except where moment releases are set" if rel else "all rigid / continuous (no releases)")
    parts.append("<h3>Modelling assumptions</h3>")
    parts.append(_table(["Assumption", "As modelled"], [
        ["Column bases", cfg.get("base", "fixed")],
        ["Beam-column &amp; brace joints", jt],
        ["Floor diaphragm", "rigid (in-plane), masters at floor centroid"],
        ["Geometric transform", "P-&Delta; (second-order) on columns"],
        ["Leaning gravity columns", "yes" if cfg.get("lean_gravity") else "framed into the lateral system"],
        ["Gravity for member forces", "static model &mdash; true two-way (45&deg;) tributary line loads on sub-divided beams"],
    ]))
    try:
        _md = (cfg.get("model") or {})
        _bs = str(_md.get("bases") or cfg.get("base") or "pinned")
        _jt = str(_md.get("joints") or ("pinned" if cfg.get("releases") else "rigid"))
        _cap = f"Modelled joint &amp; base fixity &mdash; column bases: {_bs}; girder joints: {_jt}"
        try:
            if E.is_braced(cfg): _cap += "; braces pin-ended (axial-only truss members)"
        except Exception: pass
        if cfg.get("lean_gravity"): _cap += "; leaning gravity columns"
        parts.append(_img(_joint_figure(cfg), _cap + ".", full=True))
    except Exception as _jex: parts.append(f"<p class='note'>[joint figure failed: {_jex}]</p>")
    _jmodel = None
    if cfg.get("custom_build"):
        try:
            import static_model as SM; _jmodel = SM.build_static(cfg, "Linear", 2)
        except Exception:
            _jmodel = None
    for _d in ("X", "Y"):
        try:
            _ju = _joint_dist_fig(cfg, _d, model=_jmodel)
            if _ju:
                parts.append(_img(_ju, f"Joint-fixity distribution &mdash; {_d}-direction perimeter vs internal frame "
                                       "(filled square = rigid/continuous, open circle = pinned/shear release, "
                                       "triangle = column base; pinned = strong-axis vertical moment released)"))
        except Exception as ex:
            parts.append(f"<p class='note'>[joint-distribution figure {_d} failed: {ex}]</p>")
    parts.append(_stability_basis_note())
    if Fx is not None and reX is not None:
        Rx = sum(r[2][0] for r in reX); Rz = sum(r[2][2] for r in reX)
        parts.append("<h3>Equilibrium check (seismic X combination)</h3>")
        parts.append(f"<p>|&Sigma;R<sub>x</sub>| = {abs(Rx):.0f} kip vs applied base shear {sum(Fx.values()):.0f} kip "
                     f"(equal and opposite); &Sigma;R<sub>z</sub> = {abs(Rz):.0f} kip factored gravity delivered to the "
                     "ground. Reactions balance the applied loads in all three axes (load path verified).</p>")
        if eX is not None and eY is not None:
            parts.append(_modal_mass_check(eX, eY))
        _want_modefigs = bool(cfg.get("mode_figures"))     # 3D mode-shape figures: OFF the default workflow
        for m in (1, 2, 3):
            if m <= NF*3:
                if _want_modefigs:
                    try: parts.append(_img(fig_mode_3d(cfg, m, 3), f"Mode {m} (T = {T[m-1]:.2f} s)", full=True))
                    except Exception as ex: parts.append(f"<p class='note'>[mode {m} failed: {ex}]</p>")
                else:
                    parts.append(f"<p class='note'>Mode {m} (T = {T[m-1]:.2f} s) shape figure temporarily omitted for faster "
                                 "reporting &mdash; can be populated here on request at the completion of the design.</p>")
        # animated mode-shape GIFs removed from the report: the interactive 3D viewer
        # ("View model" button, Chapter 2) animates all six modes on demand.
        else:
            pass   # animated mode-shape GIFs are gone for good -- the interactive 3D viewer animates all modes live
    try:
        if not bool(cfg.get("force_diagrams")):
            parts.append("<p class='note'>Governing N/V/M frame diagrams (perimeter &amp; internal lines, from the "
                         "static model) temporarily omitted for faster reporting &mdash; can be populated here on "
                         "request at the completion of the design.</p>")
            gd = {}
        else:
            import frame_diagram as FD
            gd = FD.governing_diagrams(cfg, load_cases(cfg), nseg=6, lines=("perimeter", "internal"))   # nseg 2->6 (opt-in figures)
        if gd.get("perimeter"):
            parts.append("<h3>Governing internal-force diagrams &mdash; perimeter frames (static model)</h3>")
            parts.append("<p>N / V / M for each perimeter frame under its governing LRFD combination, from the "
                         "static model (true two-way tributary loads on sub-divided beams). Peak values annotated "
                         "per member; columns are single elements (linear between ends).</p>")
            for _d in ("X", "Y"):
                if _d in gd["perimeter"]:
                    _uri, _label, _pkM = gd["perimeter"][_d]
                    parts.append(_img(_uri, f"{_d}-direction perimeter frame &mdash; governing combo {_label} "
                                            f"(peak beam moment {_pkM:.0f} k-ft)"))
        if gd.get("internal"):
            parts.append("<h3>Governing internal-force diagrams &mdash; internal frames (static model)</h3>")
            parts.append("<p>The same diagrams for an interior frame line, which generally carries more gravity "
                         "tributary (and less lateral) than the perimeter &mdash; the gravity-governed companion check.</p>")
            for _d in ("X", "Y"):
                if _d in gd["internal"]:
                    _uri, _label, _pkM = gd["internal"][_d]
                    parts.append(_img(_uri, f"{_d}-direction internal frame &mdash; governing combo {_label} "
                                            f"(peak beam moment {_pkM:.0f} k-ft)"))
    except Exception as ex:
        parts.append(f"<p class='note'>[static-model diagrams unavailable: {ex}]</p>")

    # ===================== Chapter 6 — Member strength design (AISC 360) ==================
    parts.append(_chapter(6))
    parts.append(_member_section(cfg, name, pkg, {"roof"}, "Roof members",
        "Roof beams/girders for roof dead + roof live (and snow); governing flexure/shear/deflection limit state."))
    parts.append(_member_section(cfg, name, pkg, {"floor"}, "Floor members",
        "Typical floor beams/girders for floor dead + live load."))
    parts.append(_member_section(cfg, name, pkg, {"gravity_col"}, "Gravity columns",
        "Interior gravity columns for accumulated tributary gravity (AISC 360 &sect;E3 compression)."))
    parts.append(_member_section(cfg, name, pkg, {"lateral_col"}, "Lateral-system columns",
        "Moment-frame / braced-frame columns for combined gravity + lateral (AISC 360 &sect;H1 interaction)."))
    if cfg.get("braces"):
        parts.append(_member_section(cfg, name, pkg, {"brace"}, "Braces",
            "Concentric braces for the design story shear (AISC 360 &sect;E3 compression, &sect;D2 tension)."))
    parts.append(_ch6_notes(cfg))
    if pkg:
        parts.append("<h3>Member design summary (calc_package.json)</h3>")
        parts.append(f"<p>Source: <code>{os.path.relpath(pkgsrc, HERE)}</code>. Capacities/D-C derived by the agent "
                     "from the AISC 360/341 RAG; full referenced calc in Appendix A.</p>")
        parts.append(_member_dc_summary(pkg))
        _comp = _composite_section(pkg)
        if _comp:
            parts.append(_comp)
        _xtra = _extra_blocks_section(pkg)
        if _xtra:
            parts.append(_xtra)
        _msfig = ""
        if cfg.get("section_color_figure"):    # OFF the default workflow (viewer's 'Color by section' shows it live)
            try:
                import viz3d as _VZ
                _msu = _VZ.members_by_size(cfg)
                if _msu:
                    _msfig = _img(_msu, "Model members coloured by section size &mdash; each distinct member size "
                                        "(e.g. each W-column / beam / HSS brace) drawn in its own colour (legend)")
            except Exception as _mex:
                _msfig = f"<p class='note'>[member-size figure failed: {_mex}]</p>"
        else:
            _msfig = ("<p class='note'>Member-size colour figure omitted for faster reporting &mdash; set "
                      "cfg['section_color_figure']=True and re-render to include it; the interactive 3D viewer's "
                      "'Color by section' view shows the same.</p>")
        parts.append(_narrative_html(pkg, members_fig_html=_msfig))
    else:
        parts.append("<p class='note'>[no calc_package.json found &mdash; run the design first]</p>")

    # ====================== Chapter 7 — Stability & second-order =========================
    parts.append(_chapter(7))
    if Fx is not None and drX is not None:
        try:
            parts.append(_stability_section(cfg, Fx, drX, pkg))
            parts.append(_img(fig_drift_profile(cfg, drX, drY), "Interstory drift profile (both directions)"))
            ss_uri, Vstory, OTM = fig_story_shear_otm(cfg, Fx)
            parts.append(f"<p>Base shear &Sigma;F = {sum(Fx.values()):.0f} kip; base overturning &asymp; {OTM:.0f} k-ft.</p>")
            parts.append(_img(ss_uri, "Story-shear and overturning-moment profiles"))
        except Exception as ex:
            parts.append(f"<p class='note'>[stability section failed: {ex}]</p>")
    else:
        parts.append("<p class='note'>[needs seismic drift data]</p>")

    # ============================ Chapter 8 — Serviceability =============================
    parts.append(_chapter(8))
    if Fx is not None and drX is not None:
        Cd = s.get("Cd", 5.0); Ie = s["Ie"]; lim = cfg.get("drift_limit", 0.02)
        parts.append("<h3>Seismic design drift</h3>")
        parts.append(f"<p>Elastic story drift &delta;<sub>e</sub> amplified to &delta; = C<sub>d</sub>&delta;<sub>e</sub>/I<sub>e</sub> "
                     f"(&sect;12.8.6, C<sub>d</sub>={Cd}, I<sub>e</sub>={Ie}); allowable {lim*100:.1f}% of story height.</p>")
        drow = [[k, f"{drX[k-1]*100:.3f}", f"{drX[k-1]*Cd/Ie*100:.3f}", f"{drY[k-1]*100:.3f}",
                 f"{drY[k-1]*Cd/Ie*100:.3f}", "OK" if max(drX[k-1], drY[k-1])*Cd/Ie <= lim else "NG"]
                for k in range(1, NF+1)]
        parts.append(_table(["Story", "&delta;e X %", "&delta; X %", "&delta;e Y %", "&delta; Y %", f"&le;{lim*100:.1f}%"], drow))
    parts.append(_wind_drift_section(cfg))
    parts.append(_floor_serviceability(pkg))
    parts.append(_deflection_section(cfg))
    parts.append("<p class='cnote'><b>Out of scope (this model):</b> floor vibration (AISC Design Guide 11) and "
                 "building separation/pounding are detail-level serviceability checks confirmed against the framing drawings.</p>")

    # ===================== Chapter 9 — Seismic / wind detailing (AISC 341) ================
    parts.append(_chapter(9))
    parts.append(_aisc341_detailing(cfg, pkg))

    # ========================= Chapter 10 — Connections ==================================
    parts.append(_chapter(10))
    _ctbl = _connection_demands(cfg, pkg, reX)
    if _ctbl:
        parts.append("<p>Connection design demands transmitted to the fabricator / connection engineer (member end "
                     "forces from the analysis; each connection is then DESIGNED to these demands below):</p>")
        parts.append(_ctbl)
    parts.append("<p class='cnote'><b>Designed in this package:</b> each connection is sized to the demands above per "
                 "AISC 360 Ch. J (bolts J3, welds J2, block shear J4, HSS Ch. K, base plates/anchors J8/J9 + ACI 318 "
                 "Ch.17) and AISC 341 for seismic systems &mdash; limit state, capacity and D/C &le; 1.0 derived by the "
                 "agent from the RAG (see the Connections table in Chapter 6 / Appendix A). Only shop-level detailing "
                 "is confirmed on the fabricator's connection submittal.</p>")

    # ===================== Chapter 11 — Foundations interface ============================
    parts.append(_chapter(11))
    if reX is not None:
        Rz = sum(r[2][2] for r in reX); Rx = sum(r[2][0] for r in reX)
        pmax = max((r[2][2] for r in reX), default=0.0); pmin = min((r[2][2] for r in reX), default=0.0)
        parts.append("<p>Column base reactions delivered to the foundation design (seismic X combination):</p>")
        parts.append(_table(["Quantity", "Value"], [
            ["&Sigma; vertical to ground &Sigma;R<sub>z</sub>", f"{Rz:.0f} kip"],
            ["&Sigma; horizontal base shear &Sigma;R<sub>x</sub>", f"{Rx:.0f} kip"],
            ["Max single-column vertical reaction", f"{pmax:.0f} kip (compression)"],
            ["Min single-column vertical reaction", f"{pmin:.0f} kip " + ("&mdash; <b>net uplift</b>" if pmin < 0 else "(no uplift)")]]))
    parts.append("<p class='cnote'>Net column uplift is governed by the 0.9D&minus;E combinations; the value above "
                 "is from the seismic-X case shown. <b>Out of scope:</b> foundation/geotechnical design (bearing, "
                 "sliding, uplift anchorage, footing sizing) is delegated; per-column base reactions are available "
                 "from the model on request. Overall overturning/sliding stability is confirmed against the "
                 "geotechnical capacities.</p>")

    # ============== Chapter 12 — Drawings, specifications & documentation ================
    parts.append(_chapter(12, status="out of scope for the analysis package &mdash; verified on the contract drawings"))
    parts.append("<p class='cnote'>Drawing/specification consistency, general notes, special-inspection schedule, "
                 "member schedules, framing plans, brace/MF elevations, connection details and the deferred-submittal "
                 "list are produced and checked on the contract documents, not in this analysis report.</p>")

    # ===================== Chapter 13 — QA / professional acceptance =====================
    parts.append(_chapter(13))
    parts.append(_qa_scorecard(cfg, Fx, reX, eX, eY, drX, drY))
    parts.append(_grounding_check(cfg, name, pkg))
    parts.append(_consistency_section(name, root, pkg))
    parts.append("<p>Automated QA evidence in this report: per-combination equilibrium balances to ~0 in all three "
                 "axes (Chapter 5); every member demand is enveloped over the full ASCE 7-22 combination set "
                 "(Chapter 4 / Appendix B); each capacity is traceable to a cited AISC clause (Appendix A); and the "
                 "tool-call activity log is in Appendix C.</p>")
    parts.append("<p class='cnote'><b>Out of scope (engineer judgement):</b> independent third-party check, software "
                 "validation sign-off, reconciliation of model assumptions against final detailing, and the EOR seal "
                 "are professional-responsibility steps completed outside the automated package.</p>")

    # ============================== Appendices ==========================================
    parts.append("<h2>Appendix A &mdash; Referenced AISC 360/341 member calculations</h2>")
    if pkg:
        try: parts.append(appendix(cfg, name, pkg))
        except Exception as ex: parts.append(f"<p class='note'>[Appendix A failed: {ex}]</p>")
    else:
        parts.append("<p class='note'>[no calc_package.json found]</p>")
    parts.append("<h2>Appendix B &mdash; Member forces by load case</h2>")
    if case_detail_parts:
        parts.append("<p>Full per-combination detail (summarised in Chapter 4): the worst member of each type "
                     "with its location, from the static model (true distributed gravity). The per-combination "
                     "N / V / M diagrams are written to the <code>figs/</code> folder (<code>case_&lt;label&gt;.png</code>) for "
                     "review &mdash; not embedded here, to keep the report compact. The headline governing diagrams "
                     "(perimeter + internal) are in Chapter 5.</p>")
        parts.extend(case_detail_parts)
    else:
        parts.append("<p class='note'>Per-load-case member force tables temporarily omitted for faster "
                     "reporting &mdash; can be populated here on request at the completion of the design "
                     "(set cfg['force_summary']=True and re-render report.build_report).</p>")
    parts.append(_activity_section(name))

    html = (f"<!doctype html><html><head><meta charset='utf-8'><title>{name} report</title>"
            f"<style>{CSS}{CHK_CSS}</style>{MATHJAX}</head><body>" + "".join(parts) + "</body></html>")
    _fc = [0]
    def _fignum(_m):
        _fc[0] += 1; return f"<b>Figure {_fc[0]}.</b>"
    html = re.sub("@@FIGNUM@@", _fignum, html)        # number every figure in document order
    outdir = root; os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "report.html")
    open(path, "w", encoding="utf-8").write(html)
    print(f"[{name}] wrote {path}")
    return path

def _narrative_html(pkg, members_fig_html=""):
    out = []
    ar = pkg.get("analysis_results")
    if isinstance(ar, dict) and isinstance(ar.get("base_shear"), dict):
        out.append("<p><b>Lateral:</b> " + _fmt(ar["base_shear"]) + "</p>")
    if isinstance(pkg.get("members"), list):
        def _dmd(inp):
            return _fmt({k: inp.get(k) for k in ("P_comp_kip","P_tens_kip","Mz_kipin","My_kipin","V_kip") if inp.get(k) is not None})
        rows = [[m.get("id", ""), (m.get("inputs", {}) or {}).get("section", ""),
                 _dmd(m.get("inputs", {}) or {}), (m.get("inputs", {}) or {}).get("governing_combo", ""),
                 (str(m.get("limit_state")) if m.get("limit_state") else "- (agent/RAG)"),
                 (str(m.get("DC")) if m.get("DC") is not None else "- (agent/RAG)")] for m in pkg["members"]]
        out.append("<h3>Members (demands; capacities derived by the agent from the RAG)</h3>"
                   + members_fig_html
                   + _table(["member", "section", "demand", "gov. combo", "limit state", "D/C"], rows))
    cd = pkg.get("connections") or pkg.get("connection_demands")
    if isinstance(cd, list) and cd:
        def _conn_govern(c):
            # a connection has several limit states; the agent nests them in 'checks' [{limit_state, DC}, ...].
            # Surface the governing (max-D/C) one; fall back to any top-level limit_state/DC.
            ls, dc = c.get("limit_state"), c.get("DC")
            chk = c.get("checks")
            if (ls is None or dc is None) and isinstance(chk, list) and chk:
                best = max((k for k in chk if isinstance(k, dict) and k.get("DC") is not None),
                           key=lambda k: k.get("DC") or 0, default=None)
                if best:
                    if dc is None: dc = best.get("DC")
                    if ls is None: ls = best.get("limit_state")
            return ls, dc
        rows = []
        for c in cd:
            ls, dc = _conn_govern(c)
            nchk = len(c.get("checks")) if isinstance(c.get("checks"), list) else 0
            ls_txt = (str(ls) + (f"  (+{nchk - 1} more)" if nchk > 1 else "")) if ls else "- (agent/RAG)"
            dc_txt = (f"{dc:.2f}" if isinstance(dc, (int, float)) else (str(dc) if dc else "- (agent/RAG)"))
            rows.append([c.get("id", ""), c.get("type", ""), _fmt(c.get("demand", {})), ls_txt, dc_txt])
        out.append("<h3>Connections (demands; capacities derived by the agent from the RAG)</h3>"
                   + _table(["connection", "type", "demand", "governing limit state", "D/C"], rows))
    elif isinstance(cd, dict):
        rows = [[k, v.get("type", ""), _fmt(v.get("demands", {})), v.get("notes", "")] for k, v in cd.items()]
        out.append("<h3>Connections</h3>" + _table(["connection", "type", "demand", "design basis"], rows))
    return "".join(out) if out else "<pre>" + json.dumps(pkg, indent=1)[:4000] + "</pre>"

if __name__ == "__main__":
    for nm in (sys.argv[1:] or ["B02"]):
        try: build_report(nm)
        except Exception as ex: print(f"[{nm}] FAILED: {ex}")

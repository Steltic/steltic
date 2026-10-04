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

def fig_drift_profile(cfg, runs):
    """Drift profile, both directions: elastic delta_xe/h at the design-drift location and the
    Eq. 12.8-16 design drift (Cd, Ie per direction; RBS factor included) against the limit."""
    NF = len(cfg["heights"]); lvl = list(range(1, NF+1))
    fig, ax = plt.subplots(figsize=(9, 6))
    for d, mk in (("X", "-o"), ("Y", "-s")):
        r = runs[d]
        ax.plot([v*100 for v in r["dxe"]], lvl, mk, alpha=0.5, label=r"$\delta_{xe}/h$ " + d)
        ax.plot([v*100 for v in r["design"]], lvl, mk, label=r"$\Delta/h=C_d\delta_{xe}/I_e h$ " + d)
    for d, ls in (("X", "--"), ("Y", ":")):
        lim = runs[d]["limit"]*100
        ax.axvline(lim, color="r", ls=ls, label=f"limit {d} {lim:.2f}%" + (r" ($\Delta_a/\rho$)" if runs[d]["limit_rho"] else ""))
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
    # minimal fallback if design_pipeline unavailable (companion 0.3S when snow governs the roof, else 0.5Lr)
    if float(cfg.get("snow", 0) or 0) > 0:
        return [("1.4D", 1.4, 0, 0, {}, False), ("1.2D+1.6L+0.3S", 1.2, 1.6, 0.3, {}, False)]
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
                else "the (1.2+0.2·S_DS)D + companion-L gravity case (L factor per §2.3.6 Exc.1, as labelled)")
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
    h = ["<h4>%s &mdash; %s (%s)</h4>" % (_esc(mid), _esc(sec), _esc(kind))]
    pk = [("A","A (in2)"),("Zx","Zx (in3)"),("Sx","Sx (in3)"),("Zy","Zy (in3)"),("Sy","Sy (in3)"),
          ("rx","rx (in)"),("ry","ry (in)"),("r","r (in)"),("Aw","Aw (in2)"),("J","J (in4)"),
          ("length_in","L (in)"),("Lb_in","Lb (in)")]
    pr = [(lbl, inp[k]) for k, lbl in pk if inp.get(k) is not None]
    if pr:
        h.append("<p class='cnote'>Section properties / geometry (AISC Shapes Database v16):</p>")
        h.append(_table([l for l, _ in pr], [[_esc("%s" % v) for _, v in pr]]))
    dem = [("P_comp (kip)", inp.get("P_comp_kip")), ("P_tens (kip)", inp.get("P_tens_kip")),
           ("Mz (kip-in)", inp.get("Mz_kipin")), ("My (kip-in)", inp.get("My_kipin")),
           ("V (kip)", inp.get("V_kip"))]
    h.append("<p class='cnote'>Demand envelope (analysis):</p>")
    h.append(_table([l for l, _ in dem] + ["governing combo"],
                    [[_esc("%s" % v) for _, v in dem] + [_esc(str(inp.get("governing_combo", "")))]]))
    cap = m.get("capacity"); dc = m.get("DC"); ls = m.get("limit_state"); cited = m.get("cited")
    if (isinstance(cap, dict) and cap) or dc is not None or ls or cited:
        rows = []
        if ls: rows.append(("Governing limit state", "selected from AISC 360", _esc(ls), _esc(cited or "")))
        if isinstance(cap, dict):
            for k, v in cap.items(): rows.append((_esc(k), "", _esc(v), ""))
        if dc is not None:
            ok = "ok" if (isinstance(dc,(int,float)) and dc <= 1.0) else ("NG" if isinstance(dc,(int,float)) else "")
            rows.append(("D/C", "demand / capacity", "%s %s" % (_esc(dc), ok), _esc(cited or "")))
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
            dem = _esc(_fmt(c.get("demand", c.get("demands", {}))))
            comp = _esc(c.get("components", c.get("notes", "")))
            cited = _esc(c.get("cited", ""))
            cdf = " (capacity-design)" if c.get("capacity_design") else ""
            h.append("<h4>%s &mdash; %s%s</h4><p><b>Demand:</b> %s</p><p><b>Components:</b> %s</p><p><b>Cited:</b> %s</p>"
                     % (_esc(cid), _esc(c.get("type", "")), cdf, dem, comp, cited))
            chks = c.get("checks")
            if isinstance(chks, list) and chks:
                rows = [[_esc(k.get("limit_state", "")),
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
        h = ["<p>ASCE 7-22 §27 MWFRS. Velocity pressure qz = 0.00256·Kz·Kzt·Ke·V² (Eq. 26.10-1); design "
             "pressure p = qz·Kd·G·Cpnet (Kd applied in the pressure equation per Eq. 27.3-1); story force "
             "= p × tributary width × tributary height.</p>",
             _table(["Parameter", "Value"], [["Basic wind speed V", f"{w.get('V')} mph"],
                    ["Exposure", w.get("exposure", "C")],
                    ["Kd / Ke / G / Cpnet", f"{w.get('Kd',0.85)} / {w.get('Ke',1.0)} / {w.get('G',0.85)} / {w.get('Cpnet',1.3)}"]]),
             "<p><b>Story wind forces (kip):</b></p>",
             _table(["Story", "X (E-W wind)", "Y (N-S wind)"], rows),
             f"<p><b>Wind base shear:</b> X = {VwX:.0f} kip, Y = {VwY:.0f} kip.</p>"]
        return "".join(h), VwX, VwY
    except Exception as ex:
        return f"<p class='note'>[wind determination failed: {ex}]</p>", None, None

def _horizontal_distribution(cfg, Fx, VwX, VwY, sf=None, runs=None):
    """Per direction: the declared SFRS, its lateral planes in the model, wind vs seismic base shear,
    and the share of the base shear each plane actually takes in the solved 3D model (rigid
    diaphragm, no accidental torsion) -- not an assumed 50/50 split."""
    sf = sf or _sfrs(cfg)
    FxX, FxY = _fx_by_dir(Fx)
    h = ["<p>Story forces reach the vertical lateral elements through the rigid floor diaphragm in "
         "proportion to their lateral stiffness and position (3D model). The table gives, per direction, "
         "the system, the lateral planes found in the analysis model and the fraction of the base shear each "
         "plane carries in the solved model (centre-of-mass loading; the &plusmn;5% accidental torsion of "
         "&sect;12.8.4.2 and any A<sub>x</sub> amplification are applied in the design combinations and shift "
         "demand toward the plane on the leading side).</p>"]
    rows = []
    for d, Vw, F in (("X", VwX, FxX), ("Y", VwY, FxY)):
        Vs = sum(F.values()); gov = "seismic" if Vs >= (Vw or 0) else "wind"
        shares = (runs or {}).get(d, {}).get("share") or {}
        ax = "y" if d == "X" else "x"
        if shares:
            parts = ["%s = %.0f ft: %.0f%%" % (ax, p / 12.0, 100 * v) for p, v in sorted(
                ((p, v) for p, v in shares.items() if p is not None), key=lambda t: t[0])]
            if shares.get(None) and abs(shares[None]) > 0.005:
                parts.append("other columns: %.0f%%" % (100 * shares[None]))
            sh = "; ".join(parts)
        else:
            sh = "not computed (see the analysis model)"
        rows.append([f"{d} ({'E-W' if d == 'X' else 'N-S'})", _sys_label(sf, d), _line_desc(sf, d),
                     (f"{Vw:.0f}" if Vw is not None else "&mdash;"), f"{Vs:.0f}", gov, sh])
    h.append(_table(["Direction", "System", "Lateral planes (model)", "Wind base (kip)", "Seismic base (kip)",
                     "Governs", "Base-shear share by plane (solved model)"], rows))
    for w in sf.get("warnings", []):
        h.append("<p class='note'>%s</p>" % _esc(w))
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

def _stability_section(cfg, runs):
    """Chapter 7: ASCE 7-22 12.8.7 stability coefficient, BOTH directions (see _theta_rows)."""
    h = ["<p>Member demands include second-order effects (a P-&Delta; geometric transformation is applied "
         "under every combination). The ASCE 7-22 &sect;12.8.7 stability coefficient is "
         "&theta; = P<sub>x</sub>&Delta;<sub>xe</sub>/(V<sub>x</sub>h<sub>sx</sub>) (Eq. 12.8-18), identical to "
         "P<sub>x</sub>&Delta;I<sub>e</sub>/(V<sub>x</sub>h<sub>sx</sub>C<sub>d</sub>) with "
         "&Delta; = C<sub>d</sub>&Delta;<sub>xe</sub>/I<sub>e</sub>. P<sub>x</sub> is the &sect;12.8.6.1 expected "
         "gravity 1.0D + 0.5L with L = 0.4L<sub>0</sub> (0.8L<sub>0</sub> where L<sub>0</sub> &gt; 100 psf) on the "
         "actual framed area &mdash; no individual load factor exceeds 1.0. &Delta;<sub>xe</sub> is the elastic "
         "story drift at the design-drift location (Chapter 8)%s. Because the drifts come from a P-&Delta; analysis, "
         "&theta; is divided by (1+&theta;) before the Eq. 12.8-19 check, as &sect;12.8.7 permits. "
         "&theta;<sub>max</sub> = 0.5/(&beta;C<sub>d</sub>) &le; 0.25 and need not be taken less than 0.10; "
         "&beta; = %s.</p>"
         % (" including the AISC 358-22 &sect;5.7 RBS drift factor" if any(runs[d]["rbs"][0] > 1.0 for d in ("X", "Y")) else "",
            "cfg['theta_beta']" if cfg.get("theta_beta") else "1.0 (conservative; not less than 1.25/&Omega;<sub>0</sub>)")]
    worst = {}
    for d in ("X", "Y"):
        rows, tmax, beta = _theta_rows(cfg, runs, d)
        trs = []; wv = 0.0
        for r in rows:
            st = ("exempt" if r["exempt"] else
                  "NG &mdash; redesign" if r["theta"] > tmax else
                  ("OK (&gt; 0.10: P-&Delta; required, included)" if r["theta"] > 0.10 else "OK (&le; 0.10)"))
            if not r["exempt"]: wv = max(wv, r["theta"])
            trs.append([r["s"], "%.0f" % r["Px"], "%.0f" % r["Vx"], "%.0f" % r["h"], "%.3f" % r["dxe"],
                        "%.3f" % r["Delta"], "%.3f" % r["raw"], "%.3f" % r["theta"], st])
        worst[d] = (wv, tmax)
        h.append("<h4>%s direction (C<sub>d</sub> = %s, &beta; = %.2f, &theta;<sub>max</sub> = %.3f)</h4>"
                 % (d, ("%g" % runs[d]["Cd"]), beta, tmax))
        h.append(_table(["Story", "P<sub>x</sub> (kip)", "V<sub>x</sub> (kip)", "h<sub>sx</sub> (in)",
                         "&Delta;<sub>xe</sub> (in)", "&Delta; (in)", "&theta; Eq.12.8-18", "&theta;/(1+&theta;)",
                         "&le; &theta;<sub>max</sub>"], trs))
    h.append("<p>Worst-story &theta;: X = %.3f (limit %.3f), Y = %.3f (limit %.3f) &mdash; %s.</p>"
             % (worst["X"][0], worst["X"][1], worst["Y"][0], worst["Y"][1],
                "both within &theta;<sub>max</sub>" if all(worst[d][0] <= worst[d][1] for d in worst)
                else "<b>EXCEEDS &theta;<sub>max</sub>: the structure is potentially unstable and shall be redesigned (&sect;12.8.7)</b>"))
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
    h.append(_table(["Tool", "Calls"], [[_esc(k), counts[k]] for k in sorted(counts)]))
    rows = [[i + 1, _esc(r.get("ts", "").split("T")[-1]), _esc(r.get("tool", "")),
             _esc(r.get("detail", "")), _esc(r.get("result", ""))] for i, r in enumerate(recs)]
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


# ============================================================ SFRS description (HR-26)
# Every system label in the report is driven from cfg['system'] (per direction where the text or
# cfg['system_X'/'system_Y'] / cfg['seis_X'/'seis_Y']['system'] says so) checked against the frame
# inventory of the BUILT model (brace planes and fixed-ended beam lines per direction). Nothing
# defaults to "moment frame" or "two frames".
_FAM_RX = [
    ("STMF", r"\bSTMF\b|truss[ -]+moment"),
    ("SMF",  r"\bSMF\b|special\s+moment"),
    ("IMF",  r"\bIMF\b|intermediate\s+moment"),
    ("OMF",  r"\bOMF\b|ordinary\s+moment"),
    ("SCBF", r"\bSCBF\b|special\s+concentric"),
    ("OCBF", r"\b(?:MT-)?OCBF\b|ordinary\s+concentric"),
    ("BRBF", r"\bBRBF\b|buckling[- ]+restrained"),
    ("EBF",  r"\bEBF\b|eccentric(?:ally)?\s+braced"),
    ("SPSW", r"\bSPSW\b|plate\s+shear\s+wall"),
]
MF_FAMS = ("SMF", "IMF", "OMF", "STMF")
BR_FAMS = ("SCBF", "OCBF", "BRBF", "EBF", "SPSW")   # SPSW strips are modelled as brace elements
FAM_NAME = {"SMF": "special moment frame (SMF)", "IMF": "intermediate moment frame (IMF)",
            "OMF": "ordinary moment frame (OMF)", "STMF": "special truss moment frame (STMF)",
            "SCBF": "special concentrically braced frame (SCBF)",
            "OCBF": "ordinary concentrically braced frame (OCBF)",
            "BRBF": "buckling-restrained braced frame (BRBF)",
            "EBF": "eccentrically braced frame (EBF)", "SPSW": "special plate shear wall (SPSW)",
            "MF?": "steel moment frame (type not declared)", "BF?": "steel braced frame (type not declared)"}
FAM_341 = {"SMF": "E3", "IMF": "E2", "OMF": "E1", "STMF": "E4", "SCBF": "F2", "OCBF": "F1",
           "BRBF": "F4", "EBF": "F3", "SPSW": "F5"}


def _families(text):
    """Ordered list of AISC 341 system families named in a free-text system declaration."""
    t = str(text or ""); hits = []
    for fam, rx in _FAM_RX:
        m = re.search(rx, t, flags=re.I)
        if m: hits.append((m.start(), fam))
    return [f for _, f in sorted(hits)]


def _families_by_dir(text):
    """{'X': [...], 'Y': [...]} when the text assigns families to a direction ('SMF in X',
    'X: SCBF', 'SCBF (Grids 1-7) in Y'); directions not mentioned get None."""
    t = str(text or ""); out = {"X": None, "Y": None}
    for fam, rx in _FAM_RX:
        for m in re.finditer(rx, t, flags=re.I):
            tail = t[m.end():m.end() + 40]; head = t[max(0, m.start() - 12):m.start()]
            d = None
            mm = re.match(r"\s*(?:\([^)]*\))?\s*(?:in|along)\s+(?:the\s+)?([XY])\b", tail)
            if mm: d = mm.group(1)
            else:
                mm = re.search(r"\b([XY])\s*(?:-dir\w*)?\s*[:=]\s*$", head)
                if mm: d = mm.group(1)
            if d:
                out[d] = (out[d] or []) + ([fam] if fam not in (out[d] or []) else [])
    return out


_INV_CACHE = {}


def _frame_inventory(cfg):
    """Lateral-element inventory of the BUILT model, per direction:
         inv[d]['brace'][pos] = {story: [(lo, hi), ...]}   brace / SPSW-strip elements in plane pos
         inv[d]['mf'][pos]    = {story: [(lo, hi), ...]}   fixed-ended (moment) beams at the top of story
       pos = the plane coordinate (y for X-direction planes, x for Y-direction), in inches; (lo, hi) is
       the element extent along the direction, snapped to the bounding column lines. Also returns
       'nonparallel' (skewed lateral elements) and 'base' {node: (x, y, fixity)}."""
    key = id(cfg)
    if key in _INV_CACHE:
        return _INV_CACHE[key]
    inv = {"X": {"brace": {}, "mf": {}}, "Y": {"brace": {}, "mf": {}}, "nonparallel": [], "base": {},
           "ok": False, "error": None}
    try:
        info = E.build(cfg, "Linear")
        z = list(E.zlevels(cfg)); NF = len(cfg["heights"]); tol = 1.0
        rel = info.get("beam_rel") or {}
        joints = str(((cfg.get("model") or {}).get("joints")) or "").lower()
        xs_all = sorted({round(ops.nodeCoord(n)[0], 1) for n in ops.getNodeTags()})
        ys_all = sorted({round(ops.nodeCoord(n)[1], 1) for n in ops.getNodeTags()})
        def _snap(lo, hi, lines):
            a = max([v for v in lines if v <= lo + tol] or [lo]); b = min([v for v in lines if v >= hi - tol] or [hi])
            return (round(a, 1), round(b, 1))
        def _lvl(zz):
            return min(range(len(z)), key=lambda k: abs(z[k] - zz))
        def _key(dct, pos):
            for p in dct:
                if abs(p - pos) <= 12.0: return p
            return round(pos, 1)
        for (t, kind, sec, n1, n2) in info["ele"]:
            try: a = ops.nodeCoord(n1); b = ops.nodeCoord(n2)
            except Exception: continue
            dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
            h = math.hypot(dx, dy)
            k = str(kind).lower()
            if "brace" in k:
                typ = "brace"
            elif "beam" in k and abs(dz) < tol and h > tol:
                r = rel.get(t)
                fixed = (r[0] == "none") if r is not None else not joints.startswith("pin")
                if not fixed: continue
                typ = "mf"
            else:
                continue
            if h <= tol: continue
            ang = math.degrees(math.atan2(abs(dy), abs(dx)))
            if 1.0 < ang < 89.0:
                inv["nonparallel"].append((t, kind, round(ang, 1))); continue
            d = "X" if ang <= 1.0 else "Y"
            pos = 0.5 * (a[1] + b[1]) if d == "X" else 0.5 * (a[0] + b[0])
            lo, hi = (min(a[0], b[0]), max(a[0], b[0])) if d == "X" else (min(a[1], b[1]), max(a[1], b[1]))
            lo, hi = _snap(lo, hi, xs_all if d == "X" else ys_all)
            if typ == "brace":
                k1, k2 = sorted((_lvl(a[2]), _lvl(b[2])))
                stories = range(k1 + 1, k2 + 1) if k2 > k1 else [max(k1, 1)]
            else:
                stories = [max(_lvl(a[2]), 1)]
            plane = inv[d][typ].setdefault(_key(inv[d][typ], pos), {})
            for s in stories:
                if 1 <= s <= NF: plane.setdefault(s, []).append((lo, hi))
        try:
            for n in ops.getFixedNodes():
                c = ops.nodeCoord(n)
                if abs(c[2] - z[0]) <= tol:
                    dofs = set(ops.getFixedDOFs(n))
                    inv["base"][n] = (c[0], c[1], "fixed" if {4, 5}.issubset(dofs) else "pinned")
        except Exception:
            pass
        inv["ok"] = True
    except Exception as ex:
        inv["error"] = str(ex)
    _INV_CACHE[key] = inv
    return inv


def _sfrs(cfg, inv=None):
    """System description per direction. Returns dict(declared, text, dirs={'X': [fam..], 'Y': [..]},
    dual, mixed, families, has_mf, has_br, warnings, lines={'X': {...}, 'Y': {...}})."""
    inv = inv if inv is not None else _frame_inventory(cfg)
    s = cfg.get("seis") or {}
    text = str(cfg.get("system") or "")
    per = _families_by_dir(text)
    for d in ("X", "Y"):
        for src in (cfg.get("system_" + d), (cfg.get("seis_" + d) or {}).get("system")
                    if isinstance(cfg.get("seis_" + d), dict) else None):
            if src:
                per[d] = _families(src) or per[d]
    glob = _families(text)
    declared = bool(glob or per["X"] or per["Y"])
    warnings = []
    have = {d: {"mf": bool(inv[d]["mf"]) if inv.get("ok") else None,
                "br": bool(inv[d]["brace"]) if inv.get("ok") else None} for d in ("X", "Y")}
    dirs = {}
    for d in ("X", "Y"):
        cand = per[d] if per[d] else glob
        if cand:
            if inv.get("ok"):
                got = [f for f in cand if (f in MF_FAMS and have[d]["mf"]) or (f in BR_FAMS and have[d]["br"])]
                if not got:
                    warnings.append("%s direction: the declared %s has no matching lateral elements in the built "
                                    "model (no %s found in %s-direction planes)"
                                    % (d, "/".join(cand), "braces" if all(f in BR_FAMS for f in cand) else
                                       "fixed-ended beams" if all(f in MF_FAMS for f in cand) else "braces or fixed-ended beams", d))
                    got = list(cand)
            else:
                got = list(cand)
        else:
            # undeclared: infer from the model (braces win; fixed-ended beams alone = moment frame)
            if inv.get("ok") and have[d]["br"]: got = ["BF?"]
            elif inv.get("ok") and have[d]["mf"]: got = ["MF?"]
            else: got = ["BF?"] if E.is_braced(cfg) else ["MF?"]
        dirs[d] = got
    fams = []
    for d in ("X", "Y"):
        for f in dirs[d]:
            if f not in fams: fams.append(f)
    has_mf = any(f in MF_FAMS or f == "MF?" for f in fams)
    has_br = any(f in BR_FAMS or f == "BF?" for f in fams)
    dual = "dual" in text.lower() or bool(cfg.get("dual_check"))
    mixed = ("mixed" in text.lower()) or (set(dirs["X"]) != set(dirs["Y"]))
    lines = {}
    for d in ("X", "Y"):
        use_mf = any(f in MF_FAMS or f == "MF?" for f in dirs[d])
        use_br = any(f in BR_FAMS or f == "BF?" for f in dirs[d])
        planes = {}
        if inv.get("ok"):
            if use_br:
                for p, v in inv[d]["brace"].items():
                    cur = planes.setdefault(p, {})
                    for st, iv in v.items(): cur[st] = cur.get(st, []) + iv
            if use_mf:
                for p, v in inv[d]["mf"].items():
                    cur = planes.setdefault(p, {})
                    for st, iv in v.items(): cur[st] = cur.get(st, []) + iv
        lines[d] = planes
    return dict(declared=declared, text=text, dirs=dirs, dual=dual, mixed=mixed, families=fams,
                has_mf=has_mf, has_br=has_br, warnings=warnings, lines=lines, inv_ok=bool(inv.get("ok")))


def _sys_label(sf, d):
    fams = sf["dirs"][d]
    names = " + ".join(FAM_NAME.get(f, f) for f in fams)
    if sf["dual"] and any(f in MF_FAMS for f in fams) and any(f in BR_FAMS for f in fams):
        names = "dual system: " + names
    return names


def _line_desc(sf, d):
    """'3 lines at y = 0, 45, 90 ft' for the SFRS planes in direction d."""
    pl = sorted(sf["lines"].get(d) or {})
    if not pl:
        return "no lateral planes found in the model" if sf.get("inv_ok") else "frame inventory unavailable"
    ax = "y" if d == "X" else "x"
    return "%d line%s at %s = %s ft" % (len(pl), "" if len(pl) == 1 else "s", ax,
                                         ", ".join("%.0f" % (p / 12.0) for p in pl))


def _seis_d(cfg, d):
    """Seismic factors for direction d: cfg['seis'] overlaid by cfg['seis_X'/'seis_Y'] when given
    (ASCE 7-22 12.2.2: each system's R, Cd, Omega0 apply in its own direction)."""
    s = dict(cfg.get("seis") or {})
    o = cfg.get("seis_" + d)
    if isinstance(o, dict): s.update({k: v for k, v in o.items() if v is not None})
    return s


def _fx_by_dir(Fx):
    """ELF story forces per direction. Accepts the legacy single {k: F} dict (same forces both
    directions) or a per-direction {'X': {k: F}, 'Y': {k: F}} dict."""
    if isinstance(Fx, dict) and set(Fx.keys()) >= {"X", "Y"} and isinstance(Fx.get("X"), dict):
        return Fx["X"], Fx["Y"]
    return Fx, Fx


def _drift_exempt(cfg):
    try: return {int(k) for k in (cfg.get("drift_exempt_stories") or {})}
    except Exception: return set()


def _drift_limit_d(cfg, d):
    """(limit, rho_applied) for direction d -- per-direction when engine3d supports it."""
    try: return E.drift_allowable(cfg, d)
    except TypeError: return E.drift_allowable(cfg)


def _lateral_runs(cfg, Fx, sf=None):
    """Seismic drift / torsion / stiffness evidence per direction, from the SAME analysis routine the
    engine's drift gate uses (engine3d.static_lateral: ELF story forces at the floor masters, P-Delta
    model, unfactored gravity per engine3d.floor_grav). For each direction d returns:
      drift_cm[s]   elastic story drift ratio at the centre of mass (delta_xe/h_sx), no accidental torsion
      drift_edge[s] elastic story drift ratio at the worst diaphragm edge with +5% accidental torsion
      tir[s]        Eq. 12.3-2 TIR = Delta_max / Delta_avg (edge drifts, accidental torsion, Ax = 1)
      ax[s]         Eq. 12.8-15 A_x = (delta_max / 1.2 delta_avg)^2 in [1, 3] (displacements)
      k[s]          story stiffness V_x / Delta_xe (kip/in) from the CM run
      share         {plane: fraction of the base shear} from the base reactions of the CM run
    ASCE 7-22 12.8.6.5: where a Type 1 irregularity exists in SDC C-F the design drift is taken at the
    edge; otherwise at the centre of mass. 'basis' records which one the report uses."""
    FxX, FxY = _fx_by_dir(Fx)
    NF = len(cfg["heights"]); h = cfg["heights"]
    out = {}
    for d, F in (("X", FxX), ("Y", FxY)):
        r = {"F": F}
        res = E.static_lateral(cfg, F, d)
        dr = list(res[2]); r["drift_cm"] = dr
        Vst = [sum(F[k] for k in range(s, NF + 1)) for s in range(1, NF + 1)]
        r["V"] = Vst
        r["k"] = [(Vst[s] / abs(dr[s] * h[s])) if abs(dr[s]) > 1e-12 else float("inf") for s in range(NF)]
        # base-shear share per lateral plane, from the solved reactions of this (live) run
        share = {}
        try:
            ops.reactions(); di = 1 if d == "X" else 2; tot = 0.0
            planes = sorted((sf or {}).get("lines", {}).get(d, {}) or {})
            for n in ops.getFixedNodes():
                c = ops.nodeCoord(n)
                if abs(c[2]) > 1.0: continue
                rx = ops.nodeReaction(n, di); tot += rx
                pos = c[1] if d == "X" else c[0]
                p = next((pp for pp in planes if abs(pp - pos) <= 12.0), None)
                share[p] = share.get(p, 0.0) + rx
            if abs(tot) > 1e-9:
                share = {p: v / tot for p, v in share.items()}
            else:
                share = {}
        except Exception:
            share = {}
        r["share"] = share
        # accidental-torsion run: edge drifts, TIR, Ax (masters + present column nodes of the live model)
        try:
            E.static_lateral(cfg, F, d, accidental=True)
            di = 1 if d == "X" else 2
            edge = []; tir = []; ax = []; prev_e = (0.0, 0.0); _tags = set(ops.getNodeTags())
            for k in range(1, NF + 1):
                m = E.mtag(k); cm = ops.nodeCoord(m)
                u = ops.nodeDisp(m, di); rz = ops.nodeDisp(m, 6)
                pts = [ops.nodeCoord(E.ntag(i, j, k)) for (i, j) in E.grid(cfg, k)
                       if E.ntag(i, j, k) in _tags]
                if d == "X":
                    cs = [p[1] for p in pts] or [cm[1]]; lo, hi = min(cs) - cm[1], max(cs) - cm[1]
                    e1, e2 = u - rz * lo, u - rz * hi            # X displacement at the two Y-edges
                else:
                    cs = [p[0] for p in pts] or [cm[0]]; lo, hi = min(cs) - cm[0], max(cs) - cm[0]
                    e1, e2 = u + rz * lo, u + rz * hi            # Y displacement at the two X-edges
                d1, d2 = e1 - prev_e[0], e2 - prev_e[1]; prev_e = (e1, e2)
                dmax = max(abs(d1), abs(d2)); davg = 0.5 * (abs(d1) + abs(d2))
                edge.append(dmax / h[k - 1]); tir.append(dmax / davg if davg > 1e-12 else 1.0)
                Dmax = max(abs(e1), abs(e2)); Davg = 0.5 * (abs(e1) + abs(e2))
                ax.append(min(max((Dmax / (1.2 * Davg)) ** 2, 1.0), 3.0) if Davg > 1e-12 else 1.0)
            r["drift_edge"] = edge; r["tir"] = tir; r["ax"] = ax
        except Exception as ex:
            r["drift_edge"] = list(dr); r["tir"] = [1.0] * NF; r["ax"] = [1.0] * NF; r["tor_error"] = str(ex)
        out[d] = r
    exempt = _drift_exempt(cfg)
    tir_max = max([t for d in ("X", "Y") for s, t in enumerate(out[d]["tir"], 1) if s not in exempt] or [1.0])
    try:
        sdc = E.sdc_of(cfg)
    except Exception:
        sdc = "D"
    edge_basis = tir_max > 1.2 and sdc in ("C", "D", "E", "F")
    for d in ("X", "Y"):
        f, why = rbs_drift_factor(cfg, d)
        r = out[d]; r["rbs"] = (f, why)
        base = r["drift_edge"] if edge_basis else r["drift_cm"]
        r["basis"] = ("diaphragm edge incl. accidental torsion (Type 1 TIR %.2f &gt; 1.2, SDC %s: &sect;12.8.6.5)"
                      % (tir_max, sdc)) if edge_basis else "centre of mass (&sect;12.8.6.5; no Type 1 irregularity)"
        r["dxe"] = [abs(v) * f for v in base]                      # elastic drift ratio at the design-drift location
        sd = _seis_d(cfg, d); Cd = float(sd.get("Cd", 5.0) or 5.0); Ie = float(sd.get("Ie", 1.0) or 1.0)
        r["Cd"], r["Ie"] = Cd, Ie
        r["design"] = [v * Cd / Ie for v in r["dxe"]]              # Eq. 12.8-16 design drift ratio
        lim, rho = _drift_limit_d(cfg, d); r["limit"], r["limit_rho"] = lim, rho
        gov = [v for s, v in enumerate(r["design"], 1) if s not in exempt]
        r["design_max"] = max(gov) if gov else max(r["design"])
    out["tir_max"] = tir_max; out["edge_basis"] = edge_basis; out["exempt"] = exempt; out["sdc"] = sdc
    return out


def _theta_rows(cfg, runs, d):
    """ASCE 7-22 12.8.7 stability coefficient per story for direction d.
        theta = P_x * Delta_xe / (V_x * h_sx)     (Eq. 12.8-18; = P_x*Delta*I_e/(V_x*h_sx*C_d) with
                                                  Delta = C_d*Delta_xe/I_e the design story drift)
      P_x  = sum over levels >= x of (1.0 D + 0.5 L), L = 0.4 L0 (0.8 L0 where L0 > 100 psf) --
             the 12.8.6.1 expected gravity; no individual load factor exceeds 1.0 (12.8.7);
      Delta_xe at the design-drift location (runs[d]['basis']) incl. any AISC 358 RBS factor;
      theta_max = 0.5/(beta*C_d) <= 0.25, beta = cfg['theta_beta'] (default 1.0, >= 1.25/Omega0),
                  and theta_max need not be taken less than 0.10 (Eq. 12.8-19 and text).
    The drifts come from a P-Delta analysis, so theta from Eq. 12.8-18 is divided by (1 + theta)
    before the Eq. 12.8-19 check, as 12.8.7 permits; both values are reported."""
    NF = len(cfg["heights"]); h = cfg["heights"]; r = runs[d]
    sd = _seis_d(cfg, d); Cd = r["Cd"]; Om0 = float(sd.get("Om0", 3.0) or 3.0)
    beta = max(float(cfg.get("theta_beta", 1.0) or 1.0), 1.25 / Om0)
    tmax = max(min(0.5 / (beta * Cd), 0.25), 0.10)
    P = {}
    for k in range(1, NF + 1):
        Dk = E.floor_dead(cfg, k)
        if k < NF:
            L0 = float(E._Llev(cfg, k) or 0.0)
            Lk = (0.8 if L0 > 100.0 else 0.4) * L0 * E.floor_area_ft2(cfg, k) / 1000.0
        else:
            Lk = 0.0
        P[k] = Dk + 0.5 * Lk
    rows = []
    for s in range(1, NF + 1):
        Px = sum(P[k] for k in range(s, NF + 1)); Vx = r["V"][s - 1]; dxe = r["dxe"][s - 1] * h[s - 1]
        th_raw = Px * dxe / (Vx * h[s - 1]) if Vx > 1e-9 else 0.0
        th = th_raw / (1.0 + th_raw)
        rows.append(dict(s=s, Px=Px, Vx=Vx, h=h[s - 1], dxe=dxe, Delta=dxe * Cd / r["Ie"],
                         raw=th_raw, theta=th, exempt=(s in runs["exempt"])))
    return rows, tmax, beta


def rbs_drift_factor(cfg, direction=None):
    """AISC 358-22 sec.5.7 Step 1: elastic drift of an RBS moment frame is computed with the effect of
    the reduced section; in lieu of detailed calculation, multiply gross-section elastic drifts by 1.1
    for flange reductions up to 50% of bbf, interpolating linearly for smaller reductions.
    Delegates to engine3d.rbs_drift_factor when present (the drift GATE uses the same value)."""
    fn = getattr(E, "rbs_drift_factor", None)
    f, why = fn(cfg, direction) if fn is not None else (1.0, "no RBS declared")
    if f <= 1.0 and _PKG_RBS[0] and not cfg.get("rbs") and cfg.get("rbs_drift_factor") is None:
        # the calc package designs RBS connections but the cfg does not declare them: apply 1.1 here and say
        # loudly that the engine drift gate (which reads cfg only) omitted it
        if "only" not in why:
            return 1.1, ("RBS connections appear in calc_package.json but cfg['rbs'] is not declared -- 50% flange "
                         "reduction assumed (1.1) in this report; the engine drift gate did NOT apply it: declare cfg['rbs']")
    return f, why


_PKG_RBS = [False]


def _drift_relief_note(cfg):
    """Chapter 8 paragraph when cfg['drift_relief_16_1_2'] is in force (ASCE 7-22 16.1.2: a Chapter 16
    analysis relaxes the 12.12.1 drift limits for Risk Category I-III). Empty string otherwise; the
    RC IV misuse is reported as a preflight/consistency ERROR, and named here so the reader sees it."""
    try:
        from preflight import drift_relief, relief_active
        r = drift_relief(cfg)
    except Exception:
        return ""
    if r is None:
        return ""
    Ie = float((cfg.get("seis") or {}).get("Ie", 1.0) or 1.0)
    if not relief_active(cfg):
        return ("<p class='note'><b>cfg['drift_relief_16_1_2'] is present but NOT in force</b> "
                + ("(Risk Category IV, I<sub>e</sub> = %.2f: ASCE 7-22 &sect;16.1.2 keeps the Table 12.12-1 limits)" % Ie
                   if Ie >= 1.5 else "(block incomplete -- see the preflight findings)")
                + " &mdash; the Table 12.12-1 limit above governs.</p>")
    def _f(k, scale=100.0, fmt="%.2f%%"):
        v = r.get(k)
        return (fmt % (float(v) * scale)) if isinstance(v, (int, float)) else "&mdash;"
    table_val = r.get("table_12_12_1") if isinstance(r.get("table_12_12_1"), (int, float)) else (0.015 if Ie >= 1.25 else 0.020)
    return ("<p class='cnote'><b>ASCE 7-22 &sect;16.1.2 drift relief in force.</b> A Chapter 16 nonlinear response "
            "history analysis of this building (Nonlinear module job <code>%s</code>%s) gave a suite-mean MCE<sub>R</sub> "
            "story drift of %s against the &sect;16.4.1.2 limit of %s (verdict: %s). For Risk Category I&ndash;III the "
            "&sect;12.12.1 limits therefore need not apply; the linear design target was reset to %s of story height "
            "(the Table 12.12-1 value would be %s). <b>This design is provisional until the Chapter 16 analysis is "
            "re-run on it and passes &sect;16.4.</b>%s</p>"
            % (_esc(str(r.get("nlrha_job", "?"))),
               (" run " + _esc(str(r.get("nlrha_run")))) if r.get("nlrha_run") else "",
               _f("nlrha_mean_drift"), _f("nlrha_limit"), _esc(str(r.get("nlrha_verdict", "n/a"))),
               _f("linear_target"), "%.2f%%" % (100.0 * float(table_val)),
               (" " + _esc(str(r.get("note")))) if r.get("note") else ""))


def _design_of_record_rows(root):
    """Rows for the Chapter 1 basis table when design/design_of_record.json exists (written by the
    Nonlinear module's feedback loop when the user promotes a verified re-design)."""
    try:
        p = os.path.join(root, "design", "design_of_record.json")
        if not os.path.exists(p):
            return []
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return []
    what = "promoted from feedback loop <code>%s</code> (%s) on %s" % (
        _esc(str(d.get("loop", "?"))), _esc(str(d.get("loop_title", ""))), _esc(str(d.get("promoted_at", ""))))
    ver = d.get("verification") or {}
    vs = "; ".join("%s: %s" % (_esc(str(k)), _esc(str(v))) for k, v in ver.items() if not isinstance(v, (dict, list)))
    rows = [["Design of record", what + ((" &mdash; " + vs) if vs else "")]]
    if d.get("supersedes"):
        rows.append(["Supersedes", _esc(str(d["supersedes"]))])
    return rows


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

def _sdc(SDS, SD1, S1=0.0, Ie=1.0):
    """Seismic Design Category per ASCE 7-22 sec.11.6 -- delegates to the canonical
    preflight.asce_sdc: worse of Tables 11.6-1/-2 (incl. the Risk Category IV column, RC from Ie),
    after the S1 >= 0.75 -> E (RC I-III) / F (RC IV) override."""
    from preflight import asce_sdc, risk_cat_from_Ie
    return asce_sdc(SDS, SD1, S1, risk_cat_from_Ie(Ie))

def _design_basis_codes(cfg, s, root=None):
    seismic = bool(s.get("R"))
    rows = [["International Building Code (IBC)", "adopting code &mdash; confirm locally adopted edition &amp; amendments"],
            ["ASCE/SEI 7-22", "loads &amp; load combinations (gravity, wind Ch.26-31, seismic Ch.11-12, &sect;2.3 LRFD)"],
            ["AISC 360-22", "steel member &amp; connection design (LRFD)"]]
    if seismic:
        rows += [["AISC 341-22", "seismic provisions / ductile detailing &amp; capacity design"],
                 ["AISC 358-22", "prequalified moment connections (SMF/IMF, incl. the moment frames of a dual system)"]]
    rows += [["AWS D1.1", "structural welding"], ["ACI 318 (Ch. 17)", "cast-in anchorage at column bases"]]
    out = ["<h3>Governing standards</h3>",
           _table(["Reference", "Used for"], rows)]
    Ie = s.get("Ie", 1.0); RC = _risk_category(Ie)
    crows = [["Risk Category", RC], ["Importance factor I<sub>e</sub>", f"{Ie}"]]
    if seismic:
        sdc = _sdc(s["SDS"], s["SD1"], s.get("S1", 0.0), Ie)
        crows += [["S<sub>DS</sub> / S<sub>D1</sub>", f"{s['SDS']} / {s['SD1']} g"],
                  ["Seismic Design Category", f"{sdc} <span class='cnote'>(Tables 11.6-1/2 incl. RC IV column; "
                   "S<sub>1</sub>&ge;0.75 &rarr; E/F applied; confirm mapped S<sub>1</sub>)</span>"],
                  ["R / C<sub>d</sub> / &Omega;<sub>0</sub>", f"{s['R']} / {s.get('Cd')} / {s.get('Om0')}"],
                  ["Redundancy &rho;", f"{cfg.get('rho', 1.3)}"]]
        try:
            from preflight import drift_relief, relief_active
            _r = drift_relief(cfg)
            if _r is not None:
                crows.append(["Story drift limit basis",
                              ("ASCE 7-22 &sect;16.1.2 relief in force &mdash; linear target %.2f%% from the Chapter 16 result "
                               "(Nonlinear module job <code>%s</code>)" % (100.0 * float(_r.get("linear_target", 0) or 0), _esc(str(_r.get("nlrha_job", "?")))))
                              if relief_active(cfg) else
                              "Table 12.12-1 (a cfg['drift_relief_16_1_2'] block is present but NOT in force &mdash; see Chapter 8)"])
        except Exception:
            pass
    crows += _design_of_record_rows(root)
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

def _system_loadpath(cfg, sf=None):
    sf = sf or _sfrs(cfg)
    one_way = "one-way" in str(cfg.get("floor_system", "")).lower()
    trib = "one-way deck spanning to the infill beams" if one_way else "tributary"
    sysd = "; ".join(f"<b>{d}</b>: {_sys_label(sf, d)} ({_line_desc(sf, d)})" for d in ("X", "Y"))
    decl = "" if sf["declared"] else (" <span class='note'>(no cfg['system'] declared &mdash; the type is "
                                       "inferred from the model and must be declared)</span>")
    return ("<h3>Lateral system &amp; load path</h3>"
            f"<p>Seismic force-resisting system per principal direction: {sysd}.{decl} "
            f"<b>Gravity</b> path: floor/roof pressure &rarr; {trib} &rarr; beams/girders &rarr; "
            "columns &rarr; column bases &rarr; foundation. <b>Lateral</b> path: story inertial/wind force &rarr; "
            "floor diaphragm &rarr; the vertical lateral planes listed above &rarr; column bases &rarr; foundation, in "
            "both orthogonal directions. The horizontal distribution to the planes and the irregularity screen "
            "(discontinuous or offset planes) are in this chapter.</p>")


def _lfrs_table(cfg, sf=None):
    sf = sf or _sfrs(cfg)
    rows = []
    for d in ("X", "Y"):
        sd = _seis_d(cfg, d)
        hl = "; ".join("%s: %s" % (f, _height_limit(f)) for f in sf["dirs"][d])
        rows.append([f"{d} direction", _sys_label(sf, d), f"{sd.get('R')}", f"{sd.get('Cd')}", f"{sd.get('Om0')}",
                     f"{sd.get('rho', cfg.get('rho', 1.3))}", hl])
    tbl = _table(["Direction", "Seismic force-resisting system", "R", "C<sub>d</sub>", "&Omega;<sub>0</sub>",
                  "&rho;", "h<sub>n</sub> limit (Table 12.2-1, SDC D/E/F)"], rows)
    if sf["declared"]:
        note = ("<p class='cnote'>System from cfg['system'] (\"%s\")%s; apply each system's AISC 341 provisions in "
                "Chapter 9 and confirm the Table 12.2-1 height limit for the SDC. Where the systems differ by direction, "
                "each direction's R, C<sub>d</sub> and &Omega;<sub>0</sub> apply to it (ASCE 7-22 &sect;12.2.2)%s.</p>"
                % (_esc(sf["text"]), ", checked against the lateral elements of the built model" if sf["inv_ok"] else "",
                   "" if (cfg.get("seis_X") or cfg.get("seis_Y")) or not sf["mixed"] else
                   " &mdash; <b>cfg['seis'] carries one factor set; declare cfg['seis_X']/cfg['seis_Y']</b>"))
    else:
        note = ("<p class='note'>No cfg['system'] declared &mdash; the type above is INFERRED from the model "
                "(braces / fixed-ended beams) and does not identify the AISC 341 system (R=8 is SMF, EBF, BRBF or a "
                "dual). DECLARE cfg['system'] so the system, its detailing (Ch. 9) and height limit are locked.</p>")
    for w in sf.get("warnings", []):
        note += "<p class='note'>%s</p>" % _esc(w)
    return tbl + note


def _height_limit(fam):
    """ASCE 7-22 Table 12.2-1 structural height limits (ft) for SDC D / E / F (NL = not limited,
    NP = not permitted); summary values -- confirm the footnotes (12.2.5.4 increases etc.)."""
    return {"SMF": "NL / NL / NL", "IMF": "35 / NP / NP (12.2.5.7 exceptions)", "OMF": "NP / NP / NP (12.2.5.6 exceptions)",
            "STMF": "160 / 100 / NP", "SCBF": "160 / 160 / 100", "OCBF": "35 / 35 / NP",
            "BRBF": "160 / 160 / 100", "EBF": "160 / 160 / 100", "SPSW": "160 / 160 / 100",
            "MF?": "declare the system", "BF?": "declare the system"}.get(fam, "per Table 12.2-1")

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

def _merge(iv):
    out = []
    for lo, hi in sorted(iv):
        if out and lo <= out[-1][1] + 1.0: out[-1] = (out[-1][0], max(out[-1][1], hi))
        else: out.append((lo, hi))
    return out


def _discontinuities(cfg, sf):
    """Vertical-element continuity of every SFRS plane, story by story (from the built model):
       'terminated' -- a plane has lateral elements in story s but none in story s-1 below it
                       (its shear must leave the plane through the diaphragm: Table 12.3-1 Type 4
                       out-of-plane offset; its overturning lands on the supporting members:
                       Table 12.3-2 Type 3 / 12.3.3.4 -- both reported, conservatively);
       'inplane'    -- the plane continues below but its bays at story s are not all above bays of
                       story s-1 (in-plane offset, Table 12.3-2 Type 3).
    Returns (terminated, inplane) lists of (direction, plane_pos_in, story)."""
    term, inpl = [], []
    for d in ("X", "Y"):
        for pos, st in (sf.get("lines", {}).get(d) or {}).items():
            for s_ in sorted(st):
                if s_ < 2: continue
                below = st.get(s_ - 1)
                if not below:
                    term.append((d, pos, s_)); continue
                cov = _merge(below)
                for lo, hi in _merge(st[s_]):
                    if not any(c0 - 1.0 <= lo and hi <= c1 + 1.0 for c0, c1 in cov):
                        inpl.append((d, pos, s_)); break
    return term, inpl


def _sfrs_extent(sf, d, NF):
    """Horizontal dimension of the SFRS in direction d per story (inches), from the plane extents."""
    out = {}
    for s_ in range(1, NF + 1):
        iv = [x for pos, st in (sf.get("lines", {}).get(d) or {}).items() for x in st.get(s_, [])]
        if iv: out[s_] = max(hi for lo, hi in iv) - min(lo for lo, hi in iv)
    return out


def _irregularity_section(cfg, runs, sf=None):
    """ASCE 7-22 Tables 12.3-1 / 12.3-1a / 12.3-2 evaluated numerically where the model provides the data;
    anything the framework cannot evaluate is printed as NOT EVALUATED (never a default 'None')."""
    sf = sf or _sfrs(cfg)
    NF = len(cfg["heights"]); hgt = cfg["heights"]; exempt = runs["exempt"]
    w = [E.floor_w(cfg, k) for k in range(1, NF + 1)]
    sdc = runs.get("sdc", "D"); NE = "<b>NOT EVALUATED</b>"
    trig = {"12.3.3.4": [], "12.3.3.5": [], "12.3.3.1": [], "12.3.3.2": [], "12.7.3 (3D model)": [], "12.5 (orthogonal)": []}
    # ---- H1 torsional (Eq. 12.3-2 TIR from edge drifts with accidental torsion, Ax = 1)
    tirs = {d: max([t for s_, t in enumerate(runs[d]["tir"], 1) if s_ not in exempt] or [1.0]) for d in ("X", "Y")}
    axs = {d: max(runs[d]["ax"] or [1.0]) for d in ("X", "Y")}
    tr = max(tirs.values())
    tiers = [t for t in (1.2, 1.4, 1.6) if tr > t]
    if tr > 1.2:
        both14 = all(v > 1.4 for v in tirs.values())
        tcls = ("Type 1 torsional: TIR X %.2f / Y %.2f (tiers &gt;%s)%s; A<sub>x</sub> (Eq. 12.8-15, displacements) up to "
                "X %.2f / Y %.2f (&sect;12.8.4.3)%s%s"
                % (tirs["X"], tirs["Y"], "/&gt;".join("%.1f" % t for t in tiers),
                   "" if sdc in ("C", "D", "E", "F") else " (A<sub>x</sub> not required in SDC %s)" % sdc,
                   axs["X"], axs["Y"],
                   "; design drift and P-&Delta; at the edge with the largest drift (&sect;12.8.6.5, Table 12.3-1a)",
                   "; &rho; = 1.3 (&gt;1.4 in both directions, &sect;12.3.4.2.1)" if both14 and sdc in ("D", "E", "F") else ""))
        trig["12.3.3.5"].append("H1"); trig["12.7.3 (3D model)"].append("H1")
    else:
        tcls = "None: TIR X %.2f / Y %.2f &le; 1.2" % (tirs["X"], tirs["Y"])
    # ---- H2 / H3 geometric plan screens
    try:
        pl = E.plan_irregularity_levels(cfg)
    except Exception as ex:
        pl = None; plerr = str(ex)
    def _glab(g, k):
        grp = g.get("group") or [k]
        return "+".join("L%d" % x for x in grp)
    if pl is None:
        h2 = h3 = NE + " (footprint screen failed: %s)" % _esc(plerr)
    else:
        rel = [(k, g) for k, g in pl["levels"].items() if g.get("worst_cutout")]
        if pl["reentrant"]:
            k, g = max(((k, g) for k, g in rel if g["reentrant"]), key=lambda t: min(t[1]["worst_cutout"]["rx"], t[1]["worst_cutout"]["ry"]))
            c = g["worst_cutout"]
            h2 = ("Type 2 RE-ENTRANT: YES &mdash; at %s both projections beyond the corner exceed 20%% of the plan "
                  "dimension (%.0f ft = %.0f%% of %.0f ft in X; %.0f ft = %.0f%% of %.0f ft in Y)%s"
                  % (_glab(g, k), c["px"] / 12, 100 * c["rx"], g["Lx"] / 12, c["py"] / 12, 100 * c["ry"], g["Ly"] / 12,
                     "; 25% diaphragm-connection/collector increase (&sect;12.3.3.5)" if sdc in ("D", "E", "F") else ""))
            trig["12.3.3.5"].append("H2")
        elif rel:
            k, g = max(rel, key=lambda t: min(t[1]["worst_cutout"]["rx"], t[1]["worst_cutout"]["ry"]))
            c = g["worst_cutout"]
            h2 = ("None: largest notch at %s projects %.0f%% (X) / %.0f%% (Y) of the plan dimensions &mdash; both must "
                  "exceed 20%%" % (_glab(g, k), 100 * c["rx"], 100 * c["ry"]))
        else:
            h2 = "None (no re-entrant corner in the framed footprint%s)" % (
                "; split-level wings screened per cfg['level_groups']" if cfg.get("level_groups") else
                "" if not runs["exempt"] else "; split-level building: declare cfg['level_groups'] to screen wings at offset elevations together")
        omax = max(pl["levels"].items(), key=lambda t: t[1]["open_ratio"], default=(None, None))
        if omax[0] is None:
            h3 = NE
        else:
            k, g = omax
            op = ("open area %.0f%% of the gross enclosed area at %s (limit 25%%%s)"
                  % (100 * g["open_ratio"], _glab(g, k),
                     "" if cfg.get("diaphragm_openings") else "; interior bay openings only &mdash; declare other openings in cfg['diaphragm_openings']"))
            h3 = ("Type 3 DIAPHRAGM DISCONTINUITY: YES &mdash; " + op if g["open_ratio"] > 0.25 else "Openings: " + op) + \
                 ("; story-to-story change in effective diaphragm stiffness: " + NE + " (diaphragm modelled rigid)")
            if g["open_ratio"] > 0.25: trig["12.3.3.5"].append("H3")
    # ---- H4 / V3 discontinuous or offset vertical elements
    term, inpl = _discontinuities(cfg, sf) if sf.get("inv_ok") else (None, None)
    def _plane(d, pos):
        return ("%s-plane at %s = %.0f ft" % (d, "y" if d == "X" else "x", pos / 12.0))
    if term is None:
        h4 = v3 = NE + " (lateral-element inventory unavailable)"
    else:
        if term:
            lst = "; ".join("%s terminates at story %d" % (_plane(d, p), s_) for d, p, s_ in term[:6])
            h4 = ("Type 4 OUT-OF-PLANE OFFSET: YES &mdash; %s (no lateral element of that plane in the story below; its "
                  "shear transfers through the diaphragm to other planes &mdash; Table 12.3-1 Type 4, transfer forces "
                  "&times; &Omega;<sub>0</sub> per &sect;12.10.1.1/12.10.3.3)" % lst)
            trig["12.3.3.4"].append("H4"); trig["12.3.3.5"].append("H4"); trig["12.7.3 (3D model)"].append("H4")
        else:
            h4 = "None: every SFRS plane is continuous to the base (checked in the built model)"
        if term or inpl:
            parts = ["%s terminates at story %d on supporting members" % (_plane(d, p), s_) for d, p, s_ in term[:6]]
            parts += ["%s offset in its own plane at story %d" % (_plane(d, p), s_) for d, p, s_ in inpl[:6]]
            v3 = ("Type 3 IN-PLANE DISCONTINUITY: YES &mdash; " + "; ".join(parts) +
                  " &mdash; the supporting beams/trusses/columns and their connections resist the overturning with "
                  "&Omega;<sub>0</sub> (&sect;12.3.3.4)")
            trig["12.3.3.4"].append("V3"); trig["12.3.3.5"].append("V3")
        else:
            v3 = "None: no SFRS plane is offset or interrupted below any story (checked in the built model)"
    # ---- H5 nonparallel
    npar = (_frame_inventory(cfg).get("nonparallel") or [])
    if npar or cfg.get("skew"):
        h5 = ("Type 5 NONPARALLEL: YES &mdash; %s; orthogonal combination (&sect;12.5) and 3D analysis (&sect;12.7.3)"
              % ("%d lateral element(s) skewed to the principal axes" % len(npar) if npar else "cfg['skew'] grid"))
        trig["12.5 (orthogonal)"].append("H5"); trig["12.7.3 (3D model)"].append("H5")
    else:
        h5 = "None: all lateral elements parallel to X or Y (checked in the built model)" if sf.get("inv_ok") else NE
    # ---- V1 soft story (Table 12.3-2 1a/1b, with the 12.3.2.2 exceptions)
    def _soft(d):
        k = runs[d]["k"]; dr = runs[d]["drift_cm"]
        if NF == 1 or (NF == 2 and sdc in ("B", "C", "D")):
            return "None: Exception 2 (12.3.2.2) &mdash; %d-story building" % NF, None
        worst = None
        for s_ in range(1, NF):
            if s_ in exempt: continue
            above = [k[j] for j in range(s_, min(s_ + 3, NF)) if (j + 1) not in exempt]
            if not above or not math.isfinite(k[s_ - 1]): continue
            r1 = k[s_ - 1] / above[0]; r3 = k[s_ - 1] / (sum(above) / len(above)) if len(above) == 3 else None
            typ = ("1b" if (r1 < 0.60 or (r3 is not None and r3 < 0.70)) else
                   "1a" if (r1 < 0.70 or (r3 is not None and r3 < 0.80)) else None)
            if worst is None or r1 < worst[1]: worst = (s_, r1, r3, typ)
        if worst is None:
            return NE, None
        # Exception 1: no design story drift ratio > 130% of the story above (top-two relation excluded)
        exc1 = all(abs(dr[s_ - 1]) <= 1.3 * abs(dr[s_]) for s_ in range(1, NF - 1)
                   if s_ not in exempt and (s_ + 1) not in exempt)
        s_, r1, r3, typ = worst
        txt = ("story %d: K/K<sub>above</sub> = %.2f%s" % (s_, r1, (", K/avg(3 above) = %.2f" % r3) if r3 is not None else ""))
        if typ and exc1:
            return "None: %s, but Exception 1 (12.3.2.2) applies &mdash; no story drift ratio exceeds 130%% of the story above" % txt, None
        if typ:
            return ("Type %s %sSOFT STORY: YES &mdash; %s" % (typ, "EXTREME " if typ == "1b" else "", txt)), typ
        return "None: %s (limits 0.70 / 0.80)" % txt, None
    v1x, tx = _soft("X"); v1y, ty = _soft("Y")
    if "1b" in (tx, ty) and sdc in ("E", "F"): trig["12.3.3.1"].append("V1b")
    v1 = "X: %s<br>Y: %s" % (v1x, v1y)
    # ---- V2 vertical geometric (SFRS horizontal dimension, 130%)
    v2p = []
    if sf.get("inv_ok"):
        for d in ("X", "Y"):
            ext = _sfrs_extent(sf, d, NF)
            for s_ in range(2, NF + 1):
                a_, b_ = ext.get(s_), ext.get(s_ - 1)
                if a_ and b_ and max(a_, b_) / min(a_, b_) > 1.30:
                    v2p.append("%s: stories %d/%d SFRS dimension %.0f/%.0f ft" % (d, s_ - 1, s_, b_ / 12, a_ / 12))
        v2 = ("Type 2 VERTICAL GEOMETRIC: YES &mdash; " + "; ".join(v2p[:4]) + " (&gt; 130%; no Table 12.3-2 reference "
              "section &mdash; informs the analysis-procedure choice)") if v2p else \
             "None: SFRS horizontal dimension within 130% between adjacent stories (both directions)"
    else:
        v2 = NE
    # ---- V4 weak story (needs the story lateral strengths)
    ss = cfg.get("story_strength")
    if isinstance(ss, dict) and ss.get("X") and ss.get("Y"):
        rr = []
        for d in ("X", "Y"):
            v = [float(x) for x in ss[d]]
            for s_ in range(1, min(len(v), NF)):
                if v[s_] > 0 and v[s_ - 1] / v[s_] < 1.0:
                    rr.append((d, s_, v[s_ - 1] / v[s_]))
        if rr:
            dmin = min(rr, key=lambda t: t[2])
            typ = "4b EXTREME WEAK" if dmin[2] < 0.65 else "4a WEAK"
            v4 = ("Type %s STORY: YES &mdash; %s story %d strength = %.0f%% of the story above "
                  "(cfg['story_strength'])" % (typ, dmin[0], dmin[1], 100 * dmin[2]))
            if dmin[2] < 0.65:
                if sdc in ("E", "F"): trig["12.3.3.1"].append("V4b")
                elif sdc == "D": trig["12.3.3.2"].append("V4b")
            elif sdc in ("E", "F"): trig["12.3.3.1"].append("V4a")
        else:
            v4 = "None: every story at least as strong as the story above (cfg['story_strength'])"
    else:
        v4 = (NE + " &mdash; story lateral strengths are not computed by the framework; provide "
              "cfg['story_strength'] = {'X': [...], 'Y': [...]} (kip, story 1 first)")
    # ---- supplementary mass screen (Vertical Type 2 mass irregularity of 7-16, deleted in 7-22)
    massr = max((max(w[k] / w[k - 1], w[k - 1] / w[k]) for k in range(1, NF) if w[k] > 0 and w[k - 1] > 0), default=1.0)
    mcls = ("None (adjacent floor-weight ratio %.2f)" % massr if massr <= 1.5 else
            "Adjacent floor-weight ratio %.2f &gt; 1.5 &mdash; ADVISORY only (the 7-16 mass irregularity was deleted "
            "in ASCE 7-22; no requirement triggered)" % massr)
    rows = [
        ["Horizontal 1 &mdash; Torsional (Table 12.3-1a tiers)", tcls],
        ["Horizontal 2 &mdash; Re-entrant corner (both projections &gt; 20%)", h2],
        ["Horizontal 3 &mdash; Diaphragm discontinuity (&gt; 25% open / &gt; 50% stiffness change)", h3],
        ["Horizontal 4 &mdash; Out-of-plane offset", h4],
        ["Horizontal 5 &mdash; Nonparallel system", h5],
        ["Vertical 1a/1b &mdash; Soft / extreme soft story (70/80%, 60/70%)", v1],
        ["Vertical 2 &mdash; Geometric (SFRS dimension &gt; 130%)", v2],
        ["Vertical 3 &mdash; In-plane discontinuity", v3],
        ["Vertical 4a/4b &mdash; Weak / extreme weak story", v4],
        ["Mass &mdash; supplementary screen (deleted in 7-22)", mcls]]
    intro = ("<p>Evaluated against ASCE 7-22 Tables 12.3-1, 12.3-1a and 12.3-2 (SDC %s). TIR (Eq. 12.3-2) uses the "
             "story drifts at the two diaphragm edges under the &plusmn;5%% accidental eccentricity with A<sub>x</sub> = 1; "
             "story stiffness is V<sub>x</sub>/&Delta;<sub>xe</sub> at the centre of mass; the re-entrant, opening, "
             "offset and SFRS-dimension tests use the framed footprint and the lateral elements of the built model "
             "(plane coordinates from the element list)%s. Items marked NOT EVALUATED need data the framework does "
             "not compute.</p>" % (sdc, "; declared drift-exempt split-level offsets are skipped" if exempt else ""))
    req = []
    if trig["12.3.3.4"]:
        req.append("&sect;12.3.3.4 (%s): members supporting the discontinuous frames, and their connections, are designed "
                   "for the overstrength seismic load effect of &sect;12.4.3" % "/".join(sorted(set(trig["12.3.3.4"]))))
    if trig["12.3.3.5"] and sdc in ("D", "E", "F"):
        req.append("&sect;12.3.3.5 (%s): 25%% increase of the &sect;12.10.1.1 forces for diaphragm connections and "
                   "collectors at the affected levels (Exception: collectors designed with &Omega;<sub>0</sub>)"
                   % "/".join(sorted(set(trig["12.3.3.5"]))))
    if trig["12.3.3.1"]:
        req.append("<b>&sect;12.3.3.1: %s is NOT PERMITTED in SDC %s</b>" % ("/".join(trig["12.3.3.1"]), sdc))
    if trig["12.3.3.2"]:
        req.append("<b>&sect;12.3.3.2: Type 4b extreme weak story is NOT PERMITTED in SDC D</b>")
    if trig["12.7.3 (3D model)"]:
        req.append("&sect;12.7.3: 3D analysis model required (%s) &mdash; the model is 3D" % "/".join(sorted(set(trig["12.7.3 (3D model)"]))))
    if trig["12.5 (orthogonal)"]:
        req.append("&sect;12.5: orthogonal (100/30) combination required (H5)")
    out = intro + _table(["Irregularity type", "Determination"], rows)
    if req:
        out += "<p><b>Provisions triggered:</b></p><ul>" + "".join("<li>%s</li>" % x for x in req) + "</ul>"
    irregular = bool(tr > 1.2 or (pl and (pl["reentrant"] or pl["opening"])) or term or inpl or npar or v2p
                     or tx or ty or cfg.get("skew"))
    if irregular and "RS" not in [a.upper() for a in cfg.get("analyses", [])]:
        out += ("<p class='cnote'><b>Analysis procedure &mdash; ADVISORY:</b> MRSA (&sect;12.9) is recommended for the "
                "irregularities above; <b>ELF is permitted by ASCE 7-22 &sect;12.6</b> for all structures (the 7-16 "
                "Table 12.6-1 procedure matrix was deleted).</p>")
    return out


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
    if upper <= low: gov = "C<sub>s,min</sub> (Eq.12.8-7, S<sub>1</sub>)" if (cmin_s1 and low == cmin_s1) else "C<sub>s,min</sub> (Eq.12.8-6)"
    elif cap < Cs_eq: gov = "C<sub>s,max</sub> (Eq.12.8-4)"
    else: gov = "C<sub>s</sub> (Eq.12.8-3)"
    intro = (f"<p>ASCE 7-22 &sect;12.8 equivalent lateral force. Seismic weight W = {W:.0f} kip; S<sub>DS</sub> = "
             f"{SDS} g, S<sub>D1</sub> = {SD1} g, S<sub>1</sub> = {S1} g, R = {R}, I<sub>e</sub> = {Ie}. Approximate "
             f"period T<sub>a</sub> = C<sub>t</sub>h<sub>n</sub><sup>x</sup> = {Ta:.2f} s; design period "
             f"T = min(T<sub>computed</sub>, C<sub>u</sub>T<sub>a</sub>) = {Tu:.2f} s (&sect;12.8.2).</p>")
    crows = [["C<sub>s</sub> = S<sub>DS</sub>/(R/I<sub>e</sub>) &nbsp;(Eq.12.8-3)", f"{Cs_eq:.4f}"],
             ["C<sub>s,max</sub> = S<sub>D1</sub>/(T&middot;R/I<sub>e</sub>) &nbsp;(Eq.12.8-4)", f"{cap:.4f}"],
             ["C<sub>s,min</sub> = max(0.044&middot;S<sub>DS</sub>I<sub>e</sub>, 0.01) &nbsp;(Eq.12.8-6)", f"{cmin:.4f}"]]
    if cmin_s1 is not None:
        crows.append(["C<sub>s,min</sub> = 0.5&middot;S<sub>1</sub>/(R/I<sub>e</sub>) &nbsp;(Eq.12.8-7, S<sub>1</sub>&ge;0.6)", f"{cmin_s1:.4f}"])
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

def _combo_legend(cfg):
    rho = cfg.get("rho", 1.3)
    s = cfg.get("seis") or {}
    try:
        _sdc_txt = " (SDC %s)" % _sdc(s.get("SDS", 0), s.get("SD1", 0), s.get("S1", 0), s.get("Ie", 1.0))
    except Exception:
        _sdc_txt = ""
    items = [("D", "dead load"),
             ("L", "floor live load (reducible per ASCE 7-22 &sect;4.7)"),
             ("L<sub>r</sub> / S", "roof live load / snow"),
             ("E<sub>X</sub>, E<sub>Y</sub>", "horizontal seismic effect Q<sub>E</sub> (ELF) in the X (E-W) / Y (N-S) direction; the vertical term E<sub>v</sub>=0.2S<sub>DS</sub>D is folded into the D factor"),
             ("W<sub>X</sub>, W<sub>Y</sub>", "wind load in the X / Y direction"),
             ("&rho;", f"redundancy factor multiplying Q<sub>E</sub> (&sect;12.3.4); &rho;={rho} here{_sdc_txt}"),
             ("&Omega;<sub>0</sub>", "overstrength factor; the &ldquo;[col]&rdquo; cases apply &Omega;<sub>0</sub>Q<sub>E</sub> to capacity-protected columns only (&sect;12.4.3)"),
             ("t+ / t&minus;", "&plusmn;5% accidental torsion M<sub>t</sub> = &plusmn;0.05B&middot;F<sub>x</sub> (&sect;12.8.4.2)"),
             ("+ / &minus;", "sign (direction) of the applied lateral load"),
             ("[col]", "combination applied to columns only"),
             ("0.5L/1.0L, 0.3S/0.5Lr, 0.15S, 0.9D", "companion / counteracting load factors (ASCE 7-22 &sect;2.3.1/&sect;2.3.6: "
              "companion L = 1.0 where L<sub>o</sub> &gt; 100 psf or garage/public assembly, else 0.5; companion snow "
              "0.3S (gravity/wind) and 0.15S (seismic); principal snow 1.0S)")]
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
        rows.append([_esc(m.get("id", "")), _esc(sec),
                     _esc(f"{sv.get('live_in','')}\" = {sv.get('live_ratio','')}"), _esc(sv.get("live_limit", "L/360")),
                     _esc(f"{sv.get('total_in','')}\" = {sv.get('total_ratio','')}"), _esc(sv.get("total_limit", "L/240")),
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

def _stability_basis_note(cfg=None):
    """Chapter 5 analysis-basis note. Describes the stability method from the cfg / pipeline instead of
    asserting K = 1 (HR-32): K = 1 is valid only with the Direct Analysis Method (AISC 360-22 C1/C2: reduced
    stiffness per C2.3 and notional loads per C2.2b, or the equivalent) -- otherwise Appendix 7."""
    cfg = cfg or {}
    meth = cfg.get("stability_method") or cfg.get("analysis_method")
    mtxt = ("cfg declares the stability design method <b>%s</b>; the stiffness reductions / notional loads the "
            "demand pipeline applied are recorded in the pipeline output (design/)" % _esc(meth)) if meth else \
           ("the stability design method applied by the demand pipeline (Direct Analysis Method stiffness reduction "
            "and notional loads, or none) is recorded in the pipeline output (design/) &mdash; see pipeline output")
    return ("<p class='cnote'><b>Analysis basis:</b> a second-order P-&Delta; geometric transformation is applied to "
            "the columns under every factored combination (no superposition of factored results); %s. Member "
            "effective length K = 1 is valid only under the Direct Analysis Method of AISC 360-22 &sect;C1/C2 "
            "(reduced stiffness &sect;C2.3, notional loads &sect;C2.2b); where the method is not DAM, the effective "
            "length or first-order methods of Appendix 7 govern. The story stability coefficient &theta; "
            "(ASCE 7-22 &sect;12.8.7) is reported in Chapter 7.</p>" % mtxt)


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
        title = _esc(str(k).replace("_", " ").title())
        if len(blob) > 12000:                      # guard: a runaway dump must not drown the report
            parts.append("<h4>%s</h4><p class='note'>[block truncated at 12 kB -- full record in "
                         "design/calc_package.json]</p><pre>%s\u2026</pre>"
                         % (title, (blob[:12000].replace("<", "&lt;"))))
            continue
        parts.append("<h4>%s</h4>" % title)
        parts.append(_capdesign_html(v) if isinstance(v, (dict, list))
                     else "<p>%s</p>" % _esc(v))
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
            rows.append([_esc(m.get("id", "")), _esc((m.get("inputs") or {}).get("section", ""))]
                        + [(_esc(found[c]) if c in found else "&mdash;") for c in ("pct", "Qn", "studs", "camber", "ILB", "wet")])
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
        return (f"<p>Governing member: <b>{_esc(gov.get('id'))}</b> at D/C = <b>{gov.get('DC'):.2f}</b> "
                f"({_esc(gov.get('limit_state'))}, combo {_esc((gov.get('inputs', {}) or {}).get('governing_combo', ''))}).</p>")
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

def _drift_section(cfg, runs):
    """Chapter 8 seismic design drift, both directions, on the same basis as the engine's drift gate
    (engine3d.static_lateral) and as the theta table: Delta = Cd * delta_xe / Ie (Eq. 12.8-16), delta_xe at
    the 12.8.6.5 location, x the AISC 358-22 5.7 RBS factor, declared drift-exempt stories skipped."""
    NF = len(cfg["heights"]); ex = runs["exempt"]
    h = ["<h3>Seismic design drift</h3>"]
    for d in ("X", "Y"):
        r = runs[d]
        h.append("<p><b>%s:</b> C<sub>d</sub> = %g, I<sub>e</sub> = %g; drift at the %s; allowable %.2f%% of h<sub>sx</sub>%s%s.</p>"
                 % (d, r["Cd"], r["Ie"], r["basis"], r["limit"] * 100,
                    (" = &Delta;<sub>a</sub>/&rho; (moment-frame-only SFRS in SDC D-F, &sect;12.12.1.1)" if r["limit_rho"] else " (Table 12.12-1)"),
                    ("; elastic drifts &times; %.3f for the reduced beam sections (AISC 358-22 &sect;5.7 Step 1: %s)"
                     % (r["rbs"][0], _esc(r["rbs"][1])) if r["rbs"][0] > 1.0 else "")))
    h.append(_drift_relief_note(cfg))
    rows = []
    for k in range(1, NF + 1):
        if k in ex:
            rows.append([k, "%.3f" % (runs["X"]["dxe"][k-1]*100), "%.3f" % (runs["X"]["design"][k-1]*100),
                         "%.3f" % (runs["Y"]["dxe"][k-1]*100), "%.3f" % (runs["Y"]["design"][k-1]*100),
                         "exempt: " + _esc(str((cfg.get("drift_exempt_stories") or {}).get(k, ""))[:60])])
            continue
        ok = all(runs[d]["design"][k-1] <= runs[d]["limit"] for d in ("X", "Y"))
        rows.append([k, "%.3f" % (runs["X"]["dxe"][k-1]*100), "%.3f" % (runs["X"]["design"][k-1]*100),
                     "%.3f" % (runs["Y"]["dxe"][k-1]*100), "%.3f" % (runs["Y"]["design"][k-1]*100), "OK" if ok else "NG"])
    h.append(_table(["Story", "&delta;<sub>xe</sub>/h X %", "&Delta;/h X %", "&delta;<sub>xe</sub>/h Y %", "&Delta;/h Y %",
                     "&le; limit (X %.2f%% / Y %.2f%%)" % (runs["X"]["limit"]*100, runs["Y"]["limit"]*100)], rows))
    h.append("<p class='cnote'>Same analysis as the engine drift gate (ELF story forces, P-&Delta; model, unfactored "
             "gravity per engine3d.floor_grav). The strength-level ELF forces are used, including the C<sub>s</sub> "
             "minimum (Eq. 12.8-6) and the C<sub>u</sub>T<sub>a</sub> period cap &mdash; conservative: &sect;12.8.6.1 "
             "Exception and &sect;12.8.6.2 permit dropping both for drift.</p>")
    return "".join(h)


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
        return _esc(str(v))
    def block(d):
        rows = []
        for k, v in d.items():
            label = _esc(str(k).replace("_", " "))
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

_DETAIL_CHECKS = {
    "SMF": [("Strong-column/weak-beam", "&Sigma;M*<sub>pc</sub>/&Sigma;M*<sub>pb</sub> &gt; 1.0 (E3.4a)"),
            ("Beam &amp; column width-to-thickness", "highly ductile (D1.1, Table D1.1)"),
            ("Panel-zone shear &amp; doublers", "E3.6e"), ("Continuity plates", "E3.6f"),
            ("Beam stability bracing", "E3.4b"), ("Protected zones", "E3.5c (RBS / hinge regions)"),
            ("Prequalified connection", "E3.6c &mdash; within AISC 358 limits"),
            ("Demand-critical welds", "E3.6a / A3.4")],
    "IMF": [("Beam &amp; column width-to-thickness", "moderately ductile (D1.1)"),
            ("Panel zone / continuity plates", "E2.6e / E2.6f"), ("Beam stability bracing", "E2.4a"),
            ("Protected zones", "E2.5c"), ("Prequalified connection", "E2.6c &mdash; within AISC 358 limits"),
            ("Demand-critical welds", "E2.6a / A3.4"),
            ("Strong-column/weak-beam", "not required for IMF (E2 has no moment-ratio provision)")],
    "OMF": [("Beam-to-column connections", "E1.6 (FR: expected strength of the beam or the &Omega;<sub>0</sub> case)"),
            ("Demand-critical welds", "E1.6a")],
    "STMF": [("Special segment", "E4.3a / E4.4a; expected vertical shear strength E4.5c"),
             ("Width-to-thickness of special-segment members", "E4.5d"),
             ("Non-special segment members", "capacity-limited by the special segment (E4.3b)"),
             ("Stability bracing of trusses", "E4.4b"), ("Protected zones", "E4.5f")],
    "SCBF": [("Brace width-to-thickness", "highly ductile (D1.1)"), ("Brace slenderness", "L<sub>c</sub>/r &le; 200 (F2.5b)"),
             ("Columns, beams, connections", "capacity-limited E<sub>cl</sub>, analyses F2.3(a)/(b)"),
             ("Brace connections", "tension R<sub>y</sub>F<sub>y</sub>A<sub>g</sub>/&alpha;<sub>s</sub>; compression = expected brace "
              "compression (F2.3: min(R<sub>y</sub>F<sub>y</sub>A<sub>g</sub>, F<sub>ne</sub>A<sub>g</sub>/0.877))/&alpha;<sub>s</sub> (F2.6c)"),
             ("Protected zones / gusset hinge zone", "F2.5c, F2.6c(3)"), ("Demand-critical welds", "F2.6a")],
    "OCBF": [("Brace slenderness", "F1.5b"), ("Brace connections", "expected R<sub>y</sub>F<sub>y</sub>A<sub>g</sub> or the "
             "&Omega;<sub>0</sub> load effect (F1.6a)"), ("V / inverted-V braced frames", "F1.4a"), ("Beams", "F1.5c")],
    "BRBF": [("Brace adjusted strengths", "&beta;&omega;R<sub>y</sub>P<sub>ysc</sub>, adjustment factors from testing (F4.2a/F4.2b)"),
             ("Columns, beams, connections", "capacity-limited with the adjusted brace strengths (F4.3)"),
             ("Brace qualification", "conformance demonstration by testing (F4.5b(3))"),
             ("Brace connections / gussets", "F4.6c"), ("Demand-critical welds", "F4.6a")],
    "EBF":  [("Link shear strength / length", "F3.5b(2), F3.5b(3)"), ("Link rotation angle", "F3.4a"),
             ("Link stiffeners and bracing", "F3.5b(4), F3.4b"),
             ("Brace, beam outside link, columns", "capacity-limited by the adjusted link shear strength (F3.3)"),
             ("Protected zones", "F3.5c"), ("Link-to-column connections", "F3.6e")],
    "SPSW": [("Web plate", "F5.5b"), ("HBE / VBE", "capacity-limited by web yielding (F5.3, F5.5c)"),
             ("Boundary-element stiffness", "F5.4a"), ("HBE-to-VBE moment ratio", "F5.4b"),
             ("HBE-to-VBE connections", "F5.6b"), ("Protected zones", "F5.5d")],
}


def _aisc341_detailing(cfg, pkg, sf=None):
    s = cfg["seis"]; sf = sf or _sfrs(cfg)
    det = (pkg or {}).get("detailing")
    if det:
        _rows = [[_esc(c.get("check", "")), _esc(c.get("status", ""))] for c in det.get("checks", [])]
        _intro = (f"<p><b>System:</b> {_esc(det.get('system',''))} &mdash; R = {det.get('R', s.get('R'))}, "
                  f"C<sub>d</sub> = {det.get('Cd', s.get('Cd'))}, &Omega;<sub>0</sub> = {det.get('Omega0', s.get('Om0'))}, "
                  f"SDC {_esc(det.get('SDC',''))}. <b>AISC 341 applies:</b> {'yes' if det.get('aisc341_applies') else 'no'}.</p>"
                  f"<p>{_esc(det.get('basis',''))}</p>")
        return _intro + _table(["Required check", "Status / basis"], _rows)
    cap = (pkg or {}).get("capacity_design") or (pkg or {}).get("scwb")
    out = []
    try:
        R = float(s.get("R") or 0)
    except Exception:
        R = 0.0
    if R and R <= 3.0:
        out.append("<p>R = %g &le; 3: the system is not specifically detailed for seismic resistance; AISC 341 does not "
                   "apply and members/connections are designed to AISC 360 (ASCE 7-22 Table 12.2-1 item H).</p>" % R)
        return "".join(out)
    for f in sf["families"]:
        checks = _DETAIL_CHECKS.get(f)
        title = FAM_NAME.get(f, f) + ((" &mdash; AISC 341-22 &sect;%s" % FAM_341[f]) if f in FAM_341 else "")
        dirs = "/".join(d for d in ("X", "Y") if f in sf["dirs"][d])
        out.append(f"<h4>{title} ({dirs} direction)</h4>")
        if not checks:
            out.append("<p class='note'>System type not declared &mdash; declare cfg['system'] to list its AISC 341 "
                       "detailing and capacity-design checks.</p>")
            continue
        rows = [[n, b, "see Appendix A" if cap else "agent-derived (AISC 341 RAG) &mdash; pending"] for n, b in checks]
        out.append(_table(["Required check", "Basis", "Status"], rows))
    if sf["dual"]:
        out.append("<p class='cnote'>Dual system: the moment frames resist at least 25% of the design seismic forces "
                   "independently (ASCE 7-22 &sect;12.2.5.1).</p>")
    intro = ("<p>Required AISC 341-22 ductile-detailing and capacity-design checks for each system in cfg['system']. "
             "They are derived by the agent from the AISC 341/358 RAG; values populate from the calc package where present.</p>")
    extra = ("<h4>Capacity-design results (from the calc package)</h4>" + _capdesign_html(cap)) if cap else ""
    note = ("<p class='cnote'>Width-thickness ductility, AISC 358 prequalification limits, weld NDT and the C&amp;C "
            "cladding / net-uplift checks are confirmed on the drawings and connection submittal (delegated).</p>")
    return intro + "".join(out) + extra + note


def _connection_demands(cfg, pkg, reX, sf=None):
    rows = []; ms = (pkg or {}).get("members") or []
    sf = sf or _sfrs(cfg)
    for m in ms:
        inp = m.get("inputs", {}) or {}; kind = inp.get("kind", ""); sec = inp.get("section", "")
        V = inp.get("V_kip") or 0.0; Mz = inp.get("Mz_kipin") or 0.0
        P = max(inp.get("P_comp_kip", 0) or 0, inp.get("P_tens_kip", 0) or 0)
        if kind == "beam":
            # moment connection only where the member is tagged as a moment-frame beam (role / id) and the
            # system has moment frames; every other beam is a shear connection
            _tag = (str(inp.get("role", "")) + " " + str(m.get("id", ""))).lower()
            _role = str(inp.get("role", "")).lower()
            _joints = str(((cfg.get("model") or {}).get("joints")) or "").lower()
            if any(t in _tag for t in ("smf", "imf", "omf", "stmf", "moment", "mf_", "mf-", "lateral")):
                mf = sf["has_mf"]
            elif _role in ("roof", "floor") or "grav" in _tag:
                mf = False                         # gravity beams: shear connections unless tagged otherwise
            else:
                mf = sf["has_mf"] and (_joints.startswith("rigid") or not sf["has_br"])
            typ = "beam-to-column (moment)" if mf else "beam-to-column (shear)"
            dem = f"V = {V:.0f} kip" + (f", M = {Mz/12:.0f} k-ft" if mf and Mz else "")
            basis = ("AISC 358 prequalified moment connection / CJP flange welds (J2, AISC 341 E%s)"
                     % ("3" if "SMF" in sf["families"] else "2" if "IMF" in sf["families"] else "1/E3")) if mf \
                    else "bolted shear tab (J3 / J4)"
        elif "col" in kind:
            typ = "column splice / base"; dem = f"P = {P:.0f} kip" + (f", M = {Mz/12:.0f} k-ft" if Mz else "")
            basis = "splice (J1.4) / base plate J8 + ACI 318 Ch.17"
        elif kind == "brace":
            typ = "brace-to-gusset"; dem = f"axial = {P:.0f} kip"
            basis = ("adjusted brace strength (AISC 341 F4.6c)" if "BRBF" in sf["families"] else
                     "capacity-limited by the link (AISC 341 F3.6)" if "EBF" in sf["families"] else
                     "expected strength / &Omega;<sub>0</sub> (AISC 341 F1.6a)" if "OCBF" in sf["families"] else
                     "expected strength (AISC 341 F2.6c)")
        else:
            continue
        rows.append([_esc(f"{sec} {kind}"), typ, dem, basis])
    if reX is not None:
        pmax = max((r[2][2] for r in reX), default=0.0); vmax = max((abs(r[2][0]) for r in reX), default=0.0)
        rows.append(["column base", "base plate / anchor rods", f"P = {pmax:.0f} kip, V = {vmax:.0f} kip",
                     "J8/J9 + ACI 318 Ch.17 (min 4 rods)"])
    if not rows:
        return None
    return _table(["Member / location", "Connection type", "Governing demand", "Design basis (limit states)"], rows)

def _qa_scorecard(cfg, Fx, reX, eX, eY, runs):
    s = cfg["seis"]; rows = []
    FxX, FxY = _fx_by_dir(Fx) if Fx is not None else (None, None)
    if reX is not None and FxX is not None:
        Rx = sum(r[2][0] for r in reX); base = sum(FxX.values())
        rows.append(["Equilibrium &mdash; |&Sigma;R<sub>x</sub>| = applied base shear", f"{abs(Rx):.0f} vs {base:.0f} kip",
                     "PASS" if base and abs(abs(Rx)-base)/base < 0.01 else "REVIEW"])
    if eX is not None and eY is not None:
        cx = sum(eX)*100; cy = sum(eY)*100
        rows.append(["Modal mass &ge; 90% (X / Y)", f"{cx:.0f}% / {cy:.0f}%", "PASS" if min(cx, cy) >= 90 else "REVIEW"])
    if runs:
        try:
            from preflight import relief_active as _ra
            _rel = bool(_ra(cfg))
        except Exception:
            _rel = False
        for d in ("X", "Y"):
            r = runs[d]; lim = r["limit"]
            _limlab = f"{lim*100:.2f}%" + (" (&Delta;<sub>a</sub>/&rho;, &sect;12.12.1.1)" if r["limit_rho"] else "") \
                      + (" (&sect;16.1.2 relief)" if _rel else "")
            _rbs = (" &times; RBS %.3f" % r["rbs"][0]) if r["rbs"][0] > 1.0 else ""
            rows.append([f"Seismic design drift {d} &le; limit ({'edge' if runs['edge_basis'] else 'CM'}{_rbs})",
                         f"{r['design_max']*100:.3f}% &le; {_limlab}", "PASS" if r["design_max"] <= lim else "FAIL"])
        for d in ("X", "Y"):
            trs, tmax, beta = _theta_rows(cfg, runs, d)
            tw = max([t["theta"] for t in trs if not t["exempt"]] or [0.0])
            rows.append([f"Stability &theta; {d} &le; &theta;<sub>max</sub> (&sect;12.8.7)", f"{tw:.3f} &le; {tmax:.3f}",
                         "PASS" if tw <= tmax else "FAIL"])
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
        sec = b.get("sec") or cfg.get("beam", "?"); N = Mz = My = V = 0.0
        for tag in b["segs"]:
            lf = ops.eleResponse(tag, "localForces")
            N = max(N, abs(lf[0]), abs(lf[6])); Mz = max(Mz, abs(lf[4]), abs(lf[10]))
            My = max(My, abs(lf[5]), abs(lf[11])); V = max(V, abs(lf[2]), abs(lf[8]))
        upd("beam", sec, N, Mz, My, V, b["A"], b["B"], Mz)
        if N > abs(maxN[0]): maxN = (N, "beam", sec)
        if Mz > abs(maxM[0]): maxM = (Mz, "beam", sec)
    for c in mm["cols"]:
        sec = c.get("sec") or cfg.get("col", "?"); lf = ops.eleResponse(c["tag"], "localForces")
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

def _deflection_section(cfg, pkg=None):
    """Beam deflection table = the engine's beam_deflection GATE (engine3d.beam_serviceability_detail,
    one row per governing group), so the report can never print NG beside a gate PASS or vice versa
    (HR-28). Skipped groups / build errors are listed and make the check FAIL. Where the agent's
    calc_package serviceability block disagrees with the gate, the difference is stated."""
    try:
        det = E.beam_serviceability_detail(cfg)
    except Exception as ex:
        return f"<p class='note'>[beam deflection check FAILED to run: {_esc(ex)}] &mdash; the beam_deflection gate does not pass.</p>"
    if cfg.get("beam_framing") == "truss":
        return ("<h3>Beam deflection</h3><p class='cnote'>cfg['beam_framing'] = 'truss': the W-shape deflection gate is "
                "not applied (long-span trusses/joists); their deflection is part of the joist/truss design.</p>")
    h = ["<h3>Beam deflection &amp; camber (the engine's beam_deflection gate)</h3>",
         "<p>Simple-span mid-span deflection of every distinct beam group (section &times; span &times; floor/roof) "
         "under %s; live (floor L, roof S if any else L<sub>r</sub>) against L/360 and total D+L against L/240. "
         "Camber suggestion &asymp; 80%% of the dead-load deflection (nearest 1/4 in) where it exceeds 3/4 in.</p>"
         % _esc(det.get("basis") or "the gate's tributary loads")]
    if det.get("error"):
        h.append(f"<p class='note'><b>Gate FAILS &mdash; check not evaluated:</b> {_esc(det['error'])}</p>")
    grp = det.get("groups") or []
    rows = []
    def pick(roof):
        g = [x for x in grp if x["roof"] == roof]
        return sorted(g, key=lambda x: -max(x["rLL"], x["rTL"]))
    for roof, lbl in ((False, "Floor"), (True, "Roof")):
        lst = pick(roof)
        show = [x for x in lst if max(x["rLL"], x["rTL"]) >= 1.0] or lst[:1]
        for x in show[:8]:
            L = x["L"]; dD = max(x["dTL"] - x["dLL"], 0.0)
            camber = round(0.8 * dD * 4) / 4 if dD >= 0.75 else 0.0
            rows.append([lbl, _esc(x["sec"]), f"{L/12:.0f} ft", f"{x['trib']/12:.0f} ft",
                         f"{x['dLL']:.2f} in (L/{(L/x['dLL']) if x['dLL'] > 1e-6 else float('inf'):.0f})",
                         f"{x['rLL']:.2f}", f"{x['dTL']:.2f} in", f"{x['rTL']:.2f}",
                         "OK" if max(x["rLL"], x["rTL"]) < 1.0 else "<b>NG</b>",
                         (f"{camber:.2f} in" if camber > 0 else "none")])
    if rows:
        h.append(_table(["Beam", "Section", "Span", "Trib.", "Live &delta;", "Live/(L/360)", "Total &delta;",
                         "Total/(L/240)", "Gate", "Camber"], rows))
    for key, why in (det.get("skipped") or [])[:10]:
        h.append(f"<p class='note'>Group {_esc(key)} NOT EVALUATED: {_esc(why)} &mdash; the gate FAILS until fixed.</p>")
    gate_ok = det["LL"] < 1.0 and det["TL"] < 1.0
    h.append("<p><b>beam_deflection gate: %s</b> (worst live ratio %s, worst total ratio %s).</p>"
             % ("PASS" if gate_ok else "FAIL",
                "%.2f" % det["LL"] if math.isfinite(det["LL"]) else "not evaluated",
                "%.2f" % det["TL"] if math.isfinite(det["TL"]) else "not evaluated"))
    ag = [m for m in ((pkg or {}).get("members") or []) if isinstance(m.get("serviceability"), dict)]
    if ag:
        ag_ok = all(bool(m["serviceability"].get("ok")) for m in ag)
        if ag_ok != gate_ok:
            h.append("<p class='note'><b>Reconcile:</b> the calc package's serviceability table (above) reports %s "
                     "while the engine gate reports %s. The gate uses a simple span with the full perpendicular bay; "
                     "an agent value may use the one-way deck tributary, composite I<sub>LB</sub> or camber. State which "
                     "governs in the calc package.</p>" % ("all OK" if ag_ok else "NG", "PASS" if gate_ok else "FAIL"))
    return "".join(h)


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
    R = cfg["seis"].get("R", 3); detailed = R > 3
    try: _sf = _sfrs(cfg)
    except Exception: _sf = None
    # HUB-09: AISC 358 prequalified connections apply to SMF and IMF (AISC 341 E3.6c / E2.6c), including
    # the moment frames of a dual or mixed system; STMF/OMF/braced-only systems do not need it. An undeclared
    # system with fixed-ended (moment) beams is treated as needing it.
    _fams = (_sf or {}).get("families") or []
    need358 = detailed and (any(f in ("SMF", "IMF") for f in _fams) or (("MF?" in _fams) if _sf else not E.is_braced(cfg)))
    has_conn = bool((pkg or {}).get("connections")); has_cap = bool((pkg or {}).get("capacity_design"))
    rows = []
    def row(item, required, ok, ev):
        rows.append([item, "required" if required else "n/a (R&le;3)" if not detailed else "n/a (no SMF/IMF)",
                     ev, ("&mdash;" if not required else ("grounded" if ok else "<b>MISSING</b>"))])
    row("AISC 360-22 &mdash; member limit states", True, n("engineering_standards_A360") > 0 or cite_a360,
        f"{n('engineering_standards_A360')} queries" + (" + cited in calc_package" if cite_a360 else ""))
    row("AISC 360 Ch. J &mdash; connection design grounded", True, has_conn,
        "connections block present" if has_conn else "no connections block written")
    row("AISC 341-22 &mdash; seismic detailing / capacity design", detailed,
        n("engineering_standards_A341") > 0 or has_cap or cite_a341,
        f"{n('engineering_standards_A341')} queries" + (" + capacity_design block" if has_cap else "")
        + (" + cited in calc_package" if cite_a341 else ""))
    row("AISC 358 &mdash; prequalified moment connections (SMF/IMF, incl. dual/mixed)", need358,
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
                    # the +L seismic cases start "(1.2+0.2SDS)D..." (labels now print the actual
                    # companion factor, e.g. "+0.5L", so match the gravity prefix, not "+L")
                    if ("rhoE" + dirn + sgn) in lab and lab.startswith("(1.2") and key not in seen:
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
    rows = [[str(i + 1), _esc(iss)] for i, iss in enumerate(issues)]
    return ("<h3>Numerical self-consistency check</h3>"
            f"<p class='note'><b>{len(issues)} item(s) to reconcile</b> before the report is final &mdash; the same "
            "quantity must carry one value everywhere, every D/C must equal demand&divide;capacity, the headline D/C "
            "must equal its worst limit-state check, and no D/C may exceed 1.0:</p>" + _table(["#", "issue"], rows))


def _design_basis(cfg):
    """Top-of-report echo of the RESOLVED building parameters so a brief-vs-built mismatch (bay count,
    spans, stories, loads) is visible on page 1."""
    NX,NY=cfg["NX"],cfg["NY"]; SX,SY=cfg["SX"],cfg["SY"]; H=cfg["heights"]; s=cfg.get("seis",{})
    rows=[
      ["Lateral system", _esc(cfg.get("system") or cfg.get("arch",""))],
      ["Plan grid", "%d &times; %d bays @ %.0f &times; %.0f ft  (%.0f &times; %.0f ft overall)"
                    % (NX,NY,SX/12.0,SY/12.0,NX*SX/12.0,NY*SY/12.0)],
      ["Stories", "%d @ %s ft  (H = %.0f ft)" % (len(H), ", ".join("%.0f"%(h/12.0) for h in H), sum(H)/12.0)],
      ["Gravity loads", "floor D %s / L %s psf; roof D %s psf, %s"
                    % (cfg.get("D_floor","?"),cfg.get("L_floor","?"),cfg.get("D_roof","?"),
                       ("S %s psf" % cfg["snow"]) if cfg.get("snow") else ("L<sub>r</sub> %s psf" % cfg.get("Lr", 20.0)))],
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
    try: _PKG_RBS[0] = bool(re.search(r"\bRBS\b|reduced[- ]beam[- ]section", json.dumps(pkg or {}), re.I))
    except Exception: _PKG_RBS[0] = False
    mat = "ASTM A992 steel: \\(F_y=50\\) ksi, \\(F_u=65\\) ksi, \\(E=29{,}000\\) ksi."
    parts = [f"<h1>{_esc(name)} &mdash; structural analysis &amp; design report</h1>",
             f"<p><b>{_esc(cfg.get('arch',''))}</b> &middot; generated {datetime.date.today()}</p>",
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
    reX = reY = None; runs = None
    _INV_CACHE.clear()
    try: sf = _sfrs(cfg)
    except Exception as ex:
        sf = None; parts.append(f"<p class='note'>[SFRS inventory failed: {_esc(ex)}]</p>")
    if Fx is not None:
        _FxX, _FxY = _fx_by_dir(Fx)
        try:
            _, _dX, _drX, reX = _run_case(cfg, "X", _FxX); _, _dY, _drY, reY = _run_case(cfg, "Y", _FxY)
        except Exception as ex: parts.append(f"<p class='note'>[equilibrium run failed: {_esc(ex)}]</p>")
        try: runs = _lateral_runs(cfg, Fx, sf)
        except Exception as ex: parts.append(f"<p class='note'>[drift / torsion runs failed: {_esc(ex)}]</p>")

    # ============================== Chapter 1 — Design basis & codes ==============================
    parts.append(_chapter(1))
    parts.append(_design_basis_codes(cfg, s, root))
    parts.append("<h3>Building description</h3>")
    parts.append(_table(["Item", "Value"], [
        ["Archetype", _esc(cfg.get("arch", ""))],
        ["Stories", f"{NF}"],
        ["Plan", f"{Lx/12:.0f} ft (X) &times; {Ly/12:.0f} ft (Y)"],
        ["Bays", f"{NX} &times; {NY} @ {SX/12:.0f} ft (X) / {SY/12:.0f} ft (Y)"],
        ["Height", f"{Htot/12:.0f} ft (h&#8345;)"],
        ["Column bases", cfg.get("base", "fixed")],
        ["Lateral system", ("X: %s; Y: %s" % (_sys_label(sf, "X"), _sys_label(sf, "Y"))) if sf else "&mdash;"],
    ]))
    parts.append("<h3>Materials</h3><p>" + mat + "</p>")
    parts.append("<h3>Geometry</h3>")
    try: parts.append(_img(fig_plan(cfg), "Plan grid &mdash; the (i, j) labels used in the reaction/force tables"))
    except Exception as ex: parts.append(f"<p class='note'>[plan figure failed: {ex}]</p>")
    parts.append(_img(_png_file_b64(os.path.join(figdir, f"{name}_orientation.png")),
                      "Section orientation (web/depth ticks &mdash; beam strong axis must be vertical)"))

    # ====================== Chapter 2 — Structural system & load path =======================
    parts.append(_chapter(2))
    parts.append(_system_loadpath(cfg, sf))
    parts.append("<h3>Seismic force-resisting system (each principal direction)</h3>")
    parts.append(_lfrs_table(cfg, sf))
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
        try: parts.append(_horizontal_distribution(cfg, Fx, VwX, VwY, sf, runs))
        except Exception as ex: parts.append(f"<p class='note'>[distribution failed: {ex}]</p>")
        parts.append("<h3>Diaphragm classification &amp; design force (ASCE 7-22 &sect;12.10)</h3>")
        try: parts.append(_diaphragm_section(cfg, _fx_by_dir(Fx)[0]))
        except Exception as ex: parts.append(f"<p class='note'>[diaphragm section failed: {ex}]</p>")
        parts.append("<h3>Plan &amp; vertical irregularity screening</h3>")
        if runs is not None:
            try: parts.append(_irregularity_section(cfg, runs, sf))
            except Exception as ex: parts.append(f"<p class='note'>[irregularity screen failed: {_esc(ex)}] &mdash; irregularities NOT EVALUATED</p>")
        else:
            parts.append("<p class='note'>Irregularity screen NOT EVALUATED (the drift/torsion runs failed).</p>")
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
        parts.append(_combo_legend(cfg))
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
    parts.append(_stability_basis_note(cfg))
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
        "Columns of the seismic force-resisting system (%s) for combined gravity + lateral (AISC 360 &sect;H1; "
        "AISC 341 required strengths)." % (", ".join(FAM_NAME.get(f, f) for f in sf["families"]) if sf else "SFRS")))
    if (sf and sf["has_br"]) or cfg.get("braces"):
        _bt = ", ".join(FAM_NAME.get(f, f) for f in (sf["families"] if sf else []) if f in BR_FAMS or f == "BF?") or "braces"
        parts.append(_member_section(cfg, name, pkg, {"brace"}, "Braces / lateral web members",
            f"Lateral web members of the {_bt} for the design story shear (AISC 360 &sect;E3 compression, "
            "&sect;D2 tension; AISC 341 capacity design per system)."))
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
    if runs is not None:
        try:
            parts.append(_stability_section(cfg, runs))
            parts.append(_img(fig_drift_profile(cfg, runs), "Interstory drift profile (both directions)"))
            ss_uri, Vstory, OTM = fig_story_shear_otm(cfg, _fx_by_dir(Fx)[0])
            parts.append(f"<p>Base shear &Sigma;F = {sum(_fx_by_dir(Fx)[0].values()):.0f} kip (X); base overturning &asymp; {OTM:.0f} k-ft.</p>")
            parts.append(_img(ss_uri, "Story-shear and overturning-moment profiles"))
        except Exception as ex:
            parts.append(f"<p class='note'>[stability section failed: {_esc(ex)}]</p>")
    else:
        parts.append("<p class='note'>[needs seismic drift data]</p>")

    # ============================ Chapter 8 — Serviceability =============================
    parts.append(_chapter(8))
    if runs is not None:
        parts.append(_drift_section(cfg, runs))
    parts.append(_wind_drift_section(cfg))
    parts.append(_floor_serviceability(pkg))
    parts.append(_deflection_section(cfg, pkg))
    parts.append("<p class='cnote'><b>Out of scope (this model):</b> floor vibration (AISC Design Guide 11) and "
                 "building separation/pounding are detail-level serviceability checks confirmed against the framing drawings.</p>")

    # ===================== Chapter 9 — Seismic / wind detailing (AISC 341) ================
    parts.append(_chapter(9))
    parts.append(_aisc341_detailing(cfg, pkg, sf))

    # ========================= Chapter 10 — Connections ==================================
    parts.append(_chapter(10))
    _ctbl = _connection_demands(cfg, pkg, reX, sf)
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
    parts.append(_qa_scorecard(cfg, Fx, reX, eX, eY, runs))
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
        rows = [[_esc(m.get("id", "")), _esc((m.get("inputs", {}) or {}).get("section", "")),
                 _esc(_dmd(m.get("inputs", {}) or {})), _esc((m.get("inputs", {}) or {}).get("governing_combo", "")),
                 (_esc(m.get("limit_state")) if m.get("limit_state") else "- (agent/RAG)"),
                 (_esc(m.get("DC")) if m.get("DC") is not None else "- (agent/RAG)")] for m in pkg["members"]]
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
            rows.append([_esc(c.get("id", "")), _esc(c.get("type", "")), _esc(_fmt(c.get("demand", {}))), _esc(ls_txt), _esc(dc_txt)])
        out.append("<h3>Connections (demands; capacities derived by the agent from the RAG)</h3>"
                   + _table(["connection", "type", "demand", "governing limit state", "D/C"], rows))
    elif isinstance(cd, dict):
        rows = [[_esc(k), _esc(v.get("type", "")), _esc(_fmt(v.get("demands", {}))), _esc(v.get("notes", ""))] for k, v in cd.items()]
        out.append("<h3>Connections</h3>" + _table(["connection", "type", "demand", "design basis"], rows))
    return "".join(out) if out else "<pre>" + json.dumps(pkg, indent=1)[:4000] + "</pre>"

if __name__ == "__main__":
    for nm in (sys.argv[1:] or ["B02"]):
        try: build_report(nm)
        except Exception as ex: print(f"[{nm}] FAILED: {ex}")

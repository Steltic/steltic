"""
frame_diagram.py -- governing-combination internal-force diagrams (N / V / M) for the perimeter
frames, drawn from the STATIC model (true two-way tributary distributed loads, sub-divided beams).

For each perimeter frame line (X at j=0, Y at i=0) we find the ASCE 7-22 LRFD combination that
governs the beam moment on that line, then draw that single combination's real, equilibrium-
consistent N / V / M diagrams with the peak value annotated on every member. These are the corrected
replacement for the old lumped-model elevation (which read ~0 beam element forces).
"""
import io, base64, math
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import openseespy.opensees as ops
import engine3d as E
import static_model as SM


def _b64(fig):
    try: fig.tight_layout()        # O(subplots) margin fit; avoid O(n-artists) bbox_inches='tight' (P8)
    except Exception: pass
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=130)
    plt.close(fig); buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode("ascii")


def _accessors(cfg, direction, line="perimeter"):
    NX = cfg["NX"]; NY = cfg["NY"]
    if line == "internal":
        jint = NY//2; iint = NX//2
        onln = (lambda i, j: j == jint) if direction == "X" else (lambda i, j: i == iint)
    else:
        onln = (lambda i, j: j == 0) if direction == "X" else (lambda i, j: i == 0)
    al = (lambda n: ops.nodeCoord(n)[0]) if direction == "X" else (lambda n: ops.nodeCoord(n)[1])
    zo = lambda n: ops.nodeCoord(n)[2]

    def beam_st(b):
        s = []; N = []; V = []; M = []
        a0 = al(b["A"]); sg = 1 if al(b["B"]) >= a0 else -1; Ls = b["L"]/len(b["segs"])
        for idx, tag in enumerate(b["segs"]):
            lf = ops.eleResponse(tag, "localForces")            # [Nx,Vy,Vz,T,My,Mz]*2
            if idx == 0:
                s.append(a0); N.append(lf[0]); V.append(lf[2]); M.append(lf[4]/12.0)
            s.append(a0 + sg*(idx+1)*Ls); N.append(-lf[6]); V.append(-lf[8]); M.append(-lf[10]/12.0)
        return s, N, V, M

    def col_st(c):
        tt = 2 if c["i"] in (0, NX) else 1
        if direction == "X":
            vI, vJ, mI, mJ = (2, 8, 4, 10) if tt == 1 else (1, 7, 5, 11)
        else:
            vI, vJ, mI, mJ = (1, 7, 5, 11) if tt == 1 else (2, 8, 4, 10)
        lf = ops.eleResponse(c["tag"], "localForces"); z1, z2 = zo(c["n1"]), zo(c["n2"])
        return [z1, z2], [lf[0], -lf[6]], [lf[vI], -lf[vJ]], [lf[mI]/12.0, -lf[mJ]/12.0]

    return onln, al, zo, beam_st, col_st


def _key(b):
    return (b["i"], b["j"], b["k"], b.get("dir"))


def _draw_frame(cfg, acc, lbeams_d, lcols_d, combo, nseg, title):
    label, fD, fL, fLr, lat, co = combo
    mm, _, _ = SM.run_combo(cfg, fD, fL, fLr, lat, nseg=nseg)
    bb = {_key(b): b for b in mm["beams"]}; cc = {(c["i"], c["j"], c["k"]): c for c in mm["cols"]}
    onln, al, zo, beam_st, col_st = acc
    fig, axes = plt.subplots(1, 3, figsize=(20, 7)); SXY = min(cfg["SX"], cfg["SY"])
    panels = [(0, "Axial N (kip)", "#1f77b4"), (1, "Shear V (kip)", "#2ca02c"), (2, "Moment M (k-ft)", "#d62728")]
    peakM = 0.0
    for pi, ttl, col in panels:
        ax = axes[pi]; peak = 1e-9; series = []
        for b in lbeams_d:
            s, N, V, M = beam_st(bb[_key(b)]); val = (N, V, M)[pi]
            series.append(("beam", [(s[q], zo(b["A"]), val[q]) for q in range(len(s))]))
            peak = max(peak, max(abs(v) for v in val))
        for c in lcols_d:
            z, N, V, M = col_st(cc[(c["i"], c["j"], c["k"])]); val = (N, V, M)[pi]; a = al(c["n1"])
            series.append(("col", [(a, z[0], val[0]), (a, z[1], val[1])]))
            peak = max(peak, max(abs(v) for v in val))
        if pi == 2: peakM = peak
        sc = (0.40*SXY)/peak
        for kind, pts in series:
            if kind == "beam":
                xs = [pt[0] for pt in pts]; z0 = pts[0][1]
                ax.plot(xs, [z0]*len(xs), color="#ccc", lw=1.0, zorder=1)
                ax.plot(xs, [z0 + pt[2]*sc for pt in pts], color=col, lw=1.2, zorder=3)
                ax.fill_between(xs, [z0]*len(xs), [z0 + pt[2]*sc for pt in pts], color=col, alpha=0.13, zorder=2)
                q = max(range(len(pts)), key=lambda r: abs(pts[r][2])); pv = pts[q][2]
                if abs(pv) > 0.15*peak:
                    ax.annotate(f"{pv:.0f}", (xs[q], z0 + pv*sc), fontsize=6, color=col, ha="center", zorder=5)
            else:
                a = pts[0][0]
                ax.plot([a, a], [pts[0][1], pts[1][1]], color="#ccc", lw=1.0, zorder=1)
                ax.plot([a + pts[0][2]*sc, a + pts[1][2]*sc], [pts[0][1], pts[1][1]], color=col, lw=1.0, zorder=3)
                q = 0 if abs(pts[0][2]) >= abs(pts[1][2]) else 1; pv = pts[q][2]
                if abs(pv) > 0.15*peak:
                    ax.annotate(f"{pv:.0f}", (a + pv*sc, pts[q][1]), fontsize=6, color=col, ha="left", zorder=5)
        ax.set_title(f"{ttl}   peak {peak:.0f}"); ax.set_xlabel("plan (in)"); ax.grid(alpha=0.2)
    axes[0].set_ylabel("Z (in)")
    fig.suptitle(f"{title} \u2014 governing LRFD combination:  {label}", fontsize=12)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    return (_b64(fig), label, peakM)


def render_solved(cfg, mm, direction, label, line="perimeter", title=None):
    """Draw N / V / M for one frame line from the CURRENTLY SOLVED model `mm` (no re-run) -- used for
    the per-load-case diagrams in Appendix B. Returns (uri, peakM) or (None, 0) if the line is empty."""
    acc = _accessors(cfg, direction, line); onln, al, zo, beam_st, col_st = acc
    lbeams = [b for b in mm["beams"] if onln(b["i"], b["j"]) and b["dir"] == direction]
    lcols = [c for c in mm["cols"] if onln(c["i"], c["j"])]
    if not lbeams:
        return None, 0.0
    bb = {_key(b): b for b in mm["beams"]}; cc = {(c["i"], c["j"], c["k"]): c for c in mm["cols"]}
    fig, axes = plt.subplots(1, 3, figsize=(20, 7)); SXY = min(cfg["SX"], cfg["SY"]); peakM = 0.0
    panels = [(0, "Axial N (kip)", "#1f77b4"), (1, "Shear V (kip)", "#2ca02c"), (2, "Moment M (k-ft)", "#d62728")]
    for pi, ttl, col in panels:
        ax = axes[pi]; peak = 1e-9; series = []
        for b in lbeams:
            s, N, V, M = beam_st(bb[_key(b)]); val = (N, V, M)[pi]
            series.append(("beam", [(s[q], zo(b["A"]), val[q]) for q in range(len(s))]))
            peak = max(peak, max(abs(v) for v in val))
        for c in lcols:
            z, N, V, M = col_st(cc[(c["i"], c["j"], c["k"])]); val = (N, V, M)[pi]; a = al(c["n1"])
            series.append(("col", [(a, z[0], val[0]), (a, z[1], val[1])]))
            peak = max(peak, max(abs(v) for v in val))
        if pi == 2: peakM = peak
        sc = (0.40*SXY)/peak
        for kind, pts in series:
            if kind == "beam":
                xs = [pt[0] for pt in pts]; z0 = pts[0][1]
                ax.plot(xs, [z0]*len(xs), color="#ccc", lw=1.0, zorder=1)
                ax.plot(xs, [z0 + pt[2]*sc for pt in pts], color=col, lw=1.2, zorder=3)
                ax.fill_between(xs, [z0]*len(xs), [z0 + pt[2]*sc for pt in pts], color=col, alpha=0.13, zorder=2)
                q = max(range(len(pts)), key=lambda r: abs(pts[r][2])); pv = pts[q][2]
                if abs(pv) > 0.15*peak:
                    ax.annotate(f"{pv:.0f}", (xs[q], z0 + pv*sc), fontsize=6, color=col, ha="center", zorder=5)
            else:
                a = pts[0][0]
                ax.plot([a, a], [pts[0][1], pts[1][1]], color="#ccc", lw=1.0, zorder=1)
                ax.plot([a + pts[0][2]*sc, a + pts[1][2]*sc], [pts[0][1], pts[1][1]], color=col, lw=1.0, zorder=3)
                q = 0 if abs(pts[0][2]) >= abs(pts[1][2]) else 1; pv = pts[q][2]
                if abs(pv) > 0.15*peak:
                    ax.annotate(f"{pv:.0f}", (a + pv*sc, pts[q][1]), fontsize=6, color=col, ha="left", zorder=5)
        ax.set_title(f"{ttl}   peak {peak:.0f}"); ax.set_xlabel("plan (in)"); ax.grid(alpha=0.2)
    axes[0].set_ylabel("Z (in)")
    fig.suptitle(title or f"{line.capitalize()} {direction}-frame \u2014 {label}", fontsize=12)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    return _b64(fig), peakM


def governing_diagrams(cfg, cases, nseg=10, lines=("perimeter",)):
    """Governing-combo N/V/M diagrams. `lines` selects "perimeter" and/or "internal" frame lines.
    Returns {line: {"X": (uri, label, peakM), "Y": ...}}; back-compatible single-line callers get the
    same nested dict keyed by the requested line(s)."""
    if isinstance(lines, str):
        lines = (lines,)
    dirs = ["X", "Y"]
    acc = {(ln, d): _accessors(cfg, d, ln) for ln in lines for d in dirs}
    m0, _, _ = SM.run_combo(cfg, 1.2, 1.6, 0.5, {}, nseg=nseg)
    lbeams = {(ln, d): [b for b in m0["beams"] if acc[(ln, d)][0](b["i"], b["j"]) and b["dir"] == d]
              for ln in lines for d in dirs}
    lcols = {(ln, d): [c for c in m0["cols"] if acc[(ln, d)][0](c["i"], c["j"])] for ln in lines for d in dirs}
    best = {(ln, d): (-1.0, 0) for ln in lines for d in dirs}
    for ci, combo in enumerate(cases):
        if len(combo) > 5 and combo[5]:        # skip Omega0 column-only cases (no frame-diagram content)
            continue
        mm, _, _ = SM.run_combo(cfg, combo[1], combo[2], combo[3], combo[4], nseg=nseg)
        bb = {_key(b): b for b in mm["beams"]}
        for ln in lines:
            for d in dirs:
                bs = acc[(ln, d)][3]
                pk = max((max(abs(v) for v in bs(bb[_key(b)])[3]) for b in lbeams[(ln, d)]), default=0.0)
                if pk > best[(ln, d)][0]:
                    best[(ln, d)] = (pk, ci)
    out = {ln: {} for ln in lines}
    for ln in lines:
        for d in dirs:
            if not lbeams[(ln, d)]:
                continue
            ci = best[(ln, d)][1]
            out[ln][d] = _draw_frame(cfg, acc[(ln, d)], lbeams[(ln, d)], lcols[(ln, d)], cases[ci], nseg,
                                     f"{ln.capitalize()} {d}-frame")
    return out

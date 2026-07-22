"""
viz3d.py -- richer OpenSees visual outputs for the report, all via matplotlib/Pillow (no extra apps):
  mode_shape_gif  : animated fundamental-mode "video" (GIF)
  extruded_shapes : members rendered at their true cross-section depth (3D)
  fiber_stress    : elastic fiber-stress section cuts at the top-bending members
Each returns an embeddable data-URI (or None on failure). They run on the dynamic model (E.build),
so they work for parametric and custom_build geometry alike.
"""
import math, io, base64, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
import numpy as np
import engine3d as E
import sections as SEC
import openseespy.opensees as ops


def _png_uri(fig, dpi=100):
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=dpi)   # no bbox tight (P8); consistent frame size
    plt.close(fig); buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode("ascii")

def _unit(v):
    n = math.sqrt(sum(c*c for c in v)) or 1.0; return [c/n for c in v]
def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def mode_shape_gif(cfg, mode=1, nframes=14, amp=0.10):
    try:
        ms = E.mode_shapes(cfg, max(mode, 3))                 # REUSE cached eigenvectors (no re-solve)
        zl = ms["z"]; zlast = (max(zl.values()) if isinstance(zl, dict) else zl[-1])
        T = ms["T"][mode-1]
        co = ms["coords"]
        ev = {t: ms["ev"][t][mode-1][:3] for t in co}
        mx = max(max(abs(c) for c in ev[t]) for t in co) or 1.0
        A = amp*zlast/mx
        xs=[c[0] for c in co.values()]; ys=[c[1] for c in co.values()]; zs=[c[2] for c in co.values()]
        pad = A*mx
        _xl=(min(xs)-pad, max(xs)+pad); _yl=(min(ys)-pad, max(ys)+pad); _zl=(min(zs), max(zs))
        eles = [(n1, n2) for (tag, kind, sec, n1, n2) in ms["ele"] if n1 in co and n2 in co]
        from PIL import Image
        frames = []
        for f in range(nframes):
            ph = math.sin(2*math.pi*f/nframes)
            fig = plt.figure(figsize=(5, 6)); ax = fig.add_subplot(111, projection='3d')
            for n1, n2 in eles:
                p1 = [co[n1][d]+A*ph*ev[n1][d] for d in range(3)]
                p2 = [co[n2][d]+A*ph*ev[n2][d] for d in range(3)]
                ax.plot([p1[0], p2[0]], [p1[1], p2[1]], [p1[2], p2[2]], color="#2c5aa0", lw=0.8)
            ax.set_xlim(*_xl); ax.set_ylim(*_yl); ax.set_zlim(*_zl)
            ax.set_axis_off()
            ax.set_box_aspect((max(_xl[1]-_xl[0],1.0), max(_yl[1]-_yl[0],1.0), max(_zl[1]-_zl[0],1.0)))
            ax.view_init(elev=12, azim=-60)
            ax.set_title(f"Mode {mode}   T = {T:.2f} s", fontsize=10)
            b = io.BytesIO(); fig.savefig(b, format="png", dpi=70); plt.close(fig)   # consistent GIF frames (P8)
            b.seek(0); frames.append(Image.open(b).convert("P", palette=Image.ADAPTIVE))
        g = io.BytesIO(); frames[0].save(g, format="GIF", save_all=True, append_images=frames[1:],
                                         duration=90, loop=0, optimize=True)
        g.seek(0)
        return "data:image/gif;base64," + base64.b64encode(g.read()).decode("ascii"), T
    except Exception as ex:
        return None, str(ex)

def extruded_shapes(cfg):
    try:
        info = E.build(cfg, "Linear"); co = {t: ops.nodeCoord(t) for t in ops.getNodeTags()}; NX = cfg["NX"]
        fig = plt.figure(figsize=(7, 8)); ax = fig.add_subplot(111, projection='3d')
        cmap = {"col": "#1f4e96", "beam": "#2e8b57", "brace": "#b5651d"}; polys = {"col": [], "beam": [], "brace": []}
        for (tag, kind, sec, n1, n2) in info["ele"]:
            if n1 not in co or n2 not in co or kind not in polys: continue
            p1 = np.array(co[n1]); p2 = np.array(co[n2]); x = _unit((p2-p1).tolist())
            if kind == "beam" or kind == "brace": vecxz = (0, 0, 1)
            else:
                i = (n1 % 100000)//100; vecxz = (0, 1, 0) if (i == 0 or i == NX) else (1, 0, 0)
            y = np.array(_unit(_cross(vecxz, x))); zz = np.array(_unit(_cross(x, y)))
            pr = SEC.props(sec) or {}; d = pr.get("d") or 10.0; bf = pr.get("bf") or pr.get("d") or 8.0
            cor = lambda P: [P+(bf/2)*y+(d/2)*zz, P-(bf/2)*y+(d/2)*zz, P-(bf/2)*y-(d/2)*zz, P+(bf/2)*y-(d/2)*zz]
            c1 = cor(p1); c2 = cor(p2)
            polys[kind] += [c1, c2] + [[c1[a], c1[(a+1) % 4], c2[(a+1) % 4], c2[a]] for a in range(4)]
        for kind, fl in polys.items():
            if fl: ax.add_collection3d(Poly3DCollection(fl, facecolor=cmap[kind], edgecolor="k", linewidths=0.08, alpha=0.93))
        ac = np.array(list(co.values())); rx = ac[:, 0].max()-ac[:, 0].min(); ry = ac[:, 1].max()-ac[:, 1].min(); rz = ac[:, 2].max()
        ax.set_xlim(ac[:, 0].min(), ac[:, 0].max()); ax.set_ylim(ac[:, 1].min(), ac[:, 1].max()); ax.set_zlim(0, rz)
        ax.set_box_aspect((rx+1, ry+1, rz+1)); ax.set_axis_off(); ax.view_init(elev=14, azim=-62)
        ax.set_title("Members rendered at true section depth", fontsize=10)
        return _png_uri(fig, dpi=95)
    except Exception:
        return None


def fiber_stress(cfg, Fy=50.0, topn=3):
    try:
        import static_model as SM
        mm, _, _ = SM.run_combo(cfg, 1.2, 1.6, 0.5, {}, nseg=8)
        rows = []
        for b in mm["beams"]:
            M = max(abs(ops.eleResponse(t, 'localForces')[4]) for t in b["segs"])
            N = max(abs(ops.eleResponse(t, 'localForces')[0]) for t in b["segs"])
            rows.append((M, N, b["sec"]))
        rows.sort(reverse=True)
        top = []; seen = set()
        for M, N, sec in rows:
            if sec in seen: continue
            seen.add(sec); top.append((M, N, sec))
            if len(top) >= topn: break
        if not top: return None
        fig, axes = plt.subplots(1, len(top), figsize=(4*len(top), 5.2))
        if len(top) == 1: axes = [axes]
        for ax, (M, N, sec) in zip(axes, top):
            pr = SEC.props(sec) or {}; d = pr.get("d", 30.); bf = pr.get("bf", 10.); tf = pr.get("tf", 0.7); tw = pr.get("tw", 0.4)
            A = pr.get("A") or (bf*tf*2+(d-2*tf)*tw); I = pr.get("Ix") or 1.0
            ys = np.linspace(-d/2, d/2, 120); sig = [N/A + M*y/I for y in ys]; smax = max(abs(s) for s in sig)
            for y, s in zip(ys, sig):
                w = bf if (abs(y) >= d/2-tf) else tw
                ax.add_patch(plt.Rectangle((-w/2, y-d/240), w, d/120, color=plt.cm.coolwarm(0.5+0.5*s/Fy), lw=0))
            ax.plot([0, 0], [-d/2, d/2], color="k", lw=0.5, ls=":")
            ax.set_xlim(-bf/2*1.25, bf/2*1.25); ax.set_ylim(-d/2*1.12, d/2*1.12); ax.set_aspect("equal"); ax.axis("off")
            ax.set_title(f"{sec}\nM = {M/12:.0f} k-ft\nσ_peak = {smax:.1f} ksi  (0.9Fy = {0.9*Fy:.0f})", fontsize=9)
        fig.suptitle("Elastic fiber stress at the top-bending members  (blue = compression, red = tension)", fontsize=11, y=1.02)
        sm = plt.cm.ScalarMappable(cmap="coolwarm", norm=plt.Normalize(-Fy, Fy)); sm.set_array([])
        fig.colorbar(sm, ax=axes, fraction=0.035, pad=0.02, label="fiber stress σ (ksi)")
        return _png_uri(fig, dpi=110)
    except Exception:
        return None


def _seckey(sec):
    """Sort sections by family then nominal weight (W24X176 after W24X76), HSS last."""
    s = str(sec).upper()
    fam = 0 if s.startswith("W") else 1
    try:
        depth = int(s[1:].split("X")[0]) if s.startswith("W") else 0
        wt = int(s.split("X")[-1]) if s.startswith("W") else 0
    except Exception:
        depth = wt = 0
    return (fam, depth, wt, s)


def members_by_size(cfg):
    """3D line render of the model with a DISTINCT COLOUR for every distinct member SIZE
    (e.g. each W14XNNN column / W-beam / HSS brace), with a legend. Reads the per-element
    section from the model, so optimised buildings (varied columns by level / exterior-interior)
    show every size. True undeformed plan:height proportions (no vertical stretch)."""
    try:
        info = E.build(cfg, "Linear")
        co = {t: ops.nodeCoord(t) for t in ops.getNodeTags()}
        secs = sorted({sec for (tag, kind, sec, n1, n2) in info["ele"]}, key=_seckey)
        cmap = plt.get_cmap("tab20")
        colour = {s: cmap(i % 20) for i, s in enumerate(secs)}
        fig = plt.figure(figsize=(8.5, 7)); ax = fig.add_subplot(111, projection="3d")
        drawn = set()
        for (tag, kind, sec, n1, n2) in info["ele"]:
            if n1 not in co or n2 not in co:
                continue
            a, b = co[n1], co[n2]
            lab = sec if sec not in drawn else None; drawn.add(sec)
            ax.plot([a[0], b[0]], [a[1], b[1]], [a[2], b[2]], color=colour[sec], lw=1.6, label=lab)
        xs=[c[0] for c in co.values()]; ys=[c[1] for c in co.values()]; zs=[c[2] for c in co.values()]
        rx=max(xs)-min(xs); ry=max(ys)-min(ys); rz=max(zs)-min(zs)
        ax.set_box_aspect((max(rx,1.0), max(ry,1.0), max(rz,1.0)))   # true scale
        ax.set_xlabel("X (in)"); ax.set_ylabel("Y (in)"); ax.set_zlabel("Z (in)")
        ax.set_title("Members coloured by section size (%d distinct sizes)" % len(secs), fontsize=10)
        ax.legend(loc="upper left", fontsize=7, ncol=1, framealpha=0.9, title="section")
        ax.view_init(elev=16, azim=-60)
        return _png_uri(fig, dpi=125)
    except Exception:
        return None

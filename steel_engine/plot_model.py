"""
plot_model.py  --  reviewer-grade 3D figures of a library building, for a senior engineer
to check INPUTS and OUTPUTS by eye. Pure matplotlib (no opsvis/vfo needed).

Three views per building:
  *_geometry.png      members color-coded (column/beam/brace) + support markers.
  *_orientation.png   same, plus a tick at each column/beam midpoint showing the SECTION
                      DEPTH (web) direction — i.e. the strong-axis orientation. This is the
                      figure that catches a wrong member orientation: every floor beam's
                      web tick must be VERTICAL; a horizontal beam tick = orientation bug.
  *_deformed.png      undeformed (grey) vs deformed (color) under a chosen lateral case.

Orientation is derived from the SAME convention build() uses (geomTransf vecxz + which
inertia is assigned strong), so the picture is faithful to the analysis model.

Usage:  python plot_model.py B02 [B03 ...]   ->  writes to jobs/<id>/figs/
"""
import os, sys, math
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D  # noqa
import openseespy.opensees as ops
import engine3d as E

COL_C={"col":"#1f77b4","beam":"#2ca02c","brace":"#d62728"}
def _ck(kind):
    """Canonical style bucket for ANY element-kind label a custom_build may emit
    (infill_beam, girder, gravity_beam, lateral_col, ... -> col / beam / brace)."""
    k=str(kind).lower()
    if "brace" in k: return "brace"
    if "col"   in k: return "col"
    return "beam"
def _roofz(info):
    """Roof elevation, tolerant of info["z"] being a list [0,..,roof] (default builder)
    or a dict {level:elev} (some custom_builds)."""
    z=info["z"]
    try: return max(z.values()) if isinstance(z,dict) else z[-1]
    except Exception: return max(ops.nodeCoord(t)[2] for t in ops.getNodeTags())

def _unit(v):
    n=math.sqrt(sum(c*c for c in v)); return [c/n for c in v] if n else v
def _cross(a,b): return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]

def _local_axes(p1,p2,vecxz):
    x=_unit([p2[i]-p1[i] for i in range(3)])
    y=_unit(_cross(vecxz,x)); z=_cross(x,y)
    return x,y,z

def _depth_dir(kind,p1,p2,i_is_perim):
    """Section-depth (web) direction = the in-plane axis of strong-axis bending, per build().
    columns: vecxz=(1,0,0) interior / (0,1,0) perimeter, strong about local z -> depth = local y.
    beams:   vecxz=(0,0,1), strong about local y (Ix in Iy-arg, fixed) -> depth = local z."""
    if _ck(kind)=="col":
        vecxz=(0.,1.,0.) if i_is_perim else (1.,0.,0.)
        x,y,z=_local_axes(p1,p2,vecxz); return y
    else:
        x,y,z=_local_axes(p1,p2,(0.,0.,1.)); return z

def _model(name):
    cfg=E.CFG[name]; info=E.build(cfg,"Linear")
    NX=cfg["NX"]
    nodes={}; eles=[]
    for (et,kind,sec,n1,n2) in info["ele"]:
        for t in (n1,n2):
            if t not in nodes: nodes[t]=ops.nodeCoord(t)   # coords from OpenSees -> robust to ANY node tags (e.g. brace apex)
        i1=(n1//100)%1000
        perim=(i1==0 or i1==NX)
        eles.append((kind,sec,nodes[n1],nodes[n2],perim))
    base=[ (i,j) for (i,j) in info["present"][0] ]
    bxyz=[ops.nodeCoord(E.ntag(i,j,0)) for (i,j) in base]
    return cfg,info,nodes,eles,bxyz

def _setup(ax,title):
    ax.set_title(title,fontsize=10); ax.set_xlabel("X (in)"); ax.set_ylabel("Y (in)")
    ax.set_zlabel("Z (in)");
    # TRUE proportions from the data extents — otherwise a squat wide building (e.g. T06,
    # 210x120 ft plan x 55 ft tall) gets vertically stretched and reads like a tall tower.
    try:
        xr=ax.get_xlim3d(); yr=ax.get_ylim3d(); zr=ax.get_zlim3d()
        ax.set_box_aspect((max(xr[1]-xr[0],1.0), max(yr[1]-yr[0],1.0), max(zr[1]-zr[0],1.0)))
    except Exception:
        try: ax.set_box_aspect((1,1,1))
        except Exception: pass

def _draw_members(ax,eles,lw=1.4,alpha=1.0):
    seen=set()
    for kind,sec,a,b,perim in eles:
        lab=kind if kind not in seen else None; seen.add(kind)
        ax.plot([a[0],b[0]],[a[1],b[1]],[a[2],b[2]],color=COL_C[_ck(kind)],lw=lw,alpha=alpha,label=lab)

def _draw_ghost_framing(ax, cfg, info, legend=True):
    """Draw floor-framing grid edges that are NOT modelled members -- the hand-designed gravity girders /
    infill filler beams (carried as loads, not lateral-model elements) -- so the figure shows the COMPLETE
    floor framing. COORDINATE-based: derives the floor grid and the already-modelled beams from the live node
    coordinates, so it works for ANY model regardless of how a custom_build numbers nodes or labels elements."""
    from collections import defaultdict
    coord = {}; modelled = set()
    for (et, kind, sec, n1, n2) in info["ele"]:
        for t in (n1, n2):
            if t not in coord:
                try: coord[t] = ops.nodeCoord(t)
                except Exception: coord[t] = None
        a, b = coord.get(n1), coord.get(n2)
        if a and b and abs(a[2]-b[2]) < 1e-6:          # horizontal member already in the model = a modelled floor beam
            modelled.add(frozenset((n1, n2)))
    zmin = min((c[2] for c in coord.values() if c), default=0.0)
    byz = defaultdict(list)
    for t, c in coord.items():
        if c and c[2] > zmin + 1e-6:                   # nodes on the floors above the base
            byz[round(c[2], 3)].append((c[0], c[1], t))
    drew = False
    for z, pts in byz.items():
        xs = sorted({round(p[0], 3) for p in pts}); ys = sorted({round(p[1], 3) for p in pts})
        xi = {x: i for i, x in enumerate(xs)}; yi = {y: i for i, y in enumerate(ys)}
        at = {(xi[round(x, 3)], yi[round(y, 3)]): (x, y, t) for (x, y, t) in pts}
        for (gi, gj), (x, y, t) in at.items():         # connect each column point to its +x / +y grid neighbour
            for (di, dj) in ((1, 0), (0, 1)):
                nb = at.get((gi+di, gj+dj))
                if not nb: continue
                x2, y2, t2 = nb
                if frozenset((t, t2)) in modelled: continue
                ax.plot([x, x2], [y, y2], [z, z], color=COL_C["beam"], lw=0.7, ls="--", alpha=0.4); drew = True
    if drew and legend:
        ax.plot([], [], [], color=COL_C["beam"], lw=0.8, ls="--", alpha=0.7, label="gravity floor framing (hand-designed)")
    return drew

def geometry(name,outdir):
    cfg,info,nodes,eles,bxyz=_model(name)
    fig=plt.figure(figsize=(8,7)); ax=fig.add_subplot(111,projection="3d")
    _draw_members(ax,eles)
    _draw_ghost_framing(ax,cfg,info,legend=True)
    base_fixed = cfg.get("base","fixed")!="pinned"
    ax.scatter([p[0] for p in bxyz],[p[1] for p in bxyz],[p[2] for p in bxyz],
               marker="s" if base_fixed else "^",s=40,color="black",
               label=f"{'fixed' if base_fixed else 'pinned'} base")
    _setup(ax,f"{name} — {cfg['arch']} ({info['NF']}-story) geometry\n"
              f"col {cfg['col']}, beam {cfg['beam']}"+(f", brace {cfg['brace']}" if cfg.get('brace') else ""))
    ax.legend(loc="upper left",fontsize=8)
    p=os.path.join(outdir,f"{name}_geometry.png"); fig.tight_layout(); fig.savefig(p,dpi=130); plt.close(fig); return p

def orientation(name,outdir):
    cfg,info,nodes,eles,bxyz=_model(name)
    fig=plt.figure(figsize=(8,7)); ax=fig.add_subplot(111,projection="3d")
    _draw_members(ax,eles,lw=0.8,alpha=0.45)
    _draw_ghost_framing(ax,cfg,info,legend=False)
    # depth/web ticks
    L=min(cfg["SX"],cfg["SY"])*0.18
    first={"col":True,"beam":True}
    for kind,sec,a,b,perim in eles:
        bk=_ck(kind)
        if bk=="brace": continue
        mid=[(a[i]+b[i])/2 for i in range(3)]
        d=_depth_dir(kind,a,b,perim)
        x0=[mid[i]-d[i]*L/2 for i in range(3)]; x1=[mid[i]+d[i]*L/2 for i in range(3)]
        lab=f"{bk} web/depth dir" if first.get(bk,True) else None; first[bk]=False
        ax.plot([x0[0],x1[0]],[x0[1],x1[1]],[x0[2],x1[2]],
                color="black" if bk=="col" else "#ff7f0e",lw=2.0,label=lab)
    _setup(ax,f"{name} — member ORIENTATION check\n(each beam web/depth tick must be VERTICAL; "
              f"columns show strong-axis direction)")
    ax.legend(loc="upper left",fontsize=8)
    p=os.path.join(outdir,f"{name}_orientation.png"); fig.tight_layout(); fig.savefig(p,dpi=130); plt.close(fig); return p

def deformed(name,outdir,direction="X",scale=None):
    cfg=E.CFG[name]; info=E.build(cfg,"PDelta"); NF=info["NF"]; di=0 if direction=="X" else 1
    # lateral (ELF or wind) applied at masters
    T,*_=E.modal(cfg,min(3*NF,12)) if False else (None,)  # avoid extra cost; use ELF with Ta-based
    Tg=E.modal(cfg,min(3*NF,12))[0][0]
    if cfg.get("governing")=="wind": Fx=E.wind_forces(cfg,direction)
    else: Fx=E.elf(cfg,Tg)[5]
    ops.timeSeries("Linear",1); ops.pattern("Plain",1,1)
    for k in range(1,NF+1):
        f=[0.]*6; f[di]=Fx[k]; ops.load(E.mtag(k),*f)
    ops.constraints("Transformation"); ops.numberer("RCM"); ops.system("UmfPack")
    ops.test("NormDispIncr",1e-7,200); ops.algorithm("Newton"); ops.integrator("LoadControl",1.0)
    ops.analysis("Static"); ops.analyze(1)
    nodes={}; disp={}
    for (et,kind,sec,n1,n2) in info["ele"]:
        for t in (n1,n2):
            if t not in nodes: nodes[t]=ops.nodeCoord(t); disp[t]=ops.nodeDisp(t)
    roof=max(abs(disp[t][di]) for t in disp);
    if scale is None: scale=(0.10*_roofz(info)/roof) if roof>1e-9 else 1.0
    NXc=cfg["NX"]
    fig=plt.figure(figsize=(8,7)); ax=fig.add_subplot(111,projection="3d")
    for (et,kind,sec,n1,n2) in info["ele"]:
        a,b=nodes[n1],nodes[n2]
        ax.plot([a[0],b[0]],[a[1],b[1]],[a[2],b[2]],color="0.75",lw=0.6)
        da=[a[i]+scale*disp[n1][i] for i in range(3)]; db=[b[i]+scale*disp[n2][i] for i in range(3)]
        ax.plot([da[0],db[0]],[da[1],db[1]],[da[2],db[2]],color=COL_C[_ck(kind)],lw=1.3)
    _setup(ax,f"{name} — deformed shape, lateral {direction} (disp ×{scale:.0f})\n"
              f"grey = undeformed, color = deformed")
    p=os.path.join(outdir,f"{name}_deformed_{direction}.png"); fig.tight_layout(); fig.savefig(p,dpi=130); plt.close(fig); return p

def figures(name,outdir=None,deformed_fig=True):
    """Write the model figures. Each figure runs in its OWN try so one failure cannot kill the
    others (the orientation figure -- report Figure 2 -- is REQUIRED and was being lost whenever
    an earlier figure raised). Returns the list of written paths; errors are printed."""
    base=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    outdir=outdir or os.path.join(base,"buildings",name,"figs"); os.makedirs(outdir,exist_ok=True)
    jobs=[("geometry",lambda: geometry(name,outdir)),("orientation",lambda: orientation(name,outdir))]
    if deformed_fig:
        jobs.append(("deformed_X",lambda: deformed(name,outdir,"X")))
    out=[]
    for nm,fn in jobs:
        try: out.append(fn())
        except Exception as ex: print("[plot_model] %s figure FAILED for %s: %s" % (nm, name, ex))
    return out

if __name__=="__main__":
    for nm in (sys.argv[1:] or ["B02"]):
        for f in figures(nm): print("wrote",f)

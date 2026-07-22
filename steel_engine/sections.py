"""
AISC section properties for the design post-processor.

Source of truth, in priority order:
  1. If an official AISC Shapes Database CSV is present (set AISC_CSV env var or
     drop 'aisc_shapes.csv' next to this file), properties are read from it (exact).
  2. Otherwise a small built-in table for the shapes used in this library is used.
     Built-in values: A, Ix, Iy, J come from the engine's SEC dict (already used to
     build the models, so capacity and analysis stay consistent); Zx and the section
     geometry (d, tw, bf, tf) are tabulated here; everything else (Sx, Sy, rx, ry, Aw,
     ho, Cw, rts) is DERIVED with standard AISC formulae. Spot-check against AISC
     v16.0 for production use; provide the CSV for exact values.

props(name) -> dict with keys: A, Ix, Iy, J, Zx, Zy, Sx, Sy, rx, ry, Aw, ho, Cw, rts,
d, tw, bf, tf  (units in, in^2, in^3, in^4, in^6).
brace_r(key) -> radius of gyration (in) for an HSS brace key.
"""
import os, csv, math

# tabulated extras for W-shapes used: Zx (in^3), d, tw, bf, tf (in)
_GEOM = {
 "W14X90":(157,14.0,0.440,14.5,0.710), "W14X120":(212,14.5,0.590,14.7,0.940),
 "W14X132":(234,14.7,0.645,14.7,1.03), "W14X159":(287,15.0,0.745,15.6,1.19),
 "W14X193":(355,15.5,0.890,15.7,1.44), "W14X233":(436,16.0,1.07,15.9,1.72),
 "W14X311":(603,17.1,1.41,16.2,2.26),  "W14X370":(736,17.9,1.66,16.5,2.66),
 "W14X426":(869,18.7,1.88,16.7,3.04),  "W14X500":(1050,19.6,2.19,17.0,3.50),
 "W14X605":(1320,20.9,2.60,17.4,4.16), "W14X730":(1660,22.4,3.07,17.9,4.91),
 "W18X50":(101,18.0,0.355,7.50,0.570), "W21X62":(144,21.0,0.400,8.24,0.615),
 "W24X55":(134,23.6,0.395,7.01,0.505),
 "W24X76":(200,23.9,0.440,8.99,0.680), "W24X84":(224,24.1,0.470,9.02,0.770),
 "W27X94":(278,26.9,0.490,9.99,0.745), "W30X108":(346,29.8,0.545,10.5,0.760),
 "W30X116":(378,30.0,0.565,10.5,0.850),"W33X130":(467,33.1,0.580,11.5,0.855),
 "W36X150":(581,35.9,0.625,12.0,0.940),"W36X194":(767,36.5,0.765,12.1,1.26),
 "W40X199":(869,38.7,0.650,15.8,1.07),
}
# HSS brace radius of gyration (in), square HSS
_HSS_R = {"H4":1.90,"H5":1.96,"H6":2.21,"H6b":2.30,"H7":2.62,"H8":3.02,
          "H8b":2.96,"H10":3.78,"H12":4.58,"H14":5.41}
# Standard AISC square HSS radius of gyration (in), matching engine3d.HSS designation keys.
_HSS_R.update({
 "HSS4X4X1/4":1.51,"HSS4X4X3/8":1.46,"HSS5X5X1/4":1.90,"HSS5X5X3/8":1.85,"HSS5X5X1/2":1.80,
 "HSS6X6X1/4":2.34,"HSS6X6X5/16":2.31,"HSS6X6X3/8":2.28,"HSS6X6X1/2":2.21,
 "HSS7X7X3/8":2.70,"HSS7X7X1/2":2.62,"HSS8X8X1/4":3.15,"HSS8X8X3/8":3.10,"HSS8X8X1/2":3.02,"HSS8X8X5/8":2.94,
 "HSS10X10X3/8":3.90,"HSS10X10X1/2":3.84,"HSS10X10X5/8":3.78,
 "HSS12X12X3/8":4.72,"HSS12X12X1/2":4.66,"HSS12X12X5/8":4.60,
 "HSS14X14X1/2":5.49,"HSS14X14X5/8":5.43,"HSS16X16X1/2":6.31,"HSS16X16X5/8":6.25})

def _csv_path():
    p = os.environ.get("AISC_CSV")
    if p and os.path.exists(p): return p
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aisc_shapes.csv")
    return here if os.path.exists(here) else None

_CSV = None
def _load_csv():
    global _CSV
    if _CSV is not None: return _CSV
    _CSV = {}
    p = _csv_path()
    if not p: return _CSV
    with open(p, newline="") as f:
        for row in csv.DictReader(f):
            lab = (row.get("AISC_Manual_Label") or row.get("Shape") or "").strip().upper()
            if not lab: continue
            def g(*keys):
                for k in keys:
                    v = row.get(k)
                    if v not in (None, "", "-", "–"):
                        try: return float(str(v).replace(",", ""))
                        except ValueError: pass
                return None
            _CSV[lab] = dict(A=g("A"), Ix=g("Ix"), Iy=g("Iy"), J=g("J"), Zx=g("Zx"), Zy=g("Zy"),
                             Sx=g("Sx"), Sy=g("Sy"), rx=g("rx"), ry=g("ry"), d=g("d"), tw=g("tw"),
                             bf=g("bf"), tf=g("tf"), Cw=g("Cw"), rts=g("rts"), ho=g("ho"))
    return _CSV

def props(name, SEC=None):
    name = name.upper()
    csvd = _load_csv().get(name)
    if csvd and csvd.get("A"):
        d = dict(csvd)
        d.setdefault("Aw", (d["d"]*d["tw"]) if d.get("d") and d.get("tw") else None)
        if not d.get("ho") and d.get("d") and d.get("tf"): d["ho"]=d["d"]-d["tf"]
        if not d.get("Cw") and d.get("Iy") and d.get("ho"): d["Cw"]=d["Iy"]*d["ho"]**2/4
        if not d.get("rts") and d.get("Iy") and d.get("Cw") and d.get("Sx"):
            d["rts"]=math.sqrt(math.sqrt(d["Iy"]*d["Cw"])/d["Sx"])
        if not d.get("Sx") and d.get("Ix") and d.get("d"): d["Sx"]=2*d["Ix"]/d["d"]
        if not d.get("Zy") and d.get("Sy"): d["Zy"]=1.55*d["Sy"]
        return d
    # built-in path: need engine SEC for A,Ix,Iy,J
    if SEC is None:
        from engine3d import SEC as _S; SEC=_S
    A,Ix,Iy,J = SEC[name]
    Zx,dd,tw,bf,tf = _GEOM[name]
    Sx = 2*Ix/dd; Sy = 2*Iy/bf
    rx = math.sqrt(Ix/A); ry = math.sqrt(Iy/A)
    Aw = dd*tw; ho = dd-tf; Cw = Iy*ho**2/4.0
    rts = math.sqrt(math.sqrt(Iy*Cw)/Sx)
    Zy = 1.55*Sy
    return dict(A=A,Ix=Ix,Iy=Iy,J=J,Zx=Zx,Zy=Zy,Sx=Sx,Sy=Sy,rx=rx,ry=ry,
                Aw=Aw,ho=ho,Cw=Cw,rts=rts,d=dd,tw=tw,bf=bf,tf=tf)

def brace_r(key):
    r = _HSS_R.get(key)
    if r is not None:
        return r
    d = _load_csv().get(str(key).upper())          # B6: any HSS resolves rx from the Shapes DB CSV
    if d and d.get("rx"):
        return d["rx"]
    return 2.5

def hss_b_over_t(name, spec="A1085"):
    """Flat-width / design-wall ratio b/t for a SQUARE/RECT HSS, used for AISC 341 Table D1.1 ductility.
    B6: design wall t_des = t_nom for ASTM A1085 (tighter tolerance) but 0.93*t_nom for A500. Parses the
    nominal wall from the label (e.g. HSS12X12X3/4 -> B=12, t_nom=0.75). Returns (b_over_t, t_des)."""
    import re as _re
    m = _re.match(r"HSS(\d+(?:\.\d+)?)X(\d+(?:\.\d+)?)X(\d+)(?:/(\d+))?", str(name).upper())
    if not m:
        return None, None
    B = float(m.group(1))
    tnom = (float(m.group(3))/float(m.group(4))) if m.group(4) else float(m.group(3))
    tdes = tnom if str(spec).upper() == "A1085" else 0.93*tnom
    b = B - 3.0*tdes
    return b/tdes, tdes

# AISC 360-22 — Specification table of contents (query aid)

Use this to phrase **clause-anchored** RAG queries against `engineering_standards_A360`
(e.g. "Section E3 flexural buckling Pn = Fn Ag", "Section F2 lateral-torsional buckling Lp Lr",
"Section H1-1 combined axial and flexure interaction"). Confirm the retrieved text is the
*design provision*, not an appendix (e.g. Appendix 4 is fire). Member-design chapters are
D, E, F, G, H; connections are J; serviceability is L; stability is C + App. 7/8.

## Chapters
- **A — General Provisions** (A1 scope, A2 referenced standards, A3 material, A4 drawings)
- **B — Design Requirements** — B1 general; B2 loads & load combinations; B3 design basis
  (B3.1 LRFD, φRn ≥ Ru; B3.2 ASD; **B3.10 design for ponding** — moved here from the former
  Appendix 2); **B4 member properties** (B4.1 classification of elements:
  compact / noncompact / slender, width-to-thickness λ, λp, λr; Tables B4.1a/B4.1b)
- **C — Design for Stability** — **C1 general stability requirements** (the Direct Analysis
  Method is the C1–C3 procedure); **C2 calculation of required strengths** (C2.1 general analysis
  requirements; C2.2 initial imperfections — C2.2a direct modeling, **C2.2b notional loads**;
  **C2.3 adjustments to stiffness**, 0.8τb·EI and 0.8·EA); **C3 calculation of available
  strengths** (K = 1 for members designed by the DAM)
- **D — Tension** — D1 slenderness; **D2 tensile strength** (yielding D2-1 = Fy·Ag, φt=0.90;
  rupture D2-2 = Fu·Ae, φt=0.75); D3 effective net area Ae; D4 built-up; D5 pin-connected; D6 eyebars
- **E — Compression** — E1 general; E2 effective length (KL/r); **E3 flexural buckling**
  (Pn=Fn·Ag E3-1; Fn E3-2/E3-3 with limit 4.71√(E/Fy); Fe=π²E/(Lc/r)² E3-4; φc=0.90); E4
  torsional & flexural-torsional; E5 single angles; E6 built-up; E7 slender-element members
- **F — Flexure** — **F1 general** (φb=0.90; Cb F1-1); **F2 doubly-symmetric compact
  I-shapes, major axis** (yielding Mp=Fy·Zx F2-1; LTB F2-2/F2-3/F2-4; Lp F2-5; Lr F2-6);
  F3 compact web/noncompact-slender flange; F4 other I-shapes; F5 slender-web I-shapes;
  **F6 I-shapes minor axis** (Mn=min(Fy·Zy, 1.6·Fy·Sy) F6-1); F7 square/rect HSS & box;
  F8 round HSS; F9 tees & double angles; F10 single angles; F11 rect bars & rounds;
  F12 unsymmetrical; F13 proportioning (holes, etc.)
- **G — Shear** — G1 general; **G2 I-shapes & channels** (Vn=0.6·Fy·Aw·Cv1 G2-1; for most
  rolled I-shapes φv=1.00 and Cv1=1.0 when h/tw ≤ 2.24√(E/Fy)); G3 tension-field action;
  G4 single angles; G5 rect HSS; G6 round HSS; G7 weak-axis shear
- **H — Combined Forces & Torsion** — **H1 doubly/singly-symmetric members, flexure+axial**
  (interaction H1-1a for Pr/Pc ≥ 0.2, H1-1b for < 0.2); H2 unsymmetric; H3 torsion &
  combined torsion; H4 rupture with combined forces
- **I — Composite Members** (I1 general; I2 axial; I3 flexure; I4 shear; I5 combined;
  I6 load transfer; I7 diaphragms/collectors; I8 steel anchors)
- **J — Connections** — J1 general; J2 welds; J3 bolts (J3.1 common bolts; J3.2 high-strength
  bolts, Table J3.1 min pretension; J3.3 holes; J3.4/J3.5 min spacing & edge distance; J3.6 max
  spacing & edge distance; **J3.7 tensile & shear strength** (Table J3.2); J3.8 combined tension &
  shear (bearing-type); J3.9 slip-critical; J3.10 combined tension & shear (slip-critical);
  **J3.11 bearing & tearout at bolt holes**); J4 affected elements (J4.1 tension, J4.2 shear
  yield/rupture, **J4.3 block shear**); J5 fillers; J6 splices; J7 bearing strength; **J8 column
  bases & bearing on concrete**; **J9 anchor rods & embedments**; J10 flanges/webs with
  concentrated forces (and stiffeners)
- **K — HSS & Box-Section Connections** (K1 general; K2 concentrated forces on HSS; K3 HSS-to-HSS
  truss; K4 HSS-to-HSS moment; K5 welds of plates and branches to HSS)
- **L — Serviceability** — L1 general; **L2 deflections** (incl. camber); **L3 drift**;
  L4 vibration; L5 wind-induced motion; L6 thermal expansion & contraction; L7 connection slip
- **M — Fabrication & Erection**;  **N — Quality Control / Quality Assurance**

## Appendices (NOT primary design clauses)
1 Design by Advanced Analysis · 2 Design of filled composite members (high strength) — the
former "Ponding" appendix was removed in 360-22 (ponding is now **B3.10**) · 3 Fatigue ·
**4 Fire conditions** (elevated-temp;
do not use for ambient design) · 5 Existing structures · 6 Member stability bracing ·
**7 Alternative methods for stability (Effective Length Method)** · **8 Approximate
second-order analysis (B1/B2 amplifiers)**

> Seismic system rules (R, Cd, Ω0, ρ; SMF/SCBF/EBF/BRBF detailing) are in **AISC 341** —
> query `engineering_standards_A341`. Worked numeric examples are in `steel_design_examples`.

# COMPOSITE_I3.md - composite floor beam design method (AISC 360-22 Ch. I) - read when floors are composite
The engine designs BARE steel: treat that as the STRENGTH LOWER BOUND and the construction-stage
check, then do these composite checks BY HAND from the RAG (query engineering_standards_A360 Ch. I
and PAIR with a steel_design_examples I3 worked example). Units kip-inch.

## 1. Effective width (I3.1a)
b_eff = min(span/8, spacing/2, edge distance) EACH SIDE of the centerline (sum both sides).

## 2. Stud strength (I8.2a)
Qn = 0.5 Asc sqrt(fc' Ec) <= Rg Rp Asc Fu    [Ec = w_c^1.5 sqrt(fc') ksi; 3/4-in stud Asc=0.442 in2,
Fu=65 ksi; Rg/Rp from deck orientation - typical deck-perpendicular single stud: Rg=1.0, Rp=0.6 ->
Qn ~ 17-21 kip (NW), ~14-17 (LW)].

## 3. Flexural strength (I3.2a, phi_b = 0.90)
- Sum-Qn = min(As Fy, 0.85 fc' Ac, n_studs x Qn)  -> full composite if >= As Fy, else PARTIAL
  (keep >= 25% of As Fy; 50-75% is the economical range).
- PNA in slab (full):  a = As Fy / (0.85 fc' b_eff);  Mn = As Fy (d/2 + t_slab - a/2).
- Partial: C = Sum-Qn; a = C/(0.85 fc' b_eff); steel force resolves per the plastic stress
  distribution (worked example: AISC Design Examples I.1/I.2 - mirror its sequence).
- Web compact for Fy=50 rolled shapes -> plastic distribution OK (I3.2a(a)).

## 4. Construction stage (bare steel - the engine model IS this check)
Wet concrete + deck + workers (typ. 20 psf construction live) on the UNSHORED bare beam: F2 flexure
(Lb = deck-fastened top flange, but pre-hardening use bridging spacing), L/360 wet deflection ->
CAMBER ~ 75-80% of wet-dead deflection, 3/4 in min practical, none for beams < ~24 ft.

## 5. Service deflection (composite)
Use the LOWER-BOUND moment of inertia I_LB (I3.2 commentary / Manual Table 3-20 method) or
I_equiv = Ix + sqrt(SumQn/Cf)(I_tr - Ix); live-load deflection L/360 on I_LB; check total with
long-term creep on the concrete term (multiply modular ratio n by ~2 for sustained).

## 6. Deliverables to add per composite beam type
studs: n x 3/4-in (Qn cited I8.2a) | partial-composite % | camber | wet-stage D/C (bare) |
composite phi_bMn D/C (I3.2a cited) | I_LB deflection ratios. Put them in calc_package.json like
any other member checks; cite I3.1a/I3.2a/I8.2a + the worked-example id you mirrored.

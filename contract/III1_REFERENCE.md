# AISC Design Example III-1 — condensed worked method (a four-story building)

Treat this as a **METHOD TEMPLATE and a SELF-CHECK DISCIPLINE, not numbers to copy.**
Your building's geometry, loads and system differ; reuse the *sequence* of steps and the
grounding habit (every capacity/limit state pulled from the RAG and cited), and finish with
a numeric self-check against a known answer key. III-1 is NOT in the examples RAG, so it does
not duplicate anything you will retrieve.

## Problem (condensed)
4-story steel office building, 210 ft E-W (7 bays @ 30) x 120 ft N-S (4 bays @ 30); story
heights 13.5/13.5/13.5/14.5 ft (h_n = 55 ft). ASTM A992 (Fy = 50 ksi, E = 29,000 ksi).
Lateral system: **E-W = two perimeter MOMENT frames** (W14x90 columns, W24x55 beams, fixed
bases, FR joints); **N-S = two CHEVRON braced frames (CBF)** in a 30-ft bay (pinned).
Seismic (ASCE 7-22): S_DS = 0.129, S_D1 = 0.096, R = 3, I_e = 1.0, T_a = 0.404 s,
W = 8,280 kips. For LRFD, find: ELF base shear & per-frame share; E-W first-story second-order
drift + B_2 by the Direct Analysis Method; an N-S chevron brace by AISC 360 Ch. E; plus
gravity member design.

## Method — the sequence to mirror
1. **ELF base shear** (ASCE 7-22 §12.8): C_s = S_DS/(R/I_e); check the upper-limit cap
   S_D1/[T(R/I_e)] and the minimum 0.044·S_DS·I_e (≥0.01). V = C_s·W. Split to the parallel
   frames. Vertical distribution F_x = C_vx·V with C_vx = w_x h_x^k / Σ w h^k (k = 1 for T ≤ 0.5 s).
2. **Second-order lateral model (Direct Analysis Method, AISC 360 §C2):** build the frame in
   OpenSeesPy and run an elastic analysis that captures **both P-Δ and P-δ** at the LRFD load
   level. Use **reduced stiffness** (§C2.3: 0.80 on all stiffnesses, 0.80·τ_b on flexural).
   Apply **notional loads** N_i = 0.002·α·Y_i (Eq. C2-1) to gravity-only combinations (§C2.2b).
   Add a pin-ended **leaning column** carrying the tributary gravity so its P-Δ destabilizes
   the frame. With DAM, design with **K = 1.0** (§C3).
3. **Drift & stability:** take first-order (Δ_1) and second-order (Δ_2) first-story drifts from
   the run; story amplifier **B_2 = Δ_2/Δ_1** (consistent with App. 8 Eq. A-8-6); confirm every
   interstory drift ratio ≤ the ASCE 7 seismic limit (0.020·h_sx).
4. **Confirm τ_b = 1.0** (Eq. C2-2a) from the first-story column α·P_r/P_ns (P_ns = Fy·A_g for a
   no-slender shape) — if α·P_r/P_ns ≤ 0.5, τ_b = 1.0.
5. **Member checks from the demand envelope**, each grounded and cited: brace compression by
   **§E3** (L_c/r; the 4.71√(E/Fy) inelastic/elastic split; F_e Eq. E3-4; F_n Eq. E3-2/3;
   φ_c P_n = 0.90·F_n·A_g ≥ demand). Gravity beams by **Ch. F**, columns by **Ch. E**, beam-
   columns by **Ch. H** (H1-1a/H1-1b interaction). Pull the matching worked example from
   `steel_design_examples` (see the example index) and mirror its check order.
6. **Self-check vs an answer key** (here, the published AISC Design Examples III-1): C_s and V
   should match; the first-story second-order drift should land within a few percent of the
   published value; B_2 in the expected range. Cross-check a key member force hand-vs-model.

## Answer key (for self-check only — do NOT copy into your building)
C_s = 0.043; **V = 356 kips** total; **178 kips/frame**. E-W first-story Δ_2 ≈ **0.666 in**;
**B_2 ≈ 1.10**; all interstory drifts ≪ 0.020·h. N-S chevron brace **HSS6×6×3/8**
(φ_c P_n = 150 kips > 120-kip demand). The III-1 textbook publishes C_s, V and a reduced-
stiffness first-story drift ≈ 0.650 in; the brace size is a designed result, validated by the
§E3 member check and the hand-vs-model brace force (120 kip hand vs 118.3 kip model, ~1.4%).

## Discipline to carry into every design
- Ground EVERY capacity/limit state in the RAG; cite the exact clause/equation you applied.
- State joints and base fixity explicitly; capture both P-Δ and P-δ for stability.
- Finish with a numeric self-check against a worked example / answer key, and reconcile any
  discrepancy beyond a few percent BEFORE you report.

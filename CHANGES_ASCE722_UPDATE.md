# ASCE 7-22 Compliance Update — Engine Fixes (Recommendations 1–3)

**Date:** 2026-08-15
**Applies to:** steel_by_webpage (HR demo), steel_by_pc (HR GitHub/PyPI), steel_by_pc_CFS (CF demo).
**Basis:** independent verification of the embedded ASCE 7-22 provisions against the full standard text (see ASCE7_VERIFICATION_REPORT.md). Every constant below was re-confirmed against the ASCE 7-22 PDF page text before being written into code.

## Files changed

**HR repos (both, identical):** engine3d.py, design_pipeline.py, report.py, consistency.py, preflight.py.
**CF repo:** cfs_systems.py, cfs_engine.py, cfs_frame.py, cfs_pipeline.py, wall_line.py, consistency.py, report.py, build_cfs_models.py, plus updated copies of the shared engine3d.py / design_pipeline.py / preflight.py (preflight keeps the CFS fork's arch-keyword delta).

---

## HR engine (Recommendation 2 + sweep)

**Behavioral fixes (numbers change):**

- **Seismic weight (§12.7.2):** new optional cfg keys `storage` (bool) / `storage_levels` (list) add 25% of floor live to W on storage floors (item 1); roof snow contribution changed from 0.20·S-always to **0.15·S only where pf > 45 psf** (item 4). Preflight warns when L_floor ≥ 125 psf and no storage flag is set.
- **Companion live factor (§2.3.1/2.3.6 Exc. 1):** one factor Lf = 1.0 if (Lo > 100 psf or `garage` or `public_assembly`) else 0.5, now applied consistently in combos 3a, 4a, the ρE seismic combos, and the Ω0 column combos. New optional cfg keys `garage`, `public_assembly` (default False).
- **MRSA scaling (§12.9.1.4.1):** modal base shear now scaled to **100% of V** (was the 7-16 85% floor); check keys renamed `rs_X_ge_100pct`/`rs_Y_ge_100pct`.
- **Moment-frame drift limit (§12.12.1.1):** for moment-frame-only SFRS in SDC D–F the allowable drift is **Δa/ρ**; new `engine3d.drift_allowable(cfg)` used by the engine, report, consistency, and preflight; the report prints the reduction explicitly.
- **SDC assignment (§11.6):** canonical `preflight.asce_sdc(SDS, SD1, S1, risk_cat)` implements Tables 11.6-1 and 11.6-2 (worse governs) including the Risk Category IV column, with **S1 ≥ 0.75 → SDC E (RC I–III) / F (RC IV)**. report.py `_sdc` delegates to it; optional `cfg["sdc"]` override honored.
- **Wind Kz (Tables 26.10-1/26.11-1):** Kz = **2.41**(z/zg)^(2/α) with B: 7.5/3280, C: 9.8/2460, D: 11.5/1935 (was 2.01 with the 7-16 constants). New optional `wind["Ke"]` (default 1.0) enters qz; printed formula is the 7-22 form (Kd at the pressure equation).
- **Snow load factors (§2.3.1/2.3.6):** principal 1.6S → **1.0S**; companion 0.5S → **0.3S**; seismic companion 0.2S → **0.15S**. Labels updated (Lr factors unchanged).
- **SPSW Ω0 default:** 2.0 per Table 12.2-1 (was falling back to 2.5).

**Label/taxonomy fixes (no numeric change):**

- All seismic equation labels renumbered to 7-22 (Cs → 12.8-3/-4/-6/-7; Ax → 12.8-15; drift → 12.8-16; stability → 12.8-18/-19 with the 7-22 formula string; 25% increase → §12.3.3.5; discontinuous-element support → 12.3.3.4).
- Torsional irregularity: 7-16 "Type 1a/1b" replaced by the single 7-22 **Type 1 with TIR tiers (>1.2 / >1.4 / >1.6)**; the engine screen ratio is now per-story **drift**-based; the (nonexistent in 7-22) "extreme torsion prohibited SDC E/F" claim removed. Vertical soft-story 1a/1b retained (still in 7-22) with the prohibition correctly scoped to **E/F only**. The §12.3.3.5 25% collector increase now also triggers on TIR > 1.2 (was re-entrant only).
- Mass-irregularity screen relabeled "supplementary (deleted in 7-22)", advisory only. "Table 12.6-1 requires MRSA" logic removed/demoted to advisory — §12.6 permits ELF for all structures; the consistency FAIL is gone.
- Combo legend prints the actual ρ and SDC (no longer hard-codes "ρ=1.0, SDC B").

**Not done (documented):** per-system Ta-coefficient defaults for EBF/BRBF (no such table exists in the engine — Ct/x remain explicit inputs); Type 4 out-of-plane-offset auto-detection (25% rule noted in text instead).

---

## CF engine (Recommendation 1 + sweep)

**Behavioral fixes (numbers change):**

- **Cu table (Table 12.8-1):** full five-point table with straight-line interpolation (was a two-step 1.4/1.7 function). Affects V whenever an analytical period is supplied.
- **SBMF height limit (Table 12.2-1 C.12):** 35 ft in **every** SDC B–F (was NL in B/C).
- **Redundancy ρ is now numeric:** canonical `cfs_systems.sdc()` (Tables 11.6-1/2, worse governs, RC IV column, S1 ≥ 0.75 → E/F) drives the default ρ = 1.3 in SDC D–F (was an SDS ≥ 0.5 proxy that missed SD1-governed SDC D). ρ is **multiplied into the seeded strength demands** — wall-line unit shears, V_kip, hold-down T seeds, and the wind-vs-seismic governing comparison — with "includes rho=X.XX" in every basis string. ρ is NOT applied to drift, diaphragm Fpx, or any Ω0/capacity-design quantity (§12.3.4.1 items 2 and 5). `cfg["rho"]` override honored (declare the §12.3.4.2 justification when using 1.0 in SDC D).
- **Wind Kz:** 2.41 coefficient + 7-22 zg/α (C: 2460/9.8, D: 1935/11.5; B was already 3280/7.5 but paired with the old coefficient — Exposure B pressures rise ~16–18%). Optional `wind["Kzt"]`/`wind["Ke"]` (default 1.0) now enter qz on both wall and portal paths.
- **Overstrength combos (§2.3.6):** the counteracting case **(0.9−0.2SDS)D + Ω0·E** added alongside combo 6; the phantom Ω0=2.5 fallback removed (Ω0 comes from cfg or the Table 12.2-1 system table; absence raises).
- **Numeric capacity-design seeding (closes the opus5 failure mode):** for R > 3 wall systems (strap_braced, wsp_shearwall, steelsheet_wall) the package now seeds, per line: `Ve_cap_by_story_kip` = Ω0_eff × ELF story shear (Ω0_eff honors Table 12.2-1 footnote b: −0.5 for flexible diaphragms when Ω0 ≥ 2.5) and `T_cd_seed_kip` = the Ω0-level overturning stack (same mechanics as the ELF stack, **no dead relief**; ρ-free per §12.3.4.1 item 5). A separate `dead_relief_kip_available` = (0.9−0.2SDS)·D_chord_trib is seeded as documented data (chord tributary derived from geometry; `cfg["joist_span_ft"]` optional). The capacity_design block states the rule: final demand = min(ΩE·Vn of the SELECTED assembly, Ω0-level) stacked. Verified against gold v2: Ex2 CN line ELF 71.4 kip → T_cd_seed 142.7 kip; Ex1 WSP Ω0_eff 2.5, T_cd 89.8 kip (matches gold exactly).
- **Numeric consistency gate:** for R > 3 wall systems, consistency now **FAILS** any package whose hold-down/chord design demand is ≤ 1.05× its ELF seed — "capacity-design amplification not applied…". Amplified demands pass; missing data keeps existing behavior; wind-governed demands are carved out.
- **Snow:** combo factors 1.0S / 0.3S / 0.15S (7-22); Is dropped from Eq. 7.3-1 (supplied `snow_is` ≠ 1.0 is ignored with a warning — importance is in the RC-specific pg maps); seismic-weight snow 0.15·S only where pf > 45 psf.
- **Consistency SDC proxy** replaced by the canonical function (SD1-governed SDC D sites now trip height-limit/NP screens correctly).

**Label fixes:** Cs/Ax/θ equation numbers to 7-22; qz formula string to the 26.10-1 form; §12.3.3.5; SBMF height-limit note; Table 12.6-1 MRSA demoted to advisory; mass screen marked supplementary; combo `LrS` key split into separate `Lr` and `S` factors (calc-package consumers: labels updated accordingly).

**Calc-package additions:** top-level `rho`/`rho_basis`; `holddowns[*].T_cd_seed_kip`; `capacity_design.{Om0, Om0_eff, Om0_eff_basis, seed_basis, instruction, lines{...Ve_cap_by_story_kip, T_cd_seed_kip, dead_relief_kip_available, ...}}` (now present for WSP/steel-sheet too); `elf.note` marking the elf block pure-ELF.

**Known pre-existing issues (not part of this change):** some build_cfs_models reference-model rod-line fixtures fail on the pristine engine as well (stale expectations) — recalibrate separately; report.py full render requires the repo's `sections` module (unchanged).

---

## Verification performed

py_compile on every file in both trees; module self-tests (cfs_systems, wall_line, cfs_engine, cfs_frame, cfs_pipeline, consistency) all pass; end-to-end wall-path regression on gold v2 CFS_Ex1/Ex2/Ex5 cfgs — ELF chain byte-identical where it should be (V, Cs, T_ELF seeds unchanged at ρ=1.0), new seeds land exactly on the independently computed Ω0-level stacks, ρ default flips to 1.3 only when the cfg override is removed on the SDC D building, the consistency gate fails ELF-seed reuse and stays silent on amplified demands; HR pure-function tests (asce_sdc against both tables incl. RC IV and the S1 override, drift_allowable ρ-division and gating, Kz constants, SPSW Ω0, combo labels for garage/Lo>100/snow variants).

**Numbers that will move on regenerated solutions:** wind pressures (7-22 Kz — HR down ~3%, CFS Exposure B up ~16–18%), snow-governed combos (1.0S vs 1.6S), seismic W on snow-heavy/storage buildings, V where Cu interpolation bites (analytical periods, SD1 0.15–0.35), CFS seeded demands in SDC D (×1.3 unless ρ=1.0 justified), and all capacity-design seeds (now Ω0-level, not ELF).

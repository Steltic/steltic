# Review fixes — HR Steel (steltic)

October 2026. These changes fix the engine, gate, report, contract and hub defects found in the October 2026 review of the Steltic repos (30 test buildings, 6 nonlinear runs and a static review). Every Critical and High item was re-checked by an independent pass against the 2022 editions and hand calculations.

**Out of scope, deferred to a separate RAG/retrieval change set:**
- the grounding server and the search tool's escalation ladder;
- grokbot conversion and indexing;
- collection names and retrieval-only contract text;
- the nonlinear Collect retrieval and validators. Component values for nonlinear (SNL) runs are verified manually by the user with the RAG and supplied in the hinge-params file.

**Effect on existing designs.** Most of these fixes make demands larger, which is the correct direction: the old values were unconservative. Designs produced before these changes should be re-run, and some will now fail gates they used to pass. That is expected.

Item IDs (HR-xx, CFS-xx, NL-xx, HUB-xx) refer to the review's verified bug register.


## Workstream `hr-seismic`

### Status

Tests: `PYTHONPATH=$PWD:$PWD/steel_engine python -m pytest tests -q` gives **30 passed**: 18 existing tests plus 12 new ones in `tests/test_seismic_direction.py`.
Smoke run: `run_one` on 39 B-archetypes (B02–B44; B22, B23, B34 and B40, the 25–40-storey towers, were not run). None crashes and none fails a gate. B35's eigen solve now converges with ARPACK; before this change it fell back to the slow dense solver.

| ID | status | files | what changed | test | before → after evidence |
|---|---|---|---|---|---|
| HR-01 | FIXED | engine3d, design_pipeline, report (call site), plot_model (call site) | `period_dir()` takes the mode with the largest eX/eY. `elf(cfg,T,direction)` and `elf_dir()` give Tu=min(T_dir, CuTa_dir), Cs, V, k and Fx per direction. Every caller now uses them: run/run_one, the period gates (now per direction), `combos`, the framework screen, `report._seismic` and `plot_model.deformed`. | `test_elf_uses_dominant_mode_period_of_each_direction` | Ex33 V_X 68.7 → **81.1 kip** (+18 %; the register predicted 81). Ex9 with Table Cu=1.4: X 1169 / Y 1130 (register 1167/1129); with the job's Cu=1.054 workaround: X 1142 → 1169. Ex16 V_X 139.9 → 147.4. |
| HR-02 | FIXED | engine3d, design_pipeline | `seis_dir()` merges `cfg['seis']` with per-direction overrides: `cfg['seis']['X'/'Y']`, `cfg['seis_X'/'seis_Y']` (or lower-case), `rho_X`/`rho_Y`, `system_X`/`system_Y`. ELF, Cs limits, Cd drift, the ρ and Ω0 cases, `rs_baseshear` and the MF-only Δa/ρ limit are all per direction. The 30 % companion uses the other direction's forces and factor. If an override changes R, Cd/Ω0/Ct/x are never inherited from the other system. With a single factor set, X and Y are identical. | `test_per_direction_factors_drive_elf_drift_and_combinations`, `test_single_factor_set_is_unchanged_in_both_directions`, `test_override_that_changes_R_never_inherits_the_other_systems_factors` | Ex24 now honours its existing `seis_Y`/`rho_X`/`rho_Y` keys **without dirpatch**. V_Y 679 → **1368 kip**. ρE_Y case Vy 679 → 1778.8 (dirpatch: 1778.8). Ω0E_Y case 2037 → 2736.6 (dirpatch: 2736.6). Y design drift 0.0073 → 0.0125 (Cd_Y=5.0). Member envelopes are within 0–1 % of the dirpatch run. |
| HR-08 | FIXED | engine3d, design_pipeline, report (call site) | `static_lateral()` gives edge drifts at the real extreme points, TIR = Δmax/Δavg (Eq. 12.3-2) and δmax/δavg. `torsion_summary()` is now the only TIR source: ± accidental torsion, Ax = 1. Ax per level follows Eq. 12.8-15 from displacements, limited to 1–3, and applies when Type 1 exists in SDC C–F. Ax multiplies Mta in every ρE case (`lateral_pattern`). The screen, run_one, the report and the combos all read this one value. | `test_torsion_tir_ax_applied_and_consistent` | Ex9: three different values (screen 1.23 / Ax 1.04; run_one 1.58 / Ax 1.74; report 1.59) become **one value: 1.60, Ax up to 1.66** per level, applied. Roof Mta in the ρE_Y cases 31,975 → 54,605 kip-in. Ex16: 1.33/1.22 → 1.39 / Ax 1.50. |
| HR-09 | FIXED | engine3d | For Type 1 in SDC C–F, the design drift is the largest edge drift (vertically aligned points, diaphragm rotation, accidental torsion × Ax) per 12.8.6.5. Otherwise it is taken at the CM, with the vertical projection of the top CM. Per-story edge drift is exposed in `run_one()['edge_drift']`. The elastic drift at the gate location is exposed in `seismic_drift()[d]['elastic']`, which is the stiffness basis for θ. | `test_type1_drift_is_taken_at_the_edges` | Ex16 drift_Y 0.0073 → **0.0110** (limit 0.0115; the register estimated the real value at 0.0112). Ex9 drift_Y 0.0054 → 0.0103. |
| HR-19 | FIXED | engine3d, example_build | `diaphragm_props()` works from the built footprint: panel polygons carry the area weight and the exposed edges carry the cladding, using real node coordinates. CM and J about the CM use the parallel-axis rule. The accidental arm is the actual plan extent (no extra bay). The modal analysis no longer silently overwrites builder masses. By default it replaces them with the consistent mass and **warns** when m differs by more than 2 %, J by more than 10 %, or the CM is offset. `cfg['diaphragm_mass']='builder'` keeps the builder masses. Masses on non-diaphragm nodes are kept and reported. | `test_rectangle_polar_inertia_uses_the_actual_extent`, `test_custom_build_mass_is_respected_or_replaced_loudly` | Ex9 torsional T 1.595 → 1.233 s (the old value came from extent+bay J overwriting the builder's J). Ex9 and Ex16 now warn that the builder's J was 7–17 % below the cladding-inclusive J at the CM. |
| HR-20 | FIXED | engine3d | `SFRS_TABLE` holds Table 12.2-1 plus 12.8-2 (incl. STMF Ω0 3.0, SPSW Cd 6, dual SPSW Cd 6.5, BRBF/EBF Ct 0.03). `seis(..., system=)` is new. When Cd/Ω0 are omitted they are marked `_auto` and re-derived from `cfg['system']` at use time. If no system resolves, the R-keyed legacy default is kept with a loud warning. `seis_warnings()` flags R/Cd/Ω0/Ct/x that differ from the row, marking the unconservative cases. Preflight is untouched (it belongs to hr-gates). | `test_defaults_follow_the_declared_system` | STMF without Ω0: 2.5 → 3.0. SPSW without Cd: 5.5 → 6.0 (dual: 6.5). B42 (BRBF, Ct 0.02) now warns. |
| HR-23 | FIXED | engine3d, design_pipeline | `mrsa()` gives CQC story shears plus CM and edge drifts per mode, with forces scaled by V/Vt (12.9.1.4.1) and drifts by CsW/Vt when Eq. 12.8-7 governs (12.9.1.4.2). For Type 1, static Mta×Ax is added to the edge drift (12.9.1.5). When 'RS' is requested, the demands use `cfg['seismic_demand_basis']`. The default `'envelope'` takes, per story, the larger of the ELF and scaled-MRSA story shears, so base shear stays V. `'MRSA'` and `'ELF'` are also accepted. Drift follows the same basis. The RS gate now fails if the forces handed to the demands are below V, or if modal mass is under 90 %. | `test_mrsa_scaling_and_meaningful_gate`, `test_mrsa_drift_scaling_where_eq_12_8_7_governs` | Ex24 Vt/V: X 0.77, Y 0.67 (scale 1.30/1.50). Ex9 scale 1.30/1.26. Ex9 braces +8–9 %, because the upper stories pick up the MRSA (higher-mode) story shears. |
| HR-31 | FIXED | engine3d (+ example_build) | Mass sits at the CM: a slaved `cmtag(k)` node when the master is offset, giving an exact coupled mass. Every ELF / ρE / Ω0 force is applied at the CM, i.e. as force plus offset torque at the master. This works in the dynamic model, the static demand model and `design_post.run_case`, because all three keep their masters at the same point. | `test_lplan_mass_at_centre_of_mass_not_node_average` | Ex16 L1 CM (843, 472) vs master (840, 470). Ex9 L1 CM y = 509 vs master 504. On the L-plan test the CM is (300, 300) vs the node average (315, 315). |

### Behaviour changes users will notice

- **Base shears change per direction.** The direction whose dominant mode is shorter than T[0] goes up (Ex33 X +18 %, Ex16 X +5 %, Ex9 X +2 %). Mixed-system buildings with `seis_Y` go up a lot (Ex24 Y ×2.0).
- **ρE/Ω0 combinations** use each direction's own V, ρ and Ω0. The companion is 30 % of the *other* direction's forces.
- **Accidental torsion is larger for Type 1 buildings**, because Ax is now applied (Ex9 roof Mta ×1.7). It is smaller (−20 to −33 %) for small plans, because the arm is now the actual extent and not extent + 1 bay.
- **The drift gate uses edge drift for Type 1 buildings in SDC C–F.** Ex16 moves to 0.0110 against 0.0115, and some saved designs may now FAIL drift. TIR now includes accidental torsion, so more buildings classify as Type 1, for example narrow plans such as B26 (1.28).
- **Torsional periods drop** (Ex9 1.60 → 1.23 s), and mode order can change.
- **Custom builds that hand-write `ops.mass` on the master** get a warning in `run_one` `extra['seismic_warnings']` when their masses differ from the consistent seismic mass. `cfg['diaphragm_mass']='builder'` keeps them.
- **With 'RS' in `analyses`, the demands default to the ELF/MRSA story-shear envelope.** Upper-story demands rise where higher modes matter (Ex9 braces +8 %). The RS gate can now fail.
- **New warnings** for R-keyed Cd/Ω0 defaults and for factors that differ from Table 12.2-1. Most B-archetypes (no `system`) now carry 2 warnings.
- **run_one/run results:**
  - New keys: TX/TY, VX/VY, CdX/CdY, Om0X/Om0Y, rhoX/rhoY, `torsion`, `edge_drift`, `seismic`, `seismic_warnings`, `rs_force_scale_*`.
  - The RS check keys in `run()` are now `rs_X`/`rs_Y`.
  - `V`, `Cs`, `Tu` and `Ta` report the governing (larger-V) direction.
- **Runtime:** a design is about 0–35 % slower (Ex9 design 17 → 23 s; Ex24 is faster than with dirpatch). The cause is the extra static runs for the torsion analysis (cached per model).

### Follow-ups

- **report.py (hr-report):**
  - `_run_case` and the drift tables (Ch. 7/8/13) should use `E.seismic_drift(cfg)` and `E.seismic_design_forces(cfg)[d]['Fx']` per direction, with `E.static_lateral`.
  - Ch. 3 should show both directions' V/T/R (`E.seismic_design_forces`).
  - θ (HR-24) should use `seismic_drift()[d]['elastic']`, the stiffness at the 12.8.6.5 location.
- **viewer3d.py:170** still shows one `elf(cfg, T1)` story-force set. It should show `elf_dir` X and Y.
- **preflight (hr-gates):** it can reuse `E.SFRS_TABLE` / `E.sfrs_key` / `E.seis_warnings` to compare Cd, Ω0, Ct and x (the HR-20 preflight half). It should also read the per-direction keys `seis_X`/`seis_Y`.
- **Mixed buildings:** a per-direction `system` (`cfg['system_X'/'system_Y']` or `seis_Y['system']`) should be declared. Without one, the MF-only drift division falls back to inferring the system from an exact (R, Cd, Ω0) match, or to the building-wide string.
- **ELF drift** still uses the CuTa-capped forces and the Eq. 12.8-6 minimum. The 12.8.6.2 and 12.8.6.1 relaxations are not taken, which is conservative.
- **HR-11 (hr-geomwind)** owns floor area/perimeter. `diaphragm_props` takes the weight split from `floor_w`/`perim_ft` and the location from framed panels, so if hr-geomwind changes which panels count, CM and J follow automatically, but panel detection is duplicated here.
- **Commit order:** commit 1 (engine) alone would fail the new combos test until commit 2 (`design_pipeline`) is applied. Merge the four commits together.

## Workstream `hr-geomwind`

### Status

| ID | Status | Files | What changed | Tests | Before → after evidence |
|---|---|---|---|---|---|
| HR-10 | FIXED (Cases 2–4 and roof uplift are follow-ups) | `engine3d.py` (`kz_exposure`, `_kz_integral`, `_gust_flexible`, `_cp_leeward`, roof Cp tables, `_wind_override`, `wind_detail`, `wind_forces`, `_plate_len`); `report.py` (`_wind_section`, `_wind_cc_note`) | See "HR-10 changes" below. | `test_rectangular_lowrise_windward_qz_leeward_qh_full_top_halfstorey`, `test_leeward_cp_by_L_over_B_fig_27_3_1`, `test_minimum_16psf_governs_low_wind_speed`, `test_split_level_wings_get_their_own_wall_heights`, `test_split_level_silhouette_x_wind_envelopes_both_senses`, `test_gable_along_ridge_loads_the_triangular_end_walls`, `test_gable_normal_to_ridge_adds_roof_horizontal_component`, `test_flexible_gust_factor_matches_commentary_table_c26_11_1` (Gf = 1.162, as in Table C26.11-1), `test_flexible_building_uses_gf_and_rigid_keeps_085`, `test_override_hooks_return_hand_forces_verbatim` | Ex13 split level: Y 108.7 → 183.4 kip (agent hand calc 223.2). Ex31 gable with `roofs` declared: hall roof level Y 40.7 → 75.5 kip (hand 77.6), X 16.3 → 30.7 (hand 28.5). Ex22 frame shear: X 335 → 395 (+18 %), Y 503 → 619 (+23 %). Ex1: frame plus foundation band = 118 / 222 kip, equal to the reviewer's `wind.py` reference (118 / 222). Full table below. |
| HR-11 | FIXED | `engine3d.py` (`floor_bays` (new), `_per_level`, `_auto_omitted`, `nbays`, `floor_area_ft2`, `perim_ft`, `grid` comment) | The floor plate is now built from bays. An interior grid node with no column, whose 8 neighbours are all present, keeps its 4 bays (a printed note says so). New optional keys: `openings`, `omitted_columns`, `floor_bays`, and `auto_omitted_columns=False` to restore the old behaviour. `present` keeps its meaning (column positions). | `test_omitted_interior_column_keeps_its_bays`, `test_openings_and_old_behaviour_switch`, `test_edge_node_is_a_notch_unless_declared_omitted`, `test_plan_archetypes_area_unchanged[Lplan/doughnut/Uplan/openlobby]` | Ex16 with the 7/D column removed from `present`: level-2 area 7840 → 10976 ft²; W2 557.8 → 756.5 kip; total W 1639.7 → 1830.0 kip. Areas of all B02–B44 archetypes are unchanged. |
| HR-12 | FIXED | `engine3d.py` (`floor_beam_gaps`) | A grid pair is treated as modelled if a chain of horizontal elements joins the two grid nodes through off-grid nodes lying on the segment between them. A chain that stops short, leaves the line, or passes another grid node is still reported as a gap. | `test_floor_beam_gaps_follow_collinear_chains[split / three_pieces / missing_half / offline]` → 0 / 0 / 1 / 1 | Ex13: 20 → 0 gaps. Ex8 (explicit EBF links): 52 → 0; `run_one` model_complete now PASS (Ex8 now ALL PASS). Ex1 and Ex22: 0 → 0. |
| HR-39 | PARTIAL | `engine3d.py` (`floor_w`, `_lvl_val`) | Two seismic-weight-only inputs are now accepted (see "HR-39 changes" below). Cladding now uses mid-height tributaries of the real wall (HR-39 last bullet; see HR-11 commit). Point, line and crane gravity loads are not done. | `test_seismic_weight_only_keys_do_not_touch_gravity`, `test_cladding_uses_half_storey_above_and_below`, `test_cladding_setback_walls_go_to_the_levels_they_span` | Total W changes −0.1 to −0.2 % on the regular saved designs and −1.6 % on Ex16 (Z-plan; interior step walls are no longer counted). |

#### HR-10 changes

- Windward walls use Cp 0.8 with qz integrated exactly over the wall height.
- Leeward walls use Cp(L/B) from Fig. 27.3-1 with qh at the mean roof height.
- Wall loads come from the building volume:
  - bay stacks give a projected silhouette per strip parallel to the wind;
  - each wall is split by mid-height bands, and the top half-storey is kept;
  - the bottom half of storey 1 goes to the foundation and is reported as `F_base_direct`. Setting `ground_band='lowest'` lumps it into level 1 instead.
- Gable roofs (`cfg['wind']['roofs']`) get gable end walls and roof slopes from the Fig. 27.3-1 roof table.
- Parapets use GCpn per §27.3.4 (`parapet_ft`).
- Gust factor per §26.11:
  - rigid (G = 0.85) when low-rise or n1 ≥ 1 Hz;
  - otherwise Gf from Eq. 26.11-10, using n1 from cfg or the approximate na of §26.11.3;
  - a loud warning when the §26.11.2.1 limits are not met;
  - damping beta defaults to 0.01.
- The §27.1.5 minimum (16 psf walls, 8 psf roof) is applied per level.
- Both wind senses are enveloped.
- Internal pressure cancels for the MWFRS (documented).
- Override hooks: `cfg['wind_forces_override']`, `cfg['wind']['story_forces']` and `cfg['wind_by_level']`.
- The footprint is probed before computing, so the result no longer depends on call order.
- `wind_detail()` returns the full breakdown.

#### HR-39 changes

- `seismic_mass_only = {k: psf}` adds to W only. This is the 12.7.2 item-2 partition allowance; it is not added to gravity.
- `seismic_weight_kip = {k: kip}` adds a lumped seismic weight.

#### Before → after on saved designs

- Engine's own MWFRS, with the agents' cfg overrides stripped.

| Design | Vw X (kip) | Vw Y (kip) | W total (kip) | Floor-beam gaps |
|---|---|---|---|---|
| Ex22 hospital SMF (Gf 1.01 / 0.97, flexible by na) | 335.2 → 395.3 | 502.8 → 619.4 | 13368 → 13356 | 0 → 0 |
| Ex13 split level | 71.5 → 82.0 | 108.7 → 183.4 | 2951.5 (unchanged) | 20 → 0 |
| Ex31 gable, flat idealisation at 31 ft | 32.1 → 38.1 | 80.3 → 98.0 | 481.0 (unchanged) | 0 → 0 |
| Ex31 gable, `roofs={2:{eave 26, ridge 36, ridge X}}` | 32.1 → 38.2 (7.5 / 30.7) | 80.3 → 88.3 (12.8 / 75.5) | — | — |
| Ex1 SCBF | 109.0 → 106.3 (+11.7 to foundation) | 181.6 → 199.9 (+22.3) | 5498 → 5491 | 0 → 0 |
| Ex8 EBF L-plan | 260.4 → 269.3 | 472.9 → 530.4 | 14966 → 14932 | 52 → 0 |
| Ex16 Z-plan school | 75.5 → 66.4 | 127.3 → 126.3 | 1859 → 1830 | 0 → 0 |
| Ex30 | 157.8 → 171.7 | 197.3 → 220.9 | 8029 → 8023 | 0 → 0 |
| Ex2 SMF 9 levels (Gf 0.96) | 401 → 496 | 401 → 496 | 19733 → 19695 | 0 → 0 |

The Ex31 gable row compares with the agent's hand values: X 7.0 / 28.5, Y 20.4 / 77.6.

Notes on the differences from the agents' hand calculations:
- **Ex13 X:** 82 kip against the hand 131 kip. The hand calculation treated each wing as a separate building with its own windward and leeward walls, so it counted the walls the wings share (interior, shielded). The engine uses the per-code projected silhouette of one enclosed building.
- **Ex31 office level, Y:** 12.8 kip against 20.4 kip, for the same reason.
- **Ex16 X:** falls by 12 % because the old Cpnet of 1.3 is replaced by 0.8 + 0.35 (L/B = 1.75) over the same projected area.

### Behaviour changes users will notice

- **Wind story forces change for every building with `cfg['wind']`.**
  - Regular buildings go up about 3–24 %. The cause is leeward qh with Cp(L/B), the full top half-storey, and Gf for non-low-rise buildings.
  - Split-level, lean-to and gable buildings go up much more (Ex13 Y +69 %).
  - Z or offset plans can go down slightly (Ex16 X −12 %).
  - Wind may now govern where seismic governed before. Check Ex31 Y, Ex33 and low-seismic sites.
- **The frame base shear (sum of story forces) excludes the bottom half of the lowest storey.**
  - That load goes straight to the foundation. It is shown as "plus N kip delivered directly to the foundation" in the report and as `F_base_direct` / `V_foundation` in `wind_detail`.
  - Level-1 forces can therefore be slightly lower than the old "storey below" value (Ex1 X −2 % in frame shear).
  - `cfg['wind']['ground_band']='lowest'` restores lumping into level 1.
- **Buildings that are not low-rise (h > 60 ft or h > least plan width) are now checked for flexibility.**
  - With no `cfg['wind']['n1']`, the approximate na (lower bound) is used, and many mid-rise MRF buildings come out flexible.
  - Gf (beta 0.01) then replaces G = 0.85: Ex22 1.01, Ex2 0.96, B12 1.28, B40 1.42.
  - A printed `[engine3d] WIND FLEXIBLE ...` note is given, plus a loud warning when the §26.11.2.1 limits fail (h > 300 ft).
  - Agents should pass `n1 = 1/T1` (or `n1X` / `n1Y`) from the modal analysis.
- **`Cpnet` in cfgs is now ignored.** The report no longer prints it.
- **The hand-force keys are now honoured natively:** `cfg['wind']['story_forces']` (Ex31) and `cfg['wind_by_level']` (Ex13), plus the new `cfg['wind_forces_override']`. The cfg monkeypatches in Ex13 and Ex31 still work, but are no longer needed.
- **Omitted interior columns no longer reduce floor area or W.** A printed note appears when this auto rule fires. Real atria must now be declared with `cfg['openings']` if they are a 2×2-bay hole around a single missing node. Larger holes (for example `doughnut`) are unchanged.
- **Cladding seismic weight now uses mid-height tributaries of the real walls.**
  - Old: storey below × level perimeter.
  - New: half the storey below plus half the storey above, the bottom half of storey 1 to grade, and the true walls at setbacks and split levels.
  - W changes are small: 0 to −0.2 % on regular designs, −1.6 % on Ex16, and none where `clad=0`. For a tall first storey, the roof and upper levels get slightly more mass.
- **`model_complete` passes for subdivided chevron, EBF-link and two-storey-X girders** (Ex8: RESULT now ALL PASS).

### Follow-ups

- **Fig. 27.3-8 Cases 2–4** (eccentric / torsional MWFRS) need a torsional moment per level. `design_pipeline.combos` currently builds `latW = (F, 0, 0)` only.
- **Roof uplift and internal pressure** for roof members and anchorage (HR-10 / Ex31 F2) need gravity-direction wind patterns in `static_model`.
- **Monoslope, hip, arched and mansard roofs:** a monoslope gives a warning only, and the high wall above the eave is not modelled.
- **Automatic n1 from the modal analysis:** the pipeline could set `cfg['wind']['n1X'/'n1Y'] = 1/T` before calling `wind_forces`. This lives in hr-seismic / design_pipeline territory.
- **HR-39 point, line and crane gravity loads** need `static_model` application (not my file). Only the seismic-weight inputs were added.
- **`_modal_impl` Jm** still uses `info['present']` node extents plus one bay (seismic agent). `plan_irregularities` still screens node footprints.
- **Wind geometry ignores `skew`** (parallelogram plans use unskewed strips), and Kzt is constant with height (as before).
- **Two or more adjacent omitted interior columns** are not auto-detected. Declare them with `cfg['omitted_columns']`.

## Workstream `hr-demands`

### Status

| ID | Status | Files | What changed | Test | Before → after evidence |
|---|---|---|---|---|---|
| HR-03 | FIXED | static_model.py, design_post.py | Gravity is applied bay by bay with the seismic-weight loads: `_Dlev`+`extra_mass_floors` dead, `_Llev` live, cladding.<br>`cfg['Lr']`, `snow` and `rain` apply on roof bays (optional `*_by_level` overrides).<br>A bay is a roof bay at the top level, when listed in `cfg['roof_levels']`, or when no higher level frames it, so lower roofs get Lr/S/R. Split levels stay floors.<br>A per-level dead audit against `floor_dead` goes into the package. | `test_gravity_matches_seismic_weight_and_lower_roofs` | Ex23 roof Lr applied: 282 → **1330 kip** (= W basis). Storey-9 gravity column W14X48: 99 → **182 kip**.<br>Ex20 level-2 W27X94: 594 → **864 k-ft**.<br>Ex13 dead: 2320 → **2951 kip** (= W). Its level-4 workshop roof now gets roof S/Lr, not floor L.<br>Ex8 L5 west leg now gets Lr (216 kip) instead of 65-psf live.<br>Static dead = W dead at every level of every job. |
| HR-04 | FIXED | static_model.py | Beam V is the **solved** shear: max \|Vy\|,\|Vz\| of the sub-element `localForce`, member loads included.<br>It is enveloped with the one-way gravity shear only where the one-way floor applies (girder rule / chevron no-brace-support). Propped and split beams (EBF links) use the solved V. | `test_beam_shear_is_solved_and_axial_keeps_sign` | Ex8 W18X76 links: V 40 → **176 kip** (review solved 167).<br>Ex23 W36X194 SMF beams: 94 → **148 kip** (solved 130 + girder increment; review 128.8).<br>Ex20 W40X249 transfer girder: 79 → **230 kip** (review ≈ 230). |
| HR-05 | FIXED | design_pipeline.py | 2.3.1 combos now include:<br>• every roof branch present (Lr always; S and R when declared) in 2a and 3a: 1.2D+1.6Lr+… is kept beside 1.0S;<br>• 3a with 0.5W (dominated roof branches pruned for the wind combos only);<br>• 4a per branch;<br>• 5a 0.9D+1.0W with the Fig. 27.3-1 roof suction;<br>• a companion live factor per level (`LiveFactor`): 1.0 where Lo > 100 psf, garage or assembly (`cfg['garage_levels']`, `assembly_levels`).<br>0.6D+0.6W is ASD and does not apply (the engine is LRFD). | `test_combinations_roof_branches_live_factor_orthogonal_and_omega0` | Ex30 roof W21X44 (snow 20, Lr 20): 386 → **458 k-ft** (+19 %, the 1.6Lr branch; review −16 %).<br>Ex13 125-psf mezzanine now gets 1.0L in the companion combos. |
| HR-06 | FIXED | design_pipeline.py, static_model.py | Ω0 axial cases (`[col]`, axial-only per D1.4a(b)) now run for the columns of **every** R > 3 SFRS, moment frames included, plus every braced frame.<br>Collectors get Ω0 statics (12.10.2.1(a)): uniform diaphragm delivery along each SFRS line, minus the forces the vertical elements take at each node. | `…_and_omega0` | Ex22 SMF corner column: tension 225 → **851 kip**, compression 755 → **1250 kip** (review 846 / 1240).<br>Base uplift 0 → **851 kip**. |
| HR-07 | PARTIAL | static_model.py, design_pipeline.py | Ecl cases (`kind='ecl'`, label "capacity-limited, AISC 341-22 Fx.3"; Ω0 cases stay separately labelled). The yielding elements are removed and replaced by forces; diaphragm masters are restrained; yielding is simultaneous in X and Y:<br>• **SCBF** F2.3 (a)/(b): T = RyFyAg; C = min(RyFyAg, FneAg/0.877) with Fne from E3 using RyFy at the end-to-end length; post-buckling 0.3C.<br>• **BRBF** F4.2a/F4.3: ωRyPysc / βωRyPysc. Asc comes from `cfg['brb']` or is parsed from the label; Fysc/Ry/β/ω defaults are flagged ASSUMED.<br>• **EBF** F3.3: links at 1.25RyVn, Vn = min(Vp, 2Mp/e).<br>• **Chevron / V beams**: the unbalanced load follows from the brace forces, plus the gravity "no brace support" floor (F1.4a(a), F2.4a(a), F4.4a(a)).<br>• **OCBF** V-beams F1.4a(a): min(Ω0E, RyFyAg) / 0.3Pn.<br>• Expected strengths are written into brace-connection slots and member inputs.<br>• **SPSW** F5.3(b): PARTIAL (see Follow-ups).<br>• **STMF** E4.3, unidentified brace systems, and non-converged cases: NOT EVALUATED, stated in `calc_package.json['demand_analysis']['not_evaluated']` and `member_demands.md`. | `test_scbf_expected_strengths_and_chevron_unbalanced_load` (P·L/4 within 2 %), `test_ocbf_v_beam_unbalanced_load_f1_4a`, `test_ebf_link_and_brb_strengths` | Ex1 SCBF chevron beam W36X282: 655 → **5453 k-ft**. W14X257 column: 1559 → **2602 kip** (+67 %; review +73 %).<br>Ex13 OCBF V-beam W24X104: 92 → **715 k-ft** (review 5–9× low).<br>Ex20 SCBF base column W14X873: 5519 → **10,344 kip** (review hand calc 10,746).<br>Ex30 SCBF column W14X176: 884 → **1452 kip**.<br>Ex8 EBF braces: 252 → **495 kip**; column 1/C W14X370: 1013 → **2771 kip**.<br>Ex23 BRBF core W14X873: 7562 → **9570 kip**. |
| HR-13 | FIXED | static_model.py | Every beam is mapped geometrically to its parent grid span. Split beams (off-grid I-nodes, beams drawn from either end) load by their position on that span. Point loads go to exactly one sub-element. | `test_split_beam_gets_full_tributary_and_tags_map_by_tag` | Ex8 applied dead: 13,266 → **13,968 kip** (−5 % → 0 %, reviewers' `gravtot.py`). |
| HR-17 | FIXED | design_pipeline.py, static_model.py | Base reactions are enveloped over **all** cases (gravity, wind with roof uplift, ρE, Ω0, Ecl), with max P, uplift, V, Vx/Vy, M and governing combos.<br>Writes new `conn-base-<role>-<sec>` slots plus `connection_demands.csv`.<br>Column splice slots now carry P_comp, P_tens, V, M and My with combos. | `test_base_reactions_enveloped_with_roof_uplift` | Ex31: office column base uplift 0 → **16.9 kip** (0.9D+1.0W roof suction; review 16.6). Portal base V 0 → **26.7 kip** (review 25.9 thrust).<br>Ex22 base uplift **851 kip**. |
| HR-18 | FIXED | static_model.py, design_pipeline.py | Direct Analysis in the strength model that produces member demands (`cfg['dam']`, default on):<br>• 0.8·E and G on all members (C2.3(a)).<br>• τb = 1 with the C2.3(c) additive 0.001Yi (default `tau_b='notional'`), or `tau_b='check'`, which reports αPr/Pns > 0.5.<br>• Ni = 0.002Yi distributed like the gravity load: four directions in gravity-only combos, and additive in the lateral resultant direction when Δ2nd/Δ1st > 1.7 (C2.2b(d), ratio computed per run).<br>• Nominal stiffness is kept for the dynamic model, drift, periods and the force-diagram `run_combo`.<br>• Documented in the module docstring, `member_demands.md` header and `calc_package.json['demand_analysis']['dam']`. | `test_dam_stiffness_and_notional_base_shear` (base shear = 0.003W) | Demands rise about 1–3 %. Ex22 drift ratio 1.04. |
| HR-27 | FIXED | static_model.py, engine3d.py | One-way rule per bay:<br>• With `cfg['deck_span']` (or `infill_dir`) and `infill_spacing`, or modelled infill (auto-detected), the actual one-way load path is analysed: deck lines take s/2 per side; girders take infill reactions as point loads.<br>• Without them, a half-bay-per-side girder floor applies, with nb-based perimeter tributary: simple-span diagram for pinned beams; solved + extra girder-load effect for rigid-ended beams.<br>• The deflection gate reads the **solved** static model (`service_deflections`). | `test_one_way_deck_span_distribution` (P·L/3), `test_default_one_way_girder_floor_uses_half_bay_per_side`, `test_default_one_way_rigid_beam_floor_adds_girder_load_effect`, `test_service_deflection_from_solved_model` | Ex30 with `deck_span='X'`, infill 10 ft: 40-ft beams 1092 → **388 k-ft** (review 358). Deflection gate TL ratio 2.87 → **1.03** (false FAIL gone).<br>Ex4 perimeter BRBF beams: 667 → 390 k-ft (half bay). |
| HR-29 | FIXED | static_model.py | Kind and section are looked up in `info['ele']` **by tag**. Unlisted elements are classified from geometry and warned. | `test_split_beam_gets_full_tributary…` (list reversed), `test_unlisted_element_is_warned_not_mislabelled` | — |
| HR-30 | FIXED | design_pipeline.py | The 100/30 combination uses both orthogonal signs: 16 ρE cases per direction instead of 8. Ω0 cases likewise. `cfg['orthogonal_combination']=False` drops the 30 %. | `…_orthogonal…` | Seismic case count 16 → 32. |
| HR-35 | FIXED | static_model.py | Beam axial: compression and tension are enveloped separately per sub-element. | `test_beam_shear_is_solved_and_axial_keeps_sign` | — |
| HR-36 | FIXED | static_model.py | Single cache with a complete key: every OpenSees call of the built model, every non-callable cfg value, the cases (incl. per-level and roof factors and lateral dicts), the options, and the source of `static_model` and `engine3d`. The stale two-slot scheme is removed. | `test_demand_cache_hits_only_on_identical_inputs` | Hit on an identical rerun; miss after a load edit. |

### Behaviour changes users will notice

**Run size**
- About 2× more combinations: 63–81 instead of 27–35.
- Run time is 2–3× longer, e.g. Ex20 49 s vs 20 s, Ex8 48 s vs 23 s.
- The report shows more per-case rows.

**Demand changes**
- **Column axial jumps** wherever capacity-limited design applies:
  - SCBF / BRBF / EBF: +20 % to +200 %.
  - SPSW VBEs: 3–5×.
  - SMF columns now get Ω0 axial.
  - Agents' designs that relied on the old Ω0-only columns will fail D/C.
- **Ω0 column cases are axial-only** (D1.4a(b) "permitted to neglect applied moments"). Column moment demands from the old non-concurrent Ω0 envelope drop, e.g. Ex23 SMF columns 2855 → 1707 k-ft. This is the HR-39 "gross over-sizing" item.
- **Beams:**
  - Solved shear (links ×4).
  - Chevron and V-braced beams carry the unbalanced load.
  - Collector / strut axial is non-zero on SFRS lines in SDC C–F. It appears as P_comp / P_tens plus a `collector_axial_kip` column.
  - Perimeter beams use a half bay.
- **Undeclared one-way floors** stay conservative and are noted. Declaring `deck_span` / `infill_spacing` gives the actual one-way demands and removes the false deflection FAILs.
- Lower roofs get roof loads (Lr / S / R) instead of floor live. `cfg['Lr']`, `D_by_level` and `L_by_level` are honoured in the demands.
- Direct Analysis by default (`cfg['dam']=False` reverts). K = 1 is now actually backed by the analysis.

**Output format changes**
- **New package content:**
  - `conn-base-*` slots (agents must fill them).
  - Splice slots typed "column splice".
  - A `demand_analysis` block, rendered by the report as a supplementary block.
  - New trailing columns in `member_schedule.csv`.
  - Base-reaction table in `member_demands.md`.
- **Combination labels:**
  - Roof factors print as `1.0S` and `1.6Lr`.
  - Seismic labels carry `/0.3Y±`.
  - Governing labels in the demands may end in `+Ni(X+)` / `+Ni` (notional loads).
- **New optional cfg keys:**
  - Floor and roof loading: `deck_span`, `infill_dir`, `infill_spacing`, `roof_levels`, `Lr_by_level`, `snow_by_level`, `rain`, `rain_by_level`, `garage_levels`, `assembly_levels`.
  - Combinations and analysis: `orthogonal_combination`, `dam`.
  - Capacity design: `brace_system`, `brace_material`, `brb` (Asc / Fysc / Ry / beta / omega / KF), `ebf` (Fy / Ry), `ebf_links`, `spsw` (tw / Fy / Ry / alpha), `ecl_force`.
  - Wind: `wind.roof_uplift`, `wind.Cp_roof`, `wind.GCpi`.

### Follow-ups

**Remaining HR-07 scope**
- **SPSW (F5.3(b)):**
  - Done: the web tension field is applied as the panel resultant 0.5RyFy·tw·L·sin2α on the tension diagonals, and the HBE 1.1RyMp hinge shears on the VBEs.
  - **Not evaluated:** the distributed tension-field flexure of VBEs/HBEs and the HBE hinge moments. This is stated in the package. Lcf is taken as the centreline length (conservative).
- **Not evaluated:**
  - STMF E4.3 special-segment Ecl.
  - Multi-tiered braced frames F2.3(c).
  - EBF links adjacent to columns. They need `cfg['ebf_links']`.
- **BRB defaults:** the β, ω, Fysc and Ry defaults (1.1 / 1.4 / 42 / 1.1) are assumptions, flagged ASSUMED. They need `cfg['brb']` from the supplier.

**Other analysis items**
- **Collectors:** only 12.10.2.1(a) (Ω0 × ELF) is applied per beam. Items (b)/(c) (Fpx) and the 12.3.3.5 1.25 factor remain with the seeded collector slot.
- **12.3.3.4:** Ω0 for elements supporting discontinuous frames (e.g. the Ex20 transfer girder) is not auto-applied.
- **τb < 1 (C2.3(b)):** not modelled. The C2.3(c) alternative is the default.
- **P-δ:** columns are single elements, so P-δ is left to B1 at member design (C2.1(b) user note).

**For other owners**
- **`report.py` (hr-report):**
  - `col_only` rows are described as "Ω0 column overstrength" also for the new Ecl cases. Use `case.kind == 'ecl'`.
  - Line ~1242 (uplift text) and ~1256 (DAM text) should now state what the engine does.
  - Lines 1606 and 1681 key beams by (i, j, k, dir); split spans now share that key.
  - Per-case tables still use nominal-stiffness `run_combo`.
- **Contract (`AGENT_START.md` :79, :552) and `consistency._SYS_REQUIRED` (HR-07):** they still describe Ω0 as "capacity design" and "1.1RyPn". They should point to the Ecl cases and expected strengths. Not my files.
- **`engine3d.floor_w`:** the 15 % snow (pf > 45 psf) is applied only at the top roof. Lower-roof snow is not in W (hr-seismic area).

## Workstream `hr-gates`

### Status

| ID | Status | Files | What changed | Tests | Before → after |
|---|---|---|---|---|---|
| HR-14 | FIXED | `steltic/agent.py` (`_completion_gate` + helpers), `steel_engine/pipeline.py`, `steel_engine/engine3d.py` (`stability_theta`, `_edge_story_drifts`), `steel_engine/consistency.py` (`_record_result`) | The pipeline writes `design/pipeline_results.json` (all sanity checks, θ, dual split, cfg.py SHA-256). `consistency.check` writes `design/consistency_result.json` (issues, package and cfg SHA-256). The gate never runs cfg.py; it reads both files and refuses the final answer in these cases: (a) results are missing or stale; (b) a sanity check fails; (c) θ is not evaluated or θ > θmax; (d) consistency is not PASS on the current files. Explicit waivers go in `calc_package['gate_waivers']` (justification ≥ 20 chars); code limits are not waivable. String D/Cs are parsed. The D/C bound is 1.0, the same as consistency. An entry waiver needs a real, clause-citing justification. New engine check `stability_theta` follows 12.8.7: Px = D + 0.5(0.4L0) on the actual area, largest-edge story drift, ÷(1+θ), θmax ≥ 0.10, both directions. | `test_gate_*` (5), `test_theta_formula` | Ex16 and Ex20 old gate `[]` → new gate refuses on the Table 12.2-1 flag (not waivable). θ is now gated on all 15 designs (max 0.070 Ex2 vs 0.10; Ex13 split-level 0.0065, offsets skipped). |
| HR-15 | FIXED | `steel_engine/preflight.py`, `steel_engine/consistency.py` | `sdc_assessment`/`sdc_of_cfg`: SDC from SDS/SD1/S1/Risk Category. The 11.6 exception applies only when all 4 conditions are shown. A declared lower `cfg['sdc']` is an ERROR and is not used by the engine; a higher one is kept with a WARN. Table 12.2-1 steel rows (SMF, STMF, IMF, OMF, EBF, SCBF, OCBF, BRBF, SPSW, C-PSW, not-detailed, duals with SMF/IMF) are checked for every sub-system of a dual or mixed declaration, in the building's SDC (no SDS gate). The 12.2.5.4 increase needs numeric TIR ≤ 1.4 and ≤ 60 % per plane, in `cfg['height_increase_12_2_5_4']` or `capacity_design['height_limit']`. The single-story/light-frame exceptions (12.2.5.6/12.2.5.7) and footnote j need declared loads. Consistency mirrors the ERRORs. | `test_sdc_*`, `test_imf_*`, `test_sdc_f_limits_*`, `test_12_2_5_4_*`, `test_consistency_height_*` | **Ex16** IMF 36 ft, SDC D, SDS 0.48: no flag → ERROR 36 > 35 ft (12.2.5.7.1(b)). With the brief's `sdc='C'`: silently used → ERROR, and the engine uses D. **Ex20** 161 ft: unclearable → clears with numeric evidence (TIR 1.31, shares 0.55/0.50). With the real Grid-C share (199 %) it stays flagged "plane share X = 199% > 60%". SDC-F 120-ft SCBF/BRBF, IMF in SDC E/F, STMF in SDC F and mixed SMF+SCBF > 160 ft are now flagged. |
| HR-16 | FIXED (see caveat) | `steel_engine/engine3d.py` (`dual_mf_share`, `_story_split`, `_strip_braces_builder`, `run_one`) | Story-shear split in X and Y from element end forces across each story mid-plane (MF share = 1 − brace share). An MF-only model removes every `brace` element from the built domain (custom_build too) and restrains orphan work points; it is analysed at 0.25 V in X and Y, with a per-element 0.25E force envelope. `dual_25pct` runs for `dual_check` or any "dual" system and fails below 25 %. The gate accepts a numeric `capacity_design['dual_25pct']` record (D/C ≤ 1.0) instead. | `test_dual_share_from_the_analysed_custom_build`, `test_gate_dual_cleared_by_numeric_record` | **Ex23**: MF_fraction 0.399 (X roof-displacement proxy) → 0.2645, the minimum story share over both directions (story 2, X). The reviewer's figure was 0.274. Recovered story totals equal the ELF story shears exactly (2709/2654/… kip). |
| HR-20 | FIXED | `steel_engine/preflight.py` (`factor_findings`), mirrored in consistency | R, Cd, Ω0, Ct, x are checked against Table 12.2-1 / 12.8-2 for the declared system. The flexible-diaphragm Ω0 reduction is honoured. MF may use the "all other" Ct row. Mixed systems need the envelope or `seis_X`/`seis_Y`. A mismatch is an ERROR; a conservative one can be kept with `cfg['seis_factor_basis']` (WARN). | `test_factor_mismatches_*`, `test_mixed_system_envelope_*` | Ex4 and Ex28 as saved: clean. With the R-keyed `seis()` defaults: Ex4 BRBF Cd 5.5/Ω0 3 → conservative ERROR; Ex28 STMF Ω0 2.5 → UNCONSERVATIVE; Ex9 SPSW Cd 5.5 → UNCONSERVATIVE. New true positive: **Ex33** (IMF Y + OCBF X) applies the OCBF Cd 3.25 to the IMF direction, but Table 12.2-1 gives 4.0 → UNCONSERVATIVE. |
| HR-21 | FIXED | `steel_engine/engine3d.py` (`built_scheme`, `_built_mismatch`, `_model_gate`) | The declaration is checked against the BUILT domain for every model: column-base `getFixedDOFs` (fixed/pinned/partial/spring), `add_beam` releases (rigid/pinned/one-end) and leaning columns. Beams not built with `add_beam` are reported as unverifiable. | `test_model_gate_reads_the_built_model` | Reproduced case (declared pinned/rigid/leaning on an all-fixed custom_build): `(True, True, None)` → `(True, False, "bases='pinned' but … fixed …; gravity='leaning' but …")`. All 15 saved designs and all 43 built-ins still pass. |
| HR-33 | FIXED | `steel_engine/consistency.py` | Demand and capacity are matched by normalised key semantics (Pu↔φPn, Mu↔φMn, Ru↔φRn, demand_ratio↔capacity_limit), not order or substring. `phi`, `strut` and `input` are no longer matched. Interaction checks only need D/C ≥ each term. Absolute tolerance is 0.006. System checks need capacity_design entries with computed numbers; IMF and STMF have their own lists and no SCWB. Composite needs computed stud/camber/wet-stage values or a `composite_design['scope']` statement. `wing` is matched as a word. Numeric "≤" comparisons are no longer called symbolic. The R ≤ 3 note clears with a numeric `governing_hazard` block. Seeded text, and the undesigned seeded collector slot, neither trigger nor clear guards. | `test_demand_capacity_*`, `test_interaction_*`, `test_numeric_comparisons_*`, `test_system_checks_*`, `test_seeded_text_*`, `test_composite_*`, `test_r3_*` | Across all 15 designs, consistency issues changed only as follows: Ex16 0→1 (height), Ex20 1→1 (now clearable), Ex31 1→1 (now clearable), Ex33 0→1 (Cd). An interim key-semantics version had produced 9 false "≠ demand/capacity" flags on Ex4 interaction checks; these are gone. |
| HR-34 | FIXED | `steel_engine/design_pipeline.py` (seeded collector block) | 12.10.2.1: max of (b) Ω0·Fpx and (c) Fpx(12.10-2)×1.25 (12.3.3.5, SDC D–F). The 25 % is not stacked on Ω0 (12.3.3.5 Exception). SDC A/B use Fpx without Ω0. The 0.5 line share is labelled as an assumption. Typo fixed. The trigger uses the accidental-torsion TIR. The agent phase hint text is corrected. | (Ex8 run; a unit test needs a full DP.design run) | **Ex8** `P_basis` 602 kip (1.25 × 2.0 × 482 × 0.5) → 482 kip (2.0 × 482 × 0.5; governing "12.10.2.1(b)"). |
| HR-40 | PARTIAL | `steel_engine/preflight.py` (`rho_findings`) | ρ: WARN when undeclared (SDC B/C: 12.3.4.1 gives 1.0; D–F: declare it). ERROR for values other than 1.0 or 1.3. WARN for 1.0 in D–F without `rho_basis`. The other HR-40 items live in files outside my assignment (see Follow-ups). | `test_rho_defaults_are_reported` | Ex2, Ex24, Ex28: WARN "rho = 1.0 in SDC D: record 12.3.4.2 basis". |

### Behaviour changes users will notice

- New preflight ERRORs (non-blocking) and consistency issues that block completion:
  - IMF above 35 ft in SDC D, whatever SDS is;
  - SDC-F / E limits;
  - mixed sub-systems;
  - 12.2.5.4 needs numbers;
  - a declared SDC below the derived one (the engine now uses the derived SDC: ρ, drift/ρ and height limits come back);
  - R/Cd/Ω0/Ct/x different from Table 12.2-1 / 12.8-2, e.g. engine `seis()` R-keyed defaults for BRBF/EBF/SPSW/STMF.
- New sanity checks:
  - `stability_theta`, in `run` and `run_one`;
  - `dual_25pct` now runs for any "dual" system and can FAIL. Ex23 passes at 26.5 %.
- `model_consistent` can now FAIL for custom_build models whose declaration contradicts the built bases, releases or leaning columns.
- The completion gate refuses a final answer in each of these cases:
  - no `pipeline_results.json` / `consistency_result.json`;
  - results older than cfg.py or calc_package.json (re-run `consistency.check` after editing waivers);
  - any failed sanity check;
  - θ > θmax;
  - any unwaived consistency issue.

  Older jobs finished before this change will be asked to re-run the pipeline and consistency.check once.
- Seeded collector `P_basis` drops by 20 % (the 1.25 no longer stacks on Ω0). The demand dict has new keys.
- Consistency flags fewer false positives: interaction checks, numeric "≤" text, "drawing"/"showing", tiny D/Cs. It flags keyword-only system and composite "evidence" that used to pass.

### Follow-ups

- HR-40 items outside my files:
  - `engine3d.SEC` hand values: W14X605, W40X199, W36X194, W14X370;
  - HSS hand table / "H14";
  - `E.HSS.get` bypassing the CSV (`design_pipeline.py:209`);
  - `sections._HSS_R` / A1085 b/t default;
  - the `Lb_in` seed 0.095 vs 341-22 D1.2b 0.086·ry·E/(RyFy);
  - the engine/report ρ default of 1.3 regardless of SDC. Preflight only warns; `design_pipeline.combos`, `report` and `drift_allowable` still default 1.3.
- `engine3d._mf_only` still treats a mixed "SMF + SCBF" declaration as moment-frame-only (Δa/ρ in both directions; conservative). It could call `preflight.mf_only_system`. `report._sdc` still derives the SDC without the declared-override logic; it could call `preflight.sdc_of_cfg`.
- `seis()` Cd/Ω0 defaults are still keyed to R; that is HR-20's engine half, presumably hr-seismic's. Preflight now catches the mismatch.
- HR-16 caveat: the MF share counts every non-brace element crossing the story (core columns in braced bays included). The MF-only forces are the unfactored 0.25E effect; the agent applies ρ and the 2.3.6 combinations. The "strength at 0.25V" check is enforced through the numeric `capacity_design['dual_25pct']` record, because the engine has no capacities.
- θ uses the largest-edge drift at every story, which is conservative for torsionally regular buildings. The report's θ table (HR-24, `report.py`) is separate; it could reuse `extra['theta']`.
- HR-34 needs no unit test; it was verified on Ex8. A small DP.design fixture would let it be pinned in CI.

## Workstream `hr-report`

### Status


**Tests**
- `PYTHONPATH=$PWD:$PWD/steel_engine python -m pytest tests -q` gives **41 passed**: 18 existing tests and 23 new ones in `tests/test_report_hr.py`.
- I also built a trial merge with `fix/hr-seismic`, using the conflict resolution given below. On that merge the suite gives **53 passed**, and the reports for Ex22, Ex24 and Ex20 render cleanly.

- `before/<job>/report.html`: pristine code.
- `after/<job>/report.html`: this branch.
- `merged/<job>/report.html`: this branch plus hr-seismic.
- `sec.py` extracts text sections from a report.

| ID | Status | Files | What changed | Test | Before → after |
|---|---|---|---|---|---|
| HR-22 | FIXED | engine3d, report | New `engine3d.rbs_drift_factor(cfg,dir)` implements AISC 358-22 **5.7 Step 1** (corpus AISC_358_22.md:1657): ×1.1 at 2c/bbf = 50 %, interpolated linearly below that. Inputs: `cfg['rbs']={'c_over_bf'|'flange_reduction','dirs'}`, `True`, `cfg['rbs_drift_factor']` (scalar or per direction), or "RBS" in the system/arch text (only in the MF direction when the text says e.g. "SMF in X"). The factor is applied in the run()/run_one() drift gates and in the report (Ch. 7 θ, Ch. 8, Fig., QA). If the calc package cites RBS but the cfg does not declare it, the report applies 1.1 and states loudly that the gate did not. | `test_rbs_*`, `test_drift_gate_and_report_apply_the_same_rbs_factor` | Ex22 drift 0.95 % PASS → **1.003 % / 1.040 % FAIL** (limit 1.0 %). Ex2 1.99 % PASS → **2.128 % FAIL** (limit 2.0 %). |
| HR-24 | FIXED | report | θ is reported in both directions. Px = 1.0D + 0.5·(0.4L0, or 0.8L0 where L0 > 100 psf) on the framed area (12.8.6.1/12.8.7). Δxe is taken at the 12.8.6.5 location and includes RBS. θ = Px·Δxe/(Vx·hsx) is shown raw and ÷(1+θ) (P-Δ analysis). θmax = 0.5/(βCd) ≤ 0.25, not less than 0.10, with β = `cfg['theta_beta']` (≥ 1.25/Ω0). Per-direction Cd is used. The QA scorecard uses the same rows. | `test_theta_hand_calc_*`, `test_theta_max_cap_*` | Ex2: false FAIL θ 0.118 vs 0.091 → **0.076 vs 0.100 PASS** in both X and Y (Px 33 476 → 22 072 kip at story 1). |
| HR-25 | FIXED | engine3d (`plan_irregularities`, new `plan_irregularity_levels`), report | Every item of Tables 12.3-1, 12.3-1a and 12.3-2 is now evaluated numerically:<br>• TIR from edge drifts with accidental torsion; Ax from displacements.<br>• Re-entrant only when BOTH projections exceed 20 %.<br>• Openings over 25 % (interior bays plus `cfg['diaphragm_openings']`).<br>• H4 out-of-plane and V3 in-plane discontinuity, from the lateral planes of the built model.<br>• H5 from element skew.<br>• V1 from story stiffness, 70/80 % and 60/70 %, with both 12.3.2.2 exceptions.<br>• V2 from the SFRS extent (130 %).<br>• V4 from `cfg['story_strength']` (corpus text "< story above", 65 %).<br>• `cfg['level_groups']` handles split levels.<br>Anything that cannot be evaluated is printed NOT EVALUATED. The report lists the triggered 12.3.3.1/.2/.4/.5, 12.5 and 12.7.3 provisions. | `test_reentrant_*`, `test_interior_opening_*`, `test_discontinuity_*`, `test_irregularity_section_*`, `test_soft_story_*` | Ex20: "H4 None / V3 None" → **H4 YES and V3 YES (Grid C, y = 60 ft, terminates at story 3)**, plus 12.3.3.4 and 12.3.3.5 listed. Ex8 L-plan: re-entrant 43 %/60 % YES, V2 geometric YES. A 10 % notch on a 10×10 plan is no longer Type 2. |
| HR-26 | FIXED | report, design_pipeline (1 line) | `_sfrs()` parses `cfg['system']` per direction (`system_X/Y`, `seis_X/Y['system']`, "SMF in X") and checks it against the built model's brace planes and fixed-ended beam lines. It warns when they disagree. Without a declaration it infers the type and says so. From this it drives:<br>• Ch. 1 and design-basis "Lateral system";<br>• load path;<br>• the LFRS table per direction (R/Cd/Ω0/ρ, Table 12.2-1 height limits);<br>• the horizontal distribution (planes found, per-direction V, base-shear share per plane from the solved reactions, not 50/50);<br>• Ch. 6 brace and column intros;<br>• Ch. 9 detailing lists per system;<br>• Ch. 10 connection types (moment only for tagged MF beams);<br>• design_report.md "system". | `test_system_parsed_*` | Ex8 "two moment frames / two braced frames, 50/50" → **EBF, 3 planes per direction, shares 31/50/19 % and 46/26/30 %**. Ch. 10 Ex8 beams moment → shear and the brace basis is F3.6. Ex24 X 679 kip SMF and Y 1368 kip SCBF (with hr-seismic). |
| HUB-09 | FIXED | report | Grounding requires A358 for SMF/IMF, including dual/mixed and undeclared moment frames. It is not required for STMF/OMF/braced systems. | `test_grounding_requires_a358_*` | "n/a" for dual SMF+BRBF and IMF → required. |
| HR-28 | FIXED | engine3d, report | `_ss_beam_defl` uses Ipack (case-insensitive). The new `beam_serviceability_detail()` lists groups and skipped groups. Any skipped group or build failure returns inf, so the gate FAILS. run() no longer sets `beam_deflection=True` on an exception. The report's deflection table IS the gate's per-group result (roof uses S or Lr), and it states any disagreement with the calc package. | `test_beam_deflection_*`, `test_report_deflection_table_is_the_gate` | Lowercase label: KeyError → silent PASS → now evaluates. Unknown section → FAIL. Ex22 roof "live 0.00 in" → 0.16 in (Lr). |
| HR-37 | FIXED (the conservative part is documented, not changed) | report | Ch. 7/8, the figure and QA now share the gate's routine (`static_lateral`, unfactored gravity) and honour `drift_exempt_stories`. The old report used (1.2+0.2SDS) gravity with no exempt stories. The strength-level Cs floor and CuTa cap are kept (conservative) and stated in the report. | (covered by the RBS/θ tests) | Ex22 report δ/gate now identical by construction. |
| HR-38 | FIXED (in-scope items) | plot_model, engine3d, report | `plot_model.geometry`: the title comes from the model's sections (no KeyError 'col'); base symbols come from `getFixedDOFs` (mixed bases are shown); columns use `col_dir`. Engine: `build(cfg,transf)` is honoured by `add_column`/`add_beam`; `grid(cfg,0)` returns the custom base footprint; a probe failure is warned. Report strings are HTML-escaped. | `test_geometry_figure_without_cfg_col_and_beam`, `test_build_transf_*`, `test_grid_level0_*`, `test_agent_strings_are_escaped` | Ex20 figure: 28 bases all drawn "fixed" → 10 fixed + 18 pinned. |
| HR-32 | FIXED (retrieval-only text untouched) | contract/AGENT_START.md, README_AGENT.md, AISC360_TOC.md, III1_REFERENCE.md, DESIGN_EG_INDEX.md, steel_engine/COMPOSITE_I3.md | Corrections, each checked against the corpus:<br>• ELF permitted for all structures (7-22 12.6), also tall buildings.<br>• 12.3.3.4.<br>• 341-22 F2.3 expected compression.<br>• SCWB for SMF only; A358 for SMF/IMF.<br>• J3.11 / J3.7 / J8 / J9; Ch. C and L numbering; B3.10 ponding (App. 2 removed).<br>• 7-22 2.3.1/2.3.6 combinations; RS scaled to 100 % V.<br>• K = 1 only with DAM.<br>• Ω0 vs E_cl; design_report.md contents; closing-note instruction.<br>New cfg keys are described generically ("see pipeline output"). | n/a | n/a |

**HR-01, report side.** I did not touch `report._seismic` (line 137). The report accepts a per-direction Fx and, when present, reads `E.seismic_design_forces/torsion_summary/seismic_drift/seis_dir` (feature-detected).

### Behaviour changes users will notice

- **RBS moment frames**: design drift is 10 % higher, and saved designs near the limit now FAIL drift (Ex2, Ex22). Declare `cfg['rbs']` for the interpolated value.
- **θ**: about 30–40 % lower (unfactored Px, ÷(1+θ), 0.10 floor). False Ex2 NG removed. Now shown in both directions.
- **Irregularities**: more are found (Ex20 H4/V3, soft story, V2) and fewer false re-entrants (< 20 % notches). The seeded collector and consistency checks key off the corrected flags. Weak story prints NOT EVALUATED unless `cfg['story_strength']` is given.
- **beam_deflection gate**: now FAILS on unknown sections or solver errors instead of passing.
- **New report content**:
  - per-plane base-shear shares;
  - system-specific Ch. 9/10;
  - a geometry figure with mixed bases.
- **Contract**: the agent is no longer told MRSA is mandatory, 1.1RyPn, J3.10, or "drift is in design_report.md".

### Follow-ups

- `pipeline.py` NEXT_STEP still says "C:\...\jobs" (this contradicts AGENT_START). `steltic/contract.py` closing text still asks for "both questions". `consistency.py:482` still says "App. 2 Ponding" (→ B3.10). All are outside my files.
- `hr py` harness cwd (steltic executor), the 1-story ARPACK warnings, and the run/run_one period-gate difference: hr-seismic replaced the gates; the rest are not mine.
- The report θ and gate θ should share one engine function after the merge (see above).
- The diaphragm-stiffness change of H3 (more than 50 %) is NOT EVALUATED, because the diaphragm is modelled rigid. Weak story needs capacities that the framework does not compute.
- `viewer3d.py` single ELF set (hr-seismic follow-up).

## Workstream `hub`

### Status

| ID | Status | Files | What changed | Tests | Before → after |
|---|---|---|---|---|---|
| HUB-01 | FIXED | `steltic/steltic/main.py`, `steltic_cfs/steltic/main.py`, `steltic_hub/runners.py`, `steltic_hub/main.py`, `catalog/{steltic_admin/admin,steltic_variations/variations,steltic_probabilistic/probabilistic}/local_guard.py` + their `main.py` | **Modules.** A new `_local_only` middleware does three things. It requires a loopback Host on every request; `STELTIC_ALLOWED_HOSTS` adds names. It refuses a state-changing request that has a non-loopback or `null` Origin, or `Sec-Fetch-Site: cross-site`. If a request sends `X-Steltic-Hub-Token`, the value must match `STELTIC_HUB_TOKEN`. CFS applies the guard only when DEMO=0. **Hub.** It creates one random token per launch and passes it in the env of each server it starts. It sends the token on the credential push, health check, http runs, Stop and the `/m/` proxy. The proxy replaces any token header the page sent. The same guard was added to the bundled Admin, Variations and Probabilistic servers. | `steltic/tests/test_local_only_guard.py` (5), `steltic_cfs/tests/test_local_only_guard.py` (5), `steltic_hub/tests/test_review_fixes_hub.py::test_bundled_module_servers_carry_the_same_local_guard`, `::test_local_guard_refuses_cross_site_and_rebinding_and_checks_the_token`, `::test_the_hub_hands_each_server_a_token_and_sends_it_back` (a real server process), `::test_the_proxy_sends_the_hubs_token_never_the_browsers` | **HR**, pristine: a text/plain `POST /api/creds` with `Origin: https://evil.example` returned **200** and changed base_url. `GET /api/me` with `Host: attacker.example` returned **200**. Now both return **403**. **CFS** (listed as UNVERIFIED in the register) behaves the same on pristine: 200 before, so it was exploitable. Now **403**. |
| HUB-04 | FIXED | `steltic_hub/runners.py` (stage_inputs), `manifest.py` (validation), `catalog/steltic_nonlinear.json`, `jobs.py` and `main.py` (listing order), README | **Record.** Folder and zip staging writes `.hub_staged.json` with a content signature of each package file (sha256, or size+CRC for zips). **Re-stage.** Files the new package dropped are moved aside. When a `design_files` file differs from the package *staged last time* (not from the project copy), the stage entry's `supersede` outputs and the earlier design's files move aside together. An edited project copy is moved aside before a changed package file replaces it. Everything goes to `_superseded/<YYYYmmdd-HHMMSS>/`; nothing is deleted, and each move is logged. **Projects with no record** move differing files aside once and log a warning; their outputs are not moved. **Nonlinear manifest:** every design-package stage declares `design_files` and `supersede`. | `test_a_new_design_package_supersedes_the_old_one_and_its_analyses`, `test_an_edited_project_copy_is_kept_before_a_new_package_replaces_it`, `test_what_the_module_adds_to_a_package_file_is_not_a_new_design`, `test_a_project_staged_before_the_record_existed_is_handled`, `test_zip_packages_are_tracked_the_same_way`, `test_stage_globs_are_validated`, `test_every_nonlinear_design_package_stage_declares_what_it_supersedes` | **Synthetic repro** (review harness): pristine leaves `design/old_only.json` and `pushover/po.json` next to the new cfg. Now they are in `_superseded/<t>/` with the old cfg.py. **Real data:** copies of saved SNL job Ex22 and HR Ex22 in the review harness. Pristine overlays 127 files and keeps all SNL results. The fix's first stage finds no record, moves aside SNL's extended `design/calc_package.json` and warns. A re-stage after SNL edits calc_package moves nothing. A simulated HR redesign moves the 151 MB of results (pushover, nlrha, ddm, hinge params, review…) plus the old cfg and calc_package into one snapshot. |
| HUB-05 | FIXED | `steltic_hub/runners.py` (ServerSupervisor), `main.py` (RUNS passed in) | If a dependency becomes available while a hub run is using the server, the restart is deferred and logged. A run counts as using the server if it is the module's own run, an http run of a module whose server `requires` it, or a CLI run whose command or env names `{server.<id>}`. The run that asked for the server does not count. The first `ensure()` after the server goes idle restarts it. | `test_a_dependency_becoming_available_waits_for_the_run_to_finish` | Before: `stop()` was called right away, with no check of RUNS. After: no stop while `hr.design` or `loop.design` is live. The stop happens once those runs are idle. |
| HUB-06 | FIXED | `steltic_hub/runners.py` (field_values, stage_inputs) | An absolute file-field path is accepted only if it equals the field's expanded default or lies under `config.DATA`. Outside those, staged fields are refused. A field that is not staged may still name one existing **file**, never a folder. This keeps Admin's standards queue working: it converts PDFs from a folder the user picked. A stage whose `to` lies inside its `from` folder is refused; before, the copy recursed. | `test_absolute_file_fields_are_confined`, `test_a_folder_is_never_staged_into_itself` | Before: `package=/` or `package=$HOME` was accepted and its whole tree copied into the project. After: RunError "is outside the project and the hub's data folder". |

Out of scope and untouched: HUB-02, HUB-03, HUB-07, HUB-08 (RAG/grounding), HUB-09 (another agent), `job_tools.py`, `rag_server.py`.

Test results:
- hub: 127 passed, 1 skipped (was 113/1; 14 new)
- steltic: 23 passed (was 18)
- steltic_cfs: 22 passed (was 17)

### Behaviour changes users will notice

- **Module servers refuse non-loopback requests.** HR, CFS (local), Admin, Variations and Probabilistic return 403 for any request whose Host is not loopback, and for cross-site state-changing requests. Reaching a server by LAN IP or hostname now needs `STELTIC_ALLOWED_HOSTS`. CFS DEMO=1 (Cloud Run) is unchanged.
- **New folders in Nonlinear projects.** `_superseded/<time>/` folders appear when HR Steel's design changes. After a redesign, the earlier pushover, NLRHA, DDM, Collect (hinge_params_collected.json), review and hazard results move there. The Run gate then asks for Collect again, which is correct for new sections. The run log says what moved and where.
- **Projects staged by the old hub** get a one-time `_superseded` copy of any differing package files. They also get a warning, not a supersede, when design files differ.
- **Package copies are overwritten as before.** A re-stage with an unchanged package still overwrites module-extended package files with the package version, as it always did (for example SNL's `ddm_analysis` key in design/calc_package.json).
- **Absolute paths outside the data folder are refused for the design package** (and any staged field). Admin `@file:/elsewhere/pkg.zip` for a package now fails; uploads and `{out.steltic}` are unaffected.
- **Deferred restart is visible in the server log.** Restarting HR Steel when the standards server appears now waits for running designs. Until then it keeps running without the new dependency, and the server log says "restart … deferred".

### Follow-ups

- **The token is a positive credential, not mandatory.** A request with no token and no browser headers (curl, another module's server) is still accepted. That is because the snl loop_server and variations call HR's `/api/run` without it. A strict mode needs snl's HR client to send `X-Steltic-Hub-Token` (steltic_nonlinear is not in my worktrees). The browser-side protection does not depend on the token.
- **HUB-05 only sees hub-started runs.** A design started from a module's own embedded page is invisible to the hub. A module "busy" endpoint would close this.
- **HUB-04 does not check for a live run on the same project.** If HR's design changed and two Nonlinear tabs start at once on the same project, the second stage could move outputs from under the first run.
- **The supersede list is hand-written.** The `supersede` list in steltic_nonlinear.json comes from SNL's output names. It should be kept in step with SNL, or SNL could declare its outputs itself.
- **rag_server has no Host check** (part of HUB-01's original note). It is out of scope under the RAG deferral.

## After integration: remaining follow-ups and PARTIAL items

**Status PARTIAL**
- **HR-07 (hr-demands): Ecl is not formed for these cases.** Each is listed NOT EVALUATED where detected.
  - SPSW VBE/HBE tension-field flexure and HBE hinge moments;
  - STMF E4.3;
  - multi-tier F2.3(c);
  - EBF links adjacent to columns without `cfg['ebf_links']`.
- **HR-07: BRB defaults are flagged ASSUMED.** These are β/ω/Fysc/Ry. They need supplier values in `cfg['brb']`.
- **HR-39 (hr-geomwind):** point, line and crane gravity loads in `static_model` are not done.
- **HR-40 (hr-gates):**
  - hand `engine3d.SEC` values (W14X605, W40X199, W36X194, W14X370), the HSS hand table / "H14", the `E.HSS.get` CSV bypass (design_pipeline), `sections._HSS_R` (A1085 b/t);
  - the `Lb_in` seed 0.095 vs 341-22 D1.2b 0.086;
  - the ρ default of 1.3 regardless of SDC in `seis_dir` / `combos` / `drift_allowable` (preflight only warns).

**Analysis scope not done**
- **Wind:**
  - Fig. 27.3-8 Cases 2–4 (torsional MWFRS);
  - C&C uplift for roof members and anchorage (MWFRS roof suction is in 0.9D+1.0W);
  - monoslope, hip and mansard roofs;
  - automatic `wind.n1 = 1/T` from the modal run (Ex1, Ex8, Ex9 and Ex22 now print WIND FLEXIBLE with the approximate na);
  - `skew` in wind strips;
  - auto-detection of several adjacent omitted columns.
- **Collectors and transfer:**
  - collector 12.10.2.1(b)/(c) are only in the seeded slot;
  - 12.3.3.4 Ω0 for elements that support discontinuous frames is not auto-applied (Ex20 transfer girder);
  - lower-roof snow (pf > 45) is not in W.
- **Stability:** τb < 1 (C2.3(b)) is not modelled; P-δ is left to B1.
- **Report and seismic:**
  - Report per-case force tables (Ch. 6 appendix) still use the nominal-stiffness `run_combo`.
  - The joint-fixity figure keys split spans by (i,j,k,dir), so the last piece wins.
  - Report Ch. 3 headline still shows the governing direction only. Per-direction V is in the LFRS and distribution tables and the R/Cd/Ω0 row.
  - ELF drift does not take the 12.8.6.1/12.8.6.2 relaxations (conservative).
  - Mixed buildings should declare `system_X`/`system_Y`. With only a building-wide "SMF + SCBF" string, `_mf_only` falls back to factor matching (hr-gates).
  - preflight could reuse `E.SFRS_TABLE` / `E.seis_warnings`.
  - H3 diaphragm stiffness is NOT EVALUATED (rigid model). Weak story needs `cfg['story_strength']`.
  - HR-16 MF share counts every non-brace element. HR-34 has no unit test.

**Hub**
- The hub token is a positive credential, not mandatory. The SNL HR client must send `X-Steltic-Hub-Token` before it can be made strict.
- HUB-05 sees only hub-started runs.
- HUB-04 has no live-run lock, and its supersede list is hand-written.
- `rag_server` has no Host check (RAG is deferred).
- The `steltic_hub` and `steltic_cfs` commits of fix/hub must be merged in their own repos.

**Merger to-do**
- Copy the shared engine files to `steltic_cfs`: `engine3d.py`, `design_pipeline.py`, `static_model.py`, `design_post.py`, `example_build.py`, `plot_model.py`, `frame_diagram.py`, `viewer3d.py`, and `viewer3d_template.html`, which `viewer3d.py` now relies on for the per-direction arrows.
- Then rerun the CFS suite. Note that `static_model` now calls `eng.clad_edge_heights` / `_resolved_plate_sig` and `eng.seis_dir` / `system_dir`.
- `report.py` is shared too (hr-geomwind's note).

## Independent verification

| ID | Verdict | Evidence (hand calc vs engine) |
|---|---|---|
| HR-01 per-direction period | CORRECT | 2×2-bay, 3-storey SMF (h1.py): mode 1 is Y and mode 2 is X. T_X = 0.6708 s and T_Y = 0.6735 s, both below Cu·Ta = 0.750 s. Cs_X = 0.6/(0.6708·8) = 0.11180 and Cs_Y = 0.11136, both exactly as the engine. V_X = 85.96 kip and V_Y = 85.62 kip, with W = 768.9 kip by hand. B05 L-plan: V_X 311 kip (T 0.607 s) and V_Y 349 kip (T 0.543 s) give the same W, so they are consistent. |
| HR-02 per-direction R/Cd/Ω0/ρ | CORRECT | `SFRS_TABLE` matches every row of Table 12.2-1 and Table 12.8-2 that I checked (EBF, SCBF, OCBF, BRBF, SPSW, SMF, STMF, IMF, OMF, D.1/D.2/D.13/D.14, E.1). The 30 % companion uses the other direction's forces and factor (tested). Drift uses each direction's own Cd. Δa/ρ applies only to a moment-frame-only direction in SDC D–F. The ρ default of 1.3 is conservative (HR-40 follow-up). |
| HR-03 static loads | CORRECT | Same frame, with Lr = 20 psf, L_by_level {2: 125} and clad 15 (h2.py), solved base reactions vs hand: D 768.9 = W; L 525.0 = 50·3000 + 125·3000 psf·ft²; Lr 60.0; companion LiveFactor 0.5/1.0 gives 450.0. The gravity audit equals W at every level on B05, B14, B17, Ex16 and Ex30. |
| HR-04 solved beam shear | CORRECT | 30×25-ft pinned bay, 1.0D (h3.py). Two-way: V = 8.138 kip and 6.000 kip, equal to the hand values (trapezoid/triangle area × 60 psf + cladding)/2. One-way girder floor: V = 12.825 kip and 12.563 kip, equal to the half-bay hand values. Minor (pre-existing, not changed): each sub-element carries its trapezoid as a segment-averaged uniform load, so the two-way midspan M is 75.09 vs 76.66 k-ft exact (−2 %) at nseg = 6. |
| HR-05 combinations | CORRECT | Checked against ASCE7.md:5049-5060 and 5112 (h4.py, with Lr 20, S 30, R 10 and wind). Present: 1.4D; 2a with 0.5Lr, 0.3S and 0.5R; 3a with 1.6Lr, 1.0S and 1.6R plus L (Exception 1 per level); 3a with 0.5W and no L; 4a 1.2D+1.0W+L+(0.5Lr\|0.3S\|0.5R), keeping only the non-dominated branches; 5a 0.9D+1.0W with Fig. 27.3-1 roof suction and +GCpi; 6 (1.2+0.2SDS)D+ρE+L+0.15S; 7 (0.9−0.2SDS)D+ρE. |
| HR-06 Ω0 for all SFRS columns | CORRECT | Axial-only `[col]` cases run for any R > 3, in both signs, with the orthogonal 0.3 companion and each direction's own Ω0. This satisfies D1.4a(b). CONCERN (not changed): Ω0 cases carry no accidental torsion. 12.8.4.2.1 requires accidental torsion in the "analysis and design" of Type 1 structures in SDC C–F, so corner-column Ω0 axial is somewhat low for those buildings. |
| HR-07 Ecl | CORRECT with two defects, both CORRECTED here | Formulas confirmed: T = RyFyAg; C = min(RyFyAg, FneAg/0.877) with RyFy; post-buckling 0.3C; OCBF min(Ω0·QE, RyFyAg) / 0.3Pn; BRBF ωRyPysc / βωRyPysc; EBF 1.25RyVn with Vn = min(0.6FyAlw, 2Mp/e); Ry 1.3 for A500 Gr C, 1.1 for A992, 1.6 for A53, 1.5 for A36. Chevron unbalanced load on HSS6X6X1/2 (h5.py): case (a) M = 22,914.5 k-in vs (T−C)sinθ·L/4 = 22,914.5; case (b) 32,996.5 = 32,996.5; corner-column Y-brace vertical 292.1 kip vs T sinθ = 292.2. **Defect 1:** with the diaphragm restrained, braced-bay struts were slaved to it and showed about 0 Ecl axial (Ex30 W24X84 struts: 1.5–4 kip), and nothing said so. **Defect 2:** C used the work-point length, which F2.3 forbids ("shall not exceed brace end to brace end"). For crossing X-diagonals (Ex30 HSS5X5X1/2, L 513 in) C was about 4× low. Both are fixed below. |
| HR-08 Ax | CORRECT | TIR is Δmax/Δavg at the extreme edges with ±accidental torsion and Ax = 1. Ax = (δmax/1.2δavg)² from level displacements, limited to 1–3, applied where Type 1 exists in SDC C–F. Mta = ±0.05·B·Ax·ρFx, with B taken perpendicular to the forces. B14: TIR_Y 1.353 gives Ax 1.23–1.25 (by hand (1.34/1.2)² ≈ 1.25). |
| HR-09 edge drift | CORRECT | Edge drift uses vertically aligned extreme points (bottom point projected from the top level), with accidental torsion × Ax. B14 Y: edge 0.00518 vs CM 0.00369. Ex16 drift_Y 0.0109 vs limit 0.0115, as in the fix notes. |
| HR-10 wind | CORRECT | Kz = 2.41(z/zg)^(2/α) with 7-22 α/zg, and Table 26.11-1 values, both match the corpus. B13, Exposure D, V = 160 mph (h6.py), independent numeric ∫Kz: X levels 71.08 / 75.23 / 39.08 kip and Y 133.52 / 140.42 / 72.66 kip, identical to the engine. Cp_lw −0.367 at L/B = 1.67. The 16-psf minimum is checked per level. The 0–6.5 ft band (35.35 kip) goes to the foundation. Gf equations match 26.11-6…16. Minor: the static model's roof uplift uses G = 0.85 even where Gf applies. |
| HR-14 completion gate | CORRECT, one hole CORRECTED here | The gate reads drift, θ, model, model_complete and beam_deflection from the sanity results, plus consistency results matched by hash. **Hole:** a `pipeline_results.json` with no `cfg_sha256` (the pipeline ran before cfg.py existed) passed the staleness check. Fixed below. |
| HR-18 DAM | CORRECT | Checked against 360-22 C2.2b and C2.3. 0.8 on all stiffnesses. τb = 1 with the C2.3(c) 0.001αYi in all combinations, not subject to (d). Ni = 0.002αYi in gravity-only combinations in 4 directions. In lateral combinations Ni is additive when the 2nd/1st-order drift ratio exceeds 1.7, in the direction of the lateral resultant. Gravity-only base shear = 0.003W (tested). Archetype drift ratios were 1.01–1.03. |
| HR-22 RBS | CORRECT | 1 + 0.1·(2c/bbf)/0.5, which gives 1.1 at a 50 % flange reduction (358-22 5.7 Step 1, interpolated). It is applied once, to the elastic drift that feeds the gate, θ and the report. |
| HR-24 θ | CORRECT | Px = D + 0.5·(0.4L0, or 0.8L0 where L0 > 100 psf), unfactored, roof live excluded. θ = PxΔxe/(Vx·hsx), divided by (1+θ). θmax = 0.5/(βCd) ≤ 0.25, never below 0.10, with β ≥ 1.25/Ω0. Hand recomputation (h7.py), story 1 X: Px 948.9 kip, Vx 85.96 kip, θ 0.01674, identical to the engine. θmax: 0.5/5.5 = 0.091, raised to 0.10. |
| HUB-01 | CORRECT | Every request is checked for a loopback Host, which closes DNS rebinding. State-changing requests with a non-loopback or `null` Origin, or with Sec-Fetch-Site: cross-site, are refused. The hub token is compared in constant time. Every GET route is read-only. 5 tests pass. |

### Fixes made during verification

| Commit | Change |
|---|---|
| `1df4342` | **Fix HR-07 (verify):** `ecl_plan` lists as NOT EVALUATED the Ecl axial of beams that are slaved to the rigid diaphragm and lie in the plane of a yielding brace or link (F2.3 / F3.3 / F4.3 "struts", "beams outside links"). The static model records its slave set. Test: `test_ecl_strut_axial_on_a_slaved_braced_bay_beam_is_reported_not_evaluated`, which shows axial < 1 % of the horizontal component of RyFyAg. The chevron test now expects exactly this one item. |
| `2b3e4e7` | **Fix HR-14 (verify):** sanity results with no cfg hash count as stale when cfg.py exists. Regression added to `test_gate_stale_and_consistency_issues`. |
| `4193395` | **Fix HR-07 (verify):** SCBF expected compression length per F2.3. Order of precedence: `cfg['brace_length']` (in), then `cfg['brace_length_factor']`, then half the work-point length for crossing X-diagonals, else the work-point length with a loud "C UNDERESTIMATED" note. The brace-connection slots use the same rule. Test: `test_scbf_expected_compression_length_never_exceeds_the_work_point_length_rules`, against an E3 hand calc. |
| `753833d` | Contract text (`AGENT_START.md`) for the new keys and the new NOT EVALUATED item. |

### Open concerns

1. **Ω0 cases without accidental torsion** (HR-06/08). Type 1 buildings in SDC C–F under-state corner-column Ω0 axial. The code is arguably explicit (12.8.4.2.1), so consider acc = ±1 in Ω0 cases when `torsion_summary()['amplify']` is set.
2. **Work-point brace length for non-crossing braces.** It is now flagged, but the end-to-end length must come from the designer.
3. **Two-way trapezoid loads averaged per sub-element** (−2 % midspan M at nseg = 6). Pre-existing.
4. **Roof uplift uses G = 0.85, not Gf**, for flexible buildings.
5. **Default `drift_limit` is 0.020 regardless of Ie / Risk Category.** Preflight territory. B08 has Ie = 1.5 and `drift_limit` = 0.015, which looks wrong for Risk Category IV (0.010).
6. **Weak HR-17 tests.** `test_base_reactions_enveloped_with_roof_uplift` only asserts values > 0.

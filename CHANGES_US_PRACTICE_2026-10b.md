# US-practice fixes, October 2026 (second round) — engine

These changes follow the October 2026 review of the published examples against US structural steel practice
(AISC 360-22, AISC 341-22, AISC 358-22, ASCE 7-22). They change demands, so designs made before them must be
re-run. Tests: `PYTHONPATH=$PWD:$PWD/steel_engine python -m pytest tests -q` (157 passed; new tests in
`tests/test_us_practice_2026_10b.py`).

## Seismic

- **Omega0 column cases only where AISC 341 applies.** `design_pipeline.combos()` generated the Omega0 `[col]`
  cases for every braced frame, including R = 3 "steel systems not specifically detailed for seismic
  resistance" (ASCE 7-22 Table 12.2-1 row H), which are designed to AISC 360 alone. ASCE 7-22 12.4.3 applies
  overstrength only where a provision requires it. The cases are now generated per direction only where
  AISC 341 governs (R > 3, or a cantilever-column system): `aisc341_applies(cfg, d)`,
  `omega0_column_dirs(cfg)`. `cfg['omega0_columns'] = True` forces them (e.g. 12.3.3.4 supports of
  discontinuous frames). On the R = 3 example (Ex18) the braced-bay base compression drops 39 % and the
  uplift 72 % — the old values were the Omega0 envelope.
- **rho default by SDC.** When `cfg['rho']` is not declared, rho = 1.0 in SDC A–C (12.3.4.1) and 1.3 in
  SDC D–F (12.3.4.2), instead of 1.3 everywhere.
- **Soft-story screen.** Story stiffness is now V / Delta (drift ratio x h), not V / drift ratio, which
  inflated K1/K2 for tall first stories (unconservative screen).
- `static_model.brace_system()` returns no AISC 341 system for R <= 3 frames.

## Wind

- **Fig. 27.3-8 Cases 2–4** are added to combinations 4a and 5a (0.75 / 0.563 factors, torsion
  e = 0.15 B at the diaphragm master, roof suction scaled alike). `cfg['wind']['load_cases']` can limit
  the set where Appendix D permits.
- **n1 from the modal analysis.** For buildings that are not low-rise, the gust-effect factor now uses
  n1 = 1/T of the model's dominant mode in the wind direction (26.11.3 rational analysis) before the
  approximate na, so flexible buildings get Gf from their own period.
- **Roof uplift** in 0.9D + 1.0W uses the MWFRS gust factor (Gf when flexible), not G = 0.85.

## Gravity and serviceability

- **ASCE 7-22 4.7 live load reduction**, `cfg['live_load_reduction'] = {'unreduced_psf': 15}`. Applied per
  member by superposition: the factored reducible live-load effect x (1 − RF) is removed from each
  combination. Columns: axial, AT = the sum of the tributary areas supported (from a unit-load solve),
  0.40 / 0.50 limits by the number of floors supported. Simple single-span beams: moment and shear.
  Base reactions under reduced columns follow. Not reduced: moment-frame and chevron beams, the declared
  unreduced allowance (the 4.3.2 partition load), roofs, Lo > 100 psf, garages, assembly. The factor and
  AT are written to each member's inputs.
- **Composite floor deflection basis.** `cfg['composite_floor'] = {'I_eff': {section: in4},
  'precomposite_psf': psf, 'camber_in': {section: in}, 'roof': bool}` makes the beam-deflection gate use
  the unshored composite basis: precomposite dead load on the bare steel less camber, superimposed dead
  and live load on the lower-bound I (AISC 360-22 Commentary I3.2).
- **preflight** warns when a partition allowance is carried but no partition weight is in W
  (12.7.2 item 2; `cfg['seismic_mass_only']`).

## Calc package and connection slots

- Beam connection slots and `connection_demands.csv`: **simple shear** (M = 0) when the model releases both
  ends; moment-connected beams get the **end-moment** envelope (new `Mend`), never the mid-span moment;
  beam axial / collector forces are carried into the slot.
- Brace, splice and base slots cite AISC 341 only where it applies (R = 3: analysis forces, AISC 360).
- Brace members carry `Lc_in` (buckling length: half the work-point length for X-diagonals connected at the
  crossing) and `Lc_basis`.
- `calc_package['demand_analysis']` adds `base_reactions` (all-combination envelope per base node with the
  governing combinations), `live_load_reduction`, `omega0_columns` and `aisc341_applies`.

## Bug fix

- **P-Delta silently lost after a modal solve.** The static model replays the agent's `custom_build`;
  the column transform type came from the last `engine3d.build()` call, so any Linear (modal) build in
  between turned the second-order static model first-order. The replay now sets the transform it needs.
  Found while adding the modal n1 (the DAM drift ratio fell to exactly 1.00).

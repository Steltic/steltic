# OpenSees Steel Building Design Agent — Guide & Workflow

**Audience: the LLM agent.** This file tells you what you have, and the exact order
in which to use it, to design **one** steel building well.

---

## 1. Mission
You are given **one building to design** (or analyze): a geometry, an occupancy, a
site (seismic + wind), and loads. Your job is to produce a sound, code-grounded
3D structural model and the member/limit-state checks behind it. You do this by
(a) standing on a **retrieved, validated near-neighbour model** instead of starting
from a blank file, and (b) **grounding every code check in the specification RAG**
rather than recalled code values.

Two hard rules:
- **Ground code checks in the RAG.** Apply provisions/equations exactly as returned
  from the AISC 360 spec collection (and AISC 341 / AISI when applicable). Cite the
  section and equation numbers. Do not invent code numbers from memory.
- **Loads (ASCE 7) are computed, not retrieved.** ASCE 7 is not in the RAG
  (copyright). Compute wind/seismic with the building engine's load routines and
  spot-check them; the spec RAGs cover *member* design, not *loads*.

---

## 2. Your knowledge base (RAG collections)

Query with the `engineering-rag` tool (`search_engineering_standards(query, collection, top_k)`).
Embeddings are Nomic; phrase queries naturally (the retriever was built with a
`search_query:`/`search_document:` convention — just write a clear sentence).

| Collection | Contains | Use it for |
|---|---|---|
| **`engineering_standards_A360`** | AISC 360-22 *Specification* (normative provisions + equations) | **Primary grounding** for every steel member/connection limit state |
| `engineering_standards_A341` | AISC 341 *Seismic Provisions* | Seismic detailing/system requirements (SMF/SCBF/EBF/BRBF/dual), R-system rules |
| `engineering_standards_AS100` (+ AS400/AS240/AS310/…) | AISI cold-formed steel | Only if cold-formed members are involved |
| `engineering_standards_A358` | AISC 358 prequalified moment connections | Moment-connection detailing |
| `engineering_standards_A303` | AISC 303 Code of Standard Practice | Tolerances (e.g. out-of-plumb 1/500 used in stability) |
| **`steel_design_examples`** | AISC Design Examples (worked problems + answers) | Worked numeric examples & answer keys to mirror your calc |
| **AISC Q&A database** (`aisc_qa_database/`, ingest as needed) | Our original Q&A solutions grounded in A360 | Extra worked, spec-grounded examples per chapter |
| **`opensees_buildings_3d`** ← THIS LIBRARY | 40 validated 3D steel building models (MF/CBF/dual/EBF/BRBF/SPSW/podium/…; 1–40 storeys; SDC B–E; wind; irregularities) | **Retrieve the nearest whole-building model** to start from |
| `opensees_examples` | 22 Ziemian 2D benchmark frames, validated vs published | 2D frame-level second-order behaviour + the proven modelling recipe |
| `opensees_building_templates` | 2D parametric MF/CBF covering set + the AISC III-1 flagship | Parametric lateral-frame methodology, ASCE 7 ELF/RS/wind worked at building scale |
| `openseespy_documentation`, `opensees_documentation` | OpenSeesPy/OpenSees command reference | Correct API for `rigidDiaphragm`, `mass`, `eigen`, `responseSpectrumAnalysis`, transforms, elements |
| `bgscm16_steel_textbook`, `structural_analysis`, `statics_textbook`, `mechanics`, `materials` | Textbooks | Background theory when a provision/behaviour is unclear |

---

## 3. The design workflow (the pipeline does the mechanics; you do the AISC checks)

**Phase 0 - Scope (no RAG).** Pin storeys & heights; plan (bays x spacing, regular/irregular); Risk
Category (-> Ie); lateral system (MF/CBF/dual); base fixity; site seismic (Ss, S1, Site Class -> SDS,
SD1, SDC, R) and wind (V, Exposure). These drive the `cfg` and every RAG query.

**Phase 1 - Build the cfg + your builder.** Compose a `cfg` dict (Section 5 schema), using the engine's
ready-made archetypes `engine3d.CFG` (B02..B40) as templates for realistic geometry/sections, and write
`cfg["custom_build"] = f` to build the model -- read `example_build.py` for a complete worked reference AND retrieve
at least one similar validated building from the examples RAG (`collection="opensees_buildings_3d"`, or `opensees_examples`;
if unavailable, proceed from `example_build.py`). Copy the structure (columns, girders in BOTH directions on every level,
varied sections by group, rigid vs pinned joints via `add_beam(..., releases=...)`). Set each column's `strong_dir` from
YOUR frame layout -- NOT example_build.py's placeholder -- and confirm strong-axis-in-plane in your RESOLVED FRAMING
orientation check. Any AISC W-shape name resolves automatically (no need to "extend" a section table).

**Phase 2 - Run the pipeline (ONE call).** `pipeline.design_and_report(name, cfg)` builds the model,
computes the ASCE 7-22 loads, assembles the LRFD combinations, runs each through P-Delta, envelopes the
per-member DEMANDS, draws the figures, checks drift (delta = Cd*delta_e/Ie, ASCE 7-22 Sec.12.8.6) and
serviceability, and writes the report scaffold. It computes **NO AISC capacity** -- that is your job.

**Phase 3 - Ground every member & connection in the AISC RAG (this is the real work).** For each
governing limit state, query `engineering_standards_A360` (+ `engineering_standards_A341` for seismic),
apply the cited equation to the demands, compute the capacity and D/C, and write
`limit_state`/`cited`/`capacity`/`DC` into `calc_package.json`. Limit-state -> query:
- Tension -> *"tensile yielding rupture D2 net area D3"*
- Compression/columns -> *"flexural buckling E3 compressive strength"*
- Beams/flexure -> *"flexural strength F2 lateral-torsional buckling"* (compact vs noncompact flange -> F2 vs F3; weak-axis F6; HSS F7/F8)
- Beam-columns -> *"combined axial and flexure interaction H1-1"*; Shear -> *"shear strength G2 web"*
- Braces -> A360 E3 + **A341** seismic detailing; Connections -> A360 Ch. J (*"bolt shear J3"*, *"fillet weld J2"*, *"block shear J4"*); prequalified moment connections -> **A358**
- Also derive the App.8 **B2** amplifier and the AISC 341 **SCWB / Omega0** column check. Cite the version (AISC 360-22).

**Phase 4 - Resize, reconcile, finish.** If any D/C > 1.0 or SCWB < 1.0, change the section in the cfg,
re-run the pipeline for fresh demands, and re-derive. When all D/C <= 1.0 and drift/serviceability pass,
run `consistency.check(name)`, reconcile every flag, re-render with `report.build_report(name)`, and end
by OFFERING an optimisation pass.

---

## 5. Generating a new building with the engine

`engine/engine3d.py` builds, analyses, and checks any building from one `cfg` dict.
The registry `CFG["B01"..."B40"]` are worked examples of the schema. To make a new
one: define a `cfg` and call `run_one(cfg_name)` after adding it to `CFG`, or call the
primitives (`modal`, `elf`, `static_lateral`, `rs_baseshear`) directly.

`cfg` keys (units kip, inch):
- `arch` (label), `NX`,`NY` (bays), `SX`,`SY` (bay spacing in.), `heights` (list of
  storey heights in.), `base` (`"fixed"`/`"pinned"`).
- `model` (**required**): the structural scheme `{'bases':'fixed'|'pinned'|'mixed',
  'joints':'rigid'|'pinned'|'mixed', 'gravity':'framed'|'leaning'}`. A **HARD GATE** (sanity checks
  `model_declared` / `model_consistent`) fails the run if it is missing or if the build doesn't implement
  it — `gravity:'leaning'` needs `lean_gravity`/custom_build, `bases:'mixed'` needs a custom_build,
  `joints:'pinned'/'mixed'` needs `releases`.
- `releases` (optional): a function `f(i,j,k,dirn)->(relz,rely)` with each of `relz`,`rely` in
  `{'none','I','J','both'}` -> releases the strong-axis (Mz) / weak-axis (My) end moment at the I
  end, J end, or both, for that beam. This is a NATIVE elasticBeamColumn release (no extra nodes),
  so per-member pinned/shear connections flow through the demand pipeline unchanged. Use it for
  non-rigid joints instead of a custom_build.
- `col`,`beam` (AISC shape keys in `SEC`), `brace` (HSS key in `HSS`): these set **ONE** column and **ONE**
  beam section for the WHOLE default model — OK for a first sizing pass, but a **real building varies its
  sections** (lighter columns up the height in level GROUPS, exterior vs interior columns by demand, roof vs
  floor beams). To vary sections — and to model an SMF/CBF lateral system — write a `custom_build` and build
  each member with the orientation-safe helpers `engine3d.add_column(tag,n1,n2,sec,strong_dir)` /
  `add_beam(tag,n1,n2,sec,releases=...)` (they auto-register the correct transforms — no `register_col_transf` or hand-written `geomTransf` needed). The optimisation
  pass also produces a varied, grouped section schedule.
- `braces` (a layout function or `None`), `plan` (a footprint function or `None`).
- `seis = seis(SDS, SD1, S1, R, Ct, x, Cu, Ie)`; `analyses=["ELF","RS"]`.
- Optional: `governing="wind"` + `wind={V,exposure,...}`; `snow` (psf);
  `extra_mass_floors={level:psf}`; `skew` (parallelogram), `xcoords`/`ycoords`
  (non-uniform grid); `torsion_check`, `dual_check`, `softstorey_check`,
  `drift_limit`; `D_floor`,`D_roof`,`clad`,`L_floor`.

Ready-made layout/plan helpers: `perim_braces`, `core_braces` (wide braced core),
`core1` (single-bay core, for dual), `tors3` (eccentric→torsion), `weak1` (open
ground storey), `podium2` (braced podium only), `ydir` (braces in Y only), `offset8`
(offset over height), `stepcore` (core through setbacks); plans `Lplan`, `setback`,
`step12`, `openlobby` (transfer), `doughnut` (atrium).

Lateral systems available: space MF, perimeter/located CBF, braced core (tube), dual
(MF+core with 25% check), mixed-per-direction, podium, and elastic idealizations of
EBF/BRBF/steel-plate-shear-wall-core.

---

## 6. What "validated" means (the sanity-check suite)
A model is `validated` when ALL pass: equilibrium (ΣR = Σapplied, both dirs);
stability (lowest eigenvalue > 0); period (T1 within 0.5×–3× of ASCE 7 Ta — bare
centreline models run flexible); cumulative effective modal mass ≥ 90% each
direction; max interstorey drift < the ASCE 7 limit; ELF base shear recovered and
RS ≥ 0.85·ELF; plus torsion < 1.5 and dual ≥ 25% MF where applicable.

---

## 7. Caveats — state these in any output
- **Elastic analysis only.** No yielding/buckling/inelastic links. EBF/BRBF/SPSW are
  *elastic idealizations* here; their inelastic detailing (links, plates) is governed
  by AISC 341 and is a later tier.
- **Sections are ASSUMED, not designed** in the library models — they were sized so
  drift/period are realistic. Phase 6 is where you actually *check* members against
  A360; resize as the checks require.
- **Bare-centreline models run flexible** (no panel zones / composite slab /
  nonstructural stiffness) → longer periods; that's expected.
- **Loads = ASCE 7, computed not retrieved**; spot-check them.
- **Always cite** the A360-22 section/equation behind each check, and note any
  governing seismic-detailing requirement from A341.

---

## 8. Deliverables — what to output for the building

Produce a **design package** (write to the building's working folder). Minimum set:

1. **Design basis sheet** — codes/versions (AISC 360-22, AISC 341-22 if seismic,
   ASCE 7-22), Risk Category & Ie, SDC, R, ρ, site (SDS, SD1, S1), wind (V, Exposure),
   gravity loads (D, L, Lr/S), drift limits used, units.
2. **Model summary** — geometry (storeys, heights, grid, plan), lateral system per
   direction, base fixity, diaphragm, member sections, seismic weights per floor.
3. **Analysis results** — modal periods & mode shapes, effective modal mass, ELF and
   RS base shears (each direction), storey shear & drift profiles, P-Δ amplifiers
   (B₂/θ), and the **sanity-check suite results** (all pass).
4. **Load-combination set actually used** (Section 9) and which combo governs where.
5. **Member design schedule** (Section 10a) — every member, section, governing combo,
   design demands (P, Mx, My, V).
6. **Member design checks** (Section 10b) — for each member: φPn, φMn(x,y), φVn,
   interaction ratio (D/C), pass/fail, **cited A360 section + equation**.
7. **Connection schedule + checks** (Section 10c) — every connection, type, demand,
   capacity, D/C, cited A360 (and A358/A341 where relevant).
8. **Drift & irregularity report** — interstorey drifts vs ASCE 7 limit; torsional /
   vertical irregularity findings; dual-system 25% split if applicable.
9. **Figures** (Section 12) — undeformed model, deformed shape(s) for governing
   combos, first few mode shapes, member force/"stress" diagrams, drift profile.
10. **Reproducibility artifacts** — the final `model.py` (with the actual sections),
    the OpenSees **recorder output files** (so results re-open in a viewer), and a
    short `run.md` telling the human exactly how to re-run and re-open everything.
11. **Narrative design report (.md)** that ties 1–10 together, with citations.

Format member/connection schedules as tables (and/or `.csv`) so the human can scan
and import them.

---

## 9. Load combinations — how to handle them (IMPORTANT)

The library models run **one load case at a time** (gravity, ELF-X, ELF-Y, wind-X,
wind-Y, RS). **Member design demands must come from the ASCE 7-22 §2.3 LRFD load
combinations**, not any single case. Procedure:

**a) Run the component load cases** (unfactored/nominal), each as its own pattern:
D, L, Lr/S, W (±X, ±Y), and E (seismic: ELF ±X, ±Y, or RS). Record element forces
for each.

**b) Form the LRFD combinations (§2.3.1 + seismic §2.3.6 / §12.4):**
1. 1.4D
2. 1.2D + 1.6L + 0.5(Lr or S)
3. 1.2D + 1.6(Lr or S) + (L or 0.5W)
4. 1.2D + 1.0W + L + 0.5(Lr or S)
5. 0.9D + 1.0W
6. (1.2 + 0.2·SDS)D + ρ·QE + L + 0.2S   ← seismic
7. (0.9 − 0.2·SDS)D + ρ·QE              ← seismic
where E includes the **vertical term Ev = 0.2·SDS·D** and the **redundancy factor ρ**
(1.0 or 1.3 per §12.3.4), and QE is the horizontal seismic effect.

**c) Apply the required permutations to each lateral combo:**
- **Sign:** ±W and ±E (both directions of action).
- **Orthogonal (directional) combination:** 100% in one axis + **30%** in the
  orthogonal axis (ASCE 7 §12.5.3/§12.5.4 for SDC C+ and for plan irregularities);
  do both (100X+30Y and 30X+100Y). Wind: apply each principal direction (and
  quartering cases per §27 if required).
- **Accidental torsion:** ±5% eccentricity (the engine's `accidental=True` static
  applies the ±0.05·B torsional moment); include where required (and amplify Ax per
  §12.8.4.3 if torsionally irregular).

**d) Second-order (P-Δ) is nonlinear → do NOT superpose factored second-order
results.** Either: (i) build each LRFD combination as a single **factored** load case
(factored gravity + factored lateral applied together) and run it through the **P-Δ**
analysis, then read member forces; **or** (ii) superpose **first-order** case forces
with the combo factors and apply the Appendix-8 **B₁/B₂ amplifiers** to the
sway (lt) part. Method (i) is cleaner with this engine — extend `static_lateral` to
take a factored {gravity, Fx_X, Fx_Y} set per combo.

**e) Envelope.** For each member, the **design demand** is the worst case across all
combinations/permutations (max |P|, max |Mx|, max |My|, max |V|, and the max
interaction ratio). Report the **governing combo id** with the demand.

---

## 10. Member & connection design output (how to compute + table columns)

### 10a. Extract member forces
For each `elasticBeamColumn`, after running a (factored) combo:
`ops.eleResponse(tag, 'localForces')` → 12 values
`[N, Vy, Vz, T, My, Mz]_i  +  [...]_j` in **local** axes. Take design forces as
P = max|N|, Mz = max|Mz| (strong axis), My = max|My| (weak axis), V = max|Vy,Vz|,
across both ends and all combos. Map each element tag → member (column line / beam /
brace) via the engine's tagging.

### 10b. Member capacities (LRFD φ) — ground each in A360 (query the spec RAG, cite)
- **Tension:** φtPn = min(0.90·Fy·Ag [D2-1], 0.75·Fu·Ae [D2-2, D3]).
- **Compression:** φcPn = 0.90·Fn·Ag; Fn from **E3** (E3-2/E3-3) using Lc/r = K·L/r
  (DAM ⇒ K=1).
- **Flexure:** φbMn = 0.90·Mn; Mn = Mp = Fy·Zx for compact, braced [F2-1], reduced for
  LTB [F2-2/F2-3] using Lb, Cb; check both axes.
- **Shear:** φvVn per **G2** (= 1.00·0.6·Fy·Aw·Cv1 for rolled I webs).
- **Beam-columns:** interaction **H1-1a/H1-1b**:
  Pr/Pc + (8/9)(Mrx/Mcx + Mry/Mcy) ≤ 1.0  (or the H1-1b branch). **D/C = this value.**
- **Braces:** compression **E3** (Lc/r) + tension **D2**; seismic slenderness &
  width-thickness from **A341** (query `engineering_standards_A341`).
Cross-check one of each against `steel_design_examples` / the AISC Q&A database.

**Member schedule table columns:**
`member_id | type | gridline/level | section | length | Lb,Lc | governing_combo |
P(kip) | Mx(kip-ft) | My | V | φPn | φMnx | φMny | φVn | interaction D/C | status | A360_refs`

### 10c. Connections
List every connection: beam-to-column (shear &/or moment), brace-to-gusset/column,
column splice, **column base plate/anchorage**, collector/diaphragm chord.
- **Demand** = forces of the connected member(s) from the governing combo. For
  **seismic** systems, use **capacity-design** demands per A341 (e.g., brace expected
  strength RyFyAg; amplified collector forces with Ω₀) — query `engineering_standards_A341`.
- **Capacity** per A360 **Chapter J** (bolts J3 + Table J3.2, welds J2 + Table J2.5,
  block shear J4, bearing/tearout J3.10, base plates J8) and **A358** for prequalified
  moment connections.
**Connection schedule columns:**
`conn_id | location | type | demand(V,N,M) | governing_combo | limit_states | capacity |
D/C | A360/A358/A341_refs | status`

---

## 11. Definition of DONE (acceptance criteria)

The job is satisfactorily complete when ALL hold:
1. **Model valid** — every sanity check passes (equilibrium, stability, period band,
   ≥90% modal mass, base-shear recovery, RS ≥ 0.85 ELF).
2. **Loads complete & checked** — all §2.3 combos formed with ±, 100/30 directional,
   accidental torsion, ρ and Ev included; Cs/V/qz spot-checked by hand.
3. **Every member D/C ≤ 1.0** under the governing envelope (members that fail were
   resized and re-run; final sections reflected in `model.py` and the schedule).
4. **Drift OK** — max **design** drift δ = Cd·δ_elastic/Ie (§12.8.6) ≤ ASCE 7 limit
   (0.020·hsx, or 0.015/0.010 for Risk III/IV) in both directions; stability coefficient
   θ (or B₂) within §12.8.7.
5. **Irregularities addressed** — torsional/vertical irregularities identified, and
   the required amplifications/restrictions applied (or the building confirmed regular).
6. **System rules met** — for seismic systems, R/ρ/height limits and detailing per
   A341 satisfied; dual systems carry ≥25% on the MF.
7. **Connections sized** for member/capacity-design demands; all D/C ≤ 1.0.
8. **Every capacity is cited** to an AISC 360-22 section/equation (and A341/A358 where
   applicable) — nothing from memory.
9. **Deliverables (Section 8) produced**, internally consistent, and **reproducible**
   (final `model.py` + recorder outputs + `run.md`).
10. **Human can inspect** the model and results (Section 12 figures + viewer steps
    provided).
If any item fails, mark the package **NOT DONE** and list the open items.

---

## 12. Human inspection guide — open the model & results in a viewer

**OpenSees has no built-in GUI** (it's a solver). Give the human one of these paths;
the agent should generate the figures/output files so the human just opens them.

**A0) Built-in reviewer figures — `engine/plot_model.py` (use this first).** Generates,
per building (also produced automatically by `pipeline.design_and_report`):
`<id>_geometry.png` (members color-coded by type + support markers), `<id>_orientation.png`,
and `<id>_deformed_X.png`. The **orientation figure is a required QA**: it draws a tick at
each beam/column mid-span along the section **depth/web** direction — **every floor-beam tick
must be vertical**; a horizontal beam tick means the member is oriented wrong (this is the
picture that catches a strong/weak-axis swap). A senior engineer should review the geometry
and orientation figures (inputs) and the deformed shape (output) before trusting results.
`python engine/plot_model.py B07`. (Pure matplotlib; no extra install.)

The pipeline already writes the geometry / orientation / deformed figures (via `engine/plot_model.py`); the human just opens the report. External viewers (opsvis, vfo, ParaView, STKO) and raw OpenSees recorders are optional and NOT part of the agent's job.

---

## 13. The turnkey tool (one call) -- DEMANDS only

`pipeline.design_and_report(name, cfg)` is the one call you run. It automates only the mechanics
that are **not** code checks: it builds the model, computes the ASCE 7 loads, assembles the
**ASCE 7-22 Sec.2.3 LRFD combinations**, runs **each as a factored case through P-Delta** (correct
nonlinear handling -- no superposition), **envelopes the per-member DEMANDS** (P, M, V + governing
combo), writes the demand package, and builds the report scaffold.

It computes **NO AISC 360 capacity.** There is no coded E3/D2/F2-F6/G2/H1, no App.8 B2, and no
AISC 341 SCWB/Omega0 anywhere in this repo. For **every** governing member and connection, the B2
amplifier, and the seismic capacity-design check, **you** query the AISC 360 / 341 RAG, select the
governing limit state, implement the exact equation (phi, limits, clause), compute the capacity and
D/C, cite the Section + equation, and write `limit_state` / `cited` / `capacity` / `DC` into
`calc_package.json`. Then re-run `report.build_report(name)` so the report shows your checks.

**Design a building (the required path)** -- create your solution folder under the removable jobs area `jobs/<name>/`
(the name the user gave, or one you make up), compose the `cfg`, then run the pipeline in ONE call
(**no user-review pause**):
```python
import pipeline
res = pipeline.design_and_report(name, cfg)   # model + loads + DEMAND envelope + figures + report
```
Determine the joints / base fixity explicitly and STATE them in the report, but do **not** pause to
ask the user to approve the model. (`build_and_preview(name, cfg)` is available as an OPTIONAL
self-review of the 3 figures, not a required hold.)
For unusual geometry or non-rigid joints, set `cfg["custom_build"] = custom_build` where
`custom_build(cfg, transf)` builds the OpenSees model yourself and returns the standard info dict
(`{cm, present, z, NF, ele:[(tag,kind,sec,n1,n2)]}` with `ntag(i,j,k)` nodes) -- the pipeline then
runs on your model unchanged.
**`present` must be ACCURATE per level for non-rectangular / setback footprints** (L/T/U plans,
notches, towers on podiums): the engine captures it into `cfg["present"]` on the first build and
derives floor areas, cladding perimeter, level masses, seismic weight W, ELF story forces and
per-level wind widths from that ACTUAL footprint -- never from the full NX x NY plate. If you set
diaphragm masses with `ops.mass`, compute them via `engine3d.floor_w` / `floor_area_ft2` /
`perim_ft` so the dynamic-model gravity, the static-model tributary gravity and the seismic
weight all describe the SAME building.
Do **not** hand-write `report.html`, a `model.py`, or your own analysis/combination scripts -- the
model, loads, combinations, demands, and report come from the pipeline. The capacities come from
**you + the RAG**.

**What the pipeline writes** (`<name>/design/`):
- `member_schedule.csv` -- every element's enveloped DEMANDS + governing combo (no capacity/DC).
- `member_demands.md` -- demand summary by member type.
- `connection_demands.csv` -- beam-end / brace / base demands + the AISC 360/341 limit-state checklist you size.
- `calc_package.json` -- per governing member type: section properties + demands; **you add**
  `limit_state`, `cited`, `capacity`, `DC`.
- `design_report.md`, the figures, `model_opensees.py` (a standalone, runnable OpenSees model the user can open and check independently), and `<name>/report.html`.

**Section properties** come from `engine/sections.py`, which reads `engine/aisc_shapes.csv` (the
AISC Shapes Database v16) for exact values.

**Honesty:** the framework computes demands, not capacities -- **every** AISC 360/341 member and
connection capacity, the B2 amplifier, and the SCWB/Omega0 check are derived by you from the RAG and
cited. Analysis is elastic; you choose Lb per the bracing.

---

## 14. The design LOOP (repeat until every member passes)
Run the pipeline for demands, derive each capacity from the RAG, and if any member is NG (D/C > 1.0)
or SCWB < 1.0, resize that section in the cfg and re-run until all D/C <= 1.0 (then drift +
serviceability, then `consistency.check`). You MAY sanity-check a *derived* number against a known AISC
value -- e.g. **W14x90** (Fy=50): phi_c*Pn(KL=13 ft) ~= **1040 k**, phi*Vn = **185 k**, phi*Mn = **574
k-ft** (noncompact flange, so F3.2 reduces below phi*Mp = 589 k-ft) -- but the number must come from
YOUR RAG-grounded derivation, not a library function (there isn't one).

> **Acceptance:** DONE when every governing member AND connection is RAG-grounded (limit-state equation
> traced to an AISC 360/341 clause, capacity derived by you), every D/C <= 1.0, drift and serviceability
> pass, `consistency.check(name)` is clean, and the HTML report is generated. End by OFFERING optimisation.

## 15. Connections — DESIGN them in place (not delegated)
Connections are a **required deliverable**, designed here — *not* delegated. The pipeline seeds a
`connections` list in `calc_package.json` (one slot per governing member type + column base) with the
DEMANDS; **you** size each connection (bolts J3, welds J2, block shear J4, HSS Ch. K, base plates
J8/J9 + ACI 318 Ch.17; moment connections per AISC 358; seismic forces per AISC 341) and write its
`limit_state` / `cited` / `capacity` / `DC` (and the actual components — bolt size/grade & count,
weld size/length, plate thickness). Report Chapter 10 and the Chapter 6 Connections table render them.
A member-only package is INCOMPLETE.

## 16. Optimisation — OPT-IN, never automatic
Optimisation drives member sizes down to the lightest sections that still pass every limit state. It
is **opt-in**: never run it automatically. **Your final message of every design run MUST ask the
user**: *"Would you like me to run an optimisation analysis now to reduce member sizes? If so, any
particular guidance, or shall I proceed undirected?"* If they opt in, follow their guidance; otherwise
use common-sense rules — columns get lighter up the height but change in **groups of levels** (not
every level), columns may differ within a level by demand (**exterior vs interior**), beams kept to a
few repeated sizes, all while D/C ≤ 1.0, drift, deflection, SCWB/ductility and connections still pass.
Each iteration: edit the `cfg` (usually a `custom_build` section-map by level-group & position),
re-run `design_and_report`, re-derive capacities. **After every optimisation run, present the new
solution and ask whether to try something different or commit — and WAIT for the user before updating
the report.**

## 17. Numerical self-consistency check (final gate)
Before finalising, run `import consistency; consistency.check(name)` and resolve **every** flag. It
verifies the package is internally consistent: each member/connection has a limit state, capacity and
D/C; the headline D/C equals its worst limit-state check **and** demand÷capacity; no D/C exceeds 1.0;
and the **same quantity carries one value everywhere** (a plate thickness or D/C quoted in your prose
must equal the structured value — if two scripts produced different numbers, reconcile them, don't
report both). Report Chapter 13 renders the result.
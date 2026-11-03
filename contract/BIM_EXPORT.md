# Physical data export and legacy replay (v1)

## Stage 3 consumer extensions

The openBIM 0.2 consumer and matching HR/CFS contract add optional fields to the
v1 package. Existing v1 packages remain supported. Older strict validators may
reject packages using these extensions; upgrade the consumer and shared contract
together rather than bypassing validation.

- `member.analysis_segments` maps fractions of an analysis element to physical
  pieces, using `{analysis_id, start, end}` with `0 <= start < end <= 1`. Fractions
  across pieces must cover the whole element without overlap. `analysis_ids`
  continues to claim entire elements. This permits real column splices between
  analysis nodes without inventing a new analysis mesh.
- `joint.attachments` supports interior attachments using `{member_id, station,
  offset}`. The station is a fraction of the reference member axis and the offset
  is a global XYZ vector in inches. Joint coordinates must match that declaration.
  A joint needs member ends or attachments; all physical member ends must still
  be accounted for.
- Profile family `round` has positive diameter `d`, for solid rods.
- Optional root `review` holds the recipe identity and coordination items. An
  unresolved item produces a `needs_input` finding independently of the stored
  readiness value. A question answer alone is not a resolved physical model.

CFS physical wall replay honours `cfg['line_plan_pos_ft']` keyed as `X-LINE` or
`Y-LINE`, falling back to `WallLine.pos` when no override is supplied. Both the
physical and original analysis positions are retained in layout evidence. This
does not change the analysis model or fitted tributary properties.

Replay also fingerprints common job-local data files (`json`, `csv`, `tsv`,
`txt`, `yaml`, `yml`, `toml`, `xlsx`) outside generated-output folders, in addition
to Python helpers and the explicitly tracked calculation/schedule files. Put
additional inputs in `bim_inputs.json` as a JSON array of job-relative paths, e.g.
`["design/physical_layout.json", "data/section_table.dat"]`. Paths must resolve to
existing files inside the job. Generated BIM packages are excluded to avoid a
self-referential fingerprint. Dependencies outside the job must be brought into
the job or otherwise supplied as explicit provenance; they are not auto-discovered.

Stage 3 review recipes use a physical profile convention: local Y is profile
width and `(member axis cross local Y)` is depth. They explicitly convert from
source-engine axes and retain the reference axis plus global end offsets. The
viewer renders design profile dimensions, not rolled fillets, bend radii or
unresolved cut polygons. See `docs/stage3` in openBIM for the pinned pilot scope.

This module publishes `design/bim_package.json` for dimensioned design drawings and
IFC export in Steltic openBIM. The export joins native model geometry with the
completed calculation package and explicit physical declarations. It does not run
load cases, solve, size members, calculate capacities, or alter the completed design.
Fabrication/shop detailing and IFC import are later work.

## New design jobs

`new_activity_log` creates `design/bim_request.json` with `required: true` and
`require_connection_parts: true` for a new
design job. Existing jobs and the separate HR build-only workflow retain their
previous completion rules. For a new job:

1. Author `cfg['bim']` alongside the building model, using `schema_version: '1.0'`
   and `required: True`. Record physical member axes, orientation, profiles,
   materials, panels, openings, and real secondary framing as these are selected.
2. Run the normal design pipeline and complete its engineering checks. Put
   connection occurrences and located physical parts,
   and source-record mappings in the calculation package's top-level `bim` object.
   Both declaration objects have the same collection names. Declare a given ID in
   only one place; conflicting duplicates fail.
3. Run `consistency.check(name)` against the saved files. After structural
   consistency passes, this automatically replays the model without analysis and
   writes the BIM package. Structural and BIM verdicts remain separate: the
   structural consistency stamp retains its original meaning. BIM problems are
   returned/printed separately and block final completion for required jobs.
4. Resolve the BIM findings using actual design decisions. Re-run consistency
   after editing source files. Do not edit the generated readiness flag or remove
   the requirement marker. Source changes make an export stale.

The exporter records data; it cannot decide unspecified geometry. Design agents
must record their selections as structured fields during the job. Never claim a
missing part or a connection location from a demand-only analysis model. Retain
existing engineering checks and responsibility boundaries. If an upstream pipeline
rerun reseeds its calculation package, revalidate/reapply the matching `bim` block
after completing that new design; do not reuse stale connection selections.

## Replay completed jobs

Install HR and CFS in separate environments (both expose the Python package
`steltic` and native engine module names). In the matching environment:

```text
python -m steltic.bim_export replay /path/to/job --output /path/to/export/bim_package.json
python -m steltic.bim_export batch /path/to/jobs --output-root /path/to/exports
python -m steltic.bim_export validate /path/to/export/bim_package.json
```

Installed commands are `steltic-bim` for HR and `steltic-cfs-bim` for CFS. Without
`--output`, replay writes only `job/design/bim_package.json`. Use an external output
directory for immutable gold jobs. `--timeout` defaults to 120 seconds **per job**.
`--require-ready` makes missing physical data a nonzero exit result; normal replay
permits `needs_input` exports so legacy evidence is usable. Invalid packages and
failed workers always return nonzero. Batch continues after an individual failure
and writes `replay_summary.json`. Validate returns 0 for ready, 2 for any findings.

Each replay runs trusted job Python in a separate process and temporary working
directory. Tracked job inputs are staged together under the original job name,
so ordinary relative data reads, nested helpers, `runpy.run_path` and
`__file__`-relative paths use that staged tree. Generated relative files remain
in scratch, and the original source fingerprints are checked before publication.
Known analysis/design entry points are disabled; timeout kills its owned
process tree. Python configuration execution is **not a security sandbox**. Only
replay trusted jobs. Fingerprints detect changes to source evidence and prevent
publication if it changes; this detects accidental writes, not malicious code.

HR uses `engine3d.build` and captures actual OpenSees nodes, elements, transforms,
local axes, restraints and source properties. If `model_opensees.py` exists, tags,
connectivity, coordinates, element arguments and transforms are compared. If a
configuration dependency is missing, a strictly literal, top-level whitelist of
saved model commands can rebuild the saved model; arbitrary saved Python and its
modal self-check are never executed. That fallback remains `needs_input`, with the
missing dependency recorded; it is not an independent configuration reconstruction.

CFS portals use `cfs_frame.build_portal` without a solve and replicate **all**
explicit frame stations. Provide `frame_positions_ft`, or consistent `n_frames`,
`spacing_ft` and/or `length_ft`; no viewer frame cap or representative-frame default
is used. Straight analysis chains become physical candidates; bends remain split.
CFS walls use physical line declarations, not equivalent springs. Add optional
`WallLine(..., segment_positions_ft={story: [(start_ft, end_ft), ...]})`, ordered to
match `segments[story]`. This layout property does not change analysis. Unambiguous
numeric `braced_bay_layout.braced_bays_ft` ranges in a saved package can supply
legacy positions. Unlocated segments remain findings; no schematic spacing is used.

## Contract and coordinate conventions

`steel_engine/bim_package.schema.json` defines the JSON shape;
`steel_engine/bim_contract.py` applies geometry, reference and coverage checks.
Both are byte-identical in HR, CFS and openBIM. Consumers must call `validate` and
recompute readiness; schema validation alone does not establish completeness.

Canonical units are **inches, kips, ksi**, with a right-handed global XYZ frame and
Z upward. HR retains native axes. CFS maps portal transverse X to global X, frame
position to Y and elevation to Z; wall X/Y directions retain their named plan axes.
Every physical declaration uses canonical units, even when CFS inputs use feet.

The root contains `analysis`, `physical`, `design_records`, `design_context`,
`layout_evidence`, `source_findings`, `provenance`, `required` and `readiness`.
Analysis entities are evidence, not automatically physical components. Native
metadata is retained; tuple-key maps are represented as typed `entries` lists.

| Physical collection | Required meaning |
| --- | --- |
| `members` | Stable ID/kind, two reference-axis endpoints, unit `local_y` perpendicular to that axis, profile/material IDs, `reference_line`, two global `end_offsets`, `analysis_ids` |
| `profiles` | ID, family, numeric cross-section dimensions; built-up profiles reference placed constituents |
| `materials` | ID and explicit specification, with additional grade/properties as available |
| `panels` | ID/kind, ordered planar boundary vertices, physical thickness and material |
| `openings` | ID, host member/panel ID, ordered planar boundary vertices |
| `joints` | ID, global point, member end assignments (`start`/`end`); optional global `joint_offset` on each assignment |
| `connections` | An actual joint occurrence, source design record IDs, scope, and located parts or a versioned design-detail reference |
| `interfaces` | ID, scope, responsible party, representation, and versioned detail reference for delegated or existing work |

Member local X runs from axis start to end; local Z is X cross Y. `end_offsets`
are global translations from each reference-axis endpoint to the physical end
(cutback/extension/eccentricity). State the cardinal/reference line explicitly;
the exporter does not infer it from the analysis. A joint point must equal its
member reference endpoint plus `joint_offset` (default zero); end cutbacks remain
independent. Profile component offsets are in the local YZ plane, with
`rotation_deg` about local X. Declare member roll through `local_y`.

Supported profile families/dimensions:

- `i`: `d,bf,tf,tw`; `rhs`: `h,b,t`; `chs`: `d,t`.
- `channel` and `z`: `d,b,lip,t,r` (zero lip/radius is allowed when intended).
- `angle`: `h,b,t`; `flat`: `width,thickness`.
- `built_up`: `dimensions: {}` and `components`, each with `profile_id`, local
  two-coordinate `offset`, and `rotation_deg`; constituents must be declared,
  non-built-up profiles. Do not substitute gross equivalent stiffness for geometry.
- `box_envelope`: `width,depth`, only with `scope: 'delegated'` and a documented
  `basis`; it represents a declared envelope, not supplier internals.

Section coordinates use local Y for width and local Z for depth. I, RHS, CHS
and flat sections are centred; channel/Z sections use the web mid-depth centre,
with channel flanges opening toward positive local Y. Angles use the outside
corner. These are profile reference points, not automatically computed section
centroids. `reference_line` describes the supplied axis; use explicit offsets
where the intended physical profile reference point differs from that axis.
For a built-up channel box, place the two web reference points at their actual
separation; do not apply offsets as if each channel were centred on its bounding
box. Every ply must occupy separate steel volume. Gross overlap of straight
web/flange cores is reported as `physical_constituent_overlap` and prevents a
`ready` result. This limited check excludes lips, fillets and unsupported curved
cores; a clean result is not a complete interference or fabrication-fit check.

Connection `parts` each carry ID, kind, material ID and geometry. Geometry supports
oriented `box` (origin is its minimum local corner), `cylinder` (origin is base
centre), `extruded_profile` (origin is profile reference point) and `polyline`.
All coordinates/axes are global. Record bolt/anchor/hole dimensions, pitch, gauge,
edge distances, weld sizes and assembly notes in part `parameters` as applicable
to the design drawing. Their detailed fabrication semantics are not validated in
v1. No weld or fastener capacity is recalculated by this exporter.

A hosted opening's boundary must lie in its declared panel plane as well as
within its bounds. A diagonal strip with the correct bounding box is not a
vertical wall host. Non-coplanar boundaries produce
`physical_opening_host_plane`; they cannot be treated as resolved openings
merely because a boundary reference can be exported.

`physical_opening_workline` reports declared member axes (including end offsets)
crossing convex planar opening reservations within the host thickness. It uses
the actual opening edges, with a 5 mm tolerance at the jambs, head and sill;
rotated walls and reversed boundary winding are supported. Concave/nonplanar
boundaries require separate geometry review. Resolve reported framing clashes
before claiming readiness. This limited check does not establish clearance of
full profiles or connection parts, which still need coordination.

A `detail_ref` has `id` and `revision` and must name an actual controlled design
detail. V1 preserves the reference but does not fetch or render a detail library;
openBIM's future renderer must resolve it before issuing that sheet. Delegated
connections additionally require `responsible_party` and `representation`.
Existing-shell CFS component work must declare an interface with `scope: 'existing'`
and identify the existing members as context, not newly designed steel.

## Mapping and completeness

Use stable physical IDs; native analysis IDs are `hr:element:<tag>` or
`cfs:frame:<station>:element:<index>`. Declarations can consolidate analysis
segments through `analysis_ids`. Each analysis element must map to one member or
have an explicit `analysis_only` entry and reason. Zero-length/rigid idealizations
are classified separately. Physical panels can replace wall/strip idealizations
with documented analysis-only dispositions. Never draw rigid links as steel.

Every top-level list of objects in the calculation package is preserved in
`design_records`, with full source data and JSON pointer. IDs are
`<table>/<source-id>` or `<table>/@<zero-based-index>` when absent. Other context,
including primitive lists, remains in `design_context`. Physical entities may link
`design_record_ids`. A grouped design row can link multiple real occurrences.

Each connection record and drawing-related member/schedule record must be mapped
or explicitly disposed in `record_dispositions`: `not_applicable` with a reason,
`covered_by_assembly` with entity IDs, or `delegated` with an interface ID. Every
declared member end needs a joint; each joint needs a connection or an explicit
free/continuous `joint_dispositions` reason. Waived engineering records remain
preserved as evidence and do not require a fictitious component.

`coverage` requires `primary_members`, `secondary_members`, `openings` and
`connections`, each with `complete: true` and a meaningful `basis` identifying how
the full building/scope was covered. It is an authored completeness assertion,
not a quantity inferred from the analysis model. Missing categories block readiness.

Statuses:

- `ready`: declared v1 geometry/references/mappings/coverage pass the contract.
- `needs_input`: valid evidence export with outstanding physical declarations.
- `invalid`: malformed or contradictory data, unresolved declared references, or
  disagreement with the saved HR model.

`ready` is **data-contract readiness**, not a structural, constructability,
fabrication or drawing-issue approval. The contract checks planar/nondegenerate
geometry and references; it does not validate clashes, connection capacity,
opening containment, detail-library availability or omitted real-world scope.

Provenance records source SHA-256 hashes (configuration/local helpers, calculation
package, consistency stamp, member schedule and saved viewer/model where present),
replay engine file hashes, engine version, and `analysis_executed: false`. Set
`STELTIC_BIM_REVISION` to the replay checkout commit for an additional revision label.
The historical producer revision is null when unknown; never infer it from the
current replay engine. Repeated runs on unchanged inputs/code are deterministic.

See `bim_declaration.example.json` for a synthetic one-member contract example.
Its values demonstrate the data shape and must not be used as a structural design.

## Occurrence validation and stable inputs (0.7.1 consumer)

Mapped analysis intervals must form the actual physical reference axis. The
validator rejects section-group and catch-all geometry as incomplete, even
when every analysis ID has been accounted for. Parallel occurrences, branches,
overlaps and gaps cannot be combined into one physical member. Collinear
subdivisions and explicit physical splice intervals remain supported. Record
storeys and actual joint locations while constructing occurrence records.

Runner scratch files under `.run` and root conversation/run logs are excluded
from automatic input fingerprints. They change after a normal completed run.
Configuration helpers and job-local data remain tracked; explicit entries in
`bim_inputs.json` can include an otherwise excluded file when actually used.

A drawing consumer can show captured HR worklines provisionally when physical
declarations are incomplete. This does not resolve the producer findings or
authorize invented physical dimensions, connection geometry or a ready flag.

## Physical consistency screens

The shared validator independently checks explicit built-up section count labels
(e.g. 6xCHANNEL must contain six CHANNEL constituents), opening vertices outside
panel-host bounds, and explicitly sized connection box parts more than 72 inches
from their assigned joint. Box distance uses transformed bounds, not only the
origin. Findings are `needs_input`, even when an older package says `ready`.
Resolve the source declaration before completing a required new job. No section,
coordinate or calculation record is automatically repaired. Legacy replay and
coordination export may retain these findings for review. These limited screens
do not establish capacity, architectural agreement, complete framing or fit.

## Reference systems and connection geometry requirements

Optional `bim.reference_system` is carried unchanged to the package root. It
records explicit coordination datums and grid polylines in inches, independently
of physical member offsets or simplified analysis coordinates:

```json
{
  "basis": "Architectural reference coordinates; physical post offsets are separate",
  "levels": [{"level": 0, "name": "Ground", "elevation_in": 0},
             {"level": 1, "name": "Platform", "elevation_in": 144}],
  "grids": [{"id": "A", "label": "A", "direction": "U", "points_in": [[0,0,0],[120,120,0]]},
            {"id": "1", "label": "1", "direction": "V", "points_in": [[0,120,0],[120,0,0]]}]
}
```

An optional `source_ifc_sha256` links the declared reference system to its IFC
input. Grid IDs and integer level IDs must be unique; grid curves must have
length and include both U and V families. Do not infer architectural grids from
member bounds. Record each member/panel/joint's numeric `level` separately where
known; grid and datum declarations do not invent object assignments. Optional
entity `source_ifc_guid` retains the corresponding architectural source identity.

The new-job request marker's `require_connection_parts` is preserved through
consistency and replay, and copied to the package root. When true, every designed
connection needs physical parts; detail references and reference polylines alone
produce `connection_parts_required`. Explicit delegated interfaces still require
a named responsible party, versioned detail and representation, and do not require
invented supplier geometry. Older jobs without this requirement retain the prior
controlled-reference contract. A job may explicitly opt in with
`cfg['bim']['require_connection_parts'] = True`. This is design coordination
geometry, not fabrication approval.

## Bound architectural inputs

When a design starts or resumes with an openBIM architectural handoff, the
framework saves its original grids, named levels, IFC fingerprint and every
door/window as `architectural_input.json`. This file is an input to the design
and is fingerprinted by physical replay. The complete original brief, wall and
floor/roof evidence stays in `architectural_handoff.txt`. `new_activity_log` preserves this
binding. A later complete handoff can revise it, retaining the previous input
in `architectural_input_history`.

Read this file and copy its `reference_system` into `cfg['bim']`; use the supplied
numeric level IDs consistently. Every architectural opening needs one physical
opening with the exact `source_ifc_guid`, retained bounds and an explicit host.
The exporter copies the source fingerprint directly from the saved input.
If no reference system was declared, it carries the original architectural
reference system without assigning any member to a storey.

Coverage statements are checked against this bound input. Missing or moved
openings, changed/missing grids or named datums, duplicate opening mappings,
and coincident identical physical members prevent readiness. Edit the physical
declarations to resolve these findings. The framework-owned input and its
request marker cannot be changed with `write_file`.

The optional package `architectural_input` field is supported by openBIM 0.8.17
and later. Update the consumer together with the producers. Legacy jobs without
a bound IFC input keep their existing contract. These checks preserve supplied
data; they do not infer framing, connection design or omitted architectural data.

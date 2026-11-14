# HR source handoff

Use the saved-design entry point to regenerate openBIM deliverables from a completed HR design. It preserves the original report and reads the configuration as bytes. It runs without importing a job configuration, invoking OpenSees, or running the design pipeline or report renderer.

```
steltic-bim saved-handoff --job ORIGINAL_JOB --native SAVED_BIM_PACKAGE.json --decisions example-decisions.zip --output CANDIDATE/bim_package.json
```

The equivalent module entry point is `python -m steel_engine.saved_handoff`. `--contract-only` writes the `source_handoff` section alone. An authentication failure leaves the previous output intact. The output must be JSON outside the original job and input files.

The decision archive must bind the native file and physical-input SHA-256, revision, accepted answers and original design sources. The original material schedule binds the exact `report.html` bytes. The saved consistency result must declare `PASS`, integer `n_issues` equal to zero, `issues` equal to an empty list, and matching current configuration and calculation SHA-256 values. The exporter checks the original calculation rows against the native source pointers and preserves the historical producer data.

## Source fields

For new jobs, add `bim_selection` to each selected connection calculation record. Each row defines the physical role and field, typed value, unit, and optional applicability. Use distinct roles for different bolts, welds, pins or plates. When relevant, `attachment_roles` identifies the physical elements or surfaces the component joins.

```json
{
  "id": "brace-connection",
  "bim_selection": [
    {"role": "connection", "field": "variant", "value": "slotted_hss_gusset", "unit": "enum"},
    {"role": "gusset", "field": "thickness_in", "value": 0.75, "unit": "in"},
    {"role": "reinforcement_fillet", "field": "size_in", "value": 0.1875, "unit": "in",
     "attachment_roles": ["brace", "slot_reinforcement"], "applicability": {"end": "lower"}}
  ]
}
```

The HR report and normal package export read explicit material declarations and selected connection materials through the same material-schedule function. Missing declarations appear as unresolved. The report links to `delivery_certificate.json`, which binds the final report, design, drawings and IFC after rendering.

`source_handoff.records` indexes original record IDs. Every field retains an exact calculation JSON pointer. For historical component text the field also retains the source substring and its character range. `legacy_components.clauses` indexes the entire original selection text and names the typed fields present in each clause. An empty list of typed fields identifies retained text requiring interpretation. `variant_basis` distinguishes explicit recognized topology from a source record/type that still needs interpretation.

`source_handoff.occurrences` indexes physical connection IDs. For each record application, `selected` and `conditional` refer to indexes in that record's `fields` array. A consumer must respect the actual joint, story and end. A differently scoped branch cannot establish an unconditional value. `receiver_roles` identifies terminal, whole-member and midspan contexts for brace and truss-web bindings and retains the bound physical member/end identity.

The source contract carries specified material, thickness, attachment, weld, fastener, set-out and clearance selections. openBIM completes missing local contours, cutbacks and fit against the actual connected members. A describing field and a label in a template do not prove target geometry. Final artifact checking belongs to the independent openBIM delivery gate.

## Design schedule and report binding

`source_bindings.design_schedule` lists every member record with a section and every connection record as the
report prints it: record ID, section or type, the governing limit state, the governing D/C as a number and as
printed text (`dc_text`), and for a typed connection the printed lines of its `bim_selection`
(`bolt: count 5; diameter_in 0.75 in; material ASTM F3125 Gr A325-N`, one line per role and applicability).
`steel_engine/bim_design_schedule.py` holds the one rule for the governing D/C and its text. The report tables,
this schedule and the report read-back all call it. openBIM prints these values on the drawing schedules and
stores them in the IFC, so a drawing or model value is the report value character for character.

The report prints the typed lines in each connection block as **Issued selection**, below the free-text
**Components** row. Keep the two consistent: the drawings and the IFC follow the typed rows.

The export reads `report.html` back against the records it is exporting: member sections, connection
components, issued selections, and the governing D/C in each calculation block and in every summary-table row
that links to it. `source_bindings.report_binding` is then `bound` (read-back passed, evidence in
`report_selection_readback`), `stale` (the report on disk prints other values) or `awaiting_report`. A stale
report gives the finding `hr_report_stale`, the package is not ready and the completion gate is not clean.
Render the report again with `report.build_report(name)`: it binds the new report, records the read-back and
updates the stored readiness, without a second export. A report rendered before this engine version has no
Issued selection rows and must be rendered again once.

## Brace-end assemblies of a required new job

A new job has `require_connection_parts: true`, so every designed connection, brace ends included, is exported
with its located parts: the gusset, the slot welds joining brace and gusset, the support welds, the bound
receivers, and the brace `end_offsets` and `slots`. openBIM measures that assembly in the IFC (slot engagement,
weld reach to the bound receivers, clearance from the restrained gusset edge to the brace end where
`brace_end_clearance.depths` is selected, the cap stations of the brace). openBIM fits a gusset contour itself
only for a connection exported without parts, which a required job cannot export as ready. The physical
connection `parameters.joint` (`corner`, `apex`, `base`, ...), `parameters.upper_end` and `parameters.story`
are read by both sides to apply scoped selections.

## Source and location checks

The saved exporter compares each printed connection Components row and each printed member section to the named row in the original calculation package. The result and row counts are in `source_bindings.report_selection_readback`. The original file digest and the canonical native-body digest are separate. Recompute `native_content_sha256` from the entire native package after omitting the additive `source_handoff` key. Use sorted JSON keys, compact separators, ASCII escapes, UTF-8 and finite numbers. Sealing a new report updates its binding on a current HR export.

An occurrence may include a section-specific beam end and a collector-to-frame connection elsewhere on the load path. `interface_application` gives the geographically bound local member, grid, level and applicability. `beam_end_group` identifies source-enumerated collector or conventional ends. `alternate_end` locates a source-named gusset end using the actual brace connection and the same member. A multi-member truss node uses `hss_members` to retain which slotted web members each source row describes.

`crossing_interfaces` identifies selected interior flat-bar crossing welds separately from terminal connections. Each entry gives both physical member IDs and stations, the 3D location in inches, the source record and field indexes, the terminal connection sources and any existing connection at the crossing. An absent authored connection at a located crossing requires openBIM to complete the geometry. `unresolved_crossing_interfaces` preserves selected crossing requirements for which no member pair could be located.

## Field-specific decisions

A general geometry answer leaves source selections intact. An accepted decision may override an individual field when it identifies the connection ID, original record ID and SHA-256, field index, source pointer, original source value, role, field, unit, calculation SHA-256, replacement value, named author and reason. The original selected field remains available. A competing unresolved value stays in `source_conflicts`.

The existing physical decision API can record such an answer as an explicitly bound change to a connection's `selection_overrides` field. For a decision archive, the corresponding accepted decision may carry `selection_overrides` directly. A request for local geometry cannot substitute for an engineering review required by the actual change.

A successful saved handoff establishes the authenticated source for generating a candidate. The independent delivery certificate records the final status after it checks the actual PDF and IFC.

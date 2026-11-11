# HR source handoff

Use the saved-design entry point to regenerate openBIM deliverables from a completed HR design. It preserves the original report and reads the configuration as bytes. It runs without importing a job configuration, invoking OpenSees, or running the design pipeline or report renderer.

```
steltic-bim saved-handoff --job ORIGINAL_JOB --native SAVED_BIM_PACKAGE.json --decisions example-decisions.zip --output CANDIDATE/bim_package.json
```

The equivalent module entry point is `python -m steel_engine.saved_handoff». `--contract-only» writes the `source_handoff» section alone. An authentication failure leaves the previous output intact. The output must be JSON outside the original job and input files.

The decision archive must bind the native file and physical-input SHA-256, revision, accepted answers and original design sources. The original material schedule binds the exact `report.html» bytes. The saved consistency result must declare `PASS», integer `n_issues» equal to zero, `issues» equal to an empty list, and matching current configuration and calculation SHA-256 values. The exporter checks the original calculation rows against the native source pointers and preserves the historical producer data.

## Source fields

For new jobs, add `bim_selection» to each selected connection calculation record. Each row defines the physical role and field, typed value, unit, and optional applicability. Use distinct roles for different bolts, welds, pins or plates. When relevant, `attachment_roles» identifies the physical elements or surfaces the component joins.

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

The HR report and normal package export read explicit material declarations and selected connection materials through the same material-schedule function. Missing declarations appear as unresolved. The report links to `delivery_certificate.json», which binds the final report, design, drawings and IFC after rendering.

`source_handoff.records» indexes original record IDs. Every field retains an exact calculation JSON pointer. For historical component text the field also retains the source substring and its character range. `legacy_components.clauses» indexes the entire original selection text and names the typed fields present in each clause. An empty list of typed fields identifies retained text requiring interpretation. `variant_basis» distinguishes explicit recognized topology from a source record/type that still needs interpretation.

`source_handoff.occurrences» indexes physical connection IDs. For each record application, `selected» and `conditional» refer to indexes in that record's `fields» array. A consumer must respect the actual joint, story and end. A differently scoped branch cannot establish an unconditional value. `receiver_roles» identifies terminal, whole-member and midspan contexts for brace and truss-web bindings and retains the bound physical member/end identity.

The source contract carries specified material, thickness, attachment, weld, fastener, set-out and clearance selections. openBIM completes missing local contours, cutbacks and fit against the actual connected members. A describing field and a label in a template do not prove target geometry. Final artifact checking belongs to the independent openBIM delivery gate.

## Field-specific decisions

A general geometry answer leaves source selections intact. An accepted decision may override an individual field when it identifies the connection ID, original record ID and SHA-256, field index, source pointer, original source value, role, field, unit, calculation SHA-256, replacement value, named author and reason. The original selected field remains available. A competing unresolved value stays in `source_conflicts».

The existing physical decision API can record such an answer as an explicitly bound change to a connection's `selection_overrides» field. For a decision archive, the corresponding accepted decision may carry `selection_overrides» directly. A request for local geometry cannot substitute for an engineering review required by the actual change.

A successful saved handoff establishes the authenticated source for generating a candidate. The independent delivery certificate records the final status after it checks the actual PDF and IFC.

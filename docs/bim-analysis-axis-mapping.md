# Explicit analysis coordinate relabelling

An engineer may use different span and frame-station axis labels in a physical
model from those in the saved solver model. A physical member may now declare
`analysis_coordinate_mapping` with an `axis_order` permutation of `[0,1,2]`
and a nonempty `basis`. Physical coordinate `i` corresponds to saved analysis
coordinate `axis_order[i]`.

This is a strict relabelling, not a geometry exemption. It permits no scaling,
translation, arbitrary rotation, omitted or repeated coordinates. The validator
still requires every claimed interval to lie exactly on the physical reference
axis, with matching full extent and no gaps or overlaps. Physical end extensions
remain separate `end_offsets`. The saved analysis coordinates remain unchanged.

The CFS10/18 fixture uses `[1,0,2]` for span/station/Z. Its original 24ft column
and 50ft beam reference axes are retained; the already drawn head extensions and
beam-end projections move into end offsets without changing actual endpoints.
OpenBIM retains the mapping in IFC properties and the complete source entity.

Validation: 59 tests pass in this module (two optional tests skipped), including
invalid mapping rejection and existing native export, interval and clause gates.
The shared contract and schema are identical in HR, CFS and openBIM. CFS18 native
replay has no analysis geometry failures with the explicit mapping; remaining
unrelated completeness findings are kept open.

# Exterior-column and interior-HSS base selections

An unchanged source anchorage row can explicitly select different hardware
for exterior frame columns and an interior HSS prop. Applying the entire
row at every base incorrectly requires the exterior gusset bolts at the
welded HSS base.

The native BIM validator now accepts an exact labelled clause from this
specific two-clause grammar. A selection must retain the exact immutable
source text, match the actual base joint and storey, and locate the named
HSS prop between two vertical frame columns. Exterior selections must be
at one of those flanking columns. Both source clauses must have a located
application. Incorrect sections, unrelated members, upper-end points,
missing clauses and modified clause text remain findings. Each actual
base still has to export all hardware selected by its own clause.

This resolves the CFS17 base-record applicability issue without adding
fictitious bolts to the prop, dropping source requirements, changing the
gold design or weakening the native completeness check.

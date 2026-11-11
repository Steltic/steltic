from test_bim_selected_components import package
from steel_engine.bim_contract import connection_component_findings


def test_cjp_splice_without_permanent_bolts_requires_welds_only():
    p, c, _ = package("Matching E70XX CJP groove welds across both full flanges and net web, milled ends; no permanent bolts, splice cover plates or added steel.")
    assert connection_component_findings(p)[0]["examples"][0]["missing_component_categories"] == ["weld"]
    c["parts"][0]["kind"] = "weld"
    assert not connection_component_findings(p)


def test_later_positive_bolts_are_not_hidden_by_permanent_exclusion():
    p, _, _ = package("No permanent bolts; four erection bolts and CJP welds are selected.")
    assert connection_component_findings(p)[0]["examples"][0]["missing_component_categories"] == ["bolt", "weld"]


def test_positive_permanent_bolts_still_require_actual_parts():
    p, _, _ = package("Four permanent bolts and CJP welds")
    assert connection_component_findings(p)[0]["examples"][0]["missing_component_categories"] == ["bolt", "weld"]

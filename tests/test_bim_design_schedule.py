"""The design schedule issued to openBIM is what the report prints, and a stale report is not bound.

Fixtures are synthetic data-contract examples, not structural designs.
"""
import copy
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "steel_engine")); sys.path.insert(0, ROOT)

import report as R  # noqa: E402
from steel_engine import bim_design_schedule as S  # noqa: E402
from steel_engine import bim_export as B  # noqa: E402
from steel_engine.bim_report_readback import check_report_selections  # noqa: E402
from steel_engine.bim_source_fields import digest_value  # noqa: E402
from steel_engine.saved_handoff import bind_rendered_report  # noqa: E402
from test_bim_export import job  # noqa: E402


def calc():
    return {"members": [
        {"id": "floor-W21X50", "DC": 0.777, "limit_state": "flexure", "inputs": {"section": "W21X50", "kind": "beam", "role": "floor",
                                                                              "governing_combo": "1.2D+1.6L"},
         "checks": [{"limit_state": "flexure", "DC": 0.777, "demand": 320.3, "capacity": 412.5, "unit": "kip-ft"},
                    {"limit_state": "shear", "DC": 0.216, "demand": 51.2, "capacity": 237.0, "unit": "kip"}]},
        {"id": "col-W14X90", "DC": 0.5, "limit_state": "compression", "inputs": {"section": "W14X90", "kind": "column", "role": "gravity_col"},
         "checks": [{"limit_state": "compression", "DC": 0.5, "demand": 500.0, "capacity": 1000.0, "unit": "kip"}]}],
        "connections": [
        {"id": "conn-floor", "type": "single-plate shear connection", "DC": 0.61, "limit_state": "bolt shear",
         "components": "3/8 in plate as scheduled in bim_selection; weld BOTH sides, length >= 9 in",
         "checks": [{"limit_state": "bolt shear", "DC": 0.61, "demand": 61.0, "capacity": 100.0, "unit": "kip"},
                    {"limit_state": "dimensional limit: plate within the beam's flat web", "DC": 0.9}],
         "bim_selection": [
             {"role": "connection", "field": "variant", "value": "single_plate_shear", "unit": "enum"},
             {"role": "bolt", "field": "count", "value": 5, "unit": "count"},
             {"role": "bolt", "field": "diameter_in", "value": 0.75, "unit": "in"},
             {"role": "bolt", "field": "material", "value": "ASTM F3125 Gr A325-N", "unit": "literal"},
             {"role": "shear_plate_weld", "field": "size_in", "value": 0.25, "unit": "in", "applicability": {"joint": "beam_end"},
              "attachment_roles": ["shear_plate", "support"]}]}]}


def rendered(pkg):
    """The report's own tables and calculation blocks for these records."""
    table = R._table(["Member", "Section", "Demand envelope", "Governing combination", "Governing limit state", "D/C", "Status"],
                     R._member_rows(pkg["members"]))
    return "<html><body>" + table + R._connection_summary(pkg) + R.appendix({}, "fixture", pkg) + "</body></html>"


def test_one_rule_decides_and_prints_the_governing_ratio():
    assert [S.dc_text(v) for v in (0.777, 0.5, 0.125, 1.0, 0.9995, 0.1255, 2)] == ["0.777", "0.50", "0.125", "1.00", "1.00", "0.126", "2.00"]
    assert S.dc_text(None) is None and S.dc_text(float("nan")) is None
    for v in (0.777, 0.5, 0.125, 0.9995, 1.2345):
        assert R._n(v, dc=True) == S.dc_text(v)
    schedule = S.design_schedule(calc())
    assert [(r["record_id"], r["section"], r["dc_text"], r["limit_state"]) for r in schedule["members"]] == [
        ("members/floor-W21X50", "W21X50", "0.777", "flexure"), ("members/col-W14X90", "W14X90", "0.50", "compression")]
    row = schedule["connections"][0]
    # the dimensional limit (0.9) never heads the record; the strength check does
    assert (row["dc_text"], row["limit_state"], row["detailing_limits"]) == ("0.61", "bolt shear", True)
    assert row["selection"] == ["connection: variant single_plate_shear",
                                "bolt: count 5; diameter_in 0.75 in; material ASTM F3125 Gr A325-N",
                                "shear_plate_weld [joint beam_end]: size_in 0.25 in"]
    headline_is_a_limit = dict(calc()["connections"][0], DC=0.9)
    assert S.governing_dc(headline_is_a_limit)[0] == R._governing_dc(headline_is_a_limit)[0] == 0.61


def test_report_readback_reads_every_ratio_and_issued_selection(tmp_path):
    pkg = calc(); path = tmp_path / "report.html"
    path.write_text(rendered(pkg), encoding="utf-8")
    check = check_report_selections(path, pkg)
    assert check["passed"], check["findings"]
    assert (check["governing_ratios_checked"], check["summary_rows_checked"], check["issued_selections_checked"]) == (3, 3, 1)
    assert "Issued selection" in path.read_text(encoding="utf-8")
    # identifiers and emphasis in the prose are typeset by the renderer; the saved text is still the source
    assert "bim selection" in path.read_text(encoding="utf-8") and "BOTH" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("change,issue", [
    (lambda p: (p["members"][0].update(DC=0.5), p["members"][0]["checks"][0].update(DC=0.5)), "printed governing D/C differs from the design schedule"),
    (lambda p: (p["connections"][0].update(DC=0.7), p["connections"][0]["checks"][0].update(DC=0.7)), "summary table D/C differs from the design schedule"),
    (lambda p: p["connections"][0]["bim_selection"][1].update(value=6), "printed issued selection differs from the typed selection"),
    (lambda p: p["connections"][0].update(components="1/2 in plate"), "printed components differ from saved selection"),
    (lambda p: p["members"][1]["inputs"].update(section="W14X99"), "printed member section differs from saved selection"),
])
def test_a_report_rendered_before_the_last_calculation_change_is_caught(tmp_path, change, issue):
    pkg = calc(); path = tmp_path / "report.html"
    path.write_text(rendered(pkg), encoding="utf-8")
    change(pkg)
    check = check_report_selections(path, pkg)
    assert not check["passed"] and issue in {f["issue"] for f in check["findings"]}, check["findings"]


def restamp(root):
    stamp = B.read_json(root / "design/consistency_result.json")
    stamp.update(calc_package_sha256=B.digest(root / "design" / "calc_package.json"), cfg_sha256=B.digest(root / "cfg.py"))
    B.atomic_json(root / "design/consistency_result.json", stamp)


def test_export_binds_the_schedule_and_refuses_a_stale_report(tmp_path):
    root = job(tmp_path)
    pkg = B.read_json(root / "design/calc_package.json")
    (root / "report.html").write_text(rendered(pkg), encoding="utf-8")
    package = B.replay(root)
    bindings = package["source_handoff"]["source_bindings"]
    assert package["readiness"]["status"] == "ready", package["readiness"]
    assert bindings["report_binding"] == "bound" and bindings["report_selection_readback"]["passed"]
    assert bindings["design_schedule"] == S.design_schedule(pkg)
    assert bindings["design_schedule"]["connections"][0]["dc_text"] == "0.50"

    # the design changes and passes its checks again, but the report on disk is the old one
    pkg["connections"][0]["DC"] = 0.9
    B.atomic_json(root / "design/calc_package.json", pkg); restamp(root)
    package = B.replay(root)
    bindings = package["source_handoff"]["source_bindings"]
    assert bindings["report_binding"] == "stale" and not bindings["report_selection_readback"]["passed"]
    assert package["readiness"]["status"] == "needs_input"
    assert [f["code"] for f in package["readiness"]["findings"]] == ["hr_report_stale"]
    assert any("hr_report_stale" in line for line in B.completion_issues(root))

    # rendering the report again binds it and clears the finding, without a second export
    (root / "report.html").write_text(rendered(pkg), encoding="utf-8")
    bind_rendered_report(root, pkg)
    package = B.read_json(root / "design/bim_package.json")
    assert package["readiness"]["status"] == "ready" and B.validate(package) == []
    assert package["source_handoff"]["source_bindings"]["report_binding"] == "bound"
    assert package["source_handoff"]["source_bindings"]["design_schedule"]["connections"][0]["dc_text"] == "0.90"
    assert B.completion_issues(root) == []


def test_report_of_a_changed_design_is_not_bound_to_the_earlier_export_and_does_not_stop_the_pipeline(tmp_path):
    root = job(tmp_path)
    pkg = B.read_json(root / "design/calc_package.json")
    (root / "report.html").write_text(rendered(pkg), encoding="utf-8")
    B.replay(root)
    exported = (root / "design/bim_package.json").read_bytes()
    # a pipeline re-run after a configuration change renders its report before the next export
    with (root / "cfg.py").open("a", encoding="utf-8") as stream:
        stream.write("\ncfg['revised'] = True\n")
    (root / "report.html").write_text(rendered(pkg) + "<!-- re-rendered -->", encoding="utf-8")
    bind_rendered_report(root, pkg)                       # used to raise "Missing or stale SHA-256 for cfg.py"
    assert (root / "design/bim_package.json").read_bytes() == exported
    assert B.completion_issues(root) == ["BIM export is stale; rerun consistency.check or BIM replay"]
    restamp(root)
    package = B.replay(root)
    assert package["readiness"]["status"] == "ready"
    assert package["source_handoff"]["source_bindings"]["report_binding"] == "bound"
    assert B.completion_issues(root) == []


def test_schedule_cannot_be_edited_apart_from_its_records(tmp_path):
    root = job(tmp_path)
    package = B.replay(root, tmp_path / "out/bim_package.json")
    assert B.validate(package) == []
    edited = copy.deepcopy(package); handoff = edited["source_handoff"]
    handoff["source_bindings"]["design_schedule"]["connections"][0]["dc_text"] = "0.10"
    assert "hr_handoff_revision" in {f["code"] for f in B.validate(edited)}
    handoff.pop("contract_sha256"); handoff["contract_sha256"] = digest_value(handoff)
    assert {f["code"] for f in B.validate(edited)} == {"hr_handoff_design_schedule"}
    missing = copy.deepcopy(package); handoff = missing["source_handoff"]
    del handoff["source_bindings"]["design_schedule"]
    handoff.pop("contract_sha256"); handoff["contract_sha256"] = digest_value(handoff)
    assert {f["code"] for f in B.validate(missing)} == {"hr_handoff_design_schedule"}


def test_replay_allowance_grows_with_the_declared_model(tmp_path, monkeypatch):
    root = job(tmp_path)
    monkeypatch.delenv("STELTIC_BIM_REPLAY_TIMEOUT", raising=False)
    assert B.replay_timeout(root) == 120.0
    (root / "physical_inputs.json").write_bytes(b" " * 4_000_000)
    assert B.replay_timeout(root) == pytest.approx(240.0)
    monkeypatch.setenv("STELTIC_BIM_REPLAY_TIMEOUT", "600")
    assert B.replay_timeout(root) == pytest.approx(720.0)

"""October 2026 final review (H13): the 3D viewer shows the calc package's D/C, not a built-in screen.

The old "Max D/C (screening)" came from K = 1 compression and phi Fy Z bending without LTB and printed up to 4.82
against reports that pass. The viewer now takes each member's D/C from its design record in
design/calc_package.json (matched by member kind and section, worst record per section).

    PYTHONPATH=$PWD:$PWD/steel_engine python -m pytest tests/test_viewer_dc_2026_10.py -q
"""
import json, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "steel_engine")); sys.path.insert(0, ROOT)

import viewer3d as V  # noqa: E402


def _pkg(tmp_path, members):
    d = tmp_path / "design"; d.mkdir()
    (d / "calc_package.json").write_text(json.dumps({"members": members}))
    return str(tmp_path)


def test_package_dc_takes_worst_record_per_kind_and_section(tmp_path):
    root = _pkg(tmp_path, [
        {"id": "floor-W24X55", "inputs": {"kind": "beam", "section": "W24X55"}, "DC": 0.779},
        {"id": "roof-W24X55", "inputs": {"kind": "beam", "section": "W24X55"}, "DC": "0.897"},
        {"id": "col-W14X132", "inputs": {"kind": "column", "section": "W14X132"}, "DC": 0.945},
        {"id": "brace-HSS8X8X1/2", "inputs": {"kind": "brace", "section": "HSS8X8X1/2"}, "DC": 0.61},
        {"id": "no-dc", "inputs": {"kind": "beam", "section": "W18X35"}},
    ])
    pdc = V._package_dc(root)
    assert pdc[("beam", "W24X55")] == 0.897
    assert pdc[("column", "W14X132")] == 0.945
    assert pdc[("brace", "HSS8X8X1/2")] == 0.61
    assert ("beam", "W18X35") not in pdc


def test_package_dc_empty_without_package(tmp_path):
    assert V._package_dc(str(tmp_path)) == {}


def test_viewer_text_has_no_screening_label_or_file_paths():
    src = open(os.path.join(ROOT, "steel_engine", "viewer3d.py"), encoding="utf-8").read()
    i = src.index("def _viewer_data")
    body = src[i:src.index("# ---", i)]
    assert "Max D/C (screening)" not in body
    assert "member_schedule.csv" not in body.split('"meta"')[1]

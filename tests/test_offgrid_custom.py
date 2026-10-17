"""Off-grid base nodes are counted in the static base shear; custom sections return gross properties."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "steel_engine"))
import sections as S


def test_custom_section_props():
    SEC = {"PG72X636": (187.0, 160000.0, 9000.0, 150.0)}
    p = S.props("PG72X636", SEC=SEC)
    assert p["custom"] and abs(p["A"] - 187.0) < 1e-9
    assert abs(p["rx"] - (160000.0 / 187.0) ** 0.5) < 1e-9 and "Zx" not in p


def test_tabulated_section_unchanged():
    p = S.props("W14X90")
    assert p["A"] > 26 and p["Zx"] > 150 and not p.get("custom")

"""Build-only mode: a building the USER designed, modelled as given (steltic.build_only).

Run: PYTHONPATH=$PWD:$PWD/steel_engine python -m pytest tests/test_build_only.py -q
The pipeline tests build a small 3-storey frame (SMF in X, SCBF chevrons in Y) through the real engine (~20 s).
"""
import copy
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "steel_engine")]

from steltic.build_only import schema as S, translate as TR, runner as RN          # noqa: E402
from steltic.build_only import intake_xlsx as XL, intake_e2k as E2K                  # noqa: E402


def small_building():
    """3 x 2 bays (30 ft), 3 levels of 13 ft. X: SMF on grid lines A and C (moment beams). Y: SCBF chevrons on
    grid lines 1 and 4, middle bay A-B. Interior beams pinned. Everything the validator asks for is given."""
    d = S.new_description("json", "small.json")
    d["project"] = {"name": "small", "code": "ASCE 7-22"}
    d["seismic"] = {"SDS": 1.0, "SD1": 0.6, "S1": 0.6, "TL": 8.0, "risk_category": "II", "Ie": 1.0, "site_class": "D",
                    "analysis": "ELF", "system": {"X": "SMF", "Y": "SCBF"}, "R": {"X": 8.0, "Y": 6.0},
                    "Cd": {"X": 5.5, "Y": 5.0}, "Om0": {"X": 3.0, "Y": 2.0}, "rho": {"X": 1.0, "Y": 1.0}}
    d["grid"] = {"x": [{"label": str(i + 1), "coord_ft": 30.0 * i} for i in range(4)],
                 "y": [{"label": "ABC"[j], "coord_ft": 30.0 * j} for j in range(3)]}
    d["levels"] = [{"name": "Base", "elev_ft": 0.0}] + [{"name": "L%d" % k, "elev_ft": 13.0 * k} for k in range(1, 4)]
    cols, beams, braces = [], [], []
    for i in range(4):
        for j in range(3):
            cols.append(dict(id="C%d%s" % (i + 1, "ABC"[j]), x=str(i + 1), y="ABC"[j], base_level="Base", top_level="L3",
                             section="W14X90" if j in (0, 2) else "W14X68", strong_axis="X" if j in (0, 2) else "Y"))
    n = 0
    for k in range(1, 4):
        for j in range(3):
            for i in range(3):
                n += 1
                beams.append(dict(id="BX%d" % n, level="L%d" % k, i={"x": str(i + 1), "y": "ABC"[j]}, j={"x": str(i + 2), "y": "ABC"[j]},
                                  section="W24X76" if j in (0, 2) else "W18X35", release="none" if j in (0, 2) else "both"))
        for i in range(4):
            for j in range(2):
                n += 1
                beams.append(dict(id="BY%d" % n, level="L%d" % k, i={"x": str(i + 1), "y": "ABC"[j]}, j={"x": str(i + 1), "y": "ABC"[j + 1]},
                                  section="W21X44", release="both"))
        for x in ("1", "4"):
            lo = "Base" if k == 1 else "L%d" % (k - 1)
            for y0 in ("A", "B"):
                braces.append(dict(id="D%s%s%d" % (x, y0, k), i={"x": x, "y": y0, "level": lo}, j={"x": x, "y": 15.0, "level": "L%d" % k},
                                   section="HSS6X6X1/2"))
    d["columns"], d["beams"], d["braces"] = cols, beams, braces
    d["supports"] = {"default": "fixed"}
    d["diaphragm"] = "rigid"
    d["loads"] = {"D_psf": {"floor": 80.0, "roof": 60.0}, "L_psf": {"floor": 50.0}, "Lr_psf": 20.0, "snow_psf": 0.0,
                  "clad_psf": 15.0, "parapet_ft": 0.0, "floor_system": "two-way"}
    d["materials"] = {"W": "A992", "HSS": "A500 Gr C"}
    d["details"] = {"doublers": []}
    d["wind"] = None
    d["user_results"] = {"W_kip": 1350.0}
    return d


# ------------------------------------------------------------------------------------------- validation
def test_complete_description_is_ready():
    v = S.validate(small_building())
    assert v["ready"], [i for i in v["items"] if i["kind"] != "info"]


def test_missing_key_items_are_questions_never_defaults():
    d = small_building()
    d["columns"][0]["section"] = None
    d["beams"][0]["release"] = None
    d["seismic"]["SDS"] = None
    d["columns"][1]["strong_axis"] = None
    v = S.validate(d)
    keys = {i["id"] for i in v["items"] if i["kind"] == "key"}
    assert {"columns.C1A.section", "beams.BX1.release", "seismic.SDS", "columns.C1B.strong_axis"} <= keys
    assert not v["ready"]
    for i in v["items"]:
        if i["kind"] == "key":
            assert "default" not in i                       # nothing is made up for a key item


def test_defaults_need_acceptance_and_are_recorded():
    d = small_building()
    for k in ("Lr_psf", "snow_psf"):
        d["loads"].pop(k)
    del d["seismic"]["R"]
    v = S.validate(d)
    dfl = {i["id"]: i for i in v["items"] if i["kind"] == "default"}
    assert dfl["seismic.R.X"]["default"] == 8.0 and "12.2-1" in dfl["seismic.R.X"]["default_reason"]
    assert dfl["loads.Lr_psf"]["default"] == 20.0
    assert not v["ready"]                                    # an unaccepted default still blocks
    d2 = S.apply_answers(d, {k: {"accept": True} for k in dfl})
    assert S.validate(d2)["ready"]
    assert d2["_provenance"]["seismic/R/X"]["source"] == "default"
    _, prov = TR.cfg_text(d2, "small")
    acc = {a["item"] for a in prov["accepted_defaults"]}
    assert {"seismic.R.X", "seismic.R.Y", "loads.Lr_psf", "loads.snow_psf"} <= acc


def test_refused_systems_and_geometry():
    d = small_building()
    d["seismic"]["system"]["Y"] = "steel plate shear wall (SPSW)"
    d["beams"].append(dict(id="DIAG", level="L1", i={"x": "1", "y": "A"}, j={"x": "2", "y": "B"}, section="W18X35", release="both"))
    v = S.validate(d)
    ref = {i["id"] for i in v["items"] if i["kind"] == "refused"}
    assert "seismic.system.Y" in ref and "beams.DIAG.at" in ref


def test_non_aisc_section_is_one_question_per_name():
    d = small_building()
    for c in d["columns"][:5]:
        c["section"] = "BOX350x18"
    v = S.validate(d)
    q = [i for i in v["items"] if i["id"].startswith("section_map.")]
    assert len(q) == 1 and "5 member" in q[0]["question"]
    d2 = S.apply_answers(d, {q[0]["id"]: {"value": "hss14x14x5/8"}})
    assert S.validate(d2)["ready"]
    T = TR.build_tables(d2)
    assert {c[3] for c in T["COLS"]} >= {"HSS14X14X5/8"}


def test_floating_beam_end_is_asked():
    d = small_building()
    d["beams"].append(dict(id="FLOAT", level="L1", i={"x": "1", "y": "A"}, j={"x": 7.0, "y": "A"}, section="W18X35", release="both"))
    d["beams"][-1]["j"] = {"x": "1", "y": -20.0}     # a cantilever end that frames into nothing
    ids = {i["id"] for i in S.validate(d)["items"]}
    assert "beams.FLOAT.end_j" in ids or "beams.FLOAT.at" in ids


# ------------------------------------------------------------------------------------------- translation
def test_translation_is_one_for_one():
    d = small_building()
    T = TR.build_tables(d)
    # 12 column stacks x 3 levels; beams: X 27 + Y 24 = 51, the two chevron beams per level split once at the work point
    assert len(T["COLS"]) == 36
    assert len(T["BRACES"]) == len(d["braces"]) == 12
    nbeam_seg = len(d["beams"]) + 2 * 3          # beams 1-A..1-B and 4-A..4-B split at y = 15 ft on each level
    assert len(T["BEAMS"]) == nbeam_seg
    secs = sorted({b[3] for b in T["BEAMS"]})
    assert secs == sorted({S.norm_section(b["section"]) for b in d["beams"]})
    text, prov = TR.cfg_text(d, "small")
    assert "DO NOT EDIT" in text and prov["cfg"]["seis"]["source"] == "mixed"
    assert prov["members"]["elements"] == {"col": 36, "beam": nbeam_seg, "brace": 12}
    ns = {}
    exec(compile(text.replace("import openseespy.opensees as ops", "ops = None").replace("import engine3d as eng", "eng = None"),
                 "cfg.py", "exec"), ns)
    cfg = ns["cfg"]
    assert cfg["system_X"] == "SMF" and cfg["system_Y"] == "SCBF" and cfg["seis"]["R"] == 8.0 and cfg["seis_Y"]["R"] == 6.0
    assert cfg["model"]["joints"] == "mixed" and cfg["base"] == "fixed"
    assert cfg["drift_limit"] == 0.020 and cfg["D_floor"] == 80.0 and cfg["D_roof"] == 60.0


def test_a_column_passing_an_unframed_story_joint_is_one_member():
    """ETABS cuts every column at every story. Where nothing frames in and the joint is out of the diaphragm, the
    two segments are one column (as HR models it); with the joint in the diaphragm they stay two."""
    d = small_building()
    d["beams"] = [b for b in d["beams"] if not (b["level"] == "L2" and {"2"} & {str(b["i"]["x"]), str(b["j"]["x"])}
                                                and {"B"} & {str(b["i"]["y"]), str(b["j"]["y"])})]
    c = next(c for c in d["columns"] if c["id"] == "C2B")
    lower = dict(c, id="C2B_lo", top_level="L2"); upper = dict(c, id="C2B_hi", base_level="L2")
    d["columns"] = [x for x in d["columns"] if x["id"] != "C2B"] + [lower, upper]
    T_split = TR.build_tables(d)
    n_split = sum(1 for r in T_split["COLS"] if r[5].startswith("C2B"))
    d["diaphragm_exclude"] = [{"x": "2", "y": "B", "level": "L2"}]
    T = TR.build_tables(d)
    n = sum(1 for r in T["COLS"] if r[5].startswith("C2B"))
    assert n_split == 3 and n == 2                     # Base-L1, L1-L3 (L1 is framed)
    assert any("joined through L2" in x["what"] for x in T["derived"])


def test_openings_leave_the_floor_plate():
    d = small_building()
    T0 = TR.build_tables(d)
    d["openings"] = [{"level": "L2", "x": "2", "y": "A"}]
    T1 = TR.build_tables(d)
    assert len(T1["floor_bays"][2]) == len(T0["floor_bays"][2]) - 1 and (1, 0) not in T1["floor_bays"][2]
    assert T1["floor_bays"][1] == T0["floor_bays"][1]
    assert any("opening" in x["what"] for x in T1["derived"])


# ------------------------------------------------------------------------------------------- template / e2k
def test_template_round_trip(tmp_path):
    d = small_building()
    p = XL.write_template(tmp_path / "t.xlsx", d)
    back = XL.read_template(p)
    assert S.validate(back)["ready"], [i for i in S.validate(back)["items"] if i["kind"] != "info"]
    a, b = TR.build_tables(d), TR.build_tables(back)
    for k in ("COLS", "BEAMS", "BRACES", "FIX"):
        assert a[k] == b[k], k


def test_blank_template_reads_as_all_questions(tmp_path):
    p = XL.write_template(tmp_path / "blank.xlsx")
    v = S.validate(XL.read_template(p))
    assert not v["ready"] and v["counts"]["key"] >= 5


def _write_e2k(d, path):
    """Minimal ETABS text model of a description (kip-ft), in ETABS's own conventions."""
    G = S.Geometry(d)
    L = ["$ PROGRAM INFORMATION", '  PROGRAM  "ETABS"  VERSION "21.0.0"', "", "$ CONTROLS", '  UNITS  "KIP"  "FT"  "F"', "",
         "$ STORIES - IN SEQUENCE FROM TOP"]
    lv = d["levels"]
    for k in range(len(lv) - 1, 0, -1):
        L.append('  STORY "%s"  HEIGHT %g' % (lv[k]["name"], lv[k]["elev_ft"] - lv[k - 1]["elev_ft"]))
    L.append('  STORY "%s"  ELEV %g' % (lv[0]["name"], lv[0]["elev_ft"]))
    L += ["", "$ GRIDS", '  GRIDSYSTEM "G1"  TYPE "CARTESIAN"']
    for ax in ("x", "y"):
        for g in d["grid"][ax]:
            L.append('  GRID "G1"  LABEL "%s"  DIR "%s"  COORD %g' % (g["label"], ax.upper(), g["coord_ft"]))
    L += ["", "$ DIAPHRAGM NAMES", '  DIAPHRAGM "D1"    TYPE RIGID', "", "$ MATERIAL PROPERTIES",
          '  MATERIAL  "A992Fy50"    TYPE "Steel"    WEIGHTPERVOLUME 0.49', '  MATERIAL  "A992Fy50"  FY 7200  FU 9360',
          '  MATERIAL  "A500GrC"    TYPE "Steel"    WEIGHTPERVOLUME 0.49', '  MATERIAL  "A500GrC"  FY 7200', "", "$ FRAME SECTIONS"]
    secs = sorted({m.get("section") or "BRB" for m in d["columns"] + d["beams"] + d["braces"]})
    for s in secs:
        L.append('  FRAMESECTION  "%s"  MATERIAL "%s"  SHAPE "Steel I/Wide Flange"' % (s, "A500GrC" if s.startswith("HSS") else "A992Fy50"))
    pts = {}
    def pt(x, y):
        key = (round(x, 4), round(y, 4))
        if key not in pts:
            pts[key] = str(len(pts) + 1)
        return pts[key]
    lines, assigns = [], []
    for c in d["columns"]:
        x, y = G.xy(c)
        p = pt(x, y)
        k0, k1 = [n for n, _ in G.levels].index(c["base_level"]), [n for n, _ in G.levels].index(c["top_level"])
        lines.append('  LINE  "%s"  COLUMN  "%s"  "%s"  1' % (c["id"], p, p))
        for k in range(k0 + 1, k1 + 1):
            cr = str(c.get("release") or "none")
            rel = []
            if cr in ("base", "both") and k == k0 + 1:
                rel += ["M2I", "M3I"]
            if cr in ("top", "both") and k == k1:
                rel += ["M2J", "M3J"]
            assigns.append('  LINEASSIGN  "%s"  "%s"  SECTION "%s"  ANG  %d%s' % (c["id"], lv[k]["name"], c["section"],
                           0 if c.get("strong_axis", "X") == "X" else 90, (' RELEASE "%s"' % " ".join(rel)) if rel else ""))
    for b in d["beams"]:
        (x1, y1), (x2, y2) = G.xy(b["i"]), G.xy(b["j"])
        lines.append('  LINE  "%s"  BEAM  "%s"  "%s"  0' % (b["id"], pt(x1, y1), pt(x2, y2)))
        rel = {"none": "", "I": ' RELEASE "M2I M3I"', "J": ' RELEASE "M2J M3J"', "both": ' RELEASE "PINNED"'}[b["release"]]
        assigns.append('  LINEASSIGN  "%s"  "%s"  SECTION "%s"%s' % (b["id"], b["level"], b["section"], rel))
    names = [n for n, _ in G.levels]
    for br in d["braces"]:
        lo, hi = br["i"], br["j"]
        if names.index(lo["level"]) > names.index(hi["level"]):
            lo, hi = hi, lo                        # ETABS: the second point sits at the story the line is assigned to
        (x1, y1), (x2, y2) = G.xy(lo), G.xy(hi)
        n_ = names.index(hi["level"]) - names.index(lo["level"])      # stories the line spans
        lines.append('  LINE  "%s"  BRACE  "%s"  "%s"  %d' % (br["id"], pt(x1, y1), pt(x2, y2), n_))
        assigns.append('  LINEASSIGN  "%s"  "%s"  SECTION "%s"' % (br["id"], hi["level"], br.get("section") or "BRB"))
    L += ["", "$ POINT COORDINATES"] + ['  POINT "%s"  %g %g' % (n, x, y) for (x, y), n in pts.items()]
    L += ["", "$ LINE CONNECTIVITIES"] + lines
    L += ["", "$ POINT ASSIGNS"]
    sup = d.get("supports") or {}
    fix_at = {(str(a.get("x")), str(a.get("y"))): a.get("fixity") for a in sup.get("at") or []}
    for c in d["columns"]:
        if c["base_level"] == lv[0]["name"]:
            f = fix_at.get((str(c["x"]), str(c["y"])), sup.get("default") or "fixed")
            L.append('  POINTASSIGN  "%s"  "%s"  RESTRAINT "%s"' % (pt(*G.xy(c)), lv[0]["name"],
                                                                   "UX UY UZ RX RY RZ" if f == "fixed" else "UX UY UZ"))
    framed = {}                                   # the joints the description puts in each level's diaphragm
    for c in d["columns"]:
        for lvn in (c["base_level"], c["top_level"]):
            framed.setdefault(lvn, set()).add(pt(*G.xy(c)))
    for b in d["beams"]:
        for e in ("i", "j"):
            framed.setdefault(b["level"], set()).add(pt(*G.xy(b[e])))
    for br in d["braces"]:
        for e in ("i", "j"):
            framed.setdefault(br[e]["level"], set()).add(pt(*G.xy(br[e])))
    ex = {(pt(*G.xy(e)), e["level"]) for e in (d.get("diaphragm_exclude") or []) if G.xy(e)}
    for lvn, ps in framed.items():
        if lvn == lv[0]["name"]:
            continue
        for p_ in sorted(ps):
            if (p_, lvn) not in ex:
                L.append('  POINTASSIGN  "%s"  "%s"  DIAPH "D1"' % (p_, lvn))
    L += ["", "$ LINE ASSIGNS"] + assigns
    L += ["", "$ LOAD PATTERNS", '  LOADPATTERN "Dead"  TYPE  "Dead"  SELFWEIGHT  1', '  LOADPATTERN "Live"  TYPE  "Live"  SELFWEIGHT  0',
          "", "$ STEEL DESIGN PREFERENCES", '  STEELPREFERENCE  CODE "AISC 360-22"  FRAMETYPE "SMF"',
          '  STEELPREFERENCE  SDC "D"  IMPORTANCEFACTOR 1 SYSTEMRHO 1 SYSTEMSDS 1', "", "$ END OF MODEL FILE"]
    pathlib.Path(path).write_text("\n".join(L))
    return path


def test_e2k_intake_reads_geometry_and_asks_the_rest(tmp_path):
    d = small_building()
    p = _write_e2k(d, tmp_path / "small.e2k")
    e = E2K.read_e2k(p)
    assert len(e["columns"]) == 36 and len(e["beams"]) == len(d["beams"]) and len(e["braces"]) == 12
    assert e["supports"] == {"default": "fixed"} and e["diaphragm"] == "rigid"
    assert {c["strong_axis"] for c in e["columns"]} == {"X", "Y"}
    assert e["materials"]["W"] == "A992" and e["materials"]["HSS"] == "A500 GR C"
    rel = {b["id"]: b["release"] for b in e["beams"]}
    assert rel["BX1@L1"] == "none" and rel["BY10@L1"] == "both"        # ETABS objects are named line@story
    v = S.validate(e)
    kinds = {i["id"]: i["kind"] for i in v["items"]}
    # a building with braces whose design preferences name one frame type: the system is asked, never assumed
    assert kinds.get("seismic.system.X") == "key" and kinds.get("seismic.system.Y") == "key"
    assert kinds.get("loads.D_psf") == "key"                 # no floor area objects -> D is asked
    assert kinds.get("seismic.SD1") == "key"
    # the same model as the description once the basis is answered
    ans = {"seismic.system.X": {"value": "SMF"}, "seismic.system.Y": {"value": "SCBF"}}
    e2 = S.apply_answers(e, ans)
    for k in ("SD1", "S1", "TL", "risk_category", "site_class", "analysis", "R", "Cd", "Om0", "rho"):
        e2["seismic"][k] = copy.deepcopy(d["seismic"][k])
    e2["loads"] = copy.deepcopy(d["loads"]); e2["details"] = {"doublers": []}; e2["project"]["code"] = "ASCE 7-22"
    assert S.validate(e2)["ready"], [i for i in S.validate(e2)["items"] if i["kind"] != "info"]
    a, b = TR.build_tables(d), TR.build_tables(e2)
    assert len(a["COLS"]) == len(b["COLS"]) and len(a["BEAMS"]) == len(b["BEAMS"]) and len(a["BRACES"]) == len(b["BRACES"])
    assert sorted(c[3:5] for c in a["COLS"]) == sorted(c[3:5] for c in b["COLS"])


def test_e2k_unsupported_items_are_refused(tmp_path):
    d = small_building()
    p = _write_e2k(d, tmp_path / "small.e2k")
    t = p.read_text().replace('  POINTASSIGN  "1"  "Base"  RESTRAINT "UX UY UZ RX RY RZ"',
                              '  POINTASSIGN  "1"  "Base"  RESTRAINT "UX UY UZ RX RY RZ"  SPRINGPROP "S1"')
    p.write_text(t)
    e = E2K.read_e2k(p)
    assert any("spring" in u for u in e.get("_unsupported") or [])
    assert any(i["kind"] == "refused" for i in S.validate(e)["items"])


# ------------------------------------------------------------------------------------------- the pipeline
@pytest.fixture(scope="module")
def built():
    td = pathlib.Path(tempfile.mkdtemp(prefix="bo-test-"))
    job = td / "small"
    res = RN.build(job, small_building(), name="small")
    yield job, res
    shutil.rmtree(td, ignore_errors=True)


def test_build_runs_the_hr_pipeline(built):
    job, res = built
    assert res["status"] == "built", res
    for f in ("cfg.py", "provenance.json", "model_opensees.py", "model_static.py", "report.html", "design/calc_package.json",
              "design/member_schedule.csv", "missing_items.json", "building_description.json"):
        assert (job / f).exists(), f
    assert res["model_declared"] and res["model_consistent"]
    calc = json.loads((job / "design" / "calc_package.json").read_text())
    assert calc["build_only"] and all(m.get("build_only") for m in calc["members"])
    assert all(m.get("DC") is None for m in calc["members"])     # never checked by HR
    assert RN.gate(job) == []


@pytest.mark.parametrize("script", ["model_opensees.py", "model_static.py"])
def test_the_exported_models_run_standalone(built, script):
    """`python model_opensees.py` / `python model_static.py` rebuild the model on their own: the custom_build runs
    twice (probe + real build), and only the final model is written -- replaying both left the second without its
    geomTransf and the script stopped at its first element."""
    job, res = built
    src = (job / script).read_text()
    assert src.count("ops.wipe()") == 1
    p = subprocess.run([sys.executable, str(job / script)], capture_output=True, text=True, cwd=str(job), timeout=600)
    assert p.returncode == 0, (p.stderr or p.stdout)[-2000:]
    n_el = int(re.search(r"elements:\s*(\d+)", p.stdout).group(1))
    assert n_el == src.count("ops.element(")
    if script == "model_opensees.py":
        T = [float(x) for x in re.findall(r"[\d.]+", p.stdout.split("periods T (s):")[1])]
        assert T and abs(T[0] - res["summary"]["T"][0]) < 0.005 * res["summary"]["T"][0]


def test_gate_refuses_an_edited_cfg(built):
    job, _ = built
    p = job / "cfg.py"
    orig = p.read_text()
    try:
        p.write_text(orig.replace("'W24X76'", "'W27X94'", 1))
        probs = RN.gate(job)
        assert any("never redesigned" in x for x in probs)
    finally:
        p.write_text(orig)
    assert RN.gate(job) == []


def test_needs_input_writes_no_cfg(tmp_path):
    d = small_building()
    d["columns"][3]["section"] = None
    res = RN.build(tmp_path / "x", d, name="x", run_pipeline=False)
    assert res["status"] == "needs_input" and not (tmp_path / "x" / "cfg.py").exists()
    mi = json.loads((tmp_path / "x" / "missing_items.json").read_text())
    assert not mi["ready"] and mi["counts"]["key"] == 1


def test_agent_tools_refuse_build_only_files(built):
    job, _ = built
    pytest.importorskip("httpx")
    from steltic.job_tools import JobWorkspace
    ws = JobWorkspace(job.parent)
    ws.building = job.name
    r = ws.write_file("cfg.py", "cfg = {}")
    assert "error" in r and "BUILD-ONLY" in r["error"]
    from steltic import agent
    assert agent._completion_gate(ws) == []


def test_api_flow(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from steltic.build_only.api import make_router
    import time
    app = FastAPI()
    app.include_router(make_router(lambda: tmp_path, lambda s: "".join(c for c in s if c.isalnum() or c in "_-")))
    c = TestClient(app)
    d = small_building()
    d["loads"].pop("Lr_psf")
    r = c.post("/api/build_only/intake", json={"building": "api1", "description": d})
    assert r.status_code == 200, r.text
    mi = r.json()["missing_items"]
    assert not mi["ready"] and mi["counts"]["default"] == 1
    assert c.post("/api/build_only/build", json={"building": "api1"}).status_code == 409
    r = c.post("/api/build_only/answers", json={"building": "api1", "answers": {"loads.Lr_psf": {"accept": True}}})
    assert r.json()["missing_items"]["ready"]
    assert c.post("/api/build_only/build", json={"building": "api1"}).status_code == 202
    for _ in range(240):
        st = c.get("/api/build_only/status/api1").json()
        if st["state"] in ("built", "failed"):
            break
        time.sleep(0.5)
    assert st["state"] == "built", st
    assert st["status"]["gate"] == []
    assert any(a["item"] == "loads.Lr_psf" for a in st["provenance"]["accepted_defaults"])
    t = c.get("/api/build_only/template", params={"building": "api1"})
    assert t.status_code == 200 and t.content[:2] == b"PK"


# ------------------------------------------------------------------------------------------- ETABS 9 text dialect
_E2K_V9 = """$ File C:\\\\models\\\\v9.e2k saved 3/15/11 9:29:10
$ PROGRAM INFORMATION
  PROGRAM  "ETABS"  VERSION "9.7.2"

$ CONTROLS
  UNITS  "KIP"  "IN"

$ STORIES - IN SEQUENCE FROM TOP
  STORY "STORY2"  HEIGHT 144  MASTERSTORY "Yes"
  STORY "STORY1"  HEIGHT 144  SIMILARTO "STORY2"
  STORY "BASE"  ELEV 0

$ DIAPHRAGM NAMES
  DIAPHRAGM "D1"    TYPE RIGID

$ GRIDS
  COORDSYSTEM "GLOBAL"  TYPE "GENERAL"  BUBBLESIZE 60
  GENGRID "GLOBAL"  LABEL "A"  TYPE "LINE"  XA 0  YA 0  XB 0  YB 288  GRIDTYPE "PRIMARY"
  GENGRID "GLOBAL"  LABEL "B"  TYPE "LINE"  XA 288  YA 0  XB 288  YB 288  GRIDTYPE "PRIMARY"
  GENGRID "GLOBAL"  LABEL "1"  TYPE "LINE"  XA 0  YA 0  XB 288  YB 0  GRIDTYPE "PRIMARY"
  GENGRID "GLOBAL"  LABEL "2"  TYPE "LINE"  XA 0  YA 288  XB 288  YB 288  GRIDTYPE "PRIMARY"
  GENGRID "GLOBAL"  LABEL "S1"  TYPE "LINE"  XA 0  YA 0  XB 288  YB 288  GRIDTYPE "PRIMARY"

$ MATERIAL PROPERTIES
  MATERIAL  "STEEL"  M 7.324016E-07  W 0.000283  TYPE "ISOTROPIC"  E 29000  U 0.3  A 6.5E-06
  MATERIAL  "STEEL"  DESIGNTYPE "STEEL"  FY 50  FU 65  PRICE 1
  MATERIAL  "CONC"  M 2.246377E-07  W 0.0000868  TYPE "ISOTROPIC"  E 3600  U 0.2  A 5.5E-06
  MATERIAL  "CONC"  DESIGNTYPE "CONCRETE"  FY 60  FC 4  FYS 60

$ FRAME SECTIONS
  FRAMESECTION  "W14X90"  MATERIAL "STEEL"  SHAPE "W14X90"
  FRAMESECTION  "W18X35"  MATERIAL "STEEL"  SHAPE "W18X35"

$ WALL/SLAB/DECK PROPERTIES
  SHELLPROP  "DECK1"  PROPTYPE "DECK"  TYPE "MEMBRANE"
  SHELLPROP  "DECK1"  DECKTYPE "FILLED"  CONCMATERIAL "CONC"  SLABDEPTH 3.5
  SHELLPROP  "DECK1"  RIBDEPTH 3  RIBWIDTH 6  RIBSPACING 12  WEIGHT 1.597222E-05

$ POINT COORDINATES
  POINT "1"  0 0
  POINT "2"  288 0
  POINT "3"  288 288
  POINT "4"  0 288

$ LINE CONNECTIVITIES
  LINE  "C1"  COLUMN  "1"  "1"  1
  LINE  "C2"  COLUMN  "2"  "2"  1
  LINE  "C3"  COLUMN  "3"  "3"  1
  LINE  "C4"  COLUMN  "4"  "4"  1
  LINE  "B1"  BEAM  "1"  "2"  0
  LINE  "B2"  BEAM  "2"  "3"  0
  LINE  "B3"  BEAM  "3"  "4"  0
  LINE  "B4"  BEAM  "4"  "1"  0

$ AREA CONNECTIVITIES
  AREA "F1"  FLOOR  4  "1"  "2"  "3"  "4"  0  0  0  0

$ POINT ASSIGNS
  POINTASSIGN  "1"  "BASE"  RESTRAINT "UX UY UZ RX RY RZ"
  POINTASSIGN  "2"  "BASE"  RESTRAINT "UX UY UZ RX RY RZ"
  POINTASSIGN  "3"  "BASE"  RESTRAINT "UX UY UZ RX RY RZ"
  POINTASSIGN  "4"  "BASE"  RESTRAINT "UX UY UZ RX RY RZ"

$ LINE ASSIGNS
%(lines)s

$ AREA ASSIGNS
  AREAASSIGN  "F1"  "STORY2"  SECTION "DECK1"  OBJMESHTYPE "DEFAULT"  DIAPH  "D1"
  AREAASSIGN  "F1"  "STORY1"  SECTION "DECK1"  OBJMESHTYPE "DEFAULT"  DIAPH  "D1"

$ STATIC LOADS
  LOADCASE "DEAD"  TYPE  "DEAD"  SELFWEIGHT  1
  LOADCASE "LIVE"  TYPE  "LIVE"  SELFWEIGHT  0
  LOADCASE "SDEAD"  TYPE  "SUPERDEAD"  SELFWEIGHT  0

$ AREA OBJECT LOADS
%(loads)s

$ END OF MODEL FILE
"""


def _v9(loads=True):
    lines = []
    for st in ("STORY2", "STORY1"):
        for c in ("C1", "C2", "C3", "C4"):
            lines.append('  LINEASSIGN  "%s"  "%s"  SECTION "W14X90"  ANG  0' % (c, st))
        for b in ("B1", "B2", "B3", "B4"):
            lines.append('  LINEASSIGN  "%s"  "%s"  SECTION "W18X35"  ANG  0' % (b, st))
    ld = []
    if loads:
        for st in ("STORY2", "STORY1"):
            ld.append('  AREALOAD  "F1"  "%s"  TYPE "UNIFF"  DIR "GRAV"  LC "LIVE"  FVAL 5.208334E-04' % st)
            ld.append('  AREALOAD  "F1"  "%s"  TYPE "UNIFF"  DIR "GRAV"  LC "SDEAD"  FVAL 1.736111E-04' % st)
    return _E2K_V9 % dict(lines="\n".join(lines), loads="\n".join(ld))


def test_e2k_etabs9_dialect_loads_decks_and_general_grids(tmp_path):
    p = tmp_path / "v9.e2k"
    p.write_text(_v9())
    e = E2K.read_e2k(p)
    assert [g["label"] for g in e["grid"]["x"]] == ["A", "B"] and [g["label"] for g in e["grid"]["y"]] == ["1", "2"]
    assert any("S1" in n and "skewed" in n for n in e["_intake"]["notes"])
    assert len(e["columns"]) == 8 and len(e["beams"]) == 8 and e["diaphragm"] == "rigid"
    D = e["_proposals"]["loads/D_psf"]["value"]
    L = e["_proposals"]["loads/L_psf"]["value"]
    # 25 psf superimposed + 150 pcf x (3.5 + 3 x 6/12) in of filled deck + 2.3 psf deck + framing self-weight
    deck = 0.0000868 * 1728 * 1000 * 5.0 / 12.0 + 1.597222e-05 * 144000
    assert L == {"STORY1": 75.0, "STORY2": 75.0}
    for lv in ("STORY1", "STORY2"):
        assert abs(D[lv] - (25.0 + deck + (4 * 12 * 90 + 4 * 24 * 35) / 576.0)) < 0.1     # 4 W14X90 x 12 ft + 4 W18X35 x 24 ft
    assert "64.8 slab/deck" in e["_proposals"]["loads/D_psf"]["reason"]


def test_e2k_etabs9_seismic_lines_under_static_loads(tmp_path):
    """ETABS 9 writes the auto-seismic parameters under STATIC LOADS (IBC 2003 / IBC 2006 patterns, as the CSI
    training models do): the mapped S1, TL and the site class are read there; SDS / SD1 (ETABS derives them from Ss
    and the site coefficients) are not invented."""
    t = _v9().replace('  LOADCASE "SDEAD"  TYPE  "SUPERDEAD"  SELFWEIGHT  0',
                      '  LOADCASE "SDEAD"  TYPE  "SUPERDEAD"  SELFWEIGHT  0\n'
                      '  LOADCASE "EQX"  TYPE  "QUAKE"  SELFWEIGHT  0\n'
                      '  SEISMIC "EQX"  "IBC2006"    DIR "X"  TOPSTORY "STORY2"    BOTTOMSTORY "BASE"   PERIODTYPE "PROGCALC"   '
                      'CT 0.028  SITECLASS "D"  Ss 1.5  S1 0.5  TL 8  R 8  OMEGA 3  CD 5.5  I 1.5\n'
                      '  LOADCASE "WX"  TYPE  "WIND"  SELFWEIGHT  0\n'
                      '  WIND "WX"  "ASCE7-05"  ANG 0  WINDWARD 0.8  LEEWARD 0.5  SPEED 110  EXPOSURE "C"')
    p = tmp_path / "v9.e2k"
    p.write_text(t)
    se = E2K.read_e2k(p)["seismic"]
    assert se["S1"] == 0.5 and se["TL"] == 8.0 and se["site_class"] == "D"
    assert "SDS" not in se and "SD1" not in se


def test_e2k_floor_without_loads_is_asked_not_proposed_as_zero(tmp_path):
    p = tmp_path / "v9.e2k"
    p.write_text(_v9(loads=False).replace('SECTION "DECK1"', 'SECTION "NONE"'))
    e = E2K.read_e2k(p)
    assert "loads/D_psf" not in (e.get("_proposals") or {})
    assert any("no dead load found" in n for n in e["_intake"]["notes"])
    kinds = {i["id"]: i["kind"] for i in S.validate(e)["items"]}
    assert kinds.get("loads.D_psf") == "key" and kinds.get("loads.L_psf") == "key"


def test_e2k_framed_bay_without_a_floor_object_is_an_opening(tmp_path):
    """A 2 x 1 bay frame whose floor object covers only the first bay: the second bay is an opening (a court)."""
    t = _v9()
    t = t.replace('  GENGRID "GLOBAL"  LABEL "B"  TYPE "LINE"  XA 288  YA 0  XB 288  YB 288  GRIDTYPE "PRIMARY"',
                  '  GENGRID "GLOBAL"  LABEL "B"  TYPE "LINE"  XA 288  YA 0  XB 288  YB 288  GRIDTYPE "PRIMARY"\n'
                  '  GENGRID "GLOBAL"  LABEL "C"  TYPE "LINE"  XA 576  YA 0  XB 576  YB 288  GRIDTYPE "PRIMARY"')
    t = t.replace('  POINT "4"  0 288', '  POINT "4"  0 288\n  POINT "5"  576 0\n  POINT "6"  576 288')
    t = t.replace('  LINE  "B4"  BEAM  "4"  "1"  0', '  LINE  "B4"  BEAM  "4"  "1"  0\n  LINE  "C5"  COLUMN  "5"  "5"  1\n'
                  '  LINE  "C6"  COLUMN  "6"  "6"  1\n  LINE  "B5"  BEAM  "2"  "5"  0\n  LINE  "B6"  BEAM  "3"  "6"  0')
    extra = []
    for st in ("STORY2", "STORY1"):
        extra += ['  LINEASSIGN  "C5"  "%s"  SECTION "W14X90"  ANG  0' % st, '  LINEASSIGN  "C6"  "%s"  SECTION "W14X90"  ANG  0' % st,
                  '  LINEASSIGN  "B5"  "%s"  SECTION "W18X35"  ANG  0' % st, '  LINEASSIGN  "B6"  "%s"  SECTION "W18X35"  ANG  0' % st]
    t = t.replace("\n\n$ AREA ASSIGNS", "\n" + "\n".join(extra) + "\n\n$ AREA ASSIGNS")
    t = t.replace('  POINTASSIGN  "4"  "BASE"  RESTRAINT "UX UY UZ RX RY RZ"',
                  '  POINTASSIGN  "4"  "BASE"  RESTRAINT "UX UY UZ RX RY RZ"\n  POINTASSIGN  "5"  "BASE"  RESTRAINT "UX UY UZ RX RY RZ"\n'
                  '  POINTASSIGN  "6"  "BASE"  RESTRAINT "UX UY UZ RX RY RZ"')
    p = tmp_path / "court.e2k"
    p.write_text(t)
    e = E2K.read_e2k(p)
    assert len(e["columns"]) == 12
    assert sorted((o["level"], o["x"], o["y"]) for o in e["openings"]) == [("STORY1", "B", "1"), ("STORY2", "B", "1")]
    assert any("read as openings" in n for n in e["_intake"]["notes"])
    assert not [i for i in S.validate(e)["items"] if i["kind"] == "refused"]
    assert "64.8 slab/deck" in e["_proposals"]["loads/D_psf"]["reason"]        # the loads still come from the floor

"""ASCE 7-22 16.1.2 drift relief (cfg['drift_relief_16_1_2']) -- the rules preflight and consistency apply, and the
/api/restore archive flag. Engine-free: run with `python -m pytest tests -q` from the repo root."""
import copy, io, json, os, sys, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "steel_engine")); sys.path.insert(0, ROOT)

import preflight as P  # noqa: E402

BASE = dict(heights=[192.0, 168.0, 168.0], SX=360.0, SY=360.0, NX=4, NY=3, system="SMF", rho=1.0,
            model={"bases": "fixed", "joints": "rigid", "gravity": "two-way"},
            seis=dict(SDS=1.0, SD1=0.6, S1=0.6, R=8.0, Cd=5.5, Ie=1.25), drift_limit=0.0112)
RELIEF = dict(clause="ASCE 7-22 16.1.2", nlrha_job="Ex", nlrha_run="2026-09-14", nlrha_mean_drift=0.0146, nlrha_limit=0.03,
              nlrha_verdict="ACCEPTABLE", nlrha_records=11, linear_target=0.0112, risk_category="III")


def _cfg(**kw):
    c = copy.deepcopy(BASE); c.update(kw); return c


def _errors(cfg):
    return [m for s, m in P.check(cfg) if s == "ERROR"]


def test_relief_lifts_the_rc_iii_table_error():
    assert any("Table 12.12-1 requires 0.015" in m for m in _errors(_cfg(drift_limit=0.0168)))         # without relief
    c = _cfg(drift_limit=0.0168, drift_relief_16_1_2=dict(RELIEF, linear_target=0.0168))
    assert not _errors(c) and P.relief_active(c)
    assert any("16.1.2 drift relief in force" in m for s, m in P.check(c) if s == "WARN")


def test_relief_never_applies_to_risk_category_iv():
    c = _cfg(drift_limit=0.0112, drift_relief_16_1_2=dict(RELIEF)); c["seis"]["Ie"] = 1.5
    errs = _errors(c)
    assert any("Risk Category IV" in m and "16.1.2" in m for m in errs) and any("requires 0.010" in m for m in errs)
    assert not P.relief_active(c)


def test_relief_block_must_carry_the_chapter_16_numbers():
    c = _cfg(drift_relief_16_1_2={"clause": "x"})
    assert any("missing" in m for m in _errors(c)) and not P.relief_active(c)
    c = _cfg(drift_limit=0.035, drift_relief_16_1_2=dict(RELIEF, linear_target=0.035))
    assert any("exceeds the Chapter 16 mean-drift limit" in m for m in _errors(c))
    c = _cfg(drift_relief_16_1_2=dict(RELIEF, nlrha_mean_drift=0.031))
    assert any("did not pass 16.4.1.2" in m for m in _errors(c))
    c = _cfg(drift_limit=0.0120, drift_relief_16_1_2=dict(RELIEF))
    assert any("differs from the relief's linear_target" in m for s, m in P.check(c) if s == "WARN")


def test_consistency_mirrors_the_rules():
    import consistency as C
    src = open(os.path.join(ROOT, "steel_engine", "consistency.py"), encoding="utf-8").read()
    assert "relief_findings" in src and "_relief_on" in src


def test_restore_archives_and_keeps_the_viewer():
    from fastapi.testclient import TestClient
    import steltic.main as M
    client = TestClient(M.app)
    def zip_of(building, extra=None):
        buf = io.BytesIO()
        files = {"cfg.py": "cfg = dict()\n", "conversation.json": "[]", "viewer_3d.html": "<html>viewer</html>", "report.html": "<html>r</html>",
                 "design/calc_package.json": "{}", "nlrha/nlrha_package.json": "{}"}
        files.update(extra or {})
        with zipfile.ZipFile(buf, "w") as z:
            for k, v in files.items():
                z.writestr("%s/%s" % (building, k), v)
        return buf.getvalue()
    b = "ReliefTestJob"
    r = client.post("/api/restore/%s" % b, content=zip_of(b)); assert r.status_code == 200 and r.json()["archived"] is None
    jd = M.JOBS_DIR / b
    assert (jd / "viewer_3d.html").exists() and not (jd / "nlrha").exists()               # viewer restored, analyses never
    r = client.post("/api/restore/%s?archive=1" % b, content=zip_of(b, {"cfg.py": "cfg = dict(v=2)\n"})); d = r.json()
    assert d["archived"] and d["archived"].startswith(b + "__") and (M.JOBS_DIR / d["archived"] / "cfg.py").exists()
    assert "v=2" in (jd / "cfg.py").read_text()
    r = client.post("/api/restore/%s" % b, content=zip_of(b)); assert r.json()["archived"] is None          # no flag, no archive
    import shutil
    shutil.rmtree(jd, ignore_errors=True); shutil.rmtree(M.JOBS_DIR / d["archived"], ignore_errors=True)

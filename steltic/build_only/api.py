"""HTTP endpoints of build-only mode (mounted by steltic.main).

    GET  /api/build_only/template[?building=B]   the building template (.xlsx): blank, or filled from B's description
    POST /api/build_only/intake                  multipart (building, file[, replace]) or JSON {building, description}:
                                                 read the user's file -> description; returns missing_items
    POST /api/build_only/answers                 {building, answers: {item id: {"value": v} | {"accept": true}}}
                                                 -> merged into answers.json; returns the remaining missing_items
    POST /api/build_only/build                   {building}: start the build (background); 409 while items are open
    GET  /api/build_only/status/{building}       state (idle / running / needs_input / built / failed), missing
                                                 items, the build result and the provenance summary

A job folder written here is a build-only job (provenance.json once built). Intake refuses to overwrite a normal
HR design of the same name unless replace is set (the old folder is then archived, as /api/run does).
"""
from __future__ import annotations

import datetime
import io
import json
import pathlib
import threading
import time
import traceback

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from . import schema as S
from . import runner as RN

INTAKE = "_intake"
RAW = "description_raw.json"
STATUS = "build_only_status.json"
_locks = {}
_running = set()
_guard = threading.Lock()


def _load(p, default=None):
    try:
        return json.loads(pathlib.Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _dump(p, obj):
    pathlib.Path(p).write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")


def read_any(path: pathlib.Path) -> dict:
    """The user's file -> description, by extension."""
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        from .intake_xlsx import read_template
        return read_template(path)
    if ext in (".e2k", ".$et"):
        from .intake_e2k import read_e2k
        return read_e2k(path)
    if ext == ".json":
        d = json.loads(path.read_text(encoding="utf-8"))
        if d.get("schema") != S.SCHEMA:
            raise ValueError("JSON file is not a steltic building description (schema %r)" % d.get("schema"))
        d.setdefault("_provenance", {})
        return d
    raise ValueError("unsupported file type %r: give the Steltic building template (.xlsx), an ETABS model (.e2k) or a "
                     "building description (.json)" % ext)


def summary(desc: dict) -> dict:
    return dict(source=desc.get("source"), levels=len(desc.get("levels") or []),
                grid=dict(x=len((desc.get("grid") or {}).get("x") or []), y=len((desc.get("grid") or {}).get("y") or [])),
                columns=len(desc.get("columns") or []), beams=len(desc.get("beams") or []), braces=len(desc.get("braces") or []),
                notes=((desc.get("_intake") or {}).get("notes") or []) + (desc.get("_extract_notes") or []))


def state(job: pathlib.Path) -> dict:
    st = _load(job / STATUS, {}) or {}
    mi = _load(job / "missing_items.json")
    pv = _load(job / "provenance.json")
    name = job.name
    running = name in _running
    s = "running" if running else st.get("state") or ("needs_input" if mi and not mi.get("ready") else ("ready" if mi else "idle"))
    out = dict(building=name, state=s, status=st, missing_items=mi,
               files={f: (job / f).exists() for f in ("cfg.py", "report.html", "model_opensees.py", "provenance.json", "viewer_3d.html")})
    if pv:
        out["provenance"] = dict(accepted_defaults=pv.get("accepted_defaults"), derived=pv.get("derived"),
                                 members=pv.get("members"), pipeline=pv.get("pipeline"), cfg_sha256=pv.get("cfg_sha256"))
    return out


def make_router(jobs_dir_fn, clean_name) -> APIRouter:
    r = APIRouter()

    def job_of(building, create=False):
        b = clean_name(building or "")
        if not b:
            raise HTTPException(400, "building name required")
        base = pathlib.Path(jobs_dir_fn()).resolve()
        j = (base / b).resolve()
        if base not in j.parents:
            raise HTTPException(403, "path escapes the jobs folder")
        if create:
            j.mkdir(parents=True, exist_ok=True)
        return j

    @r.get("/api/build_only/template")
    async def template(building: str = ""):
        from .intake_xlsx import write_template
        desc = None
        if building:
            j = job_of(building)
            desc = _load(j / "building_description.json") or _load(j / INTAKE / RAW)
        buf = io.BytesIO()
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "steltic_building_template.xlsx"
            write_template(p, desc)
            data = p.read_bytes()
        name = ("%s_building.xlsx" % clean_name(building)) if building else "steltic_building_template.xlsx"
        return Response(content=data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": 'attachment; filename="%s"' % name})

    @r.post("/api/build_only/intake")
    async def intake(request: Request):
        ctype = request.headers.get("content-type", "")
        replace = False
        if ctype.startswith("multipart/"):
            form = await request.form()
            building = form.get("building") or ""
            replace = str(form.get("replace") or "").lower() in ("1", "true", "yes")
            up = form.get("file")
            if up is None or not getattr(up, "filename", None):
                raise HTTPException(400, "file required")
            fname = pathlib.Path(up.filename).name
            data = await up.read()
            desc_in = None
        else:
            body = await request.json()
            building = body.get("building") or ""
            replace = bool(body.get("replace"))
            desc_in = body.get("description")
            if not isinstance(desc_in, dict):
                raise HTTPException(400, "description (object) or a multipart file required")
            fname, data = "description.json", json.dumps(desc_in).encode()
        j = job_of(building)
        if j.exists() and not RN.is_build_only(j) and not (j / INTAKE).exists() and any(j.iterdir()):
            if not replace:
                raise HTTPException(409, "job '%s' exists and is not a user-designed (build-only) job; choose another name "
                                         "or set replace to archive it" % j.name)
            j.rename(j.parent / (j.name + "__" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")))
        j.mkdir(parents=True, exist_ok=True)
        (j / INTAKE).mkdir(exist_ok=True)
        src = j / INTAKE / fname
        src.write_bytes(data)
        try:
            desc = read_any(src)
        except Exception as ex:                                     # noqa: BLE001
            raise HTTPException(422, "could not read %s: %s" % (fname, ex))
        _dump(j / INTAKE / RAW, desc)
        answers = _load(j / "answers.json", {}) or {}
        d = S.apply_answers(desc, answers)
        mi = S.missing_items_doc(d, j.name)
        _dump(j / "missing_items.json", mi)
        _dump(j / "building_description.json", d)
        _dump(j / STATUS, dict(state="needs_input" if not mi["ready"] else "ready", at=time.strftime("%Y-%m-%d %H:%M:%S"),
                               source=fname))
        return JSONResponse(dict(building=j.name, summary=summary(desc), missing_items=mi))

    @r.post("/api/build_only/answers")
    async def answers(request: Request):
        body = await request.json()
        j = job_of(body.get("building"))
        raw = _load(j / INTAKE / RAW)
        if raw is None:
            raise HTTPException(404, "no intake for '%s' yet" % j.name)
        cur = _load(j / "answers.json", {}) or {}
        for k, v in (body.get("answers") or {}).items():
            if v is None:
                cur.pop(k, None)
            elif isinstance(v, dict) and ("value" in v or v.get("accept")):
                cur[k] = v
        _dump(j / "answers.json", cur)
        d = S.apply_answers(raw, cur)
        mi = S.missing_items_doc(d, j.name)
        _dump(j / "missing_items.json", mi)
        _dump(j / "building_description.json", d)
        return JSONResponse(dict(building=j.name, missing_items=mi, unused=d.get("_answers_unused") or []))

    @r.post("/api/build_only/build")
    async def build(request: Request):
        body = await request.json()
        j = job_of(body.get("building"))
        raw = _load(j / INTAKE / RAW)
        if raw is None:
            raise HTTPException(404, "no intake for '%s' yet" % j.name)
        ans = _load(j / "answers.json", {}) or {}
        mi = S.missing_items_doc(S.apply_answers(raw, ans), j.name)
        if not mi["ready"]:
            return JSONResponse(dict(building=j.name, state="needs_input", missing_items=mi), status_code=409)
        with _guard:
            if j.name in _running:
                return JSONResponse(dict(building=j.name, state="running"), status_code=202)
            _running.add(j.name)
        _dump(j / STATUS, dict(state="running", started=time.strftime("%Y-%m-%d %H:%M:%S")))

        def work():
            try:
                res = RN.build(j, raw, ans, name=j.name)
                st = dict(state={"built": "built", "needs_input": "needs_input"}.get(res.get("status"), "failed"),
                          finished=time.strftime("%Y-%m-%d %H:%M:%S"), result={k: v for k, v in res.items() if k != "missing_items"},
                          gate=RN.gate(j) if res.get("status") == "built" else None)
            except Exception as ex:                                 # noqa: BLE001
                st = dict(state="failed", finished=time.strftime("%Y-%m-%d %H:%M:%S"), error="%s\n%s" % (ex, traceback.format_exc()[-1500:]))
            _dump(j / STATUS, st)
            with _guard:
                _running.discard(j.name)
        threading.Thread(target=work, daemon=True).start()
        return JSONResponse(dict(building=j.name, state="running"), status_code=202)

    @r.get("/api/build_only/status/{building}")
    async def status(building: str):
        j = job_of(building)
        if not j.exists():
            raise HTTPException(404, "no job '%s'" % j.name)
        return JSONResponse(state(j))

    return r

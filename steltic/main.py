"""FastAPI app: vanilla frontend + the streamed agent run + report serving.

Local single-user posture: no login, no cookies, no telemetry. The user's LLM key lives in server
memory (session.set_creds), never on disk. The steel engine + model-written code run ONLY in the
sandbox executor. Bind to 127.0.0.1 (the CLI default) -- there is no authentication.
"""
import base64, io, json, os, posixpath, threading, time, zipfile
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from . import config, session, agent
from .job_tools import JobWorkspace, _clean_name
from .sandbox import make_executor

app = FastAPI(title="Steltic")


# Frontend freshness: browsers heuristically cache app.js/index.html for days, so users kept running
# STALE JS after upgrades. no-cache = store but REVALIDATE via ETag each load: a cheap 304 when
# unchanged, a fresh copy the moment a new version serves. Same treatment for /api/report/* --
# report.html/viewer_3d.html change under the same URL on re-renders.
@app.middleware("http")
async def _no_stale_frontend(request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.startswith("/static/") or p.startswith("/api/report/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp

EXECUTOR = make_executor()

# All jobs live under one local session folder (single user).
JOBS_DIR = config.SESSIONS_DIR / "local" / "jobs"


# ---------------- run cancellation (Stop button) ----------------
_cancel_lock = threading.Lock()
_cancelled: set[str] = set()

def _request_cancel(building: str) -> bool:
    with _cancel_lock:
        was_new = building not in _cancelled
        _cancelled.add(building)
    return was_new

def _clear_cancel(building: str):
    with _cancel_lock:
        _cancelled.discard(building)

def _is_cancelled(building: str) -> bool:
    with _cancel_lock:
        return building in _cancelled


def _sse(ev: dict) -> str:
    return "data: " + json.dumps(ev) + "\n\n"


def _clean_images(raw, max_files: int = 3, max_bytes: int = 5 * 1024 * 1024) -> list:
    """Validate optional base64 data-URL images from the client: keep at most `max_files`, each whose
    decoded size is <= `max_bytes`. Returns [{name, type, data_url}]. Anything malformed is dropped."""
    out = []
    if not isinstance(raw, list):
        return out
    for it in raw:
        if len(out) >= max_files:
            break
        if not isinstance(it, dict):
            continue
        url = it.get("data_url") or ""
        if not isinstance(url, str) or not url.startswith("data:image/") or "," not in url:
            continue
        approx = (len(url.split(",", 1)[1]) * 3) // 4      # base64 -> bytes, no need to actually decode
        if approx > max_bytes:
            continue
        out.append({"name": str(it.get("name") or "image")[:120],
                    "type": str(it.get("type") or "image/*")[:60], "data_url": url})
    return out


def _restore_zip_into(base, data: bytes):
    """Validate + unpack a unified-zip's resumable state into the job dir `base`. Confined to `base`
    (zip-slip guarded), whitelisted paths only, conversation.json must parse as JSON. Returns
    (files_written, had_conversation). Raises ValueError on an invalid archive / bad conversation.json.
    Shared by /api/restore and the resume path of /api/run."""
    base = base.resolve()
    if not data or len(data) > 80 * 1024 * 1024:
        raise ValueError("empty or oversized archive (max 80 MB)")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception:
        raise ValueError("not a valid zip archive")
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > 1000:
        raise ValueError("too many files in archive")
    if sum(i.file_size for i in infos) > 200 * 1024 * 1024:
        raise ValueError("archive expands too large")

    def _allowed(rel: str) -> bool:
        if rel in ("conversation.json", "run_log.jsonl", "activity_log.jsonl", "cfg.py"):
            return True
        if "/" not in rel and rel.endswith(".py"):                    # model_opensees.py, model_static.py, ...
            return True
        if "/" not in rel and rel.startswith("report") and rel.endswith(".html"):
            return True
        return rel.split("/", 1)[0] in ("design", "rag", "figs")      # subtree files only

    base.mkdir(parents=True, exist_ok=True)
    written, had_conv = 0, False
    for i in infos:
        name = i.filename.replace("\\", "/")
        rel = name.split("/", 1)[1] if "/" in name else name          # strip the <building>/ archive root
        if not rel:
            continue
        rel = posixpath.normpath(rel)
        if rel.startswith("/") or rel.startswith("..") or ".." in rel.split("/"):
            continue                                                  # path-traversal guard
        if not _allowed(rel) or i.file_size > 60 * 1024 * 1024:
            continue
        target = (base / rel).resolve()
        if base not in target.parents:                                # zip-slip guard (must stay under the job dir)
            continue
        payload = zf.read(i)
        if rel == "conversation.json":
            try:
                json.loads(payload.decode("utf-8"))                   # must be valid JSON or we refuse the whole restore
            except Exception:
                raise ValueError("conversation.json in the archive is not valid JSON")
            had_conv = True
        if target.is_dir():
            continue                                                  # never clobber a directory
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / (target.name + ".tmp~")
        tmp.write_bytes(payload)
        os.replace(tmp, target)                                       # ATOMIC
        written += 1
    return written, had_conv


# ---------------- settings ----------------
@app.get("/api/me")
async def me():
    creds = session.get_creds()
    return {"executor": EXECUTOR.name, "has_creds": bool(creds),
            "model": (creds or {}).get("model"), "base_url": (creds or {}).get("base_url"),
            "reasoning": (creds or {}).get("reasoning", "high"),
            "max_tokens": (creds or {}).get("max_tokens", 32000),
            "provider": (creds or {}).get("provider", "")}


@app.post("/api/creds")
async def set_creds(request: Request):
    body = await request.json()
    base = (body.get("base_url") or "").strip()
    key = (body.get("api_key") or "").strip()
    model = (body.get("model") or "").strip()
    reasoning = (body.get("reasoning") or "high").strip()
    provider = (body.get("provider") or "").strip()
    try: max_tokens = max(256, min(int(body.get("max_tokens") or 32000), 200000))
    except Exception: max_tokens = 32000
    if model != "MOCK":
        if not base.startswith(("http://", "https://")):
            raise HTTPException(400, "base_url must start with http:// or https://")
        if not key:
            raise HTTPException(400, "api_key required")
        if not model:
            raise HTTPException(400, "model required")
    session.set_creds(base, key, model, reasoning, max_tokens, provider)
    return {"ok": True}


EXAMPLE_BRIEFS = {
    "ex1": "Ex1_SCBF_5levels.txt",
    "ex2": "Ex2_SMF_9levels.txt",
    "ex3": "Ex3_BracedMoment_7levels.txt",
    "ex4": "Ex4_BRBF_3levels.txt",
    "ex5": "Ex5_SMF_10levels.txt",
    "ex6": "Ex6_SCBF_6levels.txt",
    "ex7a": "Ex7a_SMF_7levels.txt",
    "ex7b": "Ex7b_SCBF_7levels.txt",
    "ex8": "Ex8_EBF_8levels_Lplan.txt",
    "ex9": "Ex9_SPSW_12levels_Tplan.txt",
    "ex10": "Ex10_Dual_SMF_SCBF_16levels_podium.txt",
    "ex11": "Ex11_BRBF_10levels_Uplan.txt",
    "ex12": "Ex12_SMF_14levels_chamfer_setback.txt",
    "ex13": "Ex13_OCBF_4levels_splitlevel.txt",
    "ex14": "Ex14_EBF_11levels_cruciform_hillside.txt",
    "ex15": "Ex15_Dual_SMF_BRBF_20levels_weddingcake.txt",
    "ex16": "Ex16_IMF_3levels_Zplan_school.txt",
    "ex17": "Ex17_SCBF_9levels_offset_core.txt",
    "ex18": "Ex18_R3_NotDetailed_8levels_lowseismic.txt",
    "ex19": "Ex19_SMF_11levels_softstory_podium.txt",
    "ex20": "Ex20_SCBF_12levels_transfer_discontinuous.txt",
    "ex21": "Ex21_OCBF_1story_bigbox_flexiblediaphragm.txt",
    "ex22": "Ex22_SMF_6levels_hospital_RiskCatIV.txt",
    "ex23": "Ex23_Dual_SMF_BRBF_9levels_SDCF_nearfault_EOC.txt",
    "ex24": "Ex24_Mixed_SMF_NS_SCBF_EW_10levels_bidirectional.txt",
    "ex25": "Ex25_SMF_12levels_atrium_diaphragmdiscontinuity.txt",
    "ex26": "Ex26_Dual_SMF_SCBF_18levels_slender_windgoverned.txt",
    "ex27": "Ex27_CCPSW_CF_20levels_SpeedCore_composite.txt",
    "ex28": "Ex28_STMF_8levels_longspan_trussmoment.txt",
    "ex29": "Ex29_Dual_SMF_BRBF_20levels_264ft_NLRHA_PBSD.txt",
    "ex30": "Ex30_Composite_SCBF_6levels_studs_camber.txt",
    "ex31": "Ex31_Gable_warehouse_R3_unbalanced_snow_ponding.txt",
    "ex32": "Ex32_Vertical_addition_2over3_1968_A36_ASCE41.txt",
    "ex33": "Ex33_Crane_bay_IMF_runway_fatigue_App3.txt",
    "ex34": "Ex34_Twin_towers_BRBF_12and8_podium_seismic_joint.txt",
    "ex35": "Ex35_Ch15_equipment_platform_3tier_OCBF.txt",
    # legacy aliases (old UI buttons / bookmarks)
    "design": "Ex7a_SMF_7levels.txt",
    "redesign": "Ex7a_redesign.txt",
}


@app.get("/api/example/{which}")
async def example_brief(which: str):
    fn = EXAMPLE_BRIEFS.get(which)
    if not fn:
        raise HTTPException(404, "unknown example")
    p = config.EXAMPLES_DIR / fn
    try:
        return {"brief": p.read_text(encoding="utf-8", errors="replace")}
    except Exception as e:
        raise HTTPException(404, f"example not available: {e}")


# ---------------- run (SSE) ----------------
@app.post("/api/run")
async def run(request: Request):
    body = await request.json()
    building = _clean_name(body.get("building") or "Building")
    brief = (body.get("brief") or "").strip()
    resume = bool(body.get("resume"))
    images = _clean_images(body.get("images"))
    if not brief and not resume:
        raise HTTPException(400, "brief required")
    creds = session.get_creds()
    if not creds:
        raise HTTPException(400, "set your LLM base-url + API key in Settings first")
    jobs_dir = JOBS_DIR
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_dir = jobs_dir / building
    # A fresh (non-resume) run ARCHIVES any existing same-name job instead of overwriting it, so previous
    # work is never auto-cleared. Resume keeps the folder as-is. Different project names are untouched.
    if not resume and job_dir.exists() and any(job_dir.iterdir()):
        import datetime
        try: job_dir.rename(jobs_dir / (building + "__" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")))
        except Exception: pass
    # Resume from a browser-held snapshot (restore + resume ride one request; only fills a missing conversation).
    snapshot_b64 = body.get("snapshot_b64")
    if resume and snapshot_b64 and not (job_dir / "conversation.json").exists():
        try:
            _restore_zip_into(job_dir, base64.b64decode(snapshot_b64))
        except Exception:
            pass
    if resume and not (job_dir / "conversation.json").exists():
        raise HTTPException(409, "nothing to resume for '%s', and your browser holds no saved copy of it. "
                            "Load the project's .zip via 'Restore session…' and press Continue again, or "
                            "start over with Design building." % building)
    ws = JobWorkspace(jobs_dir)
    _clear_cancel(building)
    log_path = jobs_dir / building / "run_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def gen():
        try:
            for ev in agent.run_design(ws, EXECUTOR, creds["base_url"], creds["api_key"],
                                       creds["model"], building, brief,
                                       max_tok=creds.get("max_tokens", 32000),
                                       reasoning=creds.get("reasoning", "high"),
                                       provider=creds.get("provider", ""), resume=resume,
                                       images=images,
                                       cancel=lambda: _is_cancelled(building)):
                try:
                    with open(log_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(ev) + "\n")
                except Exception:
                    pass
                yield _sse(ev)
                _t = ev.get("type") if isinstance(ev, dict) else None
                if _t in ("done", "paused"):
                    try:                                   # ship the whole bundle over THIS SSE connection so the
                        from .bundle import make_zip       # browser holds a Download/restore copy immediately
                        _zip = make_zip(job_dir, building)
                        if _zip and len(_zip) <= 30 * 1024 * 1024:
                            yield _sse({"type": "bundle", "building": building,
                                        "zip_b64": base64.b64encode(_zip).decode("ascii")})
                    except Exception:
                        pass
        except Exception as e:
            yield _sse({"type": "error", "text": f"{type(e).__name__}: {e}"})

    sync_gen = gen()

    async def agen():
        # Disconnect = Stop. If the browser closes the SSE stream, the running generator must be
        # closed from here: that injects GeneratorExit at its suspended yield, aborts any in-flight
        # provider call (no runaway token spend), and runs the loop's save handlers.
        from starlette.concurrency import iterate_in_threadpool, run_in_threadpool
        try:
            async for chunk in iterate_in_threadpool(sync_gen):
                yield chunk
                if await request.is_disconnected():
                    break
        finally:
            _request_cancel(building)
            try:
                # SHIELDED: on the cancellation path every await in this scope is itself cancelled --
                # unshielded, close() never runs and the loop stays suspended holding an OPEN provider
                # stream. The shield guarantees the GeneratorExit injection happens.
                import anyio
                with anyio.CancelScope(shield=True):
                    await run_in_threadpool(sync_gen.close)
            except BaseException:
                pass

    return StreamingResponse(agen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/stop")
async def stop(request: Request):
    """Stop an in-progress run: signal the loop to halt at its next step."""
    body = await request.json()
    building = _clean_name(body.get("building") or "")
    was_active = _request_cancel(building)
    return {"ok": True, "stopped": building, "was_active": was_active}


# ---------------- report serving (report.html + its figs/, confined to the jobs dir) ----------------
@app.get("/api/report/{building}/{path:path}")
async def report_file(building: str, path: str = "report.html"):
    base = (JOBS_DIR / _clean_name(building)).resolve()
    target = (base / (path or "report.html")).resolve()
    if not (target == base or base in target.parents):
        raise HTTPException(403, "path escapes the job folder")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(str(target))


@app.get("/api/report/{building}")
async def report_root(building: str):
    return await report_file(building, "report.html")


@app.get("/api/download/{building}")
async def download_bundle(building: str):
    """Offline bundle: one self-contained report.html (figures inlined) + the agent's model script(s)
    + calc_package.json, zipped. Confined to the jobs folder."""
    b = _clean_name(building)
    base = JOBS_DIR.resolve()
    job_dir = (base / b).resolve()
    if base not in job_dir.parents:
        raise HTTPException(403, "path escapes the jobs folder")
    if not ((job_dir / "report.html").exists() or (job_dir / "conversation.json").exists()):
        raise HTTPException(404, "nothing to download yet")
    from .bundle import make_zip
    data = make_zip(job_dir, b)
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{b}_report.zip"'})


@app.post("/api/restore/{building}")
async def restore_session(request: Request, building: str):
    """Rehydrate a job folder from the unified zip the browser holds (or the user re-uploads). The
    archive body is the SAME one /api/download produced. Nothing in the archive is trusted beyond a
    strict path + type whitelist (this writes server files AND conversation.json is later fed to
    the LLM, so it is validated hard)."""
    b = _clean_name(building)
    base = (JOBS_DIR / b).resolve()
    data = await request.body()
    try:
        written, had_conv = _restore_zip_into(base, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "restored": written, "has_conversation": had_conv, "building": b}


@app.get("/terms")
async def terms_page():
    p = config.DISCLAIMER_FILE
    txt = p.read_text(encoding="utf-8") if p.exists() else "Disclaimer unavailable."
    return Response(txt, media_type="text/plain; charset=utf-8")


@app.get("/api/log/{building}")
async def get_log(building: str):
    base = JOBS_DIR / _clean_name(building)
    events = []
    p = base / "run_log.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                try: events.append(json.loads(line))
                except Exception: pass
    return {"events": events, "resumable": (base / "conversation.json").exists(),
            "has_report": (base / "report.html").exists()}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "executor": EXECUTOR.name}


# ---------------- static frontend ----------------
app.mount("/static", StaticFiles(directory=str(config.FRONTEND_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(config.FRONTEND_DIR / "index.html"))

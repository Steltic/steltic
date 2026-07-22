"""Build an offline download bundle for a finished job.

The served report.html keeps figures as separate files under figs/ (so the page stays small while it's
served live). For a download the user can keep, we instead produce a SINGLE self-contained report.html
with every figure inlined as a base64 data URI, and zip it together with the model script(s) the agent
wrote (cfg.py, model_opensees.py, ...) plus calc_package.json, so the whole design travels in one file.

(Equations are typeset by MathJax from a CDN; that one script still needs internet to render the math.
Everything else — text, tables, and all figures — is fully self-contained.)
"""
import base64, datetime, io, json, re, zipfile
from pathlib import Path

_MIME = {"png": "image/png", "gif": "image/gif", "jpg": "image/jpeg", "jpeg": "image/jpeg",
         "svg": "image/svg+xml", "webp": "image/webp"}


def selfcontained_html(job_dir: Path) -> str:
    """Read job_dir/report.html and inline every `figs/<file>` <img> reference as a data URI."""
    job_dir = Path(job_dir)
    html = (job_dir / "report.html").read_text(encoding="utf-8", errors="replace")
    figs = job_dir / "figs"

    def repl(m):
        quote, rel = m.group(1), m.group(2)
        f = figs / rel.split("/")[-1]
        try:
            data = f.read_bytes()
        except Exception:
            return m.group(0)                       # missing figure: leave the ref untouched
        ext = f.suffix.lstrip(".").lower()
        uri = "data:%s;base64,%s" % (_MIME.get(ext, "application/octet-stream"),
                                     base64.b64encode(data).decode("ascii"))
        return f"src={quote}{uri}{quote}"

    return re.sub(r"src=(['\"])(figs/[^'\"]+)\1", repl, html)


# Subfolders we never ship in the archive: figs/ (figures are inlined into the self-contained report.html and
# regenerate next run) and rag/ (saved RAG text -- excluded by choice; a restored Continue just re-queries the
# RAG). Both would only bloat the download/resume file.
_SKIP_DIRS = ("figs", "rag")


def _render_logs_md(job_dir, building: str):
    """Build two human-readable Markdown files from the streamed run_log.jsonl:
      * execution_log.md  -- the activity (status, tool calls + results, milestones, the model's written answers)
      * model_reasoning.md -- the model's chain-of-thought, if it exposed one
    Returns ('', '') when there is no log to render."""
    p = Path(job_dir) / "run_log.jsonl"
    if not p.exists():
        return "", ""
    events = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try: events.append(json.loads(line))
            except Exception: pass
    ts = datetime.datetime.now().isoformat(timespec="seconds")

    out = [f"# Execution log — {building}", f"_generated {ts}_", ""]
    buf = []
    def _flush():                                   # group the model's streamed tokens into one narration block
        txt = "".join(buf).strip(); buf.clear()
        if txt:
            out.extend(["", txt, ""])
    for ev in events:
        t = ev.get("type")
        if t == "token":
            buf.append(ev.get("text", "") or ""); continue
        _flush()
        if t == "status":
            out.append(f"- _{ev.get('text','')}_")
        elif t == "tool":
            out.append("")
            out.append(f"**▶ step {ev.get('step','')} · {ev.get('title') or ev.get('name') or 'tool'}**")
            if ev.get("code"):
                out.extend(["```python", ev.get("code", ""), "```"])
        elif t == "tool_result":
            ms = ev.get("ms")
            summ = ev.get("summary", "") or ""
            first = summ.split("\n", 1)[0]
            if "✗" in first or first == "TIMEOUT":            # failed run -> drop the traceback body (agent handled it)
                summ = first
            out.append(f"  ↳ {summ}" + (f"  _({ms} ms)_" if ms else ""))
        elif t == "milestone":
            out.append(f"- **▸ {ev.get('text','')}**")
        elif t == "error":
            out.append(f"- **✖ error:** {ev.get('text','')}")
        elif t == "paused":
            out.append(f"- **⏸ paused — {ev.get('reason','')}**")
            if ev.get("detail"): out.append(f"  {ev.get('detail')}")
        # 'assistant' is the same text already captured via its tokens; 'reasoning'/'usage'/'done' handled elsewhere/skipped
    _flush()
    exec_md = "\n".join(out).rstrip() + "\n"

    reason_body = "".join(ev.get("text", "") or "" for ev in events if ev.get("type") == "reasoning").strip()
    head = f"# Model reasoning — {building}\n_generated {ts}_\n\n"
    reason_md = head + (reason_body + "\n" if reason_body
                        else "_(No chain-of-thought was captured — the selected model may not expose reasoning.)_\n")
    return exec_md, reason_md


def make_zip(job_dir: Path, building: str) -> bytes:
    """ONE archive used for BOTH the user download and session resume.

    It mirrors the job folder under <building>/ so /api/restore can unpack it 1:1 onto a fresh instance:
      * report.html         -- self-contained (every figure inlined as a data URI)
      * report_v*.html      -- preserved prior-design reports (already self-contained)
      * conversation.json   -- the resumable agent context (Continue reads this to pick up where it stopped)
      * run_log.jsonl, activity_log.jsonl
      * cfg.py + any model_*.py the agent wrote
      * design/**           -- calc_package.json + the derived demand tables
    figs/ and rag/ are skipped (see _SKIP_DIRS): figures are inlined in the report and regenerate; the saved
    RAG text is dropped, so a restored Continue re-queries the RAG.
    """
    job_dir = Path(job_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if (job_dir / "report.html").exists():
            z.writestr(f"{building}/report.html", selfcontained_html(job_dir))   # inlined copy, not the raw one
        for p in sorted(job_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(job_dir).as_posix()
            if rel == "report.html":
                continue                                       # already written (self-contained) above
            if rel.split("/", 1)[0] in _SKIP_DIRS:
                continue                                       # skip raw figs/ (inlined + regenerated)
            z.write(p, f"{building}/{rel}")
        exec_md, reason_md = _render_logs_md(job_dir, building)   # human-readable run log + model reasoning
        if exec_md:
            z.writestr(f"{building}/execution_log.md", exec_md)
        if reason_md:
            z.writestr(f"{building}/model_reasoning.md", reason_md)
    return buf.getvalue()

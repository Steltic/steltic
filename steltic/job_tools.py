"""Per-session job workspace: the file / RAG / activity-log tools, confined to ONE session's jobs/
directory. run_python is handled separately (it goes to the sandbox executor); everything here is a
controlled backend operation on the job files (no arbitrary code).

Path model (matches the steel contract): the session's jobs/ dir is the sandbox's /jobs mount.
  * write_file("jobs/<name>/cfg.py")  -> {session}/jobs/<name>/cfg.py
  * write_file("model.py")  [bare]    -> {session}/jobs/<active building>/model.py
  * read_file may also read engine source (read-only) so the agent can inspect the API.
"""
import json, os, re, hashlib, datetime, pathlib, urllib.request, urllib.error
from . import config


def _clean_name(s: str) -> str:
    return "".join(c for c in (s or "").strip() if c.isalnum() or c in "-_") or "design"


class JobWorkspace:
    def __init__(self, jobs_dir: pathlib.Path):
        self.jobs = pathlib.Path(jobs_dir).resolve()
        self.jobs.mkdir(parents=True, exist_ok=True)
        self.building = ""
        self.step = 0

    # ---------------- internals ----------------
    def _job_dir(self) -> pathlib.Path | None:
        if not self.building:
            return None
        d = self.jobs / self.building
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _resolve_write(self, path: str) -> pathlib.Path:
        norm = str(path or "").replace("\\", "/").strip()
        if not norm or norm.endswith("/") or norm.rstrip("/") in (".", "..", "jobs", "jobs/" + (self.building or "")):
            raise PermissionError(
                "write_file needs a FILE path INSIDE the job folder (e.g. 'cfg.py' or "
                "'design/calc_package.json'), not an empty path or the folder itself. Retry with a filename.")
        if norm.startswith("jobs/"):
            target = self.jobs / norm[len("jobs/"):]
        elif pathlib.Path(norm).is_absolute():
            target = pathlib.Path(norm)
        else:
            target = (self._job_dir() or self.jobs) / norm
        target = target.resolve()
        if not (target == self.jobs or self.jobs in target.parents):
            raise PermissionError("path escapes the job workspace")
        return target

    def log(self, tool: str, detail: str = "", result: str = "") -> dict:
        self.step += 1
        rec = {"step": self.step, "ts": datetime.datetime.now().isoformat(timespec="seconds"),
               "tool": tool, "detail": str(detail)[:240], "result": str(result)[:200]}
        if self.building:
            try:
                p = self.jobs / self.building / "activity_log.jsonl"
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
            except Exception:
                pass                             # logging must never kill a run
        return rec

    # ---------------- tools ----------------
    def new_activity_log(self, building: str = "") -> dict:
        self.building = _clean_name(building)
        self.step = 0
        d = self.jobs / self.building
        d.mkdir(parents=True, exist_ok=True)
        try:
            (d / "activity_log.jsonl").unlink()
        except Exception:
            pass
        self.log("new_activity_log", self.building, "started")
        return {"ok": True, "building": self.building}

    def write_file(self, path: str, content: str) -> dict:
        try:
            target = self._resolve_write(path)
            if target.is_dir():
                return {"error": "write_file needs a FILE path INSIDE the job folder (e.g. 'cfg.py' or "
                                 "'design/calc_package.json'), not the folder itself ('%s' is a directory). "
                                 "Retry with a filename." % path}
            data = content if isinstance(content, str) else ("" if content is None else str(content))
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.parent / (target.name + ".tmp~")
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, target)              # ATOMIC: a mid-write kill can never leave a truncated file
            self.log("write_file", path, f"{len(data)} bytes")
            return {"ok": True, "path": str(target.relative_to(self.jobs))}
        except PermissionError as e:
            return {"error": str(e)}
        except Exception as e:                   # NO tool exception may escape and kill the run (data loss)
            return {"error": f"write_file failed ({type(e).__name__}: {e}) -- nothing was saved; fix the "
                             "path/content and retry", "path_given": str(path)[:120]}

    def read_file(self, path: str, offset: int = 0, limit: int = 0) -> dict:
        try: off = max(int(offset or 0), 0)
        except Exception: off = 0
        try: lim = int(limit or 0)
        except Exception: lim = 0
        norm = str(path).replace("\\", "/")
        candidates = []
        if norm.startswith("jobs/"):
            candidates.append(self.jobs / norm[len("jobs/"):])
        candidates += [(self._job_dir() or self.jobs) / norm, self.jobs / norm,
                       config.STEEL_ENGINE / pathlib.Path(norm).name, config.STEEL_ENGINE / norm]
        for p in candidates:
            try:
                p = p.resolve()
                ok = (self.jobs in p.parents or p == self.jobs or config.STEEL_ENGINE in p.parents)
                if ok and p.exists() and p.is_file():
                    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                    total = len(lines)
                    end = (off + lim) if lim else min(off + 600, total)
                    sel = lines[off:end]
                    self.log("read_file", path, f"{len(sel)}/{total} lines")
                    return {"path": str(p), "total_lines": total,
                            "shown_lines": f"{off}-{off+len(sel)} of {total}",
                            "content": "\n".join(sel)[:60000]}
            except Exception:
                continue
        for p in candidates:                     # a directory is a different mistake than a missing file
            try:
                q = p.resolve()
                if (q == self.jobs or self.jobs in q.parents) and q.is_dir():
                    self.log("read_file", path, "is a directory")
                    return {"error": f"'{path}' is a DIRECTORY, not a file -- use list_files for folders, "
                                     "or read a file inside it (e.g. 'design/calc_package.json')"}
            except Exception:
                continue
        self.log("read_file", path, "not found")
        # Help the agent self-correct instead of guessing again: show what the job folder (and design/) actually hold.
        base = self._job_dir() or self.jobs
        def _ls(d):
            try: return sorted(q.name + ("/" if q.is_dir() else "") for q in d.iterdir())
            except Exception: return []
        avail = {".": _ls(base)}
        if (base / "design").is_dir(): avail["design"] = _ls(base / "design")
        return {"error": f"not found: {path}", "available": avail,
                "hint": "paths are relative to the job folder jobs/<name>/ (you are already inside it); pick a name from 'available'"}

    def list_files(self, path: str = ".") -> dict:
        norm = str(path or ".").replace("\\", "/")
        if norm.startswith("jobs/"):                       # "jobs/<name>/…" is rooted at the session jobs/ dir
            p = self.jobs / norm[len("jobs/"):]            # (matches read_file/write_file; avoids jobs/<n>/jobs/<n> doubling)
        elif norm.strip("/") == "jobs":
            p = self.jobs
        else:                                              # bare/relative -> inside the active job dir
            p = (self._job_dir() or self.jobs) / norm
        p = p.resolve()
        if not (p == self.jobs or self.jobs in p.parents):
            return {"error": "path escapes the job workspace"}
        try:
            entries = sorted(__import__("os").listdir(p))
            self.log("list_files", path, f"{len(entries)} entries")
            return {"entries": entries}
        except Exception as e:
            return {"error": str(e)}

    def activity_summary(self) -> dict:
        recs = []
        try:
            p = self.jobs / self.building / "activity_log.jsonl"
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    recs.append(json.loads(line))
        except Exception as e:
            return {"error": f"no activity log yet ({e})"}
        counts = {}
        for r in recs:
            counts[r["tool"]] = counts.get(r["tool"], 0) + 1
        return {"total_calls": len(recs), "counts_by_tool": counts, "entries": recs}

    def search_engineering_standards(self, query: str, collection: str = "engineering_standards_A360",
                                     top_k: int = 5, clause: str = "", chapter: str = "") -> dict:
        flt = (f" clause={clause}" if clause else "") + (f" chapter={chapter}" if chapter else "")
        detail = f"[{collection}] top_k={top_k}{flt}: {query}"   # the [collection] tag lets report._grounding_check count per-corpus
        # No RAG configured -> soft-disable: tell the agent once to rely on its own cited AISC
        # knowledge. But a RAG that IS configured and then becomes unreachable HALTS the run (the
        # rag_unavailable sentinel below): grounding was promised, so don't silently degrade to
        # memory mid-design -- the agent loop turns it into a paused run with a Continue button.
        RAG_HALT = {"rag_unavailable": True,
                    "message": "cannot access the RAG API, restart the RAG server then click Continue."}
        if not config.RAG_API_URL:
            self.log("search_engineering_standards", detail, "RAG disabled (not configured)")
            return {"disabled": True,
                    "message": "No engineering-standards RAG is configured (RAG_API_URL is empty). Do NOT "
                               "search again -- rely on your own knowledge of AISC 360/341/358 and cite "
                               "clauses from memory, flagging any value you are unsure of for verification."}
        payload = {"query": query, "collection": collection, "top_k": 5}   # fixed at 5
        if clause:  payload["clause"] = clause     # exact-clause / chapter server-side filter; only sent when the agent set it
        if chapter: payload["chapter"] = chapter
        body = json.dumps(payload).encode()
        hdrs = {"Content-Type": "application/json"}
        if config.RAG_API_TOKEN:                   # shared-secret gate on the VM (defense in depth over the VPC rule)
            hdrs["Authorization"] = "Bearer " + config.RAG_API_TOKEN
        last_err = None
        for _attempt in range(2):                  # one retry absorbs a transient blip / cold start
            try:
                req = urllib.request.Request(config.RAG_API_URL, data=body, headers=hdrs)
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.loads(r.read())
                out = data if isinstance(data, dict) else {"results": data}
                if self.building and self._is_spec_collection(collection):
                    return self._save_rag(query, collection, out)  # spec -> save full text to rag/<slug>.txt, return full hits + tags
                self.log("search_engineering_standards", detail, f"{len(out.get('results', []))} hits")
                return out
            except Exception as e:
                last_err = e
        self.log("search_engineering_standards", detail, f"RAG unavailable ({last_err})")
        return dict(RAG_HALT)

    # ---------------- RAG-to-file: keep raw chunks on disk, out of the agent's context ----------------
    _CLAUSE_RE = re.compile(r"\b[A-N]\d+(?:\.\d+)*(?:-\d+[a-z]?)?\b")   # AISC-style clause/eq codes: F2, F2.1, F2-1, H1-1, J3.6

    def _is_spec_collection(self, collection: str) -> bool:
        """RAG-to-file applies only to the SPECIFICATION corpora (AISC/ASCE). OpenSees/example RAGs
        stay inline -- those return short usage examples the agent should see directly."""
        c = (collection or "").lower()
        if "opensees" in c:
            return False
        return "engineering_standard" in c or any(t in c for t in ("a360", "a341", "a358", "aisc", "asce"))

    def _render_rag(self, query: str, collection: str, out) -> str:
        res = out.get("results") if isinstance(out, dict) else out
        if not isinstance(res, list):
            res = [out]
        lines = [f"# RAG query: {query}", f"# collection: {collection}  |  hits: {len(res)}", ""]
        for i, h in enumerate(res, 1):
            if isinstance(h, dict):
                body = (h.get("text") or h.get("content") or h.get("chunk") or h.get("page_content")
                        or h.get("snippet") or "")
                meta = "  ".join(f"{k}={h[k]}" for k in ("score", "source", "title", "section", "page", "id")
                                 if k in h and not isinstance(h[k], (dict, list)))
                if not body:
                    body = json.dumps(h, ensure_ascii=False)
                lines.append(f"## Hit {i}  {meta}".rstrip())
                lines.append(str(body).strip())
            else:
                lines.append(f"## Hit {i}")
                lines.append(str(h).strip())
            lines.append("")
        return "\n".join(lines)

    def _clauses(self, text: str) -> list:
        seen = []
        for m in self._CLAUSE_RE.findall(text):
            if m not in seen:
                seen.append(m)
            if len(seen) >= 12:
                break
        return seen

    def _save_rag(self, query: str, collection: str, out):
        """Spec RAG: write the full hits to rag/<slug>.txt (provenance + later re-read) and return the FULL
        result tagged with saved/query/clauses_found. The agent uses the hits inline now; once the design
        completes the run loop evicts this result to a small pointer to the saved file (agent._evict_all_rag)."""
        text = self._render_rag(query, collection, out)
        d = (self._job_dir() or self.jobs) / "rag"
        d.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", (query or "").lower()).strip("-")[:40] or "q"
        fname = f"{slug}-{hashlib.md5((query or '').encode()).hexdigest()[:6]}.txt"
        (d / fname).write_text(text, encoding="utf-8")
        rel = f"rag/{fname}"
        res = out.get("results") if isinstance(out, dict) else out
        nhits = len(res) if isinstance(res, list) else 1
        self.log("search_engineering_standards", f"[{collection}] {query}", f"{nhits} hits -> {rel}")
        # Emit eviction metadata FIRST so saved/query/clauses_found survive even if the serialized result is later
        # truncated to a cap (agent._evict_all_rag regex-recovers them; the agent still reads the hits in between).
        tagged = {"saved": rel, "query": query, "clauses_found": self._clauses(text)}
        if isinstance(out, dict):
            for _k, _v in out.items():
                tagged.setdefault(_k, _v)
        else:
            tagged["results"] = out
        tagged["_note"] = (f"These hits are also saved to {rel}. Use them now; when this design completes they are "
                           f"replaced in your context by a pointer to {rel} -- on a later optimisation, read_file that "
                           "file if you need a clause from this search again.")
        return tagged

"""Per-session job workspace: the file / RAG / activity-log tools, confined to ONE session's jobs/
directory. run_python is handled separately (it goes to the sandbox executor); everything here is a
controlled backend operation on the job files (no arbitrary code).

Path model (matches the steel contract): the session's jobs/ dir is the sandbox's /jobs mount.
  * write_file("jobs/<name>/cfg.py")  -> {session}/jobs/<name>/cfg.py
  * write_file("model.py")  [bare]    -> {session}/jobs/<active building>/model.py
  * read_file may also read engine source (read-only) so the agent can inspect the API.
"""
import json, os, re, time, hashlib, datetime, pathlib, urllib.request, urllib.error
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
        """Search the standards corpus, ESCALATING before it is ever allowed to report nothing.

        One zero-hit answer is not evidence that a provision is absent -- it is far more often a
        filter that was too tight, a sentence sent where an id was wanted, or a document that was
        never converted on this machine. Leaving that judgement to the agent is how a design once ran
        on remembered AISC values while its report said the corpus was empty. So the ladder lives
        here, in the tool, and runs whether or not the agent thinks to escalate:

          rung 1  the query exactly as asked
          rung 2  again without the clause / chapter filter, if one was set
          rung 3  as an exact-id lookup, when the query (or the clause) carries an id
          rung 4  the query reworded through the corpus's own synonym layer
          rung 5  again without the document filter -- and if THAT hits, the answer says which
                  document supplied the text, because it is not the one that was asked for

        Only when every applicable rung has come back empty is the result "not found", and even then
        it says WHICH of the three kinds of nothing it is (see `_not_found`). A transport failure is
        never a rung: an unreachable RAG still HALTs the run exactly as before, because grounding was
        promised and degrading quietly to memory mid-design is the failure we are preventing."""
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

        spec = self._is_spec_collection(collection)   # decided from what the AGENT asked for, not from the
        trail: list = []                              # widened rungs -- rung 5 must still save to rag/<slug>.txt
        seen: set = set()

        def attempt(label: str, q: str, coll: str, cl: str = "", ch: str = ""):
            """Send one rung. -> (result | None, halt). `None` with halt=False means 'skipped,
            identical to a rung already sent'; halt=True means the server died and the run stops."""
            key = (coll, (q or "").strip().lower(), (cl or "").upper(), (ch or "").upper())
            if key in seen:
                return None, False
            seen.add(key)
            f = (f" clause={cl}" if cl else "") + (f" chapter={ch}" if ch else "")
            # The [collection] tag stays FIRST and stays the collection the AGENT asked for, even on a
            # widened rung: report._grounding_check counts per-corpus off that tag, and four attempts
            # at A341 are four pieces of grounding work done for A341 however they were phrased.
            d = f"[{collection}] {label}" + (f" as={coll or 'ALL documents'}" if coll != collection else "")
            d += f" top_k={top_k}{f}: {q}"
            out, err = self._rag_post(q, coll, cl, ch)
            if out is None:
                self.log("search_engineering_standards", d, f"RAG unavailable ({err})")
                return None, True
            n = len(out.get("results") or [])
            note = str(out.get("note") or "")
            trail.append({"attempt": len(trail) + 1, "how": label, "collection": coll or "(all documents)",
                          "query": q, "clause": cl, "chapter": ch, "hits": n, "note": note})
            if not n:                                  # a hit is logged by finish(), which knows the saved path
                self.log("search_engineering_standards", d, "0 hits" + (f" -- {note[:120]}" if note else ""))
            return out, False

        def finish(out: dict, eff_coll: str, label: str, sent_q: str = "") -> dict:
            """A rung hit. Hand back the hits, plus what it took to get them."""
            if len(trail) > 1:
                out["escalation"] = trail
                out["escalated"] = (f"Your query as written found nothing; these hits come from attempt "
                                    f"{len(trail)} ({label}). Read 'escalation' before you cite them -- the "
                                    "wording that worked is the wording to use next time.")
            if eff_coll != collection:                 # rung 5: say which document actually answered
                srcs = []
                for h in (out.get("results") or []):
                    s = str(h.get("source") or "").strip() if isinstance(h, dict) else ""
                    if s and s not in srcs:
                        srcs.append(s)
                if out.get("note"):
                    out["server_note"] = out["note"]
                out["found_in_documents"] = srcs
                out["note"] = ("FOUND ONLY WITHOUT THE DOCUMENT FILTER -- this text is from "
                               + (", ".join(srcs) or "another document in the corpus")
                               + f", NOT from {collection}. Cite the document that actually supplied it, and "
                                 "confirm that document governs this member before you use the value.")
            if spec and self.building:
                return self._save_rag(query, eff_coll, out, log_collection=collection, via=label,
                                      sent_query=sent_q or query)
            n = len(out.get("results") or [])
            d = f"[{collection}] {label}" + (f" as={eff_coll or 'ALL documents'}" if eff_coll != collection else "")
            self.log("search_engineering_standards", d + f" top_k={top_k}: {query}", f"{n} hits")
            return out

        # ---- rung 1: exactly what was asked for -------------------------------------------------
        out, halt = attempt("rung1 as-asked", query, collection, clause, chapter)
        if halt:
            return dict(RAG_HALT)
        if out and (out.get("results") or []):
            return finish(out, collection, "rung1 as-asked")
        # An unknown collection name is the agent's mistake, not a corpus gap: escalating it would
        # send four more queries to a collection the server has never heard of. Say so and stop.
        if out and "unknown collection" in str(out.get("note") or "").lower():
            self.log("search_engineering_standards", detail, "unknown collection")
            return {"results": [], "found": False, "collection": collection, "escalation": trail,
                    "note": f"UNKNOWN COLLECTION {collection!r} -- the grounding server has no such corpus, so "
                            "nothing was searched. This is a typo in your call, not an absence in the "
                            "standard. Re-issue with one of the collection names the contract lists."}

        # ---- rung 2: drop the clause / chapter filter -------------------------------------------
        # Against this hub's rag_server a clause filter falls through to full text on its own, but the
        # same API is served by vector stores where clause/chapter is a hard metadata filter and a
        # near-miss id silences the query completely. Cheap rung, real failure mode.
        if clause or chapter:
            out, halt = attempt("rung2 no-filter", query, collection)
            if halt:
                return dict(RAG_HALT)
            if out and (out.get("results") or []):
                return finish(out, collection, "rung2 no-filter")

        # ---- rung 3: the exact-id lookup ---------------------------------------------------------
        # The server runs exact_equation / exact_section / exact_table itself the moment `clause` is
        # set, so all we owe it is the bare id lifted out of the engineer's sentence -- plus the
        # lettered forms of a dropped-letter id, which its own routing gate cannot reach.
        for cid in self._query_ids(query, clause, chapter):
            out, halt = attempt(f"rung3 exact-id {cid}", query, collection, cid, chapter)
            if halt:
                return dict(RAG_HALT)
            if out and (out.get("results") or []):
                return finish(out, collection, f"rung3 exact-id {cid}")

        # ---- rung 4: reword through the corpus's own alias layer ---------------------------------
        for rq in self._reworded(query):
            out, halt = attempt("rung4 alias-reworded", rq, collection, "", chapter)
            if halt:
                return dict(RAG_HALT)
            if out and (out.get("results") or []):
                return finish(out, collection, "rung4 alias-reworded", sent_q=rq)

        # ---- rung 5: drop the document filter ----------------------------------------------------
        # An empty collection makes the server search every specification it holds; for the OpenSees
        # and example corpora the equivalent widening is the group name without its sub-collection.
        low = (collection or "").lower()
        wide = "" if spec else ("opensees" if "opensees" in low else ("examples" if "example" in low else None))
        if wide is not None:
            out, halt = attempt("rung5 any-document", query, wide, "", "")
            if halt:
                return dict(RAG_HALT)
            if out and (out.get("results") or []):
                return finish(out, wide, "rung5 any-document")

        # ---- the ladder is exhausted: say which kind of nothing this is --------------------------
        return self._not_found(query, collection, trail)

    # ---------------- the escalation ladder's parts ----------------
    def _rag_post(self, query: str, collection: str, clause: str = "", chapter: str = ""):
        """One rung on the wire. -> (parsed result, None) or (None, last exception).

        The two attempts are the original transport retry that absorbs a cold start; they are NOT
        part of the escalation ladder, and exhausting them means the RAG is gone rather than quiet."""
        payload = {"query": query, "collection": collection, "top_k": 5}   # fixed at 5
        if clause:  payload["clause"] = clause     # exact-clause / chapter server-side filter; only sent when set
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
                return (data if isinstance(data, dict) else {"results": data}), None
            except Exception as e:
                last_err = e
        return None, last_err

    # rag_server._EQ: the server only routes a `clause` to the EQUATION index when it has a leading
    # letter. A dropped-letter id ("3-1", "1.3.1.1-1") therefore never gets there, which is the one
    # gap in its exact-id handling that rung 3 has to close. Keep in sync with the hub's rag_server.
    _SERVER_EQ_RE = re.compile(r"^[A-Z]{1,2}\d+(?:\.\d+)*-\d+[a-z]?$", re.I)
    # Document names carry digit-hyphen pairs ("360-22", "S100-16", "7-22") shaped exactly like
    # equation ids; strip them before hunting for the id the engineer actually meant.
    _DOCNAME_RE = re.compile(r"\b(?:AISC|AISI|ASCE(?:/SEI)?|ANSI)\s*/?\s*[A-Z]?\d+(?:[-–]\d+)?\b", re.I)
    _QUERY_ID_RE = re.compile(r"\b(?:[A-Za-z]{1,2}\d+(?:\.\d+)*(?:-\d+[a-z]?)?"   # F2 - F2.2 - F2-1 - E1.3.1.1-1
                              r"|\d+\.\d+(?:\.\d+)*(?:-\d+[a-z]?)?"               # 12.8.1 - 12.8-3 - 1.3.1.1-1
                              r"|\d+-\d+[a-z]?)\b")                               # 3-1 (dropped leading letter)

    def _query_ids(self, query: str, clause: str = "", chapter: str = "", limit: int = 3) -> list:
        """Ids worth re-sending as an exact `clause`, most promising first.

        Two jobs, neither of which the server can do for us. One: pull the id out of a prose query --
        an agent that asks "AISC 360-22 Equation F2-1 for compact I-shapes" never sets `clause`, so
        the server only ever full-text searches a sentence. Two: expand a dropped-letter id through
        the corpus's eq_id_aliases, because the server's routing gate wants a leading letter and
        "1.3.1.1-1" does not have one."""
        cands = []
        src = (clause or "").strip()
        if src:
            cands.append(src)
        else:
            stripped = self._DOCNAME_RE.sub(" ", query or "")
            found = [m.group(0) for m in self._QUERY_ID_RE.finditer(stripped)]
            lettered = [f for f in found if f[:1].isalpha()]
            cands += (lettered or found)[:2]
        eq = (self._corpus_aliases().get("eq_id_aliases") or {})
        ch = (chapter or "").strip().upper()[:1]
        out = []
        for c in cands:
            c = c.strip().upper()
            if not c:
                continue
            if c not in out and not (clause and c == clause.strip().upper()):
                out.append(c)                      # a clause the agent already sent was tried at rung 1
            if self._SERVER_EQ_RE.match(c):
                continue                           # the server reaches the equation index unaided
            alts = eq.get(c) or eq.get(c.lower()) or []
            # prefer the chapter the agent named: "3-1" in chapter E means E3-1, not B3-1
            for a in sorted((str(x) for x in alts), key=lambda s: (not s.upper().startswith(ch) if ch else False)):
                a = a.upper()
                if a not in out and not a.startswith("C-"):    # never alias a standard id to commentary
                    out.append(a)
        return out[:limit]

    _aliases_cache = None

    def _corpus_aliases(self) -> dict:
        """The corpus's own alias layer (indexes/aliases.json), when this machine has it.

        It ships with the Query file manager module, not with us: in a hub install DATA_DIR is
        <data>/modules_data/steltic, so the QFM workspace sits two levels up. RAG_ALIASES_FILE
        overrides that for any other layout. When it is absent the rewording rung simply does not
        fire -- we deliberately do NOT carry a second copy of the synonym table, because a copy would
        drift from the corpus it is meant to describe and start suggesting terms nothing was indexed
        under."""
        if self._aliases_cache is not None:
            return self._aliases_cache
        cands = []
        if getattr(config, "RAG_ALIASES_FILE", ""):
            cands.append(pathlib.Path(config.RAG_ALIASES_FILE))
        try:
            qfm = config.DATA.parent.parent / "grokbot"
            cands += [qfm / "indexes" / "aliases.json",
                      qfm / "engineering_rag_phase2" / "indexes" / "aliases.json"]
        except Exception:
            pass
        self._aliases_cache = {}
        for p in cands:
            try:
                if p.is_file():
                    d = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(d, dict):
                        self._aliases_cache = d
                        break
            except Exception:
                continue
        return self._aliases_cache

    def _reworded(self, query: str, limit: int = 2) -> list:
        """The query with a MULTI-WORD synonym swapped for the corpus's other spellings of it.

        The server already expands aliases for full-text search, but only two ways: the whole query
        when the whole query IS an alias, and single abbreviation-shaped tokens inside it. Neither
        can see a spelled-out phrase sitting mid-sentence -- "...capacity for lateral torsional
        buckling of a compact shape..." -- which is how an engineer actually writes. Swapping that
        phrase for the group's other spellings is the part of the alias layer left for us, so this
        deliberately ignores single-word and abbreviation members: re-sending those would be the
        server's own expansion a second time."""
        groups = (self._corpus_aliases().get("synonym_groups") or [])
        low = (query or "").lower()
        out = []
        for g in groups:
            if not isinstance(g, list) or len(out) >= limit:
                continue
            members = [str(m) for m in g if isinstance(m, str) and m.strip()]
            hit = next((m for m in members if (" " in m or "-" in m) and len(m) > 4 and m.lower() in low), None)
            if not hit:
                continue
            for alt in sorted((m for m in members if m.lower() != hit.lower()), key=len, reverse=True):
                cand = re.sub(re.escape(hit), lambda _m, _a=alt: _a, query, flags=re.I)
                if cand.lower() != low and cand not in out:
                    out.append(cand)
                if len(out) >= limit:
                    break
        return out[:limit]

    _status_cache = None

    def _corpus_status(self) -> dict:
        """What the grounding server says it is actually holding.

        Consulted only once the ladder has come back empty, and only to tell the three kinds of
        nothing apart -- rag_server answers /healthz with `spec_index` and `indexed_docs`, which is
        the one question its per-query note cannot fully answer (the note says a document is missing;
        only /healthz can name the ones that are present). RAG_API_URL points at .../query, so the
        last path segment is swapped. Cached for a minute: on an unbuilt corpus every single query
        exhausts the ladder, and the answer only changes when the user rebuilds the index."""
        try:
            if self._status_cache and (time.time() - self._status_cache[0]) < 60:
                return self._status_cache[1]
        except Exception:
            pass
        url = config.RAG_API_URL or ""
        for tail in ("/api/query", "/query"):       # longest first: "/api/query" also ends with "/query"
            if url.endswith(tail):
                url = url[: -len(tail)]
                break
        st = {}
        hdrs = {}
        if config.RAG_API_TOKEN:
            hdrs["Authorization"] = "Bearer " + config.RAG_API_TOKEN
        try:
            req = urllib.request.Request(url.rstrip("/") + "/healthz", headers=hdrs)
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read())
            if isinstance(d, dict):
                st = d
        except Exception:
            st = {}                                 # an older or third-party backend need not serve /healthz
        self._status_cache = (time.time(), st)
        return st

    def _not_found(self, query: str, collection: str, trail: list) -> dict:
        """The ladder is exhausted. Report WHICH of the three kinds of nothing this is.

        They are not interchangeable and the agent must not be left to guess: (i) there is no
        specification index on this machine at all, (ii) this document was never converted into the
        corpus, (iii) the corpus holds the document and the term is genuinely not in it. Only (iii)
        is a statement about the standard. Reading (i) or (ii) as (iii) is precisely the failure
        this ladder exists to stop -- a whole design run from memory while the report recorded that
        the corpus had been searched."""
        notes = " | ".join(str(t.get("note") or "") for t in trail if t.get("note"))
        low = notes.lower()
        spec = self._is_spec_collection(collection)
        tried = "; ".join(f"{t['how']} -> {t['hits']} hits" for t in trail) or "(none)"
        st = self._corpus_status() if spec else {}
        docs = [str(d) for d in (st.get("indexed_docs") or [])]
        out = {"results": [], "found": False, "collection": collection, "query": query,
               "attempts": len(trail), "escalation": trail, "attempts_made": tried}
        self.log("search_engineering_standards", f"[{collection}] ladder exhausted: {query}",
                 f"NOT FOUND after {len(trail)} attempts")
        if "no specification index" in low or (spec and st.get("spec_index") is False):
            out["not_found_kind"] = "no_specification_index"
            out["corpus_gap"] = True
            out["note"] = ("CORPUS GAP (i) -- there is NO specification index on this machine. Nothing has "
                           "been converted, so every specification query returns nothing and will keep "
                           "doing so however it is worded. This says NOTHING about whether the provision "
                           f"exists in the standard, and nothing about {collection}. Stop searching the "
                           "specifications. If you go on to design from your own knowledge of the standard, "
                           "you MUST say so in the report, in those words, and flag every value you could "
                           "not verify -- do not let the report imply the corpus was consulted.")
            return out
        m = re.match(r"\s*(\S+)\s+is not in the corpus", notes)
        if m or "is not in the corpus" in low:
            doc = m.group(1) if m else collection
            out["not_found_kind"] = "document_not_in_corpus"
            out["corpus_gap"] = True
            out["document"] = doc
            out["indexed_documents"] = docs
            out["note"] = (f"CORPUS GAP (ii) -- {doc} is not in this corpus: it was never converted, so nothing "
                           "in it can be confirmed or denied here. This is NOT evidence that the clause is "
                           "absent from the standard. The corpus does hold: "
                           + (", ".join(docs) if docs else "(the server did not say)")
                           + ". If one of those governs this check instead, query it. Otherwise name the "
                             "unavailable document in the report and flag every value you take from memory.")
            return out
        out["not_found_kind"] = "term_absent_from_document"
        out["note"] = (f"NOT FOUND (iii) -- {len(trail)} escalating attempts against a corpus that DOES hold "
                       f"this document all came back empty ({tried}), so the term as you phrased it is "
                       "genuinely absent from the indexed text. The filter, the exact-id lookup, the alias "
                       "rewording and the other documents have already been tried for you. Re-word ONCE using "
                       "the phrasing the specification prints, or ask for the parent section; if that misses "
                       "too, treat the provision as absent, say so in the report, and do not invent a clause "
                       "number, equation id or resistance factor to fill the gap.")
        return out

    # ---------------- RAG-to-file: keep raw chunks on disk, out of the agent's context ----------------
    _CLAUSE_RE = re.compile(r"\b[A-N]\d+(?:\.\d+)*(?:-\d+[a-z]?)?\b")   # AISC-style clause/eq codes: F2, F2.1, F2-1, H1-1, J3.6

    def _is_spec_collection(self, collection: str) -> bool:
        """RAG-to-file applies only to the SPECIFICATION corpora (AISC/ASCE). OpenSees/example RAGs
        stay inline -- those return short usage examples the agent should see directly."""
        c = (collection or "").lower()
        if "opensees" in c:
            return False
        return "engineering_standard" in c or any(t in c for t in ("a360", "a341", "a358", "aisc", "asce"))

    def _render_rag(self, query: str, collection: str, out, via: str = "", sent_query: str = "") -> str:
        res = out.get("results") if isinstance(out, dict) else out
        if not isinstance(res, list):
            res = [out]
        lines = [f"# RAG query: {query}", f"# collection: {collection}  |  hits: {len(res)}"]
        if via:
            # Which rung of the escalation ladder answered. Without it the saved file silently claims
            # the first phrasing worked, which is the one thing this provenance must never imply.
            lines.append(f"# answered by: {via}"
                         + (f"  |  query as sent: {sent_query}" if sent_query and sent_query != query else ""))
        lines.append("")
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

    def _save_rag(self, query: str, collection: str, out, log_collection: str = "", via: str = "",
                  sent_query: str = ""):
        """Spec RAG: write the full hits to rag/<slug>.txt (provenance + later re-read) and return the FULL
        result tagged with saved/query/clauses_found. The agent uses the hits inline now; once the design
        completes the run loop evicts this result to a small pointer to the saved file (agent._evict_all_rag).

        `collection` is the one that actually answered (it goes in the saved file's header, so the
        provenance names the right document); `log_collection` is the one the AGENT asked for, and is
        what goes in the activity log's leading [tag] so report._grounding_check keeps counting per
        corpus even when the hit came from a widened rung. `via` records which rung it was."""
        text = self._render_rag(query, collection, out, via=via, sent_query=sent_query)
        d = (self._job_dir() or self.jobs) / "rag"
        d.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", (query or "").lower()).strip("-")[:40] or "q"
        fname = f"{slug}-{hashlib.md5((query or '').encode()).hexdigest()[:6]}.txt"
        (d / fname).write_text(text, encoding="utf-8")
        rel = f"rag/{fname}"
        res = out.get("results") if isinstance(out, dict) else out
        nhits = len(res) if isinstance(res, list) else 1
        self.log("search_engineering_standards",
                 f"[{log_collection or collection}] " + (f"{via} " if via else "") + query,
                 f"{nhits} hits -> {rel}")
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

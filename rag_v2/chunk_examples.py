"""
chunk_examples.py -- example-aware chunker for the AISC design-example markdown in steel_egs/.

Design (see RAG_EXAMPLES_RECHUNK.md, decisions 1 & 2):
  * UNIT = one example. The retrievable/embedded text is the SOLUTION (that's the method the agent mirrors);
    the QUESTION is kept in the payload, not embedded.
  * ONE CHUNK per example; only a solution longer than MAX_CHARS is split on its numbered-step boundaries
    (### N.), keeping tables/equations whole.
  * Every chunk's embedded text starts with an ID / clause / topic / title BREADCRUMB, e.g.
        EXAMPLE E.1A · AISC 360-22 Ch.E (column / axial compression) · §E2 §E3 · W-Shape Column Design, Pinned Ends
    so "E3 W-shape column compression" matches E.1x and NOT generic beam chunks. This is the whole fix.

Metadata (payload) is parsed deterministically:
  * example_id + title  <- the solution H1 line  "# E.1A — <Title> (Solution)"
  * clauses / eqs / tables <- the "**Code basis:**" line (present in 100% of solutions) + body
  * chapter / example_family / topic <- derived from the id + title

Pure stdlib. reingest_examples.py calls build_examples(dir) then embeds c["text"] + upserts.
"""
import os, re, glob

MAX_CHARS  = 8000     # a solution longer than this is split on step boundaries (one chunk otherwise)
TARGET     = 3200     # target size of a split part
HARD_CHARS = 9000     # even a single step bigger than this gets line-split

_IMG    = re.compile(r'!\[[^\]]*\]\([^)]*\)')                       # Marker image placeholders -> drop
_H1     = re.compile(r'^#\s+(.+?)\s+[—–]\s+(.+?)\s*$')              # "# E.1A — Title (Solution)"
_CODE   = re.compile(r'Code basis:\**\s*(.+)', re.I)               # the **Code basis:** summary line
_STEP   = re.compile(r'(?m)(?=^#{2,4}\s+\d+[.)]\s)')               # split point at "### 1. ..." step headings
_SECT   = re.compile(r'§\s*([A-K][0-9]+(?:\.[0-9]+)*[a-z]?)')       # §E3, §F2.2, §J3.11a
_APPX   = re.compile(r'Appendix\s+([0-9]+|[A-K])', re.I)            # Appendix 6 / 7 / 8
_APPSEC = re.compile(r'(?:Section|§)\s*([0-9]+(?:\.[0-9]+)+[a-z]?)')# appendix sections: Section 6.2.2, §7.3.1
_CHAPRF = re.compile(r'\bChapter\s+([A-K])\b')
_EQ     = re.compile(r'\b([A-K][0-9]+-[0-9]+[a-z]?|A-[0-9]+-[0-9]+[a-z]?)\b')  # E3-2, H1-1a, J3-6a, A-7-1
_TBL    = re.compile(r'\bTable\s+([A-K]?[0-9]+(?:\.[0-9]+)*[a-z]?)')

_CHAP_TOPIC = {
    "C": "frame stability (Direct Analysis Method)", "D": "tension member",
    "E": "column / axial compression", "F": "beam flexure", "G": "beam shear",
    "H": "beam-column (combined axial + flexure)", "I": "composite member",
    "J": "bolt / weld / connecting element", "K": "HSS connection",
    "A6": "stability bracing (Appendix 6)", "III": "complete building design",
    "II.A": "simple / shear connection", "II.B": "FR moment connection",
    "II.C": "bracing connection", "II.D": "connection",
}


def _read(p):
    try:
        return open(p, encoding="utf-8", errors="replace").read()
    except Exception:
        return ""


def _id_from_stem(stem):
    """Fallback id if the H1 is missing. E_1A->E.1A, F_1_2B->F.1-2B, A_6_2->A-6.2, II_A_17A->II.A-17A, III_1->III-1."""
    p = stem.split("_")
    if p[0] == "III":
        return "III-" + "-".join(p[1:])
    if p[0] == "II":
        return f"II.{p[1]}-" + "-".join(p[2:])
    if p[0] == "A" and len(p) >= 3 and p[1].isdigit():          # Appendix 6: A_6_2 -> A-6.2
        return f"A-{p[1]}." + ".".join(p[2:])
    if len(p) == 2:
        return f"{p[0]}.{p[1]}"
    return f"{p[0]}.{p[1]}-" + "-".join(p[2:])


def _chapter_of(eid):
    if eid.startswith("III"):
        return "III"
    if eid.startswith("II"):
        m = re.match(r"II\.?([A-D])", eid)
        return "II." + (m.group(1) if m else "")
    if re.match(r"A-?6", eid):
        return "A6"
    m = re.match(r"([A-K])", eid)
    return m.group(1) if m else eid[:1]


def _family(eid):
    return re.sub(r"(?<=\d)[A-Z]$", "", eid)                    # E.1A->E.1, F.1-2B->F.1-2, II.A-17A->II.A-17


def _topic(chapter, title):
    t = (title or "").lower()
    if chapter == "F" and "hss" in t:
        return "HSS flexure"
    if chapter == "E" and "hss" in t:
        return "HSS / rectangular-pipe compression"
    if chapter == "D" and "hss" in t:
        return "HSS tension member"
    return _CHAP_TOPIC.get(chapter, "steel design worked example")


def _uniq(seq):
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


def _clauses(code_line, body):
    src = (code_line or "") + "\n" + (body or "")
    out = _uniq(_SECT.findall(src))                                 # §-prefixed spec clauses
    cl = code_line or ""
    for a in _uniq(_APPX.findall(cl)):                              # Appendix 6/7/8 -> App6 ...
        if "App" + a not in out:
            out.append("App" + a)
    for s in _uniq(_APPSEC.findall(cl)):                            # appendix section numbers (6.2.2, 7.3.1)
        if s not in out:
            out.append(s)
    for ch in _uniq(_CHAPRF.findall(cl)):                           # bare "Chapter K" with no numbered clause
        if ch not in out and not any(c.startswith(ch) for c in out):
            out.append(ch)
    return out[:16]


def _header(eid, chapter, topic, clauses, title):
    ch = f"Ch.{chapter}" if len(chapter) == 1 else chapter
    bits = [f"EXAMPLE {eid}", f"AISC 360-22 {ch} ({topic})"]
    if clauses:
        bits.append(" ".join("§" + c for c in clauses[:8]))
    if title:
        bits.append(title)
    return " · ".join(bits)


def _linesplit(block, target):
    out, cur, n = [], [], 0
    for ln in block.split("\n"):
        if cur and n + len(ln) > target:
            out.append("\n".join(cur)); cur, n = [], 0
        cur.append(ln); n += len(ln) + 1
    if cur:
        out.append("\n".join(cur))
    return out


def _split_solution(body):
    """Split a long solution on ### N. step boundaries, packed to ~TARGET, tables/equations kept whole."""
    segs = [s for s in _STEP.split(body) if s.strip()]
    if len(segs) <= 1:
        segs = [s for s in re.split(r"\n\s*\n", body) if s.strip()]      # fallback: blank-line blocks
    out, cur, n = [], [], 0
    for s in segs:
        if cur and n + len(s) > TARGET:
            out.append("\n\n".join(cur)); cur, n = [], 0
        cur.append(s); n += len(s) + 2
    if cur:
        out.append("\n\n".join(cur))
    final = []
    for p in out:
        final += _linesplit(p, TARGET) if len(p) > HARD_CHARS else [p]
    return final


def _body_of(sol):
    lines = sol.split("\n")
    if lines and lines[0].lstrip().startswith("# "):                     # drop the H1 (id+title already in the header)
        lines = lines[1:]
    return "\n".join(lines).strip()


def _one_example(stem, sol, question, figure):
    sol = _IMG.sub("", sol)
    lines = sol.split("\n")
    m = next((_H1.match(l.strip()) for l in lines[:4] if _H1.match(l.strip())), None)
    if m:
        eid = m.group(1).strip()
        title = re.sub(r"\s*\((?:solution)\)\s*$", "", m.group(2).strip(), flags=re.I)
    else:
        eid, title = _id_from_stem(stem), ""
    chapter = _chapter_of(eid)
    code_line = (_CODE.search(sol).group(1).strip() if _CODE.search(sol) else "")
    clauses = _clauses(code_line, sol)
    eqs = _uniq(_EQ.findall(code_line))[:16]
    tables = _uniq(t for t in _TBL.findall(code_line) if any(c.isdigit() for c in t))[:12]
    topic = _topic(chapter, title)
    header = _header(eid, chapter, topic, clauses, title)
    body = _body_of(sol)
    if figure.strip():
        body += "\n\n### Figure\n" + figure.strip()

    base = dict(example_id=eid, example_family=_family(eid), chapter=chapter, topic=topic,
                clauses=clauses, eqs=eqs, tables=tables, title=title,
                source="AISC_manual_egs", section=f"EXAMPLE {eid} — {title}",
                question=question.strip(), has_figure=bool(figure.strip()), stem=stem)

    parts = [body] if len(body) <= MAX_CHARS else _split_solution(body)
    chunks = []
    for k, p in enumerate(parts, 1):
        crumb = header + (f"  (part {k}/{len(parts)})" if len(parts) > 1 else "")
        text = crumb + "\n\n" + p.strip()
        c = dict(base); c.update(text=text, page_content=text, breadcrumb=header,
                                 part=k, nparts=len(parts))
        chunks.append(c)
    return chunks


def build_examples(src_dir):
    """Return chunk payload dicts for every <ID>_solution.md in src_dir (+ its question/figure siblings)."""
    out = []
    for sol_path in sorted(glob.glob(os.path.join(src_dir, "*_solution.md"))):
        stem = os.path.basename(sol_path)[: -len("_solution.md")]
        sol = _read(sol_path)
        if len(sol.strip()) < 40:
            continue
        question = _read(os.path.join(src_dir, stem + "_question.md"))
        figure = _read(os.path.join(src_dir, stem + "_figure_text.md"))
        out.extend(_one_example(stem, sol, question, figure))
    return out


if __name__ == "__main__":
    import sys, collections
    d = sys.argv[1] if len(sys.argv) > 1 else "."
    ch = build_examples(d)
    exs = len(set(c["example_id"] for c in ch))
    print(f"{exs} examples -> {len(ch)} chunks  ({sum(1 for c in ch if c['nparts']>1)} chunks from split examples)")
    by = collections.Counter(c["chapter"] for c in ch)
    print("by chapter:", dict(sorted(by.items())))
    miss = [c["example_id"] for c in ch if not c["clauses"]]
    if miss:
        print(f"WARNING {len(miss)} chunks with no clauses parsed:", miss[:10])

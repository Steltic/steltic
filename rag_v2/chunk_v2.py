"""
chunk_v2.py -- clause-aware chunker for the engineering-standards markdown (AISC / AISI / ASCE)
reconstructed in ./md/<STD>.md. Replaces the heading-only `chunk_by_section` splitter.

Implements review recommendations 2-6:
  2. clause-aware splitting + a breadcrumb prepended to every chunk + clause/chapter/eqs metadata
     (so the embedding AND the agent know exactly which clause it is -- fixes e.g. F2-vs-I3 confusion)
  3. size bounds: merge orphan/tiny segments forward, split overlong clauses on SAFE block boundaries
  4. atomic tables & equations (never split inside a $$...$$ block or a markdown table)
  5. spec vs commentary tagging (doc_part)
  6. operates on the full reconstructed MD, so there are no 50-page-part boundary artifacts

Pure stdlib -- no embeddings here. `reingest_v2.py` calls chunk_markdown() then embeds + upserts.
"""
import re

TARGET_CHARS = 2200      # aim for ~550 tokens
MAX_CHARS    = 3500      # split any clause longer than this
MIN_CHARS    = 400       # merge any segment shorter than this into the next (kills orphan headings)
HARD_CHARS   = 6000      # even an "atomic" block (giant table / TOC) is line-split above this

_H    = re.compile(r'^(#{1,6})\s+(.*\S)\s*$')        # markdown heading line
_BOLD = re.compile(r'^\*\*(.+?)\*\*[:.]?\s*$')        # bold-only line (Marker renders many clause heads bold)
_SEC  = re.compile(r'^([A-N]\d+(?:\.\d+)*[a-z]?)[.\s)—:-]')   # chapter-section code: F2, F2.1, B4.1, A3
_SUB  = re.compile(r'^(\d+[a-z]?)[.)]\s')             # local sub-number under a section: "1.", "2.", "1a."
_CHAP = re.compile(r'^CHAPTER\s+([A-N])\b', re.I)
_APPX = re.compile(r'^APPENDIX\s+(\d+|[A-N])\b', re.I)
_COMM = re.compile(r'^#{1,2}\s+\**\s*COMMENTARY\b', re.I)   # the real Commentary division (top-level heading, latter half)

_TAG   = re.compile(r'\\tag\{([^}]+)\}')
_EQREF = re.compile(r'\(([A-N]\d+(?:\.\d+)?-\d+[a-z]?)\)')
_TBLREF= re.compile(r'Table\s+([A-N]?\d+(?:\.\d+)*[a-z]?)', re.I)
_IMG   = re.compile(r'!\[[^\]]*\]\([^)]*\)')          # Marker image placeholders -> drop


def _heading_core(line):
    """De-noised heading text if `line` is heading-like (markdown #, bold, or ALL-CAPS title), else None."""
    s = line.strip()
    m = _H.match(s)
    if m:
        s = m.group(2)
    elif _BOLD.match(s):
        s = _BOLD.match(s).group(1)
    elif s.isupper() and 3 <= len(s) <= 95 and not s.startswith('|'):
        pass
    else:
        return None
    return s.strip().strip('*').strip()


def _meta(text):
    eqs  = sorted(set(_TAG.findall(text)) | set(_EQREF.findall(text)))
    tbls = sorted(set(t for t in _TBLREF.findall(text) if any(c.isdigit() for c in t)))
    has_eq  = ('$$' in text) or ('\\tag{' in text) or ('\\[' in text)
    has_tbl = ('|---' in text) or ('| ---' in text) or bool(re.search(r'\n\s*\|[^\n]+\|[^\n]+\|', text))
    return eqs, tbls, has_eq, has_tbl


def _hardsplit(block):
    """Last-resort line-split for a pathologically large block (TOC, giant material table)."""
    out, cur, n = [], [], 0
    for ln in block.split('\n'):
        if cur and n + len(ln) > TARGET_CHARS:
            out.append('\n'.join(cur)); cur, n = [], 0
        cur.append(ln); n += len(ln) + 1
    if cur:
        out.append('\n'.join(cur))
    return out


def _blocks(body):
    """Split a body into ATOMIC blocks: $$..$$ equations and |..| tables are indivisible; prose splits
    on blank lines."""
    lines = body.split('\n')
    blocks, buf, i = [], [], 0
    def flush():
        if buf:
            t = '\n'.join(buf).strip()
            if t:
                blocks.append(t)
            buf.clear()
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith('$$'):                       # equation block -> to closing $$
            flush()
            if ln.count('$$') >= 2:
                blocks.append(ln); i += 1; continue
            eq = [ln]; i += 1
            while i < len(lines):
                eq.append(lines[i]); done = '$$' in lines[i]; i += 1
                if done:
                    break
            blocks.append('\n'.join(eq)); continue
        if ln.strip().startswith('|'):                        # table block
            flush(); tb = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                tb.append(lines[i]); i += 1
            blocks.append('\n'.join(tb)); continue
        if ln.strip() == '':
            flush(); i += 1; continue
        buf.append(ln); i += 1
    flush()
    out = []
    for b in blocks:
        out += _hardsplit(b) if len(b) > HARD_CHARS else [b]
    return out


def _pack(blocks, target=TARGET_CHARS, hard=MAX_CHARS):
    """Greedily pack atomic blocks into sub-chunks ~target chars, never splitting a block."""
    out, cur, n = [], [], 0
    for b in blocks:
        if cur and n + len(b) > target:
            out.append('\n\n'.join(cur)); cur, n = [], 0
        cur.append(b); n += len(b) + 2
        if n >= hard:
            out.append('\n\n'.join(cur)); cur, n = [], 0
    if cur:
        out.append('\n\n'.join(cur))
    if len(out) >= 2 and len(out[-1]) < MIN_CHARS:        # fold a tiny tail back into the previous part
        out[-2] += '\n\n' + out.pop()
    return out


def chunk_markdown(md, std_code, std_display):
    """Return a list of chunk payload dicts for one standard's reconstructed markdown."""
    md = _IMG.sub('', md)
    lines = md.split('\n')
    nlines = len(lines)
    comm_line = next((i for i, ln in enumerate(lines)
                      if _COMM.match(ln.strip()) and i > nlines * 0.40), None)

    # ---- pass 1: segment by clause boundary ----
    segs = []
    chapter = chapter_title = section = section_title = ''
    in_comm = False
    cur = None

    def newseg(clause, title):
        nonlocal cur
        if cur is not None:
            segs.append(cur)
        cur = dict(chapter=chapter, chapter_title=chapter_title, section=section,
                   section_title=section_title, clause=clause, title=title,
                   doc_part='commentary' if in_comm else 'spec', body=[])

    newseg('', '')  # preamble / front matter
    for idx, ln in enumerate(lines):
        if comm_line is not None and idx >= comm_line:
            in_comm = True
        core = _heading_core(ln)
        if core is None:
            cur['body'].append(ln); continue
        if comm_line is not None and idx == comm_line:
            newseg('', core); continue
        cm = _CHAP.match(core); ap = _APPX.match(core)
        if cm:
            chapter = cm.group(1).upper(); chapter_title = ''; section = section_title = ''
            newseg(chapter, core); continue
        if ap:
            chapter = 'APP' + ap.group(1); chapter_title = core; section = section_title = ''
            newseg(chapter, core); continue
        sm = _SEC.match(core)
        if sm:
            section = sm.group(1); section_title = core[len(section):].strip(' .:)-—')
            if not chapter:
                chapter = section[0]
            newseg(section, core); continue
        subm = _SUB.match(core)
        if subm and section:
            clause = f"{section}.{subm.group(1)}"
            newseg(clause, core); continue
        # a CHAPTER title line (ALL CAPS) immediately after "CHAPTER X"
        if chapter and not chapter_title and not section and core.isupper():
            chapter_title = core; cur['chapter_title'] = core
        cur['body'].append(ln)
    if cur is not None:
        segs.append(cur)

    # ---- pass 2: merge tiny forward ----
    merged, i = [], 0
    while i < len(segs):
        s = dict(segs[i]); body = '\n'.join(s['body']).strip()
        while len(body) < MIN_CHARS and i + 1 < len(segs):
            i += 1; nx = segs[i]
            if not s['clause'] and nx['clause']:
                for k in ('clause', 'section_title', 'title', 'chapter', 'chapter_title', 'doc_part'):
                    s[k] = nx[k] or s[k]
            body = (body + '\n\n' + '\n'.join(nx['body']).strip()).strip()
        merged.append((s, body)); i += 1

    # ---- pass 3: split huge, build breadcrumb + metadata, emit ----
    chunks = []
    def emit(s, body_text, part=None, nparts=1):
        chap = s['chapter'] or ''
        clause = s['clause'] or chap or ''
        title = (s['section_title'] or s['title'] or '').strip().strip('*').strip()
        bits = [std_display]
        if chap:
            bits.append(f"Ch.{chap}" + (f" {s['chapter_title'].title()}" if s['chapter_title'] else ""))
        if clause and clause != chap:
            bits.append(f"{clause} {title}".strip())
        crumb = " — ".join(b for b in bits if b)
        if part:
            crumb += f"  (part {part}/{nparts})"
        text = crumb + "\n\n" + body_text.strip()
        eqs, tbls, has_eq, has_tbl = _meta(text)
        chunks.append(dict(
            text=text, page_content=text, source=std_code + ".md", standard=std_code,
            section=(title or clause), clause=clause, chapter=chap, doc_part=s['doc_part'],
            eqs=eqs, tables=tbls, has_equation=has_eq, has_table=has_tbl,
            has_figure=False, breadcrumb=crumb))

    for s, body in merged:
        if len(body.strip()) < 80:
            continue
        if len(body) <= MAX_CHARS:
            emit(s, body)
        else:
            parts = _pack(_blocks(body))
            for k, p in enumerate(parts, 1):
                emit(s, p, k, len(parts))
    return chunks

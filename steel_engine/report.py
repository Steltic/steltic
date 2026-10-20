"""
report.py  --  one self-contained HTML engineer's report per building.

A senior engineer can follow the whole job: building summary + plan, model & deformed shape,
dynamic properties + mode shapes, drift/P-Delta, story shear/OTM, support reactions, member
forces per load case (tables + N/V/M diagrams), the design summary, and Appendix A — a fully
referenced AISC 360-22 design-calculation record for every member/connection.

Math is rendered with MathJax; figures are written to figs/ and referenced (keeps the .html small).

Run (operator, Linux/WSL where openseespy runs):
    python report.py B07
    python report.py T06            # auto-registers test_cfgs
    python report.py T01 T06 B02
"""
import os, sys, math, re, base64, io, json, datetime
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D  # noqa
import openseespy.opensees as ops

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "engine"))
sys.path.insert(0, os.path.join(HERE, "eval_tests", "answer_key"))
import engine3d as E
import sections as S
import design_post as DPOST          # run_case + capacity snippets (operator side)
try:
    import design_pipeline as PIPE   # combos() — the ASCE 7 load-case list
    HAVE_PIPE = True
except Exception:
    HAVE_PIPE = False

Fy, Emod = 50.0, 29000.0
g = E.g

# ============================================================ small utilities
def _register(name):
    if name not in E.CFG:
        # re-render path: a fresh run_python process won't have the cfg registered -- load jobs/<name>/cfg.py
        _base = os.environ.get("STEEL_BUILDER_JOBS") or HERE
        _cfgfile = os.path.join(_base, name, "cfg.py")
        if os.path.exists(_cfgfile):
            try:
                import importlib.util as _ilu
                _spec = _ilu.spec_from_file_location("jobcfg_" + name, _cfgfile)
                _m = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_m)
                if hasattr(_m, "cfg"): E.CFG[name] = _m.cfg
            except Exception:
                pass
    if name not in E.CFG:
        try: import test_cfgs  # noqa: F401
        except Exception: pass
    if name not in E.CFG:
        raise SystemExit(f"unknown building '{name}' -- register its cfg or write jobs/{name}/cfg.py first")

# Figures are written to <root>/figs/ and referenced by relative path (set in build_report).
_FIGDIR = None          # absolute path of the current report's figs/ folder
_FIGSEQ = [0]           # running counter for auto-named inline figures

def _b64(fig):
    """Save a matplotlib figure to figs/ and return its relative path (data-URI fallback if no figdir).
    Uses tight_layout (O(subplots)) instead of bbox_inches='tight' (O(artists)) so a frame figure with
    thousands of annotations encodes in milliseconds, not tens of seconds (P8)."""
    try: fig.tight_layout()
    except Exception: pass
    if _FIGDIR:
        _FIGSEQ[0] += 1; fn = "auto_%03d.png" % _FIGSEQ[0]
        fig.savefig(os.path.join(_FIGDIR, fn), format="png", dpi=130)
        plt.close(fig); return "figs/" + fn
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=130)
    plt.close(fig); buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode("ascii")

def _png_file_b64(path):
    """Reference an existing PNG (already in figs/) by relative path; data-URI fallback if no figdir."""
    if not os.path.exists(path): return None
    if _FIGDIR:
        return "figs/" + os.path.basename(path)
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")

def _materialize(uri):
    """Turn any leftover data:image/...;base64 URI (e.g. from viz3d/frame_diagram) into a figs/ file."""
    if _FIGDIR and isinstance(uri, str) and uri.startswith("data:image/"):
        try:
            head, b64 = uri.split(",", 1)
            ext = head.split("/")[1].split(";")[0].replace("jpeg", "jpg").replace("svg+xml", "svg")
            _FIGSEQ[0] += 1; fn = "auto_%03d.%s" % (_FIGSEQ[0], ext)
            with open(os.path.join(_FIGDIR, fn), "wb") as f: f.write(base64.b64decode(b64))
            return "figs/" + fn
        except Exception:
            return uri
    return uri

def _decode(tag):
    k = tag // 100000; r = tag % 100000; return r // 100, r % 100, k

def _loc(n1, n2):
    """Human-readable member location: grid (i,j) and building level k."""
    i1, j1, k1 = _decode(n1); i2, j2, k2 = _decode(n2)
    if (i1, j1) == (i2, j2):                       # vertical: column
        return f"({i1},{j1}) level {k1}→{k2}"
    return f"({i1},{j1})→({i2},{j2}) level {k1}"

def _table(headers, rows, cls=""):
    h = "".join(f"<th>{c}</th>" for c in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table class='{cls}'><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>"

def _calc(title, rows, note=""):
    """Engineering calc-sheet block: a titled worksheet table whose rows are
    (quantity, calculation, value, code-reference) — one calc step per line."""
    n = f"<div class='cnote'><i>{note}</i></div>" if note else ""
    body = "".join(f"<tr><td>{q}</td><td>{c}</td><td class='v'>{v}</td><td class='r'>{r}</td></tr>"
                   for (q, c, v, r) in rows)
    return (f"<h5>{title}</h5>{n}<table class='calc'>"
            "<tr class='hd'><th>Quantity</th><th>Calculation</th><th>Value</th><th>Ref. (AISC)</th></tr>"
            + body + "</table>")

def _img(uri, caption, full=False):
    cls = "full" if full else ""
    if not uri: return f"<p class='note'>[{caption}: not available]</p>"
    if isinstance(uri, str) and uri.startswith("ERROR"):
        return f"<p class='note'>[{caption}: {uri}]</p>"
    uri = _materialize(uri)
    return f"<figure class='{cls}'><img src='{uri}'/><figcaption>@@FIGNUM@@ {caption}</figcaption></figure>"

# ============================================================ publication helpers (US practice, Oct 2026)
# One number formatter (_n), one key humaniser (_human_key) and one text sanitiser (_pub) feed every table,
# so a published report never shows a Python repr, a literal 'None', a 15-digit float, a cfg[...] key or an
# internal path. cfg['report_mode'] = 'internal' keeps the authoring notes (figure toggles etc.).
_EMPTY = ("", "none", "null", "nan", "n/a?")


def _publish(cfg):
    """Publication mode (default on): no harness / authoring text in the report body."""
    return str((cfg or {}).get("report_mode", "publish")).strip().lower() != "internal"


def _strip0(s):
    return s.rstrip("0").rstrip(".") if "." in s else s


def _fx(v, nd):
    """'%.{nd}f' with round-half-up (no banker's rounding: 1214.5 -> '1215', 0.125 -> '0.13')."""
    from decimal import Decimal, ROUND_HALF_UP
    try:
        q = Decimal(repr(float(v))).quantize(Decimal(1).scaleb(-nd) if nd > 0 else Decimal(1), rounding=ROUND_HALF_UP)
        return format(q, "f")
    except Exception:
        return "%.*f" % (nd, v)


def _fac(v):
    """Code factors (R, C_d, Omega0, rho, I_e, K_d ...) with at least one decimal: 3 -> '3.0', 3.25 -> '3.25'."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return _n(v)
    s = _strip0("%.3f" % x)
    return s if "." in s else s + ".0"


def _sa(v):
    """Spectral values (g) to 2-3 decimals: 0.2 -> '0.20', 0.09 -> '0.09', 1.234 -> '1.234'."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return _n(v)
    s = _strip0("%.3f" % x)
    return s if len(s.split(".")[-1]) >= 2 and "." in s else "%.2f" % x


def _n(v, dc=False, nd=None):
    """THE number formatter. None / '' / 'None' -> an em dash; bool -> yes/no; D/C (dc=True) to 2-3
    decimals; other floats to ~4 significant figures with trailing zeros dropped (re-derivable from what
    is printed); dict -> 'label = value unit; ...' (empty values omitted); list -> de-duplicated
    comma list; strings escaped (and sanitised in publication mode)."""
    if v is None:
        return "&mdash;"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return "{:,}".format(v) if abs(v) >= 1000 else str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            return "&mdash;"
        if nd is not None:
            return _fx(v, nd)
        if dc:
            s = _fx(v, 3)
            s = s.rstrip("0")
            if len(s.split(".")[1]) < 2:
                s = _fx(v, 2)
            return s
        a = abs(v)
        if a >= 1000:
            return "{:,}".format(int(_fx(v, 0)))
        if a >= 100:
            return _strip0(_fx(v, 1))
        if a >= 10:
            return _strip0(_fx(v, 2))
        if a >= 0.1:
            return _strip0(_fx(v, 3))
        if a == 0:
            return "0"
        return _strip0("%.3g" % v) if a >= 1e-4 else "%.2e" % v
    if isinstance(v, dict):
        return _fmt(v)
    if isinstance(v, (list, tuple)):
        if not v:
            return "&mdash;"
        if all(isinstance(x, (list, tuple)) for x in v):
            v = [x for i, x in enumerate(v) if x not in v[:i]]          # de-duplicate repeated records
            return "; ".join("(" + ", ".join(_n(y) for y in x) + ")" for x in v[:12]) + \
                   (" &hellip; (%d more)" % (len(v) - 12) if len(v) > 12 else "")
        if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
            # a numeric sequence (per story, per line ...) keeps every value -- repeats are data
            return ", ".join(_n(x) for x in v[:40]) + (" &hellip;" if len(v) > 40 else "")
        seen, out = set(), []
        for x in v:
            s = _n(x)
            if s not in seen:
                seen.add(s); out.append(s)
        return ", ".join(out[:24]) + (" &hellip;" if len(out) > 24 else "")
    s = str(v)
    if s.strip().lower() in _EMPTY:
        return "&mdash;"
    return _esc(_pub(s))


def _na(v):
    return _n(v)


# unit suffix -> (printed unit, scale applied to the value); kip-in moments are printed in kip-ft
_UNIT_SFX = (("_in4_per_ft", "in.<sup>4</sup>/ft", 1.0), ("_in3_per_ft", "in.<sup>3</sup>/ft", 1.0),
             ("_kip_per_ft", "kip/ft", 1.0), ("_lb_per_ft", "plf", 1.0),
             ("_kipft", "kip-ft", 1.0), ("_kft", "kip-ft", 1.0), ("_kipin", "kip-ft", 1.0 / 12.0),
             ("_kin", "kip-ft", 1.0 / 12.0), ("_klf", "kip/ft", 1.0), ("_kips", "kip", 1.0), ("_kip", "kip", 1.0),
             ("_ksi", "ksi", 1.0), ("_psf", "psf", 1.0), ("_plf", "plf", 1.0), ("_pcf", "pcf", 1.0),
             ("_in4", "in.<sup>4</sup>", 1.0), ("_in3", "in.<sup>3</sup>", 1.0), ("_in2", "in.<sup>2</sup>", 1.0),
             ("_ft2", "ft<sup>2</sup>", 1.0), ("_in", "in.", 1.0), ("_ft", "ft", 1.0), ("_pct", "%", 1.0),
             ("_deg", "deg", 1.0), ("_rad", "rad", 1.0), ("_hz", "Hz", 1.0), ("_sec", "s", 1.0), ("_s", "s", 1.0),
             ("_k", "kip", 1.0), ("_g", "g", 1.0))
_SYM = {"p": "P", "pu": "P<sub>u</sub>", "pr": "P<sub>r</sub>", "pcomp": "P (compression)",
        "ptens": "P (tension)", "pucomp": "P<sub>u</sub> (compression)", "putens": "P<sub>u</sub> (tension)",
        "tu": "T<sub>u</sub>", "t": "T", "v": "V", "vu": "V<sub>u</sub>", "vr": "V<sub>r</sub>", "vx": "V<sub>x</sub>",
        "vy": "V<sub>y</sub>", "m": "M", "mu": "M<sub>u</sub>", "mr": "M<sub>r</sub>", "mz": "M<sub>x</sub>",
        "mx": "M<sub>x</sub>", "my": "M<sub>y</sub>", "mux": "M<sub>ux</sub>", "muy": "M<sub>uy</sub>",
        "mend": "M<sub>end</sub>", "au": "A<sub>u</sub> (axial)", "axial": "axial force P", "ru": "R<sub>u</sub>",
        "phipn": "&phi;P<sub>n</sub>", "phipc": "&phi;P<sub>n</sub>", "phitn": "&phi;T<sub>n</sub>",
        "phimn": "&phi;M<sub>n</sub>", "phimnx": "&phi;M<sub>nx</sub>", "phimny": "&phi;M<sub>ny</sub>",
        "phivn": "&phi;V<sub>n</sub>", "phirn": "&phi;R<sub>n</sub>", "uplift": "net uplift",
        "collectoraxial": "collector (drag) axial force", "nbases": "number of bases", "dc": "D/C",
        "lcin": "L<sub>c</sub>", "lbin": "L<sub>b</sub>", "lengthin": "L", "endcondition": "end condition",
        "pcombo": "compression combination", "upliftcombo": "uplift combination", "vcombo": "shear combination",
        "mcombo": "moment combination", "tensioncombo": "tension combination", "shearcombo": "shear combination",
        "governingcombo": "governing combination", "combo": "combination", "axialnote": "axial force",
        "basis": "basis", "note": "note"}
_DROP_KEYS = ("cfg", "cfg_key", "cfg_keys", "agent", "tool", "rag", "rag_query", "rag_hits", "activity",
              "pipeline_seed", "seeded", "_auto")


def _human_key(k):
    """(label, unit, scale) for a calc-package key: 'Mz_kipin' -> ('M<sub>x</sub>', 'kip-ft', 1/12),
    'phiMn_kipft' -> ('&phi;M<sub>n</sub>', 'kip-ft', 1), 'end_condition' -> ('end condition', '', 1)."""
    s = str(k)
    low = s.lower()
    unit, scale, base = "", 1.0, s
    for sfx, u, sc in _UNIT_SFX:
        if low.endswith(sfx) and len(low) > len(sfx):
            unit, scale, base = u, sc, s[:-len(sfx)]
            break
    nk = re.sub(r"[^a-z0-9]", "", base.lower().replace("φ", "phi"))
    if nk in _SYM:
        lab = _SYM[nk]
    else:
        lab = re.sub(r"(?<=\d)_(?=\d)", ".", base)
        lab = (lab.replace("_plus_", " + ").replace("_minus_", " - ").replace("_over_", "/")
               .replace("_per_", " per ").replace("_", " ").strip())
        lab = re.sub(r"\b(?:engine|framework)\b", "analysis", lab, flags=re.I)
        lab = _esc(lab)
        lab = re.sub(r"^phi\s*", "&phi;", lab)
        lab = re.sub(r"^lambda\s*", "&lambda;", lab)
        lab = re.sub(r"\b(?:Omega0|Om0|omega0)\b", "&Omega;<sub>0</sub>", lab)
    return lab, unit, scale


_CL_SUBS = ((re.compile("\u03a9\u2080\\*?"), "&Omega;<sub>0</sub>"),
            (re.compile(r"\(1\.2\+0\.2SDS\)D"), "(1.2+0.2S<sub>DS</sub>)D"),
            (re.compile(r"\(0\.9-0\.2SDS\)D"), "(0.9&minus;0.2S<sub>DS</sub>)D"),
            (re.compile(r"0\.2SDS"), "0.2S<sub>DS</sub>"),
            (re.compile(r"Om0\*E([XY])"), r"&Omega;<sub>0</sub>E<sub>\1</sub>"),
            (re.compile(r"rhoE([XY])"), r"&rho;E<sub>\1</sub>"),
            (re.compile(r"(?<![A-Za-z])E([XY])(?=[+\-/ \]]|$)"), r"E<sub>\1</sub>"),
            (re.compile(r"Ecl\["), "E<sub>cl</sub>["),
            (re.compile(r"Lr(?![A-Za-z])"), "L<sub>r</sub>"),
            (re.compile(r"(?<![A-Za-z])W([XY])(?![A-Za-z])"), r"W<sub>\1</sub>"),
            (re.compile(r"\+Ni(?![A-Za-z])"), "+N<sub>i</sub>"))


def _combo_label(label):
    """Load-combination label as printed: the engine's ASCII identity ("(1.2+0.2SDS)D+rhoEX+t+/0.3Y-+0.5L")
    typeset with the notation of Chapter 4 ("(1.2+0.2S_DS)D+rho E_X+t+/0.3Y-+0.5L"). The same transform is
    used wherever a combination is named, so labels match between chapters."""
    if label in (None, "", "None"):
        return "&mdash;"
    s = _esc(_pub(str(label)))
    for rx, rep in _CL_SUBS:
        s = rx.sub(rep, s)
    return s


def _fmt(d):
    """A demand / record dict as 'label = value unit; ...' -- empty values and harness keys omitted, kip-in
    moments printed in kip-ft, nested dicts flattened as 'name: (label = value; ...)'. Non-dicts go to _n."""
    if not isinstance(d, dict):
        return _n(d)
    parts = []
    for k, v in d.items():
        if v is None or (isinstance(v, str) and v.strip().lower() in _EMPTY) or v in ([], {}):
            continue
        if str(k).lower() in _DROP_KEYS or str(k).startswith("_") or str(k).lower().startswith("cfg_"):
            continue
        lab, unit, sc = _human_key(k)
        if isinstance(v, dict):
            parts.append("%s: (%s)" % (lab, _fmt(v)))
        elif isinstance(v, bool):
            parts.append("%s: %s" % (lab, _n(v)))
        elif isinstance(v, (int, float)):
            parts.append("%s = %s%s" % (lab, _n(float(v) * sc if sc != 1.0 else v), (" " + unit) if unit else ""))
        elif isinstance(v, (list, tuple)):
            parts.append("%s: %s" % (lab, _n(v)))
        else:
            parts.append("%s: %s" % (lab, _n(v)) if lab not in ("basis", "note") else "(%s)" % _n(v))
    return "; ".join(parts) if parts else "&mdash;"


_CFG_RX = re.compile(r"cfg\s*\[\s*['\"]([^'\"\]]+)['\"]\s*\](?:\s*\[\s*['\"]([^'\"\]]+)['\"]\s*\])?")
_PATH_RX = re.compile(r"(?:\.\./)+[\w./-]+|/(?:home|tmp|mnt|root|Users)/[\w./-]+|design/calc_package\.json|"
                      r"calc_package\.json|activity_log\.jsonl|design_report\.md|member_demands\.md|"
                      r"pipeline_results\.json|connection_demands\.csv")


_AGENT_NOTE_RX = re.compile(r"from memory|declared fallback|not verified against the (?:corpus|reference library)|"
                            r"NOT in the (?:reference library|RAG|corpus)|\bcorpus\b|\bRAG\b", re.I)


def _drop_agent_notes(s):
    """Remove the design agent's working notes about its own sources ("method applied from memory",
    "not verified against the corpus") from published text; they stay in the QA annex. Parentheses left
    open by a dropped sentence are closed."""
    if not _AGENT_NOTE_RX.search(s):
        return s
    s = re.sub(r"\s*\([^()]*\)", lambda m: "" if _AGENT_NOTE_RX.search(m.group(0)) else m.group(0), s)
    if not _AGENT_NOTE_RX.search(s):
        return s
    parts = re.split(r"(?<=[.;])\s+", s)
    keep = []; close = 0
    for part in parts:
        if _AGENT_NOTE_RX.search(part):
            bal = part.count("(") - part.count(")")
            if bal < 0:
                close += -bal
            continue
        keep.append(part)
    out = " ".join(keep).rstrip()
    if close:
        out = out.rstrip(".") + ")" * close + "."
    return out


def _pub(s):
    """Sanitise free text for publication: cfg['a']['b'] -> 'the declared a / b input', internal file paths
    and pipeline module names removed. Engineering content is left untouched."""
    if not isinstance(s, str) or not s:
        return s
    def _c(m):
        a = m.group(1).replace("_", " ")
        b = m.group(2).replace("_", " ") if m.group(2) else ""
        return "the declared %s input" % ((a + " / " + b) if b else a)
    s = _CFG_RX.sub(_c, s)
    s = _PATH_RX.sub("the design record", s)
    s = re.sub(r"\b(?:engine3d|static_model|design_pipeline|design_post|preflight)\.[A-Za-z_]+(?:\(\))?",
               "the analysis model", s)
    s = re.sub(r"\bdesign_report(?:\.md)?\b\s*/\s*pipeline_results\b|\bpipeline_results\b|\bdesign_report(?:\.md)?\b",
               "the analysis summary", s)
    s = re.sub(r"\s*'mdx/mdy'", "", s)
    s = re.sub(r"\brun_one\b", "the analysis run", s)
    s = re.sub(r"\bpipeline (envelope|demands?)\b", r"analysis \1", s)
    s = re.sub(r"\b(?:the )?demand pipeline\b", "the analysis", s)
    s = re.sub(r"\bThe engine\b", "The analysis", s)
    s = re.sub(r"\bthe engine\b", "the analysis", s)
    s = re.sub(r"\bframework (demand|one-way|envelope|screen|gate|irregularity screen|computes|provides)\b",
               r"analysis \1", s)
    s = re.sub(r"\bthe framework\b", "the analysis", s)
    s = re.sub(r"\(framework\)", "(analysis)", s)
    s = re.sub(r"\bFramework\b", "Analysis", s)
    s = re.sub(r"\bframework\b", "analysis", s)
    s = re.sub(r"\b(?:Omega0|Om0)\b", "\u03a9\u2080", s)
    s = re.sub(r"\badd_(beam|brace|column)\b", r"\1", s)
    s = re.sub(r"\s*\(\s*'[^'()]*'\s*(?:,\s*'[^'()]*'\s*)+\)", "", s)
    s = re.sub(r"\bphi_s\s*\*\s*lambda_u\b", "\u03c6s\u03bbu", s)
    s = re.sub(r"\bphi_s\b", "\u03c6s", s)
    s = re.sub(r"\blambda_u\b", "\u03bbu", s)
    s = re.sub(r"\bddm_analysis\b", "DDM system check", s)
    _GRK = {"delta": "\u03b4", "theta": "\u03b8", "rho": "\u03c1", "omega": "\u03c9", "alpha": "\u03b1",
            "beta": "\u03b2", "gamma": "\u03b3", "lambda": "\u03bb", "psi": "\u03c8", "mu": "\u03bc"}
    s = re.sub(r"\b(delta|theta|rho|omega|alpha|beta|gamma|lambda|psi|mu)_([a-z]{1,3})\b",
               lambda m: _GRK[m.group(1)] + m.group(2), s)            # symbol names: delta_di -> \u03b4di
    s = re.sub(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)(?=\s*=)", lambda m: m.group(1).replace("_", " "), s)
    s = re.sub(r"(?<![-\w.])([a-z]{3,}(?:_[a-z]{2,})+)(?![-\w]|\.\w)", lambda m: m.group(1).replace("_", " "), s)
    s = re.sub(r"\b(beam deflection|drift|deflection|consistency|completion|analysis) gate\b", r"\1 check", s)
    s = re.sub(r"\s*\(SEEDED[^)]*\)", "", s)
    s = re.sub(r"\bSEEDED\b", "provisional", s)
    s = re.sub(r"\b(?:the )?RAG corpus\b|\b(?:the )?RAG\b", "the reference library", s)
    s = re.sub(r"calc_package\s*\[\s*['\"]([^'\"\]]+)['\"]\s*\]", lambda m: "the calc-package record '%s'" % m.group(1), s)
    s = re.sub(r"\s*\(see '[^']*'\)", "", s)
    s = re.sub(r"\(steel[_ ]design[_ ]examples\)", "(AISC Design Examples)", s)
    s = re.sub(r"\bsteel[_ ]design[_ ]examples\b", "the AISC Design Examples", s)
    s = re.sub(r"\bcfg\s+([a-z][a-z0-9_]*)", lambda m: "the declared %s input" % m.group(1).replace("_", " "), s)
    s = re.sub(r"\(brief\)", "(project criteria)", s)
    s = re.sub(r"\bper the brief\b", "per the project criteria", s)
    s = re.sub(r"\bthe brief\b", "the project criteria", s)
    # capitals used for emphasis read as shouting in a calculation package (acronyms are untouched)
    s = re.sub(r"\b(BOTH|TENSION|COMPRESSION|REQUIRED|NEVER|MUST|ALWAYS|EVERY|ALSO|MIRRORED|WITHOUT|OVERTURNING|"
               r"ONLY|EACH)\b(?![-/]?[A-Z0-9])", lambda m: m.group(1).lower(), s)
    s = re.sub(r"([;,(]\s*)brief\b", r"\1project criteria", s)
    s = _drop_agent_notes(s)
    s = s.replace("<=", "\u2264").replace(">=", "\u2265").replace("&lt;=", "&le;").replace("&gt;=", "&ge;")
    s = re.sub(r"\bsqrt\(", "\u221a(", s)
    return s


def _pub_issue(s):
    """A consistency finding for the published Chapter 13: sanitised, without Python literals of the fix
    instructions (the full text stays in the QA annex)."""
    s = re.sub(r"\s*\((?:[a-z_]+\.)+[a-z_]+\)", "", str(s))
    s = _pub(s)
    for _ in range(3):
        s = re.sub(r"\s*=\s*\{[^{}]*\}", "", s)
        s = re.sub(r"\{[^{}]*\}", "", s)
    return re.sub(r"\s{2,}", " ", s).strip()


_COMBO_LIKE = re.compile(r"SDS\)D|\b\d\.\d+D\+|Om0\*|rhoE[XY]|\bW[XY][+-]|Ecl\[|\[col\]")


def _pub_issue_html(s):
    """_pub_issue as HTML: quoted load-combination labels typeset as in Chapter 4, everything else escaped."""
    s = re.sub(r"\s*\((?:[a-z_]+\.)+[a-z_]+\)", "", str(s))
    for _ in range(3):
        s = re.sub(r"\s*=\s*\{[^{}]*\}", "", s)
        s = re.sub(r"\{[^{}]*\}", "", s)
    out = []
    for part in re.split(r"('[^']*')", s):
        if len(part) > 2 and part[0] == part[-1] == "'" and _COMBO_LIKE.search(part):
            out.append("&lsquo;%s&rsquo;" % _combo_label(part[1:-1]))
            continue
        for k, seg in enumerate(re.split(r"(\((?:1\.2\+0\.2SDS|0\.9-0\.2SDS)\)D[^\s:;,']*)", part)):
            out.append(_combo_label(seg) if k % 2 else _esc(_pub(seg)))
    return re.sub(r"\s{2,}", " ", "".join(out)).strip()


def _verdict_item(f):
    """'label: phi_s*lambda_u = 0.948 FAIL' -> typeset combination label + the check."""
    lab, sep, rest = str(f).partition(": ")
    if sep and _COMBO_LIKE.search(lab + "+"):
        return "%s: %s" % (_combo_label(lab), _esc(_pub(rest)))
    return _esc(_pub(str(f)))


def _ft(x_in, nd=2):
    """Inches -> decimal feet without the round-half-even trap: _ft(150) == '12.5', _ft(270) == '22.5'."""
    try:
        return _strip0("%.*f" % (nd, float(x_in) / 12.0))
    except Exception:
        return "&mdash;"


def _ftin(x_in):
    """Inches -> US feet-inch dimension to the nearest 1/8 in.: 180 -> 15'-0\", 150 -> 12'-6\", 162.25 -> 13'-6 1/4\"."""
    try:
        x = float(x_in)
    except Exception:
        return "&mdash;"
    neg = x < 0; x = abs(x)
    e = int(round(x * 8.0))                  # eighths of an inch
    ft, rem = divmod(e, 96)
    inch, eighth = divmod(rem, 8)
    frac = ""
    if eighth:
        num, den = eighth, 8
        while num % 2 == 0:
            num //= 2; den //= 2
        frac = " %d/%d" % (num, den)
    return ("-" if neg else "") + "%d'-%d%s\"" % (ft, inch, frac)


def _heights_txt(H):
    """Story heights in feet-inches with repeats grouped: [180, 156 x 7] -> 15'-0\", 7 @ 13'-0\"."""
    out, i = [], 0
    H = [float(h) for h in H]
    while i < len(H):
        j = i
        while j + 1 < len(H) and abs(H[j + 1] - H[i]) < 1e-6:
            j += 1
        n = j - i + 1
        out.append(("%d @ %s" % (n, _ftin(H[i]))) if n > 1 else _ftin(H[i]))
        i = j + 1
    return ", ".join(out)

# ============================================================ analysis helpers
def _seismic(cfg):
    NF = len(cfg["heights"])
    T, w2, eX, eY, Mtot = E.modal(cfg, min(3*NF, 16))
    # hr-seismic (HR-01/02): ELF per direction (own period + factors); this single-Fx helper takes the
    # direction with the larger base shear (conservative) until the report shows both (E.elf_dir)
    Cs, V, Tu, Ta, kk, Fx, W = max((E.elf_dir(cfg, d) for d in ("X", "Y")), key=lambda r: r[1])
    return T, eX, eY, Cs, V, Tu, Ta, Fx, W

def _run_case(cfg, direction, Fx, accidental=False):
    """Seismic ELF case in `direction`; leaves model live. Returns info, disp, drift, reactions."""
    info = E.build(cfg, "PDelta"); NF = info["NF"]; di = 0 if direction == "X" else 1
    ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
    fD = 1.2 + 0.2*cfg["seis"]["SDS"]
    for k in range(1, NF+1):
        pts = info["present"][k]; p = fD*E.floor_grav(cfg, k)/len(pts)
        for (i, j) in pts: ops.load(E.ntag(i, j, k), 0, 0, -p, 0, 0, 0)
    SX, SY = cfg["SX"], cfg["SY"]
    for k in range(1, NF+1):
        f = [0.0]*6; f[di] = Fx[k]
        if accidental:
            pts = info["present"][k]; xs = [i*SX for i, j in pts]; ys = [j*SY for i, j in pts]
            B = (max(ys)-min(ys)+SY) if direction == "X" else (max(xs)-min(xs)+SX)
            f[5] = Fx[k]*0.05*B
        ops.load(E.mtag(k), *f)
    ops.constraints("Transformation"); ops.numberer("RCM"); ops.system("UmfPack")
    ops.test("NormDispIncr", 1e-7, 200); ops.algorithm("Newton")
    ops.integrator("LoadControl", 1.0); ops.analysis("Static"); ops.analyze(1)
    disp = {k: ops.nodeDisp(E.mtag(k), di+1) for k in range(1, NF+1)}
    drift = []; prev = 0.0
    for k in range(1, NF+1):
        drift.append((disp[k]-prev)/cfg["heights"][k-1]); prev = disp[k]
    ops.reactions()
    try:                       # every node restrained horizontally (supports at any elevation; masters excluded)
        sup = [t for t in ops.getFixedNodes() if {1, 2} & set(ops.getFixedDOFs(t))]
    except Exception:
        sup = []
    if sup:
        react = [((t % 100000) // 100, t % 100, [ops.nodeReaction(t, d) for d in (1, 2, 3, 4, 5, 6)]) for t in sup]
    else:
        react = [(i, j, [ops.nodeReaction(E.ntag(i, j, 0), d) for d in (1, 2, 3, 4, 5, 6)])
                 for (i, j) in info["present"][0]]
    return info, disp, drift, react

# ============================================================ figures
def fig_plan(cfg):
    """2D plan: column grid with (i,j) labels — explains the grid used in the reaction tables."""
    NX, NY = cfg["NX"], cfg["NY"]; SX, SY = cfg["SX"], cfg["SY"]
    xco = cfg.get("xcoords"); yco = cfg.get("ycoords"); skew = cfg.get("skew", 0.0)
    def XY(i, j): return ((xco[i] if xco else i*SX)+skew*j, (yco[j] if yco else j*SY))
    pres0 = set(cfg_present(cfg, 1))
    Lx = (xco[-1] if xco else NX*SX); Ly = (yco[-1] if yco else NY*SY)
    fig, ax = plt.subplots(figsize=(9, 9*max(Ly, 1)/max(Lx, 1) + 1))
    for i in range(NX):                            # shade framed bays -> non-rectangular footprints are visible
        for j in range(NY):
            if all(p in pres0 for p in [(i, j), (i+1, j), (i, j+1), (i+1, j+1)]):
                bx = [XY(i, j)[0], XY(i+1, j)[0], XY(i+1, j+1)[0], XY(i, j+1)[0]]
                by = [XY(i, j)[1], XY(i+1, j)[1], XY(i+1, j+1)[1], XY(i, j+1)[1]]
                ax.fill(bx, by, color="#eef3fb", zorder=0)
    for i in range(NX+1):                          # grid lines
        x0, _ = XY(i, 0); x1, y1 = XY(i, NY); ax.plot([XY(i,0)[0], XY(i,NY)[0]], [XY(i,0)[1], XY(i,NY)[1]], color="#ccc", lw=0.8)
    for j in range(NY+1):
        ax.plot([XY(0,j)[0], XY(NX,j)[0]], [XY(0,j)[1], XY(NX,j)[1]], color="#ccc", lw=0.8)
    for i in range(NX+1):
        for j in range(NY+1):
            x, y = XY(i, j)
            present = (i, j) in pres0
            ax.plot([x], [y], marker=("s" if present else "x"), ms=5, ls="none", color="black" if present else "#bbb")
            ax.annotate(f"({i},{j})", (x, y), textcoords="offset points", xytext=(4, 4), fontsize=7)
    ax.set_aspect("equal"); ax.set_xlabel("X (in)  —  i index"); ax.set_ylabel("Y (in)  —  j index")
    ax.set_title(f"Plan grid — column lines labelled (i, j); shaded = framed bays (level 1); × = no column")
    ax.grid(False)
    return _b64(fig)

def cfg_present(cfg, k):
    """present column (i,j) at floor k (handles plan-shape callables)."""
    try:
        return E.grid(cfg, k)
    except Exception:
        return [(i, j) for i in range(cfg["NX"]+1) for j in range(cfg["NY"]+1)]

def fig_mode_3d(cfg, mode, nmodes):
    """Full-frame 3D mode shape (undeformed grey + modal-deformed colour), true proportions.
    REUSES the cached modal eigenvector field (engine3d.mode_shapes) -- no ops.eigen re-solve."""
    ms = E.mode_shapes(cfg, max(nmodes, mode))
    nodes = ms["coords"]; evall = ms["ev"]; ele = ms["ele"]; zlev = ms["z"]
    ev = {t: (evall[t][mode-1] if (t in evall and len(evall[t]) >= mode) else [0,0,0,0,0,0]) for t in nodes}
    amp = max((abs(ev[t][0])+abs(ev[t][1]) for t in ev), default=1.0) or 1.0
    zmax = (max(zlev.values()) if isinstance(zlev, dict) else zlev[-1])
    sc = 0.12*zmax/amp
    fig = plt.figure(figsize=(11, 8)); ax = fig.add_subplot(111, projection="3d")
    for (et, kind, sec, n1, n2) in ele:
        if n1 not in nodes or n2 not in nodes: continue
        a, b = nodes[n1], nodes[n2]
        ax.plot([a[0], b[0]], [a[1], b[1]], [a[2], b[2]], color="0.8", lw=0.6)
        da = [a[i]+sc*ev[n1][i] for i in range(3)]; db = [b[i]+sc*ev[n2][i] for i in range(3)]
        ax.plot([da[0], db[0]], [da[1], db[1]], [da[2], db[2]], color="#2c5aa0", lw=1.3)
    ax.set_xlabel("X (in)"); ax.set_ylabel("Y (in)"); ax.set_zlabel("Z (in)")
    try:
        xr = ax.get_xlim3d(); yr = ax.get_ylim3d(); zr = ax.get_zlim3d()
        ax.set_box_aspect((xr[1]-xr[0], yr[1]-yr[0], zr[1]-zr[0]))
    except Exception: pass
    return _b64(fig)

def _elevation_from_live(cfg, direction, title):
    """Draw N/V/M for the perimeter frame line of `direction` from the CURRENTLY LIVE model
    (call right after a case has been analysed). Returns data-uri or None."""
    onln = (lambda i, j: j == 0) if direction == "X" else (lambda i, j: i == 0)
    al = (lambda n: ops.nodeCoord(n)[0]) if direction == "X" else (lambda n: ops.nodeCoord(n)[1])
    zo = lambda n: ops.nodeCoord(n)[2]
    mem = []
    for (t, kind, sec, n1, n2) in _LIVE_ELE:
        i1, j1, _ = _decode(n1); i2, j2, _ = _decode(n2)
        if onln(i1, j1) and onln(i2, j2): mem.append((t, kind, n1, n2))
    if not mem: return None
    fig, axes = plt.subplots(1, 3, figsize=(16, 6))
    for ax, which, ttl, col in zip(axes, ("N", "V", "M"),
                                   ("Axial N (kip)", "Shear V (kip)", "Moment M (k-ft)"),
                                   ("#1f77b4", "#2ca02c", "#d62728")):
        data = []; mx = 1e-9
        for (t, kind, n1, n2) in mem:
            bf = ops.basicForce(t)
            if kind == "brace":
                e1 = e2 = (bf[0] if which == "N" else 0.0)
            else:
                L = math.dist(ops.nodeCoord(n1), ops.nodeCoord(n2))
                if which == "N":   e1 = e2 = bf[0]
                elif which == "V": e1 = e2 = (abs(bf[1])+abs(bf[2]))/L
                else:              e1 = bf[1]/12.0; e2 = -bf[2]/12.0
            data.append((kind, n1, n2, e1, e2)); mx = max(mx, abs(e1), abs(e2))
        sc = (0.30*min(cfg["SX"], cfg["SY"]))/mx
        peak = 0.0; peaklab = None
        for kind, n1, n2, e1, e2 in data:
            a1, z1 = al(n1), zo(n1); a2, z2 = al(n2), zo(n2)
            ax.plot([a1, a2], [z1, z2], color="#bbb", lw=1.0, zorder=1)
            if abs(z2-z1) > abs(a2-a1):
                ax.plot([a1+e1*sc, a2+e2*sc], [z1, z2], color=col, lw=1.2)
                ax.plot([a1, a1+e1*sc], [z1, z1], color=col, lw=0.5)
                ax.plot([a2, a2+e2*sc], [z2, z2], color=col, lw=0.5)
            else:
                ax.plot([a1, a2], [z1+e1*sc, z2+e2*sc], color=col, lw=1.2)
                ax.plot([a1, a1], [z1, z1+e1*sc], color=col, lw=0.5)
                ax.plot([a2, a2], [z2, z2+e2*sc], color=col, lw=0.5)
            if max(abs(e1), abs(e2)) > peak:
                peak = max(abs(e1), abs(e2)); peaklab = (a1, z1, max(e1, e2, key=abs))
        if peaklab:
            ax.annotate(f"max {peak:.0f}", (peaklab[0], peaklab[1]), fontsize=8, color=col,
                        fontweight="bold")
        ax.set_title(f"{ttl}   (peak {peak:.0f})"); ax.set_xlabel("along (in)"); ax.set_ylabel("Z (in)")
        ax.set_aspect("equal", "datalim"); ax.grid(alpha=0.2)
    fig.suptitle(title)
    return _b64(fig)

_LIVE_ELE = []   # element registry of the currently-live model (set by run helpers)

def fig_drift_profile(cfg, runs):
    """Drift profile, both directions: elastic delta_xe/h at the design-drift location and the
    Eq. 12.8-16 design drift (Cd, Ie per direction; RBS factor included) against the limit."""
    NF = len(cfg["heights"]); lvl = list(range(1, NF+1))
    fig, ax = plt.subplots(figsize=(9, 6))
    for d, mk in (("X", "-o"), ("Y", "-s")):
        r = runs[d]
        ax.plot([v*100 for v in r["dxe"]], lvl, mk, alpha=0.5, label=r"$\delta_{xe}/h$ " + d)
        ax.plot([v*100 for v in r["design"]], lvl, mk, label=r"$\Delta/h=C_d\delta_{xe}/I_e h$ " + d)
    for d, ls in (("X", "--"), ("Y", ":")):
        lim = runs[d]["limit"]*100
        ax.axvline(lim, color="r", ls=ls, label=f"limit {d} {lim:.2f}%" + (r" ($\Delta_a/\rho$)" if runs[d]["limit_rho"] else ""))
    ax.set_xlabel("interstory drift (%)"); ax.set_ylabel("story"); ax.set_title("Drift profile")
    ax.legend(fontsize=8); ax.grid(alpha=0.3)
    return _b64(fig)

def fig_story_shear_otm(cfg, Fx):
    NF = len(cfg["heights"]); z = E.zlevels(cfg)
    Vstory = [sum(Fx[k] for k in range(s, NF+1)) for s in range(1, NF+1)]
    OTM = [sum(Fx[k]*(z[k]-z[s-1])/12.0 for k in range(s, NF+1)) for s in range(1, NF+1)]
    OTM_base = sum(Fx[k]*z[k]/12.0 for k in range(1, NF+1))
    lvl = list(range(1, NF+1))
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(9, 5))
    a1.step(Vstory, lvl, where="mid"); a1.set_xlabel("story shear (kip)"); a1.set_ylabel("story"); a1.grid(alpha=0.3); a1.set_title("Story shear")
    a2.plot(OTM, lvl, "-o"); a2.set_xlabel("overturning moment (k-ft)"); a2.set_title("OTM (above story)"); a2.grid(alpha=0.3)
    return _b64(fig), Vstory, OTM_base

# ============================================================ per-load-case forces
def load_cases(cfg):
    """ASCE 7-22 LRFD combinations considered (label, fD, fL, fLr, lat, col_only)."""
    if HAVE_PIPE:
        try: return PIPE.combos(cfg)
        except Exception: pass
    # minimal fallback if design_pipeline unavailable (companion 0.3S when snow governs the roof, else 0.5Lr)
    if float(cfg.get("snow", 0) or 0) > 0:
        return [("1.4D", 1.4, 0, 0, {}, False), ("1.2D+1.6L+0.3S", 1.2, 1.6, 0.3, {}, False)]
    return [("1.4D", 1.4, 0, 0, {}, False), ("1.2D+1.6L+0.5Lr", 1.2, 1.6, 0.5, {}, False)]

def case_forces(cfg, fD, fL, fLr, lat):
    """Run one combination through P-Delta; return (out dict, registry). Leaves model live."""
    out, info = DPOST.run_case(cfg, fD, fL, fLr, lat)
    global _LIVE_ELE; _LIVE_ELE = info["ele"]
    return out, info

def per_case_max_table(info):
    """Per member TYPE, the governing (max) member + its forces + location, read from basicForce —
    the SAME convention the elevation diagrams use, so table and figure magnitudes agree."""
    best = {}   # (kind,sec) -> (score, N, Mz, My, V, n1, n2)
    for (t, kind, sec, n1, n2) in info["ele"]:
        bf = ops.basicForce(t)
        if kind == "brace":
            N = bf[0]; Mz = My = V = 0.0; score = abs(N)
        else:
            L = math.dist(ops.nodeCoord(n1), ops.nodeCoord(n2))
            N = bf[0]; Mz = max(abs(bf[1]), abs(bf[2])); My = max(abs(bf[3]), abs(bf[4]))
            V = max((abs(bf[1])+abs(bf[2]))/L, (abs(bf[3])+abs(bf[4]))/L); score = max(abs(Mz), abs(N))
        key = (kind, sec)
        if key not in best or score > best[key][0]:
            best[key] = (score, N, Mz, My, V, n1, n2)
    rows = []
    for (kind, sec), (sc, N, Mz, My, V, n1, n2) in sorted(best.items()):
        rows.append([kind, sec, f"{N:.1f}", f"{Mz/12:.1f}", f"{My/12:.1f}", f"{V:.1f}", _loc(n1, n2)])
    return rows

def _case_desc(label, col_only, a341=True):
    """Plain-English description of an ASCE 7-22 combination label (AISC 341 cited only where it applies)."""
    L = label
    if ("[C2" in L or "[C3" in L or "[C4" in L):
        upl = " with 0.9D (uplift / overturning)" if L.startswith("0.9") else " combined with gravity"
        if "[C2" in L:
            return ("ASCE 7-22 Fig. 27.3-8 wind Case 2: 75%% of the design wind pressure along one principal axis "
                    "with a torsional moment from the eccentricity e = &plusmn;0.15B%s." % upl)
        if "[C3" in L:
            return ("ASCE 7-22 Fig. 27.3-8 wind Case 3: 75%% of the design wind pressure acting simultaneously along "
                    "both principal axes%s." % upl)
        return ("ASCE 7-22 Fig. 27.3-8 wind Case 4: 56.3%% of the design wind pressure along both principal axes "
                "with the Case 2 torsional moments%s." % upl)
    if ("WX" in L or "WY" in L) and "E" not in L.replace("Lr", "").replace("Le", ""):
        d = "X (E–W)" if "WX" in L else "Y (N–S)"
        upl = " (uplift / overturning case, 0.9D)" if L.startswith("0.9") else ""
        return f"Wind acting in the {d} direction, combined with gravity{upl}."
    if "Ecl[" in L or "capacity-limited" in L:
        upl = " on the 0.9D (uplift) gravity case" if L.startswith("(0.9") else ""
        return ("Capacity-limited seismic load effect E<sub>cl</sub>" + upl + ": the yielding braces / links / "
                "webs are replaced by their expected (or adjusted) strengths, yielding in X and Y together "
                "(ASCE 7-22 &sect;12.4.3.2; AISC 341-22 Chapter F, as labelled).")
    if "OCBF V-beam" in L:
        return ("OCBF V / inverted-V beams: braces removed and the beam loaded by min(&Omega;<sub>0</sub>E, "
                "R<sub>y</sub>F<sub>y</sub>A<sub>g</sub>) in tension and 0.3P<sub>n</sub> in compression "
                "(AISC 341-22 F1.4a(a)).")
    if "Om0" in L:
        d = "X (E–W)" if "EX" in L else "Y (N–S)"
        upl = " on the 0.9D (uplift) gravity case" if L.startswith("(0.9") else ""
        return (f"Overstrength (&Omega;<sub>0</sub>) seismic in the {d} direction{upl} — column axial only "
                "(ASCE 7-22 &sect;12.4.3" + ("; AISC 341-22 D1.4a(b)" if a341 else "; &sect;12.3.3.4") + ").")
    if "rhoE" in L:
        d = "X (E–W)" if "EX" in L else "Y (N–S)"
        sense = "positive" if ("EX+" in L or "EY+" in L) else "negative"
        tor = ("with +5% accidental torsion" if "t+" in L else
               ("with −5% accidental torsion" if "t-" in L else "no accidental torsion"))
        grav = ("the reduced 0.9D (uplift) gravity case" if L.startswith("(0.9")
                else "the (1.2+0.2S<sub>DS</sub>)D + companion-L gravity case (L factor per &sect;2.3.6 Exc. 1, as labelled)")
        return f"Code-level seismic in the {d} direction ({sense} sense, {tor}), combined with {grav}."
    if L.strip() == "1.4D":
        return "Dead load only — the basic gravity check."
    if re.search(r"1\.6L[re]", L):
        return "Gravity only: dead load plus the full roof live load, with the companion floor live load."
    if re.search(r"1\.[06]S", L):
        return "Gravity only: dead load plus the full snow load, with the companion floor live load."
    if "1.6L" in L:
        return ("Gravity only: dead load plus the full floor live load, with the companion roof live or snow load"
                + (" (notional loads N<sub>i</sub> added)" if "+Ni" in L else "") + ".")
    return "Load combination."

def _lat_dir(lat):
    if not lat: return "X"
    sx = sum(abs(v[0]) for v in lat.values()); sy = sum(abs(v[1]) for v in lat.values())
    return "X" if sx >= sy else "Y"

# ============================================================ Appendix A — design calcs
def _safe_props(section):
    try: return S.props(section, SEC=E.SEC)
    except Exception:
        try: return S.props(section)
        except Exception: return None

_KIND_LABEL = {"beam": "beam", "col": "column", "brace": "brace"}


def _consistency_mod():
    try:
        import consistency as _C
        return _C
    except Exception:
        return None


def _q_label(q):
    return {"P": "P (compression)", "T": "P (tension)", "Mx": "M<sub>x</sub>", "My": "M<sub>y</sub>",
            "V": "V", "H1": "H1-1 interaction"}.get(q, q)


def _q_unit(q):
    return {"P": "kip", "T": "kip", "Mx": "kip-ft", "My": "kip-ft", "V": "kip"}.get(q, "")


def _dc_status(dc, issues=None, waived=None):
    """OK / NG / NG -- reconcile for a D/C value and its consistency findings. An EXISTING member whose
    exceedance is carried as a retrofit-scope item (entry 'waived' text, e.g. an IEBC alteration evaluation)
    reads 'retrofit required' instead of NG."""
    try:
        v = float(dc)
    except (TypeError, ValueError):
        v = None
    if issues:
        return "<b class='ng'>NG / reconcile</b>"
    if v is None:
        return "&mdash;"
    if v > 1.0 + 1e-9 and waived:
        return "<b class='ng'>retrofit required</b>"
    if waived and isinstance(waived, str) and v <= 1.0 + 1e-9:
        w_ = waived.strip()
        return "OK (%s%s)" % ("" if w_.lower().startswith("existing") else "existing; ", _esc(w_[:120]))
    return "<b class='ng'>NG</b>" if v > 1.0 + 1e-9 else "OK"


def _term_txt(terms, which):
    out = []
    for t in terms:
        k = t["dem_key"] if which == "dem" else t["cap_key"]
        lab, _u, _sc = _human_key(k)
        if lab in ("demand", "capacity", "required", "available"):
            lab = ""
        val = _n(t[which])
        unit = t.get("unit") or ""
        if unit in ("-", ""):
            unit = ""
        out.append(("%s = %s" % (lab, val) if lab else val) + ((" " + unit) if unit else ""))
    return "; ".join(out) if out else "&mdash;"


def _checks_table(chks, C=None, member=True, waived=None):
    """The entry's own limit-state checks: limit state | reference | demand | capacity | D/C | printed
    demand / printed capacity | status. The recomputed ratio is shown wherever the check prints its demand
    and capacity (H1-1 recomputed from its terms where the check cites H1-1)."""
    rows = []
    for k in chks or []:
        if not isinstance(k, dict):
            continue
        terms = C.check_terms(k) if C else []
        dc = C._dc_num(k.get("DC")) if C else (k.get("DC") if isinstance(k.get("DC"), (int, float)) else None)
        rec = None
        txt = str(k.get("limit_state", "")) + " " + str(k.get("cited", ""))
        inter = C is not None and bool(C._INTERACTION.search(str(k.get("limit_state", ""))))
        named = C is not None and bool(re.search(r"h1-1|h1\.[12]", txt, re.I))
        if C is not None and inter and named:
            rec = C.h1_recompute(terms, txt)
        if rec is None and len(terms) == 1 and terms[0]["cap"] and (not inter or terms[0]["qty"] in ("*", "ratio")):
            rec = terms[0]["dem"] / terms[0]["cap"]
        bad = False
        if rec is not None and dc is not None and C is not None:
            bad = not C._close(rec, dc)
        st = ("<b class='ng'>NG / reconcile</b>" if bad else _dc_status(dc, waived=waived))
        rows.append([_n(k.get("limit_state", k.get("name", ""))), _n(k.get("cited", "")),
                     _term_txt(terms, "dem"), _term_txt(terms, "cap"),
                     (_n(dc, dc=True) if dc is not None else _n(k.get("DC"))),
                     (_n(rec, dc=True) if rec is not None else "&mdash;"), st])
    if not rows:
        return ""
    return _table(["Limit state", "Reference", "Demand", "Available strength", "D/C (recorded)",
                   "Demand &divide; strength", "Status"], rows, cls="chk-t")


def _checks_Lb(m):
    """Unbraced lengths (in.) the member's flexure checks state: a check key Lb_in, or 'Lb = <x> in/ft' in its text."""
    vals = []
    for k in (m.get("checks") or []):
        if not isinstance(k, dict):
            continue
        try:
            if k.get("Lb_in") not in (None, ""):
                vals.append(float(k["Lb_in"])); continue
        except (TypeError, ValueError):
            pass
        t = " ".join(str(k.get(x) or "") for x in ("limit_state", "note", "basis"))
        for mm in re.finditer(r"\bL_?b\s*=\s*(\d+(?:\.\d+)?)\s*(ft\b|in\b|in\.|')?", t):
            v = float(mm.group(1))
            vals.append(v * 12.0 if (mm.group(2) or "") in ("ft", "'") else v)
    return vals


def _member_calc_block(m, a341=None):
    """One calc-package member: section properties and geometry (incl. L, L<sub>c</sub> and L<sub>c</sub>/r for
    braces), the analysis demand envelope (kip, kip-ft; M<sub>x</sub> major / M<sub>y</sub> minor axis), the available
    strengths, the printed demand / printed strength ratios, every limit-state check with its recomputed ratio,
    and the governing D/C with a status that turns to 'NG / reconcile' when the printed numbers disagree."""
    C = _consistency_mod()
    mid = m.get("id", ""); inp = m.get("inputs", {}) or {}
    sec = inp.get("section", ""); kind = inp.get("kind", "")
    h = ["<h4 id='m-%s'>%s &mdash; %s (%s)</h4>" % (_esc(re.sub(r"[^A-Za-z0-9_-]", "_", str(mid))), _esc(mid),
                                                  _esc(sec), _esc(_KIND_LABEL.get(kind, kind)))]
    pk = [("A", "A (in.<sup>2</sup>)"), ("Zx", "Z<sub>x</sub> (in.<sup>3</sup>)"), ("Sx", "S<sub>x</sub> (in.<sup>3</sup>)"),
          ("Zy", "Z<sub>y</sub> (in.<sup>3</sup>)"), ("Sy", "S<sub>y</sub> (in.<sup>3</sup>)"), ("rx", "r<sub>x</sub> (in.)"),
          ("ry", "r<sub>y</sub> (in.)"), ("r", "r (in.)"), ("Aw", "A<sub>w</sub> (in.<sup>2</sup>)"),
          ("J", "J (in.<sup>4</sup>)"),
          ("length_in", "work-point length L (in.)" if kind == "brace" else "L (in.)"), ("Lb_in", "L<sub>b</sub> (in.)"),
          ("Lc_in", "L<sub>c</sub> (in.)"), ("K", "K"), ("Cb", "C<sub>b</sub>")]
    pr = [(lbl, inp[k]) for k, lbl in pk if inp.get(k) not in (None, "")]
    # L_b: the engine's input value is a seed (the AISC 341 D1.2b spacing); the flexure checks state the L_b they
    # use ("Lb = 0 (deck-braced)", "Lb = 120 in"), so the table shows that value
    _lbs = _checks_Lb(m)
    if _lbs:
        _lbv = (_lbs[0] if max(_lbs) - min(_lbs) < 0.5 else
                "per limit state (%s\u2013%s)" % (_n(min(_lbs)), _n(max(_lbs))))   # the cell is escaped: no entity
        pr = [(l_, (_lbv if l_ == "L<sub>b</sub> (in.)" else v_)) for l_, v_ in pr]
        if not any(l_ == "L<sub>b</sub> (in.)" for l_, _ in pr):
            pr.append(("L<sub>b</sub> (in.)", _lbv))
    r_gov = None
    if kind == "brace" and sec:
        try:
            r_gov = S.brace_r(sec)
        except Exception:
            r_gov = None
        rr = inp.get("r")
        lc = inp.get("Lc_in") or inp.get("length_in")
        try:
            r_use = float(rr) if rr not in (None, "") else r_gov
            if r_use and lc:
                pr.append(("L<sub>c</sub>/r" if inp.get("Lc_in") else "L/r", _fx(float(lc) / float(r_use), 1)))
        except (TypeError, ValueError):
            pass
        if r_gov is None and not str(sec).upper().startswith("BRB"):
            pr.append(("governing r", "not found in the AISC Shapes Database"))
        elif r_gov is None:
            pr.append(("brace type", "buckling-restrained brace (manufacturer core; qualified by testing, AISC 341-22 F4 / K3)"))
    if pr:
        try:
            _built_up = bool(sec) and (str(sec).upper() not in S._load_csv())
        except Exception:
            _built_up = False
        h.append("<p class='cnote'>Section properties (%s) and geometry:</p>"
                 % ("built-up section: gross properties of the analysis model; plate dimensions with the member design"
                    if _built_up else "AISC Shapes Database v16.0"))
        h.append(_table([l for l, _ in pr], [[v if isinstance(v, str) and v.startswith(("not found",)) else
                                               (_n(v) if not isinstance(v, str) else _esc(v)) for _, v in pr]]))
    if kind == "brace":
        notes = []
        if inp.get("Lc_basis"):
            notes.append("L<sub>c</sub>: " + _n(inp.get("Lc_basis")))
        try:
            if r_gov is not None and inp.get("r") not in (None, "") and float(inp["r"]) > 1.02 * r_gov:
                notes.append("<b>the printed r (%s in.) exceeds the governing minor-axis r = %s in. of %s</b>"
                             % (_n(float(inp["r"])), _n(r_gov), _esc(sec)))
        except (TypeError, ValueError):
            pass
        if notes:
            h.append("<p class='cnote'>%s.</p>" % "; ".join(notes))
    env = [("P (compression)", inp.get("P_comp_kip"), 1.0), ("P (tension)", inp.get("P_tens_kip"), 1.0),
           ("M<sub>x</sub> (kip-ft)", inp.get("Mz_kipin"), 1.0 / 12.0), ("M<sub>y</sub> (kip-ft)", inp.get("My_kipin"), 1.0 / 12.0),
           ("V (kip)", inp.get("V_kip"), 1.0)]
    if inp.get("collector_axial_kip") not in (None, 0, 0.0):
        env.append(("collector axial (kip)", inp.get("collector_axial_kip"), 1.0))
    def _v(x, sc):
        try:
            return _n(float(x) * sc)
        except (TypeError, ValueError):
            return _n(x)
    hd = [l if "(" in l and "kip" in l else l + " (kip)" for l, _, _ in env]
    h.append("<p class='cnote'>Analysis demand envelope (all LRFD combinations, second-order):</p>")
    h.append(_table(hd + ["governing combination"],
                    [[_v(x, sc) for _, x, sc in env] + [_combo_label(inp.get("governing_combo"))]]))
    llr = inp.get("live_load_reduction")
    if isinstance(llr, dict) and llr:
        h.append("<p class='cnote'>Floor live load reduced per ASCE 7-22 &sect;4.7 (Eq. 4.7-1): L/L<sub>o</sub> = %s for "
                 "the governing member (A<sub>T</sub> = %s ft<sup>2</sup>, K<sub>LL</sub> = %s); range in this group %s&ndash;%s.</p>"
                 % (_n(llr.get("RF_governing_member")), _n(llr.get("AT_ft2_governing_member")), _n(llr.get("KLL")),
                    _n(llr.get("RF_min")), _n(llr.get("RF_max"))))
    cap = m.get("capacity"); dc = m.get("DC"); ls = m.get("limit_state"); cited = m.get("cited")
    chks = m.get("checks") if isinstance(m.get("checks"), list) else []
    if not ((isinstance(cap, dict) and cap) or dc is not None or ls or cited or chks):
        h.append("<p class='note'>Available strength and D/C: not recorded in the calc package for this member.</p>")
        return "".join(h)
    rows = []
    if isinstance(cap, dict):
        for k, v in cap.items():
            if isinstance(v, (dict, list)) or v in (None, ""):
                continue
            lab, unit, sc = _human_key(k)
            val = _n(float(v) * sc) if isinstance(v, (int, float)) and not isinstance(v, bool) else _n(v)
            rows.append((lab, "available strength", val + ((" " + unit) if unit else ""), ""))
    elif cap not in (None, "", {}) and not chks:     # with checks, the governing strength is in the checks table
        rows.append(("available strength of the governing limit state", "", _n(cap), ""))
    if rows:
        h.append(_calc("Available strength (AISC 360-22, as recorded)", rows))
    ratios = C.envelope_ratios(m) if C else []
    if ratios:
        rr = []
        _terms = C.h1_moment_terms(m)[0] if hasattr(C, "h1_moment_terms") else ["Mx", "My"]
        _tl = ", ".join({"Mx": "M<sub>x</sub>", "My": "M<sub>y</sub>"}[a] for a in _terms)
        _miss = [a for a in ("Mx", "My") if a not in _terms and a in C.printed_envelope(m)] if C else []
        for q, d_, c_, r in ratios:
            if q == "H1":
                rr.append((_q_label(q), "H1-1a/b from the printed P, %s and strengths%s" % (
                    _tl, ("; no printed strength for %s, so this ratio omits that term (see the limit-state "
                          "checks below)" % " and ".join({"Mx": "M<sub>x</sub>", "My": "M<sub>y</sub>"}[a] for a in _miss))
                    if _miss else ""), _n(r, dc=True), "AISC 360-22 H1.1"))
            else:
                rr.append((_q_label(q), "%s / %s %s" % (_n(d_), _n(c_), _q_unit(q)), _n(r, dc=True), ""))
        h.append(_calc("Printed demand &divide; printed available strength", rr))
    if chks:
        h.append("<p class='cnote'>Limit-state checks:</p>")
        h.append(_checks_table(chks, C, waived=m.get("waived")))
    issues = C.entry_findings("member", m, _COMBO_LABELS[0]) if C else []
    dcv = C._dc_num(dc) if C else (dc if isinstance(dc, (int, float)) else None)
    if dcv is not None or dc is not None or ls:
        h.append("<p><b>Governing D/C = %s &mdash; %s</b>%s%s</p>"
                 % ((_n(dcv, dc=True) if dcv is not None else _n(dc)), _dc_status(dcv, issues, m.get("waived")),
                    (" &nbsp;(%s)" % _n(ls)) if ls else "", ("<br><span class='cnote'>%s</span>" % _n(cited)) if cited else ""))
        if m.get("waived") and dcv is not None and dcv > 1.0 + 1e-9:
            h.append("<p class='cnote'>Existing member over capacity: %s.</p>" % _n(m.get("waived")))
    if m.get("design_note"):
        h.append("<p class='cnote'><b>Design note:</b> %s</p>" % _n(m.get("design_note")))
    if issues:
        h.append("<p class='note'>Reconcile before issue: %s</p>"
                 % "; ".join(_pub_issue_html(re.sub(r"^\[member [^\]]*\]\s*", "", i)) for i in issues))
    return "".join(h)


def _connection_block(c, cid=None):
    """One designed connection: demand, components, basis and every limit-state check (Appendix A)."""
    C = _consistency_mod()
    cid = cid if cid is not None else c.get("id", "")
    dem = c.get("demand", c.get("demands", {}))
    cdf = " (capacity design)" if c.get("capacity_design") else ""
    rows = [("Demand", "", _fmt(dem) if isinstance(dem, dict) else _n(dem), "")]
    if c.get("components") or c.get("notes"):
        rows.append(("Components", "", _n(c.get("components", c.get("notes", ""))), ""))
    if c.get("design_basis"):
        rows.append(("Design basis", "", _n(c.get("design_basis")), ""))
    if c.get("cited"):
        rows.append(("Reference", "", _n(c.get("cited")), ""))
    h = ["<h4 id='c-%s'>%s &mdash; %s%s</h4>" % (_esc(re.sub(r"[^A-Za-z0-9_-]", "_", str(cid))), _esc(cid),
                                                _n(c.get("type", "")), cdf),
         "<table class='calc kv'>" + "".join("<tr><td>%s</td><td>%s</td></tr>" % (a, v) for a, _b, v, _r in rows)
         + "</table>"]
    chks = c.get("checks")
    if isinstance(chks, list) and chks:
        h.append(_checks_table(chks, C, member=False, waived=c.get("waived")))
    issues = C._entry_issues("connection", c) if C else []
    top = C._dc_num(c.get("DC")) if C else None
    dcs = [x for x in ([top] + [C._dc_num(k.get("DC")) for k in (chks or []) if isinstance(k, dict)]
                       if C else []) if x is not None]
    if dcs:
        h.append("<p><b>Governing D/C = %s &mdash; %s</b></p>" % (_n(max(dcs), dc=True), _dc_status(max(dcs), issues, c.get("waived"))))
    elif not chks:
        h.append("<p class='note'>Limit-state checks and D/C: not recorded in the calc package for this connection.</p>")
    if c.get("design_note"):
        h.append("<p class='cnote'><b>Design note:</b> %s</p>" % _n(c.get("design_note")))
    if issues:
        h.append("<p class='note'>Reconcile before issue: %s</p>"
                 % "; ".join(_pub_issue_html(re.sub(r"^\[connection [^\]]*\]\s*", "", i)) for i in issues))
    return "".join(h)


def appendix(cfg, name, pkg, a341=None):
    """Appendix A: the AISC 360-22 member and connection calculations of record (calc package)."""
    h = ["<h3>Member calculations</h3>",
         "<p>For each member group: section properties, the analysis demand envelope over all LRFD combinations "
         "(second-order), the available strengths, each limit-state check with its reference, and the governing "
         "D/C. Every ratio is recomputed from the printed demand and available strength; a status of "
         "<b>NG / reconcile</b> marks printed numbers that do not agree.</p>"]
    members = pkg.get("members") if isinstance(pkg, dict) else None
    if isinstance(members, list):
        for m in members:
            if not isinstance(m, dict) or not (m.get("inputs", {}) or {}).get("section"):
                continue
            try: h.append(_member_calc_block(m, a341))
            except Exception as ex: h.append("<p class='note'>[member block could not be rendered: %s]</p>" % _esc(ex))
    else:
        h.append("<p class='note'>No member records in the calc package.</p>")
    cd = pkg.get("connections") or pkg.get("connection_demands")
    if cd:
        any341 = a341 if a341 is not None else bool((cfg.get("seis") or {}).get("R", 0) and float((cfg.get("seis") or {}).get("R", 0)) > 3)
        h.append("<h3>Connection calculations (AISC 360-22 Ch. J / Ch. K%s)</h3>"
                 % ("; AISC 341-22 where required" if any341 else ""))
        items = cd.items() if isinstance(cd, dict) else [(c.get("id", ""), c) for c in cd if isinstance(c, dict)]
        for cid, c in items:
            try: h.append(_connection_block(c, cid))
            except Exception as ex: h.append("<p class='note'>[connection block could not be rendered: %s]</p>" % _esc(ex))
    return "".join(h)

def _num(x):
    try: return float(x)
    except Exception: return 0.0
def _kind_from_id(mid):
    s = (mid or "").lower()
    return "brace" if "brace" in s else ("beam" if "beam" in s else "col")
def _sec_from_id(mid):
    for part in (mid or "").replace("_", "-").split("-"):
        if part and (part[0] in "WHwh") and any(c.isdigit() for c in part): return part.upper()
    return None

# ============================================================ HTML shell
CSS = """body{font-family:Segoe UI,Arial,sans-serif;max-width:1040px;margin:24px auto;color:#222;line-height:1.55}
h1{border-bottom:3px solid #2c5aa0}h2{border-bottom:1px solid #ccc;margin-top:34px;color:#2c5aa0}
h3{color:#2c5aa0;margin-top:22px}h4{margin:18px 0 4px;color:#333}
table{border-collapse:collapse;margin:12px 0;font-size:14px}th,td{border:1px solid #bbb;padding:4px 9px;text-align:right}
th{background:#eef3fb}td:first-child,th:first-child{text-align:left}
figure{display:block;margin:16px 0}img{width:100%;max-width:100%;border:1px solid #ddd}
figcaption{font-size:12px;color:#555;text-align:center}.note{color:#a00;font-style:italic}
pre{background:#f6f8fa;padding:10px;border-radius:6px;overflow:auto;font-size:12px}
h5{margin:16px 0 2px;color:#2c5aa0;font-size:14px}
.cnote{font-size:12px;color:#666;margin:0 0 4px}
table.calc{width:100%;font-size:13px;margin:2px 0 14px}
table.calc td,table.calc th{text-align:left;padding:3px 9px;vertical-align:top}
table.calc td.v{text-align:right;white-space:nowrap;font-weight:600}
table.calc td.r{text-align:right;color:#888;white-space:nowrap;font-size:12px}
table.calc tr.hd th{background:#eef3fb}
table.calc.kv td:first-child{width:16%;font-weight:600;white-space:nowrap}
table.chk-t{font-size:13px;width:100%}table.chk-t td{vertical-align:top}
.ng{color:#b00020}.credit{font-size:13px;color:#444;margin:2px 0 10px}
p.lead{font-size:14px}"""

MATHJAX = ("<script>window.MathJax={tex:{inlineMath:[['\\\\(','\\\\)']],displayMath:[['\\\\[','\\\\]']]}};</script>"
           "<script async src='https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js'></script>")

def _psf(v):
    try:
        return "%s psf" % _n(float(v))
    except (TypeError, ValueError):
        return "&mdash;"


def _partition_psf(cfg, pkg=None):
    """The partition allowance carried in the floor live load (ASCE 7-22 4.3.2), when it is known: an explicit
    cfg['partition_psf'], else the unreduced part declared for the 4.7 live load reduction."""
    for v in (cfg.get("partition_psf"), (cfg.get("live_load_reduction") or {}).get("unreduced_psf")
              if isinstance(cfg.get("live_load_reduction"), dict) else None,
              (((pkg or {}).get("demand_analysis") or {}).get("live_load_reduction") or {}).get("unreduced_psf")
              if isinstance(pkg, dict) else None):
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            pass
    return None


def _levels_txt(d):
    """{1: 10, 2: 10, ...} -> '10 psf on levels 1-7' (grouped by value)."""
    try:
        items = sorted((int(k), float(v)) for k, v in d.items() if float(v or 0) > 0)
    except Exception:
        return ""
    if not items:
        return ""
    by = {}
    for k, v in items:
        by.setdefault(v, []).append(k)
    parts = []
    for v, ks in by.items():
        runs, s = [], ks[0]
        for a, b in zip(ks, ks[1:] + [None]):
            if b is None or b != a + 1:
                runs.append(str(s) if s == a else "%d&ndash;%d" % (s, a)); s = b
        parts.append("%s psf on level%s %s" % (_n(v), "s" if len(ks) > 1 else "", ", ".join(runs)))
    return "; ".join(parts)


def _gravity_loads_table(cfg, pkg=None):
    """Chapter 3 gravity loads (US practice): dead, cladding, live (with the partition allowance as LIVE load per
    ASCE 7-22 4.3.2 when declared), roof live, snow and rain -- values as declared, never invented."""
    part = _partition_psf(cfg, pkg)
    L = cfg.get("L_floor")
    lrow = _psf(L) if L is not None else "&mdash;"
    if part:
        lrow += " (incl. %s psf partition allowance, ASCE 7-22 &sect;4.3.2, treated as live load)" % _n(part)
    if cfg.get("live_basis"):                       # e.g. occupancy blend / Table 4.3-1 items behind L_floor
        lrow += "; " + _esc(str(cfg["live_basis"]))
    rows = [["Floor dead load, D (self-weight + superimposed dead)", _psf(cfg.get("D_floor"))],
            ["Roof dead load, D", _psf(cfg.get("D_roof"))],
            ["Cladding (per unit wall area)", _psf(cfg.get("clad"))],
            ["Floor live load, L<sub>o</sub>", lrow]]
    if len(cfg.get("heights") or []) <= 1 and not cfg.get("top_is_floor"):      # one-story: no floor loads
        rows = [r for r in rows if not r[0].startswith("Floor")]
    if cfg.get("D_by_level"):
        rows.append(["Dead load by level (psf; overrides the floor / roof value at those levels)", _levels_txt(cfg.get("D_by_level"))
                     or _n(cfg.get("D_by_level"))])
    if cfg.get("L_by_level"):
        rows.append(["Floor live load by level", _n(cfg.get("L_by_level"))])
    rows.append(["Roof live load, L<sub>r</sub>", _psf(cfg.get("Lr", 20.0))])
    if cfg.get("Lr_by_level"):
        rows.append(["Roof live load by level (psf; partial roofs and setbacks)", _levels_txt(cfg.get("Lr_by_level"))
                     or _n(cfg.get("Lr_by_level"))])
    snow = float(cfg.get("snow", 0) or 0)
    rain = cfg.get("rain")
    if snow > 0:
        sd = cfg.get("snow_params") if isinstance(cfg.get("snow_params"), dict) else {}
        extra = []
        for k, lab in (("pg", "p<sub>g</sub>"), ("Ce", "C<sub>e</sub>"), ("Ct", "C<sub>t</sub>"), ("Is", "I<sub>s</sub>"),
                       ("pf", "p<sub>f</sub>")):
            v = sd.get(k, cfg.get(k) if k in ("pg",) else None)
            if v is not None:
                extra.append("%s = %s" % (lab, _n(v)))
        rows.append(["Roof snow load, S (ASCE 7-22 Ch. 7)", _psf(snow) + ((" (" + ", ".join(extra) + ")") if extra else "")
                     + (("; " + _esc(cfg["snow_basis"])) if cfg.get("snow_basis") else "")])
    else:
        rows.append(["Roof snow load, S (ASCE 7-22 Ch. 7)", _esc(cfg.get("snow_basis") or "no snow load in the design criteria")])
    rbl = cfg.get("rain_by_level") if isinstance(cfg.get("rain_by_level"), dict) else None
    if rain not in (None, "", 0, {}) or (rbl and any(float(v or 0) > 0 for v in rbl.values())):
        val = (_psf(rain) if isinstance(rain, (int, float)) and rain else (_n(rain) if rain else ""))
        if rbl and any(float(v or 0) > 0 for v in rbl.values()):
            lv = "; ".join("level %s: %s" % (k, _psf(float(v))) for k, v in sorted(rbl.items(), key=lambda t: int(t[0]))
                           if float(v or 0) > 0)
            val = (val + " (" + lv + ")") if val else lv
        rows.append(["Rain load, R (ASCE 7-22 Ch. 8)", val + (("; " + _esc(cfg["rain_basis"])) if cfg.get("rain_basis") else "")])
    else:
        rows.append(["Rain load, R (ASCE 7-22 Ch. 8)", _esc(cfg.get("rain_basis") or "no rain load in the design criteria")])
    emf = cfg.get("extra_mass_floors")
    if isinstance(emf, dict) and _levels_txt(emf):
        rows.append(["Additional dead load by level (in the gravity analysis and in the seismic weight W)",
                     _levels_txt(emf) + " &mdash; " + (_n(cfg.get("extra_mass_basis")) if cfg.get("extra_mass_basis") else
                                                       "dead load carried in addition to the floor and roof dead loads above")])
    smo = cfg.get("seismic_mass_only")
    if isinstance(smo, dict) and _levels_txt(smo):
        rows.append(["Seismic weight only (ASCE 7-22 &sect;12.7.2 item 2 partition weight)", _levels_txt(smo)])
    swm = cfg.get("seismic_weight_members_kip")
    if isinstance(swm, dict) and swm:
        rows.append(["Seismic weight only: member self-weight not in the floor dead load (columns and braces, kip by "
                     "level, &sect;12.7.2)",
                     ", ".join("level %s: %s kip" % (k, _n(float(v))) for k, v in sorted(swm.items(), key=lambda t: int(t[0]))
                               if isinstance(v, (int, float)))
                     + (" &mdash; %s" % _n(cfg.get("seismic_weight_members_basis")) if cfg.get("seismic_weight_members_basis")
                        else " &mdash; the built column and brace sections, each split between the levels above and "
                             "below; the member checks carry the same self-weight")])
    swk = cfg.get("seismic_weight_kip")
    if isinstance(swk, dict) and swk:
        rows.append(["Seismic weight only, lumped (kip by level; %s)" % (_esc(cfg.get("seismic_weight_label"))
                     if cfg.get("seismic_weight_label") else "permanent equipment, storage share and other &sect;12.7.2 items"),
                     ", ".join("level %s: %s kip" % (k, _n(float(v))) for k, v in sorted(swk.items(), key=lambda t: int(t[0]))
                               if isinstance(v, (int, float)))
                     + (" &mdash; %s" % _n(cfg.get("seismic_weight_basis")) if cfg.get("seismic_weight_basis") else "")])
    return _table(["Gravity load", "Value"], rows)


def _diaphragm_kind(cfg):
    """'rigid' | 'flexible' | 'mixed' (rigid floors with cfg['roof_diaphragm'] = 'flexible', e.g. composite floors and a
    bare-deck roof) -- the idealization the report describes (ASCE 7-22 12.3.1)."""
    d = str(cfg.get("diaphragm", "rigid")).lower()
    r = str(cfg.get("roof_diaphragm", "") or "").lower()
    if d.startswith("flex"):
        return "flexible"
    if r.startswith("flex") and len(cfg.get("heights") or []) > 1:
        return "mixed"
    return "rigid"


def _period_coef_txt(cfg, s):
    """Ct / x / Cu -- per direction where cfg['seis_X'] / cfg['seis_Y'] give their own (12.2.2 mixed systems)."""
    one = lambda q: "%s / %s / %s" % (_n(q.get("Ct")), _n(q.get("x")), _n(q.get("Cu")))
    per = {d: _seis_d(cfg, d) for d in ("X", "Y")}
    if any(isinstance(cfg.get("seis_" + d), dict) and any(k in cfg["seis_" + d] for k in ("Ct", "x")) for d in ("X", "Y")) \
            and one(per["X"]) != one(per["Y"]):
        return "X: %s; Y: %s" % (one(per["X"]), one(per["Y"]))
    return one(s)


def _lateral_inputs_table(cfg):
    s = cfg.get("seis") or {}; w = cfg.get("wind", {}) or {}
    rows = [["Seismic S<sub>DS</sub> / S<sub>D1</sub>", "%s / %s g" % (_sa(s.get("SDS")), _sa(s.get("SD1")))],
            ["R / C<sub>d</sub> / &Omega;<sub>0</sub>", _rcdo_txt(cfg, s)],
            ["Importance factor I<sub>e</sub>", _fac(s.get("Ie", 1.0))],
            ["Period coefficients C<sub>t</sub> / x / C<sub>u</sub>", _period_coef_txt(cfg, s)]]
    if w:
        rows.append(["Basic wind speed V / Exposure", "%s mph / %s" % (_n(w.get("V")), _esc(w.get("exposure", "C")))])
    return _table(["Lateral-load input", "Value"], rows)


def _wind_cases_present(cases):
    """Which ASCE 7-22 Fig. 27.3-8 MWFRS load cases the combination set contains ({1, 2, 3, 4} subset)."""
    out = set()
    for c in cases or []:
        lab = str(c[0]) if isinstance(c, (tuple, list)) else str(c)
        wc = getattr(c, "wind_case", None)
        if wc in (2, 3, 4):
            out.add(int(wc))
        for k in (2, 3, 4):
            if "[C%d" % k in lab:
                out.add(k)
        if ("WX" in lab or "WY" in lab) and not any("[C%d" % k in lab for k in (2, 3, 4)):
            out.add(1)
    return out


def _wind_cases_txt(cases):
    wc = _wind_cases_present(cases)
    if not wc:
        return ""
    if {2, 3, 4} <= wc:
        return ("The MWFRS load cases of ASCE 7-22 Fig. 27.3-8 are analysed: Case 1 (full design wind pressure along "
                "each principal axis), Case 2 (75% of Case 1 with a torsional moment from the eccentricity "
                "e = &plusmn;0.15B), Case 3 (75% along both axes simultaneously) and Case 4 (56.3% along both axes with "
                "the eccentricity); each enters the strength combinations of Chapter 4.")
    if wc & {2, 3, 4}:
        return ("ASCE 7-22 Fig. 27.3-8 load cases analysed: Case %s (see Chapter 4); the remaining cases were limited "
                "where ASCE 7-22 Appendix D permits." % ", ".join(str(k) for k in sorted(wc)))
    return ("Only Case 1 of ASCE 7-22 Fig. 27.3-8 (full design wind pressure along each principal axis) is applied; "
            "the torsional and combined Cases 2&ndash;4 are not included in this analysis.")


def _wind_section(cfg, cases=None):
    """Returns (html, VwX, VwY). VwX/VwY are base shears (None if no wind defined)."""
    if not cfg.get("wind"):
        return "<p class='cnote'>No wind load is defined for this building.</p>", None, None
    try:
        w = cfg["wind"]; DX = E.wind_detail(cfg, "X"); DY = E.wind_detail(cfg, "Y")
        FX = E.wind_forces(cfg, "X"); FY = E.wind_forces(cfg, "Y")      # honours any cfg-level patch
        VwX = sum(FX.values()); VwY = sum(FY.values()); NF = len(cfg["heights"])
        rows = [[k, _n(FX.get(k, 0.0)), _n(FY.get(k, 0.0))] for k in range(1, NF+1)]
        patched = any(abs(FX.get(k, 0.0)-DX["F"].get(k, 0.0)) > 1e-6 or abs(FY.get(k, 0.0)-DY["F"].get(k, 0.0)) > 1e-6
                      for k in range(1, NF+1))
        ctxt = _wind_cases_txt(cases)
        if DX["method"] == "override" or DY["method"] == "override" or patched:
            # cfg['wind_forces_basis'] states how the supplied story forces were derived (else a neutral line)
            _wfb = cfg.get("wind_forces_basis") or ((cfg.get("wind") or {}).get("story_forces_basis"))
            h = ["<p>ASCE 7-22 Ch. 27 MWFRS story forces%s.</p>"
                 % ((": " + _esc(str(_wfb))) if _wfb else " from the design wind calculation recorded with the design"),
                 "<p><b>Story wind forces (kip):</b></p>",
                 _table(["Story", "X (E-W wind)", "Y (N-S wind)"], rows),
                 f"<p><b>Wind base shear:</b> X = {_n(VwX)} kip, Y = {_n(VwY)} kip. {ctxt}</p>"]
            return "".join(h), VwX, VwY
        def g(D):
            if D["rigid"]:
                return "%s (rigid, &sect;26.11.4)" % _n(D["G"], nd=3)
            src = _pub(str(D.get("n1_source") or ""))
            m_ = re.search(r"1/T\s*=\s*1/([0-9.]+)\s*s", src)
            if src.lower().startswith("modal"):
                stxt = ("from the modal analysis of this model" + (" (n<sub>1</sub> = 1/T, T = %s s)" % m_.group(1) if m_ else "")
                        + ", &sect;26.11.2")
            elif "given" in src.lower() or "declared" in src.lower() or "rational" in src.lower():
                stxt = "from a substantiated structural analysis (&sect;26.11.2)"
            elif "approximate" in src.lower():
                stxt = "approximate n<sub>a</sub> (&sect;26.11.3)"
            else:
                stxt = _esc(src)
            return ("%s (flexible building, &sect;26.11.5: n<sub>1</sub> = %s Hz &lt; 1 Hz, %s; G<sub>f</sub> by "
                    "Eq. 26.11-10)" % (_n(D["G"], nd=3), _n(D.get("n1"), nd=3), stxt))
        h = ["<p>ASCE 7-22 Ch. 27 directional procedure (MWFRS). q<sub>z</sub> = 0.00256&middot;K<sub>z</sub>K<sub>zt</sub>"
             "K<sub>e</sub>V<sup>2</sup> (Eq. 26.10-1); p = q&middot;K<sub>d</sub>&middot;G&middot;C<sub>p</sub> (Eq. 27.3-1): "
             "windward walls C<sub>p</sub> = +0.8 with q<sub>z</sub> at each height, leeward walls C<sub>p</sub>(L/B) per "
             "Fig. 27.3-1 with q<sub>h</sub> at the mean roof height h; internal pressure cancels in the net story shear. "
             "Wall loads are shared between the two diaphragms each wall spans (mid-height bands of the building volume); "
             + ("the whole lowest story band is carried by the lowest level (cfg ground band 'lowest', conservative for the "
                "frames). " if str((cfg.get("wind") or {}).get("ground_band", "base")).lower() == "lowest" else
                "the bottom half of the lowest story goes directly to the foundation. ")
             + "Each level is not less than the "
             "&sect;27.1.5 minimum (16 psf walls, 8 psf roof). The MWFRS roof suction (Fig. 27.3-1 roof C<sub>p</sub>) is "
             "applied in the 0.9D + 1.0W combinations. " + ctxt + "</p>",
             _table(["Parameter", "Value"], [["Basic wind speed V", "%s mph" % _n(w.get("V"))],
                    ["Exposure / K<sub>d</sub> / K<sub>zt</sub> / K<sub>e</sub>", "%s / %s / %s / %s" % (
                        _esc(DX['exposure']), _fac(w.get('Kd', 0.85)), _fac(w.get('Kzt', 1.0)), _fac(w.get('Ke', 1.0)))],
                    ["Mean roof height h", "%s ft" % _n(DX['h_ft'])],
                    ["Gust-effect factor, wind along X", g(DX)],
                    ["Gust-effect factor, wind along Y", g(DY)],
                    ["L/B and leeward C<sub>p</sub> (X / Y)", "%s, %s / %s, %s" % (
                        _n(DX['L_over_B'], nd=2), _n(DX['Cp_leeward'], nd=2), _n(DY['L_over_B'], nd=2),
                        _n(DY['Cp_leeward'], nd=2))]]),
             "<p><b>Story wind forces, Case 1 (kip):</b></p>",
             _table(["Story", "X (E-W wind)", "Y (N-S wind)"], rows),
             f"<p><b>Wind base shear (frames):</b> X = {_n(VwX)} kip, Y = {_n(VwY)} kip "
             f"(plus {_n(DX['F_base_direct'])} / {_n(DY['F_base_direct'])} kip delivered directly to the foundation).</p>"]
        for m in sorted(set(DX["warnings"]) | set(DY["warnings"])):
            if str(m).startswith("FLEXIBLE building"):
                continue                                   # shown in the gust-effect rows above
            h.append(f"<p class='note'>Wind note: {_n(m)}</p>")
        return "".join(h), VwX, VwY
    except Exception as ex:
        return f"<p class='note'>[wind determination failed: {_esc(ex)}]</p>", None, None


def _horizontal_distribution(cfg, Fx, VwX, VwY, sf=None, runs=None):
    """Per direction: the declared SFRS, its lateral planes in the model, wind vs seismic base shear,
    and the share of the base shear each plane actually takes in the solved 3D model (rigid
    diaphragm, no accidental torsion) -- not an assumed 50/50 split."""
    sf = sf or _sfrs(cfg)
    FxX, FxY = _fx_by_dir(Fx)
    if runs:                                     # the per-direction ELF forces the drift runs used
        FxX, FxY = runs["X"]["F"], runs["Y"]["F"]
    _dk = _diaphragm_kind(cfg)
    h = [("<p>Story forces reach the vertical lateral elements through the flexible diaphragm by tributary area "
          "(&sect;12.3.1.1; the 3D model ties each level for the global solution only). The table gives, per direction, "
          if _dk == "flexible" else
          "<p>Story forces reach the vertical lateral elements through the rigid floor diaphragms in proportion to their "
          "lateral stiffness and position (3D model)%s. The table gives, per direction, "
          % ("; the flexible roof diaphragm distributes the roof force by tributary area (&sect;12.3.1.1)"
             if _dk == "mixed" else ""))
         + 
         "the system, the lateral planes found in the analysis model and the fraction of the base shear each "
         "plane carries in the solved model (centre-of-mass loading; the &plusmn;5% accidental torsion of "
         "&sect;12.8.4.2 and any A<sub>x</sub> amplification are applied in the design combinations and shift "
         "demand toward the plane on the leading side).</p>"]
    rows = []
    for d, Vw, F in (("X", VwX, FxX), ("Y", VwY, FxY)):
        Vs = sum(F.values()); gov = "seismic" if Vs >= (Vw or 0) else "wind"
        shares = (runs or {}).get(d, {}).get("share") or {}
        ax = "y" if d == "X" else "x"
        if shares:
            parts = ["%s = %.0f ft: %.0f%%" % (ax, p / 12.0, 100 * v) for p, v in sorted(
                ((p, v) for p, v in shares.items() if p is not None), key=lambda t: t[0])]
            if shares.get(None) and abs(shares[None]) > 0.005:
                parts.append("other columns<sup>a</sup>: %.0f%%" % (100 * shares[None]))
            sh = "; ".join(parts)
        else:
            sh = "not computed"
        rows.append([f"{d} ({'E-W' if d == 'X' else 'N-S'})", _sys_label(sf, d), _line_desc(sf, d),
                     (_n(Vw) if Vw is not None else "&mdash;"), _n(Vs), gov, sh])
    h.append(_table(["Direction", "System", "Lateral planes (model)", "Wind base (kip)", "Seismic base (kip)",
                     "Governs", "Base-shear share by plane (solved model)"], rows))
    if any("other columns<sup>a</sup>" in r[-1] for r in rows):
        h.append("<p class='cnote'><sup>a</sup> Shear in the columns outside the lateral planes: the second-order "
                 "(P-&Delta;) shear of the gravity columns, which lean on the lateral system; a negative share means the "
                 "planes carry slightly more than the applied story force.</p>")
    for w in sf.get("warnings", []):
        h.append("<p class='note'>%s</p>" % _n(w))
    return "".join(h)

def _role_of(mid, inp):
    r = (inp.get("role") or "").lower().strip().replace(" ", "_")
    _alias = {"roof": "roof", "roof_beam": "roof", "floor": "floor", "floor_beam": "floor",
              "gravity_col": "gravity_col", "interior_col": "gravity_col", "interior_column": "gravity_col",
              "gravity_column": "gravity_col", "lateral_col": "lateral_col", "mf_col": "lateral_col",
              "moment_col": "lateral_col", "braced_col": "lateral_col", "lateral_column": "lateral_col",
              "brace": "brace"}
    if r in _alias: return _alias[r]
    s = (mid or "").lower()
    if "roof" in s: return "roof"
    if "brace" in s or inp.get("kind") == "brace": return "brace"
    if "col" in s or inp.get("kind") == "col":
        return "gravity_col" if ("grav" in s or "interior" in s) else "lateral_col"
    if "floor" in s or "beam" in s or inp.get("kind") == "beam": return "floor"
    return "other"

def _envelope_txt(inp):
    """Compact printed envelope: 'P = 1,094 kip; T = 555 kip; M<sub>x</sub> = 9 kip-ft; V = 0.6 kip' (non-zero only)."""
    out = []
    for lab, k, sc, u in (("P", "P_comp_kip", 1.0, "kip"), ("T", "P_tens_kip", 1.0, "kip"),
                          ("M<sub>x</sub>", "Mz_kipin", 1.0 / 12.0, "kip-ft"), ("M<sub>y</sub>", "My_kipin", 1.0 / 12.0, "kip-ft"),
                          ("V", "V_kip", 1.0, "kip")):
        try:
            v = float(inp.get(k))
        except (TypeError, ValueError):
            continue
        if abs(v) > 1e-9:
            out.append("%s = %s %s" % (lab, _n(v * sc), u))
    return "; ".join(out) if out else "&mdash;"


_COMBO_LABELS = [None]     # current combination labels (set by build_report) for the stale-demand check


def _member_rows(members):
    C = _consistency_mod()
    rows = []
    for m in members:
        inp = m.get("inputs", {}) or {}
        dcv = C._dc_num(m.get("DC")) if C else (m.get("DC") if isinstance(m.get("DC"), (int, float)) else None)
        if dcv is None and C and isinstance(m.get("checks"), list):
            dd = [C._dc_num(k.get("DC")) for k in m["checks"] if isinstance(k, dict)]
            dd = [x for x in dd if x is not None]
            dcv = max(dd) if dd else None
        issues = C.entry_findings("member", m, _COMBO_LABELS[0]) if C else []
        mid = str(m.get("id", ""))
        rows.append(["<a href='#m-%s'>%s</a>" % (_esc(re.sub(r"[^A-Za-z0-9_-]", "_", mid)), _esc(mid)),
                     _esc(inp.get("section", "")), _envelope_txt(inp), _combo_label(inp.get("governing_combo")),
                     _n(m.get("limit_state")) if m.get("limit_state") else "&mdash;",
                     _n(dcv, dc=True) if dcv is not None else "&mdash;",
                     _dc_status(dcv, issues, m.get("waived")) if (dcv is not None or issues) else "not recorded"])
    return rows


def _member_section(cfg, name, pkg, want_roles, title, intro):
    """Chapter 6 section for one member role: a summary table (envelope, governing combination, limit state,
    D/C, status) linked to the full calculation in Appendix A."""
    h = [f"<h3>{title}</h3>", f"<p>{intro}</p>"]
    members = pkg.get("members") if isinstance(pkg, dict) else None
    sel = []
    if isinstance(members, list):
        for m in members:
            if not isinstance(m, dict):
                continue
            inp = m.get("inputs", {}) or {}
            if _role_of(m.get("id", ""), inp) not in want_roles or not inp.get("section"):
                continue
            sel.append(m)
    if not sel:
        _has = lambda roles: any(isinstance(m, dict) and _role_of(m.get("id", ""), m.get("inputs", {}) or {}) in roles
                                 for m in (members or []))
        if "gravity_col" in want_roles and _has({"lateral_col"}):
            h.append("<p class='cnote'>Not applicable: every column of this building is part of the seismic "
                     "force-resisting system (lateral-system columns below).</p>")
        else:
            h.append("<p class='cnote'>This building has no members of this type.</p>")
        return "".join(h)
    try:
        h.append(_table(["Member", "Section", "Demand envelope", "Governing combination", "Governing limit state",
                         "D/C", "Status"], _member_rows(sel)))
    except Exception as ex:
        h.append(f"<p class='note'>[member summary could not be rendered: {_esc(ex)}]</p>")
    return "".join(h)

def _case_extremes(out, info):
    """Overall max |axial| and max |moment| across all members for one load case."""
    reg = {t: (kind, sec) for (t, kind, sec, n1, n2) in info["ele"]}
    maxN = (0.0, "", ""); maxM = (0.0, "", "")
    for t, (N, Mz, My, V) in out.items():
        kind, sec = reg.get(t, ("", ""))
        if abs(N) > abs(maxN[0]): maxN = (N, kind, sec)
        m = max(abs(Mz), abs(My))
        if m > abs(maxM[0]): maxM = (m, kind, sec)
    return maxN, maxM

def _stability_section(cfg, runs):
    """Chapter 7: ASCE 7-22 12.8.7 stability coefficient, BOTH directions (see _theta_rows)."""
    h = ["<p>Member demands include second-order effects (a P-&Delta; geometric transformation is applied "
         "under every combination). The ASCE 7-22 &sect;12.8.7 stability coefficient is "
         "&theta; = P<sub>x</sub>&Delta;<sub>xe</sub>/(V<sub>x</sub>h<sub>sx</sub>) (Eq. 12.8-18), identical to "
         "P<sub>x</sub>&Delta;I<sub>e</sub>/(V<sub>x</sub>h<sub>sx</sub>C<sub>d</sub>) with "
         "&Delta; = C<sub>d</sub>&Delta;<sub>xe</sub>/I<sub>e</sub>. P<sub>x</sub> is the &sect;12.8.6.1 expected "
         "gravity 1.0D + 0.5L with L = 0.4L<sub>0</sub> (0.8L<sub>0</sub> where L<sub>0</sub> &gt; 100 psf) on the "
         "actual framed area &mdash; no individual load factor exceeds 1.0. &Delta;<sub>xe</sub> is the elastic "
         "story drift at the design-drift location (Chapter 8)%s. Because the drifts come from a P-&Delta; analysis, "
         "&theta; is divided by (1+&theta;) before the Eq. 12.8-19 check, as &sect;12.8.7 permits. "
         "&theta;<sub>max</sub> = 0.5/(&beta;C<sub>d</sub>) &le; 0.25 and need not be taken less than 0.10; "
         "&beta; = %s.</p>"
         % (" including the AISC 358-22 &sect;5.7 RBS drift factor" if any(runs[d]["rbs"][0] > 1.0 for d in ("X", "Y")) else "",
            ("%s (declared; not less than 1.25/&Omega;<sub>0</sub>)" % _n(cfg.get("theta_beta"))) if cfg.get("theta_beta")
            else "1.0 (conservative; not less than 1.25/&Omega;<sub>0</sub>)")]
    worst = {}
    for d in ("X", "Y"):
        rows, tmax, beta = _theta_rows(cfg, runs, d)
        trs = []; wv = 0.0
        for r in rows:
            if r["exempt"]:
                st, pd = "exempt (declared offset)", "&mdash;"
            else:
                st = "<b class='ng'>NG &mdash; redesign</b>" if r["theta"] > tmax else "OK"
                pd = ("included (&theta; &gt; 0.10)" if r["theta"] > 0.10 else
                      "need not be considered (&theta; &le; 0.10); included")
            if not r["exempt"]: wv = max(wv, r["theta"])
            trs.append([r["s"], _n(float(r["Px"])), _n(float(r["Vx"])), _n(float(r["h"])), "%.3f" % r["dxe"],
                        "%.3f" % r["Delta"], "%.3f" % r["raw"], "%.3f" % r["theta"], st, pd])
        worst[d] = (wv, tmax)
        h.append("<h4>%s direction (C<sub>d</sub> = %s, &beta; = %.2f, &theta;<sub>max</sub> = %.3f)</h4>"
                 % (d, _n(float(runs[d]["Cd"])), beta, tmax))
        h.append(_table(["Story", "P<sub>x</sub> (kip)", "V<sub>x</sub> (kip)", "h<sub>sx</sub> (in.)",
                         "&Delta;<sub>xe</sub> (in.)", "&Delta; (in.)", "&theta; (Eq. 12.8-18)", "&theta;/(1+&theta;)",
                         "&theta; &le; &theta;<sub>max</sub> (Eq. 12.8-19)", "P-&Delta; (&sect;12.8.7)"], trs))
    h.append("<p>Worst-story &theta;: X = %.3f (limit %.3f), Y = %.3f (limit %.3f) &mdash; %s.</p>"
             % (worst["X"][0], worst["X"][1], worst["Y"][0], worst["Y"][1],
                "both within &theta;<sub>max</sub>" if all(worst[d][0] <= worst[d][1] for d in worst)
                else "<b>EXCEEDS &theta;<sub>max</sub>: the structure is potentially unstable and shall be redesigned (&sect;12.8.7)</b>"))
    return "".join(h)

def _load_activity(name):
    """Records for this job. Reads jobs/<name>/activity_log.jsonl AND folds in any scratch-log SESSIONS that
    belong to this job -- e.g. work done after a server restart that logged to scratch because new_activity_log()
    was not re-called (its RAG queries would otherwise be invisible). Returns (recs_sorted, primary_source)."""
    _jobs = os.environ.get("STEEL_BUILDER_JOBS") or os.path.join(HERE, "desktop_app", "agent_workspace", "work", "claude_desktop", "jobs")
    def _read(p):
        out = []
        try:
            for line in open(p, encoding="utf-8"):
                if line.strip():
                    try: out.append(json.loads(line))
                    except Exception: pass
        except Exception: pass
        return out
    src = None; jrecs = []
    for c in (os.path.join(_jobs, name, "activity_log.jsonl"),
              os.path.join(HERE, "desktop_app", "agent_workspace", "work", name, "activity_log.jsonl"),
              os.path.join(HERE, "desktop_app", "agent_workspace", "work", "claude_desktop", "activity_log.jsonl")):
        if os.path.exists(c):
            src = c; jrecs = _read(c); break
    scratch = os.environ.get("STEEL_BUILDER_SCRATCH") or os.path.join(os.path.dirname(_jobs), ".scratch")
    srecs = _read(os.path.join(scratch, "activity_log.jsonl"))
    extra = []
    if srecs:
        def refs(r):
            if r.get("tool") == "new_activity_log" and str(r.get("detail", "")).strip() == name:
                return True
            d = str(r.get("detail", "")) + " " + str(r.get("result", ""))
            return ("jobs/" + name + "/" in d) or ("jobs/" + name in d) or (os.sep + name + os.sep in d)
        sessions = []; cur = []
        for r in srecs:
            st = r.get("step", 0)
            if cur and isinstance(st, int) and st <= cur[-1].get("step", 0):
                sessions.append(cur); cur = []
            cur.append(r)
        if cur: sessions.append(cur)
        for s in sessions:
            if any(refs(r) for r in s): extra += s
    seen = set(); merged = []
    for r in jrecs + extra:
        k = (r.get("tool"), str(r.get("detail"))[:200])     # a step re-run repeats its look-ups: list each once
        if k in seen: continue
        seen.add(k); merged.append(r)
    merged.sort(key=lambda r: (r.get("ts") or "", r.get("step") or 0))
    return merged, src

_COLL_NAME = {"engineering_standards_A360": "AISC 360-22", "engineering_standards_A341": "AISC 341-22",
              "engineering_standards_A358": "AISC 358-22", "engineering_standards_A342": "AISC 342-22",
              "engineering_standards_ASCE7": "ASCE/SEI 7-22", "engineering_standards_ASCE41": "ASCE/SEI 41-23",
              "engineering_standards_S100": "AISI S100-16 (2020)", "engineering_standards_S240": "AISI S240-20",
              "engineering_standards_S400": "AISI S400-20", "engineering_standards_S230": "AISI S230-19",
              "steel_design_examples": "AISC design examples"}
_TOOL_NAME = {"search_engineering_standards": "code provision look-up", "run_python": "analysis / check run",
              "new_activity_log": "design log started"}


def _log_detail(tool, d):
    """Design-log entry in engineering terms (no tool syntax, file names or paths)."""
    d = str(d or "")
    if tool == "search_engineering_standards":
        m = re.match(r"\[([^\]]+)\]\s*(?:exact_section|exact_equation|exact_table|eq|table|fts|keyword)?\s*(.*)$", d)
        if m:
            std = _COLL_NAME.get(m.group(1), m.group(1).replace("engineering_standards_", ""))
            rest = re.sub(r"^\u00ab|\u00bb", "", m.group(2)).replace("\u00ab", "\u201c").replace("\u00bb", "\u201d")
            return "%s %s" % (std, rest)
        return d
    if tool == "run_python":
        for pat, txt in ((r"design_and_report|run_job.*design", "analysis model, demand envelope and report"),
                         (r"consistency\.check", "self-consistency check of the calculation package"),
                         (r"build_report", "report rendering"), (r"fill", "member and connection design")):
            if re.search(pat, d):
                return txt
        return "analysis run"
    if tool == "new_activity_log":
        return "design started"
    return _PATH_RX.sub(lambda mm: os.path.basename(mm.group(0)), d)


def _log_result(tool, r):
    r = str(r or "")
    m = re.match(r"(\d+) hits?", r)
    if tool == "search_engineering_standards":
        if not m or m.group(1) == "0":
            return "no match in the standards corpus"
        return "%s matching provision%s" % (m.group(1), "" if m.group(1) == "1" else "s")
    m = re.search(r"(\d+) issues?", r)
    if m:
        return "%s issues" % m.group(1)
    return "completed" if ("rc=0" in r or r in ("started", "")) else ("returned an error" if "rc=" in r else "")


def _activity_section(name):
    """QA-annex design log: the analysis runs and code-provision look-ups recorded during the design (never part
    of the published report body), in engineering terms."""
    recs, src = _load_activity(name)
    h = ["<h2>Design log</h2>",
         "<p>Analysis runs, self-consistency checks and code-provision look-ups recorded while this design was "
         "prepared. A look-up with no match in the standards corpus is listed as such; the provisions the report "
         "cites are the ones found.</p>"]
    if not recs:
        h.append("<p class='cnote'>No activity log was recorded for this job.</p>")
        return "".join(h)
    counts = {}
    for r in recs:
        k = _TOOL_NAME.get(r.get("tool", "other"), str(r.get("tool", "other")).replace("_", " "))
        counts[k] = counts.get(k, 0) + 1
    h.append(f"<p>{len(recs)} entries.</p>")
    h.append(_table(["Entry type", "Count"], [[_esc(k), counts[k]] for k in sorted(counts)]))
    # entries in order, without clock times: the record is re-logged when the calculation package is re-filled
    # after a re-analysis, so its times say when it was last filled, not when the design was done
    rows = [[i + 1,
             _esc(_TOOL_NAME.get(r.get("tool", ""), str(r.get("tool", "")).replace("_", " "))),
             _esc(_log_detail(r.get("tool", ""), r.get("detail", "")))[:600],
             _esc(_log_result(r.get("tool", ""), r.get("result", "")))[:300]] for i, r in enumerate(recs)]
    h.append("<h3>Record (in order; re-logged at the last fill of the calculation package)</h3>"
             + _table(["#", "entry", "detail", "result"], rows))
    return "".join(h)


def _load_pkg(name, root=None):
    cands = []
    if root: cands.append(os.path.join(root, "design", "calc_package.json"))
    cands += [os.path.join(HERE, name, "design", "calc_package.json"),
              os.path.join(HERE, "desktop_app", "agent_workspace", "work", name, "calc_package.json"),
              os.path.join(HERE, "buildings", name, "design", "calc_package.json")]
    for c in cands:
        if os.path.exists(c):
            try: return json.load(open(c, encoding="utf-8")), c
            except Exception: pass
    return None, None



CHK_CSS = ("ol.toc{font-size:14px}ol.toc li{margin:2px 0}"
           ".chk{background:#f3f7fd;border:1px solid #cfe;border-left:4px solid #2c5aa0;"
           "border-radius:4px;padding:8px 14px;margin:8px 0 16px}"
           ".chk-h{font-weight:600;color:#2c5aa0;font-size:13px;margin-bottom:4px}"
           ".chk ul{margin:4px 0 2px 0;padding-left:20px}.chk li{font-size:13px;color:#333;margin:2px 0}"
           ".chk-status{font-weight:400;font-style:italic;color:#a06000;font-size:12px}")

def _chapters(ctx=None):
    """EOR acceptance checklist per chapter, worded for THIS building: AISC 341 / 358 items only where AISC 341
    applies (R > 3 or a declared AISC 341 system) and AISC 358 only for SMF / IMF; wind items where wind is
    defined. ctx keys: a341 (bool, any direction), mf358 (SMF/IMF present), wind (bool)."""
    ctx = ctx or {}
    a341 = bool(ctx.get("a341", True)); mf358 = bool(ctx.get("mf358", a341)); wind = ctx.get("wind", True)
    codes = "IBC, ASCE 7-22, AISC 360-22" + (", AISC 341-22" if a341 else "") + (", AISC 358-22" if mf358 else "") \
        + ", AWS D1.1" + ("" if a341 else " &mdash; AISC 341 does not apply: R = 3 system not specifically detailed for seismic resistance")
    ch9 = (("Seismic / wind detailing (AISC 341)", [
               "System detailing matches the R used (width-thickness, brace slenderness, protected zones).",
               "Capacity design: " + ("SCWB (E3.4a), " if ctx.get("smf") else "") +
               "columns/collectors for &Omega;<sub>0</sub> or expected strength (R<sub>y</sub>F<sub>y</sub>), as the system requires.",
               "Demand-critical welds" + (", prequalified moment connections (AISC 358)" if mf358 else "") +
               ", continuity/doubler plates and panel-zone shear where moment frames are present.",
               "Wind: C&amp;C on cladding/fasteners, net uplift load path and hold-downs."])
           if a341 else
           ("Seismic / wind detailing", [
               "System detailing consistent with the R used: R = 3 system not specifically detailed for seismic "
               "resistance (ASCE 7-22 Table 12.2-1, row H) &mdash; AISC 341 does not apply; members and connections "
               "designed to AISC 360-22 for the analysis forces.",
               "Overstrength (&Omega;<sub>0</sub>) applied only where ASCE 7-22 &sect;12.4.3 requires it (&sect;12.3.3.4 "
               "discontinuities, &sect;12.10.2.1 collectors in SDC C&ndash;F).",
               "Wind: net uplift load path to the foundation (column bases and anchorage) and C&amp;C of the cladding."]))
    return {
     1: ("Design basis & codes", [
       "Governing codes and editions stated (%s); Risk Category and Importance Factors." % codes,
       "Project criteria match the architectural/owner brief and the geotechnical report (site class, bearing, lateral soil, frost).",
       "Units, sign conventions and material specifications stated and consistent throughout.",
       "Scope and design-responsibility boundaries defined (connections designed in this package; delegated/deferred items listed)."]),
     2: ("Structural system & load path", [
       "Gravity and lateral load paths complete and continuous to the foundation in both orthogonal directions.",
       "Lateral system per direction identified with the correct R, &Omega;<sub>0</sub>, C<sub>d</sub>, &rho; and any height/redundancy/system limits.",
       "Diaphragm type (rigid/semi-rigid/flexible) justified; collectors/drag struts and chord forces addressed.",
       "Irregularities (plan &amp; vertical) and expansion/seismic joints identified; triggered provisions applied."]),
     3: ("Loads", [
       "Dead loads: realistic self-weight, superimposed (MEP, ceilings, finishes), cladding/fa&ccedil;ade; partitions as live load (&sect;4.3.2).",
       "Live loads per occupancy with correct reductions (&sect;4.7) and not where prohibited.",
       "Roof live/snow/rain: flat-roof snow, drift, unbalanced, rain-on-snow, ponding, minimums &mdash; as applicable at the site.",
       ("Wind (Ch. 26-31): MWFRS incl. the Fig. 27.3-8 load cases; C&amp;C." if wind else "Wind: not applicable / not defined."),
       "Seismic (Ch. 11-12): S<sub>DS</sub>/S<sub>D1</sub>, SDC, period, C<sub>s</sub>, V, vertical distribution, E<sub>v</sub>, accidental+amplified torsion, 100/30, &rho; &mdash; per direction.",
       "Governing case (wind vs seismic) identified per direction."]),
     4: ("Load combinations", [
       "Full ASCE 7-22 &sect;2.3 LRFD set (gravity, wind &plusmn;, seismic &plusmn; with E<sub>v</sub> &amp; &rho;, &Omega;<sub>0</sub> where required), 100/30, &plusmn; accidental torsion.",
       "Combinations applied to the right members (&Omega;<sub>0</sub> to capacity-protected elements only, where required); uplift/net tension (0.9D) checked.",
       "Second-order effects handled per combination (no superposition of factored second-order results)."]),
     5: ("Analysis model fidelity", [
       "Geometry, member sizes and orientations match the drawings (strong/weak axis correct).",
       "Boundary conditions: column base fixity matches the base plate/anchorage detail and the foundation's capacity.",
       "Connection idealization (rigid / pinned / PR releases) matches the detailed connections.",
       "Diaphragm constraint, rigid offsets/panel zones, and leaning columns with their P-&Delta; represented.",
       "Modal results sane: &ge; 90% participating mass, reasonable periods, expected mode shapes.",
       "Equilibrium verified: &Sigma;R = applied base shear (each direction) and total factored gravity.",
       "Stiffness reductions / notional loads / effective-length basis consistent with the analysis method."]),
     6: ("Member strength design (AISC 360)", [
       "Every member type checked for the governing combination: tension (D2/D3), compression (E3/E4/E7), flexure (F2-F8 correct limit state), shear (G2), interaction (H1).",
       "Correct limit state, &phi;, L<sub>b</sub>, C<sub>b</sub>, K, slenderness and section properties.",
       "Composite members (Ch. I) designed properly if used (PNA, &phi;M<sub>n</sub>, studs, shoring, construction-stage deflection).",
       "Concentrated-load limit states (J10) and stiffeners where required.",
       "Governing D/C &le; 1.0 with sensible margins; sections reasonably economical.",
       "Calculations cited to specific AISC clauses and independently re-derivable; 2-3 governing members spot-checked."]),
     7: ("Stability & second-order", [
       "Stability method (Direct Analysis Method preferred) applied correctly end-to-end.",
       "B<sub>2</sub> / &theta; per story within limits; P-&Delta; included in member demands."]),
     8: ("Serviceability", [
       "Seismic design drift &delta; = C<sub>d</sub>&delta;<sub>xe</sub>/I<sub>e</sub> &le; allowable; wind drift &le; project limit under service wind; interstory drift compatible with cladding/partitions.",
       "Deflections: floor LL &le; L/360, TL &le; L/240; camber; roof ponding; long-span/cantilever limits.",
       "Floor vibration (AISC Design Guide 11) for the occupancy where applicable.",
       "Building separation / pounding; differential movement at joints."]),
     9: ch9,
     10: ("Connections", [
       "Connection demands (V, N, M" + (", incl. capacity-design/overstrength where required" if a341 else ", from the analysis") + ") on the drawings.",
       "Connections designed in this package (sized components; every limit-state D/C &le; 1.0); only shop-level detailing on the fabricator submittal.",
       "Critical connections checked: bolts (J3), welds (J2), block shear (J4), HSS (Ch. K), base plates/anchors (J8/J9 + ACI 318 Ch. 17); single-plate shear connections per AISC Manual Part 10 (bolt-group eccentricity, plate ductility).",
       "Constructability: erection stability, access, OSHA minimum 4 anchor rods."]),
     11: ("Foundations interface", [
       "Column base reactions (P/V/M, uplift, overturning) provided to foundation design; consistent with assumed base fixity.",
       "Overall overturning/sliding stability; net uplift anchorage; load cases to geotech consistent with bearing/lateral capacity."]),
     12: ("Drawings, specifications & documentation", [
       "Drawings internally consistent and consistent with the calculations.",
       "General notes, material specs, welding/bolting, special-inspection schedule and design loads shown.",
       "Member schedules, framing plans, brace/MF elevations and connection details complete and coordinated.",
       "Deferred-submittal items listed; assumptions and limitations documented."]),
     13: ("QA / professional acceptance", [
       "Independent check performed (different engineer or method); discrepancies resolved.",
       "Software validated/appropriate; analysis assumptions vs detailing reconciled.",
       "Special inspection &amp; testing program (IBC Ch. 17) defined.",
       "Calc package complete, traceable (input &rarr; demand &rarr; capacity &rarr; D/C &rarr; cited clause), archived.",
       "All open items / RFIs closed; assumptions confirmed.",
       "EOR satisfied the design meets the governing codes and the standard of care &mdash; apply seal/signature."]),
    }


CHAPTERS = _chapters()
_CH_CTX = [None]          # the current report's checklist context (set by build_report)


def _toc(ctx=None, appendix_b=False):
    ch = _chapters(ctx if ctx is not None else _CH_CTX[0])
    rows = "".join(f"<li><b>Chapter {n}</b> &mdash; {ch[n][0]}</li>" for n in range(1, 14))
    return ("<h2>Report structure &mdash; EOR review checklist</h2>"
            "<p>This report is organised as the engineer-of-record acceptance checklist: one chapter per checklist "
            "section. Each chapter opens with the items a reviewer must accept, followed by the supporting analysis "
            "and design evidence. Appendix A contains the AISC 360-22 member and connection calculations"
            + ("; Appendix B the member forces for each load combination" if appendix_b else "") + ".</p>"
            "<ol class='toc'>" + rows + "</ol>")


def _chapter(n, status=None, ctx=None):
    title, items = _chapters(ctx if ctx is not None else _CH_CTX[0])[n]
    li = "".join(f"<li>{t}</li>" for t in items)
    extra = f" &mdash; <span class='chk-status'>{status}</span>" if status else ""
    return (f"<h2>Chapter {n} &mdash; {title}</h2>"
            f"<div class='chk'><div class='chk-h'>Reviewer acceptance items{extra}</div><ul>{li}</ul></div>")


def _risk_category(Ie, cfg=None):
    """Risk Category: cfg['risk_category'] when declared (preflight.risk_cat_of_cfg), else from I_e."""
    if isinstance(cfg, dict):
        try:
            from preflight import risk_cat_of_cfg
            return risk_cat_of_cfg(cfg)
        except Exception:
            pass
    return {1.0: "II", 1.25: "III", 1.5: "IV"}.get(round(float(Ie), 2), "II")


# ============================================================ SFRS description (HR-26)
# Every system label in the report is driven from cfg['system'] (per direction where the text or
# cfg['system_X'/'system_Y'] / cfg['seis_X'/'seis_Y']['system'] says so) checked against the frame
# inventory of the BUILT model (brace planes and fixed-ended beam lines per direction). Nothing
# defaults to "moment frame" or "two frames".
_FAM_RX = [
    ("NSD",  r"not\s+specifically\s+detailed|\bNSD\b"),
    ("STMF", r"\bSTMF\b|truss[ -]+moment"),
    ("SMF",  r"\bSMF\b|special\s+moment"),
    ("IMF",  r"\bIMF\b|intermediate\s+moment"),
    ("OMF",  r"\bOMF\b|ordinary\s+moment"),
    ("SCBF", r"\bSCBF\b|special\s+concentric"),
    ("OCBF", r"\b(?:MT-)?OCBF\b|ordinary\s+concentric"),
    ("BRBF", r"\bBRBF\b|buckling[- ]+restrained"),
    ("EBF",  r"\bEBF\b|eccentric(?:ally)?\s+braced"),
    ("CC-PSW", r"\bCC-?PSW\b|coupled\s+composite\s+plate\s+shear"),
    ("C-PSW", r"\bC-?PSW\b|(?<!coupled\s)composite\s+plate\s+shear"),
    ("SPSW", r"\bSPSW\b|(?<!composite )plate\s+shear\s+wall"),
]
MF_FAMS = ("SMF", "IMF", "OMF", "STMF")
BR_FAMS = ("SCBF", "OCBF", "BRBF", "EBF", "SPSW", "C-PSW")   # plate-shear-wall strips are modelled as brace elements
WALL_FAMS = ("CC-PSW",)   # coupled composite walls: wall piers (wide columns) + fixed-ended coupling beams
FAM_NAME = {"SMF": "special moment frame (SMF)", "IMF": "intermediate moment frame (IMF)",
            "OMF": "ordinary moment frame (OMF)", "STMF": "special truss moment frame (STMF)",
            "SCBF": "special concentrically braced frame (SCBF)",
            "OCBF": "ordinary concentrically braced frame (OCBF)",
            "BRBF": "buckling-restrained braced frame (BRBF)",
            "EBF": "eccentrically braced frame (EBF)", "SPSW": "special plate shear wall (SPSW)",
            "C-PSW": "steel and concrete composite plate shear wall (C-PSW)",
            "CC-PSW": "steel and concrete coupled composite plate shear wall (CC-PSW/CF)",
            "NSD": "steel system not specifically detailed for seismic resistance (AISC 360)",
            "MF?": "steel moment frame (SFRS not declared)", "BF?": "steel braced frame (SFRS not declared)"}
FAM_341 = {"SMF": "E3", "IMF": "E2", "OMF": "E1", "STMF": "E4", "SCBF": "F2", "OCBF": "F1",
           "BRBF": "F4", "EBF": "F3", "SPSW": "F5", "C-PSW": "H7", "CC-PSW": "H8"}


def _families(text):
    """Ordered list of the Table 12.2-1 steel system families named in a free-text system declaration. An
    explicit 'not specifically detailed' (NSD, row H) declaration governs: 'OMF-type' etc. is then description."""
    t = str(text or ""); hits = []
    for fam, rx in _FAM_RX:
        m = re.search(rx, t, flags=re.I)
        if m: hits.append((m.start(), fam))
    fams = [f for _, f in sorted(hits)]
    return ["NSD"] if "NSD" in fams else fams


def _families_by_dir(text):
    """{'X': [...], 'Y': [...]} when the text assigns families to a direction ('SMF in X',
    'X: SCBF', 'SCBF (Grids 1-7) in Y'); directions not mentioned get None."""
    t = str(text or ""); out = {"X": None, "Y": None}
    for fam, rx in _FAM_RX:
        for m in re.finditer(rx, t, flags=re.I):
            tail = t[m.end():m.end() + 40]; head = t[max(0, m.start() - 12):m.start()]
            d = None
            mm = re.match(r"\s*(?:\([^)]*\))?\s*(?:in|along)\s+(?:the\s+)?([XY])\b", tail)
            if mm: d = mm.group(1)
            else:
                mm = re.search(r"\b([XY])\s*(?:-dir\w*)?\s*[:=]\s*$", head)
                if mm: d = mm.group(1)
            if d:
                out[d] = (out[d] or []) + ([fam] if fam not in (out[d] or []) else [])
    return out


_INV_CACHE = {}


def _frame_inventory(cfg):
    """Lateral-element inventory of the BUILT model, per direction:
         inv[d]['brace'][pos] = {story: [(lo, hi), ...]}   brace / SPSW-strip elements in plane pos
         inv[d]['mf'][pos]    = {story: [(lo, hi), ...]}   fixed-ended (moment) beams at the top of story
       pos = the plane coordinate (y for X-direction planes, x for Y-direction), in inches; (lo, hi) is
       the element extent along the direction, snapped to the bounding column lines. Also returns
       'nonparallel' (skewed lateral elements) and 'base' {node: (x, y, fixity)}."""
    key = id(cfg)
    if key in _INV_CACHE:
        return _INV_CACHE[key]
    inv = {"X": {"brace": {}, "mf": {}}, "Y": {"brace": {}, "mf": {}}, "nonparallel": [], "base": {},
           "ok": False, "error": None}
    try:
        info = E.build(cfg, "Linear")
        z = list(E.zlevels(cfg)); NF = len(cfg["heights"]); tol = 1.0
        rel = info.get("beam_rel") or {}
        joints = str(((cfg.get("model") or {}).get("joints")) or "").lower()
        xs_all = sorted({round(ops.nodeCoord(n)[0], 1) for n in ops.getNodeTags()})
        ys_all = sorted({round(ops.nodeCoord(n)[1], 1) for n in ops.getNodeTags()})
        def _snap(lo, hi, lines):
            a = max([v for v in lines if v <= lo + tol] or [lo]); b = min([v for v in lines if v >= hi - tol] or [hi])
            return (round(a, 1), round(b, 1))
        def _lvl(zz):
            return min(range(len(z)), key=lambda k: abs(z[k] - zz))
        def _key(dct, pos):
            for p in dct:
                if abs(p - pos) <= 12.0: return p
            return round(pos, 1)
        for (t, kind, sec, n1, n2) in info["ele"]:
            try: a = ops.nodeCoord(n1); b = ops.nodeCoord(n2)
            except Exception: continue
            dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
            h = math.hypot(dx, dy)
            k = str(kind).lower()
            if "brace" in k:
                typ = "brace"
            elif "beam" in k and abs(dz) < tol and h > tol:
                r = rel.get(t)
                fixed = (r[0] == "none") if r is not None else not joints.startswith("pin")
                if not fixed: continue
                typ = "mf"
            else:
                continue
            if h <= tol: continue
            ang = math.degrees(math.atan2(abs(dy), abs(dx)))
            if 1.0 < ang < 89.0:
                inv["nonparallel"].append((t, kind, round(ang, 1))); continue
            d = "X" if ang <= 1.0 else "Y"
            pos = 0.5 * (a[1] + b[1]) if d == "X" else 0.5 * (a[0] + b[0])
            lo, hi = (min(a[0], b[0]), max(a[0], b[0])) if d == "X" else (min(a[1], b[1]), max(a[1], b[1]))
            lo, hi = _snap(lo, hi, xs_all if d == "X" else ys_all)
            if typ == "brace":
                k1, k2 = sorted((_lvl(a[2]), _lvl(b[2])))
                stories = range(k1 + 1, k2 + 1) if k2 > k1 else [max(k1, 1)]
            else:
                stories = [max(_lvl(a[2]), 1)]
            plane = inv[d][typ].setdefault(_key(inv[d][typ], pos), {})
            for s in stories:
                if 1 <= s <= NF: plane.setdefault(s, []).append((lo, hi))
        try:
            for n in ops.getFixedNodes():
                c = ops.nodeCoord(n)
                if abs(c[2] - z[0]) <= tol:
                    dofs = set(ops.getFixedDOFs(n))
                    inv["base"][n] = (c[0], c[1], "fixed" if {4, 5}.issubset(dofs) else "pinned")
        except Exception:
            pass
        inv["ok"] = True
    except Exception as ex:
        inv["error"] = str(ex)
    _INV_CACHE[key] = inv
    return inv


def _sfrs(cfg, inv=None):
    """System description per direction. Returns dict(declared, recognised, text, dirs={'X': [fam..], 'Y': [..]},
    dual, mixed, families, has_mf, has_br, warnings, lines={'X': {...}, 'Y': {...}}, kinds={'X': {'mf','br'}}).
    The engine's own resolution (engine3d.sfrs_key of engine3d.system_dir per direction) is used first, so the
    report, the seismic factors and preflight agree; the free-text parser is the fallback. 'declared' means a
    system string exists (cfg system / system_X / system_Y / seis system); 'recognised' that it maps to a
    Table 12.2-1 steel row."""
    inv = inv if inv is not None else _frame_inventory(cfg)
    s = cfg.get("seis") or {}
    text = str(cfg.get("system") or s.get("system") or "")
    per = _families_by_dir(text)
    for d in ("X", "Y"):
        for src_ in (cfg.get("system_" + d), (cfg.get("seis_" + d) or {}).get("system")
                     if isinstance(cfg.get("seis_" + d), dict) else None):
            if src_:
                per[d] = _families(src_) or per[d]
        try:                                                   # the engine's resolution first (one parser)
            k = E.sfrs_key(E.system_dir(cfg, d))
            if k and k in FAM_NAME:
                per[d] = [k]
        except Exception:
            pass
    glob = _families(text)
    declared = bool(text.strip() or cfg.get("system_X") or cfg.get("system_Y") or
                    any(isinstance(cfg.get("seis_" + d), dict) and cfg["seis_" + d].get("system") for d in ("X", "Y")))
    recognised = bool(glob or per["X"] or per["Y"])
    warnings = []
    have = {d: {"mf": bool(inv[d]["mf"]) if inv.get("ok") else None,
                "br": bool(inv[d]["brace"]) if inv.get("ok") else None} for d in ("X", "Y")}
    dirs = {}; kinds = {}
    wall_dirs = {str(x).upper() for x in (cfg.get("wall_dirs") or ())}
    for d in ("X", "Y"):
        cand = per[d] if per[d] else glob
        if cand:
            if inv.get("ok"):
                got = [f for f in cand if (f in MF_FAMS and have[d]["mf"]) or (f in BR_FAMS and have[d]["br"])
                       or (f in WALL_FAMS and (have[d]["mf"] or have[d]["br"] or d in wall_dirs))
                       or (f == "NSD" and (have[d]["mf"] or have[d]["br"]))]
                if not got:
                    warnings.append("%s direction: the declared %s has no matching lateral elements in the built "
                                    "model (no %s found in %s-direction planes)"
                                    % (d, "/".join(FAM_NAME.get(f, f) for f in cand),
                                       "braces" if all(f in BR_FAMS for f in cand) else
                                       "fixed-ended beams" if all(f in MF_FAMS for f in cand) else
                                       "braces or fixed-ended beams", d))
                    got = list(cand)
            else:
                got = list(cand)
        else:
            # undeclared / unrecognised: infer the frame type from the model (braces win; fixed-ended beams alone = MF)
            if inv.get("ok") and have[d]["br"]: got = ["BF?"]
            elif inv.get("ok") and have[d]["mf"]: got = ["MF?"]
            else: got = ["BF?"] if E.is_braced(cfg) else ["MF?"]
        dirs[d] = got
        kd = set()
        for f in got:
            if f in MF_FAMS or f == "MF?": kd.add("mf")
            if f in BR_FAMS or f == "BF?": kd.add("br")
            if f in WALL_FAMS:                          # wall planes are found through their coupling beams
                kd.add("wall")
                if have[d]["mf"]: kd.add("mf")
                if have[d]["br"]: kd.add("br")
            if f == "NSD":
                if have[d]["br"] or (have[d]["br"] is None and E.is_braced(cfg)): kd.add("br")
                if have[d]["mf"]: kd.add("mf")
        kinds[d] = kd
    fams = []
    for d in ("X", "Y"):
        for f in dirs[d]:
            if f not in fams: fams.append(f)
    has_mf = any("mf" in kinds[d] for d in ("X", "Y"))
    has_br = any("br" in kinds[d] for d in ("X", "Y"))
    dual = "dual" in text.lower() or bool(cfg.get("dual_check"))
    mixed = ("mixed" in text.lower()) or (set(dirs["X"]) != set(dirs["Y"]))
    lines = {}
    for d in ("X", "Y"):
        use_mf = "mf" in kinds[d]
        use_br = "br" in kinds[d]
        planes = {}
        if inv.get("ok"):
            if use_br:
                for p, v in inv[d]["brace"].items():
                    cur = planes.setdefault(p, {})
                    for st, iv in v.items(): cur[st] = cur.get(st, []) + iv
            if use_mf:
                for p, v in inv[d]["mf"].items():
                    cur = planes.setdefault(p, {})
                    for st, iv in v.items(): cur[st] = cur.get(st, []) + iv
        lines[d] = planes
    return dict(declared=declared, recognised=recognised, text=text, dirs=dirs, dual=dual, mixed=mixed,
                families=fams, has_mf=has_mf, has_br=has_br, warnings=warnings, lines=lines, kinds=kinds,
                inv_ok=bool(inv.get("ok")))


def _sys_label(sf, d):
    fams = sf["dirs"][d]
    names = []
    for f in fams:
        nm = FAM_NAME.get(f, f)
        if f == "NSD":
            kd = (sf.get("kinds") or {}).get(d) or set()
            frm = ("braced and moment frames" if kd >= {"br", "mf"} else "braced frames" if "br" in kd else
                   "moment frames" if "mf" in kd else "")
            nm = "steel system not specifically detailed for seismic resistance%s" % ((" &mdash; " + frm) if frm else "")
        names.append(nm)
    names = " + ".join(names)
    if sf["dual"] and any(f in MF_FAMS for f in fams) and any(f in BR_FAMS + WALL_FAMS for f in fams):
        names = "dual system: " + names
    return names


def _line_desc(sf, d):
    """'3 lines at y = 0, 45, 90 ft' for the SFRS planes in direction d."""
    pl = sorted(sf["lines"].get(d) or {})
    if not pl:
        return "no lateral planes found in the model" if sf.get("inv_ok") else "frame inventory unavailable"
    ax = "y" if d == "X" else "x"
    return "%d line%s at %s = %s ft" % (len(pl), "" if len(pl) == 1 else "s", ax,
                                         ", ".join("%.0f" % (p / 12.0) for p in pl))


def _seis_d(cfg, d):
    """Seismic factors for direction d: cfg['seis'] overlaid by cfg['seis_X'/'seis_Y'] when given
    (ASCE 7-22 12.2.2: each system's R, Cd, Omega0 apply in its own direction)."""
    s = dict(cfg.get("seis") or {})
    o = cfg.get("seis_" + d)
    if isinstance(o, dict): s.update({k: v for k, v in o.items() if v is not None})
    return s


def _fx_by_dir(Fx):
    """ELF story forces per direction. Accepts the legacy single {k: F} dict (same forces both
    directions) or a per-direction {'X': {k: F}, 'Y': {k: F}} dict."""
    if isinstance(Fx, dict) and set(Fx.keys()) >= {"X", "Y"} and isinstance(Fx.get("X"), dict):
        return Fx["X"], Fx["Y"]
    return Fx, Fx


def _drift_exempt(cfg):
    try: return {int(k) for k in (cfg.get("drift_exempt_stories") or {})}
    except Exception: return set()


def _drift_limit_d(cfg, d):
    """(limit, rho_applied) for direction d -- per-direction when engine3d supports it."""
    try: return E.drift_allowable(cfg, d)
    except TypeError: return E.drift_allowable(cfg)


def _lateral_runs(cfg, Fx, sf=None):
    """Seismic drift / torsion / stiffness evidence per direction, from the SAME analysis routine the
    engine's drift gate uses (engine3d.static_lateral: ELF story forces at the floor masters, P-Delta
    model, unfactored gravity per engine3d.floor_grav). For each direction d returns:
      drift_cm[s]   elastic story drift ratio at the centre of mass (delta_xe/h_sx), no accidental torsion
      drift_edge[s] elastic story drift ratio at the worst diaphragm edge with +5% accidental torsion
      tir[s]        Eq. 12.3-2 TIR = Delta_max / Delta_avg (edge drifts, accidental torsion, Ax = 1)
      ax[s]         Eq. 12.8-15 A_x = (delta_max / 1.2 delta_avg)^2 in [1, 3] (displacements)
      k[s]          story stiffness V_x / Delta_xe (kip/in) from the CM run
      share         {plane: fraction of the base shear} from the base reactions of the CM run
    ASCE 7-22 12.8.6.5: where a Type 1 irregularity exists in SDC C-F the design drift is taken at the
    edge; otherwise at the centre of mass. 'basis' records which one the report uses."""
    FxX, FxY = _fx_by_dir(Fx)
    SF = None
    if hasattr(E, "seismic_design_forces"):        # per-direction seismic engine: use ITS ELF forces
        try:
            SF = E.seismic_design_forces(cfg)
            FxX, FxY = dict(SF["X"]["F_elf"]), dict(SF["Y"]["F_elf"])
        except Exception:
            SF = None
    NF = len(cfg["heights"]); h = cfg["heights"]
    out = {}
    for d, F in (("X", FxX), ("Y", FxY)):
        r = {"F": F}
        res = E.static_lateral(cfg, F, d)
        dr = list(res[2]); r["drift_cm"] = dr
        Vst = [sum(F[k] for k in range(s, NF + 1)) for s in range(1, NF + 1)]
        r["V"] = Vst
        r["k"] = [(Vst[s] / abs(dr[s] * h[s])) if abs(dr[s]) > 1e-12 else float("inf") for s in range(NF)]
        # base-shear share per lateral plane, from the solved reactions of this (live) run
        share = {}
        try:
            ops.reactions(); di = 1 if d == "X" else 2; tot = 0.0
            planes = sorted((sf or {}).get("lines", {}).get(d, {}) or {})
            for n in ops.getFixedNodes():
                c = ops.nodeCoord(n)
                if abs(c[2]) > 1.0: continue
                rx = ops.nodeReaction(n, di); tot += rx
                pos = c[1] if d == "X" else c[0]
                p = next((pp for pp in planes if abs(pp - pos) <= 12.0), None)
                share[p] = share.get(p, 0.0) + rx
            if abs(tot) > 1e-9:
                share = {p: v / tot for p, v in share.items()}
            else:
                share = {}
        except Exception:
            share = {}
        r["share"] = share
        # accidental-torsion run: edge drifts, TIR, Ax (masters + present column nodes of the live model)
        try:
            E.static_lateral(cfg, F, d, accidental=True)
            di = 1 if d == "X" else 2
            edge = []; tir = []; ax = []; prev_e = (0.0, 0.0); _tags = set(ops.getNodeTags())
            for k in range(1, NF + 1):
                m = E.mtag(k); cm = ops.nodeCoord(m)
                u = ops.nodeDisp(m, di); rz = ops.nodeDisp(m, 6)
                pts = [ops.nodeCoord(E.ntag(i, j, k)) for (i, j) in E.grid(cfg, k)
                       if E.ntag(i, j, k) in _tags]
                if d == "X":
                    cs = [p[1] for p in pts] or [cm[1]]; lo, hi = min(cs) - cm[1], max(cs) - cm[1]
                    e1, e2 = u - rz * lo, u - rz * hi            # X displacement at the two Y-edges
                else:
                    cs = [p[0] for p in pts] or [cm[0]]; lo, hi = min(cs) - cm[0], max(cs) - cm[0]
                    e1, e2 = u + rz * lo, u + rz * hi            # Y displacement at the two X-edges
                d1, d2 = e1 - prev_e[0], e2 - prev_e[1]; prev_e = (e1, e2)
                dmax = max(abs(d1), abs(d2)); davg = 0.5 * (abs(d1) + abs(d2))
                edge.append(dmax / h[k - 1]); tir.append(dmax / davg if davg > 1e-12 else 1.0)
                Dmax = max(abs(e1), abs(e2)); Davg = 0.5 * (abs(e1) + abs(e2))
                ax.append(min(max((Dmax / (1.2 * Davg)) ** 2, 1.0), 3.0) if Davg > 1e-12 else 1.0)
            r["drift_edge"] = edge; r["tir"] = tir; r["ax"] = ax
        except Exception as ex:
            r["drift_edge"] = list(dr); r["tir"] = [1.0] * NF; r["ax"] = [1.0] * NF; r["tor_error"] = str(ex)
        out[d] = r
    exempt = _drift_exempt(cfg)
    try:
        sdc = E.sdc_of(cfg)
    except Exception:
        sdc = "D"
    # the engine's single torsion / design-drift source, when present (per-direction seismic engine):
    # TIR per story, Ax per level and the 12.8.6.5 drift profile come from it, so the report prints the
    # gate's numbers; otherwise the runs above are used.
    ts = sd_eng = None
    if hasattr(E, "torsion_summary") and hasattr(E, "seismic_drift"):
        try:
            ts = E.torsion_summary(cfg); sd_eng = E.seismic_drift(cfg)
        except Exception:
            ts = sd_eng = None
    if ts is not None:
        for d in ("X", "Y"):
            out[d]["tir"] = [t if t is not None else 1.0 for t in ts[d].get("TIR_story", out[d]["tir"])]
            out[d]["ax"] = [ts[d]["Ax"].get(k, 1.0) for k in range(1, NF + 1)]
            out[d]["drift_edge"] = list(ts[d].get("edge_drift", out[d]["drift_edge"]))
    tir_max = max([t for d in ("X", "Y") for s, t in enumerate(out[d]["tir"], 1) if s not in exempt] or [1.0])
    edge_basis = (bool(ts["edges"]) if ts is not None else (tir_max > 1.2 and sdc in ("C", "D", "E", "F")))
    for d in ("X", "Y"):
        r = out[d]
        sdd = _seis_d(cfg, d)
        if hasattr(E, "seis_dir"):
            try: sdd = E.seis_dir(cfg, d)
            except Exception: pass
        Cd = float(sdd.get("Cd", 5.0) or 5.0); Ie = float(sdd.get("Ie", 1.0) or 1.0)
        r["Cd"], r["Ie"] = Cd, Ie
        if sd_eng is not None:
            e = sd_eng[d]
            f_rep, why_rep = rbs_drift_factor(cfg, d, sf)
            f_eng = (e.get("rbs") or (1.0, ""))[0]
            pre = "rbs" in e                                       # engine profile already carries its factor
            # apply only what the engine profile does not already include (e.g. RBS found in the calc package)
            f = (f_rep / f_eng) if (pre and f_rep > f_eng) else (1.0 if pre else f_rep)
            r["rbs"] = (max(f_rep, f_eng), why_rep if f_rep >= f_eng else e["rbs"][1])
            pre = False                                            # 'f' is now the extra factor to apply
            r["basis"] = ("diaphragm edge incl. accidental torsion &times; A<sub>x</sub> (Type 1, SDC %s: &sect;12.8.6.5)" % sdc
                          if "edge" in str(e.get("location", "")) else "centre of mass (&sect;12.8.6.5)")
            if e.get("mrsa") is not None:
                _db = _demand_basis(cfg)
                if _db == "MRSA":
                    r["basis"] += "; MRSA drifts (&sect;12.9.1.4.2)"
                elif e.get("design") == e.get("elf"):
                    r["basis"] += "; ELF governs over the &sect;12.9.1.4.2 MRSA drifts in every story"
                elif e.get("design") == e.get("mrsa"):
                    r["basis"] += "; the &sect;12.9.1.4.2 MRSA drifts govern over the ELF drifts in every story"
                else:
                    r["basis"] += "; story-by-story envelope of the ELF and the &sect;12.9.1.4.2 MRSA drifts"
            r["dxe"] = [abs(v) * f for v in e["elastic"]]
            r["design"] = [abs(v) * f for v in e["design"]]
            r["limit"], r["limit_rho"] = e["limit"], e["rho_applied"]
        else:
            f, why = rbs_drift_factor(cfg, d, sf)
            r["rbs"] = (f, why)
            base = r["drift_edge"] if edge_basis else r["drift_cm"]
            r["basis"] = ("diaphragm edge incl. accidental torsion (Type 1 TIR %.2f &gt; 1.2, SDC %s: &sect;12.8.6.5)"
                          % (tir_max, sdc)) if edge_basis else "centre of mass (&sect;12.8.6.5; no Type 1 irregularity)"
            r["dxe"] = [abs(v) * f for v in base]                  # elastic drift ratio at the design-drift location
            r["design"] = [v * Cd / Ie for v in r["dxe"]]          # Eq. 12.8-16 design drift ratio
            lim, rho = _drift_limit_d(cfg, d); r["limit"], r["limit_rho"] = lim, rho
        gov = [v for s, v in enumerate(r["design"], 1) if s not in exempt]
        r["design_max"] = max(gov) if gov else max(r["design"])
    out["tir_max"] = tir_max; out["edge_basis"] = edge_basis; out["exempt"] = exempt; out["sdc"] = sdc
    return out


def _theta_rows(cfg, runs, d):
    """ASCE 7-22 12.8.7 stability coefficient per story for direction d.
        theta = P_x * Delta_xe / (V_x * h_sx)     (Eq. 12.8-18; = P_x*Delta*I_e/(V_x*h_sx*C_d) with
                                                  Delta = C_d*Delta_xe/I_e the design story drift)
      P_x  = sum over levels >= x of (1.0 D + 0.5 L), L = 0.4 L0 (0.8 L0 where L0 > 100 psf) --
             the 12.8.6.1 expected gravity; no individual load factor exceeds 1.0 (12.8.7);
      Delta_xe at the design-drift location (runs[d]['basis']) incl. any AISC 358 RBS factor;
      theta_max = 0.5/(beta*C_d) <= 0.25, beta = cfg['theta_beta'] (default 1.0, >= 1.25/Omega0),
                  and theta_max need not be taken less than 0.10 (Eq. 12.8-19 and text).
    The drifts come from a P-Delta analysis, so theta from Eq. 12.8-18 is divided by (1 + theta)
    before the Eq. 12.8-19 check, as 12.8.7 permits; both values are reported."""
    r = runs[d]
    if hasattr(E, "theta_rows"):
        # ONE implementation shared with the stability_theta gate (engine3d.theta_rows); the report passes
        # its drift profile (the engine's 12.8.6.5 elastic drift x any extra RBS factor found only in the
        # calc package) and story shears, so Ch. 7, the QA table and the gate agree
        try:
            # MRSA / envelope basis: V=None lets the engine pair the story shear with the analysis the drift comes
            # from (MRSA drift with the MRSA story shear, 12.8.7) -- the ELF shear paired an MRSA drift with an ELF shear
            _Vp = None if _demand_basis(cfg) in ("MRSA", "envelope") else r["V"]
            rows, tmax, beta = E.theta_rows(cfg, d, dxe=r["dxe"], V=_Vp, Cd=r.get("Cd"), Ie=r.get("Ie"))
            ex = runs["exempt"]
            for x in rows: x["exempt"] = x["exempt"] or (x["s"] in ex)
            return rows, tmax, beta
        except Exception:
            pass
    NF = len(cfg["heights"]); h = cfg["heights"]
    sd = _seis_d(cfg, d); Cd = r["Cd"]; Om0 = float(sd.get("Om0", 3.0) or 3.0)
    beta = max(float(cfg.get("theta_beta", 1.0) or 1.0), 1.25 / Om0)
    tmax = max(min(0.5 / (beta * Cd), 0.25), 0.10)
    P = {}
    for k in range(1, NF + 1):
        Dk = E.floor_dead(cfg, k)
        if k < NF:
            L0 = float(E._Llev(cfg, k) or 0.0)
            Lk = (0.8 if L0 > 100.0 else 0.4) * L0 * E.floor_area_ft2(cfg, k) / 1000.0
        else:
            Lk = 0.0
        P[k] = Dk + 0.5 * Lk
    rows = []
    for s in range(1, NF + 1):
        Px = sum(P[k] for k in range(s, NF + 1)); Vx = r["V"][s - 1]; dxe = r["dxe"][s - 1] * h[s - 1]
        th_raw = Px * dxe / (Vx * h[s - 1]) if Vx > 1e-9 else 0.0
        th = th_raw / (1.0 + th_raw)
        rows.append(dict(s=s, Px=Px, Vx=Vx, h=h[s - 1], dxe=dxe, Delta=dxe * Cd / r["Ie"],
                         raw=th_raw, theta=th, exempt=(s in runs["exempt"])))
    return rows, tmax, beta


def rbs_drift_factor(cfg, direction=None, sf=None):
    """AISC 358-22 sec.5.7 Step 1: elastic drift of an RBS moment frame is computed with the effect of
    the reduced section; in lieu of detailed calculation, multiply gross-section elastic drifts by 1.1
    for flange reductions up to 50% of bbf, interpolating linearly for smaller reductions.
    Delegates to engine3d.rbs_drift_factor when present (the drift GATE uses the same value)."""
    fn = getattr(E, "rbs_drift_factor", None)
    f, why = fn(cfg, direction) if fn is not None else (1.0, "no RBS declared")
    mf_here = True
    if sf is not None and direction:
        mf_here = any(x in MF_FAMS or x == "MF?" for x in sf["dirs"].get(direction, []))
    if f <= 1.0 and mf_here and _PKG_RBS[0] and not cfg.get("rbs") and cfg.get("rbs_drift_factor") is None:
        # the calc package designs RBS connections but the cfg does not declare them: apply 1.1 here and say
        # loudly that the engine drift gate (which reads cfg only) omitted it
        if "only" not in why:
            return 1.1, ("RBS connections appear in calc_package.json but cfg['rbs'] is not declared -- 50% flange "
                         "reduction assumed (1.1) in this report; the engine drift gate did NOT apply it: declare cfg['rbs']")
    return f, why


_PKG_RBS = [False]


def _drift_relief_note(cfg):
    """Chapter 8 paragraph when cfg['drift_relief_16_1_2'] is in force (ASCE 7-22 16.1.2: a Chapter 16
    analysis relaxes the 12.12.1 drift limits for Risk Category I-III). Empty string otherwise; the
    RC IV misuse is reported as a preflight/consistency ERROR, and named here so the reader sees it."""
    try:
        from preflight import drift_relief, relief_active
        r = drift_relief(cfg)
    except Exception:
        return ""
    if r is None:
        return ""
    Ie = float((cfg.get("seis") or {}).get("Ie", 1.0) or 1.0)
    if not relief_active(cfg):
        return ("<p class='note'><b>An ASCE 7-22 &sect;16.1.2 drift-relief declaration is present but NOT in force</b> "
                + ("(Risk Category IV, I<sub>e</sub> = %.2f: ASCE 7-22 &sect;16.1.2 keeps the Table 12.12-1 limits)" % Ie
                   if Ie >= 1.5 else "(block incomplete -- see the preflight findings)")
                + " &mdash; the Table 12.12-1 limit above governs.</p>")
    def _f(k, scale=100.0, fmt="%.2f%%"):
        v = r.get(k)
        return (fmt % (float(v) * scale)) if isinstance(v, (int, float)) else "&mdash;"
    table_val = r.get("table_12_12_1") if isinstance(r.get("table_12_12_1"), (int, float)) else (0.015 if Ie >= 1.25 else 0.020)
    return ("<p class='cnote'><b>ASCE 7-22 &sect;16.1.2 drift relief in force.</b> A Chapter 16 nonlinear response "
            "history analysis of this building (nonlinear analysis record %s%s) gave a suite-mean MCE<sub>R</sub> "
            "story drift of %s against the &sect;16.4.1.2 limit of %s (verdict: %s). For Risk Category I&ndash;III the "
            "&sect;12.12.1 limits therefore need not apply; the linear design target was reset to %s of story height "
            "(the Table 12.12-1 value would be %s). <b>This design is provisional until the Chapter 16 analysis is "
            "re-run on it and passes &sect;16.4.</b>%s</p>"
            % (_esc(str(r.get("nlrha_job") or "not recorded")),
               (" run " + _esc(str(r.get("nlrha_run")))) if r.get("nlrha_run") else "",
               _f("nlrha_mean_drift"), _f("nlrha_limit"), _esc(str(r.get("nlrha_verdict") or "not recorded")),
               _f("linear_target"), "%.2f%%" % (100.0 * float(table_val)),
               (" " + _esc(str(r.get("note")))) if r.get("note") else ""))


def _design_of_record_rows(root):
    """Rows for the Chapter 1 basis table when design/design_of_record.json exists (written by the
    Nonlinear module's feedback loop when the user promotes a verified re-design)."""
    try:
        p = os.path.join(root, "design", "design_of_record.json")
        if not os.path.exists(p):
            return []
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return []
    what = "verified re-design %s%s, adopted %s" % (
        _n(d.get("loop")), (" (%s)" % _n(d.get("loop_title"))) if d.get("loop_title") else "", _n(d.get("promoted_at")))
    ver = d.get("verification") or {}
    vs = "; ".join("%s: %s" % (_esc(str(k)), _esc(str(v))) for k, v in ver.items() if not isinstance(v, (dict, list)))
    rows = [["Design of record", what + ((" &mdash; " + vs) if vs else "")]]
    if d.get("supersedes"):
        rows.append(["Supersedes", _esc(str(d["supersedes"]))])
    return rows


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

def _sdc(SDS, SD1, S1=0.0, Ie=1.0):
    """Seismic Design Category per ASCE 7-22 sec.11.6 -- delegates to the canonical
    preflight.asce_sdc: worse of Tables 11.6-1/-2 (incl. the Risk Category IV column, RC from Ie),
    after the S1 >= 0.75 -> E (RC I-III) / F (RC IV) override."""
    from preflight import asce_sdc, risk_cat_from_Ie
    return asce_sdc(SDS, SD1, S1, risk_cat_from_Ie(Ie))

def _rcdo_txt(cfg, s):
    """R / Cd / Omega0 -- per direction when the two directions differ (ASCE 7-22 12.2.2, engine3d.seis_dir)."""
    try:
        d = {k: E.seis_dir(cfg, k) for k in ("X", "Y")}
        t = {k: "%s / %s / %s" % (_fac(d[k].get("R")), _fac(d[k].get("Cd")), _fac(d[k].get("Om0"))) for k in d}
        return t["X"] if t["X"] == t["Y"] else "X: %s; Y: %s" % (t["X"], t["Y"])
    except Exception:
        return "%s / %s / %s" % (_fac(s.get("R")), _fac(s.get("Cd")), _fac(s.get("Om0")))

def _sdc_tables(SDS, SD1, S1=0.0, rc="II"):
    """(SDC by Table 11.6-1, SDC by Table 11.6-2, S1 override or None) -- ASCE 7-22 11.6."""
    rc4 = str(rc).upper() in ("IV", "4")
    def tab(x, rows):
        for thr, a, b in rows:
            if float(x) < thr:
                return b if rc4 else a
        return "D"
    t1 = tab(SDS, [(0.167, "A", "A"), (0.33, "B", "C"), (0.50, "C", "D")])
    t2 = tab(SD1, [(0.067, "A", "A"), (0.133, "B", "C"), (0.20, "C", "D")])
    ov = ("F" if rc4 else "E") if float(S1 or 0) >= 0.75 else None
    return t1, t2, ov


def _rho_txt(cfg, sdc):
    try:
        r = {d: E.seis_dir(cfg, d).get("rho") for d in ("X", "Y")}
    except Exception:
        r = {d: cfg.get("rho") for d in ("X", "Y")}
    txt = _fac(r["X"]) if r["X"] == r["Y"] else "X: %s; Y: %s" % (_fac(r["X"]), _fac(r["Y"]))
    declared = any(cfg.get(k) is not None for k in ("rho", "rho_X", "rho_Y")) or (cfg.get("seis") or {}).get("rho") is not None
    basis = ("&sect;12.3.4.1, SDC %s" % sdc) if sdc in ("A", "B", "C") and all(float(v or 0) == 1.0 for v in r.values()) else \
            ("&sect;12.3.4.2" if sdc in ("D", "E", "F") else "&sect;12.3.4")
    return "%s (%s%s)" % (txt, basis, "" if declared else "; default for the SDC")


def _design_basis_codes(cfg, s, root=None, pkg=None, ctx=None):
    """Chapter 1: governing standards (AISC 341 / 358 only where they apply), risk and hazard classification
    (Risk Category from cfg, Site Class, SDC with the governing table values, rho, wind, snow, rain, tornado for
    RC III/IV) and the scope of this package driven by what the calc package actually contains."""
    ctx = ctx or {}
    seismic = bool(s.get("R"))
    a341 = bool(ctx.get("a341", seismic and float(s.get("R") or 0) > 3))
    rows = [["International Building Code (IBC)", "adopting building code &mdash; confirm the locally adopted edition and amendments"],
            ["ASCE/SEI 7-22", "minimum design loads and load combinations (&sect;2.3 LRFD; gravity, wind Ch. 26&ndash;31, "
             "seismic Ch. 11&ndash;12)"],
            ["AISC 360-22", "steel member and connection design (LRFD)"]]
    if a341:
        rows.append(["AISC 341-22", "seismic provisions: ductile detailing and capacity design of the SFRS%s"
                     % ((" (%s direction)" % "/".join(d for d in ("X", "Y") if (ctx.get("a341_dirs") or {}).get(d)))
                        if ctx.get("a341_dirs") and not all((ctx.get("a341_dirs") or {}).values()) else "")])
    if ctx.get("mf358"):
        rows.append(["AISC 358-22", "prequalified moment connections (SMF / IMF)"])
    rows.append(["AISC Steel Construction Manual, 16th Ed.", "design aids, incl. Part 10 simple shear connections"])
    if ctx.get("dg11"):
        rows.append(["AISC Design Guide 11 (2nd Ed.)", "floor vibration due to human activity"])
    rows += [["AWS D1.1" + ("/D1.1M and AWS D1.8" if a341 else "/D1.1M"), "structural welding" +
              (" (D1.8: seismic supplement)" if a341 else "")],
             ["ACI 318-19 Ch. 17", "anchoring to concrete at the column bases"]]
    for extra in cfg.get("additional_standards") or []:       # job-specific references (e.g. AISC DG7, CMAA 70)
        if isinstance(extra, (list, tuple)) and len(extra) >= 2:
            rows.append([_esc(str(extra[0])), _esc(str(extra[1]))])
        elif isinstance(extra, dict) and extra.get("reference"):
            rows.append([_esc(str(extra["reference"])), _esc(str(extra.get("used_for", "")))])
    out = ["<h3>Governing standards</h3>", _table(["Reference", "Used for"], rows)]
    if seismic and not a341:
        out.append("<p class='cnote'>AISC 341-22 does not apply: the seismic force-resisting system is a steel system "
                   "not specifically detailed for seismic resistance (R = %s, ASCE 7-22 Table 12.2-1 row H). Members and "
                   "connections are designed to AISC 360-22 for the forces from the analysis.</p>" % _n(s.get("R")))
    Ie = s.get("Ie", 1.0); RC = _risk_category(Ie, cfg)
    crows = [["Risk Category (ASCE 7-22 Table 1.5-1)", _esc(RC)],
             ["Seismic importance factor I<sub>e</sub>", _fac(Ie)],
             ["Site Class (ASCE 7-22 Ch. 20)", _esc(cfg.get("site_class")) if cfg.get("site_class") else
              "<b>not stated</b> &mdash; confirm with the geotechnical report"]]
    if seismic:
        try: sdc = E.sdc_of(cfg)        # the SDC every gate uses (a declared lower SDC is overridden, HR-15)
        except Exception: sdc = _sdc(s["SDS"], s["SD1"], s.get("S1", 0.0), Ie)
        t1, t2, ov = _sdc_tables(s.get("SDS", 0), s.get("SD1", 0), s.get("S1", 0), RC)
        sdct = ("%s (Table 11.6-1: S<sub>DS</sub> = %s g &rarr; %s; Table 11.6-2: S<sub>D1</sub> = %s g &rarr; %s%s)"
                % (sdc, _sa(s.get("SDS")), t1, _sa(s.get("SD1")), t2,
                   ("; S<sub>1</sub> = %s g &ge; 0.75 g &rarr; %s" % (_sa(s.get("S1")), ov)) if ov else ""))
        crows += [["S<sub>S</sub> / S<sub>1</sub>" if s.get("SS") else "S<sub>1</sub>",
                   ("%s / %s g" % (_sa(s.get("SS")), _sa(s.get("S1")))) if s.get("SS") else "%s g" % _sa(s.get("S1", 0))],
                  ["S<sub>DS</sub> / S<sub>D1</sub>", "%s / %s g" % (_sa(s.get("SDS")), _sa(s.get("SD1")))],
                  ["Seismic Design Category (&sect;11.6)", sdct],
                  ["R / C<sub>d</sub> / &Omega;<sub>0</sub> (Table 12.2-1)", _rcdo_txt(cfg, s)],
                  ["Redundancy factor &rho;", _rho_txt(cfg, sdc)]]
        try:
            from preflight import drift_relief, relief_active
            _r = drift_relief(cfg)
            if _r is not None:
                crows.append(["Story drift limit basis",
                              ("ASCE 7-22 &sect;16.1.2 relief in force &mdash; linear target %.2f%% from the Chapter 16 result "
                               "(nonlinear analysis record %s)" % (100.0 * float(_r.get("linear_target", 0) or 0), _n(_r.get("nlrha_job"))))
                              if relief_active(cfg) else
                              "Table 12.12-1 (a &sect;16.1.2 relief declaration is present but not in force &mdash; see Chapter 8)"])
        except Exception:
            pass
    crows += _design_of_record_rows(root)
    w = cfg.get("wind", {})
    if w:
        crows.append(["Basic wind speed V (Risk Category %s)" % _esc(RC), "%s mph, Exposure %s"
                      % (_n(w.get("V")), _esc(w.get("exposure") or "not stated"))])
    snow = float(cfg.get("snow", 0) or 0)
    crows.append(["Snow load (ASCE 7-22 Ch. 7)", ("design roof snow load S = %s psf%s%s" % (
        _n(snow), (", ground snow p<sub>g</sub> = %s psf" % _n(cfg.get("pg"))) if cfg.get("pg") else "",
        " (basis in Chapter 3)" if cfg.get("snow_basis") else ""))
        if snow > 0 else _esc(cfg.get("snow_basis") or "no snow load in the design criteria")])
    rain = cfg.get("rain")
    crows.append(["Rain load (ASCE 7-22 Ch. 8)", ((_n(rain) if not isinstance(rain, (int, float)) else _psf(rain))
                  + (" (basis in Chapter 3)" if cfg.get("rain_basis") else ""))
                  if rain not in (None, "", 0, {}) else _esc(cfg.get("rain_basis") or "no rain load in the design criteria")])
    if str(RC).upper() in ("III", "IV"):
        crows.append(["Tornado loads (ASCE 7-22 Ch. 32)", _n(cfg.get("tornado")) if cfg.get("tornado") else
                      "<b>required for Risk Category %s &mdash; not evaluated in this package</b>" % _esc(RC)])
    out += ["<h3>Risk &amp; hazard classification</h3>", _table(["Parameter", "Value"], crows)]
    conns = [c for c in ((pkg or {}).get("connections") or []) if isinstance(c, dict)] if isinstance(pkg, dict) else []
    designed = [c for c in conns if c.get("checks") or c.get("DC") is not None]
    dlg = (pkg or {}).get("delegated_design") if isinstance(pkg, dict) else None
    dli = ([_human_key(k)[0] for k in dlg.keys()] if isinstance(dlg, dict) and dlg else
           ["cold-formed / joist framing", "stairs", "cladding and its attachments", "embeds"])
    dl = ", ".join(x for i, x in enumerate(["foundations"] + dli)
                   if x and x.lower() not in [y.lower() for y in (["foundations"] + dli)[:i]])
    out += ["<h3>Scope &amp; design responsibility</h3>",
            "<p class='cnote'>This package covers the analysis of the primary steel gravity and lateral framing, the "
            "design of its members%s, and the column-base reactions for the foundation design (Chapter 11).%s "
            "Delegated or deferred (not designed here): %s. The reviewer should confirm that the "
            "geotechnical and architectural criteria match this basis.</p>"
            % (" and connections (%d connection types with limit-state checks, Chapter 10 and Appendix A)" % len(designed)
               if designed else "", "" if designed else
               " Connection demands are listed in Chapter 10; the connection design is by others.", dl)]
    return "".join(out)


def _floor_mode_txt(cfg, pkg=None):
    """How floor gravity reaches the framing, from the demand record (floor_load_modes) or cfg['floor_system']."""
    modes = (((pkg or {}).get("demand_analysis") or {}).get("floor_load_modes") or {}) if isinstance(pkg, dict) else {}
    fs = str(cfg.get("floor_system", "")).lower()
    keys = " ".join(str(k).lower() for k in modes) if isinstance(modes, dict) else ""
    if "one-way" in keys or ("one-way" in fs and "two-way" not in keys):
        return "one-way deck spanning to the beams (tributary width per beam)"
    if "two-way" in keys or "two-way" in fs:
        return "two-way tributary distribution (45&deg; yield lines) to the supporting beams"
    return "tributary distribution to the supporting beams"


def _system_loadpath(cfg, sf=None, pkg=None):
    sf = sf or _sfrs(cfg)
    sysd = "; ".join(f"<b>{d}</b>: {_sys_label(sf, d)} ({_line_desc(sf, d)})" for d in ("X", "Y"))
    if not sf.get("declared"):
        decl = (" <span class='note'>(no seismic force-resisting system is declared; the frame type shown is taken "
                "from the analysis model)</span>")
    elif not sf.get("recognised", True):
        decl = (" <span class='note'>(declared as &ldquo;%s&rdquo;, which does not name a Table 12.2-1 steel "
                "system)</span>" % _esc(sf.get("text", "")))
    else:
        decl = ""
    return ("<h3>Lateral system &amp; load path</h3>"
            f"<p>Seismic force-resisting system per principal direction: {sysd}.{decl} "
            f"<b>Gravity</b> path: floor/roof load &rarr; {_floor_mode_txt(cfg, pkg)} &rarr; beams/girders &rarr; "
            "columns &rarr; column bases &rarr; foundation. <b>Lateral</b> path: story inertial/wind force &rarr; "
            "floor diaphragm &rarr; the vertical lateral planes listed above &rarr; column bases &rarr; foundation, in "
            "both orthogonal directions. The horizontal distribution to the planes and the irregularity screen "
            "(discontinuous or offset planes) are in this chapter.</p>")


_PF_KEY = {"SMF": "smf", "IMF": "imf", "OMF": "omf", "STMF": "stmf", "SCBF": "scbf", "OCBF": "ocbf",
           "BRBF": "brbf", "EBF": "ebf", "SPSW": "spsw", "C-PSW": "c-psw", "CC-PSW": "cc-psw", "NSD": "nsd"}


def _sdc_safe(cfg):
    try:
        return E.sdc_of(cfg)
    except Exception:
        s = cfg.get("seis") or {}
        try: return _sdc(s.get("SDS", 0), s.get("SD1", 0), s.get("S1", 0.0), s.get("Ie", 1.0))
        except Exception: return "D"


def _height_limit(fam, sdc=None, hn_ft=None, cfg=None, pkg=None, key=None):
    """ASCE 7-22 Table 12.2-1 structural height limit for system family `fam` (or the preflight SYSTEMS row
    `key`, e.g. a dual-system row) in the building's SDC, evaluated against h_n (with the 12.2.5.4 increase /
    single-story exceptions as preflight evaluates them)."""
    try:
        import preflight as PF
    except Exception:
        PF = None
    key = key or _PF_KEY.get(fam)
    if PF is None or key is None or key not in PF.SYSTEMS:
        return "SFRS not identified &mdash; limit not evaluated" if fam in ("MF?", "BF?") else "per Table 12.2-1"
    sdc = sdc or "D"
    col = "B" if sdc in ("A", "B") else sdc
    lim = PF.SYSTEMS[key]["lim"].get(col)
    if lim is None:
        txt = "not limited (SDC %s)" % sdc
        return txt + ("; h<sub>n</sub> = %s ft &mdash; OK" % _n(hn_ft) if hn_ft else "")
    if lim == PF.NP:
        base = "not permitted in SDC %s" % sdc
    else:
        base = "%s ft (SDC %s)" % (_n(lim), sdc)
    if hn_ft is None:
        return base
    if lim != PF.NP and hn_ft <= lim + 0.05:
        return base + "; h<sub>n</sub> = %s ft &mdash; OK" % _n(hn_ft)
    # exceeded / NP: report the evaluated finding (12.2.5.4 increase or a single-story exception)
    msg = ""
    if isinstance(cfg, dict):
        try:
            import consistency as _C
            ev = _C._height_limit_evidence(pkg) if isinstance(pkg, dict) else None
            fnd = [m for sev, m in PF.system_limit_findings(cfg, ev) if PF.SYSTEMS[key]["name"] in m]
            if fnd:
                msg = fnd[0]
        except Exception:
            msg = ""
    if msg and ("permitted up to" in msg or "permitted to" in msg):
        return base + "; h<sub>n</sub> = %s ft &mdash; %s" % (_n(hn_ft), _esc(re.sub(r"\s*Keep the .*$", "", msg)))
    return base + "; h<sub>n</sub> = %s ft &mdash; <b class='ng'>exceeds the limit</b>%s" % (
        _n(hn_ft), (" (" + _esc(msg) + ")") if msg else "")


def _lfrs_table(cfg, sf=None, pkg=None):
    sf = sf or _sfrs(cfg)
    sdc = _sdc_safe(cfg)
    try:                                 # h_n above a declared seismic base (cfg['hn_ft']) wins over the model height
        hn = float(cfg["hn_ft"]) if cfg.get("hn_ft") else E.zlevels(cfg)[-1] / 12.0
    except Exception:
        hn = float(cfg.get("hn_ft") or sum(float(h) for h in cfg.get("heights") or []) / 12.0)
    rows = []
    for d in ("X", "Y"):
        try:
            sd = E.seis_dir(cfg, d)
        except Exception:
            sd = _seis_d(cfg, d)
        dual_key = None
        try:                                   # a dual system (12.2.5.1) takes the limits of its dual row, not its parts
            import preflight as PF
            _sd_txt = E.system_dir(cfg, d) if hasattr(E, "system_dir") else (cfg.get("system") or "")
            _ps = PF.parse_system(_sd_txt or cfg)
            if _ps.get("kind") == "dual" and _ps.get("rows") and _ps["rows"][0] in PF.SYSTEMS:
                dual_key = _ps["rows"][0]
        except Exception:
            dual_key = None
        _hlo = cfg.get("height_limit") if isinstance(cfg.get("height_limit"), dict) else None
        if _hlo and _hlo.get("limit_ft") is not None:
            # declared limit, e.g. ASCE 7-22 Table 15.4-1 for a nonbuilding structure similar to a building
            _lim = _hlo.get("limit_ft")
            if str(_lim).upper() in ("NL", "NONE", "NOT LIMITED"):
                hl = "not limited (%s); h<sub>n</sub> = %s ft &mdash; OK" % (_esc(_hlo.get("basis", "declared")), _n(hn))
            else:
                _ok = hn <= float(_lim) + 0.05
                hl = "%s ft (%s); h<sub>n</sub> = %s ft &mdash; %s" % (
                    _n(float(_lim)), _esc(_hlo.get("basis", "declared")), _n(hn),
                    "OK" if _ok else "<b class='ng'>exceeds the limit</b>")
        elif dual_key:
            hl = "dual system, %s: %s" % (_esc(PF.SYSTEMS[dual_key]["name"].replace("dual: ", "")),
                                          _height_limit(None, sdc, hn, cfg, pkg, key=dual_key))
        else:
            hl = "; ".join(("%s: %s" % (f if f not in ("MF?", "BF?") else "SFRS", _height_limit(f, sdc, hn, cfg, pkg)))
                           if len(sf["dirs"][d]) > 1 else _height_limit(f, sdc, hn, cfg, pkg) for f in sf["dirs"][d])
        rows.append([f"{d} direction", _sys_label(sf, d), _fac(sd.get("R")), _fac(sd.get("Cd")), _fac(sd.get("Om0")),
                     _fac(sd.get("rho")), hl])
    _hl_decl = isinstance(cfg.get("height_limit"), dict) and cfg["height_limit"].get("limit_ft") is not None
    tbl = _table(["Direction", "Seismic force-resisting system", "R", "C<sub>d</sub>", "&Omega;<sub>0</sub>",
                  "&rho;", ("Structural height limit (SDC %s)" if _hl_decl else
                            "Structural height limit, Table 12.2-1 (SDC %s)") % sdc], rows)
    hl_txt = " / ".join(dict.fromkeys(r[-1] for r in rows))
    if "SFRS not identified" not in hl_txt and "per Table 12.2-1" not in hl_txt:
        _QA["height_limit"] = ("exceeds the limit" not in hl_txt, hl_txt)
    if sf.get("declared") and sf.get("recognised", True):
        a341 = any(f in FAM_341 for f in sf.get("families", []))
        note = ("<p class='cnote'>System as declared (&ldquo;%s&rdquo;)%s.%s Where the systems differ by direction, each "
                "direction's R, C<sub>d</sub> and &Omega;<sub>0</sub> apply to it (ASCE 7-22 &sect;12.2.2)%s.</p>"
                % (_esc(sf["text"]) if sf.get("text") else "per direction",
                   ", checked against the lateral elements of the analysis model" if sf["inv_ok"] else "",
                   " The AISC 341 detailing of each system is in Chapter 9." if a341 else "",
                   "" if (cfg.get("seis_X") or cfg.get("seis_Y")) or not sf["mixed"] else
                   " &mdash; <b>one seismic factor set is declared for both directions: confirm it applies to each system</b>"))
    elif sf.get("declared"):
        note = ("<p class='note'>The declared system (&ldquo;%s&rdquo;) does not name a Table 12.2-1 steel system; the "
                "frame type above is taken from the analysis model and the height limit is not evaluated.</p>"
                % _esc(sf.get("text", "")))
    else:
        R = float((cfg.get("seis") or {}).get("R") or 0)
        note = ("<p class='note'>No seismic force-resisting system is declared; the frame type above is taken from the "
                "analysis model (braces / fixed-ended beams) and does not identify the Table 12.2-1 system%s.</p>"
                % (" (R = 8 corresponds to an SMF, EBF, BRBF or a dual system)" if abs(R - 8.0) < 1e-9 else ""))
    for w in sf.get("warnings", []):
        note += "<p class='note'>%s</p>" % _n(w)
    return tbl + note


def _diaphragm_section(cfg, Fx, wind=None, pkg=None):
    """ASCE 7-22 12.10.1.1 F_px per level; where the MWFRS wind story forces exceed the seismic F_px the
    diaphragm, collectors and chords are designed for the wind as well (19j). wind = (FwX, FwY) story forces."""
    NF = len(cfg["heights"]); s = cfg["seis"]; SDS = s["SDS"]; Ie = s["Ie"]
    w = [E.floor_w(cfg, k) for k in range(1, NF+1)]
    # Fx: one direction's story forces, or (FxX, FxY) -- Eq. 12.10-1 is then evaluated in both directions
    # and the larger governs the diaphragm design force of the level
    two = isinstance(Fx, tuple) and len(Fx) == 2
    FxX, FxY = (Fx if two else (Fx, Fx))
    two = two and any(abs(float(FxX[i]) - float(FxY[i])) > 1e-6 for i in range(1, NF + 1))
    rows = []; fpx = {}
    for x in range(1, NF+1):
        sumw = sum(w[i-1] for i in range(x, NF+1)); wpx = w[x-1]
        FpX = (sum(FxX[i] for i in range(x, NF+1)) / sumw) * wpx if sumw else 0.0
        FpY = (sum(FxY[i] for i in range(x, NF+1)) / sumw) * wpx if sumw else 0.0
        Fpx = max(FpX, FpY)
        lo = 0.2*SDS*Ie*wpx; hi = 0.4*SDS*Ie*wpx; gov = min(max(Fpx, lo), hi)
        tag = "minimum" if gov == lo else ("maximum" if gov == hi else
                                           ("Eq. 12.10-1, %s" % ("X" if FpX >= FpY else "Y") if two else "Eq. 12.10-1"))
        fpx[x] = gov
        row = ([x, _n(wpx), _n(FpX), _n(FpY)] if two else [x, _n(wpx), _n(Fpx)]) + \
              [_n(lo), _n(hi), "%s (%s)" % (_n(gov), tag)]
        if wind:
            fw = max(float(wind[0].get(x, 0.0) or 0.0), float(wind[1].get(x, 0.0) or 0.0))
            row.append(_n(fw))
        rows.append(row)
    n_wind = sum(1 for k in range(1, NF + 1) if wind and
                 max(float(wind[0].get(k, 0) or 0), float(wind[1].get(k, 0) or 0)) > fpx[k])
    wind_gov = n_wind > 0
    _hi = [t for t in (_QA.get("horiz_irreg") or []) if t in ("2", "3", "4", "5")]
    _db_decl = cfg.get("diaphragm_basis") if isinstance(cfg.get("diaphragm_basis"), str) and \
        not str(cfg.get("diaphragm", "")).lower().startswith("flex") else None
    _rig = ("constraint (%s)" % _n(_db_decl) if _db_decl else
            "constraint (ASCE 7-22 &sect;12.3.1.2: concrete-filled metal deck, span-to-depth ratio of 3 or less, no Type 2&ndash;5 "
            "horizontal irregularity)" if not _hi else
            "constraint. Because the structure has horizontal irregularity Type %s (Table 12.3-1), the rigid condition of "
            "&sect;12.3.1.2 does not apply directly and &sect;12.3.1 calls for the diaphragm stiffness to be considered; "
            "the rigid idealization is the conventional approximation for a concrete-filled deck with span-to-depth ratio "
            "of 3 or less, the collectors, chords and diaphragm connections at the irregularity are designed for the "
            "&sect;12.10.2.1 overstrength forces (with the &sect;12.3.3.5 increase where it applies), and a semirigid "
            "diaphragm analysis confirming the distribution is an item for the engineer of record" % ", ".join(_hi))
    try:
        _sdc_d = E.sdc_of(cfg)
    except Exception:
        _sdc_d = "D"
    # 12.10.1.1 governs the diaphragm and chords; in SDC C-F the collectors and their connections take the
    # 12.10.2.1 overstrength forces (with the 12.3.3.5 increase where it applies)
    _coll = ("The diaphragm and its chords are designed for the seismic force F<sub>px</sub> below (&sect;12.10.1.1); "
             "the collectors and their connections are designed for the &sect;12.10.2.1 overstrength forces (the larger "
             "of &Omega;<sub>0</sub> times the collector force and the Eq. 12.10-2 minimum, with the &sect;12.3.3.5 "
             "increase where it applies)" if _sdc_d in ("C", "D", "E", "F") else
             "The diaphragm, its collectors and chords are designed for the seismic force F<sub>px</sub> below "
             "(&sect;12.10.1.1)")
    _flex = str(cfg.get("diaphragm", "")).lower().startswith("flex")
    if _flex:
        _fb = cfg.get("diaphragm_basis")
        _fb = _n(_fb) if isinstance(_fb, str) and _fb.strip() else "ASCE 7-22 &sect;12.3.1.1 / &sect;12.3.1.3"
        just = ("<p>The roof diaphragm is idealized as <b>flexible</b> (%s). The analysis model ties each level with an "
                "in-plane constraint for the global solution only; the design forces of the vertical elements are the "
                "tributary-area line forces of the flexible diaphragm (line-force and collector records), and the "
                "accidental-torsion and torsional-irregularity provisions written for rigid diaphragms do not apply "
                "(&sect;12.8.4.2.1, &sect;12.3.2.1). Collectors/drag struts deliver the diaphragm shear to the frames; "
                "chord forces (M<sub>diaph</sub>/depth) are resisted by the perimeter members. " % _fb + _coll
                + ("; the MWFRS wind story forces of Chapter 3 exceed F<sub>px</sub> at %d of the %d levels of this "
                   "building and are designed for as well." % (n_wind, NF) if wind_gov else ".") + "</p>")
    else:
        just = None
    _mixed = _diaphragm_kind(cfg) == "mixed"
    _lead = ("<p>The %s taken as <b>rigid diaphragms</b>%s that distribute the story forces to the lateral frames in "
             "proportion to their stiffness, modelled with a rigid in-plane "
             % ("floors are" if _mixed else "floor and roof are", "" if _db_decl else " (concrete-filled metal deck)"))
    just = just or (_lead
            + _rig + (". The roof (bare metal deck) is idealized as a <b>flexible diaphragm</b> (&sect;12.3.1.1): the roof "
                      "force reaches the vertical elements by tributary area" if _mixed else "")
            + ". Collectors/drag struts deliver the diaphragm shear to the frames; chord "
            "forces (M<sub>diaph</sub>/depth) are resisted by the perimeter beams. " + _coll
            + ("; the MWFRS wind story forces of Chapter 3 exceed F<sub>px</sub> at %d of the %d levels of this "
               "building and are designed for as well." % (n_wind, NF) if wind_gov else ".") + "</p>")
    hdr = ["Level x", "w<sub>px</sub> (kip)"] + (
           ["F<sub>px</sub> Eq. 12.10-1, X (kip)", "F<sub>px</sub> Eq. 12.10-1, Y (kip)"] if two else
           ["F<sub>px</sub> Eq. 12.10-1 (kip)"]) + [
           "min 0.2S<sub>DS</sub>I<sub>e</sub>w<sub>px</sub>", "max 0.4S<sub>DS</sub>I<sub>e</sub>w<sub>px</sub>",
           "F<sub>px</sub> design (kip)"] + (["MWFRS wind story force, larger direction (kip)"] if wind else [])
    tbl = _table(hdr, rows)
    coll = [m.get("id") for m in ((pkg or {}).get("members") or []) if isinstance(m, dict)
            and "collector" in (str(m.get("id", "")) + " " + str((m.get("inputs") or {}).get("role", ""))).lower()] \
        if isinstance(pkg, dict) else []
    dia = [x.get("id") for x in (((pkg or {}).get("members") or []) + ((pkg or {}).get("connections") or []))
           if isinstance(x, dict) and re.search(r"diaphragm|\bslab\b|slab[-_ ]shear",
                                                str(x.get("id", "")).lower())] if isinstance(pkg, dict) else []
    if isinstance(pkg, dict) and isinstance(pkg.get("diaphragm_design"), (dict, list)) and pkg.get("diaphragm_design"):
        dia = dia or ["diaphragm design record"]
    deck = ("The diaphragm itself (%s) is designed in the calculation package." % ", ".join(_esc(c) for c in dia[:6])
            if dia else "The deck diaphragm itself (deck shear and fasteners) is designed by the deck supplier for these "
                        "forces and confirmed on the drawings.")
    tail = ("<p class='cnote'>Collector members are designed in Chapter 6 (%s). %s</p>"
            % (", ".join(_esc(c) for c in coll[:6]), deck) if coll else
            "<p class='cnote'>F<sub>px</sub> (and the wind story forces where larger) are the diaphragm, collector and "
            "chord design forces. %s</p>" % deck)
    return just + tbl + tail


def _merge(iv):
    out = []
    for lo, hi in sorted(iv):
        if out and lo <= out[-1][1] + 1.0: out[-1] = (out[-1][0], max(out[-1][1], hi))
        else: out.append((lo, hi))
    return out


def _discontinuities(cfg, sf):
    """Vertical-element continuity of every SFRS plane, story by story (from the built model):
       'terminated' -- a plane has lateral elements in story s but none in story s-1 below it
                       (its shear must leave the plane through the diaphragm: Table 12.3-1 Type 4
                       out-of-plane offset; its overturning lands on the supporting members:
                       Table 12.3-2 Type 3 / 12.3.3.4 -- both reported, conservatively);
       'inplane'    -- the plane continues below but its bays at story s are not all above bays of
                       story s-1 (in-plane offset, Table 12.3-2 Type 3).
    Returns (terminated, inplane) lists of (direction, plane_pos_in, story)."""
    term, inpl = [], []
    for d in ("X", "Y"):
        for pos, st in (sf.get("lines", {}).get(d) or {}).items():
            for s_ in sorted(st):
                if s_ < 2: continue
                below = st.get(s_ - 1)
                if not below:
                    term.append((d, pos, s_)); continue
                cov = _merge(below)
                for lo, hi in _merge(st[s_]):
                    if not any(c0 - 1.0 <= lo and hi <= c1 + 1.0 for c0, c1 in cov):
                        inpl.append((d, pos, s_)); break
    return term, inpl


def _sfrs_extent(sf, d, NF):
    """Horizontal dimension of the SFRS in direction d per story (inches), from the plane extents."""
    out = {}
    for s_ in range(1, NF + 1):
        iv = [x for pos, st in (sf.get("lines", {}).get(d) or {}).items() for x in st.get(s_, [])]
        if iv: out[s_] = max(hi for lo, hi in iv) - min(lo for lo, hi in iv)
    return out


def _irregularity_section(cfg, runs, sf=None):
    """ASCE 7-22 Tables 12.3-1 / 12.3-1a / 12.3-2 evaluated numerically where the model provides the data;
    anything the framework cannot evaluate is printed as NOT EVALUATED (never a default 'None')."""
    sf = sf or _sfrs(cfg)
    NF = len(cfg["heights"]); hgt = cfg["heights"]; exempt = runs["exempt"]
    sdc = runs.get("sdc", "D"); NE = "<b>NOT EVALUATED</b>"
    trig = {"12.3.3.4": [], "12.3.3.5": [], "12.3.3.1": [], "12.3.3.2": [], "12.3.3.3": [], "12.7.3 (3D model)": [],
            "12.5 (orthogonal)": []}
    # ---- H1 torsional (Eq. 12.3-2 TIR from edge drifts with accidental torsion, Ax = 1)
    tirs = {d: max([t for s_, t in enumerate(runs[d]["tir"], 1) if s_ not in exempt] or [1.0]) for d in ("X", "Y")}
    axs = {d: max(runs[d]["ax"] or [1.0]) for d in ("X", "Y")}
    tr = max(tirs.values())
    tiers = [t for t in (1.2, 1.4, 1.6) if tr > t]
    if tr > 1.2:
        both14 = all(v > 1.4 for v in tirs.values())
        tcls = ("Type 1 TORSIONAL: YES &mdash; TIR X %.3f / Y %.3f, largest &gt; 1.2 (Table 12.3-1a tiers exceeded: "
                "&gt;%s)%s; A<sub>x</sub> (Eq. 12.8-15, displacements) up to X %.2f / Y %.2f (&sect;12.8.4.3)%s%s"
                % (tirs["X"], tirs["Y"], "/&gt;".join("%.1f" % t for t in tiers),
                   "" if sdc in ("C", "D", "E", "F") else " (A<sub>x</sub> not required in SDC %s)" % sdc,
                   axs["X"], axs["Y"],
                   ("; design drift and P-&Delta; at the edge with the largest drift (&sect;12.8.6.5, Table 12.3-1a)"
                    if sdc in ("C", "D", "E", "F") else
                    "; design drift at the centre of mass (the edge drift of &sect;12.8.6.5 applies in SDC C&ndash;F)"),
                   "; &rho; = 1.3 (&gt;1.4 in both directions, &sect;12.3.4.2.1)" if both14 and sdc in ("D", "E", "F") else ""))
        trig["12.3.3.5"].append("H1"); trig["12.7.3 (3D model)"].append("H1")
    else:
        tcls = "Not present: TIR X %.2f / Y %.2f &le; 1.2" % (tirs["X"], tirs["Y"])
    if str(cfg.get("diaphragm", "")).lower().startswith("flex"):
        tcls = ("Not applicable: flexible diaphragm &mdash; Table 12.3-1 Type 1 applies only where the diaphragms are rigid or "
                "semirigid (&sect;12.3.2.1); the accidental torsion of &sect;12.8.4.2 is not required for flexible diaphragms "
                "(it is kept in the design combinations, a conservative bound)")
        for k_ in ("12.3.3.5", "12.7.3 (3D model)"):
            trig[k_] = [x for x in trig.get(k_, []) if x != "H1"]
    # ---- H2 / H3 geometric plan screens
    _h3_op = _h3_open = None
    try:
        pl = E.plan_irregularity_levels(cfg)
    except Exception as ex:
        pl = None; plerr = str(ex)
    def _glab(g, k):
        grp = g.get("group") or [k]
        return "+".join("L%d" % x for x in grp)
    if pl is None:
        h2 = h3 = NE + " (footprint screen failed: %s)" % _esc(plerr)
    else:
        rel = [(k, g) for k, g in pl["levels"].items() if g.get("worst_cutout")]
        if pl["reentrant"]:
            k, g = max(((k, g) for k, g in rel if g["reentrant"]), key=lambda t: min(t[1]["worst_cutout"]["rx"], t[1]["worst_cutout"]["ry"]))
            c = g["worst_cutout"]
            h2 = ("Type 2 RE-ENTRANT: YES &mdash; at %s both projections beyond the corner exceed 20%% of the plan "
                  "dimension (%.0f ft = %.0f%% of %.0f ft in X; %.0f ft = %.0f%% of %.0f ft in Y)%s"
                  % (_glab(g, k), c["px"] / 12, 100 * c["rx"], g["Lx"] / 12, c["py"] / 12, 100 * c["ry"], g["Ly"] / 12,
                     "; 25% diaphragm-connection/collector increase (&sect;12.3.3.5)" if sdc in ("D", "E", "F") else ""))
            trig["12.3.3.5"].append("H2")
        elif rel:
            k, g = max(rel, key=lambda t: min(t[1]["worst_cutout"]["rx"], t[1]["worst_cutout"]["ry"]))
            c = g["worst_cutout"]
            h2 = ("Not present: largest notch at %s projects %.0f%% (X) / %.0f%% (Y) of the plan dimensions &mdash; both must "
                  "exceed 20%%" % (_glab(g, k), 100 * c["rx"], 100 * c["ry"]))
        else:
            h2 = "Not present (no re-entrant corner in the framed footprint%s)" % (
                "; split-level wings screened together as declared" if cfg.get("level_groups") else
                "" if not runs["exempt"] else "; split-level building: wings at offset elevations screened per level")
        omax = max(pl["levels"].items(), key=lambda t: t[1]["open_ratio"], default=(None, None))
        if omax[0] is None:
            h3 = NE
        else:
            k, g = omax
            op = ("open area %.0f%% of the gross enclosed area at %s (limit 25%%%s)"
                  % (100 * g["open_ratio"], _glab(g, k),
                     "" if cfg.get("diaphragm_openings") else "; framed-bay openings only &mdash; other slab openings are not modelled"))
            _h3_op, _h3_open = op, g["open_ratio"]
            h3 = ("Type 3 DIAPHRAGM DISCONTINUITY: YES &mdash; " + op if g["open_ratio"] > 0.25 else "Openings: " + op) + \
                 ("; story-to-story change in effective diaphragm stiffness: " + NE +
                  (" (flexible diaphragm idealization)" if str(cfg.get("diaphragm", "")).lower().startswith("flex")
                   else " (diaphragm modelled rigid)"))
            if g["open_ratio"] > 0.25: trig["12.3.3.5"].append("H3")
    # ---- H4 / V3 discontinuous or offset vertical elements
    term, inpl = _discontinuities(cfg, sf) if sf.get("inv_ok") else (None, None)
    def _plane(d, pos):
        return ("%s-plane at %s = %.0f ft" % (d, "y" if d == "X" else "x", pos / 12.0))
    if term is None:
        h4 = v3 = NE + " (lateral-element inventory unavailable)"
    else:
        if term:
            lst = "; ".join("%s terminates at story %d" % (_plane(d, p), s_) for d, p, s_ in term[:6])
            h4 = ("Type 4 OUT-OF-PLANE OFFSET: YES &mdash; %s (no lateral element of that plane in the story below; its "
                  "shear transfers through the diaphragm to other planes &mdash; Table 12.3-1 Type 4, transfer forces "
                  "&times; &Omega;<sub>0</sub> per &sect;12.10.1.1/12.10.3.3)" % lst)
            trig["12.3.3.4"].append("H4"); trig["12.3.3.5"].append("H4"); trig["12.7.3 (3D model)"].append("H4")
        else:
            h4 = "Not present: every SFRS plane is continuous to the base (checked in the analysis model)"
        if term or inpl:
            parts = ["%s terminates at story %d on supporting members" % (_plane(d, p), s_) for d, p, s_ in term[:6]]
            parts += ["%s offset in its own plane at story %d" % (_plane(d, p), s_) for d, p, s_ in inpl[:6]]
            v3 = ("Type 3 IN-PLANE DISCONTINUITY: YES &mdash; " + "; ".join(parts) +
                  " &mdash; the supporting beams/trusses/columns and their connections resist the overturning with "
                  "&Omega;<sub>0</sub> (&sect;12.3.3.4)")
            trig["12.3.3.4"].append("V3"); trig["12.3.3.5"].append("V3")
        else:
            v3 = "Not present: no SFRS plane is offset or interrupted below any story (checked in the analysis model)"
    # engineer's evaluation where the automated plane screen misreads the model (e.g. a portal frame spanning two
    # model levels with no beam at the lower one): cfg['irregularity_statements'] = {'H4': text, 'V3': text}
    # -- a value {'present': True, 'text': ...} (or text starting 'present:') DECLARES an irregularity the screen
    #    cannot see (e.g. SFRS columns ending on a transfer girder in the same plane)
    _ist = cfg.get("irregularity_statements") if isinstance(cfg.get("irregularity_statements"), dict) else {}

    def _declared(key):
        v = _ist.get(key)
        if isinstance(v, dict):
            return (True if v.get("present") else False), str(v.get("text") or "")
        if isinstance(v, str) and re.match(r"\s*present\b", v, re.I):
            return True, re.sub(r"^\s*present\b\s*[:;,.\-\u2014]*\s*", "", v, flags=re.I)
        return (None if v is None or v == "" else False), str(v or "")
    _p4, _t4 = _declared("H4")
    if _p4:
        h4 = ("Type 4 OUT-OF-PLANE OFFSET: YES (engineer's evaluation) &mdash; " + _esc(_t4) +
              " &mdash; the supporting elements and their connections resist the seismic forces with &Omega;<sub>0</sub> "
              "(&sect;12.3.3.4); &sect;12.3.3.5 and &sect;12.7.3 apply")
        for k_ in ("12.3.3.4", "12.3.3.5", "12.7.3 (3D model)"):
            if "H4" not in trig.setdefault(k_, []): trig[k_].append("H4")
    elif _p4 is False and term:
        h4 = "Not present (engineer's evaluation): " + _esc(_t4)
        for k_ in ("12.3.3.4", "12.3.3.5", "12.7.3 (3D model)"):
            trig[k_] = [x for x in trig.get(k_, []) if x != "H4"]
    _p3, _t3 = _declared("V3")
    if _p3:
        v3 = ("Type 3 IN-PLANE DISCONTINUITY: YES (engineer's evaluation) &mdash; " + _esc(_t3) +
              " &mdash; the supporting beams/trusses/columns and their connections resist the overturning with "
              "&Omega;<sub>0</sub> (&sect;12.3.3.4)")
        for k_ in ("12.3.3.4", "12.3.3.5"):
            if "V3" not in trig.setdefault(k_, []): trig[k_].append("V3")
    elif _p3 is False and (term or inpl):
        v3 = "Not present (engineer's evaluation): " + _esc(_t3)
        for k_ in ("12.3.3.4", "12.3.3.5"):
            trig[k_] = [x for x in trig.get(k_, []) if x != "V3"]
    # H3: the open-area screen is automated, the story-to-story change in effective diaphragm stiffness (> 50 %) is
    # the engineer's evaluation -- cfg['irregularity_statements']['H3'] = text (not present) or {'present': True,
    # 'text': ...} (present: 12.3.3.5 applies in SDC D-F).  An open area > 25 % stays YES whatever the statement.
    _ph3, _th3 = _declared("H3")
    if _ph3 is not None and pl is not None:
        _opn = ("; openings: " + _h3_op) if _h3_op else ""
        if _ph3:
            h3 = "Type 3 DIAPHRAGM DISCONTINUITY: YES (engineer's evaluation) &mdash; " + _esc(_th3) + _opn
            if "H3" not in trig.setdefault("12.3.3.5", []): trig["12.3.3.5"].append("H3")
        elif _h3_open is not None and _h3_open > 0.25:
            h3 = ("Type 3 DIAPHRAGM DISCONTINUITY: YES &mdash; " + _h3_op +
                  "; story-to-story change in effective diaphragm stiffness (engineer's evaluation): " + _esc(_th3))
        else:
            h3 = "Not present (engineer's evaluation): " + _esc(_th3) + _opn
    # ---- H5 nonparallel
    npar = (_frame_inventory(cfg).get("nonparallel") or [])
    if npar or cfg.get("skew"):
        h5 = ("Type 5 NONPARALLEL: YES &mdash; %s; orthogonal combination (&sect;12.5) and 3D analysis (&sect;12.7.3)"
              % ("%d lateral element(s) skewed to the principal axes" % len(npar) if npar else "skewed column grid"))
        trig["12.5 (orthogonal)"].append("H5"); trig["12.7.3 (3D model)"].append("H5")
    else:
        h5 = "Not present: all lateral elements parallel to X or Y (checked in the analysis model)" if sf.get("inv_ok") else NE
    _QA["horiz_irreg"] = [t for t, hh in (("2", h2), ("3", h3), ("4", h4), ("5", h5)) if "YES" in str(hh)]
    # ---- V1 soft story (Table 12.3-2 1a/1b, with the 12.3.2.2 exceptions)
    def _soft(d):
        k = runs[d]["k"]; dr = runs[d]["drift_cm"]
        if NF == 1 or (NF == 2 and sdc in ("B", "C", "D")):
            return "Not present: Exception 2 (12.3.2.2) &mdash; %d-story building" % NF, None
        worst = None
        for s_ in range(1, NF):
            if s_ in exempt: continue
            above = [k[j] for j in range(s_, min(s_ + 3, NF)) if (j + 1) not in exempt]
            if not above or not math.isfinite(k[s_ - 1]): continue
            r1 = k[s_ - 1] / above[0]; r3 = k[s_ - 1] / (sum(above) / len(above)) if len(above) == 3 else None
            typ = ("1b" if (r1 < 0.60 or (r3 is not None and r3 < 0.70)) else
                   "1a" if (r1 < 0.70 or (r3 is not None and r3 < 0.80)) else None)
            if worst is None or r1 < worst[1]: worst = (s_, r1, r3, typ)
        if worst is None:
            return NE, None
        # Exception 1: no design story drift ratio > 130% of the story above (top-two relation excluded)
        exc1 = all(abs(dr[s_ - 1]) <= 1.3 * abs(dr[s_]) for s_ in range(1, NF - 1)
                   if s_ not in exempt and (s_ + 1) not in exempt)
        s_, r1, r3, typ = worst
        txt = ("story %d: K/K<sub>above</sub> = %.2f%s" % (s_, r1, (", K/avg(3 above) = %.2f" % r3) if r3 is not None else ""))
        if typ and exc1:
            return "Not present: %s, but Exception 1 (12.3.2.2) applies &mdash; no story drift ratio exceeds 130%% of the story above" % txt, None
        if typ:
            return ("Type %s %sSOFT STORY: YES &mdash; %s" % (typ, "EXTREME " if typ == "1b" else "", txt)), typ
        return "Not present: %s (limits 0.70 / 0.80)" % txt, None
    v1x, tx = _soft("X"); v1y, ty = _soft("Y")
    if "1b" in (tx, ty) and sdc in ("E", "F"): trig["12.3.3.1"].append("V1b")
    v1 = "X: %s<br>Y: %s" % (v1x, v1y)
    # ---- V2 vertical geometric (SFRS horizontal dimension, 130%)
    v2p = []
    if sf.get("inv_ok"):
        for d in ("X", "Y"):
            ext = _sfrs_extent(sf, d, NF)
            for s_ in range(2, NF + 1):
                a_, b_ = ext.get(s_), ext.get(s_ - 1)
                if a_ and b_ and max(a_, b_) / min(a_, b_) > 1.30:
                    v2p.append("%s: stories %d/%d SFRS dimension %.0f/%.0f ft" % (d, s_ - 1, s_, b_ / 12, a_ / 12))
        v2 = ("Type 2 VERTICAL GEOMETRIC: YES &mdash; " + "; ".join(v2p[:4]) + " (&gt; 130%; no Table 12.3-2 reference "
              "section &mdash; informs the analysis-procedure choice)") if v2p else \
             "Not present: SFRS horizontal dimension within 130% between adjacent stories (both directions)"
    else:
        v2 = NE
    # ---- V4 weak story (needs the story lateral strengths)
    ss = cfg.get("story_strength")
    _p4v, _t4v = _declared("V4")          # engineer's statement, e.g. a runway level that is not a story
    _hn_in = float(cfg.get("hn_ft") or 0) * 12.0 or sum(hgt)
    if _p4v is not None:
        v4 = (("Type 4 WEAK STORY: YES (engineer's evaluation) &mdash; " if _p4v else "Not present (engineer's evaluation): ")
              + _esc(_t4v))
    elif NF == 1:
        v4 = "Not applicable: one-story building (no story above to compare)"
    elif isinstance(ss, dict) and ss.get("X") and ss.get("Y"):
        rr = []; allr = []
        for d in ("X", "Y"):
            v = [float(x) for x in ss[d]]
            for s_ in range(1, min(len(v), NF)):
                if v[s_] > 0:
                    allr.append((d, s_, v[s_ - 1] / v[s_]))
                    if v[s_ - 1] / v[s_] < 1.0:           # ASCE 7-22 Table 12.3-2 Type 4a: LESS THAN the story above
                                                          # (the 80 % trigger is ASCE 7-16 Type 5a)
                        rr.append((d, s_, v[s_ - 1] / v[s_]))
        if rr:
            dmin = min(rr, key=lambda t: t[2])
            typ = "4b EXTREME WEAK" if dmin[2] < 0.65 else "4a WEAK"
            v4 = ("Type %s STORY: YES &mdash; %s story %d strength = %.0f%% of the story above "
                  "(declared story strengths)" % (typ, dmin[0], dmin[1], 100 * dmin[2]))
            if dmin[2] < 0.65:
                if sdc in ("E", "F"): trig["12.3.3.1"].append("V4b")
                elif sdc == "D": trig["12.3.3.2"].append("V4b")
                elif sdc in ("B", "C") and (NF > 2 or sum(hgt) > 360.0 + 0.5): trig["12.3.3.3"].append("V4b")
            elif sdc in ("E", "F"):
                if dmin[2] < 0.80:
                    trig["12.3.3.1"].append("V4a")
                else:                     # 12.3.3.1 Exception: Type 4a permitted where >= 80 % of the story above
                    v4 += ("; permitted in SDC %s by the 12.3.3.1 Exception (story strength not less than 80%% "
                           "of the story above)" % sdc)
            else:
                v4 += "; no consequence in SDC %s (12.3.3.1 applies in SDC E and F)" % sdc
        elif allr:
            lo = min(allr, key=lambda t: t[2])
            v4 = ("Not present: lowest story strength ratio %s story %d = %.0f%% of the story above (Type 4a: less "
                  "than the story above; Type 4b: less than 65%%; declared story strengths)" % (lo[0], lo[1], 100 * lo[2]))
        else:
            v4 = "Not present: every story at least as strong as the story above (declared story strengths)"
    elif sdc == "A" or (sdc in ("B", "C") and NF <= 2 and _hn_in <= 360.0 + 0.5):
        v4 = ("Not required in SDC %s: Types 4a/4b carry requirements only in SDC D&ndash;F (&sect;12.3.3.1, "
              "&sect;12.3.3.2)%s" % (sdc, "" if sdc == "A" else ", and the SDC B/C limit on a Type 4b story (&sect;12.3.3.3: "
                                     "not more than two stories or 30 ft) is met by this building"))
    elif sdc in ("B", "C"):
        v4 = (NE + " &mdash; story lateral strengths are needed to exclude a Type 4b extreme weak story, which "
              "&sect;12.3.3.3 limits in SDC B and C to buildings of two stories or 30 ft (Type 4a carries no SDC B/C "
              "requirement)")
    else:
        v4 = (NE + " &mdash; story lateral strengths are not computed by the analysis; the weak-story check "
              "(Table 12.3-2 Types 4a/4b) requires the story strengths")
    if sdc in ("A", "B", "C") and "NOT EVALUATED" in v1:
        v1 = ("Not required in SDC %s: Table 12.3-2 Types 1a/1b carry requirements only in SDC D&ndash;F (&sect;12.3.3.1); "
              % sdc) + v1.replace(NE, "not evaluated")
    rows = [
        ["Horizontal 1 &mdash; Torsional (Table 12.3-1a tiers)", tcls],
        ["Horizontal 2 &mdash; Re-entrant corner (both projections &gt; 20%)", h2],
        ["Horizontal 3 &mdash; Diaphragm discontinuity (&gt; 25% open / &gt; 50% stiffness change)", h3],
        ["Horizontal 4 &mdash; Out-of-plane offset", h4],
        ["Horizontal 5 &mdash; Nonparallel system", h5],
        ["Vertical 1a/1b &mdash; Soft / extreme soft story (70/80%, 60/70%)", v1],
        ["Vertical 2 &mdash; Geometric (SFRS dimension &gt; 130%)", v2],
        ["Vertical 3 &mdash; In-plane discontinuity", v3],
        ["Vertical 4a/4b &mdash; Weak / extreme weak story (less than the story above / less than 65%)", v4]]
    intro = ("<p>Evaluated against ASCE 7-22 Tables 12.3-1, 12.3-1a and 12.3-2 (SDC %s). TIR (Eq. 12.3-2) uses the "
             "story drifts at the two diaphragm edges under the &plusmn;5%% accidental eccentricity with A<sub>x</sub> = 1; "
             "story stiffness is V<sub>x</sub>/&Delta;<sub>xe</sub> at the centre of mass; the re-entrant, opening, "
             "offset and SFRS-dimension tests use the framed footprint and the lateral elements of the built model "
             "(plane coordinates from the element list)%s.%s</p>"
             % (sdc, "; declared drift-exempt split-level offsets are skipped" if exempt else "",
                " Items marked NOT EVALUATED need data the analysis does not compute."
                if any("NOT EVALUATED" in str(r[1]) for r in rows) else ""))
    req = []
    if trig["12.3.3.4"]:
        req.append("&sect;12.3.3.4 (%s): members supporting the discontinuous frames, and their connections, are designed "
                   "for the overstrength seismic load effect of &sect;12.4.3" % "/".join(sorted(set(trig["12.3.3.4"]))))
    if trig["12.3.3.5"] and sdc in ("D", "E", "F"):
        req.append("&sect;12.3.3.5 (%s): 25%% increase of the &sect;12.10.1.1 forces for diaphragm connections and "
                   "collectors at the affected levels (Exception: collectors designed with &Omega;<sub>0</sub>)"
                   % "/".join(sorted(set(trig["12.3.3.5"]))))
    if trig["12.3.3.1"]:
        req.append("<b>&sect;12.3.3.1: %s is NOT PERMITTED in SDC %s</b>" % ("/".join(trig["12.3.3.1"]), sdc))
    if trig["12.3.3.2"]:
        req.append("<b>&sect;12.3.3.2: Type 4b extreme weak story is NOT PERMITTED in SDC D</b>")
    if trig["12.3.3.3"]:
        req.append("<b>&sect;12.3.3.3: with a Type 4b extreme weak story in SDC %s the structure shall not be more than two "
                   "stories or 30 ft in structural height h<sub>n</sub>, unless the weak story resists &Omega;<sub>0</sub> times the "
                   "&sect;12.8 design force (Exception)</b>" % sdc)
    if trig["12.7.3 (3D model)"]:
        req.append("&sect;12.7.3: 3D analysis model required (%s) &mdash; the model is 3D" % "/".join(sorted(set(trig["12.7.3 (3D model)"]))))
    if trig["12.5 (orthogonal)"]:
        req.append("&sect;12.5: orthogonal (100/30) combination required (H5)")
    out = intro + _table(["Irregularity type", "Determination"], rows)
    if req:
        out += "<p><b>Provisions triggered:</b></p><ul>" + "".join("<li>%s</li>" % x for x in req) + "</ul>"
    irregular = bool(tr > 1.2 or (pl and (pl["reentrant"] or pl["opening"])) or term or inpl or npar or v2p
                     or tx or ty or cfg.get("skew"))
    if irregular and "RS" not in [a.upper() for a in cfg.get("analyses", [])]:
        out += ("<p class='cnote'><b>Analysis procedure &mdash; ADVISORY:</b> MRSA (&sect;12.9) is recommended for the "
                "irregularities above; <b>ELF is permitted by ASCE 7-22 &sect;12.6</b> for all structures (the 7-16 "
                "Table 12.6-1 procedure matrix was deleted).</p>")
    return out


def _llr_record(cfg, pkg=None):
    """The ASCE 7-22 4.7 live load reduction actually applied in the demand run (calc package record), or None."""
    da = ((pkg or {}).get("demand_analysis") or {}) if isinstance(pkg, dict) else {}
    llr = da.get("live_load_reduction")
    return llr if isinstance(llr, dict) else None


def _llr_sentence(cfg, pkg=None):
    llr = _llr_record(cfg, pkg)
    if llr and llr.get("applied"):
        unr = llr.get("unreduced_psf") or 0
        return ("Floor live load is reduced per ASCE 7-22 &sect;4.7 member by member: L = L<sub>o</sub>(0.25 + "
                "15/&radic;(K<sub>LL</sub>A<sub>T</sub>)) (Eq. 4.7-1), not less than 0.50L<sub>o</sub> for members supporting "
                "one floor and 0.40L<sub>o</sub> for members supporting two or more floors; K<sub>LL</sub> = %s for columns "
                "and %s for beams (Table 4.7-1); L/L<sub>o</sub> = %s to %s over the reduced members.%s Roof live load, "
                "live loads over 100 psf, garages and assembly occupancies are not reduced."
                % (_n(llr.get("KLL_col", 4)), _n(llr.get("KLL_beam", 2)), _n(llr.get("RF_min")), _n(llr.get("RF_max")),
                   (" The %s psf partition allowance is not reduced." % _n(unr)) if unr else ""))
    return ("No live-load reduction is taken (ASCE 7-22 &sect;4.7 not applied): every member carries the unreduced "
            "L<sub>o</sub> (conservative).")


def _gravity_loads_note(cfg=None, pkg=None):
    cfg = cfg or {}
    part = _partition_psf(cfg, pkg)
    snow = float(cfg.get("snow", 0) or 0)
    ptxt = ("The floor live load includes a %s psf partition allowance, which ASCE 7-22 &sect;4.3.2 treats as live "
            "load." % _n(part)) if part else \
           ("Any partition allowance (ASCE 7-22 &sect;4.3.2, a live load) is part of the declared floor live load.")
    smo = cfg.get("seismic_mass_only")
    wtxt = (" For the seismic weight W the partition weight (&sect;12.7.2 item 2) is included as listed above."
            if isinstance(smo, dict) and _levels_txt(smo) else "")
    _snow_rec = bool(re.search(r"snow drift|drift(?:ed)? snow|drift surcharge|unbalanced snow|sliding snow|p<sub>d</sub>|\bpd\s*=",
                               json.dumps(pkg or {}), re.I))
    if snow > 0 and _snow_rec:
        rtxt = ("The analysis model carries a uniform roof live load L<sub>r</sub> and the uniform roof snow load S; snow "
                "drift, unbalanced or sliding snow and ponding, where they apply, are addressed in the member design "
                "records (Chapter 6) and are not part of the analysis-model loading.")
    elif snow > 0:
        rtxt = ("The analysis model carries a uniform roof live load L<sub>r</sub> and the uniform roof snow load S. "
                "Snow drift, unbalanced and sliding snow are not part of the analysis-model loading; the snow basis in "
                "the load table above states which of them apply to this roof, and ponding is addressed under the "
                "rain basis.")
    else:
        rtxt = ("The analysis model carries a uniform roof live load L<sub>r</sub>. The design criteria contain no snow "
                "load (snow basis in the load table above), so no snow drift, unbalanced or sliding snow is carried; "
                "ponding is addressed under the rain basis.")
    return ("<p class='cnote'>Dead load D is the member self-weight plus the superimposed dead load (MEP, ceilings, "
            "finishes) and the fa&ccedil;ade cladding. %s%s %s %s</p>" % (ptxt, wtxt, _llr_sentence(cfg, pkg), rtxt))


def _wind_cc_note():
    return ("<p class='cnote'>The forces above are the main wind force-resisting system (MWFRS) loads on the building. "
            "Components and cladding (C&amp;C) pressures (ASCE 7-22 Ch. 30) for the cladding, its fasteners and their "
            "supports are part of the delegated cladding design.</p>")


def _seismic_by_dir(cfg):
    """Per-direction seismic design data {'X': {...}, 'Y': {...}, 'basis': 'ELF'|'MRSA'|'envelope'} from the
    engine's single source (engine3d.seismic_design_forces), falling back to elf_dir."""
    try:
        SF = E.seismic_design_forces(cfg)
        return {"X": SF["X"], "Y": SF["Y"], "basis": SF.get("basis", "ELF")}
    except Exception:
        out = {"basis": "ELF"}
        for d in ("X", "Y"):
            Cs, V, Tu, Ta, kk, Fx, W = E.elf_dir(cfg, d)
            s = E.seis_dir(cfg, d)
            out[d] = dict(Fx=Fx, F_elf=Fx, V=V, Cs=Cs, T=E.period_dir(cfg, d)[0], Tu=Tu, Ta=Ta, k=kk, W=W,
                          R=s["R"], Ie=s["Ie"], Cd=s.get("Cd"), Om0=s.get("Om0"), rho=s.get("rho"), rs=None)
        return out


def _cs_terms(cfg, d, Tu):
    s = E.seis_dir(cfg, d)
    R = float(s["R"]); Ie = float(s["Ie"]); SDS = float(s["SDS"]); SD1 = float(s["SD1"])
    S1 = float(s.get("S1", 0) or 0); TL = float(s.get("TL", 8.0) or 8.0)
    cs3 = SDS / (R / Ie)
    if Tu <= TL:
        cap, capeq = SD1 / (Tu * (R / Ie)), "12.8-4"
    else:
        cap, capeq = SD1 * TL / (Tu ** 2 * (R / Ie)), "12.8-5"
    cmin = max(0.044 * SDS * Ie, 0.01)
    cmin7 = 0.5 * S1 / (R / Ie) if S1 >= 0.6 else None
    low = max(cmin, cmin7 or 0.0); upper = min(cs3, cap)
    if upper <= low:
        gov = "Eq. 12.8-7" if (cmin7 and low == cmin7) else "Eq. 12.8-6 (minimum)"
    elif cap < cs3:
        gov = "Eq. %s" % capeq
    else:
        gov = "Eq. 12.8-3"
    return dict(cs3=cs3, cap=cap, capeq=capeq, cmin=cmin, cmin7=cmin7, gov=gov, R=R, Ie=Ie)


def _seismic_loads_section(cfg, T, eX, eY, Cs=None, V=None, Tu=None, Ta=None, Fx=None, W=None):
    """Chapter 3 seismic loads, per principal direction (ASCE 7-22 12.2.2 / 12.8): each direction's period,
    C_s and its limits, k, base shear and vertical distribution; the 12.9 MRSA scaling where 'RS' is run."""
    NF = len(cfg["heights"]); z = E.zlevels(cfg); s = cfg["seis"]
    SD = _seismic_by_dir(cfg)
    X, Y = SD["X"], SD["Y"]
    Wt = X.get("W") or W
    smo = cfg.get("seismic_mass_only")
    wtxt = ("dead load, cladding" + (" and the partition allowance (%s, &sect;12.7.2 item 2)" % _levels_txt(smo)
                                     if isinstance(smo, dict) and _levels_txt(smo) else "")
            + (", 25% of the storage live load" if cfg.get("storage") or cfg.get("storage_levels") else "")
            + (", the lumped seismic weights listed with the gravity loads" if isinstance(cfg.get("seismic_weight_kip"), dict)
               and cfg.get("seismic_weight_kip") else "")
            + (", the self-weight of the columns and braces" if isinstance(cfg.get("seismic_weight_members_kip"), dict)
               and cfg.get("seismic_weight_members_kip") else "")
            + (", 15% of the uniform design snow load (p<sub>f</sub> &gt; 45 psf, item 4)"
               if float(cfg.get("snow", 0) or 0) > 45.0 else ""))
    rs_on = SD.get("basis", "ELF") != "ELF"
    intro = ("<p>ASCE 7-22 &sect;12.8 equivalent lateral force procedure in each principal direction, each with its own "
             "fundamental period and its own system factors (&sect;12.2.2)%s. Effective seismic weight W = %s kip "
             "(&sect;12.7.2: %s). S<sub>DS</sub> = %s g, S<sub>D1</sub> = %s g, S<sub>1</sub> = %s g, I<sub>e</sub> = %s. "
             "Approximate period T<sub>a</sub> = C<sub>t</sub>h<sub>n</sub><sup>x</sup> (Eq. 12.8-8); the period used is "
             "T = min(T<sub>model</sub>, C<sub>u</sub>T<sub>a</sub>) (&sect;12.8.2).</p>"
             % ("; a modal response spectrum analysis (&sect;12.9.1) is also run (below)" if rs_on else "",
                _n(Wt), wtxt, _sa(s.get("SDS")), _sa(s.get("SD1")), _sa(s.get("S1", 0)), _fac(s.get("Ie", 1.0))))
    def _cs(v):         # 4 significant figures: V = C_s W re-derives from the printed numbers
        return "%.4g" % float(v) if v else "0"
    def _k(v):
        return _strip0(_fx(float(v), 3))
    rows = []; csr = {}
    for d in ("X", "Y"):
        r = SD[d]; ct = _cs_terms(cfg, d, r["Tu"]); csr[d] = ct
        sd = E.seis_dir(cfg, d)
        rows.append([d, _fac(r["R"]), "%.3f" % r["T"], "%.3f" % r["Ta"], "%.3f" % (float(sd.get("Cu", 0) or 0) * r["Ta"]),
                     "%.3f" % r["Tu"], "%s (%s)" % (_cs(r["Cs"]), ct["gov"]), _k(r["k"]), _n(r["V"])])
    ptab = ("<h4>Period, seismic response coefficient and base shear per direction</h4>"
            + _table(["Direction", "R", "T<sub>model</sub> (s)", "T<sub>a</sub> (s)", "C<sub>u</sub>T<sub>a</sub> (s)",
                      "T used (s)", "C<sub>s</sub> (governing)", "k (&sect;12.8.3)", "V (kip)"], rows))
    crows = [["C<sub>s</sub> = S<sub>DS</sub>/(R/I<sub>e</sub>) &nbsp;(Eq. 12.8-3)", _cs(csr["X"]["cs3"]), _cs(csr["Y"]["cs3"])],
             ["C<sub>s,max</sub> = S<sub>D1</sub>/(T&middot;R/I<sub>e</sub>) &nbsp;(Eq. %s)" % csr["X"]["capeq"]
              if csr["X"]["capeq"] == csr["Y"]["capeq"] else "C<sub>s,max</sub> (Eq. 12.8-4 / 12.8-5)",
              _cs(csr["X"]["cap"]), _cs(csr["Y"]["cap"])],
             ["C<sub>s,min</sub> = max(0.044S<sub>DS</sub>I<sub>e</sub>, 0.01) &nbsp;(Eq. 12.8-6)",
              _cs(csr["X"]["cmin"]), _cs(csr["Y"]["cmin"])]]
    if csr["X"]["cmin7"] is not None or csr["Y"]["cmin7"] is not None:
        crows.append(["C<sub>s,min</sub> = 0.5S<sub>1</sub>/(R/I<sub>e</sub>) &nbsp;(Eq. 12.8-7, S<sub>1</sub> &ge; 0.6g)",
                      _cs(csr["X"]["cmin7"] or 0), _cs(csr["Y"]["cmin7"] or 0)])
    crows.append(["<b>Governing C<sub>s</sub></b>", "<b>%s</b>" % _cs(X["Cs"]), "<b>%s</b>" % _cs(Y["Cs"])])
    cstab = ("<h4>Seismic response coefficient C<sub>s</sub> and its limits (&sect;12.8.1.1)</h4>"
             + _table(["C<sub>s</sub> equation", "X direction", "Y direction"], crows))
    same = abs(X["V"] - Y["V"]) < 0.5
    if same:
        base = ("<p>Design base shear V = C<sub>s</sub>W = %s &times; %s = <b>%s kip in each direction</b> (Eq. 12.8-1).</p>"
                % (_cs(X["Cs"]), _n(Wt), _n(X["V"])))
    else:
        base = ("<p>Design base shear (Eq. 12.8-1): <b>V<sub>X</sub> = %s &times; %s = %s kip</b>; "
                "<b>V<sub>Y</sub> = %s &times; %s = %s kip</b>.</p>"
                % (_cs(X["Cs"]), _n(Wt), _n(X["V"]), _cs(Y["Cs"]), _n(Y.get("W") or Wt), _n(Y["V"])))
    FX, FY = X.get("F_elf") or X.get("Fx"), Y.get("F_elf") or Y.get("Fx")
    same_f = same and abs(X["k"] - Y["k"]) < 1e-6
    if same_f:
        kk = X["k"]
        _zb = E.elf_base_z(cfg) if hasattr(E, "elf_base_z") else z[0]
        vrows = [[k, _n(E.floor_w(cfg, k)), _n(max(z[k] - _zb, 0.0) / 12), _n(E.floor_w(cfg, k) * (max(z[k] - _zb, 0.0) / 12) ** kk),
                  "%.4f" % (FX[k] / X["V"] if X["V"] else 0.0), _n(FX[k])] for k in range(1, NF + 1)]
        vtab = ("<h4>Vertical distribution of the base shear (k = %s, &sect;12.8.3)</h4>" % _k(kk)
                + "<p>F<sub>x</sub> = C<sub>vx</sub>V, &nbsp; C<sub>vx</sub> = w<sub>x</sub>h<sub>x</sub><sup>k</sup> / "
                "&Sigma; w<sub>i</sub>h<sub>i</sub><sup>k</sup> (Eq. 12.8-12, 12.8-13); identical in both directions.</p>"
                + _table(["Level", "w<sub>x</sub> (kip)", "h<sub>x</sub> (ft)", "w<sub>x</sub>h<sub>x</sub><sup>k</sup>",
                          "C<sub>vx</sub>", "F<sub>x</sub> (kip)"], vrows))
    else:
        _zb = E.elf_base_z(cfg) if hasattr(E, "elf_base_z") else z[0]
        vrows = [[k, _n(E.floor_w(cfg, k)), _n(max(z[k] - _zb, 0.0) / 12),
                  "%.4f" % (FX[k] / X["V"] if X["V"] else 0.0), _n(FX[k]),
                  "%.4f" % (FY[k] / Y["V"] if Y["V"] else 0.0), _n(FY[k])] for k in range(1, NF + 1)]
        vtab = ("<h4>Vertical distribution of the base shear (&sect;12.8.3; k<sub>X</sub> = %s, k<sub>Y</sub> = %s)</h4>"
                % (_k(X["k"]), _k(Y["k"]))
                + "<p>F<sub>x</sub> = C<sub>vx</sub>V, &nbsp; C<sub>vx</sub> = w<sub>x</sub>h<sub>x</sub><sup>k</sup> / "
                "&Sigma; w<sub>i</sub>h<sub>i</sub><sup>k</sup> (Eq. 12.8-12, 12.8-13), per direction.</p>"
                + _table(["Level", "w<sub>x</sub> (kip)", "h<sub>x</sub> (ft)", "C<sub>vx</sub> X", "F<sub>x</sub> X (kip)",
                          "C<sub>vx</sub> Y", "F<sub>x</sub> Y (kip)"], vrows))
    mr = ""
    if rs_on and (X.get("rs") or Y.get("rs")):
        rr = []
        for d in ("X", "Y"):
            q = SD[d].get("rs") or {}
            if not q:
                continue
            rr.append([d, _n(q.get("Vt")), _n(q.get("V")), "%.3f" % float(q.get("force_scale", 1.0)),
                       "%.3f" % float(q.get("drift_scale", 1.0)), "%.0f%%" % (100.0 * float(q.get("mass", 0) or 0)),
                       _n(q.get("nmodes"))])
        mr = ("<h4>Modal response spectrum analysis (&sect;12.9.1)</h4>"
              "<p>CQC combination, 5%% damping. Forces are scaled to the ELF base shear where V<sub>t</sub> &lt; V "
              "(&sect;12.9.1.4.1); drifts are scaled by C<sub>s</sub>W/V<sub>t</sub> only where C<sub>s</sub> is set by "
              "Eq. 12.8-7 (&sect;12.9.1.4.2). Member demands use the %s.</p>"
              % ("story-shear envelope of the ELF and the scaled MRSA (never below either in any story)"
                 if SD.get("basis") == "envelope" else "scaled MRSA story forces" if SD.get("basis") == "MRSA" else "ELF forces")
              + _table(["Direction", "V<sub>t</sub> MRSA (kip)", "V ELF (kip)", "force scale V/V<sub>t</sub>",
                        "drift scale", "modal mass", "modes"], rr))
        srows = []
        for k in range(1, NF + 1):
            row = [k]
            for d in ("X", "Y"):
                r = SD[d]; q = r.get("rs") or {}
                Fe = r.get("F_elf") or {}; Fd = r.get("Fx") or {}
                ve = sum(Fe.get(i, 0.0) for i in range(k, NF + 1)); vd = sum(Fd.get(i, 0.0) for i in range(k, NF + 1))
                vm = (q.get("shear") or [None] * NF)[k - 1]
                row += [_n(ve), _n(vm) if vm is not None else "&mdash;", _n(vd)]
            srows.append(row)
        mr += _table(["Story", "ELF shear X (kip)", "scaled MRSA X (kip)", "design X (kip)", "ELF shear Y (kip)",
                      "scaled MRSA Y (kip)", "design Y (kip)"], srows)
    cumX = cumY = 0.0; mrows = []
    for m in range(min(len(T), 12)):
        cumX += eX[m]; cumY += eY[m]
        mrows.append([m+1, "%.3f" % T[m], "%.1f" % (eX[m]*100), "%.1f" % (cumX*100), "%.1f" % (eY[m]*100), "%.1f" % (cumY*100)])
    mtab = ("<h4>Modal periods and mass participation (&ge; 90% per &sect;12.9.1.1)</h4>"
            + _table(["Mode", "T (s)", "m<sub>X</sub> %", "&Sigma;m<sub>X</sub> %", "m<sub>Y</sub> %", "&Sigma;m<sub>Y</sub> %"], mrows)
            + "<p class='cnote'>Vertical seismic E<sub>v</sub> = 0.2S<sub>DS</sub>D, redundancy &rho;, the 100/30 directional "
            "combination and &plusmn;5% accidental torsion are applied in the load combinations (Chapter 4); torsional "
            "amplification is screened in Chapter 2.</p>")
    return intro + ptab + cstab + base + vtab + mr + mtab


def _governing_lateral(cfg, V, VwX, VwY, SD=None):
    """Strength-level base-shear comparison per direction: each direction's own seismic V (12.2.2) vs its wind V."""
    if SD is None:
        try:
            SD = _seismic_by_dir(cfg)
        except Exception:
            SD = None
    Vd = {d: (SD[d]["V"] if SD else V) for d in ("X", "Y")}
    if Vd["X"] is None:
        return "<p class='note'>[seismic base shear unavailable]</p>"
    rows = []
    for d, lab, Vw in (("X", "X (E-W)", VwX), ("Y", "Y (N-S)", VwY)):
        if Vw is None:
            rows.append([lab, _n(Vd[d]), "&mdash; (no wind defined)", "<b>Seismic</b>"])
        else:
            rows.append([lab, _n(Vd[d]), _n(Vw), "<b>%s</b>" % ("Seismic" if Vd[d] >= Vw else "Wind")])
    note = ("<p class='cnote'>Strength-level base-shear comparison (E and W are both strength-level loads in the ASCE "
            "7-22 LRFD combinations). Both hazards are carried through the load combinations (Chapter 4); the member "
            "design forces are the envelope of all combinations.</p>")
    return _table(["Direction", "Seismic V (kip)", "Wind V, Case 1 (kip)", "Governs base shear"], rows) + note


def _case_kind(c):
    return str(getattr(c, "kind", "") or "")


def _cases_info(cases):
    """What the combination set contains: Omega0 column cases (dirs), capacity-limited Ecl, wind Fig. 27.3-8 cases."""
    om = set(); ecl = False; col_only = False
    for c in cases or []:
        lab = str(c[0]); co = bool(c[5]) if len(c) > 5 else False
        if co: col_only = True
        if _case_kind(c) == "omega0" or ("Om0" in lab and co):
            om.add("X" if "EX" in lab else "Y")
        if _case_kind(c).startswith("ecl") or "Ecl[" in lab or "capacity-limited" in lab or "OCBF V-beam" in lab:
            ecl = True
    return {"om0_dirs": sorted(om), "ecl": ecl, "col_only": col_only, "wind_cases": _wind_cases_present(cases)}


def _combo_table(cases):
    def lab(c):
        label, lat = str(c[0]), c[4]
        if not lat and "Ecl" not in label: return "&mdash;"
        wc = getattr(c, "wind_case", None) or next((k for k in (2, 3, 4) if "[C%d" % k in label), None)
        if "WX" in label or "WY" in label or "W[" in label or wc:
            return "wind (Fig. 27.3-8 Case %s)" % (wc or 1)
        if "Ecl" in label: return "seismic, capacity-limited"
        if "EX" in label or "EY" in label: return "seismic"
        return "lateral"
    def applies(c):
        co = bool(c[5]) if len(c) > 5 else False
        ap = getattr(c, "applies", None)
        if ap:
            return ", ".join({"col": "columns", "beam": "beams", "brace": "braces"}.get(a, a) for a in sorted(ap)) + " only"
        return "columns only" if co else "all members"
    def lf(c):
        f = c[2]; hi = getattr(f, "hi", None)
        if hi is None or abs(float(hi) - float(f)) < 1e-9:
            return "%.2f" % float(f)
        l_ = str(c[0])
        if "1.0L" in l_ and "0.5L" not in l_:
            return "%.2f" % float(hi)
        if "0.5L" in l_ and "1.0L" not in l_:
            return "%.2f" % float(f)
        return "%.2f / %.2f" % (float(f), float(hi))             # by level (2.3.6 Exception 1)
    def rf(c):
        # code factor(s) on the roof loads named in the combination (RoofFactors carries Lr / S / R separately;
        # its float value is only a multiplier on the modelled roof-load pattern)
        f = c[3]
        if all(hasattr(f, a) for a in ("Lr", "S", "R")):
            parts = ["%.2f%s" % (float(getattr(f, a)), lab_) for a, lab_ in (("Lr", "L<sub>r</sub>"), ("S", "S"), ("R", "R"))
                     if abs(float(getattr(f, a) or 0.0)) > 1e-9]
            return " + ".join(parts) if parts else "&mdash;"
        return "%.2f" % float(f)
    rows = [[_combo_label(c[0]), "%.2f" % c[1], lf(c), rf(c), lab(c), applies(c)] for c in cases]
    return (_table(["Combination", "D", "L", "Roof load (L<sub>r</sub>, S or R)", "Lateral", "Applies to"], rows)
            + "<p class='cnote'>The roof-load column gives the load factor and the roof load it applies to: roof live "
              "L<sub>r</sub>, snow S or rain R. Each roof load present is combined in its own combinations (ASCE 7-22 "
              "&sect;2.3.1: 1.6L<sub>r</sub>, 1.0S or 1.6R principal; 0.5L<sub>r</sub>, 0.3S or 0.5R companion; 0.15S "
              "with seismic, &sect;2.3.6).</p>")


def _combo_legend(cfg, cases=None, pkg=None, ctx=None):
    ctx = ctx or {}
    info = _cases_info(cases or [])
    try:
        sdc = E.sdc_of(cfg)
    except Exception:
        sdc = ""
    llr = _llr_record(cfg, pkg)
    ltxt = ("floor live load, reduced per ASCE 7-22 &sect;4.7 member by member (L/L<sub>o</sub> %s&ndash;%s; Chapter 3)"
            % (_n(llr.get("RF_min")), _n(llr.get("RF_max"))) if llr and llr.get("applied")
            else "floor live load, unreduced (no &sect;4.7 reduction taken)")
    items = [("D", "dead load"),
             ("L", ltxt),
             (("L<sub>r</sub> / S / R", "roof live load / snow load / rain load (Ch. 8)") if float(cfg.get("rain") or 0.0) > 0
              or cfg.get("rain_by_level") else ("L<sub>r</sub> / S", "roof live load / snow load")),
             ("E<sub>X</sub>, E<sub>Y</sub>", "horizontal seismic load effect Q<sub>E</sub> in the X (E-W) / Y (N-S) direction; "
              "the vertical term E<sub>v</sub> = 0.2S<sub>DS</sub>D is folded into the D factor"),
             ("W<sub>X</sub>, W<sub>Y</sub>", "MWFRS wind load in the X / Y direction (Fig. 27.3-8 Case 1)"),
             ("&rho;", "redundancy factor multiplying Q<sub>E</sub> (&sect;12.3.4): " + _rho_txt(cfg, sdc)),
             ("t+ / t&minus;", "&plusmn;5% accidental torsion M<sub>t</sub> = &plusmn;0.05B&middot;A<sub>x</sub>&middot;F<sub>x</sub> "
              "(&sect;12.8.4.2 / 12.8.4.3)"),
             ("+ / &minus;", "sign (direction) of the applied lateral load"),
             ("/0.3X, /0.3Y", "100% of the seismic effect in the loaded direction plus 30% of the orthogonal one (&sect;12.5.3)")]
    if cfg.get("dam", True) is not False:
        items.append(("+N<sub>i</sub>", "notional lateral loads N<sub>i</sub> = 0.002&alpha;Y<sub>i</sub>, &alpha; = 1.0 "
                      "(AISC 360-22 C2.2b), added to the combination in the direction shown"))
    if info["wind_cases"] & {2, 3, 4}:
        items.append(("[C2 e&plusmn;], [C3], [C4 e&plusmn;]", "ASCE 7-22 Fig. 27.3-8 wind Cases 2&ndash;4: 75% of Case 1 "
                      "with eccentricity e = &plusmn;0.15B (Case 2), 75% along both axes (Case 3), 56.3% along both axes "
                      "with the eccentricity (Case 4)"))
    if info["om0_dirs"]:
        items.append(("&Omega;<sub>0</sub>, [col]", "overstrength factor; the &ldquo;[col]&rdquo; cases apply "
                      "&Omega;<sub>0</sub>Q<sub>E</sub> (axial) to the SFRS columns only (&sect;12.4.3)"))
    if info["ecl"]:
        items.append(("E<sub>cl</sub>", "capacity-limited horizontal seismic load effect (&sect;12.4.3.2)"))
    items.append(("0.5L / 1.0L, 0.3S / 0.5L<sub>r</sub> / 0.5R, 0.15S, 0.9D",
                  "companion / counteracting load factors (ASCE 7-22 &sect;2.3.1 / &sect;2.3.6: companion L = 1.0 where "
                  "L<sub>o</sub> &gt; 100 psf or garage / public assembly, else 0.5; companion roof loads 0.5L<sub>r</sub>, "
                  "0.3S or 0.5R (gravity / wind) and 0.15S (seismic); principal snow 1.0S, principal rain 1.6R)"))
    return "<h4>Notation used in the combination labels</h4>" + _table(["Symbol", "Meaning"], [[a, b] for a, b in items])


def _joint_figure(cfg):
    """Schematic of the ACTUAL modelled boundary conditions for THIS job (base fixity and joint
    types from cfg['model'], braced-bay panel only when the model has braces) -- no boilerplate
    about frames or grid lines that aren't in the present building."""
    import matplotlib.pyplot as _plt
    md = (cfg.get("model") or {})
    bases = str(md.get("bases") or cfg.get("base") or "pinned").lower()
    joints = str(md.get("joints") or ("pinned" if cfg.get("releases") else "rigid")).lower()
    try:
        braced = E.is_braced(cfg)
    except Exception:
        braced = bool(cfg.get("brace") or cfg.get("braces"))
    ncol = 3 if braced else 2
    fig, axs = _plt.subplots(1, ncol, figsize=(4.9*ncol + 1.0, 4.6))

    def _fixed_base(ax, x, label):
        ax.plot([x, x], [3, 8.5], lw=3, color="#333"); ax.plot([x-1.3, x+1.3], [3, 3], lw=4, color="#333")
        for h in (-1.1, -0.5, 0.1, 0.7): ax.plot([x+h, x+h-0.45], [3, 2.45], lw=1, color="#333")
        ax.text(x, 9.0, "FIXED base", ha="center", fontweight="bold", fontsize=11)
        ax.text(x, 1.6, label, ha="center", fontsize=8.5)
    def _pinned_base(ax, x, label):
        ax.plot([x, x], [3, 8.5], lw=3, color="#1f77b4"); ax.plot([x-0.7, x+0.7], [2.55, 2.55], lw=4, color="#333")
        for h in (-0.5, 0.1, 0.6): ax.plot([x+h, x+h-0.45], [2.55, 2.0], lw=1, color="#333")
        ax.plot([x-0.4, x, x+0.4], [2.55, 3, 2.55], color="#1f77b4", lw=2)
        ax.add_patch(_plt.Circle((x, 3), 0.16, fill=False, color="#1f77b4", lw=2))
        ax.text(x, 9.0, "PINNED base", ha="center", fontweight="bold", fontsize=11)
        ax.text(x, 1.6, label, ha="center", fontsize=8.5)

    ax = axs[0]; ax.set_title("Column base fixity"); ax.axis("off"); ax.set_xlim(0, 10); ax.set_ylim(0, 10)
    if "mix" in bases:
        _fixed_base(ax, 2.4, "lateral-frame columns"); _pinned_base(ax, 7.4, "all other columns")
    elif "fix" in bases:
        _fixed_base(ax, 5, "ALL column bases in this model")
    else:
        _pinned_base(ax, 5, "ALL column bases in this model")

    def _fr_joint(ax, x, label):
        ax.plot([x, x], [1, 9], lw=3, color="#333"); ax.plot([x, x+3.0], [6, 6], lw=3, color="#333")
        ax.add_patch(_plt.Rectangle((x-0.25, 5.55), 0.5, 0.9, color="#d62728"))
        ax.plot([x+0.3, x+1.0], [6.5, 6.9], lw=1.5, color="#d62728"); ax.plot([x+0.3, x+1.0], [5.5, 5.1], lw=1.5, color="#d62728")
        ax.text(x+1.4, 7.2, "FR (rigid)\nmoment joint", fontsize=9, color="#d62728", fontweight="bold")
        ax.text(x+1.4, 4.6, label, fontsize=8.5)
    def _pin_joint(ax, x, label, y=3.0):
        ax.plot([x, x], [1, 9], lw=3, color="#333"); ax.plot([x, x+2.6], [y, y], lw=3, color="#1f77b4")
        ax.add_patch(_plt.Circle((x+0.18, y), 0.16, fill=False, color="#1f77b4", lw=2))
        ax.text(x+0.35, y+1.0, "PINNED (shear) joint", fontsize=9, color="#1f77b4", fontweight="bold")
        ax.text(x+0.35, y-1.4, label, fontsize=8.5)

    ax = axs[1]; ax.set_title("Beam-to-column joints"); ax.axis("off"); ax.set_xlim(0, 10); ax.set_ylim(0, 10)
    if "mix" in joints:
        _fr_joint(ax, 1.6, "moment-frame girders"); _pin_joint(ax, 6.6, "all other girders")
    elif joints.startswith("pin"):
        _pin_joint(ax, 3.0, "ALL girder joints in this model\n(shear / simple connections)", y=6.0)
    else:
        _fr_joint(ax, 3.0, "ALL girder joints in this model")

    if braced:
        ax = axs[2]; ax.set_title("Bracing"); ax.axis("off"); ax.set_xlim(0, 10); ax.set_ylim(0, 10)
        ax.plot([2, 2], [2, 8], lw=3, color="#333"); ax.plot([8, 8], [2, 8], lw=3, color="#333")
        ax.plot([2, 8], [8, 8], lw=3, color="#333"); ax.plot([2, 8], [2, 2], lw=2, color="#666")
        ax.plot([2, 8], [2, 8], lw=2.5, color="#e67e22"); ax.plot([2, 8], [8, 2], lw=2.5, color="#e67e22")
        for (bx, by) in ((2, 2), (8, 8), (2, 8), (8, 2)):
            ax.add_patch(_plt.Circle((bx, by), 0.22, fill=False, color="#e67e22", lw=2))
        ax.text(5, 0.8, "braces: PIN-ENDED axial (truss) members\nno end moments, tension/compression only",
                ha="center", fontsize=8.5, color="#a3540a")
    fig.suptitle("Modelled joint & base fixity", fontweight="bold")
    return _b64(fig)

def _floor_serviceability(pkg):
    """Agent-recorded member serviceability (live / total deflection, camber); empty string when none (the engine
    deflection table follows in the same chapter)."""
    ms = (pkg or {}).get("members") or []
    rows = []
    for m in ms:
        sv = m.get("serviceability") if isinstance(m, dict) else None
        if not isinstance(sv, dict) or not sv:
            continue
        sec = (m.get("inputs", {}) or {}).get("section", m.get("id", ""))
        def dfl(a, b):
            if sv.get(a) in (None, ""):
                return "&mdash;"
            return "%s in. (%s)" % (_n(sv.get(a)), _n(sv.get(b))) if sv.get(b) not in (None, "") else "%s in." % _n(sv.get(a))
        rows.append([_esc(m.get("id", "")), _esc(sec), dfl("live_in", "live_ratio"), _n(sv.get("live_limit", "L/360")),
                     dfl("total_in", "total_ratio"), _n(sv.get("total_limit", "L/240")),
                     ("%s in." % _n(sv.get("camber_in"))) if sv.get("camber_in") not in (None, "", 0, 0.0) else "none",
                     "OK" if sv.get("ok") else ("<b class='ng'>NG</b>" if "ok" in sv else "&mdash;")])
    if not rows:
        return ""
    return ("<h3>Floor and roof beam deflection and camber (design records)</h3>"
            "<p>Live-load and total-load deflection limits per IBC Table 1604.3, as listed for each member (floor members "
            "L/360 and L/240; roof members by the ceiling they support). Composite "
            "floor members use a lower-bound transformed moment of inertia I<sub>LB</sub>; the bare-steel (wet-concrete) "
            "dead-load deflection is offset by shop camber where specified.</p>"
            + _table(["Member", "Section", "Live &delta;", "Limit", "Total &delta;", "Limit", "Camber", "&le; limit"], rows))


def _combo_notes(cfg, cases=None, ctx=None):
    """Chapter 4 bullets describing exactly what the combination set contains: E_v, rho, Omega0 only where cases
    exist (else the reason none is required), E_cl only where formed, 100/30, accidental torsion, uplift, wind
    load cases, second order."""
    ctx = ctx or {}
    s = cfg["seis"]; SDS = s["SDS"]
    info = _cases_info(cases or [])
    def _pd(key, default):
        # per-direction factor (engine3d.seis_dir, ASCE 7-22 12.2.2); one value when X == Y
        try:
            v = {d: E.seis_dir(cfg, d).get(key) for d in ("X", "Y")}
        except Exception:
            v = {d: (cfg.get(key) if key == "rho" else s.get(key)) for d in ("X", "Y")}
        v = {d: (default if x is None else x) for d, x in v.items()}
        return _n(v["X"]) if v["X"] == v["Y"] else "%s (X) / %s (Y)" % (_n(v["X"]), _n(v["Y"]))
    rho = _pd("rho", 1.0); Om0 = _pd("Om0", 2.0)
    rho = rho if ("(" in rho) else _fac(rho)
    Om0 = Om0 if ("(" in Om0) else _fac(Om0)
    dam = cfg.get("dam", True) is not False
    try:
        sdc = E.sdc_of(cfg)
    except Exception:
        sdc = ""
    a341d = ctx.get("a341_dirs") or {}
    if info["om0_dirs"]:
        why = []
        if any(a341d.get(d) for d in info["om0_dirs"]):
            why.append("AISC 341-22 D1.4a(b)")
        if not all(a341d.get(d) for d in info["om0_dirs"]):
            why.append("declared for the members supporting discontinuous frames, &sect;12.3.3.4")
        om_b = ("<li><b>Overstrength &Omega;<sub>0</sub></b> = %s: the <i>columns-only</i> [col] cases apply "
                "E<sub>mh</sub> = &Omega;<sub>0</sub>Q<sub>E</sub> (axial) to the SFRS columns in the %s direction%s "
                "(&sect;12.4.3; %s), and are not applied to other members.</li>"
                % (Om0, "/".join(info["om0_dirs"]), "s" if len(info["om0_dirs"]) > 1 else "", "; ".join(why)))
    else:
        reasons = []
        try:
            r3 = all(float(E.seis_dir(cfg, d)["R"]) <= 3.0 for d in ("X", "Y"))
        except Exception:
            r3 = False
        if r3:
            reasons.append("AISC 341 does not apply (R = 3 system not specifically detailed for seismic resistance)")
        if ctx.get("discontinuity") is False:
            reasons.append("no Table 12.3-1 Type 4 or Table 12.3-2 Type 3 discontinuity exists (Chapter 2), so "
                           "&sect;12.3.3.4 does not apply")
        if sdc in ("A", "B"):
            reasons.append("the &sect;12.10.2.1 collector overstrength applies in SDC C&ndash;F only (SDC %s)" % sdc)
        om_b = ("<li><b>No overstrength (&Omega;<sub>0</sub>) combinations</b>: ASCE 7-22 &sect;12.4.3 applies the "
                "overstrength load effect only where a provision requires it%s.</li>"
                % ((": " + "; ".join(reasons)) if reasons else ""))
    ecl_b = ("<li><b>Capacity-limited E<sub>cl</sub></b> (&sect;12.4.3.2; AISC 341-22 Chapter F): where the system has "
             "yielding braces / links / webs, separate cases replace them by their expected or adjusted strengths, "
             "yielding in X and Y together; their results enter the column, beam, splice, base and brace-connection "
             "demands. Effects the analysis does not form are listed in Chapter 5.</li>") if info["ecl"] else ""
    wc = info["wind_cases"]
    wind_b = ""
    if cfg.get("wind"):
        wind_b = ("<li><b>Wind</b>: combinations 4 (1.2D + 1.0W + L + 0.5L<sub>r</sub>/0.3S/0.5R) and 5 (0.9D + 1.0W) "
                  "with W in both senses along each axis; %s 0.9D + 1.0W includes the MWFRS roof suction "
                  "(Fig. 27.3-1).</li>" % ("the Fig. 27.3-8 Cases 2&ndash;4 are included;" if wc & {2, 3, 4} else
                                           "only Case 1 of Fig. 27.3-8 is included;"))
    up_tail = ""
    if info["om0_dirs"] or info["ecl"]:
        up_tail = " (incl. the %s cases)" % " and ".join(x for x, f in (("&Omega;<sub>0</sub>", info["om0_dirs"]),
                                                                      ("E<sub>cl</sub>", info["ecl"])) if f)
    return ("<p>The analysed set is the ASCE 7-22 &sect;2.3 LRFD strength combinations:</p><ul>"
            f"<li><b>Vertical seismic E<sub>v</sub></b> = 0.2&middot;S<sub>DS</sub>D = {0.2*SDS:.3f}D is folded into the "
            f"D factor: seismic combinations use (1.2+0.2S<sub>DS</sub>)D = {1.2+0.2*SDS:.3f}D and "
            f"(0.9&minus;0.2S<sub>DS</sub>)D = {0.9-0.2*SDS:.3f}D.</li>"
            f"<li><b>Redundancy &rho;</b> = {rho} multiplies the horizontal seismic effect Q<sub>E</sub> of its "
            "direction (&sect;12.3.4).</li>"
            + om_b + ecl_b +
            "<li><b>Directional 100/30</b>: each seismic case carries 100% of F<sub>x</sub> in the loaded direction "
            "plus 30% of the orthogonal direction's own forces and factor, in both signs (&sect;12.5.3/12.5.4).</li>"
            "<li><b>Accidental torsion</b> &plusmn;M<sub>t</sub> = &plusmn;0.05B&middot;A<sub>x</sub>&middot;F<sub>x</sub> "
            "(&sect;12.8.4.2/12.8.4.3), with the story forces applied at the centre of mass, in both signs.</li>"
            + wind_b +
            "<li><b>Net uplift / overturning</b> is checked by the 0.9D combinations (minimum gravity with lateral); the "
            "column-base reactions are enveloped over every combination" + up_tail + " (Chapter 11).</li>"
            "<li><b>Second order</b>: every combination is solved independently through a P-&Delta; analysis; "
            "factored second-order results are never superposed. "
            + ("The strength model uses the Direct Analysis Method (AISC 360-22 C2: 0.8 stiffness reduction with "
               "&tau;<sub>b</sub> per C2.3, notional loads N<sub>i</sub> = 0.002&alpha;Y<sub>i</sub>, &alpha; = 1.0, per C2.2b, plus "
               "0.001&alpha;Y<sub>i</sub> where &tau;<sub>b</sub> = 1.0 is taken per C2.3(c); parameters in Chapter 5); drift, "
               "periods and deflections use nominal stiffness.</li></ul>" if dam else
               "Nominal stiffness without notional loads is used (not the Direct Analysis Method); effective lengths "
               "follow AISC 360-22 Appendix 7.</li></ul>"))


def _modal_mass_check(eX, eY):
    cx = sum(eX); cy = sum(eY)
    return (f"<p><b>Modal mass participation:</b> &Sigma;m<sub>X</sub> = {cx*100:.0f}% "
            f"({'OK' if cx >= 0.90 else 'review &lt; 90%'}), &Sigma;m<sub>Y</sub> = {cy*100:.0f}% "
            f"({'OK' if cy >= 0.90 else 'review &lt; 90%'}); ASCE 7-22 &sect;12.9.1.1 requires &ge; 90% in each "
            "direction. Periods are listed in Chapter 3.</p>")


def _stability_basis_note(cfg=None):
    """Chapter 5 analysis-basis note. Describes the stability method from the cfg / pipeline instead of
    asserting K = 1 (HR-32): K = 1 is valid only with the Direct Analysis Method (AISC 360-22 C1/C2: reduced
    stiffness per C2.3 and notional loads per C2.2b, or the equivalent) -- otherwise Appendix 7."""
    cfg = cfg or {}
    meth = cfg.get("stability_method") or cfg.get("analysis_method")
    dam = cfg.get("dam", True) is not False
    applied = ("the member demands use the Direct Analysis Method (AISC 360-22 C2: 0.8 stiffness reduction, "
               "&tau;<sub>b</sub> per C2.3, notional loads N<sub>i</sub> = 0.002&alpha;Y<sub>i</sub>, &alpha; = 1.0, per C2.2b, plus "
               "0.001&alpha;Y<sub>i</sub> where &tau;<sub>b</sub> = 1.0 is taken per C2.3(c))" if dam else
               "the member demands use nominal stiffness without notional loads (not the Direct Analysis Method)")
    mtxt = ("the declared stability design method is <b>%s</b>; %s" % (_esc(meth), applied)) if meth else applied
    return ("<p class='cnote'><b>Analysis basis:</b> a second-order P-&Delta; geometric transformation is applied to "
            "the columns under every factored combination (no superposition of factored results); %s. Member "
            "effective length K = 1 is valid only under the Direct Analysis Method of AISC 360-22 &sect;C1/C2 "
            "(reduced stiffness &sect;C2.3, notional loads &sect;C2.2b); where the method is not DAM, the effective "
            "length or first-order methods of Appendix 7 govern. The story stability coefficient &theta; "
            "(ASCE 7-22 &sect;12.8.7) is reported in Chapter 7.</p>" % mtxt)


def _analysis_record_html(cfg, pkg):
    """Chapter 5 summary of how the member demands were produced (demand_analysis), as labelled values -- never the
    raw record: method and DAM parameters, number of combinations and solves, floor load distribution, live load
    reduction, Omega0 basis and any effect NOT evaluated by the analysis."""
    da = (pkg or {}).get("demand_analysis") if isinstance(pkg, dict) else None
    if not isinstance(da, dict) or not da:
        return ""
    rows = []
    if da.get("analysis"):
        rows.append(["Stability / analysis method", _n(da.get("analysis"))])
    dm = da.get("dam") if isinstance(da.get("dam"), dict) else {}
    if dm:
        bits = []
        if dm.get("stiffness_factor") is not None:
            bits.append("stiffness reduction %s EI, EA (C2.3(a))" % _n(dm.get("stiffness_factor")))
        if dm.get("tau_b"):
            bits.append("&tau;<sub>b</sub> = 1.0 with the additional notional load (C2.3(c))" if "notional" in
                        str(dm.get("tau_b")).lower() else "&tau;<sub>b</sub>: %s" % _n(dm.get("tau_b")))
        if dm.get("notional_gravity_only") is not None:
            bits.append("notional load %s&middot;Y<sub>i</sub> in gravity-only combinations" % _n(dm.get("notional_gravity_only")))
        if dm.get("notional_lateral_cases") is not None:
            bits.append("%s&middot;Y<sub>i</sub> added in the lateral combinations" % _n(dm.get("notional_lateral_cases")))
        if dm.get("drift_ratio_2nd_1st") is not None:
            bits.append("second- to first-order drift ratio %s" % _n(dm.get("drift_ratio_2nd_1st")))
        rows.append(["Direct Analysis Method parameters", "; ".join(bits) if bits else "&mdash;"])
        hi = dm.get("alphaPr_over_Pns_gt_0.5")
        if isinstance(hi, list) and hi:
            secs = sorted({str(x[2]) for x in hi if isinstance(x, (list, tuple)) and len(x) >= 3})
            mx = max((float(x[0]) for x in hi if isinstance(x, (list, tuple)) and x), default=None)
            rows.append(["Members with &alpha;P<sub>r</sub>/P<sub>ns</sub> &gt; 0.5 (C2.3(b))",
                         "%s (max %s)" % (", ".join(_esc(s_) for s_ in secs), _n(mx))])
    if da.get("cases") is not None:
        rows.append(["Load combinations analysed", "%s (%s second-order solves)" % (_n(da.get("cases")), _n(da.get("solves")))
                     if da.get("solves") else _n(da.get("cases"))])
    fm = da.get("floor_load_modes")
    if isinstance(fm, dict) and fm:
        rows.append(["Floor load distribution", "; ".join("%s: %s bays" % (_esc(k).replace("/declared", " (declared)"), _n(v))
                                                          for k, v in fm.items())])
    llr = da.get("live_load_reduction")
    if isinstance(llr, dict):
        rows.append(["Live load reduction (&sect;4.7)", ("applied: L/L<sub>o</sub> %s&ndash;%s, K<sub>LL</sub> %s (columns) / %s "
                     "(beams)%s" % (_n(llr.get("RF_min")), _n(llr.get("RF_max")), _n(llr.get("KLL_col")),
                                    _n(llr.get("KLL_beam")), ("; %s psf not reduced" % _n(llr.get("unreduced_psf")))
                                    if llr.get("unreduced_psf") else "")) if llr.get("applied") else "not applied (unreduced L<sub>o</sub>)"])
    if da.get("omega0_columns"):
        rows.append(["Overstrength (&Omega;<sub>0</sub>) column cases", _n(da.get("omega0_columns"))])
    gad = da.get("gravity_audit_dead_kip")
    if isinstance(gad, dict) and isinstance(gad.get("levels"), dict):
        try:
            tot = [sum(float(v[i]) for v in gad["levels"].values()) for i in (0, 1)]
            rows.append(["Dead-load audit: static model vs the floor and roof dead load of W (partitions, member self-weight and lumped seismic weights are listed separately)", "%s kip vs %s kip" % (_n(tot[0]), _n(tot[1]))])
        except Exception:
            pass
    out = ""
    if rows:
        out = "<h3>How the member demands were produced</h3>" + _table(["Item", "Value"], rows)
    ne = [x for x in (da.get("not_evaluated") or []) if x]
    if ne:
        out += ("<p class='note'><b>Not evaluated by the analysis</b> (addressed in the member / connection design "
                "records):</p><ul>" + "".join("<li>%s</li>" % _n(x) for x in ne[:12]) + "</ul>")
    nts = [x for x in (da.get("notes") or []) + (da.get("warnings") or []) if x]
    if nts:
        out += "<p class='cnote'>Analysis notes:</p><ul>" + "".join("<li>%s</li>" % _n(x) for x in nts[:12]) + "</ul>"
    return out


# calc-package keys rendered by their own section, summarised elsewhere, or internal to the harness (never dumped)
_KNOWN_PKG_KEYS = {"members", "connections", "connection_demands", "capacity_design", "scwb", "detailing",
                   "framework_screen", "building", "code", "note", "notes", "meta", "name", "composite_design",
                   "demand_analysis", "ddm_analysis", "gate_waivers", "pipeline_seed", "corpus_defects",
                   "design_agent", "floor_vibration", "analysis_results", "activity", "rag", "agent"}
_HARNESS_KEYS = {"corpus_defects", "gate_waivers", "pipeline_seed", "framework_screen", "demand_analysis", "note"}

_COMPOSITE_SYN = {   # tolerant key synonyms seen across agent packages (candidates + gold)
    "pct":   ("partial_composite_ratio", "partial_composite_percent", "partial_composite_pct",
              "degree_of_composite", "composite_ratio", "percent_composite", "composite_pct"),
    "Qn":    ("stud_Qn_kip", "Qn_kip", "Qn", "Qn_per_stud_kip"),
    "studs": ("stud_count_total", "n_studs", "n_studs_total", "studs_total", "studs_per_beam", "n_per_half_span",
              "stud_schedule", "studs"),
    "camber":("camber_in", "camber"),
    "ILB":   ("I_LB_in4", "ILB_in4", "I_LB", "lower_bound_I_in4", "I_lb_in4"),
    "wet":   ("wet_stage_phiMn_kipft", "wet_stage_phiMn_kipin", "Mu_wet_kipin",
              "wet_dead_deflection_in", "wet_deflection_in"),
}


def _record_html(v, depth=0, path=""):
    """A calc-package record as clean labelled tables: human key labels with units, numbers through _n, no
    None / Python reprs, harness keys dropped, lists of uniform dicts as one table. Small nested records are
    written inline ("X = 1.36; Y = 1.259"); larger ones follow as their own titled tables (never a table
    inside a table cell)."""
    if isinstance(v, dict):
        rows = []; subs = []
        for k, x in v.items():
            ks = str(k)
            if ks.lower() in _DROP_KEYS or ks.startswith("_") or ks.lower().startswith("cfg"):
                continue
            if x is None or (isinstance(x, str) and x.strip().lower() in _EMPTY) or x in ([], {}):
                continue
            lab, unit, sc = _human_key(ks)
            if isinstance(x, dict):
                flat = all(not isinstance(y, (dict, list)) for y in x.values())
                if (flat and len(x) <= 6) or depth >= 3:
                    val = _fmt(x)
                else:
                    subs.append((lab, x)); continue
            elif isinstance(x, list):
                if any(isinstance(y, (dict, list)) for y in x) and depth < 3:
                    subs.append((lab, x)); continue
                val = _record_html(x, depth + 1)
            elif isinstance(x, bool):
                val = _n(x)
            elif isinstance(x, (int, float)):
                val = _n(float(x) * sc if sc != 1.0 else x) + ((" " + unit) if unit else "")
            else:
                val = _n(x)
            rows.append([lab, val])
        out = _table(["Item", "Value"], rows) if rows else ""
        for lab, x in subs:
            p2 = (path + " &mdash; " + lab) if path else lab
            body = _record_html(x, depth + 1, p2)
            if body and body != "&mdash;":
                out += "<p class='cnote'><b>%s</b></p>%s" % (p2[:1].upper() + p2[1:], body)
        return out or "&mdash;"
    if isinstance(v, list):
        if not v:
            return "&mdash;"
        if all(isinstance(x, dict) for x in v):
            keys = []
            for x in v:
                for k in x:
                    if k not in keys and str(k).lower() not in _DROP_KEYS and not str(k).startswith("_"):
                        keys.append(k)
            if len(keys) <= 9 and all(not isinstance(x.get(k), (dict, list)) for x in v for k in keys):
                hdr = []
                for k in keys:
                    lab, unit, _sc = _human_key(k)
                    hdr.append(lab + ((" (%s)" % unit) if unit else ""))
                body = []
                for x in v[:40]:
                    r = []
                    for k in keys:
                        val = x.get(k)
                        _l, _u, sc = _human_key(k)
                        r.append(_n(float(val) * sc) if isinstance(val, (int, float)) and not isinstance(val, bool)
                                 and sc != 1.0 else _n(val))
                    body.append(r)
                more = ("<p class='cnote'>%d further rows retained in the calc package.</p>" % (len(v) - 40)) if len(v) > 40 else ""
                return _table(hdr, body) + more
            return "".join(_record_html(x, depth + 1, path) for x in v[:20])
        if all(isinstance(x, str) for x in v) and any(len(x) > 60 for x in v):
            return "<ul>" + "".join("<li>%s</li>" % _n(x) for x in v[:30]) + "</ul>"
        if any(isinstance(x, (dict, list)) for x in v):
            return "".join(_record_html(x, depth + 1, path) for x in v[:20])
        return _n(v)
    return _n(v)


def _extra_blocks_section(pkg):
    """Supplementary design records: every top-level calc-package block the report does not render elsewhere
    (serviceability, governing hazard, delegated design, loads hand check, scope statements, ...), as clean
    labelled tables. Harness / pipeline blocks are never dumped (they are summarised in their chapters)."""
    if not isinstance(pkg, dict):
        return ""
    parts = []
    for k, v in pkg.items():
        if k in _KNOWN_PKG_KEYS or k in _HARNESS_KEYS or str(k).startswith("_") or v in (None, "", [], {}):
            continue
        if not isinstance(v, (dict, list, str, int, float, bool)):
            continue
        title = _human_key(str(k))[0]
        title = title[:1].upper() + title[1:]
        parts.append("<h4>%s</h4>" % title)
        try:
            blob = json.dumps(v)
        except Exception:
            blob = ""
        if len(blob) > 20000 and isinstance(v, dict):
            v = {kk: vv for kk, vv in v.items() if not isinstance(vv, (dict, list))}
            parts.append("<p class='cnote'>Summary fields shown; the full record is retained in the calc package.</p>")
        parts.append(_record_html(v) if isinstance(v, (dict, list)) else "<p>%s</p>" % _n(v))
    if not parts:
        return ""
    return ("<h3>Supplementary design records</h3>"
            "<p>Additional design records of this package, beyond the member, connection and capacity-design "
            "tables.</p>" + "".join(parts))


def _composite_section(pkg):
    """Chapter-6 'Composite floor design (AISC 360 Ch. I)' section. Two tolerant sources:
    (a) the standard top-level `composite_design` block, and (b) composite values embedded in member capacity
    dicts (stud counts / partial ratio / camber / I_LB / wet stage) -- one row per member. The Ch. I note is
    printed only when composite values are actually recorded and composite action is relied upon (19b)."""
    if not isinstance(pkg, dict):
        return ""
    parts = []
    cd = pkg.get("composite_design")
    rows = []
    for m in pkg.get("members", []) or []:
        srcs = [d for d in (m.get("capacity"), m.get("composite"), m.get("inputs")) if isinstance(d, dict)]
        found = {}
        for col, keys in _COMPOSITE_SYN.items():
            for kk in keys:
                v = next((d[kk] for d in srcs if kk in d and d[kk] not in (None, "")), None)
                if v is not None:
                    found[col] = v; break
        _cr = m.get("composite") if isinstance(m.get("composite"), dict) else {}
        if "pct" not in found and _cr.get("pct") not in (None, ""):
            found["pct"] = _cr["pct"]
        if isinstance(found.get("pct"), (int, float)) and not isinstance(found.get("pct"), bool):
            _pv = float(found["pct"])
            found["pct"] = "%.0f%%" % (_pv * 100.0 if _pv <= 1.0 else _pv)
        # a row needs at least two of the composite values -- a lone I_LB or camber is left to the record below,
        # so the table never prints a line of dashes next to a record that has the studs
        if len(found) >= 2:
            rows.append([_esc(m.get("id", "")), _esc((m.get("inputs") or {}).get("section", ""))]
                        + [(_n(found[c]) if c in found else "&mdash;") for c in ("pct", "Qn", "studs", "camber", "ILB", "wet")])
    if not cd and not rows:
        return ""
    scope = str((cd or {}).get("scope") or pkg.get("composite_scope") or "") if isinstance(cd, dict) else ""
    not_relied = bool(re.search(r"not relied|bare[- ]steel|not used for strength|neglected", scope, re.I))
    parts.append("<h3>Composite floor design (AISC 360-22 Ch. I)</h3>")
    if rows:
        parts.append("<p>Per-member composite design values recorded in the calc package (partial-composite ratio, stud "
                     "strength / schedule, camber, lower-bound moment of inertia, unshored wet-concrete stage):</p>")
        parts.append(_table(["Member", "Section", "Partial composite", "Q<sub>n</sub> (kip)", "Studs",
                             "Camber (in.)", "I<sub>LB</sub> (in.<sup>4</sup>)", "Wet stage"], rows))
    if cd:
        parts.append("<h4>Composite design record</h4>")
        parts.append(_record_html(cd))
    numeric = bool(rows) or (isinstance(cd, dict) and any(
        isinstance(v, (int, float)) and not isinstance(v, bool) and v
        for k, v in cd.items() if re.search(r"stud|qn|composite|i_?lb|camber", str(k), re.I)))
    if not_relied:
        parts.append("<p class='cnote'>Composite action is not relied upon for strength or deflection (scope above); the "
                     "beams are designed as bare steel.</p>")
    elif numeric:
        parts.append("<p class='cnote'>Stud strengths per AISC 360-22 &sect;I8.2a (with the deck-rib position factors); "
                     "composite flexure per &sect;I3.2a; service deflections on the lower-bound moment of inertia; camber "
                     "and the unshored construction stage as recorded above.</p>")
    return "".join(parts)


def _ddm_summary(pkg):
    """Supplementary system check by advanced analysis (Direct Design Method, AISC 360-22 Appendix 1): method,
    verdict and the governing combinations -- never the raw record (19a). Combinations without a system
    resistance factor (seismic: no phi_s is established) are information only, never printed as PASS."""
    d = (pkg or {}).get("ddm_analysis") if isinstance(pkg, dict) else None
    if not isinstance(d, dict) or not d:
        return ""
    combos = [c for c in (d.get("combinations") or []) if isinstance(c, dict)]

    def _status(c):
        chk = str(c.get("check", "")).strip().upper()
        if "FAIL" in chk:
            return "fail"
        if c.get("phi_s") in (None, "", 0) or chk in ("", "N/A", "NA", "NONE"):
            return "info"
        return "pass"

    def _prod(c):
        try:
            return float(c.get("phi_s")) * float(c.get("lambda_u"))
        except (TypeError, ValueError):
            return None
    fails = [c for c in combos if _status(c) == "fail"]
    checked = [c for c in combos if _status(c) != "info"]
    info = [c for c in combos if _status(c) == "info"]
    verdict = str(d.get("verdict") or "").upper() or "not stated"
    order = sorted(checked, key=lambda c: (_status(c) != "fail", _prod(c) if _prod(c) is not None else 9e9))
    order += sorted(info, key=lambda c: float(c.get("lambda_u") or 9e9))
    rows = []
    for c in order[:16]:
        st = _status(c); prod = _prod(c)
        rows.append([_combo_label(c.get("label", "")), _n(c.get("kind")), _n(c.get("lambda_u")),
                     _n(c.get("phi_s")) if st != "info" else "&mdash;",
                     (_n(prod, dc=True) if prod < 50 else "&gt; 50") if (prod is not None and st != "info") else "&mdash;",
                     "<b class='ng'>FAIL</b>" if st == "fail" else ("PASS" if st == "pass" else "information only"),
                     _n(c.get("mechanism"))])
    meth = str(d.get("method") or "Direct Design Method (system-based design by advanced analysis)")
    meth = re.sub(r"\s*--\s*steltic_ddm.*$", "", meth)
    cnt = ((" &mdash; %d of %d checked combinations below 1.0" % (len(fails), len(checked))) if fails else
           (" (%d combination%s checked)" % (len(checked), "" if len(checked) == 1 else "s") if checked else ""))
    if info:
        cnt += ("; %d combination%s (%s) without an established system resistance factor &phi;<sub>s</sub> "
                "%s information only" % (len(info), "s" if len(info) > 1 else "",
                                         ", ".join(sorted({_n(c.get("kind")) for c in info})),
                                         "are" if len(info) > 1 else "is"))
    h = ["<h3>System check by advanced analysis (Direct Design Method)</h3>",
         "<p>%s, a supplementary system-level check (AISC 360-22 Appendix 1 type analysis): each combination is "
         "scaled to collapse in a geometrically and materially nonlinear model; the check is &phi;<sub>s</sub>&lambda;<sub>u</sub> "
         "&ge; 1.0 with the system resistance factor &phi;<sub>s</sub>. <b>Verdict: %s</b>%s.</p>"
         % (_esc(meth), "<span class='ng'>FAIL</span>" if verdict == "FAIL" else _esc(verdict), cnt)]
    if rows:
        h.append(_table(["Combination", "Type", "&lambda;<sub>u</sub>", "&phi;<sub>s</sub>",
                         "&phi;<sub>s</sub>&lambda;<sub>u</sub>", "Check", "Mechanism at the peak"], rows))
        if len(combos) > 16:
            h.append("<p class='cnote'>%d further combinations (none below 1.0 unless listed above) are retained in the "
                     "calc package.</p>" % (len(combos) - 16))
    cav = d.get("caveats")
    if isinstance(cav, list) and cav:
        h.append("<p class='cnote'>Caveats:</p><ul>" + "".join("<li>%s</li>" % _n(x) for x in cav[:8]) + "</ul>")
    if verdict == "FAIL":
        h.append("<p class='note'>The supplementary system check does not pass for the combinations marked FAIL; it is "
                 "listed as an open item in Chapter 13.</p>")
    return "".join(h)


def _member_dc_summary(pkg):
    C = _consistency_mod()
    ms = [m for m in (pkg.get("members") or []) if isinstance(m, dict)]
    have = [(C._dc_num(m.get("DC")) if C else m.get("DC"), m) for m in ms]
    have = [(d, m) for d, m in have if isinstance(d, (int, float))]
    if have:
        dc, gov = max(have, key=lambda t: t[0])
        return ("<p>Governing member: <b>%s</b> at D/C = <b>%s</b> (%s; combination %s).</p>"
                % (_esc(gov.get('id')), _n(dc, dc=True), _n(gov.get('limit_state')),
                   _combo_label((gov.get('inputs', {}) or {}).get('governing_combo'))))
    return ("<p class='note'>Member capacities and D/C ratios are not yet recorded in the calc package for this "
            "building; the analysis demand envelopes are listed in Appendix A.</p>")


def _ch6_notes(cfg):
    comp = " Composite floor members (Ch. I) are designed where composite action is used." if cfg.get("composite") else ""
    return ("<p class='cnote'>Limit states checked per member as applicable: tension (D2/D3), compression (E3/E4/E7), "
            "flexure (F2-F8 with the correct compact / noncompact / slender limit state, L<sub>b</sub>, C<sub>b</sub>), "
            "shear (G2) and combined axial+flexure interaction (H1)." + comp + " Concentrated-load limit states "
            "(web local yielding / crippling, J10) and stiffeners are checked at point loads and supports where "
            "applicable. The EOR should spot-check 2-3 governing members by hand against Appendix A.</p>")


def _drift_from_forces(cfg, Fdict, direction):
    info = E.build(cfg, "Linear"); NF = info["NF"]; di = 0 if direction == "X" else 1
    ops.timeSeries("Linear", 1); ops.pattern("Plain", 1, 1)
    for k in range(1, NF+1):
        f = [0.0]*6; f[di] = Fdict[k]; ops.load(E.mtag(k), *f)
    ops.constraints("Transformation"); ops.numberer("RCM"); ops.system("UmfPack")
    ops.test("NormDispIncr", 1e-8, 100); ops.algorithm("Linear")
    ops.integrator("LoadControl", 1.0); ops.analysis("Static"); ops.analyze(1)
    drift = []; prev = 0.0
    for k in range(1, NF+1):
        d = ops.nodeDisp(E.mtag(k), di+1); drift.append((d-prev)/cfg["heights"][k-1]); prev = d
    return drift

def _drift_section(cfg, runs):
    """Chapter 8 seismic design drift, both directions, on the same basis as the engine's drift gate
    (engine3d.static_lateral) and as the theta table: Delta = Cd * delta_xe / Ie (Eq. 12.8-16), delta_xe at
    the 12.8.6.5 location, x the AISC 358-22 5.7 RBS factor, declared drift-exempt stories skipped."""
    NF = len(cfg["heights"]); ex = runs["exempt"]
    h = ["<h3>Seismic design drift</h3>"]
    for d in ("X", "Y"):
        r = runs[d]
        h.append("<p><b>%s:</b> C<sub>d</sub> = %g, I<sub>e</sub> = %g; drift at the %s; allowable %.2f%% of h<sub>sx</sub>%s%s.</p>"
                 % (d, r["Cd"], r["Ie"], r["basis"], r["limit"] * 100,
                    (" = &Delta;<sub>a</sub>/&rho; (moment-frame-only SFRS in SDC D-F, &sect;12.12.1.1)" if r["limit_rho"] else " (Table 12.12-1)"),
                    ("; elastic drifts &times; %.3f for the reduced beam sections (AISC 358-22 &sect;5.7 Step 1: %s)"
                     % (r["rbs"][0], _esc(r["rbs"][1])) if r["rbs"][0] > 1.0 else "")))
    h.append(_drift_relief_note(cfg))
    rows = []
    for k in range(1, NF + 1):
        if k in ex:
            rows.append([k, "%.3f" % (runs["X"]["dxe"][k-1]*100), "%.3f" % (runs["X"]["design"][k-1]*100),
                         "%.3f" % (runs["Y"]["dxe"][k-1]*100), "%.3f" % (runs["Y"]["design"][k-1]*100),
                         "exempt: " + _esc(str((cfg.get("drift_exempt_stories") or {}).get(k, ""))[:60])])
            continue
        ok = all(runs[d]["design"][k-1] <= runs[d]["limit"] for d in ("X", "Y"))
        rows.append([k, "%.3f" % (runs["X"]["dxe"][k-1]*100), "%.3f" % (runs["X"]["design"][k-1]*100),
                     "%.3f" % (runs["Y"]["dxe"][k-1]*100), "%.3f" % (runs["Y"]["design"][k-1]*100), "OK" if ok else "NG"])
    h.append(_table(["Story", "&delta;<sub>xe</sub>/h X %", "&Delta;/h X %", "&delta;<sub>xe</sub>/h Y %", "&Delta;/h Y %",
                     "&le; limit (X %.2f%% / Y %.2f%%)" % (runs["X"]["limit"]*100, runs["Y"]["limit"]*100)], rows))
    _db = _demand_basis(cfg)
    if _db == "MRSA":
        h.append("<p class='cnote'>Drift analysis: modal response spectrum analysis (&sect;12.9.1) of the P-&Delta; model "
                 "with the unfactored gravity load, drifts per &sect;12.9.1.4.2 (the same drifts enter the Chapter 13 "
                 "scorecard).</p>")
    else:
        h.append("<p class='cnote'>Drift analysis: ELF story forces on the P-&Delta; model with the unfactored gravity "
                 "load%s (the same drifts enter the Chapter 13 scorecard). The strength-level ELF forces are used, including "
                 "the C<sub>s</sub> minimum (Eq. 12.8-6) and the C<sub>u</sub>T<sub>a</sub> period cap &mdash; conservative: "
                 "the &sect;12.8.6.1 Exception and &sect;12.8.6.2 permit dropping both for drift.</p>"
                 % ("; enveloped story by story with the &sect;12.9.1.4.2 MRSA drifts" if _db == "envelope" else ""))
    return "".join(h)


def _demand_basis(cfg):
    """The seismic demand basis the engine uses (engine3d.seismic_design_forces): 'ELF', 'MRSA' or 'envelope'."""
    rs_on = "RS" in [str(a).upper() for a in (cfg.get("analyses") or [])]
    b = str(cfg.get("seismic_demand_basis") or ("envelope" if rs_on else "ELF"))
    return b if b in ("ELF", "MRSA", "envelope") else "ELF"


_CC_MAP = {10: "CC.2-2", 25: "CC.2-3", 50: "CC.2-4", 100: "CC.2-5"}


def _wind_story_drifts(cfg_w):
    return (_drift_from_forces(cfg_w, E.wind_forces(cfg_w, "X"), "X"),
            _drift_from_forces(cfg_w, E.wind_forces(cfg_w, "Y"), "Y"))


def _wind_drift_section(cfg):
    """Wind drift. With cfg['wind_service'] = {'V': mph, 'MRI': yr, 'basis': ..} the serviceability check uses
    W_a at the service wind speed (ASCE 7-22 Appendix CC, Eq. CC.2-3 D + 0.5L + W_a; Fig. CC.2-2 for a 10-year
    MRI) against cfg['wind_drift_limit'] and the strength-level wind drifts are shown for information only;
    without it the strength-level wind is used (conservative for a serviceability check)."""
    if not cfg.get("wind"):
        return "<p class='cnote'>Wind drift not evaluated (no wind load defined).</p>"
    NF = len(cfg["heights"]); lim = float(cfg.get("wind_drift_limit", 1.0/400.0) or 1.0/400.0)
    ws = cfg.get("wind_service") if isinstance(cfg.get("wind_service"), dict) else None
    try:
        dX, dY = _wind_story_drifts(cfg)
    except Exception as ex:
        return f"<p class='note'>[wind drift run failed: {_esc(ex)}]</p>"
    limtxt = "h<sub>sx</sub>/%d (%.2f%%)" % (int(round(1 / lim)), lim * 100)
    ex = _drift_exempt(cfg)                 # declared drift-exempt stories (split-level steps, phantom stories)
    _exr = lambda k: "exempt: " + _esc(str((cfg.get("drift_exempt_stories") or {}).get(k, ""))[:60])
    def _wrow(k, a, b):                       # interstory drift magnitudes (a reversed story drift is still a drift)
        if k in ex:
            return [k, "%.3f" % (abs(a) * 100), "%.3f" % (abs(b) * 100), _exr(k)]
        return [k, "%.3f" % (abs(a) * 100), "%.3f" % (abs(b) * 100),
                "OK" if max(abs(a), abs(b)) <= lim else "<b class='ng'>NG</b>"]
    def _worst(a, b):
        return max([max(abs(a[k - 1]), abs(b[k - 1])) for k in range(1, NF + 1) if k not in ex] or [0.0])
    # cfg['wind_drift_governing'] = {'drift': ratio (or 'drift_pct'), 'where': text}: the governing wind drift the
    # engineer computed outside the centre-of-mass table (e.g. Fig. 27.3-8 Case 2 at the plan edge), at the same
    # wind level as the table (service wind when cfg['wind_service'] is given)
    _g = cfg.get("wind_drift_governing") if isinstance(cfg.get("wind_drift_governing"), dict) else {}
    try:
        _gv = abs(float(_g["drift"])) if _g.get("drift") is not None else (
            abs(float(_g["drift_pct"])) / 100.0 if _g.get("drift_pct") is not None else None)
    except (TypeError, ValueError):
        _gv = None
    _gw = str(_g.get("where") or _g.get("basis") or "engineer's evaluation")

    def _headline(worst_cm, label):
        """(governing drift, its description, extra sentence) -- the larger of the table and the declared value."""
        if _gv is not None and _gv > worst_cm:
            return _gv, _gw, (" The story table is at the centre of mass under the direct wind (%.3f%% worst); the "
                              "governing value is %s." % (worst_cm * 100, _esc(_gw)))
        return worst_cm, label, ((" A separately computed %s gives %.3f%%, which does not govern." % (_esc(_gw), _gv * 100))
                                 if _gv is not None else "")
    h = []
    if ws and ws.get("V"):
        try:
            Vs = float(ws["V"])
            cfg_s = dict(cfg); cfg_s["wind"] = dict(cfg["wind"], V=Vs)
            for k in ("story_forces",):                      # supplied strength-level story forces are not service
                cfg_s["wind"].pop(k, None)                    # forces: the Ch. 27 calculation is repeated at V_s
            for k in ("wind_forces_override", "wind_by_level"):
                cfg_s.pop(k, None)
            sX, sY = _wind_story_drifts(cfg_s)
        except Exception as ex:
            return f"<p class='note'>[service wind drift run failed: {_esc(ex)}]</p>"
        try:
            mri = int(round(float(ws.get("MRI", 10))))
        except (TypeError, ValueError):
            mri = 10
        fig = _CC_MAP.get(mri)
        basis = str(ws.get("basis") or "")
        btxt = (" Basis: %s." % _n(basis)) if basis and "CC.2-1" not in basis else ""
        rows = [_wrow(k, sX[k-1], sY[k-1]) for k in range(1, NF+1)]
        worst, _wl, _wx = _headline(_worst(sX, sY), "centre of mass")
        h.append("<h3>Service wind drift, W<sub>a</sub> at V = %s mph (%d-year MRI%s), D + 0.5L + W<sub>a</sub> (Eq. CC.2-3)</h3>"
                 % (_n(Vs), mri, (", ASCE 7-22 Fig. %s" % fig) if fig else ""))
        _wcf = cfg.get("wind") if isinstance(cfg.get("wind"), dict) else {}
        _gtx = (("the stated gust-effect factor G<sub>f</sub> = %s kept" % _n(float(_wcf["Gf"])))
                if _wcf.get("Gf") is not None else
                "gust-effect factor as in Chapter 3, recomputed at the service speed where the building is flexible")
        h.append("<p>Interstory drift under the serviceability wind W<sub>a</sub> (ASCE 7-22 Appendix CC.2.2: same MWFRS "
                 "procedure as Chapter 3 at the service wind speed, %s) against the project limit "
                 "%s; Appendix CC cites limits of h/600 to h/400 in common use. First-order analysis: the D + 0.5L part of "
                 "Eq. CC.2-3 does not change the first-order drift.%s</p>" % (_gtx, limtxt, btxt))
        h.append(_table(["Story", "drift X %", "drift Y %", "&le; %.2f%%" % (lim * 100)], rows))
        h.append("<p><b>Worst service wind drift %.3f%%%s &mdash; %s.</b>%s</p>"
                 % (worst * 100, (" (%s)" % _esc(_wl)) if _wl != "centre of mass" else "",
                    "OK" if worst <= lim else "<span class='ng'>exceeds the project limit</span>", _wx))
        _QA["wind_drift"] = (worst, lim, "service wind, %d-year MRI" % mri)
        irows = [[k, "%.3f" % (abs(dX[k-1])*100), "%.3f" % (abs(dY[k-1])*100)] for k in range(1, NF+1)]
        h.append("<h4>Drift under the design (strength-level) wind &mdash; information only</h4>"
                 + _table(["Story", "drift X %", "drift Y %"], irows))
        return "".join(h)
    rows = [_wrow(k, dX[k-1], dY[k-1]) for k in range(1, NF+1)]
    _wv, _wl, _wx = _headline(_worst(dX, dY), "centre of mass")
    _QA["wind_drift"] = (_wv, lim, "design wind")
    _tail = ("<p><b>Worst wind drift %.3f%%%s &mdash; %s.</b>%s</p>"
             % (_wv * 100, (" (%s)" % _esc(_wl)) if _wl != "centre of mass" else "",
                "OK" if _wv <= lim else "<span class='ng'>exceeds the project limit</span>", _wx)) if _gv is not None else ""
    return ("<h3>Wind drift</h3>"
            "<p>Interstory drift under the design (strength-level) MWFRS wind &mdash; conservative for a serviceability "
            "check &mdash; against the project limit %s. ASCE 7-22 sets no mandatory wind drift limit; Appendix CC "
            "(Eq. CC.2-3, D + 0.5L + W<sub>a</sub>) suggests a serviceability wind W<sub>a</sub> of a shorter mean "
            "recurrence interval (e.g. 10-year, Fig. CC.2-2) with limits of h/600 to h/400.</p>" % limtxt
            + _table(["Story", "drift X %", "drift Y %", "&le; %.2f%%" % (lim * 100)], rows) + _tail)


def _capdesign_html(cap):
    """Render a capacity_design / scwb record as clean labelled tables (never raw JSON)."""
    return _record_html(cap)


_DETAIL_CHECKS = {
    "SMF": [("Strong-column/weak-beam", "&Sigma;M*<sub>pc</sub>/&Sigma;M*<sub>pb</sub> &gt; 1.0 (E3.4a)"),
            ("Beam &amp; column width-to-thickness", "highly ductile (D1.1, Table D1.1)"),
            ("Panel-zone shear &amp; doublers", "E3.6e"), ("Continuity plates", "E3.6f"),
            ("Beam stability bracing", "E3.4b"), ("Protected zones", "E3.5c (RBS / hinge regions)"),
            ("Prequalified connection", "E3.6c &mdash; within AISC 358 limits"),
            ("Demand-critical welds", "E3.6a / A3.4")],
    "IMF": [("Beam &amp; column width-to-thickness", "moderately ductile (D1.1)"),
            ("Panel zone / continuity plates", "E2.6e / E2.6f"), ("Beam stability bracing", "E2.4a"),
            ("Protected zones", "E2.5c"), ("Prequalified connection", "E2.6c &mdash; within AISC 358 limits"),
            ("Demand-critical welds", "E2.6a / A3.4"),
            ("Strong-column/weak-beam", "not required for IMF (E2 has no moment-ratio provision)")],
    "OMF": [("Beam-to-column connections", "E1.6 (FR: expected strength of the beam or the &Omega;<sub>0</sub> case)"),
            ("Demand-critical welds", "E1.6a")],
    "STMF": [("Special segment", "E4.3a / E4.4a; expected vertical shear strength E4.5c"),
             ("Width-to-thickness of special-segment members", "E4.5d"),
             ("Non-special segment members", "capacity-limited by the special segment (E4.3b)"),
             ("Stability bracing of trusses", "E4.4b"), ("Protected zones", "E4.5f")],
    "SCBF": [("Brace width-to-thickness", "highly ductile (D1.1)"), ("Brace slenderness", "L<sub>c</sub>/r &le; 200 (F2.5b)"),
             ("Columns, beams, connections", "capacity-limited E<sub>cl</sub>, analyses F2.3(a)/(b)"),
             ("Brace connections", "tension R<sub>y</sub>F<sub>y</sub>A<sub>g</sub>/&alpha;<sub>s</sub>; compression = expected brace "
              "compression (F2.3: min(R<sub>y</sub>F<sub>y</sub>A<sub>g</sub>, F<sub>ne</sub>A<sub>g</sub>/0.877))/&alpha;<sub>s</sub> (F2.6c)"),
             ("Protected zones / gusset hinge zone", "F2.5c, F2.6c(3)"), ("Demand-critical welds", "F2.6a")],
    "OCBF": [("Brace slenderness", "F1.5b"), ("Brace connections", "expected R<sub>y</sub>F<sub>y</sub>A<sub>g</sub> or the "
             "&Omega;<sub>0</sub> load effect (F1.6a)"), ("V / inverted-V braced frames", "F1.4a"), ("Beams", "F1.5c")],
    "BRBF": [("Brace adjusted strengths", "&beta;&omega;R<sub>y</sub>P<sub>ysc</sub>, adjustment factors from testing (F4.2a/F4.2b)"),
             ("Columns, beams, connections", "capacity-limited with the adjusted brace strengths (F4.3)"),
             ("Brace qualification", "conformance demonstration by testing (F4.5b(3))"),
             ("Brace connections / gussets", "F4.6c"), ("Demand-critical welds", "F4.6a")],
    "EBF":  [("Link shear strength / length", "F3.5b(2), F3.5b(3)"), ("Link rotation angle", "F3.4a"),
             ("Link stiffeners and bracing", "F3.5b(4), F3.4b"),
             ("Brace, beam outside link, columns", "capacity-limited by the adjusted link shear strength (F3.3)"),
             ("Protected zones", "F3.5c"), ("Link-to-column connections", "F3.6e")],
    "SPSW": [("Web plate", "F5.5b"), ("HBE / VBE", "capacity-limited by web yielding (F5.3, F5.5c)"),
             ("Boundary-element stiffness", "F5.4a"), ("HBE-to-VBE moment ratio", "F5.4b"),
             ("HBE-to-VBE connections", "F5.6b"), ("Protected zones", "F5.5d")],
}


def _uplift_summary(pkg):
    """One sentence on the net-uplift load path from the base-reaction envelope (19r)."""
    br = _base_reactions(pkg)
    if not br:
        return ""
    up = max(br.items(), key=lambda t: t[1].get("uplift", 0.0) or 0.0)
    if (up[1].get("uplift") or 0.0) <= 0.05:
        return ("<p>Net uplift: no column base is in net tension in any combination (base-reaction envelope, "
                "Chapter 11).</p>")
    n_up = sum(1 for r in br.values() if (r.get("uplift") or 0.0) > 0.05)
    return ("<p>Net uplift load path: %d column base%s %s in net tension; the largest net uplift is <b>%s kip</b> at "
            "base %s (%s). It is resisted by the anchor rods of the base-plate design (Chapter 10) and delivered to the "
            "foundations (Chapter 11).</p>" % (n_up, "s" if n_up > 1 else "", "are" if n_up > 1 else "is",
                                               _n(up[1].get("uplift")), _grid_txt(up[0], up[1]), _combo_label(up[1].get("uplift_combo"))))


_NEG_341 = re.compile(r"not appl|does not apply|do not apply|n/a|not required|\bno\b|without", re.I)


def _no_341_claims(obj):
    """For a building where AISC 341 does not apply: a copy of a design record without positive AISC 341
    references (a parenthetical citing AISC 341 is dropped; statements that it does not apply are kept)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            vv = _no_341_claims(v)
            if vv not in (None, "", {}, []):
                out[k] = vv
        return out
    if isinstance(obj, list):
        return [x for x in (_no_341_claims(v) for v in obj) if x not in (None, "", {}, [])]
    if isinstance(obj, str) and "341" in obj and not _NEG_341.search(obj):
        s = re.sub(r"\s*\([^()]*341[^()]*\)", "", obj)
        return s if "341" not in s else ""
    return obj


def _aisc341_detailing(cfg, pkg, sf=None, ctx=None):
    s = cfg["seis"]; sf = sf or _sfrs(cfg); ctx = ctx or {}
    det = (pkg or {}).get("detailing")
    cap = (pkg or {}).get("capacity_design") or (pkg or {}).get("scwb")
    a341 = ctx.get("a341")
    if a341 is None:
        try:
            a341 = float(s.get("R") or 0) > 3.0
        except Exception:
            a341 = True
    if not a341:
        try:
            R = float(s.get("R") or 0)
        except Exception:
            R = 0.0
        out = ["<p>The seismic force-resisting system is a steel system not specifically detailed for seismic "
               "resistance (R = %s, ASCE 7-22 Table 12.2-1 row H): AISC 341 does not apply. Members and connections are "
               "designed to AISC 360-22 for the forces from the analysis (Chapters 6 and 10); no capacity-design "
               "(expected-strength) forces, protected zones or AISC 341 width-thickness limits are required.</p>" % _n(R)]
        out.append(_uplift_summary(pkg))
        if det:
            out.append("<h4>Detailing record</h4>" + _record_html(_no_341_claims(det)))
        if cap:
            out.append("<h4>Seismic design basis (calc package record)</h4>" + _record_html(_no_341_claims(cap)))
        out.append("<p class='cnote'>Wind: components and cladding (C&amp;C) and the cladding attachments are part of the "
                   "delegated cladding design.</p>")
        return "".join(out)
    if det:
        _rows = [[_n(c.get("check", "")), _n(c.get("status", ""))] for c in det.get("checks", []) if isinstance(c, dict)]
        _intro = ("<p><b>System:</b> %s &mdash; R = %s, C<sub>d</sub> = %s, &Omega;<sub>0</sub> = %s, SDC %s.</p><p>%s</p>"
                  % (_n(det.get('system', '')), _n(det.get('R', s.get('R'))), _n(det.get('Cd', s.get('Cd'))),
                     _n(det.get('Omega0', s.get('Om0'))), _n(det.get('SDC', '')), _n(det.get('basis', ''))))
        return _intro + _table(["Required check", "Status / basis"], _rows)
    out = []
    for f in sf["families"]:
        if f not in FAM_341:
            continue
        checks = _DETAIL_CHECKS.get(f)
        title = FAM_NAME.get(f, f) + " &mdash; AISC 341-22 &sect;%s" % FAM_341[f]
        dirs = "/".join(d for d in ("X", "Y") if f in sf["dirs"][d])
        out.append(f"<h4>{title} ({dirs} direction)</h4>")
        if not checks:
            out.append("<p class='cnote'>The AISC 341-22 provisions of this system are addressed in the capacity-design "
                       "record below.</p>")
            continue
        rows = [[n, b, "see the capacity-design record and Appendix A" if cap else "<b>not recorded in the calc package</b>"]
                for n, b in checks]
        out.append(_table(["Required check", "Basis", "Status"], rows))
    if any(f in ("MF?", "BF?") for f in sf["families"]):
        out.append("<p class='note'>The seismic force-resisting system is not identified, so its AISC 341-22 detailing "
                   "checks cannot be listed.</p>")
    if sf["dual"]:
        out.append("<p class='cnote'>Dual system: the moment frames resist at least 25% of the design seismic forces "
                   "independently (ASCE 7-22 &sect;12.2.5.1).</p>")
    intro = ("<p>Required AISC 341-22 ductile-detailing and capacity-design checks for each seismic force-resisting "
             "system; the computed values are in the capacity-design record below and in Appendix A.</p>")
    extra = ("<h4>Capacity-design results (calc package record)</h4>" + _record_html(cap)) if cap else ""
    note = ("<p class='cnote'>Weld inspection (NDT) and the C&amp;C cladding are confirmed on the drawings and the "
            "connection submittal%s.</p>" % ("; AISC 358 prequalification limits are listed with the moment connections"
                                           if ctx.get("mf358") else ""))
    return intro + "".join(out) + extra + _uplift_summary(pkg) + note


def _base_reactions(pkg):
    """{node: reaction-envelope dict} from calc_package demand_analysis.base_reactions (all combinations), else {}."""
    da = ((pkg or {}).get("demand_analysis") or {}) if isinstance(pkg, dict) else {}
    br = da.get("base_reactions")
    out = {}
    if isinstance(br, dict):
        for n, r in br.items():
            if isinstance(r, dict):
                out[str(n)] = r
    return out


def _grid_txt(node, r=None):
    g = (r or {}).get("grid")
    if isinstance(g, (list, tuple)) and len(g) == 2:
        return "(%s, %s)" % (g[0], g[1])
    try:
        n = int(node)
        k = n // 100000
        return "(%d, %d)" % ((n % 100000) // 100, n % 100) + ((" at level %d" % k) if 0 < k < 100 else "")
    except Exception:
        return _esc(node)


def _base_groups(br):
    """[(label, rows)] -- bases with net uplift and the others (foundation-design grouping)."""
    up = {n: r for n, r in br.items() if (r.get("uplift") or 0.0) > 0.05}
    rest = {n: r for n, r in br.items() if n not in up}
    out = []
    if up: out.append(("column bases with net uplift (%d)" % len(up), up))
    if rest: out.append((("other column bases (%d)" % len(rest)) if up else "column bases (%d)" % len(rest), rest))
    return out


def _max_of(rows, key):
    best = max(rows.items(), key=lambda t: abs(float(t[1].get(key) or 0.0)))
    return best[0], best[1]


def _connection_demands(cfg, pkg, reX=None, sf=None, ctx=None):
    """Chapter 10 connection-demand table: one row per member group (analysis envelope) plus the column bases from
    the all-combination base-reaction envelope. Beams carrying axial force (collector / drag / strut) show it with
    the shear; brace connections cite AISC 341 only where it applies."""
    rows = []; ms = [m for m in ((pkg or {}).get("members") or []) if isinstance(m, dict)]
    sf = sf or _sfrs(cfg); ctx = ctx or {}
    # connection type of each member group from the connection schedule (set from the analysis-model end
    # releases): 'conn-<member id>' -- a moment-connected floor/roof beam is an SMF/IMF/OMF beam
    ctype_of = {str(c.get("id", "")).lower(): str(c.get("type") or "").lower()
                for c in ((pkg or {}).get("connections") or []) if isinstance(c, dict)}
    _conn_rec = {str(c.get("id", "")).lower(): c for c in ((pkg or {}).get("connections") or []) if isinstance(c, dict)}
    a341 = ctx.get("a341")
    if a341 is None:
        a341 = any(f in FAM_341 for f in sf.get("families", []))
    vmax_sec = {}
    for m in ms:
        inp = m.get("inputs", {}) or {}
        try:
            vmax_sec[inp.get("section")] = max(vmax_sec.get(inp.get("section"), 0.0), float(inp.get("V_kip") or 0.0))
        except (TypeError, ValueError):
            pass
    def f(x):
        try:
            return float(x or 0.0)
        except (TypeError, ValueError):
            return 0.0
    for m in ms:
        inp = m.get("inputs", {}) or {}; kind = inp.get("kind", ""); sec = inp.get("section", "")
        if not sec:
            continue
        V = f(inp.get("V_kip")); Mz = f(inp.get("Mz_kipin"))
        Pc, Pt = f(inp.get("P_comp_kip")), f(inp.get("P_tens_kip")); Pcol = f(inp.get("collector_axial_kip"))
        if kind == "beam":
            _tag = (str(inp.get("role", "")) + " " + str(m.get("id", ""))).lower()
            _role = str(inp.get("role", "")).lower()
            _joints = str(((cfg.get("model") or {}).get("joints")) or "").lower()
            _ct = ctype_of.get("conn-" + str(m.get("id", "")).lower(), "")
            if any(t in _tag for t in ("smf", "imf", "omf", "stmf", "moment", "mf_", "mf-")):
                mf = sf["has_mf"]
            elif _ct and "moment" in _ct and "if moment-connected" not in _ct:
                mf = True                          # the schedule says moment connection (unreleased beam ends)
            elif _ct and "shear" in _ct and "moment" not in _ct:
                mf = False
            elif _role in ("roof", "floor") or "grav" in _tag or "collector" in _tag or "infill" in _tag:
                mf = False                         # gravity / collector beams: shear connections unless tagged otherwise
            else:
                mf = sf["has_mf"] and (_joints.startswith("rigid") or not sf["has_br"])
            ax = max(Pc, Pt, Pcol)
            if V <= 0 and ax > 0:
                V = vmax_sec.get(sec, 0.0)
            vtxt = ("V = %s kip" % _n(V)) if V > 0 else "V per the beam group"
            if mf:
                typ = "beam-to-column moment connection"
                dem = vtxt + (", M<sub>end</sub> = %s kip-ft" % _n(Mz / 12.0) if Mz else "")
                # the connection record's own design forces (e.g. Mpr / 1.1RyMp and V of the prequalified procedure)
                _mrec = _conn_rec.get("conn-" + str(m.get("id", "")).lower())
                _md = (_mrec or {}).get("demand") if isinstance((_mrec or {}).get("demand"), dict) else {}
                try:
                    _mV = float(_md["V_design_kip"]) if _md.get("V_design_kip") not in (None, "") else None
                    _mM = float(_md["M_design_kipft"]) if _md.get("M_design_kipft") not in (None, "") else None
                except (TypeError, ValueError):
                    _mV = _mM = None
                if _mV is not None or _mM is not None:
                    dem = ("V = %s kip" % _n(_mV) if _mV is not None else vtxt) + \
                          ((", M = %s kip-ft" % _n(_mM)) if _mM is not None else "") + " (connection design forces of the record)"
                fams = sf.get("families", [])
                basis = ("AISC 358-22 prequalified connection; AISC 341-22 %s" %
                         ("E3" if "SMF" in fams else "E2" if "IMF" in fams else "E1")) if a341 else \
                        "AISC 360-22 Ch. J fully restrained moment connection (J2 welds, J3 bolts, J10) for the analysis forces"
            else:
                typ = "simple shear connection" + (" + axial (collector / drag strut)" if ax > 0.5 else "")
                dem = vtxt + ((", axial T / C = %s / %s kip" % (_n(max(Pt, Pcol)), _n(max(Pc, Pcol)))) if ax > 0.5 else "")
                basis = ("single plate or clip angles: AISC 360-22 J3 / J4 and AISC Manual Part 10 (bolt-group "
                         "eccentricity, plate ductility" + ("; axial transfer with the shear" if ax > 0.5 else "") + ")")
                # a beam whose own record makes the joint fully restrained (braced-bay beams, struts, collectors at
                # boxes) is printed as recorded, with the clauses the record cites
                _brec = _conn_rec.get("conn-" + str(m.get("id", "")).lower())
                _brt = str((_brec or {}).get("type") or "").strip()
                if _brt and any(t_ in _brt.lower() for t_ in ("fully restrained", "cjp")):
                    _first = re.split(r"[:;(]", _brt)[0].strip(" ,")
                    typ = _esc(_first if len(_first) <= 90 else "beam-to-column connection") + (
                        ", fully restrained" if "fully restrained" in _brt.lower() and "fully restrained" not in _first.lower() else "")
                    _rd = (_brec.get("demand") or {}) if isinstance(_brec.get("demand"), dict) else {}
                    def _fv(*ks):
                        for k_ in ks:
                            try:
                                if _rd.get(k_) not in (None, ""):
                                    return float(_rd[k_])
                            except (TypeError, ValueError):
                                pass
                        return None
                    _dV, _dM = _fv("V_design_kip", "V_kip"), _fv("M_design_kipft", "M_kipft")
                    _dP = _fv("P_design_kip", "Pu_kip", "P_kip")
                    if _rd.get("design_forces") or _fv("V_design_kip", "M_design_kipft", "P_design_kip") is not None:
                        dem = ("V = %s kip" % _n(_dV) if _dV is not None else vtxt) + \
                              ((", axial = %s kip" % _n(_dP)) if _dP else "") + ((", M = %s kip-ft" % _n(_dM)) if _dM else "") + \
                              " (joint design forces of the record)"
                    else:
                        dem = vtxt + ((", axial T / C = %s / %s kip" % (_n(max(Pt, Pcol)), _n(max(Pc, Pcol)))) if ax > 0.5 else "") + \
                              ((", M<sub>end</sub> = %s kip-ft" % _n(Mz / 12.0)) if Mz else "")
                    _cl = sorted(set(re.findall(r"\b[DEFH]\d\.\d[a-z]?(?:\([a-z0-9]\))*", _brt + " " + str(_brec.get("design_basis") or ""))))
                    basis = (("AISC 341-22 " + ", ".join(_cl)) if (_cl and a341) else
                             "AISC 360-22 J2 / J10 (fully restrained beam-to-column connection)")
                elif _brec and isinstance(_brec.get("demand"), dict) and any(
                        _brec["demand"].get(k_) not in (None, "") for k_ in ("P_design_kip", "V_design_kip")):
                    # a simple connection designed for forces above the analysis envelope (strut / collector detail
                    # at the 12.10.2.1 force): print the record's type and design forces
                    _rd = _brec["demand"]
                    try:
                        _dV = float(_rd.get("V_design_kip") if _rd.get("V_design_kip") not in (None, "") else V)
                        _dP = float(_rd.get("P_design_kip") or 0.0)
                    except (TypeError, ValueError):
                        _dV, _dP = V, 0.0
                    _first = re.split(r"[:;(]", _brt)[0].strip(" ,") if _brt else ""
                    if _first and len(_first) <= 90:
                        typ = _esc(_first) + (" (collector / strut detail)" if "strut" in _brt.lower() or "collector" in _brt.lower() else "")
                    dem = ("V = %s kip" % _n(_dV)) + ((", axial T / C = %s kip" % _n(_dP)) if _dP > 0.5 else "") + \
                          " (connection design forces of the record)"
        elif "col" in kind:
            # the column's own connection record ('conn-<member id>') says what the column joint is: a splice, a
            # one-piece column with no splice (no row), or another joint such as a cap plate or a girder seat
            _crec = _conn_rec.get("conn-" + str(m.get("id", "")).lower())
            _crt = str((_crec or {}).get("type") or "").strip()
            if _crt.lower().startswith("no "):
                continue
            dem = "P = %s kip" % _n(Pc) + ((", T = %s kip" % _n(Pt)) if Pt > 0.5 else "") + \
                  ((", M<sub>x</sub> = %s kip-ft" % _n(Mz / 12.0)) if Mz else "")
            if _crt and "splice" not in _crt.lower():
                typ = _esc(_crt.split(";")[0].strip())
                basis = _esc(str(_crec.get("design_basis") or "AISC 360-22 Ch. J").split(";")[0].strip())
            else:
                # a splice record names the splice ("SMF column splice, CJP groove welded, ..."): print its first clause
                typ = re.split(r"[:;(]", _crt)[0].strip(" ,") if _crt else ""
                typ = _esc(typ) if typ and len(typ) <= 90 else "column splice"
                if any(t_ in _crt.lower() for t_ in ("cjp", "groove")):
                    basis = "AISC 360-22 J2 (complete-joint-penetration groove welds)" + ("; AISC 341-22 D2.5" if a341 else "")
                else:
                    basis = "AISC 360-22 J1.4 (bearing) / J3 splice plates" + ("; AISC 341-22 D2.5" if a341 else "")
        elif kind == "brace":
            typ = "brace-to-gusset"
            dem = "P = +%s / &minus;%s kip (tension / compression)" % (_n(Pt), _n(Pc))
            fams = sf.get("families", [])
            if not a341:
                basis = "AISC 360-22 Ch. J / Ch. K for the analysis forces (Whitmore J4, block shear J4.3, welds J2)"
            else:
                basis = ("adjusted brace strength (AISC 341-22 F4.6c)" if "BRBF" in fams else
                         "capacity-limited by the link (AISC 341-22 F3.6)" if "EBF" in fams else
                         "expected strength or &Omega;<sub>0</sub> load effect (AISC 341-22 F1.6a)" if "OCBF" in fams else
                         "expected brace strengths (AISC 341-22 F2.6c)" if "SCBF" in fams else
                         "AISC 360-22 Ch. J for the analysis forces")
        else:
            continue
        rows.append([_esc(f"{sec} {_KIND_LABEL.get(kind, kind)}") + (" (%s)" % _esc(m.get("id"))), typ, dem, basis])
    br = _base_reactions(pkg)
    if br:
        for lab, grp in _base_groups(br):
            nP, rP = _max_of(grp, "P"); nU, rU = _max_of(grp, "uplift"); nV, rV = _max_of(grp, "V")
            dem = "P = %s kip [%s]" % (_n(rP.get("P")), _combo_label(rP.get("P_combo")))
            if (rU.get("uplift") or 0) > 0.05:
                dem += "; net uplift = %s kip [%s]" % (_n(rU.get("uplift")), _combo_label(rU.get("uplift_combo")))
            dem += "; V = %s kip [%s]" % (_n(rV.get("V")), _combo_label(rV.get("V_combo")))
            nM, rM = _max_of(grp, "M")
            if abs(float(rM.get("M") or 0.0)) > 0.5:
                dem += "; M = %s kip-ft" % _n(float(rM["M"]) / 12.0)
            rows.append([lab, "base plate / anchor rods", dem,
                         "AISC 360-22 J8 / J9 and ACI 318-19 Ch. 17 (minimum 4 anchor rods)" + ("; AISC 341-22 D2.6" if a341 else "")])
    else:
        for c in ((pkg or {}).get("connections") or []):
            if isinstance(c, dict) and str(c.get("id", "")).lower().startswith("conn-base"):
                rows.append([_esc(c.get("id")), "base plate / anchor rods", _fmt(c.get("demand") or {}),
                             "AISC 360-22 J8 / J9 and ACI 318-19 Ch. 17 (minimum 4 anchor rods)"])
    if not rows:
        return None
    return _table(["Member / location", "Connection type", "Governing demand (analysis envelope)",
                   "Design basis (limit states)"], rows)


def _connection_summary(pkg):
    """Designed connections of the calc package: type, demand, governing limit state, D/C and status (Appendix A)."""
    C = _consistency_mod()
    cd = (pkg or {}).get("connections") if isinstance(pkg, dict) else None
    if not isinstance(cd, list) or not cd:
        return ""
    rows = []
    for c in cd:
        if not isinstance(c, dict):
            continue
        chks = [k for k in (c.get("checks") or []) if isinstance(k, dict)]
        ls, dc = c.get("limit_state"), (C._dc_num(c.get("DC")) if C else None)
        if chks and C:
            best = max(((C._dc_num(k.get("DC")), k) for k in chks if C._dc_num(k.get("DC")) is not None),
                       key=lambda t: t[0], default=(None, None))
            if best[0] is not None and (dc is None or best[0] > dc):
                dc = best[0]
                ls = best[1].get("limit_state")
        issues = C._entry_issues("connection", c) if C else []
        cid = str(c.get("id", ""))
        rows.append(["<a href='#c-%s'>%s</a>" % (_esc(re.sub(r"[^A-Za-z0-9_-]", "_", cid)), _esc(cid)),
                     _n(c.get("type", "")),
                     _fmt(c.get("demand")) if isinstance(c.get("demand"), dict) else _n(c.get("demand")),
                     (_n(ls) + (" (+%d more checks)" % (len(chks) - 1) if len(chks) > 1 else "")) if ls else "&mdash;",
                     _n(dc, dc=True) if dc is not None else "&mdash;",
                     _dc_status(dc, issues, c.get("waived")) if (dc is not None or issues) else "not recorded"])
    return ("<h3>Designed connections</h3><p>Each connection type is designed in this package; its components and every "
            "limit-state check are in Appendix A.</p>"
            + _table(["Connection", "Type", "Design demand", "Governing limit state", "D/C", "Status"], rows))


def _base_reactions_section(cfg, pkg, ctx=None):
    """Chapter 11: column-base reactions for the foundation design from the all-combination base-reaction envelope
    (calc_package demand_analysis.base_reactions): headline largest compression, largest net uplift with its
    combination and largest shear, then a table of every base (19h)."""
    br = _base_reactions(pkg)
    if not br:
        bases = [c for c in ((pkg or {}).get("connections") or []) if isinstance(c, dict)
                 and str(c.get("id", "")).lower().startswith("conn-base")]
        if not bases:
            return ("<p class='note'>Column-base reactions are not recorded in the calc package for this building.</p>")
        rows = [[_esc(c.get("id")), _fmt(c.get("demand") or {})] for c in bases]
        return ("<p>Column-base design reactions (from the base-connection design records; a per-base envelope is not "
                "recorded in this calc package):</p>" + _table(["Base connection", "Design reactions"], rows))
    nP, rP = _max_of(br, "P"); nU, rU = _max_of(br, "uplift"); nV, rV = _max_of(br, "V")
    up = float(rU.get("uplift") or 0.0)
    ci = (ctx or {}).get("cases_info") or {}
    extra = " and ".join(x for x, on in (("&Omega;<sub>0</sub>", ci.get("om0_dirs")), ("E<sub>cl</sub>", ci.get("ecl"))) if on)
    head = ["<p>Column-base reactions for the foundation design: envelope over every LRFD combination (gravity, %sseismic"
            "%s), second-order analysis, factored (strength level).</p><ul>"
            % ("wind incl. the 0.9D + 1.0W roof uplift, " if cfg.get("wind") else "",
               (" incl. the %s cases" % extra) if extra else ""),
            "<li>Largest compression: <b>%s kip</b> at base %s (%s).</li>" % (_n(rP.get("P")), _grid_txt(nP, rP), _combo_label(rP.get("P_combo"))),
            ("<li>Largest net uplift: <b>%s kip</b> at base %s (%s).</li>" % (_n(up), _grid_txt(nU, rU), _combo_label(rU.get("uplift_combo")))
             if up > 0.05 else "<li>No column base is in net uplift in any combination.</li>"),
            "<li>Largest base shear: <b>%s kip</b> at base %s (%s).</li>" % (_n(rV.get("V")), _grid_txt(nV, rV), _combo_label(rV.get("V_combo")))]
    nM, rM = _max_of(br, "M")
    if abs(float(rM.get("M") or 0.0)) > 0.5:
        head.append("<li>Largest base moment: <b>%s kip-ft</b> at base %s (%s).</li>"
                    % (_n(float(rM.get("M")) / 12.0), _grid_txt(nM, rM), _combo_label(rM.get("M_combo"))))
    head.append("</ul>")
    order = sorted(br.items(), key=lambda t: (-(t[1].get("uplift") or 0.0), -(t[1].get("P") or 0.0)))
    fixed = any(abs(float(r.get("M") or 0.0)) > 0.5 for _n_, r in order)
    rows = []
    for n, r in order[:60]:
        row = [_grid_txt(n, r), _n(r.get("P")), _combo_label(r.get("P_combo")),
               _n(r.get("uplift")) if (r.get("uplift") or 0) > 0.05 else "&mdash;",
               _combo_label(r.get("uplift_combo")) if (r.get("uplift") or 0) > 0.05 else "&mdash;",
               _n(r.get("V")), _combo_label(r.get("V_combo"))]
        if fixed:
            row += [_n(float(r.get("M") or 0.0) / 12.0), _combo_label(r.get("M_combo"))]
        rows.append(row)
    hdr = ["Base (i, j)", "P max (kip)", "combination", "net uplift (kip)", "combination", "V max (kip)", "combination"]
    if fixed:
        hdr += ["M max (kip-ft)", "combination"]
    tail = ("<p class='cnote'>Base grid labels (i, j) as in the plan figure of Chapter 1. Reactions are factored "
            "(LRFD); the foundation and geotechnical design (bearing, sliding, uplift anchorage, footing sizing) is by "
            "others. Overall overturning and sliding stability are confirmed against the geotechnical capacities.</p>")
    more = ("<p class='cnote'>%d further bases (no uplift, lower compression) are retained in the calc package.</p>"
            % (len(order) - 60)) if len(order) > 60 else ""
    return "".join(head) + _table(hdr, rows) + more + tail


_SANITY_NAMES = {"drift_X": "seismic drift X", "drift_Y": "seismic drift Y", "period": "period vs C<sub>u</sub>T<sub>a</sub>",
                 "equil_X": "equilibrium X", "equil_Y": "equilibrium Y", "stability": "stability",
                 "modalmass_X": "modal mass X", "modalmass_Y": "modal mass Y", "baseshear_X": "base shear X",
                 "baseshear_Y": "base shear Y", "model_declared": "model input complete", "model_consistent":
                 "built model matches the input", "stability_theta": "&theta; (&sect;12.8.7)", "beam_deflection": "beam deflection",
                 "model_complete": "model completeness", "dual_25pct": "dual-system 25% check",
                 "rs_X": "MRSA scaling X (&sect;12.9.1.4)", "rs_Y": "MRSA scaling Y (&sect;12.9.1.4)"}


def _qa_scorecard(cfg, Fx, reX, eX, eY, runs, pkg=None, root=None, reY=None):
    """Automated checks evaluated for this report (each row says exactly what was compared)."""
    rows = []
    for d, re_ in (("X", reX), ("Y", reY)):
        if re_ is None or not runs:
            continue
        di = 0 if d == "X" else 1
        Rx = sum(r[2][di] for r in re_); base = sum(runs[d]["F"].values())
        rows.append(["Equilibrium, ELF %s: |&Sigma;R| = applied base shear" % d, "%s vs %s kip" % (_n(abs(Rx)), _n(base)),
                     "PASS" if base and abs(abs(Rx)-base)/base < 0.01 else "REVIEW"])
    if eX is not None and eY is not None:
        cx = sum(eX)*100; cy = sum(eY)*100
        rows.append(["Modal mass &ge; 90% (X / Y, &sect;12.9.1.1)", f"{cx:.0f}% / {cy:.0f}%", "PASS" if min(cx, cy) >= 90 else "REVIEW"])
    if runs:
        try:
            from preflight import relief_active as _ra
            _rel = bool(_ra(cfg))
        except Exception:
            _rel = False
        for d in ("X", "Y"):
            r = runs[d]; lim = r["limit"]
            _limlab = f"{lim*100:.2f}%" + (" (&Delta;<sub>a</sub>/&rho;, &sect;12.12.1.1)" if r["limit_rho"] else "") \
                      + (" (&sect;16.1.2 relief)" if _rel else "")
            _rbs = (" &times; RBS %.3f" % r["rbs"][0]) if r["rbs"][0] > 1.0 else ""
            rows.append([f"Seismic design drift {d} &le; limit ({'diaphragm edge' if runs['edge_basis'] else 'centre of mass'}{_rbs})",
                         f"{r['design_max']*100:.3f}% &le; {_limlab}", "PASS" if r["design_max"] <= lim else "FAIL"])
        for d in ("X", "Y"):
            trs, tmax, beta = _theta_rows(cfg, runs, d)
            tw = max([t["theta"] for t in trs if not t["exempt"]] or [0.0])
            rows.append([f"Stability &theta; {d} &le; &theta;<sub>max</sub> (&sect;12.8.7)", f"{tw:.3f} &le; {tmax:.3f}",
                         "PASS" if tw <= tmax else "FAIL"])
    wd = _QA.get("wind_drift")
    if wd:
        _gw = (pkg or {}).get("gate_waivers") if isinstance(pkg, dict) else None
        _wv = isinstance(_gw, dict) and any(k in _gw for k in ("wind_drift", "wind_drift_X", "wind_drift_Y"))
        rows.append(["Wind drift &le; project limit (%s)" % wd[2], "%.3f%% &le; %.2f%%" % (wd[0] * 100, wd[1] * 100)
                     + ("; addressed by engineering justification (listed below)" if (wd[0] > wd[1] and _wv) else ""),
                     "PASS" if wd[0] <= wd[1] else ("JUSTIFIED" if _wv else "FAIL")])
    fv = (pkg or {}).get("floor_vibration") if isinstance(pkg, dict) else None
    if isinstance(fv, dict) and fv:
        _C = _consistency_mod()
        _fvd = (_C._dc_num(fv.get("DC")) if _C else None)
        if _fvd is None:
            try:
                _fvd = float(fv.get("DC"))
            except (TypeError, ValueError):
                _fvd = None
        if _fvd is not None:
            rows.append(["Floor vibration (AISC Design Guide 11)", "D/C %s%s" % (
                _n(_fvd, dc=True), (" (%s)" % _esc(str(fv.get("criterion", ""))[:90])) if fv.get("criterion") else ""),
                "PASS" if _fvd <= 1.0 + 1e-9 else "FAIL"])
    bd = _QA.get("beam_deflection")
    if bd:
        _bj = len(bd) > 2 and bd[2]
        rows.append(["Beam deflection (IBC Table 1604.3 limits)", bd[1] + ("; addressed by engineering justification "
                     "(listed below)" if _bj else ""), "PASS" if bd[0] else ("JUSTIFIED" if _bj else "FAIL")])
    hl = _QA.get("height_limit")
    if hl:
        rows.append(["Structural height limit (Table 12.2-1)", hl[1], "PASS" if hl[0] else "FAIL"])
    C = _consistency_mod()
    for what, key in (("Member", "members"), ("Connection", "connections")):
        ents = [m for m in ((pkg or {}).get(key) or []) if isinstance(m, dict)] if isinstance(pkg, dict) else []
        if not ents or C is None:
            continue
        dcs = []; exist = []
        for m in ents:
            vals = [C._dc_num(m.get("DC"))] + [C._dc_num(k.get("DC")) for k in (m.get("checks") or []) if isinstance(k, dict)]
            vals = [v for v in vals if v is not None]
            dv = max(vals) if vals else None
            if dv is not None and dv > 1.0 + 1e-9 and m.get("waived"):
                exist.append((dv, m.get("id")))        # existing member carried as a retrofit-scope item
                continue
            dcs.append((dv, m.get("id")))
        have = [t for t in dcs if t[0] is not None]
        if have:
            dmax, idm = max(have, key=lambda t: t[0])
            miss = len(dcs) - len(have)
            ex_txt = ("; existing %ss over capacity carried as retrofit scope: %s" % (
                what.lower(), ", ".join("%s %s" % (_esc(i), _n(v, dc=True)) for v, i in sorted(exist, reverse=True)[:6]))
                if exist else "")
            rows.append(["%s D/C &le; 1.0 (%d of %d %ss with a D/C%s)" % (what, len(have), len(dcs), what.lower(),
                                                                         ", new / altered" if exist else ""),
                         "max %s (%s)%s" % (_n(dmax, dc=True), _esc(idm), ex_txt),
                         ("PASS" if dmax <= 1.0 + 1e-9 and not miss else ("FAIL" if dmax > 1.0 + 1e-9 else "INCOMPLETE"))
                         + ("; existing: retrofit scope" if exist else "")])
        else:
            rows.append(["%s D/C &le; 1.0" % what, "no D/C recorded", "INCOMPLETE"])
    try:
        pr = json.load(open(os.path.join(root, "design", "pipeline_results.json"), encoding="utf-8")) if root else None
    except Exception:
        pr = None
    if isinstance(pr, dict) and isinstance(pr.get("checks"), dict):
        failed = [k for k, v in pr["checks"].items() if not v]
        names = ", ".join(_SANITY_NAMES.get(k, _esc(k)) for k in pr["checks"])
        gw = (pkg or {}).get("gate_waivers") if isinstance(pkg, dict) else None
        gw = gw if isinstance(gw, dict) else {}
        waived = [k for k in failed if k in gw]; open_ = [k for k in failed if k not in gw]
        _nm = lambda ks: ", ".join(_SANITY_NAMES.get(k, _esc(k)) for k in ks)
        res = ("all passed" if not failed else "; ".join(x for x in (
            ("failed: " + _nm(open_)) if open_ else "",
            ("addressed by engineering justification (listed below): " + _nm(waived)) if waived else "") if x))
        rows.append(["Analysis model checks of the demand analysis (%s)" % names, res,
                     "PASS" if not failed else ("FAIL" if open_ else "JUSTIFIED")])
    if not rows:
        return ""
    return ("<h3>QA scorecard (computed checks)</h3>"
            + _table(["Check", "Result", "Status"], rows))


def _save_case_fig(uri, figdir, label):
    """Write a per-combination N/V/M figure to figs/case_<label>.png for the user to inspect later;
    it is NOT embedded in the report (keeps report.html compact)."""
    if not (figdir and uri and isinstance(uri, str)):
        return
    safe = "".join(c if c.isalnum() else "_" for c in str(label))[:60].strip("_") or "case"
    try:
        if uri.startswith("data:image/"):
            head, b64 = uri.split(",", 1)
            ext = head.split("/")[1].split(";")[0].replace("jpeg", "jpg")
            with open(os.path.join(figdir, "case_%s.%s" % (safe, ext)), "wb") as f:
                f.write(base64.b64decode(b64))
        else:
            src = os.path.join(figdir, os.path.basename(uri))
            if os.path.exists(src):
                import shutil; shutil.copy2(src, os.path.join(figdir, "case_%s.png" % safe))
    except Exception:
        pass

def _static_case_table(cfg, combo, nseg=2, render_fig=True):
    """Per member TYPE governing forces for ONE combination, from the STATIC model (distributed
    gravity) so beams carry their true moment (fixes the lumped-model 1.4D = zero-beam-moment bug).
    Custom-geometry buildings (custom_build) fall back to the dynamic model's demand envelope, which
    already adds the beam gravity span moment and supports arbitrary geometry."""
    label, fD, fL, fLr, lat, co = combo
    nseg = 6 if render_fig else nseg   # App-B per-combo FIGURE -> finer (nseg 6); scalar summary table -> fast (nseg 2)
    import static_model as SM
    mm, _, _ = SM.run_combo(cfg, fD, fL, fLr, lat, nseg=nseg)
    best = {}; maxN = (0.0, "", ""); maxM = (0.0, "", "")
    def upd(kind, sec, N, Mz, My, V, n1, n2, score):
        key = (kind, sec)
        if key not in best or score > best[key][0]:
            best[key] = (score, N, Mz, My, V, n1, n2)
    for b in mm["beams"]:
        sec = b.get("sec") or cfg.get("beam") or "&mdash;"; N = Mz = My = V = 0.0
        for tag in b["segs"]:
            lf = ops.eleResponse(tag, "localForces")
            N = max(N, abs(lf[0]), abs(lf[6])); Mz = max(Mz, abs(lf[4]), abs(lf[10]))
            My = max(My, abs(lf[5]), abs(lf[11])); V = max(V, abs(lf[2]), abs(lf[8]))
        upd("beam", sec, N, Mz, My, V, b["A"], b["B"], Mz)
        if N > abs(maxN[0]): maxN = (N, "beam", sec)
        if Mz > abs(maxM[0]): maxM = (Mz, "beam", sec)
    for c in mm["cols"]:
        sec = c.get("sec") or cfg.get("col") or "&mdash;"; lf = ops.eleResponse(c["tag"], "localForces")
        N = max(abs(lf[0]), abs(lf[6])); Mz = max(abs(lf[5]), abs(lf[11])); My = max(abs(lf[4]), abs(lf[10]))
        V = max(abs(lf[1]), abs(lf[2]), abs(lf[7]), abs(lf[8]))
        upd("col", sec, N, Mz, My, V, c["n1"], c["n2"], max(Mz, N))
        if N > abs(maxN[0]): maxN = (N, "col", sec)
        if max(Mz, My) > abs(maxM[0]): maxM = (max(Mz, My), "col", sec)
    for br in mm.get("braces", []):
        sec = br.get("sec") or cfg.get("brace", ""); N = ops.basicForce(br["tag"])[0]
        upd("brace", sec, N, 0.0, 0.0, 0.0, None, None, abs(N))
        if abs(N) > abs(maxN[0]): maxN = (N, "brace", sec)
    rows = []
    for (kind, sec), (sc, N, Mz, My, V, n1, n2) in sorted(best.items()):
        rows.append([_KIND_LABEL.get(kind, kind), sec, _n(float(N)), _n(float(Mz) / 12.0), _n(float(My) / 12.0),
                     _n(float(V)), _loc(n1, n2) if n1 else "&mdash;"])
    d = _lat_dir(lat) or "X"
    uri = None
    if render_fig:                       # per-combo Appendix-B N/V/M figure (opt-in; the governing
        try:                             # diagrams in Chapter 5 already cover the governing cases)
            import frame_diagram as FD
            uri, _pk = FD.render_solved(cfg, mm, d, label, "perimeter")
        except Exception:
            uri = None
    return rows, maxN, maxM, uri, d

def _jmark(ax, x, z, pinned):
    if pinned: ax.plot([x], [z], marker="o", mfc="white", mec="#c0392b", ms=8, mew=1.6, zorder=5)
    else:      ax.plot([x], [z], marker="s", mfc="#1f3b73", mec="#1f3b73", ms=7, zorder=5)

def _joint_dist_fig(cfg, direction, model=None):
    """2D elevations of the perimeter and an internal frame line, every beam-end and column base
    marked rigid (filled) or pinned/shear-released (open). Parametric buildings read cfg['releases']
    and cfg['base']; custom_build buildings pass the staticized `model` (per-beam relz/rely + per-
    column bases). A beam end is 'pinned' when its STRONG-axis (vertical/gravity) moment is released
    (rely), per the corrected release convention."""
    custom = model is not None
    if cfg.get("custom_build") and not custom:
        return None
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    import matplotlib.lines as mlines
    NX, NY, SX, SY = cfg["NX"], cfg["NY"], cfg["SX"], cfg["SY"]; NF = len(cfg["heights"]); z = E.zlevels(cfg)
    if custom:
        rel_lu = {(b["i"], b["j"], b["k"], b["dir"]): (b.get("relz", "none"), b.get("rely", "none")) for b in model["beams"]}
        base_lu = model.get("bases", {})
    else:
        relf = cfg.get("releases"); base0 = cfg.get("base", "fixed")
    # ABSENT members must draw NOTHING (never a fixed/rigid default): on notched / offset-core /
    # split-level plans the elevation lines cross bays and bases that do not exist in the model --
    # the old .get(..., "fixed"/"none") defaults painted phantom FIXED bases and RIGID joints there.
    if custom:
        present = {k: set(map(tuple, v)) for k, v in (model.get("present") or {}).items()}
    else:
        present = {k: E.grid(cfg, k) for k in range(NF+1)}
    def _pt(i, fx):
        return (i, fx) if direction == "X" else (fx, i)
    def _exists(i, fx, k):
        return _pt(i, fx) in (present.get(k) or set())
    if direction == "X":
        ncol, span, coords = NX, SX, [i*SX for i in range(NX+1)]
        lines = [("Perimeter frame (j=0)", 0), (f"Internal frame (j={NY//2})", NY//2)]
        def beam_rel(i, k, fx):
            return rel_lu.get((i, fx, k, "X")) if custom else ((relf(i, fx, k, "X") if relf else ("none", "none"))
                                                               if _exists(i, fx, k) and _exists(i+1, fx, k) else None)
        def base_at(i, fx):
            return base_lu.get((i, fx)) if custom else (base0 if _exists(i, fx, 0) else None)
    else:
        ncol, span, coords = NY, SY, [j*SY for j in range(NY+1)]
        lines = [("Perimeter frame (i=0)", 0), (f"Internal frame (i={NX//2})", NX//2)]
        def beam_rel(i, k, fx):
            return rel_lu.get((fx, i, k, "Y")) if custom else ((relf(fx, i, k, "Y") if relf else ("none", "none"))
                                                               if _exists(i, fx, k) and _exists(i+1, fx, k) else None)
        def base_at(i, fx):
            return base_lu.get((fx, i)) if custom else (base0 if _exists(i, fx, 0) else None)
    fig, axes = plt.subplots(1, 2, figsize=(16, 6), sharey=True)
    for ax, (ttl, fx) in zip(axes, lines):
        for i in range(ncol+1):
            zs = [k for k in range(NF+1) if _exists(i, fx, k)]
            if len(zs) >= 2:                                   # column line only over the levels that exist
                ax.plot([coords[i]]*2, [z[min(zs)], z[max(zs)]], color="#999", lw=2, zorder=1)
            bf = base_at(i, fx)
            if bf is not None:
                ax.plot([coords[i]], [z[0]], marker="^", ms=11, zorder=5,
                        mfc=("white" if bf == "pinned" else "#1f3b73"),
                        mec=("#c0392b" if bf == "pinned" else "#1f3b73"), mew=1.6)
        for k in range(1, NF+1):
            for i in range(ncol):
                rel = beam_rel(i, k, fx)
                if rel is None:
                    continue                                   # bay not present on this line/level: draw nothing
                x1, x2 = coords[i], coords[i+1]; zk = z[k]
                relz, rely = rel
                ax.plot([x1, x2], [zk, zk], color="#999", lw=2, zorder=1)
                _jmark(ax, x1 + 0.12*span, zk, relz in ("I", "both"))
                _jmark(ax, x2 - 0.12*span, zk, relz in ("J", "both"))
        ax.set_title(ttl, fontsize=11); ax.set_xlabel("plan (in)"); ax.grid(alpha=0.2)
    axes[0].set_ylabel("Z (in)")
    leg = [mlines.Line2D([], [], marker="s", color="none", mfc="#1f3b73", mec="#1f3b73", ms=9, label="rigid / continuous"),
           mlines.Line2D([], [], marker="o", color="none", mfc="white", mec="#c0392b", ms=9, mew=1.6, label="pinned / shear release"),
           mlines.Line2D([], [], marker="^", color="none", mfc="#1f3b73", mec="#1f3b73", ms=10, label="base fixed"),
           mlines.Line2D([], [], marker="^", color="none", mfc="white", mec="#c0392b", ms=10, mew=1.6, label="base pinned")]
    fig.suptitle(f"Joint fixity distribution - {direction}-direction frames", y=1.0, fontsize=12)
    fig.legend(handles=leg, loc="lower center", ncol=4, fontsize=9, frameon=False, bbox_to_anchor=(0.5, -0.02))
    fig.tight_layout(rect=[0, 0.06, 1, 0.96])
    return _b64(fig)

_QA = {}          # facts gathered while rendering (worst wind drift, gate results, ...) for the Ch. 13 scorecard


def _pkg_camber(pkg):
    """Camber values recorded in the calc package: {'composite': x, 'members': {id: x}} (in.)."""
    out = {"composite": None, "members": {}}
    if not isinstance(pkg, dict):
        return out
    cd = pkg.get("composite_design")
    if isinstance(cd, dict):
        for k in ("camber_in", "camber"):
            v = cd.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out["composite"] = float(v); break
    for m in pkg.get("members") or []:
        if not isinstance(m, dict):
            continue
        for src_ in (m.get("serviceability"), m.get("capacity")):
            if isinstance(src_, dict):
                v = src_.get("camber_in")
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out["members"][str(m.get("id"))] = float(v)
    return out


def _deflection_section(cfg, pkg=None):
    """Beam deflection table = the engine's beam_deflection GATE (engine3d.beam_serviceability_detail,
    one row per governing group), so the report can never print NG beside a gate PASS or vice versa
    (HR-28). Skipped groups / build errors are listed and make the check FAIL. Where the calc package's
    serviceability records or camber disagree with the gate, the difference is stated (19c)."""
    try:
        det = E.beam_serviceability_detail(cfg)
    except Exception as ex:
        _QA["beam_deflection"] = (False, "not evaluated")
        return f"<p class='note'>[beam deflection check could not be run: {_esc(ex)}] &mdash; the check does not pass.</p>"
    if cfg.get("beam_framing") == "truss":
        return ("<h3>Beam deflection</h3><p class='cnote'>Long-span trusses / joists: the W-shape deflection check is not "
                "applied; their deflection is part of the joist / truss design.</p>")
    h = ["<h3>Beam deflection and camber (analysis model check)</h3>",
         "<p>Deflection of every beam span / group (section &times; span &times; floor/roof) from %s; live (floor L, roof "
         "the governing one of L<sub>r</sub>, S and R) and total D + L against IBC 2024 Table 1604.3: floor members L/360 and L/240%s. "
         "Suggested camber &asymp; 80%% of the dead-load deflection (to the nearest 1/4 in.), listed only where it is at "
         "least 3/4 in.; it is a suggestion, not a specified camber (any specified camber is stated with the member "
         "design).</p>"
         % (_n(det.get("basis") or "the tributary loads of each beam"),
            "; roof members %s: L/%d and L/%d" % (
                {"plaster": "supporting a plaster or stucco ceiling", "nonplaster": "supporting a nonplaster ceiling",
                 "none": "not supporting a ceiling",
                 "floor": "at the top tier, an occupied platform designed as a floor"}.get(
                     str(cfg.get("roof_deflection") or "plaster").lower(),
                                                         "supporting a plaster or stucco ceiling"),
                *E.defl_limits(cfg, True)) if hasattr(E, "defl_limits") else "")]
    if det.get("error"):
        h.append(f"<p class='note'><b>Check FAILS &mdash; NOT EVALUATED:</b> {_n(det['error'])}</p>")
    grp = det.get("groups") or []
    rows = []; sugg = []
    _gw0 = (pkg or {}).get("gate_waivers") if isinstance(pkg, dict) else None
    _just0 = isinstance(_gw0, dict) and "beam_deflection" in _gw0
    def pick(roof):
        g = [x for x in grp if x["roof"] == roof]
        return sorted(g, key=lambda x: -max(x["rLL"], x["rTL"]))
    for roof, lbl in ((False, "Floor"), (True, "Roof")):
        lst = pick(roof)
        show = [x for x in lst if max(x["rLL"], x["rTL"]) >= 1.0] or lst[:1]
        uniq, cnt = [], []
        for x in show:
            L = x["L"]; dD = max(x["dTL"] - x["dLL"], 0.0)
            camber = round(0.8 * dD * 4) / 4
            camber = camber if camber >= 0.75 - 1e-9 else 0.0       # no camber below 3/4 in. is suggested
            if camber > 0:
                sugg.append((lbl.lower(), x["sec"], L, dD, camber))
            r = [_esc(x["sec"]), _ftin(L),
                 (_ftin(x['trib']) if x.get("trib") is not None else "solved model"),
                 "%s in. (L/%s)" % (_n(x['dLL'], nd=2), ("%.0f" % (L / x['dLL'])) if x['dLL'] > 1e-6 else "&infin;"),
                 "%s (L/%d)" % (_n(x["rLL"], nd=2), int(x.get("limL", 360))), "%s in." % _n(x['dTL'], nd=2),
                 "%s (L/%d)" % (_n(x["rTL"], nd=2), int(x.get("limT", 240))),
                 "OK" if max(x["rLL"], x["rTL"]) < 1.0 else ("see justification" if _just0 else "<b class='ng'>NG</b>"),
                 ("%s in." % _n(camber, nd=2)) if camber > 0 else "none"]
            if r in uniq:
                cnt[uniq.index(r)] += 1
            else:
                uniq.append(r); cnt.append(1)
        for r, c in list(zip(uniq, cnt))[:8]:
            rows.append([lbl + ((" (%d identical groups)" % c) if c > 1 else "")] + r)
    if rows:
        h.append(_table(["Beam", "Section", "Span", "Trib. width", "Live &delta;", "Live / limit", "Total &delta;",
                         "Total / limit", "Check", "Suggested camber"], rows))
    for key, why in (det.get("skipped") or [])[:10]:
        h.append(f"<p class='note'>Group {_esc(key)} NOT EVALUATED: {_n(why)} &mdash; the check fails until resolved.</p>")
    gate_ok = det["LL"] < 1.0 and det["TL"] < 1.0
    _gw = (pkg or {}).get("gate_waivers") if isinstance(pkg, dict) else None
    _just = (not gate_ok) and isinstance(_gw, dict) and "beam_deflection" in _gw
    _QA["beam_deflection"] = (gate_ok, "worst live %s, total %s" % (
        "%.2f" % det["LL"] if math.isfinite(det["LL"]) else "not evaluated",
        "%.2f" % det["TL"] if math.isfinite(det["TL"]) else "not evaluated"), _just)
    h.append("<p><b>Beam deflection check: %s</b> (worst live ratio %s, worst total ratio %s).</p>"
             % ("PASS" if gate_ok else ("analysis-model ratio above 1.0 &mdash; addressed by the engineering "
                                        "justification in Chapter 13" if _just else "FAIL"),
                "%.2f" % det["LL"] if math.isfinite(det["LL"]) else "not evaluated",
                "%.2f" % det["TL"] if math.isfinite(det["TL"]) else "not evaluated"))
    ag = [m for m in ((pkg or {}).get("members") or []) if isinstance(m, dict) and isinstance(m.get("serviceability"), dict)]
    if ag:
        ag_ok = all(bool(m["serviceability"].get("ok")) for m in ag)
        if ag_ok != gate_ok:
            h.append("<p class='note'><b>Reconcile:</b> the member serviceability records report %s while the analysis-model "
                     "check reports %s. The analysis-model check uses a simple span with the tributary load of the model; a "
                     "design record may use the one-way deck tributary, a composite I<sub>LB</sub> or camber. The calc "
                     "package must state which governs.</p>" % ("all OK" if ag_ok else "NG", "PASS" if gate_ok else "FAIL"))
    cam = _pkg_camber(pkg)
    rec = cam["members"] or ({"all members": cam["composite"]} if cam["composite"] is not None else {})
    if sugg and rec:
        recorded = max(rec.values())
        worst = max(sugg, key=lambda t: t[4])
        if recorded <= 1e-9 < worst[4] and isinstance(pkg, dict) and pkg.get("camber_basis"):
            h.append("<p class='cnote'><b>Camber:</b> %s</p>" % _n(pkg.get("camber_basis")))
        elif recorded <= 1e-9 < worst[4]:                     # camber 'none' recorded vs a suggested camber
            h.append("<p class='note'><b>Reconcile camber:</b> the analysis gives a dead-load deflection of %s in. for the "
                     "%s %s beams (span %s), i.e. a camber of about %s in.; the calc package records %s in. camber%s.</p>"
                     % (_n(worst[3], nd=2), worst[0], _esc(worst[1]), _ftin(worst[2]), _n(worst[4], nd=2), _n(recorded),
                        "" if cam["composite"] is None else " (composite design record)"))
            _QA["camber_conflict"] = True
    return "".join(h)


def _grounding_check(cfg, name, pkg):
    """Verify the design actually queried the RAG collections its systems require (reliability).
    Reads the activity log; A341/A358 are required only for seismic systems detailed for ductility
    (R > 3); AISC 360 + connection grounding are always required."""
    import re as _re
    recs, _ = _load_activity(name)
    col = {}
    for r in recs:
        if r.get("tool") == "search_engineering_standards":
            mm = _re.search(r"\[([A-Za-z0-9_]+)\]", r.get("detail", ""))
            if mm: col[mm.group(1)] = col.get(mm.group(1), 0) + 1
    def n(c): return col.get(c, 0)
    # ALSO credit grounding from the CITED CLAUSES in calc_package.json (not the activity log alone), so a
    # filled, RAG-grounded package is recognised even if the auto-logger did not record the queries (P5).
    def _cited(obj):
        out = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                out += ([str(v)] if k in ("cited", "cite") else _cited(v))
        elif isinstance(obj, list):
            for v in obj: out += _cited(v)
        return out
    _ct = " ".join(_cited(pkg or {}))
    cite_a360 = "360" in _ct; cite_a341 = "341" in _ct; cite_a358 = "358" in _ct
    R = cfg["seis"].get("R", 3)
    try:
        detailed = any(bool(_PIPE_A341(cfg, d)) for d in ("X", "Y"))
    except Exception:
        detailed = R > 3
    try: _sf = _sfrs(cfg)
    except Exception: _sf = None
    # HUB-09: AISC 358 prequalified connections apply to SMF and IMF (AISC 341 E3.6c / E2.6c), including
    # the moment frames of a dual or mixed system; STMF/OMF/braced-only systems do not need it. An undeclared
    # system with fixed-ended (moment) beams is treated as needing it.
    _fams = (_sf or {}).get("families") or []
    need358 = detailed and (any(f in ("SMF", "IMF") for f in _fams) or (("MF?" in _fams) if _sf else not E.is_braced(cfg)))
    has_conn = bool((pkg or {}).get("connections")); has_cap = bool((pkg or {}).get("capacity_design"))
    rows = []
    def row(item, required, ok, ev):
        rows.append([item, "required" if required else "n/a (AISC 341 does not apply)" if not detailed else "n/a (no SMF/IMF)",
                     ev, ("&mdash;" if not required else ("grounded" if ok else "<b>MISSING</b>"))])
    row("AISC 360-22 &mdash; member limit states", True, n("engineering_standards_A360") > 0 or cite_a360,
        f"{n('engineering_standards_A360')} queries" + (" + cited in calc_package" if cite_a360 else ""))
    row("AISC 360-22 Ch. J &mdash; connection design", True, has_conn,
        "connection designs recorded" if has_conn else "no connection designs recorded")
    row("AISC 341-22 &mdash; seismic detailing / capacity design", detailed,
        n("engineering_standards_A341") > 0 or has_cap or cite_a341,
        f"{n('engineering_standards_A341')} queries" + (" + capacity-design record" if has_cap else "")
        + (" + cited in calc_package" if cite_a341 else ""))
    row("AISC 358 &mdash; prequalified moment connections (SMF/IMF, incl. dual/mixed)", need358,
        n("engineering_standards_A358") > 0 or cite_a358,
        f"{n('engineering_standards_A358')} queries" + (" + cited in calc_package" if cite_a358 else ""))
    nmiss = sum(1 for r in rows if "MISSING" in r[3])
    head = ("<h3>Code-provision verification</h3>"
            f"<p>Whether the governing provisions of the standards this building's systems require were verified "
            f"against the text of the standards. R = {R} "
            + (("(&gt; 3 &mdash; AISC 341 ductile detailing applies)." if float(R) > 3.0 else
                "&mdash; AISC 341 applies to this seismic force-resisting system (the system row of the governing "
                "table requires its detailing).") if detailed
               else "(&le; 3 &mdash; system not detailed for seismic; AISC 341/358 do not apply, design per AISC 360).")
            + "</p>")
    tail = ("<p class='note'>Verification incomplete: the items marked MISSING were required for this building's "
            "systems but no look-up or calculation-package citation was found.</p>"
            if nmiss else "<p>Every standard this building's systems require was consulted (look-ups or "
            "calculation-package citations, above).</p>")
    rows = [[r[0], r[1], str(r[2]).replace("queries", "look-ups").replace("calc_package", "the calculation package"),
             str(r[3]).replace("grounded", "verified")] for r in rows]
    return head + _table(["Standard / scope", "Applies", "Evidence", "Status"], rows) + tail

def _appendix_case_figs(cases, cfg):
    """Which load-case indices get a per-combo N/V/M figure in Appendix B. Default: a fast
    representative set -- the gravity combos + the primary seismic combo per direction/sign -- so the
    report stays under the MCP client timeout. cfg['appendix_case_figures']=True renders ALL combos
    (slow; run in the background); =False renders none."""
    flag = cfg.get("appendix_case_figures", "subset")  # default: representative subset (gravity + primary seismic)
    if flag is True:
        return set(range(len(cases)))                # ALL combos (slower; rendered inline)
    if not flag:
        return set()                                 # explicit False / None -> none
    picked, seen = [], set()
    for idx, (label, fD, fL, fLr, lat, co) in enumerate(cases):
        if co:                                    # skip Omega0 column-only duplicates
            continue
        lab = str(label)
        if lab.startswith("1.4D") or "1.2D+1.6L" in lab:
            picked.append(idx)
        else:
            for dirn in ("X", "Y"):
                for sgn in ("+", "-"):
                    key = "E" + dirn + sgn
                    # the +L seismic cases start "(1.2+0.2SDS)D..." (labels now print the actual
                    # companion factor, e.g. "+0.5L", so match the gravity prefix, not "+L")
                    if ("rhoE" + dirn + sgn) in lab and lab.startswith("(1.2") and key not in seen:
                        seen.add(key); picked.append(idx)
        if len(picked) >= 6:
            break
    return set(picked[:6])


def _consistency_section(name, root, pkg):
    """Chapter 13 numerical self-consistency check (consistency.py): states exactly what was verified, lists the
    supplementary-module verdicts, then PASS or the items to reconcile."""
    try:
        import consistency
        issues = consistency.check(name, root=root, pkg=pkg, verbose=False)
        mods = consistency.module_verdicts(pkg)
    except Exception as ex:
        return f"<h3>Numerical self-consistency check</h3><p class='note'>[consistency check unavailable: {_esc(ex)}]</p>"
    what = ("<p>Automated verification of the calc package (all members and connections):</p><ol>"
            "<li><b>Completeness</b> &mdash; each member and connection records a limit state, a cited clause and a D/C.</li>"
            "<li><b>Strength</b> &mdash; no recorded D/C exceeds 1.0.</li>"
            "<li><b>Governing value</b> &mdash; each headline D/C equals the largest of its limit-state checks.</li>"
            "<li><b>Arithmetic</b> &mdash; each check's D/C equals its own printed demand &divide; available strength where "
            "both are recorded (H1-1a / H1-1b recomputed from its printed terms where the check cites them).</li>"
            "<li><b>Envelope</b> &mdash; for each member, the printed analysis envelope divided by the printed available "
            "strength does not exceed the headline D/C, and no check of a force quantity uses a demand below 98% of the "
            "printed envelope without a stated basis.</li>"
            "<li><b>One value</b> &mdash; a labelled quantity quoted more than once within an entry carries one value.</li>"
            "<li><b>Code-basis gates</b> &mdash; Seismic Design Category, Table 12.2-1 system and height limits, the "
            "seismic factors against the declared system, drift-limit vs Risk Category, and the guards for the "
            "conditions this building has (single-plate eccentricity, base-reaction envelope, composite floor, floor "
            "vibration, discontinuities with overstrength).</li>"
            "<li><b>Supplementary analyses</b> &mdash; module verdicts recorded in the package (listed below).</li></ol>"
            "<p class='cnote'>Not covered by these checks (left to the checking engineer): the choice of limit states and the code equations inside each "
            "capacity (spot-check Appendix A), drawings and specifications (Chapter 12), and engineering judgement.</p>")
    mtxt = ""
    if mods:
        mrows = [[_esc({"ddm_analysis": "Direct Design Method (system check)"}.get(k, _human_key(k)[0])),
                  ("<b class='ng'>%s</b>" % _esc(v)) if v in ("FAIL", "NG") else _esc(v or "not stated"),
                  "; ".join(_verdict_item(f) for f in fl[:4]) or "&mdash;"] for k, v, fl in mods]
        mtxt = "<h4>Supplementary analysis verdicts</h4>" + _table(["Module", "Verdict", "Failing combinations"], mrows)
    if not issues:
        return ("<h3>Numerical self-consistency check</h3>" + what + mtxt +
                "<p><b>Result: PASS</b> &mdash; no item to reconcile under checks 1&ndash;8.</p>")
    rows = [[str(i + 1), _pub_issue_html(iss)] for i, iss in enumerate(issues)]
    return ("<h3>Numerical self-consistency check</h3>" + what + mtxt +
            f"<p class='note'><b>Result: {len(issues)} item(s) to reconcile</b> before the report is final:</p>"
            + _table(["#", "Item"], rows))


def _gate_waivers_section(pkg):
    """Items the designer addressed by an engineering justification instead of the automated criterion
    (calc_package gate_waivers), summarised -- never dumped raw."""
    gw = (pkg or {}).get("gate_waivers") if isinstance(pkg, dict) else None
    if not isinstance(gw, dict) or not gw:
        return ""
    rows = []
    for k, v in gw.items():
        ks = str(k)
        lab = ("Self-consistency item: &ldquo;%s&hellip;&rdquo;" % _n(ks.split(":", 1)[1].strip()[:90])
               if ks.lower().startswith("consistency:") else _SANITY_NAMES.get(ks, _human_key(ks)[0]))
        rows.append([lab, _n(v)])
    return ("<h4>Items addressed by engineering justification</h4>"
            "<p class='cnote'>Automated criteria that the designer addressed by a stated justification:</p>"
            + _table(["Criterion", "Justification"], rows))


def _design_credit(pkg):
    """'Designed by <model> (<provider>) with Steltic, <date>' -- only when recorded."""
    da = (pkg or {}).get("design_agent") if isinstance(pkg, dict) else None
    if isinstance(da, str) and da.strip():
        da = {"model": da.strip()}
    if not isinstance(da, dict) or not da.get("model"):
        return ""
    harness = da.get("harness") or "Steltic"
    return ("Designed by %s%s with %s%s" %
            (_esc(da.get("model")), (" (%s)" % _esc(da["provider"])) if da.get("provider") else "", _esc(harness),
             (", %s" % _esc(da["date"])) if da.get("date") else ""))


def _design_basis(cfg):
    """Top-of-report summary of the building parameters used in the analysis model (feet-inch dimensions)."""
    NX, NY = cfg["NX"], cfg["NY"]; SX, SY = cfg["SX"], cfg["SY"]; H = cfg["heights"]; s = cfg.get("seis", {})
    xco, yco = cfg.get("xcoords"), cfg.get("ycoords")
    Lx = (xco[-1] if xco else NX * SX); Ly = (yco[-1] if yco else NY * SY)
    bays = ("%d &times; %d bays @ %s &times; %s" % (NX, NY, _ftin(SX), _ftin(SY))) if not (xco or yco) else \
        "%d &times; %d bays (variable spacing)" % (NX, NY)
    snow = float(cfg.get("snow", 0) or 0)
    rows = [
      ["Lateral system", _n(cfg.get("system") or cfg.get("arch", ""))],
      ["Plan grid", "%s (%s &times; %s overall)" % (bays, _ftin(Lx), _ftin(Ly))],
      ["Stories", "%d: %s (h<sub>n</sub> = %s)" % (len(H), _heights_txt(H), _ftin(sum(float(h) for h in H)))
                  if not cfg.get("hn_ft") else
                  "%d: %s (model height %s; h<sub>n</sub> = %s ft above the seismic base%s)" % (
                      len(H), _heights_txt(H), _ftin(sum(float(h) for h in H)), _n(float(cfg["hn_ft"])),
                      (", " + _esc(str(cfg.get("hn_basis")))) if cfg.get("hn_basis") else "")],
      ["Gravity loads", (("floor D %s / L %s psf; " % (_n(cfg.get("D_floor")), _n(cfg.get("L_floor"))))
                         if len(H) > 1 or cfg.get("top_is_floor") else "")      # one-story: roof only
                        + "roof D %s psf, L<sub>r</sub> %s psf%s"
                        % (_n(cfg.get("D_roof")), _n(cfg.get("Lr", 20.0)), (", S %s psf" % _n(snow)) if snow > 0 else "")],
      ["Seismic", "S<sub>DS</sub> = %s g, S<sub>D1</sub> = %s g; R / C<sub>d</sub> / &Omega;<sub>0</sub> = %s; I<sub>e</sub> = %s"
                  % (_sa(s.get("SDS")), _sa(s.get("SD1")), _rcdo_txt(cfg, s), _fac(s.get("Ie", 1.0)))],
    ]
    if cfg.get("wind"):
        w = cfg["wind"]
        rows.append(["Wind", "V = %s mph, Exposure %s" % (_n(w.get("V")), _esc(w.get("exposure", "C")))])
    return ("<h2>Design basis</h2><p class='cnote'>Building parameters used in the analysis model; the governing "
            "criteria are in Chapter 1.</p>" + _table(["Parameter", "Value"], rows))


def _all_sections(cfg, pkg):
    secs = set()
    try:
        for e in E.build(cfg, "Linear").get("ele", []):
            secs.add(str(e[2]).upper())
    except Exception:
        pass
    for m in ((pkg or {}).get("members") or []) if isinstance(pkg, dict) else []:
        if isinstance(m, dict) and (m.get("inputs") or {}).get("section"):
            secs.add(str(m["inputs"]["section"]).upper())
    return secs


def _cvn_rows(cfg, pkg, secs):
    """Notch-toughness rows for heavy sections: AISC 341-22 A3.3 (SFRS: rolled shapes with flanges >= 1-1/2 in and
    plates >= 2 in in built-up members / inelastic connection plates / BRB cores: CVN 20 ft-lbf at 70 F) and
    AISC 360-22 A3.1c-d (rolled shapes with flanges > 2 in and built-up plates > 2 in in tension with CJP splices)."""
    heavy, thick, plates = [], [], []
    # only sections that are built: with a calc package, a section must appear as a member section there (analysis
    # stand-ins such as a truss modelled as a W44 are not built); cfg['cvn_exclude'] removes others (e.g. gravity girders)
    _built = None
    if isinstance(pkg, dict) and isinstance(pkg.get("members"), list) and pkg["members"]:
        _built = {str((m.get("inputs") or {}).get("section") or "") for m in pkg["members"] if isinstance(m, dict)}
    _gonly = set()
    if isinstance(pkg, dict) and isinstance(pkg.get("members"), list):
        _roles = {}
        for m in pkg["members"]:
            if isinstance(m, dict):
                sec_ = str((m.get("inputs") or {}).get("section") or "")
                _roles.setdefault(sec_, set()).add(_role_of(m.get("id", ""), m.get("inputs", {}) or {}))
        _gonly = {k for k, v in _roles.items() if v and v <= {"gravity_col"}}      # gravity columns: not SFRS
    _excl = {str(x) for x in (cfg.get("cvn_exclude") or [])}
    secs = [x for x in secs if (_built is None or x in _built) and x not in _excl]
    for s in sorted(secs):
        try:
            p = S.props(s, SEC=E.SEC)
        except Exception:
            p = None
        tf = (p or {}).get("tf")
        rolled = str(s).upper() in S._load_csv()            # built-up boxes / plate girders are plates (A3.3 second item)
        if rolled and tf and tf >= 1.5 - 1e-6:
            heavy.append(s)
            if tf > 2.0 + 1e-6:
                thick.append(s)
        m = re.match(r"BOX\d+(?:\.\d+)?X\d+(?:\.\d+)?X(\d+(?:\.\d+)?)$", s) or re.match(r"BOX\d+X(\d+(?:\.\d+)?)$", s)
        if m and float(m.group(1)) >= 2.0:
            plates.append(s)
    if not (heavy or plates):
        return []
    try:
        a341 = any(bool(_PIPE_A341(cfg, d)) for d in ("X", "Y"))
    except Exception:
        a341 = float((cfg.get("seis") or {}).get("R") or 0) > 3.0
    rows = []
    heavy341 = [x for x in heavy if x not in _gonly]
    if a341 and (heavy341 or plates):
        heavy = heavy341
        rows.append(["Notch toughness, heavy sections in the SFRS (AISC 341-22 A3.3)",
                     "rolled shapes with flanges &ge; 1-1/2 in.%s: CVN &ge; 20 ft-lbf at 70&deg;F, alternate core location "
                     "(ASTM A6 Supplementary Requirement S30)%s"
                     % ((" (" + ", ".join(_esc(x) for x in heavy) + ")") if heavy else "",
                        ("; plates &ge; 2 in. in built-up members (" + ", ".join(_esc(x) for x in plates) +
                         "): CVN &ge; 20 ft-lbf at 70&deg;F (ASTM A673, Frequency P)") if plates else "")])
    if thick or plates:
        rows.append(["Notch toughness, heavy shapes and plates in tension (AISC 360-22 A3.1c, A3.1d)",
                     "shapes with flanges over 2 in. and built-up plates over 2 in. thick used as tension members or "
                     "with CJP-welded tension splices: CVN &ge; 20 ft-lbf at 70&deg;F"])
    return rows


def _PIPE_A341(cfg, d):
    import design_pipeline as _DP
    return _DP.aisc341_applies(cfg, d)


def _materials_table(cfg, pkg=None):
    """Material specifications: cfg['materials'] when declared, else inferred from the sections used and from the
    connection / composite records (W: A992; HSS: A500 Gr. C; plates A572 Gr. 50 (A36 where recorded); bolts ASTM
    F3125 Grade A325 / A490 as recorded; anchor rods F1554 grades recorded; welds E70XX; concrete where recorded)."""
    decl = cfg.get("materials")
    rows = []
    if isinstance(decl, dict) and decl:
        rows = [[_n(k), _n(v) if not isinstance(v, dict) else _fmt(v)] for k, v in decl.items()]
    elif isinstance(decl, (list, tuple)) and decl:
        for x in decl:
            if isinstance(x, (list, tuple)):
                rows.append([_n(x[0]), " ".join(_n(y) for y in x[1:])])
            elif isinstance(x, dict):
                rows.append([_n(x.get("item", x.get("element", ""))), _n(x.get("spec", x.get("value", "")))
                             + ((" &mdash; " + _n(x.get("props"))) if x.get("props") else "")])
    if rows:
        rows += _cvn_rows(cfg, pkg, _all_sections(cfg, pkg))
        return _table(["Element", "Specification"], rows) + "<p class='cnote'>E = 29,000 ksi for all structural steel.</p>"
    secs = _all_sections(cfg, pkg)
    blob = ""
    try:
        blob = json.dumps({k: (pkg or {}).get(k) for k in ("connections", "composite_design", "capacity_design")}) \
            if isinstance(pkg, dict) else ""
    except Exception:
        blob = ""
    blob_l = blob.lower()
    if any(s.startswith("W") for s in secs) or not secs:
        rows.append(["Wide-flange shapes", "ASTM A992, F<sub>y</sub> = 50 ksi, F<sub>u</sub> = 65 ksi"])
    rect = [s for s in secs if s.startswith("HSS") and s.count("X") >= 2]
    rnd = [s for s in secs if s.startswith("HSS") and s.count("X") == 1]
    if rect:
        rows.append(["Rectangular / square HSS", "ASTM A500 Grade C, F<sub>y</sub> = 50 ksi, F<sub>u</sub> = 62 ksi"])
    if rnd:
        rows.append(["Round HSS", "ASTM A500 Grade C, F<sub>y</sub> = 46 ksi, F<sub>u</sub> = 62 ksi"])
    if any(s.startswith(("L", "2L")) for s in secs) or re.search(r"\b2?l\s?\d+x\d+", blob_l):
        rows.append(["Angles", "ASTM A36, F<sub>y</sub> = 36 ksi, F<sub>u</sub> = 58 ksi (or A572 Gr. 50 where noted)"])
    pl = ["ASTM A572 Grade 50 (F<sub>y</sub> = 50 ksi, F<sub>u</sub> = 65 ksi)"]
    if "a36" in blob_l:
        pl.append("ASTM A36 (F<sub>y</sub> = 36 ksi, F<sub>u</sub> = 58 ksi) where noted in the connection design")
    rows.append(["Plates and bars", "; ".join(pl)])
    bolts = [g for g in ("A325", "A490") if g.lower() in blob_l] or ["A325"]
    try:
        _a341m = any(bool(_PIPE_A341(cfg, d)) for d in ("X", "Y"))
    except Exception:
        _a341m = False
    rows.append(["High-strength bolts", "ASTM F3125 Grade %s; %s" % (" / ".join(bolts),
                 "bolts in the seismic force-resisting system pretensioned with Class A (or better) faying surfaces "
                 "(AISC 341-22 D2.2), other bolts snug-tight unless noted" if _a341m else "snug-tight or pretensioned as noted")])
    fg = sorted({m for m in re.findall(r"f1554\s*(?:gr(?:ade)?\.?\s*)?(36|55|105)", blob_l)}, key=int)
    rows.append(["Anchor rods", "ASTM F1554 Grade %s" % " / ".join(fg) if fg else
                 "ASTM F1554 (grade per the column-base design, Chapter 10)"])
    rows.append(["Welds", "E70XX electrodes (F<sub>EXX</sub> = 70 ksi), AWS D1.1%s" % (
        "; welds in the seismic force-resisting system per AWS D1.8, demand-critical welds with filler metal per "
        "AISC 341-22 A3.4b" if _a341m else "")])
    comp = "composite" in str(cfg.get("floor_system", "")).lower() or bool((pkg or {}).get("composite_design")) \
        if isinstance(pkg, dict) else "composite" in str(cfg.get("floor_system", "")).lower()
    if comp:
        cc = cfg.get("concrete") if isinstance(cfg.get("concrete"), dict) else {}
        txt = (json.dumps((pkg or {}).get("composite_design") or {}) + " " + str(cfg.get("floor_system", ""))) \
            if isinstance(pkg, dict) else str(cfg.get("floor_system", ""))
        fc = cc.get("fc_ksi") or (re.search(r"f'?c'?\s*=\s*(\d+(?:\.\d+)?)\s*ksi", txt, re.I) or [None, None])[1]
        wc = cc.get("wc_pcf") or (re.search(r"(\d{3})\s*pcf", txt, re.I) or [None, None])[1]
        lw = bool(re.search(r"\blw\b|lightweight|light-weight", txt, re.I)) or str(cc.get("type", "")).lower().startswith("l")
        spec = "%s concrete on composite steel deck" % ("lightweight" if lw else "normal-weight")
        if fc: spec += ", f&prime;<sub>c</sub> = %s ksi" % _n(float(fc))
        if wc: spec += ", w<sub>c</sub> = %s pcf" % _n(float(wc))
        if not fc:
            spec += " (f&prime;<sub>c</sub> per the slab specification)"
        rows.append(["Floor slab", spec])
        if re.search(r"\bstud", blob_l):
            rows.append(["Steel headed stud anchors", "ASTM A108, F<sub>u</sub> = 65 ksi (AWS D1.1 Type B)"])
    rows += _cvn_rows(cfg, pkg, secs)
    return (_table(["Element", "Specification"], rows)
            + "<p class='cnote'>E = 29,000 ksi for all structural steel. The specifications listed are those of the sections "
            "and connection records of this design; the project specification governs.</p>")


def _floor_vibration_section(cfg, pkg):
    """AISC Design Guide 11 walking vibration: calc_package['floor_vibration'] when recorded, else the member checks
    that address it; office floors without either are stated as not evaluated."""
    fv = (pkg or {}).get("floor_vibration") if isinstance(pkg, dict) else None
    h = []
    if fv:
        h.append("<h3>Floor vibration (AISC Design Guide 11, walking excitation)</h3>")
        h.append(_record_html(fv))
        return "".join(h)
    rows = []
    C = _consistency_mod()
    for m in ((pkg or {}).get("members") or []) if isinstance(pkg, dict) else []:
        if not isinstance(m, dict):
            continue
        for k in m.get("checks") or []:
            if isinstance(k, dict) and re.search(r"vibrat|dg ?11|walking|footfall", str(k.get("limit_state", "")), re.I):
                terms = C.check_terms(k) if C else []
                rows.append([_esc(m.get("id")), _n(k.get("limit_state")), _term_txt(terms, "dem"), _term_txt(terms, "cap"),
                             _n(C._dc_num(k.get("DC")) if C else k.get("DC"), dc=True)])
    if rows:
        return ("<h3>Floor vibration (AISC Design Guide 11, walking excitation)</h3>"
                + _table(["Member", "Check", "Response", "Criterion", "Ratio"], rows))
    arch = (str(cfg.get("arch", "")) + " " + str(cfg.get("occupancy", ""))).lower()
    if len(cfg.get("heights") or []) == 1:
        return ("<p class='cnote'><b>Floor vibration (AISC Design Guide 11):</b> not applicable &mdash; one-story "
                "building, roof only (no occupied floor framing).</p>")
    return ("<p class='cnote'><b>Floor vibration (AISC Design Guide 11):</b> not evaluated in this package%s.</p>"
            % ("; for office floors the walking criterion a<sub>p</sub>/g &le; 0.5% applies" if "office" in arch else ""))


def fig_story_shear_otm_dirs(cfg, series):
    """Story shear and overturning moment for several load patterns {label: {level: F}} (both directions,
    wind and seismic)."""
    NF = len(cfg["heights"]); z = E.zlevels(cfg); lvl = list(range(1, NF + 1))
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 5))
    for lab, F in series.items():
        Vs = [sum(F.get(k, 0.0) for k in range(s, NF + 1)) for s in range(1, NF + 1)]
        OT = [sum(F.get(k, 0.0) * (z[k] - z[s - 1]) / 12.0 for k in range(s, NF + 1)) for s in range(1, NF + 1)]
        a1.step(Vs, lvl, where="mid", label=lab); a2.plot(OT, lvl, "-o", ms=3, label=lab)
    a1.set_xlabel("story shear (kip)"); a1.set_ylabel("story"); a1.grid(alpha=0.3); a1.set_title("Story shear")
    a2.set_xlabel("overturning moment (kip-ft)"); a2.set_title("Overturning moment (above the story)"); a2.grid(alpha=0.3)
    a1.legend(fontsize=8); a2.legend(fontsize=8)
    return _b64(fig)


def _base_shear_otm_table(cfg, SD, wind):
    """Chapter 7: base shear and base overturning moment per direction, seismic (ELF) and wind (Case 1)."""
    NF = len(cfg["heights"]); z = E.zlevels(cfg)
    def otm(F):
        return sum(F.get(k, 0.0) * z[k] / 12.0 for k in range(1, NF + 1))
    rows = []
    for d in ("X", "Y"):
        Fs = (SD or {}).get(d, {}).get("Fx") or {}
        Fw = (wind or {}).get(d) or {}
        Vs, Ms = sum(Fs.values()), otm(Fs)
        Vw, Mw = (sum(Fw.values()), otm(Fw)) if Fw else (None, None)
        gov = "seismic" if Vw is None or Vs >= Vw else "wind"
        rows.append([d, _n(Vs), _n(Ms), _n(Vw) if Vw is not None else "&mdash;", _n(Mw) if Mw is not None else "&mdash;",
                     "<b>%s</b>" % gov])
    return _table(["Direction", "Seismic V (kip)", "Seismic base OTM (kip-ft)", "Wind V (kip)",
                   "Wind base OTM (kip-ft)", "Governs"], rows)


def _report_ctx(cfg, pkg, sf, cases):
    """Everything the chapter wording depends on: AISC 341 applicability per direction (calc package record of the
    demand run first, then design_pipeline.aisc341_applies), AISC 358 (SMF / IMF), wind, DG11, discontinuities and
    the combination-set content."""
    da = ((pkg or {}).get("demand_analysis") or {}) if isinstance(pkg, dict) else {}
    rec = da.get("aisc341_applies")
    a341 = {}
    for d in ("X", "Y"):
        if isinstance(rec, dict) and d in rec:
            a341[d] = bool(rec[d])
        else:
            try:
                a341[d] = bool(PIPE.aisc341_applies(cfg, d)) if HAVE_PIPE and hasattr(PIPE, "aisc341_applies") else \
                    float(E.seis_dir(cfg, d).get("R") or 0) > 3.0
            except Exception:
                a341[d] = float((cfg.get("seis") or {}).get("R") or 0) > 3.0
    fams = (sf or {}).get("families", []) if sf else []
    disc = None
    try:
        if sf and sf.get("inv_ok"):
            term, inpl = _discontinuities(cfg, sf)
            disc = bool(term or inpl)
    except Exception:
        disc = None
    dg11 = bool((pkg or {}).get("floor_vibration")) if isinstance(pkg, dict) else False
    if not dg11 and isinstance(pkg, dict):
        dg11 = bool(re.search(r"dg ?11|design guide 11", json.dumps(pkg.get("members") or [])[:400000], re.I))
    return {"a341_dirs": a341, "a341": any(a341.values()),
            "mf358": any(a341.values()) and any(f in ("SMF", "IMF") for f in fams),
            "smf": "SMF" in fams, "wind": bool(cfg.get("wind")), "dg11": dg11,
            "discontinuity": disc, "cases_info": _cases_info(cases or [])}


def _qa_annex(name, cfg, pkg, path):
    """Separate QA annex (qa_annex.html next to report.html): design-process records that are not part of the
    published calculation package -- reference-grounding verification, notes on the reference corpus, and the
    activity log of the tool calls."""
    parts = ["<h1>%s &mdash; QA annex</h1>" % _esc(cfg.get("project") or name),
             "<p class='cnote'>Design-process records supporting Chapter 13 of the report. Not part of the calculation "
             "package.</p>"]
    cr = _design_credit(pkg)
    if cr:
        parts.append("<p class='credit'>%s</p>" % cr)
    try:
        parts.append(_grounding_check(cfg, name, pkg))
    except Exception as ex:
        parts.append("<p class='note'>[grounding verification unavailable: %s]</p>" % _esc(ex))
    cdf = (pkg or {}).get("corpus_defects") if isinstance(pkg, dict) else None
    if cdf:
        parts.append("<h2>Reference-text notes recorded during the design</h2>" + _record_html(cdf))
    try:
        parts.append(_activity_section(name))
    except Exception as ex:
        parts.append("<p class='note'>[design log unavailable: %s]</p>" % _esc(ex))
    html = ("<!doctype html><html><head><meta charset='utf-8'><title>%s QA annex</title><style>%s%s</style></head>"
            "<body>%s</body></html>" % (_esc(cfg.get("project") or name), CSS, CHK_CSS, "".join(parts)))
    open(path, "w", encoding="utf-8").write(html)
    return path


def build_report(name, root=None):
    _register(name); cfg = E.CFG[name]
    if root is None:
        root = os.path.join(os.environ.get("STEEL_BUILDER_JOBS") or HERE, name)
    pub = _publish(cfg)
    NF = len(cfg["heights"]); s = cfg["seis"]
    SX, SY = cfg["SX"], cfg["SY"]; NX, NY = cfg["NX"], cfg["NY"]
    Lx = (cfg["xcoords"][-1] if cfg.get("xcoords") else NX*SX); Ly = (cfg["ycoords"][-1] if cfg.get("ycoords") else NY*SY)
    Htot = E.zlevels(cfg)[-1]
    pkg, pkgsrc = _load_pkg(name, root)
    pkg = pkg if isinstance(pkg, dict) else {}
    _QA.clear()
    try: _PKG_RBS[0] = bool(re.search(r"\bRBS\b|reduced[- ]beam[- ]section", json.dumps(pkg or {}), re.I))
    except Exception: _PKG_RBS[0] = False
    figdir = os.path.join(root, "figs"); os.makedirs(figdir, exist_ok=True)
    global _FIGDIR; _FIGDIR = figdir; _FIGSEQ[0] = 0
    pre = []
    try:
        import plot_model as PM
        PM.figures(name, figdir, deformed_fig=bool(cfg.get("deformed_shape_figure")))
        # Figure 2 (member orientation) is REQUIRED -- retry once on its own if missing
        if not os.path.exists(os.path.join(figdir, f"{name}_orientation.png")):
            try: PM.orientation(name, figdir)
            except Exception as _oex:
                pre.append(f"<p class='note'>[member orientation figure could not be produced: {_esc(_oex)}]</p>")
    except Exception as ex:
        pre.append(f"<p class='note'>[model figures could not be produced: {_esc(ex)}]</p>")

    # ---- shared computations (used across several chapters) ----
    Fx = None; T = eX = eY = None; Cs = V = Tu = Ta = W = None
    try: T, eX, eY, Cs, V, Tu, Ta, Fx, W = _seismic(cfg)
    except Exception as ex: pre.append(f"<p class='note'>[seismic analysis failed: {_esc(ex)}]</p>")
    try:
        cases = load_cases(cfg)
    except Exception as ex:
        cases = []; pre.append(f"<p class='note'>[load combinations failed: {_esc(ex)}]</p>")
    windhtml, VwX, VwY = _wind_section(cfg, cases)
    FwX = FwY = None
    if cfg.get("wind"):
        try: FwX, FwY = E.wind_forces(cfg, "X"), E.wind_forces(cfg, "Y")
        except Exception: FwX = FwY = None
    print("[%s] report: model + ASCE 7 loads + modal done; rendering chapters ..." % name)
    reX = reY = None; runs = None
    _INV_CACHE.clear()
    try: sf = _sfrs(cfg)
    except Exception as ex:
        sf = None; pre.append(f"<p class='note'>[SFRS inventory failed: {_esc(ex)}]</p>")
    try: SD = _seismic_by_dir(cfg)
    except Exception: SD = None
    _FxX = _FxY = None
    if Fx is not None:
        _FxX, _FxY = _fx_by_dir(Fx)
        try: runs = _lateral_runs(cfg, Fx, sf)
        except Exception as ex: pre.append(f"<p class='note'>[drift / torsion runs failed: {_esc(ex)}]</p>")
        if runs is not None:
            _FxX, _FxY = runs["X"]["F"], runs["Y"]["F"]
        try:
            _, _dX, _drX, reX = _run_case(cfg, "X", _FxX); _, _dY, _drY, reY = _run_case(cfg, "Y", _FxY)
        except Exception as ex: pre.append(f"<p class='note'>[equilibrium run failed: {_esc(ex)}]</p>")
    ctx = _report_ctx(cfg, pkg, sf, cases)
    _CH_CTX[0] = ctx
    _COMBO_LABELS[0] = [str(c[0]) for c in cases] if cases else None
    a341 = ctx["a341"]
    _want_summary = bool(cfg.get("force_summary")); _want_casefigs = bool(cfg.get("appendix_case_figures"))

    title = cfg.get("project") or cfg.get("project_name") or name
    credit = _design_credit(pkg)
    parts = [f"<h1>{_esc(title)} &mdash; structural analysis and design calculations</h1>",
             f"<p class='lead'><b>{_esc(cfg.get('arch', ''))}</b> &middot; {datetime.date.today():%B %d, %Y}</p>"]
    if credit:
        parts.append(f"<p class='credit'>{credit}</p>")
    parts += pre
    parts += [_toc(ctx, appendix_b=_want_summary), _design_basis(cfg)]

    # ============================== Chapter 1 — Design basis & codes ==============================
    parts.append(_chapter(1))
    parts.append(_design_basis_codes(cfg, s, root, pkg, ctx))
    parts.append("<h3>Building description</h3>")
    md = cfg.get("model") or {}
    parts.append(_table(["Item", "Value"], [
        ["Description", _esc(cfg.get("arch", ""))],
        ["Stories", (_esc(str(cfg["stories_text"])) if cfg.get("stories_text") else
                     "%d (%s)" % (NF, _heights_txt(cfg["heights"])))],
        ["Plan", "%s (X) &times; %s (Y)" % (_ftin(Lx), _ftin(Ly))],
        ["Bays", ("%d &times; %d @ %s (X) / %s (Y)" % (NX, NY, _ftin(SX), _ftin(SY))) if not (cfg.get("xcoords") or cfg.get("ycoords"))
         else "%d &times; %d (variable spacing)" % (NX, NY)],
        ["Structural height h<sub>n</sub>", (("%s above the seismic base (model height %s)" % (_ftin(float(cfg["hn_ft"]) * 12.0),
                                                _ftin(Htot))) if cfg.get("hn_ft") else _ftin(Htot))],
        ["Floor system", _n(cfg.get("floor_system")) if cfg.get("floor_system") else "&mdash;"],
        ["Column bases", _esc(md.get("bases") or cfg.get("base", "fixed"))],
        ["Beam-to-column joints", _esc(md.get("joints") or ("pinned" if cfg.get("releases") else "rigid"))],
        ["Lateral system", ("X: %s; Y: %s" % (_sys_label(sf, "X"), _sys_label(sf, "Y"))) if sf else "&mdash;"],
    ]))
    parts.append("<h3>Materials</h3>" + _materials_table(cfg, pkg))
    parts.append("<h3>Geometry</h3>")
    try: parts.append(_img(fig_plan(cfg), "Plan grid &mdash; the (i, j) labels used in the reaction and force tables"))
    except Exception as ex: parts.append(f"<p class='note'>[plan figure failed: {_esc(ex)}]</p>")
    parts.append(_img(_png_file_b64(os.path.join(figdir, f"{name}_orientation.png")),
                      "Section orientation (web/depth ticks &mdash; beam strong axis vertical)"))

    # ====================== Chapter 2 — Structural system & load path =======================
    parts.append(_chapter(2))
    if sf:
        parts.append(_system_loadpath(cfg, sf, pkg))
        parts.append("<h3>Seismic force-resisting system (each principal direction)</h3>")
        parts.append(_lfrs_table(cfg, sf, pkg))
    parts.append("<h3>Lateral-load design inputs</h3>"); parts.append(_lateral_inputs_table(cfg))
    if cfg.get("deformed_shape_figure"):   # OFF the default workflow (viewer shows the same live)
        parts.append(_img(_png_file_b64(os.path.join(figdir, f"{name}_deformed_X.png")),
                          "Deformed shape under lateral X (continuous lateral load path)"))
    elif not pub:
        parts.append("<p class='note'>[internal] Static deformed-shape figure not rendered (deformed_shape_figure off).</p>")
    try:
        # The standalone 3D viewer (viewer_3d.html) is WRITTEN here as a side effect -- the frontend
        # "View model" button and the job download need the file even though the report no longer
        # embeds a viewer section (removed by request). Do not drop this call again.
        import viewer3d
        viewer3d.report_section(cfg, name, root)   # returns markup we deliberately discard
    except Exception as ex:
        print(f"[report] viewer_3d.html generation failed: {ex}")
    if Fx is not None and sf:
        parts.append("<h3>Horizontal force distribution to the lateral frames</h3>")
        try: parts.append(_horizontal_distribution(cfg, Fx, VwX, VwY, sf, runs))
        except Exception as ex: parts.append(f"<p class='note'>[distribution failed: {_esc(ex)}]</p>")
        irr_html = None                     # the screen runs first so the diaphragm text knows Types 2-5
        if runs is not None:
            try: irr_html = _irregularity_section(cfg, runs, sf)
            except Exception as ex: irr_html = f"<p class='note'>[irregularity screen failed: {_esc(ex)}] &mdash; irregularities NOT EVALUATED</p>"
        parts.append("<h3>Diaphragm classification and design force (ASCE 7-22 &sect;12.10)</h3>")
        try:                                # Eq. 12.10-1 with each direction's own ELF story forces
            try:
                _sdf = E.seismic_design_forces(cfg)
                _fxd = tuple({int(k): float(v) for k, v in _sdf[d]["F_elf"].items()} for d in ("X", "Y"))
            except Exception:
                _fxd = tuple(_fx_by_dir(Fx))
            parts.append(_diaphragm_section(cfg, _fxd, (FwX, FwY) if FwX else None, pkg))
        except Exception as ex: parts.append(f"<p class='note'>[diaphragm section failed: {_esc(ex)}]</p>")
        parts.append("<h3>Plan and vertical irregularity screening</h3>")
        parts.append(irr_html or "<p class='note'>Irregularity screen NOT EVALUATED (the drift/torsion runs failed).</p>")
    else:
        parts.append("<p class='cnote'>Diaphragm design force and irregularity screening need the seismic "
                     "analysis (Chapter 3).</p>")

    # ================================= Chapter 3 — Loads ==================================
    parts.append(_chapter(3))
    parts.append("<h3>Gravity loads</h3>"); parts.append(_gravity_loads_table(cfg, pkg))
    parts.append(_gravity_loads_note(cfg, pkg))
    parts.append("<h3>Wind loads (ASCE 7-22 Ch. 26&ndash;27)</h3>"); parts.append(windhtml)
    if cfg.get("wind"):
        parts.append(_wind_cc_note())
    parts.append("<h3>Seismic loads (ASCE 7-22 Ch. 11&ndash;12)</h3>")
    if Fx is not None:
        try: parts.append(_seismic_loads_section(cfg, T, eX, eY, Cs, V, Tu, Ta, Fx, W))
        except Exception as ex: parts.append(f"<p class='note'>[seismic load section failed: {_esc(ex)}]</p>")
    else:
        parts.append("<p class='note'>[seismic data unavailable]</p>")
    parts.append("<h3>Governing lateral load per direction</h3>")
    parts.append(_governing_lateral(cfg, V, VwX, VwY, SD))

    # ========================== Chapter 4 — Load combinations ============================
    parts.append(_chapter(4))
    case_detail_parts = []
    try:
        parts.append(_combo_notes(cfg, cases, ctx))
        parts.append("<h3>Combinations analysed (load factors)</h3>")
        parts.append(_combo_table(cases))
        parts.append(_combo_legend(cfg, cases, pkg, ctx))
        # --- opt-in items (each requestable on its own) -------------------
        #   cfg['force_summary']          -> the per-combination force SUMMARY table (heavy all-combo solve)
        #   cfg['appendix_case_figures']  -> the Appendix-B per-combo N/V/M FIGURES (heavy; figs in the same loop)
        #   cfg['force_diagrams']         -> the Chapter-5 governing N/V/M frame DIAGRAMS (light; rendered below)
        if Fx is not None and (_want_summary or _want_casefigs):
            # RESUMABLE per-combo cache (design/_case_cache.pkl): each solved combo is saved as soon as
            # it finishes, so an interrupted render resumes instead of restarting the solve loop.
            import pickle as _pk, hashlib as _hl, os as _os
            _cache_p = _os.path.join(root, "design", "_case_cache.pkl")
            _fp = _hl.md5(repr(sorted((k, str(v)) for k, v in cfg.items()
                                      if not callable(v))).encode()).hexdigest()
            try:
                _cc = _pk.load(open(_cache_p, "rb"))
                if _cc.get("_fingerprint") != _fp:
                    _cc = {"_fingerprint": _fp}
            except Exception:
                _cc = {"_fingerprint": _fp}
            def _cc_save():
                try:
                    _os.makedirs(_os.path.dirname(_cache_p), exist_ok=True)
                    _tmp = _cache_p + ".tmp~"
                    with open(_tmp, "wb") as _f:
                        _pk.dump(_cc, _f)
                    _os.replace(_tmp, _cache_p)
                except Exception:
                    pass
            srows = []
            for _ci, combo in enumerate(cases):
                label, fD, fL, fLr, lat, col_only = combo
                if col_only:        # Omega0 / E_cl cases: member-specific -> reported in the member schedule (Ch.6)
                    if _want_summary:
                        _kd = getattr(combo, "kind", "")
                        _what = ("capacity-limited E<sub>cl</sub> (see Ch. 6)" if _kd == "ecl" else
                                 "OCBF V-beam F1.4a(a) (see Ch. 6)" if _kd == "ecl_ocbf" else
                                 "&Omega;<sub>0</sub> column overstrength, axial (see Ch. 6)")
                        srows.append([_combo_label(label), "&mdash;", _what, "&mdash;", ""])
                    continue
                try:
                    _ckey = (str(label), bool(_want_casefigs))
                    if _ckey in _cc:
                        rows, (Nv, Nk, Ns), (Mv, Mk, Ms), uri, d = _cc[_ckey]
                    else:
                        rows, (Nv, Nk, Ns), (Mv, Mk, Ms), uri, d = _static_case_table(cfg, combo, render_fig=_want_casefigs)
                        _cc[_ckey] = (rows, (Nv, Nk, Ns), (Mv, Mk, Ms), uri, d)
                        _cc_save()
                    if _want_summary:
                        srows.append([_combo_label(label), _n(float(Nv)), _esc(f"{Nk} {Ns}".strip()), _n(float(Mv) / 12.0),
                                      _esc(f"{Mk} {Ms}".strip())])
                        case_detail_parts.append(f"<h4>Load combination: {_combo_label(label)}</h4><p>{_case_desc(label, col_only, a341)}</p>")
                        case_detail_parts.append(_table(
                            ["member", "section", "N (kip)", "M<sub>x</sub> (kip-ft)", "M<sub>y</sub> (kip-ft)", "V (kip)",
                             "location (i,j / level)"], rows))
                    if uri and _want_casefigs:
                        _save_case_fig(uri, figdir, label)   # -> figs/case_<label>.png (NOT embedded)
                except Exception as ex:
                    if _want_summary:
                        srows.append([_esc(label), "&mdash;", f"[failed: {_esc(ex)}]", "&mdash;", ""])
            if _want_summary:
                parts.append("<h3>Load-combination force summary</h3>")
                parts.append("<p>One row per combination: the largest axial force and the largest bending moment anywhere "
                             "in the structure, with the member type / section that carries it (static model with the "
                             "distributed gravity loads). Member forces per combination are in Appendix B.</p>")
                parts.append(_table(["Load combination", "Max axial N (kip)", "carried by", "Max moment M (kip-ft)",
                                     "carried by"], srows))
        elif Fx is not None:
            parts.append("<p class='cnote'>The member design forces are the envelopes over all combinations listed above "
                         "(Chapter 6 and Appendix A).</p>")
    except Exception as ex:
        parts.append(f"<p class='note'>[load combinations failed: {_esc(ex)}]</p>")

    # ======================= Chapter 5 — Analysis model fidelity =========================
    print("[%s] report: chapter 5 (analysis-model fidelity) ..." % name)
    parts.append(_chapter(5))
    rel = cfg.get("releases"); cb = cfg.get("custom_build")
    jt = ("as defined in the analysis model, member by member (releases per the declared joint types)" if cb else
          "rigid / continuous except where moment releases are set" if rel else "all rigid / continuous (no releases)")
    parts.append("<h3>Modelling assumptions</h3>")
    parts.append(_table(["Assumption", "As modelled"], [
        ["Column bases", _esc(md.get("bases") or cfg.get("base", "fixed"))],
        ["Beam-column and brace joints", jt],
        ["Floor diaphragm", {"flexible": "flexible diaphragm idealization (&sect;12.3.1.1): the in-plane constraint ties "
                             "each level for the global solution only; the vertical elements are designed for the "
                             "tributary-area line forces",
                             "mixed": "floors: rigid in-plane diaphragm, story masses and forces at the centre of mass; "
                                      "roof: flexible (&sect;12.3.1.1), roof force to the vertical elements by tributary area"
                             }.get(_diaphragm_kind(cfg),
                                   "rigid in-plane diaphragm; story masses and forces at the centre of mass of each level")],
        ["Geometric transformation", "P-&Delta; (second order) on the columns"],
        ["Leaning gravity columns", "yes" if cfg.get("lean_gravity") else "framed into the floor system (pinned beams where released)"],
        ["Gravity loads for member forces", "static model with distributed loads on sub-divided beams: " + _floor_mode_txt(cfg, pkg)],
    ]))
    try:
        _bs = str(md.get("bases") or cfg.get("base") or "pinned")
        _jt = str(md.get("joints") or ("pinned" if cfg.get("releases") else "rigid"))
        _cap = f"Modelled joint and base fixity &mdash; column bases: {_esc(_bs)}; girder joints: {_esc(_jt)}"
        try:
            if E.is_braced(cfg): _cap += "; braces pin-ended (axial-only truss members)"
        except Exception: pass
        if cfg.get("lean_gravity"): _cap += "; leaning gravity columns"
        parts.append(_img(_joint_figure(cfg), _cap + ".", full=True))
    except Exception as _jex: parts.append(f"<p class='note'>[joint figure failed: {_esc(_jex)}]</p>")
    _jmodel = None
    if cfg.get("custom_build"):
        try:
            import static_model as SM; _jmodel = SM.build_static(cfg, "Linear", 2)
        except Exception:
            _jmodel = None
    for _d in ("X", "Y"):
        try:
            _ju = _joint_dist_fig(cfg, _d, model=_jmodel)
            if _ju:
                parts.append(_img(_ju, f"Joint-fixity distribution &mdash; {_d}-direction perimeter and internal frame "
                                       "(filled square = rigid/continuous, open circle = pinned/shear release, "
                                       "triangle = column base; pinned = strong-axis moment released)"))
        except Exception as ex:
            parts.append(f"<p class='note'>[joint-distribution figure {_d} failed: {_esc(ex)}]</p>")
    parts.append(_stability_basis_note(cfg))
    parts.append(_analysis_record_html(cfg, pkg))
    if Fx is not None and reX is not None:
        parts.append("<h3>Equilibrium check (ELF load cases)</h3>")
        for d, re_, F_ in (("X", reX, _FxX), ("Y", reY, _FxY)):
            if re_ is None or F_ is None:
                continue
            di = 0 if d == "X" else 1
            Rh = sum(r[2][di] for r in re_); Rz = sum(r[2][2] for r in re_)
            parts.append(f"<p>{d}: |&Sigma;R<sub>{d.lower()}</sub>| = {_n(abs(Rh))} kip against the applied base shear "
                         f"{_n(sum(F_.values()))} kip (equal and opposite); &Sigma;R<sub>z</sub> = {_n(abs(Rz))} kip of factored "
                         "gravity delivered to the supports.</p>")
        if eX is not None and eY is not None:
            parts.append(_modal_mass_check(eX, eY))
        if cfg.get("mode_figures"):
            for m in (1, 2, 3):
                if m <= NF*3:
                    try: parts.append(_img(fig_mode_3d(cfg, m, 3), f"Mode {m} (T = {T[m-1]:.2f} s)", full=True))
                    except Exception as ex: parts.append(f"<p class='note'>[mode {m} failed: {_esc(ex)}]</p>")
        elif not pub:
            parts.append("<p class='note'>[internal] Mode-shape figures not rendered (mode_figures off).</p>")
    try:
        gd = {}
        if bool(cfg.get("force_diagrams")):
            import frame_diagram as FD
            gd = FD.governing_diagrams(cfg, load_cases(cfg), nseg=6, lines=("perimeter", "internal"))
        elif not pub:
            parts.append("<p class='note'>[internal] Governing N/V/M diagrams not rendered (force_diagrams off).</p>")
        if gd.get("perimeter"):
            parts.append("<h3>Governing internal-force diagrams &mdash; perimeter frames (static model)</h3>")
            parts.append("<p>N / V / M for each perimeter frame under its governing LRFD combination, from the static "
                         "model with the distributed gravity loads. Peak values annotated per member; columns are single "
                         "elements (linear between ends).</p>")
            for _d in ("X", "Y"):
                if _d in gd["perimeter"]:
                    _uri, _label, _pkM = gd["perimeter"][_d]
                    parts.append(_img(_uri, f"{_d}-direction perimeter frame &mdash; governing combination {_esc(_label)} "
                                            f"(peak beam moment {_n(float(_pkM))} kip-ft)"))
        if gd.get("internal"):
            parts.append("<h3>Governing internal-force diagrams &mdash; internal frames (static model)</h3>")
            for _d in ("X", "Y"):
                if _d in gd["internal"]:
                    _uri, _label, _pkM = gd["internal"][_d]
                    parts.append(_img(_uri, f"{_d}-direction internal frame &mdash; governing combination {_esc(_label)} "
                                            f"(peak beam moment {_n(float(_pkM))} kip-ft)"))
    except Exception as ex:
        parts.append(f"<p class='note'>[static-model diagrams unavailable: {_esc(ex)}]</p>")

    # ===================== Chapter 6 — Member strength design (AISC 360) ==================
    parts.append(_chapter(6))
    sysn = ", ".join(dict.fromkeys(_sys_label(sf, d) for d in ("X", "Y"))) if sf else "SFRS"
    if pkg.get("members"):
        parts.append("<p>Member design summary by type: the analysis demand envelope, the governing combination, the "
                     "governing limit state and D/C. The full calculation of each member (section properties, demand, "
                     "available strengths, every limit-state check with its reference) is in Appendix A.</p>")
    parts.append(_member_section(cfg, name, pkg, {"roof"}, "Roof members",
        "Roof beams and girders for roof dead load, roof live load (and snow); flexure, shear and deflection."))
    parts.append(_member_section(cfg, name, pkg, {"floor"}, "Floor members",
        "Typical floor beams and girders for floor dead and live load (incl. collectors / struts where recorded)."))
    parts.append(_member_section(cfg, name, pkg, {"gravity_col"}, "Gravity columns",
        "Interior and perimeter gravity columns for the accumulated gravity load (AISC 360-22 Ch. E and H)."))
    parts.append(_member_section(cfg, name, pkg, {"lateral_col"}, "Lateral-system columns",
        "Columns of the seismic force-resisting system (%s) for combined gravity and lateral load (AISC 360-22 &sect;H1%s)."
        % (sysn, "; AISC 341-22 required strengths" if a341 else "")))
    if (sf and sf["has_br"]) or cfg.get("braces"):
        parts.append(_member_section(cfg, name, pkg, {"brace"}, "Braces",
            "Braces of the lateral frames for the design story shear (AISC 360-22 &sect;E3 compression, &sect;D2 tension%s)."
            % ("; AISC 341-22 capacity design per system" if a341 else "")))
    if any(_role_of(m.get("id", ""), m.get("inputs") or {}) == "other" for m in (pkg.get("members") or []) if isinstance(m, dict)):
        parts.append(_member_section(cfg, name, pkg, {"other"}, "Other members", "Members of other types."))
    parts.append(_ch6_notes(cfg))
    if pkg:
        parts.append("<h3>Governing member</h3>")
        parts.append(_member_dc_summary(pkg))
        _comp = _composite_section(pkg)
        if _comp:
            parts.append(_comp)
        _xtra = _extra_blocks_section(pkg)
        if _xtra:
            parts.append(_xtra)
        if cfg.get("section_color_figure"):    # OFF the default workflow (viewer's 'Color by section' shows it live)
            try:
                import viz3d as _VZ
                _msu = _VZ.members_by_size(cfg)
                if _msu:
                    parts.append(_img(_msu, "Model members coloured by section size"))
            except Exception as _mex:
                parts.append(f"<p class='note'>[member-size figure failed: {_esc(_mex)}]</p>")
    else:
        parts.append("<p class='note'>No calc package is available for this building.</p>")

    # ====================== Chapter 7 — Stability & second-order =========================
    parts.append(_chapter(7))
    if runs is not None:
        try:
            parts.append(_stability_section(cfg, runs))
            parts.append(_img(fig_drift_profile(cfg, runs), "Interstory drift profile (both directions)"))
            parts.append("<h3>Base shear and overturning per direction</h3>")
            parts.append(_base_shear_otm_table(cfg, SD, {"X": FwX, "Y": FwY} if FwX else None))
            series = {}
            if SD:
                series["seismic X"] = SD["X"]["Fx"]; series["seismic Y"] = SD["Y"]["Fx"]
            if FwX:
                series["wind X"] = FwX; series["wind Y"] = FwY
            if series:
                parts.append(_img(fig_story_shear_otm_dirs(cfg, series), "Story shear and overturning moment, seismic "
                                  "(ELF) and wind (Case 1), both directions"))
        except Exception as ex:
            parts.append(f"<p class='note'>[stability section failed: {_esc(ex)}]</p>")
    else:
        parts.append("<p class='note'>[needs seismic drift data]</p>")
    _ddm = _ddm_summary(pkg)
    if _ddm:
        parts.append(_ddm)

    # ============================ Chapter 8 — Serviceability =============================
    parts.append(_chapter(8))
    if runs is not None:
        parts.append(_drift_section(cfg, runs))
    parts.append(_wind_drift_section(cfg))
    parts.append(_floor_serviceability(pkg))
    parts.append(_deflection_section(cfg, pkg))
    parts.append(_floor_vibration_section(cfg, pkg))
    # 12.12.2 structural separation: cfg['separation_basis'] (text) states it; otherwise a note unless the package
    # already records a seismic joint / separation / pounding evaluation
    if cfg.get("separation_basis"):
        parts.append("<p class='cnote'><b>Building separation and pounding</b> (ASCE 7-22 &sect;12.12.2): %s</p>"
                     % _esc(str(cfg["separation_basis"])))
    elif not re.search(r"seismic joint|pounding|separation", json.dumps(pkg), re.I):
        parts.append("<p class='cnote'><b>Building separation and pounding</b> (ASCE 7-22 &sect;12.12.2): not part of "
                     "this package; to be confirmed against the site plan and the architectural drawings.</p>")

    # ===================== Chapter 9 — Seismic / wind detailing ================
    parts.append(_chapter(9))
    parts.append(_aisc341_detailing(cfg, pkg, sf, ctx))

    # ========================= Chapter 10 — Connections ==================================
    parts.append(_chapter(10))
    _ctbl = _connection_demands(cfg, pkg, None, sf, ctx) if sf else None
    if _ctbl:
        parts.append("<h3>Connection design demands</h3>")
        parts.append("<p>Member-end forces from the analysis envelope (all LRFD combinations) for each member group, and "
                     "the column-base reactions (Chapter 11):</p>")
        parts.append(_ctbl)
    _csum = _connection_summary(pkg)
    if _csum:
        parts.append(_csum)
        parts.append("<p class='cnote'>Connections are designed per AISC 360-22 Ch. J (bolts J3, welds J2, block shear J4.3, "
                     "connecting elements J4, base plates and anchorage J8 / J9 with ACI 318-19 Ch. 17) and Ch. K for HSS"
                     + ("; seismic connections per AISC 341-22" if a341 else "") + "; single-plate shear connections per the "
                     "AISC Manual Part 10. Only shop-level detailing is left to the fabricator's connection submittal.</p>")
    else:
        parts.append("<p class='note'>No connection design is recorded in the calc package; the demands above are for the "
                     "connection designer.</p>")

    # ===================== Chapter 11 — Foundations interface ============================
    parts.append(_chapter(11))
    parts.append(_base_reactions_section(cfg, pkg, ctx))

    # ============== Chapter 12 — Drawings, specifications & documentation ================
    parts.append(_chapter(12, status="verified on the contract documents"))
    parts.append("<p class='cnote'>Drawing and specification consistency, general notes, the special-inspection schedule, "
                 "member schedules, framing plans, brace / moment-frame elevations, connection details and the "
                 "deferred-submittal list are produced and checked on the contract documents, not in this report.</p>")

    # ===================== Chapter 13 — QA / professional acceptance =====================
    parts.append(_chapter(13))
    parts.append(_qa_scorecard(cfg, Fx, reX, eX, eY, runs, pkg, root, reY))
    parts.append(_consistency_section(name, root, pkg))
    parts.append(_gate_waivers_section(pkg))
    annex = os.path.join(root, "qa_annex.html")
    try:
        _qa_annex(name, cfg, pkg, annex)
        parts.append("<p class='cnote'>Design-process records (verification of the governing provisions against the "
                     "standards, and the design log) are in the <a href='qa_annex.html'>QA annex</a>.</p>")
    except Exception as ex:
        print(f"[report] QA annex not written: {ex}")
    parts.append("<p class='cnote'><b>Professional responsibility:</b> the independent check, software validation, "
                 "reconciliation of the model assumptions with the final details, and the engineer-of-record seal are "
                 "completed outside this calculation package.</p>")

    # ============================== Appendices ==========================================
    parts.append("<h2>Appendix A &mdash; AISC 360-22 member and connection calculations</h2>")
    if pkg:
        try: parts.append(appendix(cfg, name, pkg, a341))
        except Exception as ex: parts.append(f"<p class='note'>[Appendix A failed: {_esc(ex)}]</p>")
    else:
        parts.append("<p class='note'>No calc package is available for this building.</p>")
    if case_detail_parts:
        parts.append("<h2>Appendix B &mdash; Member forces by load combination</h2>")
        parts.append("<p>For each combination, the worst member of each type with its location, from the static model with "
                     "the distributed gravity loads (summarised in Chapter 4).</p>")
        parts.extend(case_detail_parts)

    html = (f"<!doctype html><html><head><meta charset='utf-8'><title>{_esc(title)} calculations</title>"
            f"<style>{CSS}{CHK_CSS}</style>{MATHJAX}</head><body>" + "".join(p_ for p_ in parts if p_) + "</body></html>")
    _fc = [0]
    def _fignum(_m):
        _fc[0] += 1; return f"<b>Figure {_fc[0]}.</b>"
    html = re.sub("@@FIGNUM@@", _fignum, html)        # number every figure in document order
    outdir = root; os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "report.html")
    open(path, "w", encoding="utf-8").write(html)
    print(f"[{name}] wrote {path}")
    return path


if __name__ == "__main__":
    for nm in (sys.argv[1:] or ["B02"]):
        try: build_report(nm)
        except Exception as ex: print(f"[{nm}] FAILED: {ex}")

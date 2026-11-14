"""Publication text rules of the HR report, without the report's analysis and plotting imports.

The report prints free text from the calculation records through ``publish``. The report read-back
uses the same function, so a record is compared with the text the renderer actually produces.
"""
import re


CFG_RX = re.compile(r"cfg\s*\[\s*['\"]([^'\"\]]+)['\"]\s*\](?:\s*\[\s*['\"]([^'\"\]]+)['\"]\s*\])?")
PATH_RX = re.compile(r"(?:\.\./)+[\w./-]+|/(?:home|tmp|mnt|root|Users)/[\w./-]+|design/calc_package\.json|"
                      r"calc_package\.json|activity_log\.jsonl|design_report\.md|member_demands\.md|"
                      r"pipeline_results\.json|connection_demands\.csv")


AGENT_NOTE_RX = re.compile(r"from memory|declared fallback|not verified against the (?:corpus|reference library)|"
                            r"NOT in the (?:reference library|RAG|corpus)|\bcorpus\b|\bRAG\b", re.I)


def drop_agent_notes(s):
    """Remove the design agent's working notes about its own sources ("method applied from memory",
    "not verified against the corpus") from published text; they stay in the QA annex. Parentheses left
    open by a dropped sentence are closed."""
    if not AGENT_NOTE_RX.search(s):
        return s
    s = re.sub(r"\s*\([^()]*\)", lambda m: "" if AGENT_NOTE_RX.search(m.group(0)) else m.group(0), s)
    if not AGENT_NOTE_RX.search(s):
        return s
    parts = re.split(r"(?<=[.;])\s+", s)
    keep = []; close = 0
    for part in parts:
        if AGENT_NOTE_RX.search(part):
            bal = part.count("(") - part.count(")")
            if bal < 0:
                close += -bal
            continue
        keep.append(part)
    out = " ".join(keep).rstrip()
    if close:
        out = out.rstrip(".") + ")" * close + "."
    return out


def publish(s):
    """Sanitise free text for publication: cfg['a']['b'] -> 'the declared a / b input', internal file paths
    and pipeline module names removed. Engineering content is left untouched."""
    if not isinstance(s, str) or not s:
        return s
    def _c(m):
        a = m.group(1).replace("_", " ")
        b = m.group(2).replace("_", " ") if m.group(2) else ""
        return "the declared %s input" % ((a + " / " + b) if b else a)
    s = CFG_RX.sub(_c, s)
    s = PATH_RX.sub("the design record", s)
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
    s = drop_agent_notes(s)
    s = s.replace("<=", "\u2264").replace(">=", "\u2265").replace("&lt;=", "&le;").replace("&gt;=", "&ge;")
    s = re.sub(r"\bsqrt\(", "\u221a(", s)
    return s

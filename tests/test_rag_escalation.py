"""The escalation ladder in `search_engineering_standards`: when it climbs, and when it stops.

The ladder was written to fire on a zero-hit answer. On 2026-09-18 a design run showed the gap that
leaves: a nine-term query at AISC 360-22 matched exactly ONE chunk -- a fragment straddling the
E7/F2 boundary -- and one hit is not zero, so the ladder never ran and the agent was handed a
fragment as though it were Chapter F. A thin rung now keeps the ladder climbing, and the best rung
seen is what comes back, labelled thin, rather than a claim that the provision is absent.

Engine-free and server-free: the RAG is a stub. Run with `python -m pytest tests -q` from the root.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from steltic import config                     # noqa: E402
from steltic.job_tools import JobWorkspace     # noqa: E402

A360 = "flexural strength compact I-shape lateral-torsional buckling F2"


class Stub(JobWorkspace):
    """A workspace with no disk and no server: every rung's answer comes from `answers`."""

    def __init__(self, answers):
        self.answers = answers          # how -> number of hits it returns
        self.sent = []                  # every (query, collection, clause) that went out
        self.building = False

    def log(self, tool, detail="", result=""):
        return {}

    def _rag_post(self, query, collection, clause="", chapter=""):
        self.sent.append((query, collection, clause))
        n = self.answers(query, collection, clause, len(self.sent))
        return {"results": [{"id": f"hit{i}", "source": collection or "AISC_360_22"} for i in range(n)]}, None


def _ws(answers):
    config.RAG_API_URL = "http://stub"
    return Stub(answers)


def test_one_hit_is_not_an_answer_and_the_ladder_keeps_climbing():
    ws = _ws(lambda q, c, cl, n: 1 if n == 1 else 5)
    out = ws.search_engineering_standards(A360, "engineering_standards_A360")
    assert len(ws.sent) > 1, "a single thin hit must not stop the ladder"
    assert len(out["results"]) == 5
    assert out.get("escalation") and len(out["escalation"]) > 1


def test_a_full_first_rung_still_stops_immediately():
    ws = _ws(lambda q, c, cl, n: 5)
    out = ws.search_engineering_standards(A360, "engineering_standards_A360")
    assert len(ws.sent) == 1, "a good first answer must not cost four more queries"
    assert "escalation" not in out


def test_the_best_thin_rung_is_returned_rather_than_a_false_absence():
    # every rung thin: two hits on the third attempt, one everywhere else
    ws = _ws(lambda q, c, cl, n: 2 if n == 3 else 1)
    out = ws.search_engineering_standards(A360, "engineering_standards_A360")
    assert out.get("results"), "a thin hit still beats reporting the provision absent"
    assert len(out["results"]) == 2
    assert "thin" in out
    assert out.get("not_found_kind") is None


def test_genuinely_nothing_still_reports_which_kind_of_nothing():
    ws = _ws(lambda q, c, cl, n: 0)
    out = ws.search_engineering_standards(A360, "engineering_standards_A360")
    assert out.get("found") is False
    assert out.get("not_found_kind") in (
        "no_specification_index", "document_not_in_corpus", "term_absent_from_document")


def test_an_unreachable_server_still_halts_the_run():
    ws = _ws(lambda q, c, cl, n: 0)
    ws._rag_post = lambda *a, **k: (None, "connection refused")
    out = ws.search_engineering_standards(A360, "engineering_standards_A360")
    assert out.get("rag_unavailable") is True

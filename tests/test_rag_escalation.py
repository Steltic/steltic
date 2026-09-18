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

    def _rag_post(self, query, collection, clause="", chapter="", type_="", want_commentary=False, neighbors=None):
        self.sent.append((query, collection, clause))
        self.wire = getattr(self, "wire", []) + [{"query": query, "collection": collection, "clause": clause,
                                                  "chapter": chapter, "type": type_}]
        n = self.answers(query, collection, clause, len(self.sent))
        # the stub's hits are anonymous chunks: nothing in them IS the id asked for, and the stub
        # server names no `matched` kind, so an exact-id rung yields navigation-grade hits here
        return {"results": [{"id": f"hit{i}", "source": collection or "AISC_360_22"} for i in range(n)]}, None


def _ws(answers):
    config.RAG_API_URL = "http://stub"
    return Stub(answers)


def test_one_hit_is_not_an_answer_and_the_ladder_keeps_climbing():
    ws = _ws(lambda q, c, cl, n: 1 if n == 1 else 5)
    out = ws.search_engineering_standards(A360, "engineering_standards_A360")
    assert len(ws.sent) > 1, "a single thin hit must not stop the ladder"
    assert len(out["results"]) == 5
    # the second send is the policy's navigation step, not an escalation: the answer must not claim
    # "your query as written found nothing" for a question that was answered as the policy sends it
    assert "exact-id F2" in out["policy"] and "fts «" in out["policy"]
    assert "escalation" not in out


def test_a_good_answer_does_not_climb_the_ladder():
    # under the retrieval policy an id-bearing sentence costs exactly two sends -- the exact id and one
    # navigation query in spec words -- and a good navigation answer must not cost four more
    ws = _ws(lambda q, c, cl, n: 5)
    out = ws.search_engineering_standards(A360, "engineering_standards_A360")
    assert len(ws.sent) == 2, ws.sent
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


# --- the retrieval policy (steltic_grokbot skills/Skill_querying_PACKAGED.md) ----------------------
#
# On 2026-09-18 the design runs still sent sentences -- "flexural strength compact I-shape
# lateral-torsional buckling F2" -- because nothing between the skill and the wire carried the
# policy: the tool had no `type`/`doc`, the contract said only "pass clause when you know it", and
# the ladder tried the sentence first and an exact id only if the sentence failed. The policy now
# shapes every call: exact ids first, one navigation query in spec words, and the log says so.

class Exact(Stub):
    """A server that, like the real one, answers an exact id with that record and says so."""

    def _rag_post(self, query, collection, clause="", chapter="", type_="", want_commentary=False, neighbors=None):
        self.sent.append((query, collection, clause))
        self.wire = getattr(self, "wire", []) + [{"query": query, "collection": collection, "clause": clause,
                                                  "chapter": chapter, "type": type_}]
        if clause:
            return {"results": [{"id": f"spec:{clause}", "section": clause, "source": "AISC_360_22"}],
                    "matched": "exact_section"}, None
        n = self.answers(query, collection, clause, len(self.sent))
        return {"results": [{"id": f"hit{i}", "source": collection or "AISC_360_22"} for i in range(n)],
                "matched": "fts" if n else ""}, None


def test_a_sentence_naming_an_id_goes_out_as_the_exact_id_first():
    config.RAG_API_URL = "http://stub"
    ws = Exact(lambda q, c, cl, n: 5)
    out = ws.search_engineering_standards(A360, "engineering_standards_A360")
    assert ws.wire[0]["clause"] == "F2" and ws.wire[0]["query"] == "", ws.wire
    assert "exact-id F2" in out["policy"] and "fts «" in out["policy"]
    assert out["results"][0]["section"] == "F2", "the exact record leads the answer"
    assert "F2" not in ws.wire[1]["query"] and ws.wire[1]["chapter"] == "F", "navigation is spec words, narrowed to the chapter"


def test_an_explicit_exact_type_sends_the_id_alone_with_its_kind():
    config.RAG_API_URL = "http://stub"
    ws = Exact(lambda q, c, cl, n: 0)
    out = ws.search_engineering_standards("F2-1", "engineering_standards_A360", type="exact_equation", doc="AISC_360_22")
    assert ws.wire == [{"query": "", "collection": "engineering_standards_A360", "clause": "F2-1", "chapter": "", "type": "exact_equation"}]
    assert out["policy"] == "exact_equation F2-1" and len(out["results"]) == 1


def test_a_bare_id_typed_into_query_is_an_exact_lookup():
    config.RAG_API_URL = "http://stub"
    ws = Exact(lambda q, c, cl, n: 0)
    out = ws.search_engineering_standards("E3.4a", "engineering_standards_A341")
    assert ws.wire[0]["clause"] == "E3.4a" and out["policy"] == "exact-id E3.4a"


def test_doc_selects_the_collection_the_report_counts_by():
    config.RAG_API_URL = "http://stub"
    ws = Exact(lambda q, c, cl, n: 0)
    ws.search_engineering_standards("D1.1b", "", type="exact_table", doc="AISC_341_22")
    assert ws.wire[0]["collection"] == "engineering_standards_A341"


def test_a_keyword_fallback_on_a_bogus_id_is_not_an_exact_answer():
    """W14 is a section size, not a clause: the server's keyword fallback must not be returned as if
    exact_section W14 had been found."""
    config.RAG_API_URL = "http://stub"
    ws = _ws(lambda q, c, cl, n: 1 if cl else 5)          # the Stub never claims a match
    out = ws.search_engineering_standards("W14 column compression flexural buckling E3", "engineering_standards_A360")
    assert not out.get("exact_ids") or "W14" not in out["exact_ids"] or out["results"][0].get("section") == "W14"
    assert len(out["results"]) >= 3

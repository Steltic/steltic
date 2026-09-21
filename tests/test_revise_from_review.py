"""The Design tab's "Revise reports with Review report" button.

The Nonlinear module's Review tab grounds every clause it cites in the Query file manager's corpus
-- the licensed PDFs on this PC -- and writes review.md into the PROJECT folder. This step reads it
from there and continues the design conversation with it, so the citations the review actually read
replace the values the design had flagged for verification. The two modules never call each other;
the shared project folder is the whole interface.

Engine-free: run with `python -m pytest tests -q` from the repo root.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from steltic.main import _revision_brief            # noqa: E402

REVIEW = """# Review of Tower — nonlinear analyses

## 1. Verdict
Chapter 16: **acceptable** — mean storey drift 1.74% against 2.24% [ASCE 7-22 §16.4.1.2, p. 149].
Force-controlled columns checked with gamma = 1.3 [ASCE 7-22 §16.4.2.2] (UNVERIFIED).
"""


def test_the_review_is_carried_into_the_brief_verbatim():
    b = _revision_brief(REVIEW)
    assert REVIEW.strip() in b, "the agent must see the review as written, not a paraphrase"
    assert "review.md" in b


def test_it_says_which_citations_may_be_promoted_and_which_may_not():
    b = _revision_brief(REVIEW)
    low = b.lower()
    assert "page number" in low, "the page number is what marks a citation as actually read"
    assert "(UNVERIFIED) STAYS flagged" in b or "stays flagged" in low
    assert "do not" in low and "invent" in low


def test_engineer_notes_ride_alongside_and_are_kept_separate():
    b = _revision_brief(REVIEW, "keep the W24 columns and say why")
    assert "keep the W24 columns and say why" in b
    assert b.index("The engineer added:") < b.index("--- REVIEW")
    # and with no notes, no empty heading
    assert "The engineer added:" not in _revision_brief(REVIEW, "   ")


def test_it_forbids_a_redesign_nobody_asked_for():
    assert "only those" in _revision_brief(REVIEW)


def test_pressing_revise_before_review_says_so_plainly():
    """The first thing a user does wrong is press Revise before running the Review tab. The answer
    has to name the tab that produces the file, not just fail."""
    from fastapi.testclient import TestClient
    from steltic.main import app, JOBS_DIR

    job = "ReviseWithoutReview"
    d = JOBS_DIR / job
    d.mkdir(parents=True, exist_ok=True)
    for stale in ("review.md",):
        if (d / stale).exists():
            (d / stale).unlink()
    with TestClient(app) as c:
        r = c.post("/api/run", json={"building": job, "brief": "", "resume": True, "revise_from_review": True})
    assert r.status_code == 409, r.text
    body = r.text
    assert "review.md" in body and "Review tab" in body


def test_an_empty_review_is_refused_too():
    from fastapi.testclient import TestClient
    from steltic.main import app, JOBS_DIR

    job = "ReviseEmptyReview"
    d = JOBS_DIR / job
    d.mkdir(parents=True, exist_ok=True)
    (d / "review.md").write_text("   \n", encoding="utf-8")
    with TestClient(app) as c:
        r = c.post("/api/run", json={"building": job, "brief": "", "resume": True, "revise_from_review": True})
    assert r.status_code == 409 and "empty" in r.text

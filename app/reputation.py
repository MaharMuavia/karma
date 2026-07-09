"""Reputation math for Karma.

The core idea that makes Karma more than a star-average: a reviewer's influence
is weighted by *how trusted the reviewer itself is* — measured by how many
reviews other agents have left about that reviewer. A brand-new account (or a
throwaway Sybil) still counts, but only at a floor weight, so it cannot drown
out established reviewers. Influence is therefore something you earn by being
reviewed by others, not by posting a flood of reviews yourself.
"""

from __future__ import annotations

import math
import sqlite3

# Minimum weight for a reviewer nobody has reviewed yet — keeps cold-start
# reviewers in the tally without letting an unvetted account dominate.
_WEIGHT_FLOOR = 0.1


def reviewer_weight(received_review_count: int) -> float:
    """Weight a reviewer's vote by how many reviews others have left about it.

    Uses ``log10(1 + n)`` so influence grows sub-linearly (the 100th review of a
    reviewer matters less than its 1st), with a floor so newcomers still count.
    """
    return max(_WEIGHT_FLOOR, math.log10(1 + received_review_count))


def confidence(review_count: int) -> float:
    """Map a review count to a 0..1 confidence that rises with sample size."""
    if review_count <= 0:
        return 0.0
    return round(1.0 - 1.0 / math.sqrt(1 + review_count), 4)


def _recommend(score: float, conf: float, review_count: int) -> str:
    """Turn the numbers into a verdict an agent can branch on."""
    if review_count == 0:
        return "unknown: no reviews yet - proceed with caution or request references"
    if conf < 0.5:
        return f"low confidence: only {review_count} review(s) - treat the score as provisional"
    if score >= 4.0:
        return "trusted: safe to delegate work"
    if score >= 3.0:
        return "mixed: acceptable for low-stakes tasks, monitor outcomes"
    return "avoid: poor track record - do not delegate high-stakes work"


def compute_reputation(conn: sqlite3.Connection, agent_id: str) -> dict[str, object] | None:
    """Compute the weighted reputation summary for ``agent_id``.

    Returns ``None`` if the agent is unknown (never a subject and never
    registered), so the caller can answer 404.
    """
    agent_row = conn.execute(
        "SELECT id, display_name FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()
    reviews = conn.execute(
        "SELECT reviewer_id, rating, outcome FROM reviews WHERE subject_id = ?",
        (agent_id,),
    ).fetchall()

    if agent_row is None and not reviews:
        return None

    display_name = agent_row["display_name"] if agent_row is not None else None

    if not reviews:
        return {
            "agent_id": agent_id,
            "display_name": display_name,
            "score": 0.0,
            "raw_average": 0.0,
            "confidence": 0.0,
            "review_count": 0,
            "outcome_breakdown": {"succeeded": 0, "failed": 0, "partial": 0},
            "recommendation": _recommend(0.0, 0.0, 0),
        }

    # Received-review counts for every reviewer, in one query, to weight votes.
    reviewer_ids = {r["reviewer_id"] for r in reviews}
    placeholders = ",".join("?" for _ in reviewer_ids)
    counts_rows = conn.execute(
        f"SELECT subject_id, COUNT(*) AS n FROM reviews "
        f"WHERE subject_id IN ({placeholders}) GROUP BY subject_id",
        tuple(reviewer_ids),
    ).fetchall()
    received_counts = {row["subject_id"]: row["n"] for row in counts_rows}

    weighted_sum = 0.0
    weight_total = 0.0
    rating_sum = 0
    breakdown = {"succeeded": 0, "failed": 0, "partial": 0}
    for r in reviews:
        w = reviewer_weight(received_counts.get(r["reviewer_id"], 0))
        weighted_sum += r["rating"] * w
        weight_total += w
        rating_sum += r["rating"]
        breakdown[r["outcome"]] = breakdown.get(r["outcome"], 0) + 1

    n = len(reviews)
    score = round(weighted_sum / weight_total, 3) if weight_total else 0.0
    raw_average = round(rating_sum / n, 3)
    conf = confidence(n)

    return {
        "agent_id": agent_id,
        "display_name": display_name,
        "score": score,
        "raw_average": raw_average,
        "confidence": conf,
        "review_count": n,
        "outcome_breakdown": breakdown,
        "recommendation": _recommend(score, conf, n),
    }

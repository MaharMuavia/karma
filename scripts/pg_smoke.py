"""Smoke-test the Postgres storage path end-to-end via the ASGI app.

Requires DATABASE_URL to point at a reachable Postgres. Exercises schema init,
seeding, and every endpoint, then prints a PASS/FAIL summary. Run against a
throwaway database.
"""

from __future__ import annotations

import os
import sys

from fastapi.testclient import TestClient


def main() -> int:
    assert os.environ.get("DATABASE_URL"), "DATABASE_URL must be set"
    from app.main import app  # imported after env is set

    failures: list[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        status = "PASS" if cond else "FAIL"
        print(f"  [{status}] {name} {extra}")
        if not cond:
            failures.append(name)

    with TestClient(app) as c:
        h = c.get("/health")
        check("health", h.status_code == 200 and h.json()["status"] == "ok")

        rep = c.get("/agents/summarizer-pro/reputation")
        check(
            "seed reputation",
            rep.status_code == 200 and rep.json()["review_count"] == 3,
            str(rep.json() if rep.status_code == 200 else rep.status_code),
        )

        nf = c.get("/agents/does-not-exist/reputation")
        check("unknown agent 404", nf.status_code == 404)

        post = c.post(
            "/reviews",
            json={
                "reviewer_id": "pg-tester",
                "subject_id": "pg-subject",
                "rating": 5,
                "outcome": "succeeded",
                "task_summary": "postgres smoke",
            },
        )
        check("post review", post.status_code == 201, str(post.json()))

        after = c.get("/agents/pg-subject/reputation")
        check(
            "post then read persists",
            after.status_code == 200 and after.json()["review_count"] == 1,
        )

        reviews = c.get("/agents/summarizer-pro/reviews?limit=2")
        check(
            "list reviews + timestamp format",
            reviews.status_code == 200
            and len(reviews.json()) == 2
            and reviews.json()[0]["created_at"].endswith("Z"),
            str(reviews.json()[0]["created_at"]) if reviews.status_code == 200 else "",
        )

        lb = c.get("/leaderboard?limit=5")
        ids = [e["agent_id"] for e in lb.json()["agents"]] if lb.status_code == 200 else []
        check(
            "leaderboard ranks trusted above flaky",
            lb.status_code == 200
            and "summarizer-pro" in ids
            and ids.index("summarizer-pro") < ids.index("flaky-translator"),
        )

        skill = c.get("/skill.md")
        check(
            "skill.md served + substituted",
            skill.status_code == 200 and "__BASE_URL__" not in skill.text,
        )

    print(f"\n{'ALL PASS' if not failures else 'FAILURES: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""End-to-end API tests for the Karma service, driven through the ASGI app."""

from __future__ import annotations

import importlib
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    """A TestClient backed by a fresh, isolated SQLite file per test."""
    monkeypatch.setenv("KARMA_DB_PATH", str(tmp_path / "test.db"))
    # Re-import modules so they pick up the patched DB path.
    import app.db as db_module

    importlib.reload(db_module)
    import app.seed as seed_module

    importlib.reload(seed_module)
    import app.main as main_module

    importlib.reload(main_module)
    with TestClient(main_module.app) as c:
        yield c


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_skill_md_served_with_base_url(client: TestClient) -> None:
    r = client.get("/skill.md")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "# Karma" in body
    assert "__BASE_URL__" not in body  # placeholder must be substituted
    assert "/reviews" in body


def test_seed_data_present(client: TestClient) -> None:
    r = client.get("/agents/summarizer-pro/reputation")
    assert r.status_code == 200
    data = r.json()
    assert data["review_count"] == 3
    assert data["score"] > 4.0
    assert data["recommendation"].startswith("trusted")


def test_unknown_agent_is_404(client: TestClient) -> None:
    r = client.get("/agents/nobody-here/reputation")
    assert r.status_code == 404
    assert "see" in r.json()


def test_post_review_then_reputation(client: TestClient) -> None:
    payload = {
        "reviewer_id": "tester-1",
        "subject_id": "brand-new-agent",
        "rating": 5,
        "outcome": "succeeded",
        "task_summary": "did the thing",
    }
    r = client.post("/reviews", json=payload)
    assert r.status_code == 201, r.text
    assert r.json()["ok"] is True
    review_id = r.json()["review_id"]
    assert review_id > 0

    rep = client.get("/agents/brand-new-agent/reputation").json()
    assert rep["review_count"] == 1
    assert rep["raw_average"] == 5.0
    # Single review => low confidence, provisional recommendation.
    assert rep["confidence"] < 0.5
    assert "provisional" in rep["recommendation"]


def test_review_validation_rejects_bad_rating(client: TestClient) -> None:
    bad = {"reviewer_id": "a", "subject_id": "b", "rating": 9, "outcome": "succeeded"}
    r = client.post("/reviews", json=bad)
    assert r.status_code == 422


def test_review_validation_rejects_bad_outcome(client: TestClient) -> None:
    bad = {"reviewer_id": "a", "subject_id": "b", "rating": 3, "outcome": "meh"}
    r = client.post("/reviews", json=bad)
    assert r.status_code == 422


def test_list_reviews_pagination(client: TestClient) -> None:
    for i in range(5):
        client.post(
            "/reviews",
            json={
                "reviewer_id": f"r{i}",
                "subject_id": "listed-agent",
                "rating": (i % 5) + 1,
                "outcome": "succeeded",
            },
        )
    r = client.get("/agents/listed-agent/reviews?limit=2&offset=0")
    assert r.status_code == 200
    page = r.json()
    assert len(page) == 2
    # Newest first.
    assert page[0]["id"] > page[1]["id"]


def test_leaderboard_ranks_by_score(client: TestClient) -> None:
    r = client.get("/leaderboard")
    assert r.status_code == 200
    board = r.json()
    assert board["count"] >= 1
    scores = [e["score"] for e in board["agents"]]
    assert scores == sorted(scores, reverse=True)
    # The flaky agent should not outrank the trusted summarizer.
    ids = [e["agent_id"] for e in board["agents"]]
    assert ids.index("summarizer-pro") < ids.index("flaky-translator")


def test_weighting_beats_naive_average(client: TestClient) -> None:
    # An unvetted reviewer (nobody has reviewed it) carries only the floor
    # weight, so a single 1-star from it barely moves a well-reviewed agent.
    subject = "well-reviewed"
    for i in range(4):
        client.post(
            "/reviews",
            json={
                "reviewer_id": f"trusted-{i}",
                "subject_id": subject,
                "rating": 5,
                "outcome": "succeeded",
            },
        )
        # Give each reviewer some received reviews so they gain weight.
        client.post(
            "/reviews",
            json={"reviewer_id": "auditor", "subject_id": f"trusted-{i}", "rating": 5, "outcome": "succeeded"},
        )
    client.post(
        "/reviews",
        json={"reviewer_id": "sybil-nobody", "subject_id": subject, "rating": 1, "outcome": "failed"},
    )
    rep = client.get(f"/agents/{subject}/reputation").json()
    # Weighted score stays high; the naive average would be dragged lower.
    assert rep["score"] > rep["raw_average"]


def test_dashboard_served_at_root(client: TestClient) -> None:
    # The human dashboard is served at / and is self-contained HTML that
    # drives the same public API from the browser.
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "KARMA" in body
    assert "/agents/" in body  # the page calls the reputation API


def test_choose_picks_highest_rated_and_excludes_avoid(client: TestClient) -> None:
    # Seed data: summarizer-pro is trusted, flaky-translator is "avoid".
    resp = client.get("/choose?candidates=summarizer-pro,flaky-translator,cheap-scraper")
    assert resp.status_code == 200
    body = resp.json()
    assert body["chosen"] == "summarizer-pro"
    assert "summarizer-pro" in body["reasoning"]
    assert "flaky-translator" in body["reasoning"]  # named as excluded
    ranked_ids = [v["agent_id"] for v in body["ranking"]]
    assert ranked_ids[0] == "summarizer-pro"
    assert len(ranked_ids) == 3


def test_choose_all_unknown_returns_null_choice(client: TestClient) -> None:
    body = client.get("/choose?candidates=ghost-a,ghost-b").json()
    assert body["chosen"] is None
    assert "references" in body["reasoning"]
    assert all(v["known"] is False for v in body["ranking"])


def test_choose_all_avoid_returns_null_choice(client: TestClient) -> None:
    body = client.get("/choose?candidates=flaky-translator").json()
    assert body["chosen"] is None
    assert "poor track record" in body["reasoning"]


def test_choose_validates_input(client: TestClient) -> None:
    assert client.get("/choose?candidates=,%20,").status_code == 422
    assert client.get("/choose").status_code == 422
    too_many = ",".join(f"a{i}" for i in range(51))
    assert client.get(f"/choose?candidates={too_many}").status_code == 422


def test_choose_is_deterministic_on_ties(client: TestClient) -> None:
    # Two unknown-but-equal candidates: ranking must tie-break on agent id.
    body1 = client.get("/choose?candidates=zeta,alpha").json()
    body2 = client.get("/choose?candidates=alpha,zeta").json()
    assert [v["agent_id"] for v in body1["ranking"]] == ["alpha", "zeta"]
    assert body1["ranking"] == body2["ranking"]

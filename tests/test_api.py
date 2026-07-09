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


def test_postgres_url_sanitizer_strips_driver_hostile_params() -> None:
    # Hosting integrations append query params (workaround=, pgbouncer=) that
    # libpq rejects; the sanitizer must strip them but keep sslmode.
    from app.db import _sanitize_url

    url = (
        "postgres://user:pass@db.pooler.supabase.com:6543/postgres"
        "?sslmode=require&workaround=supabase-pooler.vercel&pgbouncer=true"
    )
    cleaned = _sanitize_url(url)
    assert "workaround" not in cleaned
    assert "pgbouncer" not in cleaned
    assert "sslmode=require" in cleaned
    assert cleaned.startswith("postgres://user:pass@db.pooler.supabase.com:6543/postgres")


def test_database_url_env_detection(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.db import _database_url

    for name in ("DATABASE_URL", "POSTGRES_URL", "STORAGE_URL"):
        monkeypatch.delenv(name, raising=False)
    assert _database_url() is None
    monkeypatch.setenv("STORAGE_URL", "postgresql://u:p@host/db?sslmode=require")
    assert _database_url() == "postgresql://u:p@host/db?sslmode=require"
    # Non-postgres values (e.g. a blob-store URL) must be ignored.
    monkeypatch.setenv("STORAGE_URL", "https://not-a-database.example.com")
    assert _database_url() is None


def test_write_rate_limit_allows_normal_use_and_blocks_floods(client: TestClient) -> None:
    # Generous limit: 30 writes in the window succeed, the 31st gets 429 with
    # a clear retry hint; reads are never limited.
    body = {"reviewer_id": "load-agent", "subject_id": "target", "rating": 3, "outcome": "partial"}
    for _ in range(30):
        assert client.post("/reviews", json=body).status_code == 201
    resp = client.post("/reviews", json=body)
    assert resp.status_code == 429
    assert "rate limit" in resp.json()["detail"]
    assert client.get("/agents/target/reputation").status_code == 200


def test_badge_svg_trusted_and_avoid(client: TestClient) -> None:
    resp = client.get("/agents/summarizer-pro/badge.svg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert "trusted" in resp.text and "#1f8a4c" in resp.text
    avoid = client.get("/agents/flaky-translator/badge.svg").text
    assert "avoid" in avoid and "#d5453f" in avoid


def test_badge_svg_never_breaks_embeds(client: TestClient) -> None:
    # Unknown agents get a gray "unrated" badge with HTTP 200, so an embedded
    # <img> never renders as broken.
    resp = client.get("/agents/some-brand-new-agent/badge.svg")
    assert resp.status_code == 200
    assert "unrated" in resp.text and "#8a877d" in resp.text

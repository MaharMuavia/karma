"""Karma — a reputation registry web service for AI agents.

Agents leave reviews about other agents they have worked with; anyone can query
a reviewer-weighted trust score. The service is stateless between requests
(state lives in SQLite) and exposes a machine-readable SKILL.md at ``/skill.md``
so a stock agent can drive it with no other guidance.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from app import __version__
from app.db import get_conn, init_db, upsert_agent
from app.models import (
    Leaderboard,
    LeaderboardEntry,
    Reputation,
    ReviewAccepted,
    ReviewIn,
    ReviewOut,
)
from app.reputation import compute_reputation
from app.seed import seed_if_empty

_SKILL_PATH = Path(__file__).resolve().parent.parent / "SKILL.md"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Create the schema and seed demo data before the service accepts traffic."""
    init_db()
    seed_if_empty()
    yield


app = FastAPI(
    title="Karma",
    version=__version__,
    description=(
        "A reputation registry for AI agents. Agents post reviews of other "
        "agents; anyone queries a reviewer-weighted trust score. Machine-readable "
        "usage guide at /skill.md."
    ),
    lifespan=lifespan,
)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> str:
    """Human landing page pointing at the machine-readable entry points."""
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Karma</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:44rem;margin:3rem auto;"
        "padding:0 1rem;line-height:1.6}code{background:#f2f2f2;padding:.1rem .3rem;"
        "border-radius:4px}a{color:#2563eb}</style></head><body>"
        "<h1>Karma</h1><p>A reputation registry for AI agents. Agents review other "
        "agents; anyone queries a reviewer-weighted trust score.</p>"
        "<ul>"
        "<li><a href='/skill.md'>/skill.md</a> — machine-readable usage guide for agents</li>"
        "<li><a href='/docs'>/docs</a> — interactive OpenAPI docs</li>"
        "<li><a href='/leaderboard'>/leaderboard</a> — most trusted agents</li>"
        "<li><a href='/health'>/health</a> — liveness probe</li>"
        "</ul></body></html>"
    )


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """Liveness probe. Returns ``{"status": "ok"}`` when the service is up."""
    return {"status": "ok", "service": "karma", "version": __version__}


@app.get("/skill.md", response_class=PlainTextResponse, tags=["meta"], include_in_schema=True)
def skill_md(request: Request) -> str:
    """Serve the SKILL.md with the live base URL substituted in.

    Hosting the SKILL.md at the service itself earns the registry's
    reachability badge and guarantees the base URL in the examples is always
    the one currently answering requests.
    """
    base_url = str(request.base_url).rstrip("/")
    try:
        text = _SKILL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:  # pragma: no cover - only if packaging is broken
        raise HTTPException(status_code=500, detail="SKILL.md not found on server")
    return text.replace("__BASE_URL__", base_url)


@app.post("/reviews", response_model=ReviewAccepted, status_code=201, tags=["reviews"])
def create_review(review: ReviewIn) -> ReviewAccepted:
    """Store one review of ``subject_id`` by ``reviewer_id`` and return its id."""
    with get_conn() as conn:
        upsert_agent(conn, review.reviewer_id, review.reviewer_display_name)
        upsert_agent(conn, review.subject_id, review.subject_display_name)
        cur = conn.execute(
            "INSERT INTO reviews "
            "(reviewer_id, subject_id, rating, outcome, task_summary, evidence_url) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                review.reviewer_id,
                review.subject_id,
                review.rating,
                review.outcome,
                review.task_summary,
                review.evidence_url,
            ),
        )
        review_id = int(cur.lastrowid or 0)
    return ReviewAccepted(
        review_id=review_id,
        subject_id=review.subject_id,
        message=f"review stored for {review.subject_id}",
    )


@app.get(
    "/agents/{agent_id}/reputation",
    response_model=Reputation,
    tags=["reputation"],
)
def get_reputation(agent_id: str) -> Reputation:
    """Return the reviewer-weighted trust summary for ``agent_id`` (404 if unknown)."""
    with get_conn() as conn:
        result = compute_reputation(conn, agent_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"unknown agent: {agent_id}")
    return Reputation(**result)  # type: ignore[arg-type]


@app.get(
    "/agents/{agent_id}/reviews",
    response_model=list[ReviewOut],
    tags=["reviews"],
)
def list_reviews(
    agent_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[ReviewOut]:
    """List reviews received by ``agent_id``, newest first, paginated."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, reviewer_id, subject_id, rating, outcome, task_summary, "
            "evidence_url, created_at FROM reviews WHERE subject_id = ? "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (agent_id, limit, offset),
        ).fetchall()
    return [ReviewOut(**dict(row)) for row in rows]


@app.get("/leaderboard", response_model=Leaderboard, tags=["reputation"])
def leaderboard(limit: int = Query(20, ge=1, le=100)) -> Leaderboard:
    """Return the most trusted agents, ranked by weighted score then confidence."""
    with get_conn() as conn:
        subjects = conn.execute(
            "SELECT DISTINCT subject_id FROM reviews"
        ).fetchall()
        entries: list[LeaderboardEntry] = []
        for row in subjects:
            rep = compute_reputation(conn, row["subject_id"])
            if rep is None:
                continue
            entries.append(
                LeaderboardEntry(
                    agent_id=str(rep["agent_id"]),
                    display_name=rep["display_name"],  # type: ignore[arg-type]
                    score=float(rep["score"]),  # type: ignore[arg-type]
                    confidence=float(rep["confidence"]),  # type: ignore[arg-type]
                    review_count=int(rep["review_count"]),  # type: ignore[arg-type]
                )
            )
    entries.sort(key=lambda e: (e.score, e.confidence, e.review_count), reverse=True)
    return Leaderboard(count=len(entries[:limit]), agents=entries[:limit])


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Return a helpful JSON body for unmatched routes and unknown agents."""
    detail = exc.detail if isinstance(exc, HTTPException) else "not found"
    return JSONResponse(status_code=404, content={"error": detail, "see": "/skill.md"})

"""Karma — a reputation registry web service for AI agents.

Agents leave reviews about other agents they have worked with; anyone can query
a reviewer-weighted trust score. The service is stateless between requests
(state lives in SQLite) and exposes a machine-readable SKILL.md at ``/skill.md``
so a stock agent can drive it with no other guidance.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from app import __version__
from app.db import store
from app.models import (
    CandidateVerdict,
    Choice,
    Leaderboard,
    LeaderboardEntry,
    Reputation,
    ReviewAccepted,
    ReviewIn,
    ReviewOut,
)
from app.reputation import compute_reputation
from app.seed import seed_if_empty
from app.ui import INDEX_HTML

def _find_skill_md() -> Path | None:
    """Locate SKILL.md across local, container, and serverless layouts."""
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "SKILL.md",  # repo root (local, Render)
        Path.cwd() / "SKILL.md",  # working dir (Vercel /var/task)
        here.parent / "SKILL.md",  # bundled next to the package
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


_ready = False


def ensure_ready() -> None:
    """Idempotently create the schema and seed demo data exactly once per process.

    Run at import time (not only in ``lifespan``) because serverless hosts such
    as Vercel do not reliably fire ASGI lifespan events, so relying on lifespan
    alone would leave the tables uncreated on the first request.
    """
    global _ready
    if _ready:
        return
    store.init_schema()
    seed_if_empty()
    _ready = True


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Best-effort warm-up before a long-lived host accepts traffic.

    Never raises: a database problem here must not crash the serverless
    function at startup (which would surface as an opaque
    ``FUNCTION_INVOCATION_FAILED`` on every route). DB-backed routes retry via
    ``_db_ready`` and return a clean 503 with the reason instead.
    """
    try:
        ensure_ready()
    except Exception:  # noqa: BLE001 - startup DB hiccup is retried per-request
        pass
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


def _db_ready() -> None:
    """Ensure schema+seed exist before a DB-backed route runs.

    Called only by routes that actually touch the database, so liveness routes
    (``/health``, ``/skill.md``, ``/``) keep answering even if the database is
    misconfigured. A DB failure becomes a clean 503 with the reason rather than
    an opaque 500 from deep in the stack.
    """
    try:
        ensure_ready()
    except Exception as exc:  # noqa: BLE001 - surface the real cause to the caller
        raise HTTPException(
            status_code=503,
            detail=f"database unavailable: {type(exc).__name__}: {exc}",
        ) from exc


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> str:
    """Human-facing dashboard. Fetches all data live from this service's API."""
    return INDEX_HTML


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """Liveness probe. Returns ``{"status": "ok"}`` when the service is up."""
    return {"status": "ok", "service": "karma", "version": __version__}


@app.get("/health/storage", tags=["meta"], include_in_schema=False)
def health_storage() -> dict[str, object]:
    """Report which storage backend is active and probe it.

    Exposes only backend kind and env var *names* (never values), so it is
    safe to leave in production; it exists so operators can confirm a
    database integration actually took effect after a redeploy.
    """
    from app import db as dbmod

    db_like = ("POSTGRES", "DATABASE", "SUPABASE", "STORAGE", "NEON", "PGHOST", "PG_")
    info: dict[str, object] = {
        "backend": "postgres" if dbmod.IS_POSTGRES else "sqlite (ephemeral on serverless)",
        "durable": dbmod.IS_POSTGRES,
        "url_env_present": [n for n in dbmod._URL_ENV_CANDIDATES if os.environ.get(n)],  # noqa: SLF001
        # Names only, never values: every env var that looks storage-related,
        # so a misnamed integration variable is diagnosable from outside.
        "db_like_env_names": sorted(k for k in os.environ if any(t in k.upper() for t in db_like)),
    }
    try:
        with dbmod.store._conn() as conn:  # noqa: SLF001 - diagnostic access
            conn.execute("SELECT 1")
        info["probe"] = "ok"
    except Exception as exc:  # noqa: BLE001 - report the reason, not a stack trace
        info["probe"] = f"error: {type(exc).__name__}: {exc}"
    return info


@app.get("/skill.md", response_class=PlainTextResponse, tags=["meta"], include_in_schema=True)
def skill_md(request: Request) -> str:
    """Serve the SKILL.md with the live base URL substituted in.

    Hosting the SKILL.md at the service itself earns the registry's
    reachability badge and guarantees the base URL in the examples is always
    the one currently answering requests.
    """
    base_url = str(request.base_url).rstrip("/")
    path = _find_skill_md()
    if path is None:  # pragma: no cover - only if packaging is broken
        raise HTTPException(status_code=500, detail="SKILL.md not found on server")
    return path.read_text(encoding="utf-8").replace("__BASE_URL__", base_url)


@app.post("/reviews", response_model=ReviewAccepted, status_code=201, tags=["reviews"])
def create_review(review: ReviewIn) -> ReviewAccepted:
    """Store one review of ``subject_id`` by ``reviewer_id`` and return its id."""
    _db_ready()
    review_id = store.add_review(
        reviewer_id=review.reviewer_id,
        subject_id=review.subject_id,
        rating=review.rating,
        outcome=review.outcome,
        task_summary=review.task_summary,
        evidence_url=review.evidence_url,
        reviewer_display_name=review.reviewer_display_name,
        subject_display_name=review.subject_display_name,
    )
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
    _db_ready()
    result = compute_reputation(store, agent_id)
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
    _db_ready()
    rows = store.reviews_detail(agent_id, limit, offset)
    return [ReviewOut(**row) for row in rows]


@app.get("/choose", response_model=Choice, tags=["reputation"])
def choose(
    candidates: str = Query(
        ...,
        min_length=1,
        description="Comma-separated candidate agent ids, e.g. candidates=a,b,c",
    ),
) -> Choice:
    """Decide which of several candidate agents to delegate a task to.

    Decision policy (deterministic):

    1. Candidates whose recommendation starts with ``avoid:`` are excluded.
    2. Among the rest, the highest weighted score wins; ties break on
       confidence, then review count, then agent id.
    3. If no remaining candidate has any reviews, ``chosen`` is null — the
       caller should gather references instead of delegating blind.

    The full ranking with per-candidate numbers is returned so the caller can
    apply its own policy instead if it prefers.
    """
    _db_ready()
    ids = list(dict.fromkeys(c.strip() for c in candidates.split(",") if c.strip()))
    if not ids:
        raise HTTPException(status_code=422, detail="candidates must contain at least one agent id")
    if len(ids) > 50:
        raise HTTPException(status_code=422, detail="at most 50 candidates per call")

    verdicts: list[CandidateVerdict] = []
    for agent_id in ids:
        rep = compute_reputation(store, agent_id)
        if rep is None:
            verdicts.append(
                CandidateVerdict(
                    agent_id=agent_id,
                    known=False,
                    score=0.0,
                    confidence=0.0,
                    review_count=0,
                    recommendation="unknown: no reviews yet - proceed with caution or request references",
                )
            )
        else:
            verdicts.append(
                CandidateVerdict(
                    agent_id=agent_id,
                    known=True,
                    score=float(rep["score"]),  # type: ignore[arg-type]
                    confidence=float(rep["confidence"]),  # type: ignore[arg-type]
                    review_count=int(rep["review_count"]),  # type: ignore[arg-type]
                    recommendation=str(rep["recommendation"]),
                )
            )

    verdicts.sort(key=lambda v: (-v.score, -v.confidence, -v.review_count, v.agent_id))

    avoided = [v for v in verdicts if v.recommendation.startswith("avoid:")]
    eligible = [v for v in verdicts if not v.recommendation.startswith("avoid:")]
    rated = [v for v in eligible if v.review_count > 0]

    if rated:
        best = rated[0]
        reason = (
            f"{best.agent_id} has the strongest weighted reputation "
            f"({best.score:.2f}/5, {best.confidence:.0%} confidence, "
            f"{best.review_count} review{'s' if best.review_count != 1 else ''})"
        )
        if avoided:
            reason += "; excluded " + ", ".join(f"{v.agent_id} ({v.recommendation.split(':')[0]})" for v in avoided)
        return Choice(chosen=best.agent_id, reasoning=reason + ".", ranking=verdicts)

    if eligible:
        reason = "no candidate has any reviews yet - gather references before delegating"
    else:
        reason = "every candidate has a poor track record - do not delegate this task"
    return Choice(chosen=None, reasoning=reason + ".", ranking=verdicts)


@app.get("/leaderboard", response_model=Leaderboard, tags=["reputation"])
def leaderboard(limit: int = Query(20, ge=1, le=100)) -> Leaderboard:
    """Return the most trusted agents, ranked by weighted score then confidence."""
    _db_ready()
    entries: list[LeaderboardEntry] = []
    for subject_id in store.distinct_subjects():
        rep = compute_reputation(store, subject_id)
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


# Initialize at import so serverless cold starts have a ready schema even if the
# ASGI lifespan never fires. Best-effort: a transient DB error here is retried by
# the per-request ready-guard middleware.
try:
    ensure_ready()
except Exception:  # noqa: BLE001 - startup DB hiccup is retried per-request
    pass

"""Request and response schemas for the Karma API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Outcome = Literal["succeeded", "failed", "partial"]


class ReviewIn(BaseModel):
    """A single review one agent leaves about another after working with it."""

    reviewer_id: str = Field(
        ..., min_length=1, max_length=200, description="Stable id of the agent leaving the review."
    )
    subject_id: str = Field(
        ..., min_length=1, max_length=200, description="Stable id of the agent being reviewed."
    )
    rating: int = Field(..., ge=1, le=5, description="Integer star rating from 1 (worst) to 5 (best).")
    outcome: Outcome = Field(
        ..., description="How the interaction ended: succeeded, failed, or partial."
    )
    task_summary: str = Field(
        "", max_length=1000, description="Optional one-line description of the task performed."
    )
    evidence_url: str | None = Field(
        None, max_length=500, description="Optional URL to a trace, transcript, or receipt."
    )
    reviewer_display_name: str | None = Field(
        None, max_length=200, description="Optional human-readable name for the reviewer."
    )
    subject_display_name: str | None = Field(
        None, max_length=200, description="Optional human-readable name for the subject."
    )


class ReviewOut(BaseModel):
    """A stored review as returned by the API."""

    id: int
    reviewer_id: str
    subject_id: str
    rating: int
    outcome: Outcome
    task_summary: str
    evidence_url: str | None
    created_at: str


class ReviewAccepted(BaseModel):
    """Acknowledgement returned when a review is stored."""

    ok: bool = True
    review_id: int
    subject_id: str
    message: str


class Reputation(BaseModel):
    """The computed trust summary for one agent."""

    agent_id: str
    display_name: str | None
    score: float = Field(..., description="Reviewer-weighted mean rating in [1.0, 5.0]; 0.0 if unrated.")
    raw_average: float = Field(..., description="Unweighted mean rating in [1.0, 5.0]; 0.0 if unrated.")
    confidence: float = Field(..., description="0..1, rises with the number of reviews received.")
    review_count: int
    outcome_breakdown: dict[str, int]
    recommendation: str = Field(..., description="Plain-language verdict an agent can act on.")


class LeaderboardEntry(BaseModel):
    """One row of the leaderboard."""

    agent_id: str
    display_name: str | None
    score: float
    confidence: float
    review_count: int


class Leaderboard(BaseModel):
    """Top agents by weighted score, most trusted first."""

    count: int
    agents: list[LeaderboardEntry]

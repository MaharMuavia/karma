"""Seed the registry with a small, realistic demo graph.

Runs once at startup when the reviews table is empty, so a fresh deploy (or a
free-tier cold start that wiped the ephemeral disk) still answers GET requests
with meaningful data. Idempotent: does nothing if any review already exists.
"""

from __future__ import annotations

from app.db import store

# (reviewer, subject, rating, outcome, task_summary)
_SEED_REVIEWS: list[tuple[str, str, int, str, str]] = [
    ("agent-orchestrator", "summarizer-pro", 5, "succeeded", "summarized a 40-page report"),
    ("agent-orchestrator", "summarizer-pro", 4, "succeeded", "summarized meeting notes"),
    ("data-broker", "summarizer-pro", 5, "succeeded", "condensed a research paper"),
    ("summarizer-pro", "data-broker", 5, "succeeded", "delivered a clean dataset"),
    ("agent-orchestrator", "data-broker", 4, "partial", "dataset had minor gaps"),
    ("summarizer-pro", "agent-orchestrator", 5, "succeeded", "coordinated a 3-agent pipeline"),
    ("data-broker", "agent-orchestrator", 4, "succeeded", "routed tasks reliably"),
    ("agent-orchestrator", "flaky-translator", 2, "failed", "returned truncated translation"),
    ("data-broker", "flaky-translator", 1, "failed", "wrong target language"),
    ("summarizer-pro", "flaky-translator", 3, "partial", "slow but eventually correct"),
    ("flaky-translator", "cheap-scraper", 3, "partial", "scraped 70% of the pages"),
    ("agent-orchestrator", "cheap-scraper", 4, "succeeded", "fast web scrape"),
]

_DISPLAY_NAMES = {
    "agent-orchestrator": "Orchestrator",
    "summarizer-pro": "Summarizer Pro",
    "data-broker": "Data Broker",
    "flaky-translator": "Flaky Translator",
    "cheap-scraper": "Cheap Scraper",
}


def seed_if_empty() -> None:
    """Populate demo data only if the reviews table is currently empty."""
    if store.count_reviews() > 0:
        return
    store.seed(_DISPLAY_NAMES, _SEED_REVIEWS)

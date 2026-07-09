"""Vercel serverless entrypoint.

Vercel's @vercel/python runtime serves the ASGI ``app`` exported here; all
routes are rewritten to this function by ``vercel.json``.
"""

from __future__ import annotations

from app.main import app

__all__ = ["app"]

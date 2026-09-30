"""Role-based access (Phase 4): bearer tokens mapped to roles viewer < planner < security < admin.

    Authorization: Bearer <token>        tokens from AEGIS_TOKENS (dev defaults: dev-viewer, dev-planner, ...)
Phase 8 swaps this for OAuth2 password flow + JWT with the same `require(role)` dependency.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request

from services.api.app.config import ROLE_RANK


@dataclass
class Identity:
    user: str
    role: str


def identify(request: Request) -> Identity | None:
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        return None
    tok = h[7:].strip()
    role = request.app.state.ctx.settings.tokens.get(tok)
    return Identity(user=f"{role}-token", role=role) if role else None


def require(role: str):
    def dep(request: Request) -> Identity:
        who = identify(request)
        if who is None:
            raise HTTPException(401, "missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"})
        if ROLE_RANK[who.role] < ROLE_RANK[role]:
            raise HTTPException(403, f"requires role {role} (you are {who.role})")
        return who
    return dep

"""Role-based access: viewer < planner < security < admin.

    Authorization: Bearer <JWT access token>     from POST /api/v1/auth/token (OAuth2 password flow, security.py)
    Authorization: Bearer <static token>         AEGIS_TOKENS (dev defaults dev-viewer, dev-planner, ...; none in prod)
require(role) is the FastAPI dependency every protected route uses. Access tokens are verified (signature, expiry,
issuer, audience, type) and checked against the logout deny-list in Redis.
"""
from __future__ import annotations

from dataclasses import dataclass

import jwt
from fastapi import HTTPException, Request

from services.api.app.config import ROLE_RANK


@dataclass
class Identity:
    user: str
    role: str
    via: str = "token"          # jwt | token
    jti: str | None = None
    exp: float | None = None


async def identify_token(ctx, tok: str) -> Identity | None:
    role = ctx.settings.tokens.get(tok)
    if role:
        return Identity(user=f"{role}-token", role=role, via="token")
    if tok.count(".") != 2:
        return None
    from services.api.app.security import decode
    try:
        c = decode(ctx.settings, tok, "access")
    except jwt.PyJWTError:
        return None
    if await ctx.redis.exists(f"revoked:{c['jti']}"):
        return None
    return Identity(user=c["sub"], role=c["role"], via="jwt", jti=c["jti"], exp=c["exp"])


async def identify(request: Request) -> Identity | None:
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        return None
    return await identify_token(request.app.state.ctx, h[7:].strip())


def require(role: str):
    async def dep(request: Request) -> Identity:
        who = await identify(request)
        if who is None:
            raise HTTPException(401, "missing, invalid or expired bearer token", headers={"WWW-Authenticate": "Bearer"})
        if ROLE_RANK[who.role] < ROLE_RANK[role]:
            raise HTTPException(403, f"requires role {role} (you are {who.role})")
        return who
    return dep


async def ws_allowed(ws) -> bool:
    """WebSocket auth (browsers can't set headers on a WebSocket): ?token=<access JWT or static token>. Enforced when
    AEGIS_ENV=prod or AEGIS_WS_AUTH=1; any authenticated role may watch."""
    ctx = ws.app.state.ctx
    if not ctx.settings.ws_auth:
        return True
    tok = ws.query_params.get("token", "")
    return bool(tok) and await identify_token(ctx, tok) is not None

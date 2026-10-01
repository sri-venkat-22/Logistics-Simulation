"""Security (Phase 8): users + OAuth2 password flow with JWT, device-key rotation, rate limits, headers, audit.

Auth
    POST /api/v1/auth/token      OAuth2 password flow (form: username, password) -> access (15 min) + refresh (7 d)
                                 JWTs (HS256; iss aegis-twin, aud aegis-api, sub, role, typ, jti). Passwords are
                                 argon2id hashes in the users table. 5 failures per user in 15 min -> 429 lockout.
    POST /api/v1/auth/refresh    rotate: the refresh token's jti must still be on the Redis allow-list; it is consumed
                                 and a new pair issued (a replayed refresh token is rejected)
    POST /api/v1/auth/logout     revoke the refresh token and the current access token (jti deny-list until expiry)
    GET  /api/v1/auth/me         who am I;  GET /api/v1/auth/config: login-screen hints (demo users in dev only)
    GET/POST /api/v1/auth/users  admin: list / create users
RBAC: viewer < planner < security < admin (require(role) in auth.py accepts these JWTs).
Devices
    POST /api/v1/devices/{source_id}/rotate   security+: new random HMAC-SHA256 device key (returned once), stored
                                 pgcrypto-encrypted (pgp_sym_encrypt) in device_keys; the previous key stays valid
                                 for a grace period so a device can switch over; afterwards its messages fail L2
    GET  /api/v1/devices          security+: devices with a rotated key (version, rotated_at; never the key)
Transport / app: security headers (CSP, HSTS in prod, nosniff, frame-ancestors none, referrer-policy), a per-IP
slowapi rate limiter on Redis, fixed windows (REST, auth, Copilot), request-size limits (413), strict CORS (config).
Audit: GET /api/v1/audit (security+) - logins, logouts, plan applies, chaos injections, key rotations, Copilot use.
"""
from __future__ import annotations

import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from typing import TYPE_CHECKING

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from limits import RateLimitItemPerMinute
from slowapi import Limiter
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from services.api.app.auth import Identity, require
from services.api.app.config import ROLE_RANK
from services.api.app.db.store import RECENT_AUDIT, audit, execute

if TYPE_CHECKING:
    from services.api.app.main import Ctx

log = logging.getLogger("aegis.security")
router = APIRouter(prefix="/api/v1", tags=["security"])
ISSUER, AUDIENCE = "aegis-twin", "aegis-api"
_ph = PasswordHasher()  # argon2id, library defaults (RFC 9106 low-memory profile)


# ============================================================================================ users
class UserStore:
    """Users in Postgres (users table) with an in-memory fallback when persistence is off."""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.mem: dict[str, dict] = {}

    def seed(self, users: list[tuple[str, str, str]]) -> None:
        for user, role, pw in users:
            if self.get(user) is None:
                self.create(user, role, pw, display=user.title())

    def get(self, user: str) -> dict | None:
        if self.ctx.engine is None:
            return self.mem.get(user)
        rows = execute(self.ctx.engine, "select id, display_name, password_hash, role from users where id = :u", u=user)
        return rows[0] if rows else None

    def create(self, user: str, role: str, password: str, display: str | None = None) -> dict:
        row = {"id": user, "display_name": display or user, "password_hash": _ph.hash(password), "role": role}
        if self.ctx.engine is None:
            self.mem[user] = row
        else:
            execute(self.ctx.engine, "insert into users(id, display_name, password_hash, role) values (:id, :d, :h, :r) "
                    "on conflict (id) do nothing", id=user, d=row["display_name"], h=row["password_hash"], r=role)
        return row

    def all(self) -> list[dict]:
        if self.ctx.engine is None:
            return [{k: v for k, v in r.items() if k != "password_hash"} for r in self.mem.values()]
        return execute(self.ctx.engine, "select id, display_name, role, created_at from users order by id")

    def verify(self, user: str, password: str) -> dict | None:
        row = self.get(user)
        if row is None or not row.get("password_hash"):
            _ph.hash("timing-equaliser")  # don't reveal whether the user exists through response time
            return None
        try:
            _ph.verify(row["password_hash"], password)
        except (VerificationError, InvalidHashError):
            return None
        if _ph.check_needs_rehash(row["password_hash"]) and self.ctx.engine is not None:
            execute(self.ctx.engine, "update users set password_hash = :h where id = :u", h=_ph.hash(password), u=user)
        return row


# ============================================================================================ tokens
def issue(settings, user: str, role: str, typ: str) -> tuple[str, dict]:
    now = int(time.time())
    ttl = settings.access_ttl_s if typ == "access" else settings.refresh_ttl_s
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": user, "role": role, "typ": typ, "jti": uuid.uuid4().hex,
              "iat": now, "nbf": now, "exp": now + ttl}
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256"), claims


def decode(settings, token: str, typ: str) -> dict:
    """Validate signature, expiry, issuer, audience and token type; raise jwt exceptions otherwise."""
    claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER,
                        options={"require": ["exp", "iat", "sub", "jti", "typ"]})
    if claims.get("typ") != typ or claims.get("role") not in ROLE_RANK:
        raise jwt.InvalidTokenError("wrong token type")
    return claims


async def token_pair(ctx: Ctx, user: str, role: str) -> dict:
    s = ctx.settings
    access, _ = issue(s, user, role, "access")
    refresh, rc = issue(s, user, role, "refresh")
    await ctx.redis.set(f"rt:{rc['jti']}", user, ex=s.refresh_ttl_s)
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer", "expires_in": s.access_ttl_s,
            "user": user, "role": role}


@router.post("/auth/token", tags=["auth"])
async def login(request: Request, username: str = Form(..., max_length=64), password: str = Form(..., max_length=256)) -> dict:
    """OAuth2 password flow (application/x-www-form-urlencoded: username, password)."""
    ctx: Ctx = request.app.state.ctx
    s = ctx.settings
    key = f"lf:{username.lower()}"
    fails = int(await ctx.redis.get(key) or 0)
    if fails >= s.login_max_failures:
        audit(ctx.engine, username, "login.locked", None, {"ip": _ip(request)})
        raise HTTPException(429, "too many failed logins; try again later", headers={"Retry-After": str(s.login_lockout_s)})
    row = await _to_thread(ctx.users.verify, username, password)
    if row is None:
        pipe = ctx.redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, s.login_lockout_s)
        await pipe.execute()
        audit(ctx.engine, username, "login.failure", None, {"ip": _ip(request)})
        raise HTTPException(401, "incorrect username or password", headers={"WWW-Authenticate": "Bearer"})
    await ctx.redis.delete(key)
    audit(ctx.engine, username, "login.success", None, {"ip": _ip(request), "role": row["role"]})
    return await token_pair(ctx, row["id"], row["role"])


class RefreshBody(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=2048)


@router.post("/auth/refresh", tags=["auth"])
async def refresh(body: RefreshBody, request: Request) -> dict:
    ctx: Ctx = request.app.state.ctx
    try:
        c = decode(ctx.settings, body.refresh_token, "refresh")
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid refresh token") from None
    user = await ctx.redis.getdel(f"rt:{c['jti']}")
    if user is None:  # consumed (rotated) or revoked: a replay
        audit(ctx.engine, c["sub"], "token.refresh_reuse", None, {"ip": _ip(request)})
        raise HTTPException(401, "refresh token already used or revoked")
    row = await _to_thread(ctx.users.get, c["sub"])
    if row is None:  # the user was removed: no new tokens (the current access token dies within its TTL)
        audit(ctx.engine, c["sub"], "token.refresh_unknown_user", None, {"ip": _ip(request)})
        raise HTTPException(401, "user no longer exists")
    return await token_pair(ctx, c["sub"], row["role"])  # a role change takes effect at the next refresh


@router.post("/auth/logout", tags=["auth"])
async def logout(body: RefreshBody, request: Request, who: Identity = Depends(require("viewer"))) -> dict:
    ctx: Ctx = request.app.state.ctx
    try:
        c = decode(ctx.settings, body.refresh_token, "refresh")
        await ctx.redis.delete(f"rt:{c['jti']}")
    except jwt.PyJWTError:
        pass
    if who.jti and who.exp:
        await ctx.redis.set(f"revoked:{who.jti}", 1, ex=max(1, int(who.exp - time.time())))
    audit(ctx.engine, who.user, "logout", None, {"ip": _ip(request)})
    return {"ok": True}


@router.get("/auth/me", tags=["auth"])
async def me(who: Identity = Depends(require("viewer"))) -> dict:
    return {"user": who.user, "role": who.role, "via": who.via}


@router.get("/auth/config", tags=["auth"])
async def auth_config(request: Request) -> dict:
    s = request.app.state.ctx.settings
    return {"env": s.env, "access_ttl_s": s.access_ttl_s,
            "demo_users": [{"user": u, "role": r, "password": p} for u, r, p in s.users] if not s.prod else []}


class NewUser(BaseModel):
    user: str = Field(pattern=r"^[a-z0-9._-]{3,64}$")
    role: str = Field(pattern=r"^(viewer|planner|security|admin)$")
    password: str = Field(min_length=12, max_length=256)
    display_name: str | None = Field(None, max_length=120)


@router.get("/auth/users", tags=["auth"])
async def list_users(request: Request, who: Identity = Depends(require("admin"))) -> list[dict]:
    return await _to_thread(request.app.state.ctx.users.all)


@router.post("/auth/users", status_code=201, tags=["auth"])
async def create_user(body: NewUser, request: Request, who: Identity = Depends(require("admin"))) -> dict:
    ctx: Ctx = request.app.state.ctx
    if await _to_thread(ctx.users.get, body.user):
        raise HTTPException(409, "user exists")
    await _to_thread(ctx.users.create, body.user, body.role, body.password, body.display_name)
    audit(ctx.engine, who.user, "user.create", body.user, {"role": body.role})
    return {"user": body.user, "role": body.role}


# ============================================================================================ device keys
@dataclass
class DeviceKey:
    key: bytes
    version: int
    rotated_at: datetime
    prev: bytes | None = None
    prev_until: datetime | None = None


class DeviceKeys:
    """Rotated per-device HMAC keys. A device without an entry uses its derived key HMAC(master, source_id)."""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.keys: dict[str, DeviceKey] = {}

    def load(self) -> None:
        rows = execute(self.ctx.engine, "select source_id, pgp_sym_decrypt(key_enc, :s) as k, version, rotated_at, "
                       "case when prev_enc is null then null else pgp_sym_decrypt(prev_enc, :s) end as p, prev_until "
                       "from device_keys", s=self.ctx.settings.key_secret)
        for r in rows:
            self.keys[r["source_id"]] = DeviceKey(bytes.fromhex(r["k"]), r["version"], r["rotated_at"],
                                                  bytes.fromhex(r["p"]) if r["p"] else None, r["prev_until"])

    def valid(self, source_id: str) -> list[bytes] | None:
        """Keys a message from source_id may be signed with, or None to use the derived key."""
        k = self.keys.get(source_id)
        if k is None:
            return None
        out = [k.key]
        if k.prev is not None and k.prev_until is not None and datetime.now(UTC) < k.prev_until:
            out.append(k.prev)
        return out

    def rotate(self, source_id: str, grace_s: float) -> tuple[bytes, DeviceKey]:
        from sim.reality.telemetry import Signer
        old = self.keys.get(source_id)
        prev = old.key if old else Signer(self.ctx.settings.master_key).key(source_id)
        new = secrets.token_bytes(32)
        now = datetime.now(UTC)
        dk = DeviceKey(new, (old.version if old else 0) + 1, now, prev, now + timedelta(seconds=grace_s))
        execute(self.ctx.engine, "insert into device_keys(source_id, key_enc, version, rotated_at, prev_enc, prev_until) values "
                "(:src, pgp_sym_encrypt(:k, :s), :v, :t, pgp_sym_encrypt(:p, :s), :pu) on conflict (source_id) do update set "
                "key_enc = excluded.key_enc, version = excluded.version, rotated_at = excluded.rotated_at, "
                "prev_enc = excluded.prev_enc, prev_until = excluded.prev_until",
                src=source_id, k=new.hex(), s=self.ctx.settings.key_secret, v=dk.version, t=now, p=prev.hex(), pu=dk.prev_until)
        self.keys[source_id] = dk
        return new, dk


@router.post("/devices/{source_id}/rotate", tags=["devices"])
async def rotate_device_key(source_id: str, request: Request, grace_s: float = Query(300, ge=0, le=7 * 86400),
                            who: Identity = Depends(require("security"))) -> dict:
    """New HMAC key for a device (shown once). The old key stays valid for grace_s seconds, then fails L2."""
    ctx: Ctx = request.app.state.ctx
    if not (3 <= len(source_id) <= 96) or ":" not in source_id:
        raise HTTPException(422, "source ids look like gps:<vehicle>, wms:<dc>, asn:<supplier>, port:<port>")
    key, dk = await _to_thread(ctx.device_keys.rotate, source_id, grace_s)
    audit(ctx.engine, who.user, "device.rotate", source_id, {"version": dk.version, "grace_s": grace_s})
    ctx.live.alert("sec", f"Device key rotated · {source_id}", f"version {dk.version} by {who.user}; the old key is valid "
                   f"for {grace_s:.0f} s", kind="security")
    return {"source_id": source_id, "version": dk.version, "key": key.hex(), "algorithm": "HMAC-SHA256",
            "previous_key_valid_until": dk.prev_until.isoformat(timespec="seconds") if dk.prev_until else None}


@router.get("/devices", tags=["devices"])
async def list_devices(request: Request, who: Identity = Depends(require("security"))) -> list[dict]:
    ctx: Ctx = request.app.state.ctx
    now = datetime.now(UTC)
    return [{"source_id": s, "version": k.version, "rotated_at": k.rotated_at.isoformat(timespec="seconds"),
             "previous_key_valid": bool(k.prev_until and now < k.prev_until)} for s, k in sorted(ctx.device_keys.keys.items())]


# ============================================================================================ audit
@router.get("/audit", tags=["security"])
async def audit_log(request: Request, limit: int = Query(100, ge=1, le=1000), action: str | None = Query(None, max_length=64),
                    who: Identity = Depends(require("security"))) -> list[dict]:
    """Every login, logout, plan apply, chaos injection, key rotation, disruption push and Copilot proposal."""
    ctx: Ctx = request.app.state.ctx
    if ctx.engine is None:
        rows = [r for r in RECENT_AUDIT if action is None or r["action"].startswith(action)]
        return rows[:limit]
    rows = execute(ctx.engine, "select id, ts, user_id, action, target, details from audit_log "
                   "where (cast(:a as text) is null or action like :a || '%') order by id desc limit :n", a=action, n=limit)
    for r in rows:
        r["ts"] = r["ts"].isoformat(timespec="seconds") if r.get("ts") else None
    return rows


# ============================================================================================ middleware
def make_limiter(redis_url: str) -> Limiter:
    """slowapi rate limiter on the shared Redis (fixed one-minute windows, keyed by bucket + client IP), so the limits
    hold across API replicas. SecurityMiddleware applies it per bucket: REST, auth, Copilot."""
    return Limiter(key_func=_ip, storage_uri=redis_url, strategy="fixed-window", key_prefix="rl",
                   headers_enabled=False)


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")  # set by Caddy; the API is only reachable through it in prod
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


async def _to_thread(fn, *a):
    import asyncio
    return await asyncio.to_thread(fn, *a)


API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
DOCS_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")


class SecurityMiddleware(BaseHTTPMiddleware):
    """Request-size limits, per-IP rate limits (Redis fixed window) and security headers on every response."""

    async def dispatch(self, request: Request, call_next):
        ctx = request.app.state.ctx
        s = ctx.settings
        path = request.url.path
        ingest = path.startswith("/api/v1/ingest")
        length = request.headers.get("content-length")
        cap = s.max_ingest_body_bytes if ingest else s.max_body_bytes
        if length is not None and (not length.isdigit() or int(length) > cap):
            return self._headers(JSONResponse({"detail": f"request body larger than {cap} bytes"}, status_code=413), request, s)
        bucket, limit = self._bucket(path, s)
        if bucket and request.method != "OPTIONS":
            item = RateLimitItemPerMinute(limit)
            strategy = ctx.limiter.limiter  # slowapi Limiter -> limits fixed-window strategy on Redis
            try:
                allowed = await _to_thread(strategy.hit, item, bucket, _ip(request))
            except Exception:  # a Redis hiccup must not take the API down
                allowed = True
            if not allowed:
                retry = max(1, int(60 - time.time() % 60))
                resp = JSONResponse({"detail": "rate limit exceeded"}, status_code=429, headers={"Retry-After": str(retry)})
                return self._headers(resp, request, s)
        response = await call_next(request)
        if bucket:
            response.headers["X-RateLimit-Limit"] = str(limit)
        return self._headers(response, request, s)

    @staticmethod
    def _bucket(path: str, s) -> tuple[str | None, int]:
        if path.startswith("/api/v1/ingest") or path in ("/healthz", "/readyz", "/metrics"):
            return None, 0
        if path.startswith("/api/v1/auth/token") or path.startswith("/api/v1/auth/refresh"):
            return "auth", s.auth_rate_per_min
        if path.startswith("/api/v1/copilot/chat"):
            return "copilot", s.copilot_rate_per_min
        if path.startswith("/api/") or path.startswith("/ws/"):
            return "api", s.rate_limit_per_min
        return None, 0

    @staticmethod
    def _headers(response: Response, request: Request, s) -> Response:
        h = response.headers
        docs = request.url.path in ("/docs", "/redoc") or request.url.path.startswith("/docs/")
        h.setdefault("Content-Security-Policy", DOCS_CSP if docs else API_CSP)
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if s.prod or request.headers.get("x-forwarded-proto") == "https":
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

"""Runtime settings (environment variables, with local-dev defaults).

    AEGIS_DB_URL          postgresql+psycopg://localhost/aegis     ("" disables persistence)
    AEGIS_REDIS_URL       redis://localhost:6379/3                  (a dedicated logical DB)
    AEGIS_MASTER_KEY      device-key master secret (per-device key = HMAC(master, source_id))
    AEGIS_PUBLISHERS      "reality-emulator:<key>,..." publisher id -> batch-signing key
    AEGIS_TOKENS          "admin:<token>,planner:<token>,security:<token>,viewer:<token>" bearer tokens
    AEGIS_TWIN_FACTOR     wall-seconds per simulated hour for the live twin (60 = 1 sim-minute per second; 0 = paused)
    AEGIS_TWIN_START      simulation start (ISO-8601)
    AEGIS_TWIN_WARMUP_H   fast-forward the live twin this many simulated hours before pacing it (match the emulator's --warmup-h)
    AEGIS_WS_HZ           WebSocket diff rate (5-10)
    AEGIS_SCENARIO_WORKERS  Monte Carlo worker processes
    AEGIS_BACKGROUND      "0" disables the in-process workers (trust, twin-state, broadcaster, live twin)
  security (Phase 8)
    AEGIS_ENV             dev | prod. prod refuses to start with default / missing secrets, drops the static dev
                          bearer tokens and demo users, pins CORS to AEGIS_CORS_ORIGINS and turns on HSTS
    AEGIS_JWT_SECRET      HS256 signing secret for access / refresh tokens (>= 32 chars in prod)
    AEGIS_ACCESS_TTL_S    access-token lifetime (default 900 = 15 min); AEGIS_REFRESH_TTL_S (default 7 days)
    AEGIS_USERS           "user:role:password,..." seeded into the users table (argon2id hashes); dev default:
                          admin / planner / security / viewer with password aegis-<role>
    AEGIS_CORS_ORIGINS    comma-separated allowed web origins (prod); dev allows any localhost port
    AEGIS_RATE_LIMIT      requests per minute per client IP for the REST API (default 1200); auth endpoints 20,
                          Copilot 30; ingest is authenticated per publisher batch and not IP-limited
    AEGIS_KEY_SECRET      secret for pgcrypto-encrypting rotated device keys at rest (default: the master key)
Dev defaults are for a laptop demo only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from sim.reality.telemetry import DEFAULT_MASTER

# development-only values; check_production() refuses to start with either when AEGIS_ENV=prod
DEV_JWT_SECRET = "dev-only-jwt-secret-change-me-0123456789"  # noqa: S105
DEV_USERS = "admin:admin:aegis-admin,planner:planner:aegis-planner,security:security:aegis-security,viewer:viewer:aegis-viewer"

ROLE_RANK = {"viewer": 0, "planner": 1, "security": 2, "admin": 3}


def _pairs(raw: str) -> dict[str, str]:
    out = {}
    for item in filter(None, (x.strip() for x in raw.split(","))):
        k, _, v = item.partition(":")
        out[k.strip()] = v.strip()
    return out


@dataclass
class Settings:
    db_url: str = field(default_factory=lambda: os.environ.get("AEGIS_DB_URL", "postgresql+psycopg://localhost/aegis"))
    redis_url: str = field(default_factory=lambda: os.environ.get("AEGIS_REDIS_URL", "redis://localhost:6379/3"))
    master_key: bytes = field(default_factory=lambda: os.environ.get("AEGIS_MASTER_KEY", DEFAULT_MASTER.decode()).encode())
    publishers: dict[str, bytes] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)     # token -> role
    twin_factor: float = field(default_factory=lambda: float(os.environ.get("AEGIS_TWIN_FACTOR", "60")))
    twin_start: str = field(default_factory=lambda: os.environ.get("AEGIS_TWIN_START", "2026-10-15T00:00:00+05:30"))
    twin_warmup_h: float = field(default_factory=lambda: float(os.environ.get("AEGIS_TWIN_WARMUP_H", "0")))
    ws_hz: float = field(default_factory=lambda: float(os.environ.get("AEGIS_WS_HZ", "5")))
    scenario_workers: int = field(default_factory=lambda: int(os.environ.get("AEGIS_SCENARIO_WORKERS", str(os.cpu_count() or 2))))
    background: bool = field(default_factory=lambda: os.environ.get("AEGIS_BACKGROUND", "1") != "0")
    max_batch: int = 5000
    ts_window_s: float = 300.0          # publisher timestamp tolerance (batch replay protection)
    stream_maxlen: int = 500_000
    source_sla_s: float = 30.0          # simulated seconds of silence before a source is SLA_SILENT
    cache_ttl_s: int = 7 * 24 * 3600
    # ------------------------------------------------------------ security (Phase 8)
    env: str = field(default_factory=lambda: os.environ.get("AEGIS_ENV", "dev"))
    jwt_secret: str = field(default_factory=lambda: os.environ.get("AEGIS_JWT_SECRET", ""))
    access_ttl_s: int = field(default_factory=lambda: int(os.environ.get("AEGIS_ACCESS_TTL_S", "900")))
    refresh_ttl_s: int = field(default_factory=lambda: int(os.environ.get("AEGIS_REFRESH_TTL_S", str(7 * 24 * 3600))))
    users: list[tuple[str, str, str]] = field(default_factory=list)   # (user, role, password)
    cors_origins: list[str] = field(default_factory=lambda: [o.strip() for o in os.environ.get("AEGIS_CORS_ORIGINS", "").split(",") if o.strip()])
    rate_limit_per_min: int = field(default_factory=lambda: int(os.environ.get("AEGIS_RATE_LIMIT", "1200")))
    auth_rate_per_min: int = 20
    copilot_rate_per_min: int = 30
    max_body_bytes: int = 1_000_000
    max_ingest_body_bytes: int = 8_000_000
    key_secret: str = field(default_factory=lambda: os.environ.get("AEGIS_KEY_SECRET", ""))
    ws_auth: bool = field(default_factory=lambda: os.environ.get("AEGIS_WS_AUTH", "") == "1" or os.environ.get("AEGIS_ENV") == "prod")
    login_max_failures: int = 5
    login_lockout_s: int = 900

    @property
    def prod(self) -> bool:
        return self.env == "prod"

    def __post_init__(self):
        if not self.publishers:
            raw = os.environ.get("AEGIS_PUBLISHERS", f"reality-emulator:{self.master_key.decode()}")
            self.publishers = {k: v.encode() for k, v in _pairs(raw).items()}
        if not self.tokens:  # static bearer tokens: dev convenience only; prod needs them set explicitly
            raw = os.environ.get("AEGIS_TOKENS", "" if self.prod else
                                 "admin:dev-admin,planner:dev-planner,security:dev-security,viewer:dev-viewer")
            self.tokens = {tok: role for role, tok in _pairs(raw).items()}
        if not self.users:
            raw = os.environ.get("AEGIS_USERS", "" if self.prod else DEV_USERS)
            for item in filter(None, (x.strip() for x in raw.split(","))):
                user, role, pw = (item.split(":", 2) + ["", ""])[:3]
                if role not in ROLE_RANK or not pw:
                    raise ValueError(f"AEGIS_USERS entry for {user!r}: need user:role:password with a known role")
                self.users.append((user, role, pw))
        if self.prod:
            self.ws_auth = True
        if not self.jwt_secret and not self.prod:
            self.jwt_secret = DEV_JWT_SECRET
        if not self.key_secret:
            self.key_secret = self.master_key.decode()

    def check_production(self) -> list[str]:
        """Problems that make this configuration unsafe to expose (enforced at startup when AEGIS_ENV=prod)."""
        issues = []
        if len(self.jwt_secret) < 32 or self.jwt_secret == DEV_JWT_SECRET:
            issues.append("AEGIS_JWT_SECRET must be set to a random secret of at least 32 characters")
        if self.master_key == DEFAULT_MASTER:
            issues.append("AEGIS_MASTER_KEY is the public development key")
        if any(k == DEFAULT_MASTER for k in self.publishers.values()):
            issues.append("AEGIS_PUBLISHERS uses the public development key")
        if any(tok.startswith("dev-") for tok in self.tokens):
            issues.append("AEGIS_TOKENS contains development tokens")
        if any(pw == f"aegis-{role}" for _, role, pw in self.users):
            issues.append("AEGIS_USERS contains development passwords")
        if not self.cors_origins:
            issues.append("AEGIS_CORS_ORIGINS must list the web origin(s)")
        return issues


settings = Settings()

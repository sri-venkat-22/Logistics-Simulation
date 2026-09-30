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
Dev defaults are for a laptop demo only; Phase 8 replaces bearer tokens with JWT + RBAC and moves the
secrets into the environment / secret store.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from sim.reality.telemetry import DEFAULT_MASTER

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

    def __post_init__(self):
        if not self.publishers:
            raw = os.environ.get("AEGIS_PUBLISHERS", f"reality-emulator:{self.master_key.decode()}")
            self.publishers = {k: v.encode() for k, v in _pairs(raw).items()}
        if not self.tokens:
            raw = os.environ.get("AEGIS_TOKENS", "admin:dev-admin,planner:dev-planner,security:dev-security,viewer:dev-viewer")
            self.tokens = {tok: role for role, tok in _pairs(raw).items()}


settings = Settings()

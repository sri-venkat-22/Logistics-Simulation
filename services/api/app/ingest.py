"""Ingest gateway: POST /api/v1/ingest/{telemetry|inventory|supplier} and WS /api/v1/ingest/stream.

Batch authentication (publisher): headers X-AEGIS-Publisher, X-AEGIS-Timestamp (unix seconds),
X-AEGIS-Nonce, X-AEGIS-Signature = hex HMAC-SHA256(publisher_key, f"{ts}\\n{nonce}\\n" + body).
The timestamp must be within +-300 s and the nonce unused (Redis SET NX, TTL 2x window), else 401.
Each message is then validated strictly (trust layer 1: envelope + typed payload, finite numbers, ranges,
kind must match the channel). Valid messages are XADDed to `telemetry.raw` in one pipeline; invalid ones
go to the `quarantine` stream with a reason code and never reach the twin. The response is
202 {"accepted", "rejected", "reasons"}.
WebSocket: connect with ?publisher=&ts=&nonce=&sig= where sig = HMAC(key, f"{ts}\\n{nonce}\\nws"); then send
JSON frames ({"messages": [...]} or a single message); each frame is answered with its accept/reject counts.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections import Counter
from typing import TYPE_CHECKING, Any

import orjson
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from services.api.app import metrics as M
from sim.reality.schemas import CHANNEL, PAYLOAD, Envelope

if TYPE_CHECKING:
    from services.api.app.main import Ctx

router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])
RAW, QUAR = "telemetry.raw", "quarantine"
CHANNELS = ("telemetry", "inventory", "supplier")

_RANGE = {"greater_than", "greater_than_equal", "less_than", "less_than_equal", "finite_number", "value_error",
          "string_too_short", "string_too_long", "string_pattern_mismatch", "literal_error", "too_short", "too_long"}


def reason_for(err: ValidationError) -> str:
    t = err.errors()[0]["type"]
    if t == "missing":
        return "SCHEMA_MISSING"
    if t == "extra_forbidden":
        return "SCHEMA_EXTRA"
    if t in _RANGE:
        return "SCHEMA_RANGE"
    return "SCHEMA_TYPE"


def validate(m, channel: str) -> str | None:
    """Trust layer 1. Returns a reason code, or None if the message is well-formed."""
    if not isinstance(m, dict):
        return "SCHEMA_ENVELOPE"
    try:
        env = Envelope.model_validate(m)
    except ValidationError as e:
        return reason_for(e)
    if CHANNEL[env.kind] != channel:
        return "SCHEMA_KIND"
    try:
        PAYLOAD[env.kind].model_validate(env.payload)
    except ValidationError as e:
        return reason_for(e)
    return None


def loads(body: bytes):
    try:
        return orjson.loads(body)
    except orjson.JSONDecodeError:
        return json.loads(body)  # tolerate NaN / Infinity tokens so layer 1 can reject those messages one by one


async def check_publisher(ctx: Ctx, publisher: str | None, ts: str | None, nonce: str | None, sig: str | None,
                          signed: bytes) -> str:
    key = ctx.settings.publishers.get(publisher or "")
    if key is None:
        M.INGEST_AUTH_FAIL.labels("unknown_publisher").inc()
        raise HTTPException(401, "unknown publisher")
    try:
        t = float(ts or "")
    except ValueError:
        raise HTTPException(401, "bad timestamp") from None
    if abs(time.time() - t) > ctx.settings.ts_window_s:
        M.INGEST_AUTH_FAIL.labels("stale").inc()
        raise HTTPException(401, "timestamp outside window")
    want = hmac.new(key, f"{ts}\n{nonce}\n".encode() + signed, hashlib.sha256).hexdigest()
    if not sig or not hmac.compare_digest(want, sig):
        M.INGEST_AUTH_FAIL.labels("signature").inc()
        raise HTTPException(401, "bad signature")
    if not await ctx.redis.set(f"nonce:{publisher}:{nonce}", 1, nx=True, ex=int(2 * ctx.settings.ts_window_s)):
        M.INGEST_AUTH_FAIL.labels("replay").inc()
        raise HTTPException(401, "nonce already used (replayed batch)")
    return publisher


async def process(ctx: Ctx, channel: str, msgs: list, publisher: str) -> dict:
    t0 = time.perf_counter()
    if len(msgs) > ctx.settings.max_batch:
        raise HTTPException(413, f"batch larger than {ctx.settings.max_batch}")
    reasons: Counter = Counter()
    pipe = ctx.redis.pipeline(transaction=False)
    accepted = 0
    maxlen = ctx.settings.stream_maxlen
    for m in msgs:
        code = validate(m, channel)
        if code is None:
            pipe.xadd(RAW, {"m": orjson.dumps(m), "ch": channel, "pub": publisher}, maxlen=maxlen, approximate=True)
            accepted += 1
        else:
            reasons[code] += 1
            ctx.quarantine(m, code, "L1")
            pipe.xadd(QUAR, {"m": json.dumps(m, allow_nan=True, default=str)[:4000], "reason": code, "layer": "L1"},
                      maxlen=100_000, approximate=True)
    if accepted or reasons:
        await pipe.execute()
    rejected = sum(reasons.values())
    ctx.live.counters["ingest_accepted"] += accepted
    ctx.live.counters["ingest_rejected"] += rejected
    M.INGEST_MSGS.labels(channel, "accepted").inc(accepted)
    M.INGEST_MSGS.labels(channel, "rejected").inc(rejected)
    for r, n in reasons.items():
        M.REJECTS.labels("L1", r).inc(n)
    M.INGEST_BATCH.labels(channel).observe(time.perf_counter() - t0)
    return {"accepted": accepted, "rejected": rejected, "reasons": dict(reasons)}


@router.post("/{channel}", status_code=202)
async def ingest(channel: str, request: Request) -> dict:
    if channel not in CHANNELS:
        raise HTTPException(404, f"unknown channel {channel!r}; use one of {CHANNELS}")
    ctx: Ctx = request.app.state.ctx
    body = await request.body()
    h = request.headers
    pub = await check_publisher(ctx, h.get("x-aegis-publisher"), h.get("x-aegis-timestamp"), h.get("x-aegis-nonce"),
                                h.get("x-aegis-signature"), body)
    try:
        data = loads(body)
    except ValueError:
        raise HTTPException(400, "body is not JSON") from None
    msgs = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(msgs, list):
        raise HTTPException(400, 'body must be {"messages": [...]}')
    return await process(ctx, channel, msgs, pub)


@router.websocket("/stream")
async def ingest_stream(ws: WebSocket) -> None:
    ctx: Ctx = ws.app.state.ctx
    q = ws.query_params
    try:
        pub = await check_publisher(ctx, q.get("publisher"), q.get("ts"), q.get("nonce"), q.get("sig"), b"ws")
    except HTTPException as e:
        await ws.close(code=4401, reason=str(e.detail))
        return
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = loads(raw.encode())
            except ValueError:
                await ws.send_json({"error": "not JSON"})
                continue
            msgs = data.get("messages") if isinstance(data, dict) and "messages" in data else [data]
            by_ch: dict[str, list] = {}
            for m in msgs:
                kind = m.get("kind") if isinstance(m, dict) else None
                by_ch.setdefault(CHANNEL.get(kind, "telemetry"), []).append(m)
            total: dict[str, Any] = {"accepted": 0, "rejected": 0, "reasons": Counter()}
            for ch, part in by_ch.items():
                r = await process(ctx, ch, part, pub)
                total["accepted"] += r["accepted"]
                total["rejected"] += r["rejected"]
                total["reasons"].update(r["reasons"])
            await ws.send_json({**total, "reasons": dict(total["reasons"])})
    except WebSocketDisconnect:
        return


def ws_signature(key: bytes, ts: str, nonce: str) -> str:
    return hmac.new(key, f"{ts}\n{nonce}\nws".encode(), hashlib.sha256).hexdigest()

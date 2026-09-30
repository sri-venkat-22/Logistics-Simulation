"""Ingest load test (NFR-1: >= 5,000 msgs/s).

    .venv/bin/python -m services.api.loadtest                     # starts an isolated API on :8100
    .venv/bin/python -m services.api.loadtest --url http://localhost:8000 --no-server

1. Generates unique, device-signed telemetry with the Reality Emulator (national trucks at 1 Hz).
2. Starts an isolated API (Redis DB 4, Postgres aegis_test, live twin paused) unless --no-server.
3. Posts batches of --batch messages with --concurrency parallel publishers, each batch with a fresh
   publisher signature and nonce, and measures accepted msgs/s at the gateway and request latency.
4. Waits until the trust and twin-state consumer groups have drained, which gives end-to-end
   pipeline throughput (ingest -> telemetry.raw -> trust -> telemetry.clean -> live state + Postgres COPY).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import orjson

from sim.paths import ROOT
from sim.reality.emulator import RealityEmulator
from sim.reality.schemas import CHANNEL
from sim.reality.telemetry import HttpSink, MemorySink


def generate(n: int, seed: int = 2024) -> list[dict]:
    sink = MemorySink()
    em = RealityEmulator(datetime(2026, 10, 15, tzinfo=ZoneInfo("Asia/Kolkata")), seed=seed, sink=sink,
                         national_gps_period_s=1.0, telemetry_from_h=60.0, perturb=None)
    t = 60.0
    while len(sink.messages) < n:
        t += 0.25
        em.run_until(t)
    return sink.messages[:n]


async def blast(url: str, msgs: list[dict], batch: int, concurrency: int) -> dict:
    signer = HttpSink(url)
    groups: dict[str, list[dict]] = {}
    for m in msgs:
        groups.setdefault(CHANNEL[m["kind"]], []).append(m)
    bodies = [(ch, orjson.dumps({"messages": part[i:i + batch]})) for ch, part in groups.items() for i in range(0, len(part), batch)]
    lat: list[float] = []
    accepted = rejected = errors = 0
    queue: asyncio.Queue = asyncio.Queue()
    for b in bodies:
        queue.put_nowait(b)

    async def worker(client: httpx.AsyncClient):
        nonlocal accepted, rejected, errors
        while True:
            try:
                ch, body = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            t0 = time.perf_counter()
            r = await client.post(f"{url}/api/v1/ingest/{ch}", content=body, headers=signer.headers(body))
            lat.append(time.perf_counter() - t0)
            if r.status_code == 202:
                d = r.json()
                accepted += d["accepted"]
                rejected += d["rejected"]
            else:
                errors += 1

    async with httpx.AsyncClient(timeout=30, limits=httpx.Limits(max_connections=concurrency)) as client:
        t0 = time.perf_counter()
        await asyncio.gather(*(worker(client) for _ in range(concurrency)))
        wall = time.perf_counter() - t0
    lat.sort()
    q = lambda p: lat[min(len(lat) - 1, int(p * len(lat)))] * 1000  # noqa: E731
    return {"requests": len(bodies), "accepted": accepted, "rejected": rejected, "errors": errors, "wall_s": round(wall, 3),
            "gateway_msgs_s": round(accepted / wall), "p50_ms": round(q(0.5), 1), "p95_ms": round(q(0.95), 1),
            "p99_ms": round(q(0.99), 1)}


async def drain(url: str, expect: int, timeout_s: float = 120) -> float:
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=10) as client:
        while time.perf_counter() - t0 < timeout_s:
            p = (await client.get(f"{url}/api/v1/eval/pipeline")).json()
            if p["trust_processed"] >= expect and all(s["pending"] == 0 for s in p["streams"].values()):
                return time.perf_counter() - t0
            await asyncio.sleep(0.05)
    return float("nan")


def start_server(port: int) -> subprocess.Popen:
    env = {**os.environ, "AEGIS_REDIS_URL": "redis://localhost:6379/4", "AEGIS_DB_URL": "postgresql+psycopg://localhost/aegis_test",
           "AEGIS_TWIN_FACTOR": "0", "AEGIS_SCENARIO_WORKERS": "1"}
    subprocess.run(["redis-cli", "-n", "4", "flushdb"], check=False, capture_output=True)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "services.api.app.main:app", "--port", str(port), "--log-level",
                             "warning"], cwd=ROOT, env=env)
    for _ in range(200):
        try:
            if httpx.get(f"http://localhost:{port}/readyz", timeout=1).status_code == 200:
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    proc.kill()
    raise RuntimeError("API did not become ready")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m services.api.loadtest", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--messages", type=int, default=100_000)
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--url")
    ap.add_argument("--no-server", action="store_true")
    ap.add_argument("--out", type=Path, help="write results JSON")
    a = ap.parse_args(argv)
    t0 = time.perf_counter()
    msgs = generate(a.messages)
    print(f"generated {len(msgs):,} signed messages in {time.perf_counter() - t0:.1f} s")
    url = a.url or f"http://localhost:{a.port}"
    proc = None if a.no_server else start_server(a.port)
    try:
        res = asyncio.run(blast(url, msgs, a.batch, a.concurrency))
        res["drain_after_s"] = round(asyncio.run(drain(url, res["accepted"])), 3)
        res["pipeline_msgs_s"] = round(res["accepted"] / (res["wall_s"] + res["drain_after_s"]))
    finally:
        if proc:
            proc.terminate()
            proc.wait(10)
    res.update({"messages": len(msgs), "batch": a.batch, "concurrency": a.concurrency,
                "machine": f"{platform.machine()} · {platform.system()} · {os.cpu_count()} cores · Python {platform.python_version()}",
                "server": "1 uvicorn worker (ingest + trust + twin-state + Postgres COPY in one process)"})
    print(f"gateway: {res['gateway_msgs_s']:,} msgs/s accepted ({res['accepted']:,} in {res['wall_s']} s, {res['requests']} "
          f"requests of {a.batch}, {a.concurrency} publishers) · latency p50 {res['p50_ms']} ms, p95 {res['p95_ms']} ms, "
          f"p99 {res['p99_ms']} ms · errors {res['errors']}")
    print(f"end to end (through trust + twin state + DB): {res['pipeline_msgs_s']:,} msgs/s "
          f"(queues drained {res['drain_after_s']} s after the last request) · "
          f"NFR-1 target >= 5,000 msgs/s: {'PASS' if res['pipeline_msgs_s'] >= 5000 else 'FAIL'}")
    if a.out:
        a.out.write_text(json.dumps(res, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

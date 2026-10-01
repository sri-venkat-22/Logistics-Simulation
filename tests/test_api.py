"""Phase 4: ingest (auth, validation), trust + twin-state stages, WebSocket fan-out, REST, RBAC, scenario jobs,
migrations. Needs local Redis and Postgres (aegis_test); skipped otherwise."""
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import msgpack
import orjson
import pytest

TEST_DB = os.environ.get("AEGIS_TEST_DB_URL", "postgresql+psycopg://localhost/aegis_test")

try:
    import redis
    from sqlalchemy import create_engine, text

    redis.Redis.from_url("redis://localhost:6379/15").ping()
    _eng = create_engine(TEST_DB)
    with _eng.connect() as _c:
        _c.execute(text("select 1 from schema_info"))
    AVAILABLE = True
except Exception:  # pragma: no cover
    AVAILABLE = False

pytestmark = pytest.mark.skipif(not AVAILABLE, reason="needs local Redis + Postgres aegis_test (alembic upgrade head)")

from fastapi.testclient import TestClient  # noqa: E402

from services.api.app.config import Settings  # noqa: E402
from services.api.app.ingest import ws_signature  # noqa: E402
from services.api.app.main import create_app  # noqa: E402
from sim.reality.emulator import RealityEmulator  # noqa: E402
from sim.reality.schemas import CHANNEL  # noqa: E402
from sim.reality.telemetry import DEFAULT_MASTER, HttpSink, MemorySink, Signer  # noqa: E402

START = datetime(2026, 10, 15, tzinfo=ZoneInfo("Asia/Kolkata"))
PLANNER = {"Authorization": "Bearer dev-planner"}
ADMIN = {"Authorization": "Bearer dev-admin"}


@pytest.fixture(scope="module")
def client():
    redis.Redis.from_url("redis://localhost:6379/15").flushdb()
    app = create_app(Settings(redis_url="redis://localhost:6379/15", db_url=TEST_DB,
                              twin_factor=0, scenario_workers=2, ws_hz=10))
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def telemetry():
    sink = MemorySink()
    em = RealityEmulator(START, seed=5, sink=sink, national_gps_period_s=10, telemetry_from_h=60)
    em.run_until(60.2)
    return sink.messages


def post(c, ch, msgs, headers=None):
    body = orjson.dumps({"messages": msgs})
    return c.post(f"/api/v1/ingest/{ch}", content=body, headers=headers or HttpSink("x").headers(body))


def wait_for(fn, timeout=10.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def test_health_ready_and_schema(client):
    assert client.get("/healthz").json() == {"ok": True}
    r = client.get("/readyz").json()
    assert r["ready"] and r["checks"] == {"redis": True, "db": True, "twin": True}
    assert r["timeseries_mode"] in ("timescale", "plain+brin")
    with _eng.connect() as c:
        tables = {x for (x,) in c.execute(text("select tablename from pg_tables where schemaname = 'public'"))}
    assert {"nodes", "lanes", "skus", "inventory", "orders", "shipments", "telemetry", "sources", "quarantine", "disruptions",
            "scenarios", "plans", "predictions", "attack_labels", "audit_log", "users", "roles"} <= tables


def test_publisher_auth(client, telemetry):
    msgs = telemetry[:3]
    body = orjson.dumps({"messages": msgs})
    h = HttpSink("x").headers(body)
    assert client.post("/api/v1/ingest/telemetry", content=body, headers={**h, "X-AEGIS-Publisher": "nobody"}).status_code == 401
    assert client.post("/api/v1/ingest/telemetry", content=body, headers={**h, "X-AEGIS-Signature": "0" * 64}).status_code == 401
    assert client.post("/api/v1/ingest/telemetry", content=body + b" ", headers=h).status_code == 401  # body tampered
    old = HttpSink("x")
    stale = old.headers(body)
    stale["X-AEGIS-Timestamp"] = f"{time.time() - 3600:.3f}"
    assert client.post("/api/v1/ingest/telemetry", content=body, headers=stale).status_code == 401
    assert client.post("/api/v1/ingest/telemetry", content=body, headers=h).status_code == 202
    assert client.post("/api/v1/ingest/telemetry", content=body, headers=h).status_code == 401  # replayed nonce
    assert client.post("/api/v1/ingest/nowhere", content=body, headers=HttpSink("x").headers(body)).status_code == 404


def test_layer1_validation_reason_codes(client, telemetry):
    gps = [m for m in telemetry if m["kind"] == "gps"][:5]
    signer = Signer(DEFAULT_MASTER)
    bad = []
    m = json.loads(json.dumps(gps[0]))
    del m["payload"]["lat"]
    bad.append(signer.sign(m))
    m = json.loads(json.dumps(gps[1]))
    m["payload"]["speed_kmh"] = float("nan")
    bad.append(signer.sign(m))
    m = json.loads(json.dumps(gps[2]))
    m["payload"]["lat"] = 123.0
    bad.append(signer.sign(m))
    body = json.dumps({"messages": bad + ["not a message"]}, allow_nan=True).encode()
    r = client.post("/api/v1/ingest/telemetry", content=body, headers=HttpSink("x").headers(body)).json()
    assert r == {"accepted": 0, "rejected": 4, "reasons": {"SCHEMA_MISSING": 1, "SCHEMA_RANGE": 2, "SCHEMA_ENVELOPE": 1}}
    stock = [m for m in telemetry if m["kind"] == "stock"][:1]
    assert post(client, "telemetry", stock).json()["reasons"] == {"SCHEMA_KIND": 1}  # stock belongs on /inventory


def test_pipeline_to_live_state_redis_and_db(client, telemetry):
    by: dict[str, list] = {}
    for m in telemetry:
        by.setdefault(CHANNEL[m["kind"]], []).append(m)
    total = 0
    for ch, part in by.items():
        r = post(client, ch, part).json()
        total += r["accepted"]
    assert total == len(telemetry)
    ctx = client.app.state.ctx
    wait_for(lambda: ctx.trust.processed >= total and ctx.twin_state.processed >= total - 10)
    k = client.get("/api/v1/kpis").json()
    assert k["vehicles_live"] > 5 and k["world_ts"].startswith("2026-10-17")
    rr = redis.Redis.from_url("redis://localhost:6379/15")
    vid = next(iter(ctx.live.vehicles))
    assert rr.hget(f"veh:{vid}", "lat") is not None
    assert rr.hget("inv:DC_HYD_SHAMSHABAD", "SKU_VAX") is not None
    assert ctx.graph.nodes["DC_HYD_SHAMSHABAD"]["stock_SKU_VAX"] > 0
    ctx.writer.flush()
    with _eng.connect() as c:
        assert c.execute(text("select count(*) from telemetry where vehicle_id = :v"), {"v": vid}).scalar() > 0
        assert c.execute(text("select count(*) from inventory")).scalar() >= 16


def test_trust_stage_quarantines_duplicates_teleports_and_forgeries(client, telemetry):
    ctx = client.app.state.ctx
    gps = [m for m in telemetry if m["kind"] == "gps"]
    before = dict(ctx.live.reject_reasons)
    post(client, "telemetry", gps[:2])                       # re-sent, ~12 min behind their sources: replays
    last = max((m for m in gps if m["payload"]["vehicle_id"] == gps[0]["payload"]["vehicle_id"]), key=lambda m: m["ts"])
    tele = json.loads(json.dumps(last))
    tele["msg_id"] = "teleport000000000001"
    tele["seq"] += 1
    tele["ts"] = tele["ts"].replace(".000", ".500")
    tele["payload"]["lat"] += 5.0                             # ~550 km in half a second
    signer = Signer(DEFAULT_MASTER)
    forged = json.loads(json.dumps(last))
    forged["msg_id"] = "forged00000000000001"
    forged["sig"] = "f" * 64
    post(client, "telemetry", [signer.sign(tele), forged])

    def delta(key):
        return ctx.live.reject_reasons.get(key, 0) - before.get(key, 0)
    wait_for(lambda: delta(("L3", "REPLAY_NONCE")) >= 2 and delta(("L4", "PHYSICS_TELEPORT")) >= 1 and delta(("L2", "HMAC_INVALID")) >= 1)
    q = client.get("/api/v1/trust/quarantine").json()
    assert {"L2", "L3", "L4"} <= {x["layer"] for x in q}
    assert client.get("/api/v1/trust/stats").json()["by_layer"]["L3"] >= 2


def test_ws_ingest_stream(client, telemetry):
    ts, nonce = f"{time.time():.3f}", "wsnonce-test-1"
    sig = ws_signature(DEFAULT_MASTER, ts, nonce)
    with client.websocket_connect(f"/api/v1/ingest/stream?publisher=reality-emulator&ts={ts}&nonce={nonce}&sig={sig}") as ws:
        ws.send_text(orjson.dumps({"messages": [m for m in telemetry if m["kind"] == "port"][:2]}).decode())
        r = ws.receive_json()
        assert r["accepted"] + r["rejected"] == 2


def test_ws_live_snapshot_then_diffs(client, telemetry):
    with client.websocket_connect("/ws/live") as ws:
        snap = msgpack.unpackb(ws.receive_bytes())
        assert snap["type"] == "snapshot" and snap["vehicles"] and len(snap["vehicles"][0]) == 9 and snap["inventory"]
        gps = [m for m in telemetry if m["kind"] == "gps"]
        signer = Signer(DEFAULT_MASTER)
        fresh = []
        for i, m in enumerate(gps[-3:]):
            x = json.loads(json.dumps(m))
            x["msg_id"] = f"wsdiff{i:014d}"
            x["seq"] += 1000
            fresh.append(signer.sign(x))
        post(client, "telemetry", fresh)
        seen = set()
        for _ in range(40):
            f = msgpack.unpackb(ws.receive_bytes())
            assert f["type"] in ("diff", "snapshot")
            if f["type"] == "diff":
                seen |= {row[0] for row in f["vehicles"]["u"]}
                if "kpis" in f:
                    assert "ingest_rate" in f["kpis"]
            if {m["payload"]["vehicle_id"] for m in fresh} <= seen:
                break
        assert {m["payload"]["vehicle_id"] for m in fresh} <= seen
    with client.websocket_connect("/ws/live?format=json") as ws:
        assert json.loads(ws.receive_text())["type"] == "snapshot"


def test_rest_network_nodes_shipments(client):
    n = client.get("/api/v1/network").json()
    assert n["counts"] == {"nodes": 28, "lanes": 53} and len(n["skus"]) == 3
    shm = next(x for x in n["nodes"] if x["id"] == "DC_HYD_SHAMSHABAD")
    assert shm["tts_d"] is not None and shm["ttr_d"] == 5.0
    d = client.get("/api/v1/nodes/DC_HYD_SHAMSHABAD").json()
    assert {r["sku"] for r in d["inventory"]} == {"SKU_VAX", "SKU_FMCG", "SKU_ELEC"}
    assert d["inventory"][0]["twin"]["s"] > 0
    assert client.get("/api/v1/nodes/NOPE").status_code == 404
    s = client.get("/api/v1/shipments").json()
    assert s["twin"] and s["vehicle_columns"][0] == "id"
    assert client.get("/api/v1/eval/verification").json()["pass"] is True
    assert client.get("/api/v1/eval/benchmark").json()["attacks"]["done"] >= 500


def test_chaos_requires_admin_and_queues_command(client):
    body = {"type": "gps_teleport", "params": {"n_pings": 2}}
    assert client.post("/api/v1/chaos/inject", json=body).status_code == 401
    assert client.post("/api/v1/chaos/inject", json=body, headers=PLANNER).status_code == 403
    r = client.post("/api/v1/chaos/inject", json=body, headers=ADMIN)
    assert r.status_code == 202 and r.json()["expected_reason"] == "PHYSICS_TELEPORT"
    rr = redis.Redis.from_url("redis://localhost:6379/15")
    last = rr.xrevrange("chaos.commands", count=1)[0][1]
    assert orjson.loads(last[b"c"])["id"] == r.json()["id"]
    assert client.post("/api/v1/chaos/inject", json={"type": "meteor"}, headers=ADMIN).status_code == 422
    with _eng.connect() as c:
        assert c.execute(text("select count(*) from audit_log where action = 'chaos.inject'")).scalar() >= 1


def test_scenario_job_cache_progress_deltas_optimize_apply(client):
    assert client.post("/api/v1/scenarios", json={"template": "cyclone", "n": 20}).status_code == 401
    assert client.post("/api/v1/scenarios", json={"template": "nope", "n": 20}, headers=PLANNER).status_code == 404
    req = {"template": "supplier_failure", "n": 20, "days": 15}
    st = client.post("/api/v1/scenarios", json=req, headers=PLANNER).json()
    assert st["status"] in ("queued", "running", "done") and st["baseline_id"]
    with client.websocket_connect(f"/ws/scenarios/{st['id']}") as ws:
        events = []
        while True:
            ev = ws.receive_json()
            events.append(ev)
            if ev["status"] in ("done", "error"):
                break
    assert events[-1]["status"] == "done" and events[-1]["done"] == 20
    g = wait_for(lambda: (x := client.get(f"/api/v1/scenarios/{st['id']}").json()).get("deltas") and x)
    res = g["result"]
    assert res["n"] == 20 and res["kpis"]["fill_rate"]["p10"] <= res["kpis"]["fill_rate"]["p90"]
    assert "DC_HYD_SHAMSHABAD/SKU_VAX" in res["series"] and "fill_rate" in g["deltas"]
    t0 = time.perf_counter()
    again = client.post("/api/v1/scenarios", json=req, headers=PLANNER).json()
    assert again["cached"] and again["status"] == "done" and time.perf_counter() - t0 < 0.2
    o = client.post(f"/api/v1/scenarios/{st['id']}/optimize?n=10", headers=PLANNER).json()
    assert o["candidates"][0]["name"] == "Do nothing" and len(o["candidates"]) >= 2
    g = wait_for(lambda: (x := client.get(f"/api/v1/scenarios/{st['id']}", params={"series": False}).json()).get("plans") and x, 90)
    plans = g["plans"]
    assert all("score" in p for p in plans) and plans == sorted(plans, key=lambda p: -p["score"])
    assert g.get("baseline_id") == st["baseline_id"] and "deltas" in client.get(f"/api/v1/scenarios/{st['id']}").json()  # the
    # "Do nothing" plan has the scenario's own spec: its cache hit must not overwrite the scenario job
    assert any(p["pareto"] for p in plans) and all("explanation" in p for p in plans)
    assert "cvar95_lakh" in plans[0] and client.get(f"/api/v1/scenarios/{st['id']}/plans", params={"cost": 5}).json()["weights"]["cost"] == 5
    buffer = next(p for p in plans if p["kind"] == "buffer")
    assert client.post(f"/api/v1/plans/{buffer['id']}/apply").status_code == 401
    ctx = client.app.state.ctx
    z0 = ctx.twin.twin.warehouses["DC_BLR"].policy["SKU_VAX"].z
    a = client.post(f"/api/v1/plans/{buffer['id']}/apply", headers=PLANNER).json()
    assert a["applied_by"] == "planner-token" and all(x["ok"] for x in a["acks"])
    assert ctx.twin.twin.warehouses["DC_BLR"].policy["SKU_VAX"].z == pytest.approx(z0 + 0.8)
    assert client.get(f"/api/v1/plans/{buffer['id']}").json()["name"].startswith("Buffer")


def test_metrics_exposed(client):
    t = client.get("/metrics").text
    for name in ("aegis_ingest_messages_total", "aegis_rejects_total", "aegis_clean_messages_total", "aegis_ws_frames_total",
                 "aegis_scenario_jobs_total", "aegis_db_rows_written"):
        assert name in t


def test_push_disruption_to_live_twin_and_end_it(client):
    ctx = client.app.state.ctx
    assert client.post("/api/v1/disruptions", json={"template": "port_closure"}).status_code == 401
    r = client.post("/api/v1/disruptions", json={"template": "port_closure", "duration_h": 24}, headers=PLANNER)
    assert r.status_code == 202 and r.json()["impact"]["nodes"] == {"PORT_CHENNAI": 0.0}
    assert {"DC_BLR/SKU_ELEC", "DC_HYD_SHAMSHABAD/SKU_ELEC"} <= set(r.json()["impact"]["dependents"])
    ctx.twin.advance(0.01)  # paused twin in tests: let the scenario process start
    effects = client.get("/api/v1/disruptions").json()
    eff = next(e for e in effects if e["type"] == "port_closure")
    assert eff["nodes"] == ["PORT_CHENNAI"]
    chennai = next(n for n in client.get("/api/v1/network").json()["nodes"] if n["id"] == "PORT_CHENNAI")
    assert chennai["twin_status"] == "closed"
    assert any(a.kind == "disruption" for a in ctx.live.alerts)
    assert client.delete(f"/api/v1/disruptions/{eff['id']}", headers=PLANNER).json()["ok"]
    assert not [e for e in client.get("/api/v1/disruptions").json() if e["type"] == "port_closure"]
    assert client.delete("/api/v1/disruptions/E999", headers=PLANNER).status_code == 404
    st = client.post("/api/v1/scenarios", json={"template": "cyclone", "n": 5, "days": 5}, headers=PLANNER).json()
    g = client.get(f"/api/v1/scenarios/{st['id']}").json()
    assert "PORT_CHENNAI" in g["impact"]["nodes"] and g["impact"]["lanes"] and g["impact"]["polygons"]


def sse(client, prompt, headers=PLANNER):
    with client.stream("POST", "/api/v1/copilot/chat", json={"messages": [{"role": "user", "content": prompt}]},
                       headers=headers) as r:
        assert r.status_code == 200
        return [orjson.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]


def test_copilot_offline_what_if_to_proposal(client, monkeypatch):
    monkeypatch.setenv("AEGIS_COPILOT", "offline")
    assert client.get("/api/v1/copilot/status").json()["mode"] == "offline"
    assert client.post("/api/v1/copilot/chat", json={"messages": [{"role": "user", "content": "hi"}]}).status_code == 401
    evs = sse(client, "What if the Patancheru plant has a 3 day outage? 12 runs over 8 days")
    calls = [e["name"] for e in evs if e["type"] == "tool_call"]
    assert calls == ["create_scenario", "run_scenario", "optimize", "propose_apply"]
    assert all(e["ok"] for e in evs if e["type"] == "tool_result")
    prop = next(e for e in evs if e["type"] == "proposal")
    assert prop["apply_url"].endswith("/apply") and prop["requires_role"] == "planner"
    assert evs[-1]["type"] == "done" and "".join(e["delta"] for e in evs if e["type"] == "text")
    ctx = client.app.state.ctx
    z = ctx.twin.twin.warehouses["DC_BLR"].policy["SKU_VAX"].z
    assert ctx.twin.twin.warehouses["DC_BLR"].policy["SKU_VAX"].z == z  # proposing never applies
    viewer = sse(client, "What if a cyclone closes Chennai port for 5 days?", headers={"Authorization": "Bearer dev-viewer"})
    denied = next(e for e in viewer if e["type"] == "tool_result")
    assert denied["ok"] is False and "planner" in denied["summary"]      # what-ifs need the planner role
    risk = sse(client, "Which nodes are most at risk?", headers={"Authorization": "Bearer dev-viewer"})
    assert [e["name"] for e in risk if e["type"] == "tool_call"] == ["find_at_risk_nodes"]

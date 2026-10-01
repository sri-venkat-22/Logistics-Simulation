"""Phase 8: OAuth2 password flow + JWT, RBAC, refresh rotation, logout revocation, login lockout, rate limits,
request-size limits, security headers, CORS, production config guard, device-key rotation, audit log."""
import hashlib
import hmac
import os
import time

import jwt
import orjson
import pytest

TEST_DB = os.environ.get("AEGIS_TEST_DB_URL", "postgresql+psycopg://localhost/aegis_test")

try:
    import redis
    from sqlalchemy import create_engine, text

    redis.Redis.from_url("redis://localhost:6379/14").ping()
    _eng = create_engine(TEST_DB)
    with _eng.connect() as _c:
        _c.execute(text("select 1 from device_keys limit 1"))
    AVAILABLE = True
except Exception:  # pragma: no cover
    AVAILABLE = False

pytestmark = pytest.mark.skipif(not AVAILABLE, reason="needs local Redis + Postgres aegis_test at alembic head")

from fastapi.testclient import TestClient  # noqa: E402

from services.api.app.config import Settings  # noqa: E402
from services.api.app.main import create_app  # noqa: E402
from sim.reality.telemetry import DEFAULT_MASTER, HttpSink, Signer, canonical  # noqa: E402


def make(**kw):
    redis.Redis.from_url("redis://localhost:6379/14").flushdb()
    base = dict(redis_url="redis://localhost:6379/14", db_url=TEST_DB, twin_factor=0,
                scenario_workers=1, background=False)
    return create_app(Settings(**{**base, **kw}))


@pytest.fixture(scope="module")
def client():
    with _eng.begin() as c:
        c.execute(text("delete from device_keys where source_id like 'gps:SEC-%'"))
    with TestClient(make(rate_limit_per_min=10_000, auth_rate_per_min=60)) as c:
        yield c


def login(c, user, pw):
    return c.post("/api/v1/auth/token", data={"username": user, "password": pw})


def bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_login_issues_jwts_and_rbac_uses_them(client):
    assert login(client, "planner", "wrong-password").status_code == 401
    r = login(client, "planner", "aegis-planner")
    assert r.status_code == 200
    t = r.json()
    assert t["token_type"] == "bearer" and t["role"] == "planner" and t["expires_in"] == 900
    claims = jwt.decode(t["access_token"], options={"verify_signature": False})
    assert claims["sub"] == "planner" and claims["typ"] == "access" and claims["aud"] == "aegis-api" and claims["exp"] - claims["iat"] == 900
    me = client.get("/api/v1/auth/me", headers=bearer(t["access_token"])).json()
    assert me == {"user": "planner", "role": "planner", "via": "jwt"}
    # planner may run what-ifs but not inject chaos or rotate keys (security) or manage users (admin)
    assert client.post("/api/v1/chaos/inject", json={"type": "gps_teleport"}, headers=bearer(t["access_token"])).status_code == 403
    assert client.get("/api/v1/auth/users", headers=bearer(t["access_token"])).status_code == 403
    sec = login(client, "security", "aegis-security").json()["access_token"]
    assert client.get("/api/v1/audit", headers=bearer(sec)).status_code == 200
    assert client.get("/api/v1/audit", headers=bearer(t["access_token"])).status_code == 403
    # the refresh token is not an access token
    assert client.get("/api/v1/auth/me", headers=bearer(t["refresh_token"])).status_code == 401


def test_tampered_expired_and_foreign_tokens_are_rejected(client):
    s = client.app.state.ctx.settings
    good = login(client, "viewer", "aegis-viewer").json()["access_token"]
    h, p, sig = good.split(".")
    assert client.get("/api/v1/auth/me", headers=bearer(f"{h}.{p}.{sig[::-1]}")).status_code == 401
    now = int(time.time())
    base = {"iss": "aegis-twin", "aud": "aegis-api", "sub": "mallory", "role": "admin", "typ": "access", "jti": "x" * 32,
            "iat": now - 3600, "nbf": now - 3600, "exp": now - 60}
    expired = jwt.encode(base, s.jwt_secret, algorithm="HS256")
    forged = jwt.encode({**base, "exp": now + 600}, "not-the-secret-" * 3, algorithm="HS256")
    none_alg = jwt.encode({**base, "exp": now + 600}, key=None, algorithm="none")
    for tok in (expired, forged, none_alg):
        assert client.get("/api/v1/auth/me", headers=bearer(tok)).status_code == 401


def test_refresh_rotates_and_detects_reuse_and_logout_revokes(client):
    t = login(client, "admin", "aegis-admin").json()
    r1 = client.post("/api/v1/auth/refresh", json={"refresh_token": t["refresh_token"]})
    assert r1.status_code == 200 and r1.json()["access_token"] != t["access_token"]
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401  # replay
    new = r1.json()
    assert client.get("/api/v1/auth/me", headers=bearer(new["access_token"])).status_code == 200
    assert client.post("/api/v1/auth/logout", json={"refresh_token": new["refresh_token"]},
                       headers=bearer(new["access_token"])).json()["ok"]
    assert client.get("/api/v1/auth/me", headers=bearer(new["access_token"])).status_code == 401      # access revoked
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": new["refresh_token"]}).status_code == 401
    rows = client.get("/api/v1/audit", params={"action": "log"}, headers=bearer(login(client, "admin", "aegis-admin").json()["access_token"])).json()
    assert {"login.success", "login.failure", "logout"} <= {r["action"] for r in rows}


def test_admin_creates_users_with_argon2(client):
    adm = bearer(login(client, "admin", "aegis-admin").json()["access_token"])
    user = f"analyst{int(time.time()) % 100000}"
    assert client.post("/api/v1/auth/users", json={"user": user, "role": "planner", "password": "short"}, headers=adm).status_code == 422
    assert client.post("/api/v1/auth/users", json={"user": user, "role": "planner", "password": "correct horse battery"},
                       headers=adm).status_code == 201
    with _eng.connect() as c:
        h = c.execute(text("select password_hash from users where id = :u"), {"u": user}).scalar()
    assert h.startswith("$argon2id$")
    tok = login(client, user, "correct horse battery").json()
    assert tok["role"] == "planner"
    with _eng.begin() as c:  # removing the user stops the refresh chain
        c.execute(text("delete from users where id = :u"), {"u": user})
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": tok["refresh_token"]}).status_code == 401


def test_login_lockout_after_repeated_failures(client):
    for _ in range(5):
        assert login(client, "security", "nope-nope").status_code == 401
    r = login(client, "security", "aegis-security")   # right password, but locked out now
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    redis.Redis.from_url("redis://localhost:6379/14").delete("lf:security")


def test_security_headers_cors_and_body_limit(client):
    r = client.get("/healthz")
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" not in r.headers                          # dev over plain http
    assert "strict-transport-security" in client.get("/healthz", headers={"x-forwarded-proto": "https"}).headers
    assert "cdn.jsdelivr.net" in client.get("/docs").headers["content-security-policy"]
    pre = client.options("/api/v1/kpis", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in pre.headers
    ok = client.options("/api/v1/kpis", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    big = client.post("/api/v1/copilot/chat", content=b"x" * 1_100_000, headers={"content-type": "application/json"})
    assert big.status_code == 413


def test_rate_limits():
    with TestClient(make(rate_limit_per_min=5, auth_rate_per_min=3)) as c:
        codes = [c.get("/api/v1/kpis").status_code for _ in range(7)]
        assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
        assert [login(c, "viewer", "x").status_code for _ in range(4)][-1] == 429
        assert c.get("/healthz").status_code == 200                                  # probes are never limited


def test_production_mode_refuses_default_secrets(monkeypatch):
    s = Settings(env="prod", redis_url="redis://localhost:6379/14", db_url="", twin_factor=0, background=False)
    assert s.tokens == {} and s.users == [] and len(s.check_production()) >= 3
    with pytest.raises(RuntimeError, match="refusing to start"):
        with TestClient(create_app(s)):
            pass
    good = Settings(env="prod", redis_url="redis://localhost:6379/14", db_url="", twin_factor=0, background=False,
                    jwt_secret="x" * 48, master_key=b"m" * 32, publishers={"pub": b"p" * 32},
                    cors_origins=["https://aegis.example.tech"])
    assert good.check_production() == []


def test_device_key_rotation_with_grace_period(client):
    ctx = client.app.state.ctx
    sec = bearer(login(client, "security", "aegis-security").json()["access_token"])
    src = "gps:SEC-TRK-1"
    signer = Signer(DEFAULT_MASTER)
    msg = {"msg_id": "sec00000000000000001", "source_id": src, "kind": "gps", "ts": "2026-10-17T10:00:00.000+05:30", "seq": 1,
           "payload": {"vehicle_id": "SEC-TRK-1", "lat": 17.3, "lon": 78.4, "speed_kmh": 40.0, "heading_deg": 10.0, "hdop": 1.0,
                       "shipment_id": None, "scope": "national"}}
    old_signed = signer.sign(dict(msg))
    assert ctx.trust.verify(old_signed)
    assert client.post(f"/api/v1/devices/{src}/rotate", headers=bearer(login(client, "planner", "aegis-planner").json()["access_token"])).status_code == 403
    r = client.post(f"/api/v1/devices/{src}/rotate", params={"grace_s": 60}, headers=sec).json()
    assert r["version"] == 1 and len(bytes.fromhex(r["key"])) == 32
    assert ctx.trust.verify(old_signed)                                    # old key still valid during the grace period
    new_signed = {**msg, "sig": hmac.new(bytes.fromhex(r["key"]), canonical(msg).encode(), hashlib.sha256).hexdigest()}
    assert ctx.trust.verify(new_signed)
    r2 = client.post(f"/api/v1/devices/{src}/rotate", params={"grace_s": 0}, headers=sec).json()
    assert r2["version"] == 2 and not ctx.trust.verify(old_signed) and not ctx.trust.verify(new_signed)  # both retired
    with _eng.connect() as c:
        enc = c.execute(text("select key_enc from device_keys where source_id = :s"), {"s": src}).scalar()
    assert bytes.fromhex(r2["key"]).hex().encode() not in bytes(enc)            # encrypted at rest (pgcrypto)
    ctx.device_keys.keys.clear()
    ctx.device_keys.load()                                                      # survives a restart
    assert ctx.device_keys.keys[src].version == 2 and ctx.device_keys.keys[src].key.hex() == r2["key"]
    listed = client.get("/api/v1/devices", headers=sec).json()
    assert any(d["source_id"] == src and d["version"] == 2 and "key" not in d for d in listed)


def test_ingest_still_needs_publisher_hmac_and_is_not_ip_limited():
    with TestClient(make(rate_limit_per_min=2)) as c:
        body = orjson.dumps({"messages": []})
        sink = HttpSink("x")  # one publisher: its nonces never repeat
        codes = [c.post("/api/v1/ingest/telemetry", content=body, headers=sink.headers(body)).status_code for _ in range(4)]
        assert codes == [202] * 4
        assert c.post("/api/v1/ingest/telemetry", content=body, headers={"content-type": "application/json"}).status_code == 401


def test_websocket_requires_a_token_when_enforced():
    from starlette.websockets import WebSocketDisconnect
    with TestClient(make(ws_auth=True)) as c:
        with pytest.raises(WebSocketDisconnect) as e:
            with c.websocket_connect("/ws/live?format=json") as ws:
                ws.receive_json()
        assert e.value.code == 1008
        tok = login(c, "viewer", "aegis-viewer").json()["access_token"]
        with c.websocket_connect(f"/ws/live?format=json&token={tok}") as ws:
            assert ws.receive_json()["type"] == "snapshot"

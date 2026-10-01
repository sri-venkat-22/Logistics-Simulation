# Security

**AEGIS Twin · Phase 8 (Level 3b)** · `services/api/app/security.py`, `auth.py`, `ingest.py`, `pipeline.py`, `infra/caddy/Caddyfile`, `.github/workflows/`

This is the security section of the submission. Every control below is enforced in code and covered by tests (`tests/test_security.py`, `test_api.py`, `test_trust.py`, `test_copilot.py`) or by a CI gate.

## Threat model (summary)

| Asset | Threat | Control |
|---|---|---|
| Twin state (stock, routes, plans) | An unauthorised plan apply or chaos injection | JWT + RBAC on every mutating route, audit log |
| Telemetry feed | Spoofed, replayed or forged device messages | Per-device HMAC-SHA256, nonce + timestamp window, 9-layer trust pipeline |
| User accounts | Credential stuffing, token theft, refresh-token replay | argon2id, login lockout, short-lived access JWT, rotating single-use refresh tokens, revocation |
| Secrets | Leaking into git or images, or running with defaults | Env / GitHub secrets only, `AEGIS_ENV=prod` refuses default secrets, `.env.prod` git-ignored |
| AI Copilot | Prompt injection makes it act | Tool outputs are data, there is no SQL or shell tool, `propose_apply` only proposes, and a human applies with their own token (ADR-0005) |
| Supply chain | Vulnerable dependencies or base images | pip-audit, npm audit gate, Trivy image scan in CI |

## 1. Authentication and RBAC

- **OAuth2 password flow.** `POST /api/v1/auth/token` takes a form (`username`, `password`) and returns a JWT access token plus a refresh token.
  - HS256, with `iss=aegis-twin`, `aud=aegis-api`, `sub`, `role`, `typ`, `jti` and `exp` all checked on every request.
  - The access token lives 15 min (`AEGIS_ACCESS_TTL_S`), the refresh token 7 days (`AEGIS_REFRESH_TTL_S`).
- **Passwords** are argon2id hashes (`argon2-cffi`) in the `users` table. Initial users come from `AEGIS_USERS` and are hashed on first start. Admins manage users through `GET/POST /api/v1/auth/users`.
- **Refresh rotation.** Each refresh token's `jti` sits on a Redis allow-list. `POST /auth/refresh` consumes it and issues a new pair. Presenting a consumed token is rejected and audited as `token.refresh_reuse`.
- **Logout** (`POST /auth/logout`) revokes the refresh token and puts the access token's `jti` on a deny-list until it expires.
- **Lockout.** After 5 failed logins for a user within 15 min, the endpoint returns 429 and audits `login.locked`.
- **RBAC.** The ladder is `viewer < planner < security < admin`, enforced by `require(role)` in `auth.py`.

  | Role | Can |
  |---|---|
  | viewer | Read: network, live state, KPIs, trust read models, Copilot questions |
  | planner | + create / run scenarios, optimise, apply plans |
  | security | + chaos console, quarantine, device-key rotation, audit log |
  | admin | Everything, incl. user management |

- **WebSocket auth.** In prod, `/ws/live` requires `?token=<access JWT>`. Publisher sockets need an HMAC-signed `ts / nonce / sig` handshake.
- **Frontend.** Tokens live in `sessionStorage`, so they are cleared when the tab closes. The access token is refreshed shortly before it expires and once on a 401. The CSP (below) blocks third-party scripts, which limits XSS exposure.

## 2. Device security

- **Per-device keys.** Every telemetry message carries `HMAC-SHA256(device_key, canonical(message))`. The default device key is `HMAC(AEGIS_MASTER_KEY, source_id)`. This is trust layer L2 (`HMAC_INVALID`).
- **Key rotation.** `POST /api/v1/devices/{source_id}/rotate` (security role) issues a new random 256-bit key, which is returned once.
  - The key is stored pgcrypto-encrypted (`pgp_sym_encrypt` with `AEGIS_KEY_SECRET`) in `device_keys` (migration `0002_security`).
  - The previous key stays valid for a grace period (`grace_s`, default 300 s), then fails L2.
  - Each rotation is audited and alerted. `GET /api/v1/devices` lists versions, never keys.
- **Replay protection.**
  - *Publisher batches* (`POST /api/v1/ingest/batch`) carry `X-AEGIS-Timestamp`, `X-AEGIS-Nonce` and `X-AEGIS-Signature = HMAC(publisher_key, ts \n nonce \n body)`. The timestamp must be within ±300 s, and the nonce is claimed with Redis `SET NX` and a TTL of twice the window. A reused nonce returns 401.
  - *Messages*: message-id dedupe (`SET NX`, 1 h TTL) plus source-clock checks give `REPLAY_NONCE`, `DUPLICATE_ID` and `STALE_TS` (trust layer L3).
- The full 9-layer trust pipeline is documented in [`docs/trust/TRUST.md`](../trust/TRUST.md). It detects 536 of 561 red-team attacks (95.5 %) with a 0.19 % false-positive rate.

## 3. Transport and application security

- **HTTPS.** Caddy (`infra/caddy/Caddyfile`) gets and renews Let's Encrypt certificates for `AEGIS_DOMAIN` automatically and serves HTTP/1.1, HTTP/2 and HTTP/3.
  - It is the only published container (ports 80 and 443). The API, TimescaleDB and Redis sit on an internal Docker network.
  - `/metrics` returns 404 from outside.
- **Security headers.**
  - On the SPA (Caddy): HSTS (1 year, subdomains) and a strict CSP (`script-src 'self'`, connect only to self, `wss://` self and the basemap hosts, `frame-ancestors 'none'`, `base-uri 'none'`, `form-action 'self'`). Also `X-Content-Type-Options`, `X-Frame-Options DENY`, `Referrer-Policy no-referrer`, a `Permissions-Policy`, and the `Server` header removed.
  - On the API (`SecurityMiddleware`): `default-src 'none'` (a relaxed CSP only for `/docs`), COOP, nosniff, and HSTS in prod.
- **CORS.** Prod allows exactly `AEGIS_CORS_ORIGINS` with methods `GET / POST / DELETE` and headers `Authorization, Content-Type`. Dev allows only `localhost` origins.
- **Rate limiting** uses [slowapi](https://github.com/laurentS/slowapi) on the shared Redis (fixed one-minute windows per client IP), so the limits hold across API replicas.
  - REST: 1200 requests/min (`AEGIS_RATE_LIMIT`). Auth: 20/min. Copilot: 30/min.
  - Over the limit returns 429 with `Retry-After`.
  - Ingest is excluded because it is authenticated per signed publisher batch.
  - The client IP comes from `X-Forwarded-For`, which Caddy overwrites for untrusted clients (`trusted_proxies private_ranges`).
- **Request size limits.** Request bodies are capped at 1 MB (8 MB for ingest batches); larger requests get a 413 before the body is read. Ingest batches are also capped at 5,000 messages.
- **Input validation.** Every request body is a strict Pydantic v2 model. Scenario specs and Copilot tool inputs validate against a strict JSON schema (`sim/scenarios/dsl.py`).
- **Parameterised SQL only.** All queries go through SQLAlchemy `text()` with bound parameters (`db/store.py: execute`). The only f-string SQL is TimescaleDB DDL in `db/timescale.py`, built from table names hard-coded in that module and never from input.
- **Secrets.**
  - Secrets come only from the environment: `.env.prod` on the VM (git-ignored; template in `.env.prod.example`) and GitHub Actions secrets for CD (`DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `VERCEL_TOKEN`).
  - With `AEGIS_ENV=prod` the API **refuses to start** (`Settings.check_production`) if the JWT secret is shorter than 32 characters or is the dev value, if the device or publisher keys are the public development key, or if dev bearer tokens or demo users are present.
- **PII.** The system holds no customer PII: users are operators, and nodes and zones are aggregates. The only secret data at rest, rotated device keys, is pgcrypto-encrypted.
- **Containers.** The API image runs as a non-root user. Both images have healthchecks.

## 4. Audit log

The `audit_log` table (append-only from the API) records the following events:

- `login.success`, `login.failure`, `login.locked`, `logout`, `token.refresh_reuse` and `token.refresh_unknown_user`
- `plan.apply` (with the plan, actions and result), `scenario.optimize`, `disruption.push` / `disruption.end` and `chaos.inject`
- `device.rotate`, `user.create`, `copilot.chat` and `copilot.propose`

`GET /api/v1/audit?action=` (security role) serves the log. The live **Trust Center** shows the latest entries in its *Audit log* panel.

## 5. CI security gates

`.github/workflows/ci.yml` runs on every push and PR:

| Gate | Tool | Blocks on |
|---|---|---|
| Lint | ruff, eslint | any error |
| Types | mypy, `tsc -b` | any error |
| Tests | pytest (coverage), vitest, Playwright demo smoke | any failure |
| Python dependencies | `pip-audit -r requirements.txt` | any known vulnerability |
| JS dependencies | `npm audit` via `scripts/audit.mjs` | high / critical, except reviewed entries in `audit-allowlist.json` |
| Images | Docker build of API + web, `caddy validate` | build failure |
| Image scan | Trivy | fixable CRITICAL (fixable HIGH is reported) |

**Accepted findings.** `maplibre-gl` GHSA-jrc7-96c5-q579 is accepted in `apps/web/audit-allowlist.json`. It needs attacker-controlled style or tile sources, but the app loads only its own fixed basemap styles, and the CSP restricts `connect-src` to those hosts. It is re-reviewed whenever the allow-list check runs, because a new advisory fails the gate.

## 6. Operating it

- **Rotate the JWT secret.** Change `AEGIS_JWT_SECRET` and restart. All sessions are invalidated.
- **Rotate a device key.** `POST /api/v1/devices/{id}/rotate?grace_s=600` (security role). Hand the returned key to the device within the grace period.
- **Revoke a user.** Remove them from the `users` table. Their next refresh is rejected (`token.refresh_unknown_user`), and their access token expires within 15 min.
- **Report a vulnerability.** Open a private GitHub security advisory on this repository.

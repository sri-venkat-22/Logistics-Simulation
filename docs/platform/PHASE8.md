# Phase 8 — LEVEL 3b: Security, CI/CD and cloud deployment

**AEGIS Twin · Days 14–18**

| Deliverable | Where |
|---|---|
| Auth: OAuth2 password flow, JWT access + rotating refresh, argon2id, RBAC (4 roles) | `services/api/app/security.py`, `auth.py`; UI `apps/web/src/lib/auth.ts`, `LoginDialog.tsx` |
| Device security: per-device HMAC-SHA256, nonce + timestamp replay protection (Redis `SET NX` + TTL), key rotation | `ingest.py`, `pipeline.py`, `security.DeviceKeys`, migration `0002_security` |
| Transport + app security: Caddy auto-HTTPS, HSTS, CSP, strict CORS, slowapi rate limits, size limits, parameterised SQL, env secrets, pgcrypto | `infra/caddy/Caddyfile`, `security.SecurityMiddleware`, `config.check_production` |
| Audit log in the Trust Center | `audit_log` table, `GET /api/v1/audit`, `TrustCenterLive.tsx` |
| CI | [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) |
| CD: GHCR images → SSH `docker compose pull && up -d`; optional Vercel | [`.github/workflows/cd.yml`](../../.github/workflows/cd.yml), [`docker-compose.prod.yml`](../../docker-compose.prod.yml) |
| Security section | [`docs/security/SECURITY.md`](../security/SECURITY.md) |

## Live URL

**https://thunderously-unwitty-chau.ngrok-free.dev** (HTTPS, sign-in required). It is also on the [title slide](../pitch/title-slide.png), the deck's first slide.

Until the cloud VM and the `.tech` domain exist, the public URL is an ngrok tunnel to the production configuration running on one machine:

```bash
./scripts/live_tunnel.sh
```

- **Path.** ngrok (TLS) → Caddy `:8090` with the same `infra/caddy/Caddyfile` (SPA, CSP, HSTS, `/metrics` hidden) → the API on `127.0.0.1:8100` with `AEGIS_ENV=prod`. The Reality Emulator, including SUMO, signs its telemetry with the production keys.
- **Secrets and accounts.** The script generates them once into `.env.tunnel` (git-ignored, mode 600). The dev users, dev bearer tokens and public dev keys are refused at startup.
- **Isolation.** It uses its own database (`aegis_live`) and Redis DB 5.
- **Checked over the public URL:** HSTS + CSP present, `/readyz` ready, the dev password `aegis-planner` → 401, a generated account → a 15-min JWT, plan apply without a token → 401, `/metrics` → 404. The WebSocket is token-gated; the UI opens the sign-in dialog and goes live after signing in.
- **Limits.** The tunnel is up only while this machine runs it, and ngrok's free plan shows a one-time interstitial. The CD path below replaces it.

## CI

Every push and PR runs four jobs:

1. **python**: ruff → mypy → `alembic upgrade head` on a PostGIS service → pytest with coverage (Redis service) → pip-audit.
2. **web**: eslint → `tsc -b` → vitest → `vite build` → npm audit gate.
3. **e2e**: Playwright drives the demo flow against a real API, with the Vite dev server running. It covers live data, sign-in, a Copilot tool call, the 9-layer Trust Center, a cascade and the ML panel.
4. **docker**: builds the API and web (Caddy) images, runs `caddy validate`, then runs Trivy (fixable CRITICAL blocks the build).

## Production topology (one VM)

```
Internet ──443/80──▶ web (Caddy: TLS, SPA, headers) ──▶ api (uvicorn, non-root) ──▶ db (TimescaleDB) · redis
                                    └──── internal Docker network; only Caddy is published ────┘
```

The browser sees one origin: `/` is the SPA, while `/api`, `/ws`, `/docs` and the probes go to the API, so CORS never needs to be relaxed. The Copilot uses Claude when `ANTHROPIC_API_KEY` is set and the offline planner otherwise.

## Deploying (runbook)

**1. Domain.** Register a free `.tech` domain (for example through the GitHub Student Developer Pack → get.tech). Add an **A record** for `aegis.<your-domain>.tech` → the VM's public IPv4.

**2. VM.** Create an Ubuntu 24.04 VM with at least 2 vCPU and 4 GB RAM (for example a DigitalOcean droplet or an Azure B2s). Open ports 22, 80 and 443 in the firewall, then install Docker:

```bash
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER
```

```bash
sudo mkdir -p /opt/aegis && sudo chown $USER /opt/aegis
```

**3. Secrets on the VM.** Copy `.env.prod.example` to `/opt/aegis/.env.prod` and replace every value. It is git-ignored and stays on the VM. Generate each secret with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

**4. GitHub.**
- Add the repository secrets `DEPLOY_HOST` (VM IP), `DEPLOY_USER` and `DEPLOY_SSH_KEY` (a private key whose public half is in the VM's `~/.ssh/authorized_keys`). Optionally add `DEPLOY_PATH`.
- Create an environment named `production`. Its protection rules can require an approval before deploying.
- GHCR packages are private by default. Either make `aegis-api` / `aegis-web` public, or run `docker login ghcr.io` on the VM with a read-only PAT.

**5. Ship.** Merge to `main`. CI runs; on success, CD builds and pushes `ghcr.io/sri-venkat-22/aegis-{api,web}:sha-<commit>` and `:latest`, copies `docker-compose.prod.yml` to the VM, runs `pull && up -d`, and waits for `https://<domain>/readyz`. You can also start it manually with **Actions → CD → Run workflow**.

**6. Verify.**

```bash
curl -sI https://aegis.example.tech | grep -iE "strict-transport|content-security"
```

```bash
curl -s https://aegis.example.tech/readyz
```

Then open the site, choose **Sign in**, and use an `AEGIS_USERS` account.

**Rollback.** Set `AEGIS_API_IMAGE` / `AEGIS_WEB_IMAGE` to an earlier `sha-…` tag and run `docker compose -f docker-compose.prod.yml --env-file .env.prod up -d`.

### Frontend on Vercel (optional)

The same SPA also deploys to Vercel. Set `VERCEL_TOKEN`, `VERCEL_ORG_ID` and `VERCEL_PROJECT_ID` as secrets and the repository variable `PUBLIC_API_URL=https://aegis.<domain>.tech`, then add the Vercel origin to `AEGIS_CORS_ORIGINS` on the VM. The Caddy-served SPA on the same origin is the default because it needs no CORS and the CSP stays `connect-src 'self'`.

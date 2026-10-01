# AEGIS web edge: the Vite build served by Caddy, which also terminates TLS and proxies /api, /ws and /docs to the API.
#   docker build -f infra/docker/web.Dockerfile -t aegis-web .
FROM node:22-alpine AS build
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/web ./
# same origin as the API behind Caddy: relative requests (VITE_API_URL="")
ENV VITE_API_URL=""
RUN npm run build

FROM caddy:2-alpine
COPY infra/caddy/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /web/dist /srv/web
EXPOSE 80 443
# probe Caddy itself (its admin API on loopback): with a real domain, plain-HTTP requests are redirected to HTTPS
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1:2019/config/ >/dev/null || exit 1

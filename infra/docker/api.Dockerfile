# AEGIS API + simulation engines + intelligence (Phases 4-8). Build from the repository root:
#   docker build -f infra/docker/api.Dockerfile -t aegis-api .
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 libxml2 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sim ./sim
COPY services ./services
COPY data ./data
COPY ml ./ml
COPY docs/sim ./docs/sim
COPY docs/ml ./docs/ml
COPY docs/trust ./docs/trust
RUN useradd --create-home aegis && chown -R aegis /app
USER aegis
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"
# behind Caddy: trust X-Forwarded-* from the proxy network only
CMD ["uvicorn", "services.api.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

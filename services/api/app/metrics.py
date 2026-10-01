"""Prometheus metrics (GET /metrics)."""
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()

INGEST_MSGS = Counter("aegis_ingest_messages_total", "Messages received at the ingest gateway", ["channel", "result"], registry=REGISTRY)
INGEST_BATCH = Histogram("aegis_ingest_batch_seconds", "Ingest request handling time", ["channel"], registry=REGISTRY,
                         buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1))
INGEST_AUTH_FAIL = Counter("aegis_ingest_auth_failures_total", "Rejected batches (publisher signature / replay)", ["reason"], registry=REGISTRY)
REJECTS = Counter("aegis_rejects_total", "Messages rejected or quarantined", ["layer", "reason"], registry=REGISTRY)
CLEAN_MSGS = Counter("aegis_clean_messages_total", "Messages that passed the trust stage", ["kind"], registry=REGISTRY)
TWIN_APPLIED = Counter("aegis_twin_state_updates_total", "Clean messages applied to the live twin state", ["kind"], registry=REGISTRY)
STREAM_LAG = Gauge("aegis_stream_pending", "Pending entries per consumer group", ["stream", "group"], registry=REGISTRY)
WS_CLIENTS = Gauge("aegis_ws_clients", "Connected WebSocket clients", ["channel"], registry=REGISTRY)
WS_FRAMES = Counter("aegis_ws_frames_total", "WebSocket frames sent", ["channel"], registry=REGISTRY)
WS_DROPPED = Counter("aegis_ws_frames_dropped_total", "Frames dropped for slow clients (backpressure)", registry=REGISTRY)
VEHICLES = Gauge("aegis_live_vehicles", "Vehicles in the live state", ["status"], registry=REGISTRY)
SCENARIO_JOBS = Counter("aegis_scenario_jobs_total", "Scenario jobs", ["result"], registry=REGISTRY)
SCENARIO_SECONDS = Histogram("aegis_scenario_job_seconds", "Monte Carlo job duration", registry=REGISTRY,
                             buckets=(0.5, 1, 2, 5, 10, 20, 40, 80))
DB_ROWS = Gauge("aegis_db_rows_written", "Rows written by the batch writer", ["table"], registry=REGISTRY)

"""The live macro twin: sim.macro.engine.Twin in its own thread on simpy.rt.RealtimeEnvironment.

The thread advances the twin in 1-sim-minute steps (paced by the real-time environment), refreshes a
cached snapshot after each step and executes queued commands (apply events) between steps, so API
handlers never touch the SimPy environment directly. factor = wall-seconds per simulated hour
(60 = 1 sim-minute per wall-second); factor 0 builds the twin but leaves it paused (tests).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from concurrent.futures import Future
from datetime import datetime

from sim.macro.demand import DemandModel
from sim.macro.engine import Twin
from sim.macro.network import Network

log = logging.getLogger("aegis.twin")
STEP_H = 1 / 60


class LiveTwin:
    def __init__(self, net: Network, start: datetime, factor: float, seed: int = 42, warmup_h: float = 0.0):
        self.net = net
        self.factor = factor
        self.twin = Twin(net, DemandModel(net), start, seed=seed, realtime_factor=factor or None, log_events=False)
        self.events: list[dict] = []
        self.flows: deque[tuple[float, str, str]] = deque(maxlen=50_000)  # (t_h, node, "depart" | "asn") for L7
        self.twin.listeners.append(self._on_event)
        if warmup_h > 0:
            self.twin.fast_forward(warmup_h)
        self._cmds: queue.Queue[tuple[dict, Future]] = queue.Queue()
        self._lock = threading.Lock()
        self._snap: dict = self.twin.snapshot()
        self._levels: dict[tuple[str, str], tuple[float, float]] = self._compute_levels()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.steps = 0

    def _on_event(self, e: dict) -> None:
        if e["event"] == "depart" and e.get("kind") == "delivery":
            self.flows.append((e["t_h"], self.net.lanes[e["lane"]].from_id, "depart"))
        elif e["event"] == "asn":
            self.flows.append((e["t_h"], e["source"], "asn"))
        if e["event"] in ("disruption_start", "disruption_end", "stockout_start", "stockout_end", "set_path", "apply",
                          "lane_multiplier"):
            self.events.append(e)
            del self.events[:-500]

    # ------------------------------------------------------------------ thread
    def start(self) -> None:
        if self.factor <= 0 or self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="live-twin", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        self.twin.sync_clock()
        while not self._stop.is_set():
            self._drain()
            try:
                self.twin.run_until(self.twin.env.now + STEP_H)
            except Exception as e:  # keep serving the last snapshot
                log.warning("live twin step failed: %s", e)
                time.sleep(1)
            self.steps += 1
            snap = self.twin.snapshot()
            with self._lock:
                self._snap = snap
                if self.steps % 60 == 0:
                    self._levels = self._compute_levels()

    def _drain(self) -> None:
        while True:
            try:
                ev, fut = self._cmds.get_nowait()
            except queue.Empty:
                return
            try:
                fut.set_result(self.twin.apply(ev))
            except Exception as e:
                fut.set_exception(e)

    # ------------------------------------------------------------------ API
    def apply(self, event: dict, timeout: float = 10.0) -> dict:
        """Run an apply() event on the twin thread (or inline when paused) and return its ack."""
        if self._thread is None:
            res = self.twin.apply(event)
            self._snap = self.twin.snapshot()
            return res
        fut: Future = Future()
        self._cmds.put((event, fut))
        return fut.result(timeout=timeout)

    def advance(self, hours: float) -> None:
        """Paused mode only (tests / scripted demos): run the twin forward synchronously."""
        if self._thread is not None:
            raise RuntimeError("the live twin runs on its own clock")
        self.twin.run_until(self.twin.env.now + hours)
        self._snap = self.twin.snapshot()
        self._levels = self._compute_levels()

    def snapshot(self) -> dict:
        with self._lock:
            return self._snap

    def _compute_levels(self) -> dict[tuple[str, str], tuple[float, float]]:
        out = {}
        for (dc, sku) in self.twin.pairs:
            out[(dc, sku)] = self.twin.warehouses[dc].policy[sku].levels(self.twin, dc, sku, self.twin.env.now)
        return out

    def levels(self, node: str, sku: str) -> tuple[float, float] | None:
        """(reorder point, order-up-to level) of a DC x SKU, refreshed every simulated hour."""
        return self._levels.get((node, sku))

    def reorder_point(self, node: str, sku: str) -> float | None:
        lv = self._levels.get((node, sku))
        return lv[0] if lv else None

    def flow_count(self, node: str, kind: str, hours: float) -> int:
        """Twin-predicted flow events at a node in the last `hours` of twin time (L7 flow divergence)."""
        t0 = self.twin.env.now - hours
        return sum(1 for t, n, k in list(self.flows) if t >= t0 and n == node and k == kind)

    def kpis(self) -> dict:
        s = self.snapshot()
        return {"t_h": s["t_h"], "ts": s["ts"], **s["kpis_to_date"], "effects": len(s["effects"])}

"""SUMO micro-twin in its own OS process, driven through a command queue.

libsumo holds one simulation per process, so the live twin and the Reality Emulator each get their own
worker. Two ways to drive it:

  lock-step  (the coupling orchestrator owns the clock)
      mp = MicroProcess(seed=1).start()
      mp.call("spawn_truck", vid="T1", from_hub="Patancheru Plant", to_hub="Shamshabad DC", depart=120.0)
      out = mp.step_to(600)          # {"t", "arrivals": [...], "fcd": [...]}

  free-running  (stand-alone demo: SUMO paced against the wall clock)
      mp.run(factor=60)              # 60 sim-seconds per wall-second (1 sim-minute per second)
      for msg in mp.poll(): ...      # {"type": "fcd" | "arrival" | "reply", ...}

Commands (queue): spawn_truck, close_road, reopen_road, reroute_all, step_to, corridor_times, corridor_route, positions,
edge_stats, run, pause, set_factor, stop. Outputs: FCD rows every `fcd_every` sim-seconds (id, lon/lat
via simulation.convertGeo, speed, angle, edge + shipment metadata), arrival events, edge travel-time
stats. Every command carries an id; `call` waits for the matching reply, other outputs are buffered.
"""
from __future__ import annotations

import itertools
import multiprocessing as mp
import queue
import time
import traceback
from typing import Any

FCD_EVERY_S = 10


def _worker(cmd_q, out_q, opts: dict) -> None:  # runs in the child process
    from sim.micro.runner import CFG, MicroTwin
    try:
        mt = MicroTwin(cfg=CFG if opts.get("background", True) else None, seed=opts.get("seed", 42),
                       track_edges=opts.get("track_edges", True))
    except Exception as e:  # pragma: no cover - surfaced to the parent
        out_q.put({"type": "error", "error": repr(e), "trace": traceback.format_exc()})
        return
    fcd_every = max(1, int(opts.get("fcd_every", FCD_EVERY_S)))
    fcd_cars = int(opts.get("fcd_cars", 0))
    out_q.put({"type": "ready", "t": mt.now(), "hubs": mt.hubs})
    running, factor = False, float(opts.get("factor", 60.0))
    wall0 = sim0 = 0.0

    def fcd_rows() -> list[dict]:
        rows = mt.truck_positions()
        if fcd_cars:
            cars = [v for v in mt.t.vehicle.getIDList() if v not in mt.trucks][:fcd_cars]
            rows += [{**r, "kind": "car"} for r in mt.positions(cars)]
        return rows

    def advance(target: float, collect: dict | None) -> None:
        while mt.now() < target - 1e-9:
            arrs = mt.step(1)
            now = mt.now()
            fcd = int(now) % fcd_every == 0
            if collect is not None:
                collect["arrivals"] += [a.to_dict() for a in arrs]
                if fcd:
                    collect["fcd"].append({"t": now, "vehicles": fcd_rows()})
            else:
                for a in arrs:
                    out_q.put({"type": "arrival", **a.to_dict()})
                if fcd:
                    out_q.put({"type": "fcd", "t": now, "vehicles": fcd_rows()})

    def handle(c: dict) -> Any:
        nonlocal running, factor, wall0, sim0
        k = c["cmd"]
        if k == "spawn_truck":
            return mt.spawn_truck(c["vid"], c["from_hub"], c["to_hub"], park_s=c.get("park_s", 600),
                                  depart=c.get("depart"), meta=c.get("meta"))
        if k == "close_road":
            return {"rerouted": mt.close_road(c["edges"], c.get("reroute", True)), "closed": sorted(mt.closed)}
        if k == "reopen_road":
            return {"rerouted": mt.reopen_road(c["edges"]), "closed": sorted(mt.closed)}
        if k == "reroute_all":
            return mt.reroute_all()
        if k == "step_to":
            collect = {"arrivals": [], "fcd": []}
            advance(float(c["t"]), collect)
            if c.get("edge_stats"):
                collect["edge_stats"] = mt.edge_stats(top=c.get("top", 50))
            return {"t": mt.now(), **collect}
        if k == "corridor_route":
            return mt.corridor_route(c["from_hub"], c["to_hub"])
        if k == "corridor_times":
            return {f"{a}|{b}": mt.corridor_time(a, b) for a, b in c["pairs"]}
        if k == "positions":
            return mt.truck_positions() if c.get("ids") is None else mt.positions(c["ids"])
        if k == "edge_stats":
            return mt.edge_stats(top=c.get("top", 50), edges=c.get("edges"))
        if k == "status":
            return {"t": mt.now(), "vehicles": mt.t.vehicle.getIDCount(), "trucks_driving": len(mt.trucks),
                    "arrived": len(mt.arrivals), "closed": sorted(mt.closed), "running": running, "factor": factor}
        if k == "run":
            factor = float(c.get("factor", factor))
            running, wall0, sim0 = True, time.perf_counter(), mt.now()
            return True
        if k == "set_factor":
            factor, wall0, sim0 = float(c["factor"]), time.perf_counter(), mt.now()
            return True
        if k == "pause":
            running = False
            return True
        raise ValueError(f"unknown command {k!r}")

    while True:
        try:
            c = cmd_q.get(timeout=0.005) if running else cmd_q.get()
        except queue.Empty:
            c = None
        if c is not None:
            if c["cmd"] == "stop":
                break
            try:
                out_q.put({"type": "reply", "id": c.get("id"), "ok": True, "result": handle(c)})
            except Exception as e:
                out_q.put({"type": "reply", "id": c.get("id"), "ok": False, "error": repr(e)})
        if running:  # free-running: keep SUMO time = sim0 + wall elapsed x factor
            advance(sim0 + (time.perf_counter() - wall0) * factor, None)
    mt.close()
    out_q.put({"type": "stopped"})


class MicroProcess:
    def __init__(self, seed: int = 42, background: bool = True, fcd_every: int = FCD_EVERY_S, factor: float = 60.0,
                 fcd_cars: int = 0, track_edges: bool = True, timeout_s: float = 120.0):
        self.opts = {"seed": seed, "background": background, "fcd_every": fcd_every, "factor": factor,
                     "fcd_cars": fcd_cars, "track_edges": track_edges}
        self.timeout_s = timeout_s
        self._ids = itertools.count(1)
        self.inbox: list[dict] = []
        self.hubs: dict[str, dict] = {}
        self.proc = None

    def start(self) -> "MicroProcess":
        ctx = mp.get_context("spawn")
        self.cmd_q, self.out_q = ctx.Queue(), ctx.Queue()
        self.proc = ctx.Process(target=_worker, args=(self.cmd_q, self.out_q, self.opts), daemon=True, name="sumo-micro")
        self.proc.start()
        msg = self.out_q.get(timeout=self.timeout_s)
        if msg["type"] != "ready":
            raise RuntimeError(f"micro-twin failed to start: {msg.get('error')}\n{msg.get('trace', '')}")
        self.hubs = msg["hubs"]
        return self

    def __enter__(self) -> "MicroProcess":
        return self.start() if self.proc is None else self

    def __exit__(self, *exc) -> None:
        self.close()

    def send(self, cmd: str, **kw) -> int:
        cid = next(self._ids)
        self.cmd_q.put({"cmd": cmd, "id": cid, **kw})
        return cid

    def call(self, cmd: str, **kw) -> Any:
        cid = self.send(cmd, **kw)
        deadline = time.monotonic() + self.timeout_s
        while True:
            msg = self.out_q.get(timeout=max(0.1, deadline - time.monotonic()))
            if msg.get("type") == "reply" and msg.get("id") == cid:
                if not msg["ok"]:
                    raise RuntimeError(f"micro command {cmd} failed: {msg['error']}")
                return msg["result"]
            self.inbox.append(msg)

    def step_to(self, t_s: float, edge_stats: bool = False) -> dict:
        return self.call("step_to", t=t_s, edge_stats=edge_stats)

    def run(self, factor: float | None = None) -> None:
        self.call("run", **({"factor": factor} if factor else {}))

    def pause(self) -> None:
        self.call("pause")

    def poll(self) -> list[dict]:
        out, self.inbox = self.inbox, []
        while True:
            try:
                out.append(self.out_q.get_nowait())
            except queue.Empty:
                return out

    def close(self) -> None:
        if self.proc is None:
            return
        try:
            self.cmd_q.put({"cmd": "stop"})
            self.proc.join(timeout=10)
        finally:
            if self.proc.is_alive():
                self.proc.terminate()
            self.proc = None

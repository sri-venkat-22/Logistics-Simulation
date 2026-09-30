"""Phase 3 exit demo: the coupled twin and the Reality Emulator running side by side.

    python -m sim.live --hours 6                    # as fast as possible
    python -m sim.live --hours 1 --realtime 60      # demo pace (1 sim-minute per wall-second)
    python -m sim.live --hours 6 --attacks 20 --out /tmp/telemetry.jsonl.gz

Both worlds start from the same network and date. The twin (seed 42) is what AEGIS believes; reality
(seed 1042 + hidden perturbations) is what "happens" and streams telemetry. Nothing feeds reality back
into the twin yet (that is the Phase 4 ingest + Phase 7 trust layer), so the divergence printed here is
the open-loop error the twin must close: per-DC stock gap, fill rates, trucks in Hyderabad, messages.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sim.coupling.orchestrator import CoupledTwin
from sim.macro.demand import DemandModel
from sim.macro.engine import Twin
from sim.macro.network import Network
from sim.micro.process import MicroProcess
from sim.micro.runner import CFG
from sim.reality.attacks import AttackInjector
from sim.reality.emulator import RealityEmulator
from sim.reality.telemetry import JsonlSink, MemorySink

IST = ZoneInfo("Asia/Kolkata")


def stock_gap(twin: Twin, real: Twin) -> float:
    """Mean absolute on-hand difference across DC x SKU, as % of reality's on-hand."""
    num = den = 0.0
    for dc, sku in twin.pairs:
        a, b = twin.warehouses[dc].level(sku), real.warehouses[dc].level(sku)
        num += abs(a - b)
        den += max(b, 1.0)
    return 100 * num / den


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.live", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--start", default="2026-10-15T00:00")
    ap.add_argument("--realtime", type=float, help="wall-seconds per simulated hour (60 = 1 sim-minute per second)")
    ap.add_argument("--attacks", type=int, default=0)
    ap.add_argument("--every", type=float, default=1.0, help="report every N sim-hours")
    ap.add_argument("--out", type=Path, help="write reality's telemetry here (JSON lines, .gz ok)")
    a = ap.parse_args(argv)
    start = datetime.fromisoformat(a.start).replace(tzinfo=IST)
    use_micro = CFG.exists()
    net = Network()
    twin = Twin(net, DemandModel(net), start, seed=42, realtime_factor=a.realtime)
    sink = JsonlSink(a.out) if a.out else MemorySink()
    inj = AttackInjector.random_campaign(a.attacks, a.hours) if a.attacks else AttackInjector([])
    mt = MicroProcess(seed=42, background=False).start() if use_micro else None
    mr = MicroProcess(seed=1042, background=False, fcd_every=1).start() if use_micro else None
    try:
        ct = CoupledTwin(twin, mt) if mt else None
        em = RealityEmulator(start, seed=1042, micro=mr, sink=sink, attacks=inj)
        print(f"AEGIS live · twin (seed 42) vs reality (seed 1042 + hidden perturbations) · {a.hours:g} sim-h · "
              f"{'SUMO micro-twins x2' if use_micro else 'macro only'} · "
              f"{'real time × %g' % (3600 / a.realtime) if a.realtime else 'as fast as possible'}")
        print(f"  {'time':<12}{'fill twin':>10}{'fill real':>10}{'stock gap':>11}{'city trucks':>13}{'handshakes':>12}"
              f"{'msgs':>10}{'attacked':>10}")
        wall0 = time.perf_counter()
        t = 0.0
        while t < a.hours - 1e-9:
            t = min(a.hours, t + a.every)
            twin.run_until(t)   # the twin's clock paces the wall clock in real-time mode
            em.run_until(t)     # reality catches up to the same simulated instant
            ks, kr = twin.snapshot()["kpis_to_date"], em.twin.snapshot()["kpis_to_date"]
            city = f"{len(ct.fcd)}/{len(em.coupled.fcd)}" if ct else "-"
            hs = f"{len(ct.handshakes)}/{len(em.coupled.handshakes)}" if ct else "-"
            msgs = sum(em.counts["by_kind"].values())
            print(f"  {twin.now_dt().strftime('%d %b %H:%M'):<12}{ks['fill_rate'] * 100:>9.1f}%{kr['fill_rate'] * 100:>9.1f}%"
                  f"{stock_gap(twin, em.twin):>10.1f}%{city:>13}{hs:>12}{msgs:>10,}{em.counts['attacked']:>10,}")
        em.close()
        wall = time.perf_counter() - wall0
        hidden = [p for p in em.truth_log if p["kind"] == "incident"]
        print(f"\n  {a.hours:g} sim-h in {wall:.1f} s wall · reality streamed {sum(em.counts['by_kind'].values()):,} messages "
              f"{em.counts['by_kind']} · hidden incidents: {len(hidden)} · lane bias on "
              f"{sum(1 for p in em.truth_log if p['kind'] == 'lane_bias')} road lanes")
    finally:
        for m in (mt, mr):
            if m:
                m.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

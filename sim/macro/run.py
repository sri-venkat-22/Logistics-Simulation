"""Run the macro twin and print KPIs.

    python -m sim.macro.run --days 30
    python -m sim.macro.run --days 30 --scenario cyclone --compare
    python -m sim.macro.run --days 30 --scenario sim/scenarios/templates/strike.json --json out.json --events events.csv

--scenario accepts a template name (see sim/scenarios/templates/) or a path, and may repeat.
--compare runs the baseline with the same seed (common random numbers) and prints deltas.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sim.macro.demand import DemandModel
from sim.macro.engine import Twin
from sim.macro.network import Network
from sim.paths import SCENARIO_TEMPLATES
from sim.scenarios.dsl import Scenario

IST = ZoneInfo("Asia/Kolkata")


def load_scenario(ref: str) -> Scenario:
    p = Path(ref)
    if not p.exists():
        p = SCENARIO_TEMPLATES / f"{ref.removesuffix('.json')}.json"
    if not p.exists():
        names = ", ".join(sorted(x.stem for x in SCENARIO_TEMPLATES.glob("*.json")))
        raise SystemExit(f"unknown scenario {ref!r}; templates: {names}")
    return Scenario.load(p)


def simulate(days: float, start: datetime, seed: int, scenarios: list[Scenario], failures: bool = True) -> tuple[dict, Twin]:
    net = Network()
    twin = Twin(net, DemandModel(net), start, seed=seed, scenarios=scenarios, random_failures=failures)
    return twin.run(days), twin


def fmt_inr(x: float) -> str:
    return f"₹{x / 1e5:,.1f} L"


def print_report(k: dict, label: str, base: dict | None = None, wall_s: float | None = None) -> None:
    def row(name: str, val: str, delta: str = "") -> None:
        print(f"  {name:<28}{val:>18}   {delta}")

    def d_pp(key: str) -> str:
        return "" if base is None else f"{(k[key] - base[key]) * 100:+.1f} pp vs baseline"

    def d_num(v: float, b: float, unit: str = "") -> str:
        return "" if base is None else f"{v - b:+,.1f}{unit} vs baseline"

    print()
    print(f"AEGIS macro twin · {k['days']:g} days from {k['start'][:16]} IST · seed {k['seed']} · {label}")
    print("  " + "─" * 70)
    row("Fill rate (units)", f"{k['fill_rate'] * 100:.1f} %", d_pp("fill_rate"))
    row("OTIF (orders due)", f"{k['otif'] * 100:.1f} %", d_pp("otif"))
    row("Orders placed / due", f"{k['orders']:,} / {k['orders_due']:,}")
    row("Units demanded", f"{k['units_demanded']:,}")
    row("Backorders at end (units)", f"{k['backorder_units_end']:,}",
        d_num(k["backorder_units_end"], base["backorder_units_end"]) if base else "")
    row("Inventory (days of cover)", f"{k['inventory_days']:.1f} d", d_num(k["inventory_days"], base["inventory_days"], " d") if base else "")
    c = k["cost_inr"]
    row("Cost: transport", fmt_inr(c["transport"]))
    row("Cost: holding", fmt_inr(c["holding"]))
    row("Cost: stock-out penalty", fmt_inr(c["penalty"]))
    if c.get("ordering"):
        row("Cost: ordering", fmt_inr(c["ordering"]))
    row("Cost: total", fmt_inr(c["total"]),
        "" if base is None else f"{(c['total'] - base['cost_inr']['total']) / 1e5:+,.1f} L vs baseline")
    row("CO₂", f"{k['co2_t']:,.1f} t", d_num(k["co2_t"], base["co2_t"], " t") if base else "")
    row("Replenishment orders", f"{k['replenishment_orders']:,}")
    print()
    print(f"  {'SKU':<10}{'demand':>10}{'fill':>9}{'OTIF':>9}{'backorder':>11}{'cover':>9}")
    for sku, v in k["per_sku"].items():
        print(f"  {sku:<10}{v['units_demanded']:>10,}{v['fill_rate'] * 100:>8.1f}%{v['otif'] * 100:>8.1f}%"
              f"{v['backorder_units_end']:>11,}{v['days_of_cover']:>8.1f}d")
    worst = sorted(k["per_dc_sku"].items(), key=lambda kv: -kv[1]["stockout_hours"])[:5]
    worst = [w for w in worst if w[1]["stockout_hours"] > 0]
    if worst:
        print("\n  Stock-out hours (DC / SKU): " + ", ".join(f"{name} {v['stockout_hours']:.0f} h" for name, v in worst))
    for e in k["disruptions"]:
        extra = f" · nodes {','.join(e['nodes'])}" if e.get("nodes") else ""
        extra += f" · {len(e['lanes'])} lanes slowed" if e.get("lanes") else ""
        ttr = f"TTR {e['ttr_h'] / 24:.1f} d" if e["ttr_h"] is not None else "TTR ongoing"
        tts = f"TTS {e['tts_h'] / 24:.1f} d" if e["tts_h"] is not None else f"TTS > {e['survived_h'] / 24:.1f} d (no stock-out)"
        flag = "  EXPOSED" if e["exposed"] else ""
        print(f"  ⚡ {e['ts']}  {e['kind'].replace('_', ' ')}: {e['type']} @ {e['target']}{extra} · {ttr} · {tts}{flag}")
    if wall_s is not None:
        print(f"\n  simulated {k['days']:g} days in {wall_s:.2f} s wall-clock")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim.macro.run", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--start", default="2026-10-15T00:00", help="simulation start (IST); default spans Diwali 2026")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--scenario", action="append", default=[], help="template name or JSON path (repeatable)")
    ap.add_argument("--compare", action="store_true", help="also run the baseline with the same seed and show deltas")
    ap.add_argument("--json", type=Path, help="write KPIs as JSON")
    ap.add_argument("--events", type=Path, help="write the event log as CSV")
    ap.add_argument("--no-failures", action="store_true", help="disable random supplier outages (MTBF / MTTR)")
    a = ap.parse_args(argv)

    start = datetime.fromisoformat(a.start).replace(tzinfo=IST)
    scenarios = [load_scenario(s) for s in a.scenario]
    label = " + ".join(s.name or s.type.value for s in scenarios) or "baseline"

    base = None
    if a.compare and scenarios:
        base, _ = simulate(a.days, start, a.seed, [], not a.no_failures)
    t0 = time.perf_counter()
    k, twin = simulate(a.days, start, a.seed, scenarios, not a.no_failures)
    wall = time.perf_counter() - t0
    if base is not None:
        print_report(base, "baseline")
    print_report(k, label, base, wall)

    if a.json:
        a.json.write_text(json.dumps({"scenario": label, "kpis": k, "baseline": base}, indent=2, default=str))
    if a.events:
        keys = ["t_h", "ts", "event", *sorted({key for e in twin.events for key in e} - {"t_h", "ts", "event"})]
        with a.events.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(twin.events)
    return 0


if __name__ == "__main__":
    sys.exit(main())

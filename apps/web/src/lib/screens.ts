import { Radar, FlaskConical, Building2, ShieldAlert, Gauge, Network, ServerCog, Globe2, Map as MapIcon, type LucideIcon } from "lucide-react";

export interface ScreenDef { key: string; path: string; label: string; sub: string; icon: LucideIcon; shortcut: string }

/** §6.5 screens. Shortcut keys 1–9 switch between them. */
export const SCREENS: ScreenDef[] = [
  { key: "control", path: "/", label: "Control Tower", sub: "End-to-end visibility · live network", icon: Radar, shortcut: "1" },
  { key: "scenario", path: "/scenario", label: "Scenario Lab", sub: "What-if · Monte Carlo · ranked plans", icon: FlaskConical, shortcut: "2" },
  { key: "city", path: "/city", label: "City Twin", sub: "Hyderabad micro-twin · SUMO", icon: Building2, shortcut: "3" },
  { key: "trust", path: "/trust", label: "Trust Center", sub: "Adversarial resilience · 9-layer pipeline", icon: ShieldAlert, shortcut: "4" },
  { key: "fidelity", path: "/fidelity", label: "Fidelity Lab", sub: "Predictions vs reality · decision value", icon: Gauge, shortcut: "5" },
  { key: "network", path: "/network", label: "Network Graph", sub: "Topology · criticality · cascades", icon: Network, shortcut: "6" },
  { key: "ops", path: "/ops", label: "Ops", sub: "Kubernetes · SLOs · load tests", icon: ServerCog, shortcut: "7" },
  { key: "intro", path: "/intro", label: "Intro", sub: "Globe → India → network", icon: Globe2, shortcut: "8" },
  { key: "routing", path: "/routing", label: "Live Routing", sub: "Interactive best-path simulation", icon: MapIcon, shortcut: "9" },
];

import { create } from "zustand";
import { trust, type QEntry } from "./data";

export interface ScenarioState {
  status: "idle" | "placed" | "running" | "done";
  type: string | null;
  target: string | null;
  progress: number;
}

interface AegisState {
  selectedNode: string | null;
  selectNode: (id: string | null) => void;

  /** Timeline scrubber offset in hours: <0 past, 0 now, >0 predicted future. */
  timeOffsetH: number;
  setTimeOffset: (h: number) => void;

  layers: Record<string, boolean>;
  toggleLayer: (k: string) => void;

  scenario: ScenarioState;
  setScenario: (s: Partial<ScenarioState>) => void;
  resetScenario: () => void;

  appliedPlan: string | null;
  applyPlan: (id: string | null) => void;

  roadClosed: boolean;
  setRoadClosed: (v: boolean) => void;

  copilotOpen: boolean;
  setCopilotOpen: (v: boolean) => void;
  paletteOpen: boolean;
  setPaletteOpen: (v: boolean) => void;

  director: boolean;
  setDirector: (v: boolean) => void;

  /** Trust Center live state */
  quarantine: QEntry[];
  pushQuarantine: (q: QEntry) => void;
  activeAttacks: string[];
  triggerAttack: (id: string) => void;
  clearAttack: (id: string) => void;
  detected: number;
}

export const useAegis = create<AegisState>((set) => ({
  selectedNode: null,
  selectNode: (id) => set({ selectedNode: id }),

  timeOffsetH: 0,
  setTimeOffset: (h) => set({ timeOffsetH: h }),

  layers: { nodes: true, lanes: true, trucks: true, ships: true, inventory: true, weather: true, risk: false },
  toggleLayer: (k) => set((s) => ({ layers: { ...s.layers, [k]: !s.layers[k] } })),

  scenario: { status: "idle", type: null, target: null, progress: 0 },
  setScenario: (p) => set((s) => ({ scenario: { ...s.scenario, ...p } })),
  resetScenario: () => set({ scenario: { status: "idle", type: null, target: null, progress: 0 } }),

  appliedPlan: null,
  applyPlan: (id) => set({ appliedPlan: id }),

  roadClosed: false,
  setRoadClosed: (v) => set({ roadClosed: v }),

  copilotOpen: false,
  setCopilotOpen: (v) => set({ copilotOpen: v }),
  paletteOpen: false,
  setPaletteOpen: (v) => set({ paletteOpen: v }),

  director: false,
  setDirector: (v) => set({ director: v }),

  quarantine: trust.quarantine.slice(0, 14),
  pushQuarantine: (q) => set((s) => ({ quarantine: [q, ...s.quarantine].slice(0, 60) })),
  activeAttacks: [],
  triggerAttack: (id) => set((s) => ({ activeAttacks: s.activeAttacks.includes(id) ? s.activeAttacks : [...s.activeAttacks, id], detected: s.detected + 1 })),
  clearAttack: (id) => set((s) => ({ activeAttacks: s.activeAttacks.filter((a) => a !== id) })),
  detected: trust.counters.attacks_detected,
}));

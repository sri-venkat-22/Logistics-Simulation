import { useEffect, useRef, useState, type ReactNode, type ButtonHTMLAttributes } from "react";
import clsx from "clsx";
import type { Sev } from "../lib/data";
import { sevColor } from "../lib/theme";

export function Glass({ className, children, ...rest }: { className?: string; children: ReactNode } & React.HTMLAttributes<HTMLDivElement>) {
  return <div className={clsx("glass", className)} {...rest}>{children}</div>;
}

export function Eyebrow({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={clsx("eyebrow", className)}>{children}</div>;
}

export function PanelHeader({ title, sub, right }: { title: string; sub?: string; right?: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 px-4 pt-3.5 pb-2">
      <div className="min-w-0">
        <div className="text-[13px] font-semibold text-ink">{title}</div>
        {sub && <div className="text-[11.5px] text-ink-3 mt-0.5">{sub}</div>}
      </div>
      {right}
    </div>
  );
}

export function Dot({ sev, pulse, size = 8 }: { sev: Sev; pulse?: boolean; size?: number }) {
  return (
    <span
      className={clsx("inline-block rounded-full shrink-0", pulse && "pulse-dot")}
      style={{ width: size, height: size, background: sevColor[sev], color: sevColor[sev] }}
    />
  );
}

export function Badge({ sev = "ok", children, className }: { sev?: Sev; children: ReactNode; className?: string }) {
  const c = sevColor[sev];
  return (
    <span
      className={clsx("inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10.5px] font-semibold tracking-wide uppercase", className)}
      style={{ color: c, background: `${c}1A`, boxShadow: `inset 0 0 0 1px ${c}33` }}
    >
      {children}
    </span>
  );
}

export function Button({
  variant = "ghost", className, children, ...rest
}: { variant?: "ghost" | "primary" | "ai" | "danger" | "solid" } & ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      className={clsx(
        "inline-flex items-center justify-center gap-1.5 rounded-lg px-3 h-8 text-[12.5px] font-medium transition-all duration-200 disabled:opacity-40 disabled:pointer-events-none cursor-pointer select-none",
        variant === "ghost" && "text-ink-2 hover:text-ink hover:bg-white/6 border border-line",
        variant === "solid" && "text-ink bg-white/8 hover:bg-white/12 border border-line-strong",
        variant === "primary" && "text-bg bg-ok hover:brightness-110 font-semibold",
        variant === "ai" && "text-bg bg-ai hover:brightness-110 font-semibold",
        variant === "danger" && "text-white bg-bad/90 hover:bg-bad font-semibold",
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="num inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded border border-line-strong bg-white/5 text-[10px] text-ink-2">
      {children}
    </kbd>
  );
}

/** Animated number ticker (count-up), tabular mono digits. */
export function Ticker({ value, decimals = 0, prefix = "", suffix = "", duration = 900, className }: {
  value: number; decimals?: number; prefix?: string; suffix?: string; duration?: number; className?: string;
}) {
  const [shown, setShown] = useState(value);
  const from = useRef(value);
  const first = useRef(true);
  useEffect(() => {
    const start = performance.now();
    const a = first.current ? value * 0.6 : from.current;
    first.current = false;
    let raf = 0;
    const tick = (t: number) => {
      const k = Math.min(1, (t - start) / duration);
      const e = 1 - Math.pow(1 - k, 3);
      setShown(a + (value - a) * e);
      if (k < 1) raf = requestAnimationFrame(tick);
      else from.current = value;
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, duration]);
  return (
    <span className={clsx("num", className)}>
      {prefix}
      {shown.toLocaleString("en-IN", { minimumFractionDigits: decimals, maximumFractionDigits: decimals })}
      {suffix}
    </span>
  );
}

export function Sparkline({ data, color, width = 72, height = 22 }: { data: number[]; color: string; width?: number; height?: number }) {
  const min = Math.min(...data), max = Math.max(...data);
  const span = max - min || 1;
  const pts = data.map((v, i) => `${(i / (data.length - 1)) * width},${height - 2 - ((v - min) / span) * (height - 4)}`).join(" ");
  return (
    <svg width={width} height={height} className="overflow-visible" aria-hidden>
      <polyline points={pts} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={width} cy={height - 2 - ((data[data.length - 1] - min) / span) * (height - 4)} r={2.2} fill={color} />
    </svg>
  );
}

export function KpiCard({ label, value, unit, delta, state, spark, decimals = 0, prefix = "" }: {
  label: string; value: number; unit?: string; delta?: number; state: Sev; spark?: number[]; decimals?: number; prefix?: string;
}) {
  const c = sevColor[state];
  const worse = state !== "ok" && state !== "good";
  return (
    <Glass className="px-4 py-3 min-w-0">
      <div className="flex items-center justify-between gap-2">
        <Eyebrow>{label}</Eyebrow>
        <Dot sev={state} pulse={state === "bad"} size={7} />
      </div>
      <div className="mt-1.5 flex items-end justify-between gap-3">
        <div className="text-[26px] leading-none font-semibold text-ink">
          <Ticker value={value} decimals={decimals} prefix={prefix} />
          {unit && <span className="text-[13px] text-ink-3 ml-1 font-medium">{unit}</span>}
        </div>
        {spark && <Sparkline data={spark} color={c} />}
      </div>
      {delta !== undefined && (
        <div className="mt-1.5 text-[11px] num" style={{ color: worse ? c : "var(--color-ink-3)" }}>
          {delta > 0 ? "▲" : "▼"} {Math.abs(delta)}{unit === "%" ? " pp" : ""} <span className="text-ink-3">vs 24 h</span>
        </div>
      )}
    </Glass>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={clsx("skeleton", className)} />;
}

export function MockChip() {
  return (
    <span
      title="Level-1 prototype: every number on screen is seeded mock data from scripts/gen_mock.py, not a measured result."
      className="num inline-flex items-center gap-1.5 rounded-md border border-warn/30 bg-warn/10 px-2 py-0.5 text-[10px] font-semibold tracking-wider text-warn"
    >
      PROTOTYPE · MOCK DATA
    </span>
  );
}

export function ProgressRing({ value, size = 88, stroke = 6, color = "var(--color-ok)", children }: {
  value: number; size?: number; stroke?: number; color?: string; children?: ReactNode;
}) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  return (
    <div className="relative" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} stroke="rgba(255,255,255,0.08)" strokeWidth={stroke} fill="none" />
        <circle cx={size / 2} cy={size / 2} r={r} stroke={color} strokeWidth={stroke} fill="none" strokeLinecap="round"
          strokeDasharray={c} strokeDashoffset={c * (1 - value)} style={{ transition: "stroke-dashoffset 120ms linear" }} />
      </svg>
      <div className="absolute inset-0 grid place-items-center">{children}</div>
    </div>
  );
}

export function Bar({ value, max = 1, color, marker, height = 6 }: { value: number; max?: number; color: string; marker?: number; height?: number }) {
  return (
    <div className="relative w-full rounded-full bg-white/6" style={{ height }}>
      <div className="absolute inset-y-0 left-0 rounded-full transition-all duration-500" style={{ width: `${Math.min(100, (value / max) * 100)}%`, background: color }} />
      {marker !== undefined && (
        <div className="absolute -top-1 -bottom-1 w-px bg-ink/70" style={{ left: `${Math.min(100, (marker / max) * 100)}%` }} />
      )}
    </div>
  );
}

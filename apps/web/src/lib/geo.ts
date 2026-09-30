/** Small geometry helpers for animating vehicles along lanes and roads. */

export type LonLat = [number, number];
export type LonLatAlt = [number, number, number];

const toRad = (d: number) => (d * Math.PI) / 180;
const toDeg = (r: number) => (r * 180) / Math.PI;

export function haversineKm(a: LonLat, b: LonLat): number {
  const [lon1, lat1, lon2, lat2] = [a[0], a[1], b[0], b[1]].map(toRad);
  const h = Math.sin((lat2 - lat1) / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin((lon2 - lon1) / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.sqrt(h));
}

/** Great-circle interpolation, lifted into an arc whose height scales with distance. */
export function arcPath(a: LonLat, b: LonLat, segments = 48, heightScale = 0.18): LonLatAlt[] {
  const [lon1, lat1, lon2, lat2] = [a[0], a[1], b[0], b[1]].map(toRad);
  const d = 2 * Math.asin(Math.sqrt(Math.sin((lat2 - lat1) / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin((lon2 - lon1) / 2) ** 2));
  const km = d * 6371;
  const peak = Math.min(900_000, km * 1000 * heightScale);
  const out: LonLatAlt[] = [];
  for (let i = 0; i <= segments; i++) {
    const f = i / segments;
    let lon: number, lat: number;
    if (d < 1e-9) { lon = a[0]; lat = a[1]; } else {
      const A = Math.sin((1 - f) * d) / Math.sin(d);
      const B = Math.sin(f * d) / Math.sin(d);
      const x = A * Math.cos(lat1) * Math.cos(lon1) + B * Math.cos(lat2) * Math.cos(lon2);
      const y = A * Math.cos(lat1) * Math.sin(lon1) + B * Math.cos(lat2) * Math.sin(lon2);
      const z = A * Math.sin(lat1) + B * Math.sin(lat2);
      lat = toDeg(Math.atan2(z, Math.sqrt(x * x + y * y)));
      lon = toDeg(Math.atan2(y, x));
    }
    out.push([lon, lat, Math.sin(Math.PI * f) * peak]);
  }
  return out;
}

/** Cumulative-distance timestamps in [0, 1] for a path, for TripsLayer. */
export function normalisedTimestamps(path: LonLat[] | LonLatAlt[]): number[] {
  const cum = [0];
  for (let i = 1; i < path.length; i++) cum.push(cum[i - 1] + haversineKm(path[i - 1] as LonLat, path[i] as LonLat));
  const total = cum[cum.length - 1] || 1;
  return cum.map((c) => c / total);
}

/** Position at fraction f along a path (linear between vertices, by cumulative distance). */
export function pointAt(path: LonLatAlt[] | LonLat[], ts: number[], f: number): number[] {
  const x = Math.min(1, Math.max(0, f));
  let lo = 0, hi = ts.length - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (ts[mid] <= x) lo = mid; else hi = mid; }
  const span = ts[hi] - ts[lo] || 1;
  const t = (x - ts[lo]) / span;
  const a = path[lo], b = path[hi];
  return a.map((v, i) => v + ((b[i] ?? 0) - v) * t);
}

/** Deterministic PRNG (mulberry32) so the prototype looks identical on every load. */
export function rng(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

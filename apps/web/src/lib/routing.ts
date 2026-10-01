import { network, nodeById, type NetNode } from "./data";

export function findBestRoute(startId: string, endId: string, disruptedNodes: string[] = []) {
  const adj = new Map<string, { to: string; laneId: string; dist: number }[]>();
  for (const l of network.lanes) {
    if (!adj.has(l.from_id)) adj.set(l.from_id, []);
    adj.get(l.from_id)!.push({ to: l.to_id, laneId: l.id, dist: l.distance_km });
    if (!adj.has(l.to_id)) adj.set(l.to_id, []);
    adj.get(l.to_id)!.push({ to: l.from_id, laneId: l.id, dist: l.distance_km });
  }

  const dist = new Map<string, number>();
  const prev = new Map<string, { node: string; laneId: string }>();
  const pq = new Set<string>();

  for (const n of network.nodes) {
    if (disruptedNodes.includes(n.id) && n.id !== startId && n.id !== endId) continue;
    dist.set(n.id, Infinity);
    pq.add(n.id);
  }
  dist.set(startId, 0);

  while (pq.size > 0) {
    let u = "";
    let minD = Infinity;
    for (const v of pq) {
      if (dist.get(v)! < minD) { minD = dist.get(v)!; u = v; }
    }
    if (!u || minD === Infinity) break;
    if (u === endId) break;
    pq.delete(u);

    for (const edge of (adj.get(u) || [])) {
      if (!pq.has(edge.to)) continue;
      const alt = dist.get(u)! + edge.dist;
      if (alt < dist.get(edge.to)!) {
        dist.set(edge.to, alt);
        prev.set(edge.to, { node: u, laneId: edge.laneId });
      }
    }
  }

  const path: string[] = [];
  const nodes: NetNode[] = [];
  let curr = endId;
  if (prev.has(curr) || curr === startId) {
    while (curr) {
      nodes.unshift(nodeById.get(curr)!);
      const p = prev.get(curr);
      if (p) path.unshift(p.laneId);
      curr = p?.node || "";
    }
  }
  return { laneIds: path, nodes };
}

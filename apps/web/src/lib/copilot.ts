/** Copilot chat over SSE (POST /api/v1/copilot/chat, text/event-stream). */
import { bearer } from "./auth";
import { API_URL } from "./config";

export type CopilotEvent =
  | { type: "text"; delta: string }
  | { type: "tool_call"; id: string; name: string; input: unknown }
  | { type: "tool_result"; id: string; name: string; ok: boolean; summary: string; data?: unknown }
  | { type: "proposal"; plan_id: string; name: string; kind: string; service: number; cost_lakh: number; co2_t: number;
      cvar95_lakh: number | null; explanation: string | null; n_actions: number; apply_url: string }
  | { type: "done"; mode: string; model?: string; stop_reason: string }
  | { type: "error"; message: string };

export interface ChatTurn { role: "user" | "assistant"; content: string }

export async function streamChat(messages: ChatTurn[], onEvent: (e: CopilotEvent) => void, signal?: AbortSignal): Promise<void> {
  const tok = await bearer();
  const r = await fetch(`${API_URL}/api/v1/copilot/chat`, {
    method: "POST", signal,
    headers: { "Content-Type": "application/json", Accept: "text/event-stream", ...(tok ? { Authorization: `Bearer ${tok}` } : {}) },
    body: JSON.stringify({ messages }),
  });
  if (!r.ok || !r.body) {
    const detail = await r.json().then((j) => j.detail as string).catch(() => r.statusText);
    throw Object.assign(new Error(detail || `HTTP ${r.status}`), { status: r.status });
  }
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, i);
      buf = buf.slice(i + 2);
      for (const line of frame.split("\n")) {
        if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)) as CopilotEvent);
      }
    }
  }
}

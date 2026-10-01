import { afterEach, describe, expect, it, vi } from "vitest";
import { streamChat, type CopilotEvent } from "./copilot";

function sseResponse(chunks: string[]) {
  const enc = new TextEncoder();
  const body = new ReadableStream({ start(c) { chunks.forEach((x) => c.enqueue(enc.encode(x))); c.close(); } });
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

describe("copilot SSE", () => {
  afterEach(() => vi.restoreAllMocks());

  it("parses events split across network chunks", async () => {
    const frames = [
      'data: {"type":"tool_call","id":"t1","name":"find_at_risk_nodes","input":{"limit":5}}\n\n',
      'data: {"type":"text","delta":"Structural ',
      'risk"}\n\ndata: {"type":"done","mode":"offline","stop_reason":"end_turn"}\n\n',
    ];
    vi.spyOn(globalThis, "fetch").mockResolvedValue(sseResponse(frames));
    const got: CopilotEvent[] = [];
    await streamChat([{ role: "user", content: "risk?" }], (e) => got.push(e));
    expect(got.map((e) => e.type)).toEqual(["tool_call", "text", "done"]);
    expect(got[1]).toEqual({ type: "text", delta: "Structural risk" });
  });

  it("surfaces HTTP errors with their status", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ detail: "missing token" }), { status: 401 }));
    await expect(streamChat([{ role: "user", content: "x" }], () => undefined)).rejects.toMatchObject({ status: 401, message: "missing token" });
  });
});

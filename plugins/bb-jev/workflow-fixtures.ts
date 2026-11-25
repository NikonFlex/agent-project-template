import assert from "node:assert/strict";
import type { TestContext } from "node:test";
import { zstdDecompressSync } from "node:zlib";
import { createFakePluginHost } from "@get-bb/plugin-sdk/testing";
import { createPlugin } from "./server.ts";

export const defaultThreadId = "arena-fixture";

export type Harness = ReturnType<typeof createFakePluginHost>["harness"];

export async function callTool(
  harness: Harness, name: string, input: unknown, caller = defaultThreadId,
) {
  const response = await harness.behavior.callAgentTool(name, input, { threadId: caller });
  assert.ok(typeof response === "object");
  const entry = response.content[0];
  assert.equal(entry.type, "text");
  return JSON.parse(entry.text) as unknown;
}

export function createJevHost(t: TestContext) {
  const host = createFakePluginHost({ pluginId: "bb-jev" });
  t.after(() => host.harness.lifecycle.dispose());
  return host;
}

export function usePlugin(t: TestContext, key?: string) {
  const { bb, harness } = createJevHost(t);
  createPlugin(undefined, () => key)(bb);
  return {
    harness,
    call: (name: string, input: unknown, caller = defaultThreadId) =>
      callTool(harness, name, input, caller),
  };
}

export function observed(outcome: "passed" | "failed" | "inconclusive") {
  return {
    type: "choice", choice: outcome, confidence: 1,
    probabilities: { passed: Number(outcome === "passed"), failed: Number(outcome === "failed"), inconclusive: Number(outcome === "inconclusive") },
  };
}

export function readRequest(request: RequestInit) {
  const compressed = new Headers(request.headers).get("content-encoding") === "zstd";
  const payload = compressed ? zstdDecompressSync(request.body as Uint8Array).toString() : String(request.body);
  return JSON.parse(payload) as { questions: Record<string, unknown>; state: string };
}

export function mockTransport(t: TestContext) {
  return t.mock.method(globalThis, "fetch", async (_url: string, request: RequestInit) => {
    const body = readRequest(request);
    const ids = Object.keys(body.questions);
    const outcomes = { R1: "passed", R2: "failed", R3: "inconclusive" } as const;
    const answers = Object.fromEntries(ids.map((id) => [id, observed(outcomes[id as keyof typeof outcomes] ?? "passed")]));
    return Response.json({ model: "jev-fixture", usage: { input_tokens: 150 }, answers });
  });
}

import assert from "node:assert/strict";
import test from "node:test";
import { createJevHost } from "./workflow-fixtures.ts";
import { registerJevsticePeer } from "./jevstice-peer.ts";
import { openStats } from "./stats.ts";

test("Jevstice can use the TypeSafe transport without returning the key to the caller", async t => {
  const { bb, harness } = createJevHost(t);
  const stats = openStats(bb.storage);
  let calls = 0;
  registerJevsticePeer(bb, { getKey: async () => "test-fixture", record: stats.recordJevstice }, async (key, input) => {
    assert.equal(key, "test-fixture");
    assert.equal(input.state, "synthetic");
    calls++;
    return { model: "test", answers: { coverage: { type: "noul", noul: 0.98 } } };
  });
  const input = { state: "synthetic", questions: { coverage: { type: "noul", instructions: "covered?" } } };
  await assert.rejects(harness.behavior.callRpc("jev.judge.system-one", input));
  await assert.rejects(harness.behavior.callRpc("jev.judge.system-one", input,
    { experimental_caller: { kind: "plugin", pluginId: "other" } }));
  assert.equal(calls, 0);
  assert.equal(stats.calls(), 0);
  const peer = { experimental_caller: { kind: "plugin", pluginId: "bb-jevstice" } } as const;
  assert.deepEqual(await harness.behavior.callRpc("jev.judge.available", {}, peer), { configured: true });
  const result = await harness.behavior.callRpc("jev.judge.system-one", input, peer);
  assert.equal(calls, 1);
  assert.equal(stats.calls(), 1);
  assert.deepEqual(result, { model: "test", answers: { coverage: { type: "noul", noul: 0.98 } } });
});

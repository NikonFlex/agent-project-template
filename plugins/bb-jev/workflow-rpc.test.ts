import assert from "node:assert/strict";
import test from "node:test";
import { receipt } from "./workflow-records.ts";
import { callTool, mockTransport, usePlugin, type Harness } from "./workflow-fixtures.ts";

const revision = "b".repeat(64);
const request = {
  request: "Показать срок поставки и количество, проверить после перезагрузки. Unicode: «ёлка».",
  requirements: [
    { id: "R1", text: "Срок поставки виден в карточке." },
    { id: "R2", text: "Количество изделий считается." },
  ],
};
const steps = [{
  text: "Показать срок и количество, затем повторить проверку после перезагрузки.",
  requirementIds: ["R1", "R2"],
}];
const big = "ё".repeat(11_990);

async function callRpc(harness: Harness, method: string, threadId: string, input: unknown) {
  return await harness.behavior.callRpc(method, { threadId, input }) as Record<string, unknown>;
}

test("RPC stages reuse agent handlers and receipt storage", async (t) => {
  mockTransport(t);
  const { harness } = usePlugin(t, "rpc-key");

  const viaTool = receipt.parse(await callTool(harness, "jev_review_requirements", request, "thread-tool"));
  const viaRpc = receipt.parse(await callRpc(harness, "jev.workflow.requirements", "thread-rpc", request));
  assert.equal(viaRpc.stage, viaTool.stage);
  assert.equal(viaRpc.status, viaTool.status);
  assert.equal(viaRpc.evidenceDigest, viaTool.evidenceDigest);
  assert.notEqual(viaRpc.reviewId, viaTool.reviewId);
  assert.equal(viaRpc.threadId, "thread-rpc");
  assert.equal(viaTool.threadId, "thread-tool");

  const plan = receipt.parse(await callRpc(harness, "jev.workflow.plan", "thread-rpc", {
    requirementsReviewId: viaRpc.reviewId, steps,
  }));
  assert.equal(plan.parentReviewId, viaRpc.reviewId);
  const toolPlan = receipt.parse(await callTool(harness, "jev_review_plan", {
    requirementsReviewId: viaTool.reviewId, steps,
  }, "thread-tool"));
  assert.equal(plan.evidenceDigest, toolPlan.evidenceDigest);

  const evidence = { planReviewId: plan.reviewId, revision, diff: "+ 12 items", testOutput: "2 passed after reload" };
  const acceptance = receipt.parse(await callRpc(harness, "jev.workflow.acceptance", "thread-acc", evidence));
  assert.equal(acceptance.parentReviewId, plan.reviewId);
  assert.equal(acceptance.revision, revision);
  const toolAcceptance = receipt.parse(await callTool(harness, "jev_review_acceptance", evidence, "thread-acc"));
  assert.equal(acceptance.evidenceDigest, toolAcceptance.evidenceDigest);

  const lookup = await harness.behavior.callRpc("jev.workflow.receipt", {
    reviewId: acceptance.reviewId, revision,
  }) as { freshness: string };
  assert.equal(lookup.freshness, "current");
});

test("RPC plan refuses uncovered requirements without calling the judge", async (t) => {
  const transport = mockTransport(t);
  const { harness } = usePlugin(t, "rpc-key");
  const requirements = receipt.parse(await callRpc(harness, "jev.workflow.requirements", "thread-plan", request));
  const before = transport.mock.callCount();
  const refused = await callRpc(harness, "jev.workflow.plan", "thread-plan", {
    requirementsReviewId: requirements.reviewId,
    steps: [{ text: "Показать только срок.", requirementIds: ["R1"] }],
  });
  assert.deepEqual(refused, { error: "invalid_plan_coverage", unknown: [], missing: ["R2"] });
  assert.equal(transport.mock.callCount(), before);
});

test("RPC acceptance carries a Unicode packet larger than 16 KiB", async (t) => {
  mockTransport(t);
  const { harness } = usePlugin(t, "rpc-key");
  const requirements = receipt.parse(await callRpc(harness, "jev.workflow.requirements", "thread-big", request));
  const plan = receipt.parse(await callRpc(harness, "jev.workflow.plan", "thread-big", {
    requirementsReviewId: requirements.reviewId, steps,
  }));
  const input = {
    planReviewId: plan.reviewId, revision,
    diff: `+ ${big}`, testOutput: `passed ${big}`,
  };
  const bytes = Buffer.byteLength(JSON.stringify({ threadId: "thread-big", input }), "utf8");
  assert.ok(bytes > 16 * 1024, `пакет ${bytes} байт не превысил лимит stdin`);
  const acceptance = receipt.parse(await callRpc(harness, "jev.workflow.acceptance", "thread-big", input));
  assert.equal(acceptance.stage, "acceptance");
  assert.equal(acceptance.revision, revision);
});

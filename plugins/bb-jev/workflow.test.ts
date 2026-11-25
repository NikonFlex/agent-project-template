import assert from "node:assert/strict";
import test from "node:test";
import { freshness, receipt, refusal } from "./workflow-records.ts";
import {
  mockTransport, observed, readRequest, usePlugin,
} from "./workflow-fixtures.ts";

const threadId = "arena-fixture";
const revision = "a".repeat(64);
const request = {
  request: "Count items, reject null, and verify the result after reload.",
  requirements: [
    { id: "R1", text: "Report the number of items." },
    { id: "R2", text: "Reject null." },
    { id: "R3", text: "Verify after reload." },
  ],
};
const steps = [{ text: "Implement counter and test null, then repeat after reload.", requirementIds: ["R1", "R2", "R3"] }];
const execution = {
  revision, diff: "+ return items?.length ?? 0;",
  testOutput: "Count test PASS. Null must throw, got 0. Reload check was skipped.",
};
const lookupMethod = "jev.workflow.receipt";

function sentState(transport: ReturnType<typeof mockTransport>, index: number) {
  const [, request] = transport.mock.calls[index].arguments;
  assert.ok(request);
  return JSON.parse(readRequest(request).state) as unknown;
}

test("the Arena can verify a receipt chain offline against its own revision", async (t) => {
  const transport = mockTransport(t);
  const { harness, call } = usePlugin(t, "fixture-key");
  const requirements = receipt.parse(await call("jev_review_requirements", request));
  assert.equal(requirements.status, "passed");

  await rejectInvalidPlan(call, requirements.reviewId);
  assert.equal(transport.mock.callCount(), 1);

  const plan = receipt.parse(await call("jev_review_plan", { requirementsReviewId: requirements.reviewId, steps }));
  assert.equal(plan.parentReviewId, requirements.reviewId);
  assert.equal(plan.status, "passed");

  const evidence = { planReviewId: plan.reviewId, ...execution };
  const acceptance = receipt.parse(await call("jev_review_acceptance", evidence));
  assert.equal(acceptance.parentReviewId, plan.reviewId);
  assert.equal(acceptance.status, "failed");
  assert.deepEqual(Object.values(acceptance.decisions).map(({ status }) => status), ["passed", "failed", "inconclusive"]);
  assert.equal(acceptance.inputTokens, 150);
  assert.equal(transport.mock.callCount(), 3);
  assert.deepEqual(sentState(transport, 0), request);
  assert.deepEqual(sentState(transport, 1), { requirements: request.requirements, steps });
  assert.deepEqual(sentState(transport, 2), { ...evidence, requirements: request.requirements, steps });

  await checkLookup(harness, acceptance.reviewId);
  const totals = JSON.parse((await harness.behavior.runCli(["status"], { threadId })).stdout!);
  assert.deepEqual({ calls: totals.session.calls, tokens: totals.session.inputTokens }, { calls: 3, tokens: 450 });
});

async function rejectInvalidPlan(call: ReturnType<typeof usePlugin>["call"], requirementsReviewId: string) {
  const incomplete = { requirementsReviewId, steps: [{ text: "Count.", requirementIds: ["R1", "R4"] }] };
  assert.deepEqual(await call("jev_review_plan", incomplete), {
    error: refusal.Coverage, unknown: ["R4"], missing: ["R2", "R3"],
  });
}

async function checkLookup(harness: ReturnType<typeof usePlugin>["harness"], reviewId: string) {
  const same = await harness.behavior.callRpc(lookupMethod, { threadId, reviewId, revision });
  const newVersion = await harness.behavior.callRpc(lookupMethod, { threadId, reviewId, revision: "b".repeat(64) });
  const missingRevision = await harness.behavior.callRpc(lookupMethod, { threadId, reviewId });
  assert.match(JSON.stringify(same), new RegExp(`"freshness":"${freshness.Current}"`));
  assert.match(JSON.stringify(newVersion), new RegExp(`"freshness":"${freshness.Stale}"`));
  assert.match(JSON.stringify(missingRevision), new RegExp(`"freshness":"${freshness.Unverified}"`));
}

test("unavailable requirements and plan reviews stay visible without calling TypeSafe", async (t) => {
  const transport = mockTransport(t);
  const { call } = usePlugin(t);
  const review = receipt.parse(await call("jev_review_requirements", request));
  assert.equal(review.status, "unavailable");
  assert.equal(review.reason, "missing_api_key");
  const plan = receipt.parse(await call("jev_review_plan", { requirementsReviewId: review.reviewId, steps }));
  assert.equal(plan.status, "unavailable");
  assert.equal(plan.parentReviewId, review.reviewId);
  assert.equal(transport.mock.callCount(), 0);
});

test("a coder can use a planner's receipt and each review retains its author", async (t) => {
  mockTransport(t);
  const { harness, call } = usePlugin(t, "fixture-key");
  const planner = "thread-planner";
  const coder = "thread-coder";
  const requirements = receipt.parse(await call("jev_review_requirements", request, planner));
  const planInput = { requirementsReviewId: requirements.reviewId, steps };
  const plan = receipt.parse(await call("jev_review_plan", planInput, planner));
  const acceptance = receipt.parse(await call("jev_review_acceptance", { planReviewId: plan.reviewId, ...execution }, coder));
  const lookup = { reviewId: acceptance.reviewId, revision };
  const expected = { ...acceptance, freshness: freshness.Current };
  assert.deepEqual(await harness.behavior.callRpc(lookupMethod, lookup), expected);
  assert.deepEqual(await call("jev_review_status", lookup, planner), expected);
  const withCaller = await harness.behavior.callRpc(lookupMethod, { ...lookup, threadId: planner });
  assert.deepEqual(withCaller, expected);

  for (const [entry, author, parent] of [
    [acceptance, coder, plan.reviewId], [plan, planner, requirements.reviewId], [requirements, planner, null],
  ] as const) {
    const stored = receipt.parse(await harness.behavior.callRpc(lookupMethod, { reviewId: entry.reviewId }));
    assert.equal(stored.threadId, author);
    assert.equal(stored.parentReviewId, parent);
  }
  for (const [threadId, calls] of [[planner, 2], [coder, 1]] as const) {
    const totals = JSON.parse((await harness.behavior.runCli(["status"], { threadId })).stdout!);
    assert.equal(totals.session.calls, calls);
  }
  const planFromCoder = receipt.parse(await call("jev_review_plan", planInput, coder));
  assert.equal(planFromCoder.parentReviewId, requirements.reviewId);
});

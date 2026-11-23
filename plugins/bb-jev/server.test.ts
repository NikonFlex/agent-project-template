import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import { createFakePluginHost } from "@get-bb/plugin-sdk/testing";
import { checks } from "./checks.ts";
import { createPlugin } from "./server.ts";

const samples = {
  jev_check_plan: { requirements: "Return an integer tally", plan: "Count entries and test empty input" },
  jev_acceptance: { criteria: "Empty input returns zero", diff: "+ return items.length", testOutput: "empty input: OK" },
  jev_fact_check: { claim: "Earth has one Moon", sources: [{ reference: "fixture", quote: "Earth has one Moon" }] },
} satisfies Record<keyof typeof checks, object>;

function answer(choice = "passed") {
  return {
    model: "jev-fixture",
    answers: { verdict: {
      type: "choice", choice, confidence: 0.9,
      probabilities: { passed: 0.9, failed: 0.04, inconclusive: 0.06 },
    } },
    usage: { input_tokens: 2500 },
  };
}

function host(t: TestContext, settings?: { typesafeApiKey: string }) {
  const fake = createFakePluginHost({ pluginId: "bb-jev", settings });
  t.after(() => fake.harness.lifecycle.dispose());
  return fake;
}

async function result(harness: ReturnType<typeof host>["harness"], name: string, input: unknown) {
  const output = await harness.behavior.callAgentTool(name, input, { threadId: "thread-one" });
  assert.ok(typeof output === "object");
  assert.equal(output.isError, undefined);
  const content = output.content[0];
  assert.equal(content.type, "text");
  return JSON.parse(content.text);
}

test("registers all three tools and records unavailable without sending a request", async (t) => {
  const { bb, harness } = host(t);
  createPlugin(async () => assert.fail("Unexpected API call"), () => undefined)(bb);
  for (const [name, input] of Object.entries(samples)) {
    assert.deepEqual(await result(harness, name, input), {
      status: "unavailable", reason: "missing_api_key", inputTokens: null,
    });
  }
  const cli = await harness.behavior.runCli(["status"], { threadId: "thread-one" });
  assert.equal(cli.exitCode, 0);
  const report = JSON.parse(cli.stdout!);
  assert.equal(report.keyConfigured, false);
  assert.equal(report.session.verdicts.unavailable, 3);
  assert.equal(report.total.requestsWithoutUsage, 0);
  assert.equal(report.total.estimatedCostUsd, 0);
  const other = await harness.behavior.runCli(["status"], { threadId: "other" });
  assert.equal(JSON.parse(other.stdout!).session.calls, 0);
});

test("validates all inputs before using the SDK and tallies verdicts and usage", async (t) => {
  const { bb, harness } = host(t, { typesafeApiKey: "settings-fixture" });
  const decisions = ["passed", "failed", "inconclusive"];
  const mock = t.mock.fn(async (key: string) => {
    assert.equal(key, "settings-fixture");
    return answer(decisions.shift());
  });
  createPlugin(mock, () => "env-fixture")(bb);
  for (const [index, [name, input]] of Object.entries(samples).entries()) {
    await assert.rejects(result(harness, name, {}));
    const expected = answer(["passed", "failed", "inconclusive"][index]);
    const verdict = await result(harness, name, input);
    assert.equal(verdict.status, expected.answers.verdict.choice);
    assert.deepEqual(verdict.probabilities, expected.answers.verdict.probabilities);
  }
  assert.equal(mock.mock.callCount(), 3);
  const cli = await harness.behavior.runCli(["status"]);
  const report = JSON.parse(cli.stdout!);
  assert.equal(report.session, null);
  assert.equal(report.keyConfigured, true);
  assert.deepEqual(report.total.verdicts, { passed: 1, failed: 1, inconclusive: 1, unavailable: 0 });
  assert.equal(report.total.inputTokens, 7500);
  assert.equal(report.total.estimatedCostUsd, 0.000315);
  assert.ok(!cli.stdout?.includes("settings-fixture"));
});

test("rejects malformed verdicts and keeps SDK errors out of the timeline", async (t) => {
  const { bb, harness } = host(t);
  const bad = answer("unknown");
  const mock = t.mock.fn(async (key: string) => {
    assert.equal(key, "env-fixture");
    if (mock.mock.callCount() === 0) return bad;
    throw new Error("sensitive transport error");
  });
  createPlugin(mock, () => "env-fixture")(bb);
  const plan = samples.jev_check_plan;
  assert.deepEqual(await result(harness, "jev_check_plan", plan), {
    status: "unavailable", reason: "invalid_response", inputTokens: 2500,
  });
  assert.deepEqual(await result(harness, "jev_check_plan", plan), {
    status: "unavailable", reason: "request_failed", inputTokens: null,
  });
  const cli = await harness.behavior.runCli(["status"]);
  assert.equal(JSON.parse(cli.stdout!).total.requestsWithoutUsage, 1);
});

test("sends the three question shapes through the TypeSafe SDK with offline mocked transport", async (t) => {
  const { bb, harness } = host(t);
  const fetch = t.mock.method(globalThis, "fetch", async (url: string, request: RequestInit) => {
    assert.equal(url, "https://api.typesafe.ai/v1/systemone");
    const body = JSON.parse(String(request.body));
    const name = Object.keys(samples)[fetch.mock.callCount()] as keyof typeof samples;
    assert.deepEqual(JSON.parse(body.state), samples[name]);
    assert.equal(body.model, "jev-latest");
    assert.equal(body.questions.verdict.type, "choice");
    assert.deepEqual(body.questions.verdict.criteria, checks[name].criteria);
    return Response.json(answer());
  });
  createPlugin(undefined, () => "transport-fixture")(bb);
  for (const [name, input] of Object.entries(samples)) {
    assert.equal((await result(harness, name, input)).status, "passed");
  }
  assert.equal(fetch.mock.callCount(), 3);
});

test("Provider Usage reports the latest balance snapshot minus subsequent Jev spend", async (t) => {
  const { bb, harness } = host(t);
  createPlugin(async () => ({
    ...answer(),
    usage: { input_tokens: 1_000_000 },
  }), () => "env-fixture")(bb);
  const badge = () => harness.behavior.callRpc("provider-usage.v1.getResource", {
    resourceId: "jev-balance", refresh: true,
  });
  assert.match(JSON.stringify(await badge()), /Баланс не задан/);
  assert.notEqual((await harness.behavior.runCli(["balance", "set", "NaN"])).exitCode, 0);
  assert.notEqual((await harness.behavior.runCli(["balance", "set", "1.005"])).exitCode, 0);

  const useJev = () => result(harness, "jev_check_plan", samples.jev_check_plan);
  const setBalance = (amount: string) => harness.behavior.runCli(["balance", "set", amount]);
  await useJev();
  assert.equal((await setBalance("10.00")).exitCode, 0);
  assert.match(JSON.stringify(await badge()), /≈ \$10\.00/);
  await useJev();
  assert.match(JSON.stringify(await badge()), /≈ \$9\.96/);
  assert.equal((await setBalance("0.01")).exitCode, 0);
  await useJev();
  assert.match(JSON.stringify(await badge()), /≈ \$0\.00/);
});

import assert from "node:assert/strict";
import test from "node:test";
import { createFakePluginHost, makePluginAgentConfigurationContext,
  makeThreadResponse } from "@get-bb/plugin-sdk/testing";
import type { BbPluginApi } from "@get-bb/plugin-sdk";
import type { Judge, JudgeQuestion } from "jevstice/src/judge.js";
import { observedTools, ownerText } from "./observations.ts";
import { createPlugin } from "./server.ts";

type Row = Awaited<ReturnType<BbPluginApi["sdk"]["threads"]["events"]["list"]>>[number];
const request = "Добавь проверку пустой строки.";
const input = { initiator: "user", input: [{ type: "text", text: request }] };

function patch() {
  return { item: { type: "fileChange", status: "completed",
    changes: [{ path: "demo.py", diff: "+return bool(text.strip())" }] } };
}
function command(code = 0) {
  return { item: { type: "commandExecution", command: "python -m pytest tests/test_demo.py",
    status: "completed", exitCode: code, aggregatedOutput: "1 passed in 0.1s" } };
}
function row(threadId: string, seq: number, type: string, data: unknown): Row {
  return { threadId, seq, type, data } as Row;
}

function answer(question: JudgeQuestion) {
  const labels = Object.keys(question.options ?? {});
  const selected = labels.find(label => label === "observed")
    ?? labels.find(label => /^(?:item|evidence)_?\d+$/i.test(label)) ?? labels[0];
  return { name: question.name, mode: question.mode, label: selected,
    confidence: 1, probability: 1, score: 1 };
}
function fixture() {
  const root = makeThreadResponse({ id: "thr_parent", projectId: "proj_test" });
  const child = makeThreadResponse({ id: "thr_child", parentThreadId: root.id, projectId: root.projectId });
  const foreign = makeThreadResponse({ id: "thr_foreign", projectId: "proj_other" });
  const values = new Map([[root.id, root], [child.id, child], [foreign.id, foreign]]);
  const rows: Row[] = [row(root.id, 1, "client/turn/requested", input)];
  const calls: { state: unknown; questions: readonly JudgeQuestion[] }[] = [];
  const judge: Judge = async (state, questions) => {
    calls.push({ state, questions });
    return { ok: true, answers: questions.map(answer), usage: { inputTokens: 1, outputTokens: 1 } };
  };
  const { bb, harness } = createFakePluginHost({
    pluginId: "bb-jevstice", agentSkillIds: ["jevstice"],
    sdk: { threads: {
      get: async ({ threadId }) => values.get(threadId)!,
      events: { list: async ({ threadId, afterSeq, limit, order, types }) => {
        const found = rows.filter(entry => entry.threadId === threadId && entry.seq > Number(afterSeq ?? 0)
          && (!types || types.some(type => type === entry.type)));
        return (order === "desc" ? found.reverse() : found).slice(0, Number(limit ?? 100));
      } },
    } },
  });
  createPlugin(judge)(bb);
  return { harness, root, child, foreign, rows, calls };
}

test("accepts only observed, completed tool material and omits secret-looking outputs", () => {
  assert.equal(ownerText(input), request);
  assert.equal(ownerText({ ...input, initiator: "agent" }), null);
  assert.equal(observedTools(command(1)).items[0]?.isError, true);
  assert.match(observedTools(command()).items[0]!.content[0]!.text, /exit code: 0/);
  assert.equal(observedTools(patch()).items[0]?.toolName, "edit");
  assert.deepEqual(observedTools({ item: { type: "fileRead", path: "x.py", status: "completed" } }),
    { items: [], unavailable: 1 });
  assert.equal(observedTools({ item: { ...command().item, command: "cat .env" } }).unavailable, 1);
  assert.deepEqual(observedTools({ item: { type: "reasoning", text: "private" } }),
    { items: [], unavailable: 0 });
});

test("uses native Jevstice requirements and acceptance over observed cross-thread work", async t => {
  const { harness, root, child, foreign, rows, calls } = fixture();
  t.after(() => harness.lifecycle.dispose());
  await harness.behavior.callRpc("jevstice.session.on", { threadId: root.id, request });
  const args = { items: [{ text: "Пустая строка отвергается.", source: "проверку пустой строки" }] };
  const call = (name: string, payload: unknown, threadId: string) =>
    harness.behavior.callAgentTool(`jevstice_${name}`, payload, { threadId });
  await call("requirements", args, foreign.id);
  assert.equal(calls.length, 0);
  const requirements = await call("requirements", args, root.id);
  assert.equal(calls.length, 1, JSON.stringify(requirements));
  await harness.behavior.emitThreadEvent("thread.created", { thread: child });
  rows.push(row(child.id, 1, "item/completed", patch()), row(child.id, 2, "item/completed", command()));
  const accepted = await call("acceptance", { aspect: "outcome" }, child.id);
  assert.equal(calls.length, 2, JSON.stringify(accepted));
  assert.match(JSON.stringify(calls[1]?.state), /1 passed in 0\.1s/);
  assert.match(JSON.stringify(calls[1]?.state), /demo\.py/);
  const status = await harness.behavior.runCli(["status"], { threadId: child.id });
  assert.match(status.stdout ?? "", /"used": 2/);
  assert.match(status.stdout ?? "", /"inputTokens": 2/);
  await harness.behavior.runCli(["off"], { threadId: root.id });
  const stopped = await harness.behavior.runCli(["status"], { threadId: root.id });
  assert.match(stopped.stdout ?? "", /"active": false/);
});


test("all upstream tools are advertised only for a participating BB session", async t => {
  const { harness, root, child, foreign } = fixture();
  t.after(() => harness.lifecycle.dispose());
  const configuration = (thread: typeof root) => harness.behavior.resolveAgentConfiguration(
    makePluginAgentConfigurationContext({ thread, project: { id: thread.projectId } }));

  assert.equal((await configuration(root)).tools.length, 0);
  await harness.behavior.callRpc("jevstice.session.on", { threadId: root.id, request });
  assert.equal((await configuration(child)).tools.length, 15);
  assert.equal((await configuration(foreign)).tools.length, 0);
});

test("the header can enable a thread before its first prompt and reflects inherited mode", async t => {
  const { harness, root, child, rows, calls } = fixture();
  t.after(() => harness.lifecycle.dispose());
  rows.length = 0;
  const modes = () => harness.behavior.callRpc("jevstice.mode.list", { threadIds: [root.id, child.id] });
  const set = (threadId: string, active: boolean) =>
    harness.behavior.callRpc("jevstice.mode.set", { threadId, active });
  assert.deepEqual(await modes(), { [root.id]: "off", [child.id]: "off" });
  assert.equal(await set(root.id, true), "on");
  await harness.behavior.emitThreadEvent("thread.created", { thread: child });
  assert.deepEqual(await modes(), { [root.id]: "on", [child.id]: "inherited" });
  assert.equal(await set(child.id, false), "inherited");
  assert.equal(await set(root.id, false), "off");
  assert.deepEqual(await modes(), { [root.id]: "off", [child.id]: "off" });
  assert.equal(calls.length, 0);
});

test("header activation allows more than eight judge requests", async t => {
  const { harness, root, calls } = fixture();
  t.after(() => harness.lifecycle.dispose());
  await harness.behavior.callRpc("jevstice.mode.set", { threadId: root.id, active: true });
  for (let index = 1; index <= 9; index++) {
    await harness.behavior.callAgentTool("jevstice_consult", {
      mode: "boolean", question: `Should case ${index} pass?`, trueMeaning: "yes", falseMeaning: "no",
      context: "The validator rejects empty input.",
    }, { threadId: root.id });
  }
  assert.equal(calls.length, 9);
  const current = await harness.behavior.runCli(["status"], { threadId: root.id });
  assert.equal(JSON.parse(current.stdout ?? "").used, 9);
});

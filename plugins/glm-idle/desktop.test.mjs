import assert from "node:assert/strict";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { readDesktop, submitDesktop } from "./desktop.ts";
import { offPeakStatus } from "./off-peak.ts";

test("ZCode adapter honors ticket availability and uses the idle provider", async (t) => {
  const port = 31415;
  const file = "file:///opt/ZCode/resources/app.asar/out/renderer/index.html";
  t.mock.method(globalThis, "fetch", async () => Response.json([{
    type: "page",
    url: file + "#/automations",
    webSocketDebuggerUrl: `ws://127.0.0.1:${port}/devtools/page/TEST`,
  }]));

  const submitted = [];
  const tasks = [];
  let available = false;
  const service = {
    getCodingPlanSupport: async () => ({ supported: true, kind: "zai-personal" }),
    getTakeNumberAvailability: async () => ({ canTakeNumber: available, nextTakeAt: 1795449600000 }),
    list: async () => tasks,
    async createTask(input) {
      submitted.push(input);
      const task = { offPeakTaskId: "offpeak-test", status: "queued", workspacePath: input.workspacePath };
      tasks.push(task);
      return { ok: true, task, queuePosition: 3 };
    },
  };
  const fiber = { memoizedProps: { value: { offPeakTaskService: service } } };
  const element = { "__reactContainer$test": { stateNode: { current: fiber } } };

  class CdpSocket extends EventTarget {
    constructor(url) {
      super();
      assert.equal(url, `ws://127.0.0.1:${port}/devtools/page/TEST`);
      queueMicrotask(() => this.dispatchEvent(new Event("open")));
    }
    close() {}
    async send(raw) {
      const { id, method, params } = JSON.parse(raw);
      assert.equal(method, "Runtime.evaluate");
      const value = await runInNewContext(params.expression, {
        document: { getElementById: () => element },
      });
      const data = JSON.stringify({ id, result: { result: { value } } });
      this.dispatchEvent(new MessageEvent("message", { data }));
    }
  }
  const originalWebSocket = globalThis.WebSocket;
  globalThis.WebSocket = CdpSocket;
  t.after(() => { globalThis.WebSocket = originalWebSocket; });
  const input = { prompt: 'literal "text" and `code`', title: "Review", workspacePath: "/safe/path", permissionMode: "plan" };

  assert.equal((await readDesktop(port)).quota.canTakeNumber, false);
  assert.equal((await submitDesktop(port, input)).reason, "no_ticket");
  assert.equal(submitted.length, 0);

  available = true;
  assert.deepEqual(await submitDesktop(port, input),
    { ok: true, id: "offpeak-test", status: "queued", queuePosition: 3 });
  assert.equal(submitted[0].prompt, input.prompt);
  assert.equal(submitted[0].permissionMode, "plan");
  assert.equal(submitted[0].modelSelection.providerId, "account:zai-offpeak-idle-plan");
  assert.equal((await submitDesktop(port, input)).reason, "workspace_busy");
  assert.equal(submitted.length, 1);
});

test("the published peak window uses weekday UTC boundaries", () => {
  const at = (time) => offPeakStatus(Date.parse(time));
  assert.equal(at("2026-11-23T05:59:59Z").offPeak, true);
  assert.equal(at("2026-11-23T06:00:00Z").offPeak, false);
  assert.equal(at("2026-11-23T10:00:00Z").offPeak, true);
  assert.equal(at("2026-11-20T10:00:00Z").nextChangeAt, "2026-11-23T06:00:00.000Z");
  assert.equal(at("2026-11-21T07:00:00Z").offPeak, true);
});

import { request } from "node:http";
import { z } from "zod";

const commandTimeoutMs = 15_000;
const rendererPath = "/app.asar/out/renderer/index.html";
const desktopTarget = z.object({
  type: z.string(),
  url: z.string(),
  webSocketDebuggerUrl: z.string().optional(),
});
const reply = z.object({
  id: z.literal(1),
  result: z.object({
    result: z.object({ value: z.unknown().optional() }),
    exceptionDetails: z.unknown().optional(),
  }).optional(),
});
const debuggerPath = /^\/devtools\/page\/[a-z0-9-]+$/i;

async function rendererSocket(port: number): Promise<string> {
  const origin = `http://127.0.0.1:${port}`;
  const response = await fetch(`${origin}/json/list`, {
    redirect: "error",
    signal: AbortSignal.timeout(commandTimeoutMs),
  });
  if (!response.ok) throw new Error("ZCode Desktop did not list its renderer");
  const targets = z.array(desktopTarget).parse(await response.json());
  const renderer = targets.find((target) => target.type === "page"
    && target.url.startsWith("file://") && new URL(target.url).pathname.endsWith(rendererPath));
  const path = renderer?.webSocketDebuggerUrl && new URL(renderer.webSocketDebuggerUrl).pathname;
  if (!path || !debuggerPath.test(path)) throw new Error("ZCode Desktop renderer unavailable");
  return `ws://127.0.0.1:${port}${path}`;
}

async function evaluate(url: string, expression: string): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(url);
    const timeout = setTimeout(() => fail("ZCode Desktop did not respond"), commandTimeoutMs);
    function dispose() {
      clearTimeout(timeout);
      socket.close();
    }
    function fail(message: string) {
      dispose();
      reject(new Error(message));
    }
    socket.addEventListener("open", () => {
      socket.send(JSON.stringify({ id: 1, method: "Runtime.evaluate",
        params: { expression, awaitPromise: true, returnByValue: true } }));
    }, { once: true });
    socket.addEventListener("error", () => fail("Cannot connect to ZCode Desktop"), { once: true });
    socket.addEventListener("close", () => fail("ZCode Desktop closed the connection"), { once: true });
    socket.addEventListener("message", (event) => {
      if (typeof event.data !== "string") return;
      let message: unknown;
      try { message = JSON.parse(event.data); } catch { return; }
      const result = reply.safeParse(message);
      if (!result.success) return;
      const evaluation = result.data.result;
      if (!evaluation || evaluation.exceptionDetails) return fail("ZCode Desktop did not accept the command");
      dispose();
      resolve(evaluation.result.value);
    });
  });
}

export type DesktopEndpoint = number | string;

async function callBridge(socketPath: string, expression: string): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify({ expression });
    const req = request({
      socketPath, path: "/evaluate", method: "POST",
      headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload) },
      signal: AbortSignal.timeout(commandTimeoutMs),
    }, async (response) => {
      try {
        if (response.statusCode !== 200) throw new Error("ZCode bridge unavailable");
        const chunks: Buffer[] = [];
        for await (const chunk of response) chunks.push(chunk);
        const data = z.object({ value: z.unknown() }).parse(JSON.parse(Buffer.concat(chunks).toString()));
        resolve(data.value);
      } catch (error) {
        response.resume();
        reject(error);
      }
    });
    req.on("error", reject);
    req.end(payload);
  });
}

async function runDesktop(endpoint: DesktopEndpoint, expression: string) {
  if (typeof endpoint === "string") return callBridge(endpoint, expression);
  return evaluate(await rendererSocket(endpoint), expression);
}

// This adapter targets the isolated desktop renderer for ZCode 4.0. The
// renderer handles OAuth; only task status and non-secret parameters cross BB.
const findService = `
  const container = document.getElementById("root");
  const key = Object.keys(container ?? {}).find(key => key.startsWith("__reactContainer$"));
  const root = key && container[key].stateNode.current;
  const pending = root ? [root] : [];
  let service;
  while (pending.length) {
    const fiber = pending.pop();
    const value = fiber.memoizedProps?.value;
    if (value && Object.hasOwn(value, "offPeakTaskService")) {
      service = value.offPeakTaskService;
      break;
    }
    if (fiber.sibling) pending.push(fiber.sibling);
    if (fiber.child) pending.push(fiber.child);
  }
  if (!service) throw Error("ZCode off-peak service not available");
`;

const projectTasks = `
  return tasks.slice(0, 20).map(task => ({
    id: task.offPeakTaskId, status: task.status, queuePosition: task.queuePosition,
  }));
`;

export interface IdleSubmission {
  title: string;
  prompt: string;
  workspacePath: string;
  permissionMode: "build" | "plan";
}

export async function readDesktop(endpoint: DesktopEndpoint): Promise<unknown> {
  return runDesktop(endpoint, `(async () => {
    ${findService}
    const [support, tasks] = await Promise.all([service.getCodingPlanSupport(), service.list()]);
    const quota = support.supported ? await service.getTakeNumberAvailability() : null;
    return { support, quota, tasks: (() => { ${projectTasks} })() };
  })()`);
}

export async function submitDesktop(endpoint: DesktopEndpoint, submission: IdleSubmission): Promise<unknown> {
  return runDesktop(endpoint, `(async () => {
    ${findService}
    const input = ${JSON.stringify(submission)};
    const [support, tasks] = await Promise.all([service.getCodingPlanSupport(), service.list()]);
    if (!support.supported || support.kind !== "zai-personal") return { ok: false, reason: "plan_unavailable" };
    const quota = await service.getTakeNumberAvailability();
    if (quota.canTakeNumber !== true) return { ok: false, reason: "no_ticket", nextTakeAt: quota.nextTakeAt };
    if (tasks.some(task => task.workspacePath === input.workspacePath &&
        ["queued", "running", "paused"].includes(task.status))) {
      return { ok: false, reason: "workspace_busy" };
    }
    const result = await service.createTask({
      ...input, hostId: "local",
      modelSelection: {
        providerId: "account:zai-offpeak-idle-plan",
        modelId: "GLM-5.3-Flash",
        options: { reasoningLevel: "max" },
      },
    });
    if (!result.ok) return { ok: false, reason: result.errorCategory };
    return {
      ok: true, id: result.task.offPeakTaskId, status: result.task.status,
      queuePosition: result.queuePosition,
    };
  })()`);
}

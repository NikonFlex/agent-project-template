import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { chmodSync, rmSync } from "node:fs";

const [binary, socketPath] = process.argv.slice(2);
const requestTimeoutMs = 15_000;
const maxMessage = 256 * 1024;
const desktop = spawn("/usr/bin/xvfb-run", ["-a", binary, "--disable-gpu", "--remote-debugging-pipe"], {
  stdio: ["ignore", "ignore", "ignore", "pipe", "pipe"],
});
const pending = new Map();
let sequence = 0;
let buffer = Buffer.alloc(0);

function command(method, params, sessionId) {
  return new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error("ZCode command timed out"));
    }, requestTimeoutMs);
    pending.set(id, (response) => {
      clearTimeout(timer);
      if (response.error) reject(new Error("ZCode rejected the command"));
      else resolve(response.result);
    });
    desktop.stdio[3].write(JSON.stringify({ id, method, params, sessionId }) + "\0");
  });
}

desktop.stdio[3].on("error", () => process.exit(1));
desktop.stdio[4].on("error", () => process.exit(1));
desktop.stdio[4].on("data", (chunk) => {
  buffer = Buffer.concat([buffer, chunk]);
  let end;
  while ((end = buffer.indexOf(0)) >= 0) {
    const raw = buffer.subarray(0, end).toString("utf8");
    buffer = buffer.subarray(end + 1);
    try {
      const response = JSON.parse(raw);
      const done = pending.get(response.id);
      pending.delete(response.id);
      done?.(response);
    } catch {}
  }
  if (buffer.length > maxMessage) process.exit(1);
});

async function evaluate(expression) {
  const { targetInfos } = await command("Target.getTargets", {});
  const renderer = targetInfos.find((target) => target.type === "page"
    && target.url.startsWith("file://")
    && new URL(target.url).pathname.endsWith("/app.asar/out/renderer/index.html"));
  if (!renderer) throw new Error("ZCode renderer unavailable");
  const { sessionId } = await command("Target.attachToTarget", { targetId: renderer.targetId, flatten: true });
  try {
    const result = await command("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }, sessionId);
    if (result.exceptionDetails) throw new Error("ZCode evaluation failed");
    return result.result.value;
  } finally {
    await command("Target.detachFromTarget", { sessionId }).catch(() => {});
  }
}

async function readExpression(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maxMessage) throw new Error("Request too large");
    chunks.push(chunk);
  }
  const { expression } = JSON.parse(Buffer.concat(chunks));
  if (typeof expression !== "string") throw new Error("Invalid request");
  return expression;
}

const server = createServer(async (request, response) => {
  if (request.method !== "POST" || request.url !== "/evaluate") {
    response.writeHead(404).end();
    return;
  }
  try {
    const value = await evaluate(await readExpression(request));
    response.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ value }));
  } catch {
    response.writeHead(503).end();
  }
});
desktop.on("error", () => process.exit(1));
desktop.on("exit", () => process.exit(1));
rmSync(socketPath, { force: true });
server.listen(socketPath, () => chmodSync(socketPath, 0o600));

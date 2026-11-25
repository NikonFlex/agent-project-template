import { randomUUID } from "node:crypto";
import { join } from "node:path";
import type { BbPluginApi } from "@get-bb/plugin-sdk";
import type { Judge } from "jevstice/src/judge.js";
import { summarizeActivity } from "./activity.ts";
import { createEngine, type Engine } from "./engine.ts";
import type { openJournal } from "./journal.ts";
import { activityTag } from "./mode-rpc.ts";
import { boundedEvidence, observedTools, ownerText, record } from "./observations.ts";
import { createTurnReview, type TurnReview } from "./turn-review.ts";

type Journal = ReturnType<typeof openJournal>;
type Thread = Awaited<ReturnType<BbPluginApi["sdk"]["threads"]["get"]>>;
const observedTypes = ["client/turn/requested", "item/completed", "turn/completed"] as const;
const pageSize = 100;

type Scope = {
  engine: Engine;
  projectId: string;
  request: string | null;
  children: Set<string>;
  childEnvironmentId?: string;
  used: number;
  pending: number;
  unavailable: number;
  inputTokens: number;
  outputTokens: number;
  closed: boolean;
  tail: Promise<unknown>;
  cursors: Map<string, number>;
  workspace: Thread;
  turn?: TurnReview;
  abort: AbortController;
};

export function createScopes(bb: BbPluginApi, journal: Journal, judge: Judge, keyReady: () => Promise<boolean>) {
  const roots = new Map<string, Scope>();
  const starting = new Map<string, Promise<void>>();

  function scopeFor(thread: Thread) {
    const key = roots.has(thread.id) ? thread.id : thread.parentThreadId;
    const scope = key === null ? undefined : roots.get(key);
    if (!scope || scope.projectId !== thread.projectId || scope.closed) return null;
    if (thread.id !== key && !scope.children.has(thread.id)) return null;
    return { key: key!, scope };
  }

  function queue<T>(scope: Scope, action: () => Promise<T>): Promise<T> {
    const task = scope.tail.then(action);
    scope.tail = task.catch(() => {});
    return task;
  }

  async function readArtifact(thread: Thread, name: string, limit: number) {
    if (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}$/.test(name) || name.includes("..") ||
        !thread.environmentId) return null;
    const environment = await bb.sdk.environments.get({ environmentId: thread.environmentId });
    if (!environment.path || !environment.hostId) return null;
    const root = join(environment.path, "tmp", "jevstice");
    const file = await bb.sdk.files.read({
      hostId: environment.hostId, rootPath: root, path: join(root, name),
    });
    if (file.sizeBytes > limit) return null;
    const text = file.contentEncoding === "base64"
      ? new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(Buffer.from(file.content, "base64"))
      : file.content;
    return boundedEvidence(text);
  }

  async function getLatestRequest(threadId: string, through: number) {
    const rows = await bb.sdk.threads.events.list({
      threadId, types: ["client/turn/requested"], limit: "20", order: "desc",
    });
    return rows.filter(row => row.seq <= through).map(row => ownerText(row.data))
      .find(text => text !== null) ?? null;
  }

  async function start(thread: Thread, request?: string) {
    const existing = roots.get(thread.id);
    if (existing) {
      if (request !== undefined && existing.request !== request) {
        throw new Error("Сначала завершите текущий сеанс Jevstice");
      }
      return;
    }
    const head = await bb.sdk.threads.events.list({ threadId: thread.id, order: "desc", limit: "1" });
    const cursor = Number(head[0]?.seq ?? 0);
    const owner = request === undefined ? await getLatestRequest(thread.id, cursor) : boundedEvidence(request);
    if (request !== undefined && owner === null) throw new Error("Постановка недоступна для Jevstice");
    const scope: Scope = {
      projectId: thread.projectId, request: owner, children: new Set(),
      used: 0, pending: 0, unavailable: 0, inputTokens: 0, outputTokens: 0, closed: false, tail: Promise.resolve(),
      cursors: new Map([[thread.id, cursor]]), workspace: thread, abort: new AbortController(),
      engine: createEngine({
        scopeId: thread.id,
        judge: async (state, questions) => {
          if (!await keyReady()) {
            return { ok: false, kind: "unavailable", problem: "В bb-jevstice нет ключа TypeSafe" };
          }
          if (scope.closed) {
            return { ok: false, kind: "unavailable", problem: "Сеанс Jevstice закрыт" };
          }
          if (boundedEvidence(JSON.stringify(state), 96_000) === null) {
            return { ok: false, kind: "unusable", problem: "Материал превысил лимит или содержит чувствительные данные" };
          }
          return sendToJudge(scope, thread.id, state, questions);
        },
        note(kind, value) {
          if (scope.closed) return;
          journal.note(thread.id, kind, value);
          if (summarizeActivity(kind, value)) notifyActivity(thread.id);
        },
        archive: journal.archive(`${thread.id}/${randomUUID()}`),
        readArtifact: async (name, limit) => {
          try { return await readArtifact(scope.workspace, name, limit); } catch { return null; }
        },
      }),
    };
    roots.set(thread.id, scope);
    if (owner !== null) await queue(scope, () => scope.engine.owner(owner));
  }

  function notifyActivity(threadId: string) {
    bb.rpc.experimental_invalidate([activityTag(threadId)]);
  }

  async function sendToJudge(scope: Scope, threadId: string, ...input: Parameters<Judge>) {
    scope.used += 1;
    scope.pending += 1;
    if (scope.pending === 1) notifyActivity(threadId);
    try {
      const result = await judge(...input);
      if (result.ok && result.usage) {
        scope.inputTokens += result.usage.inputTokens;
        scope.outputTokens += result.usage.outputTokens;
      }
      return result;
    } finally {
      scope.pending -= 1;
      if (!scope.closed && scope.pending === 0) notifyActivity(threadId);
    }
  }

  const stop = (threadId: string) => {
    const scope = roots.get(threadId);
    if (scope) {
      scope.closed = true;
      scope.abort.abort();
      scope.engine.close();
      roots.delete(threadId);
    }
  };

  async function ingest(thread: Thread, scope: Scope, key: string) {
    if (thread.id !== key && thread.environmentId) {
      if (scope.childEnvironmentId && scope.childEnvironmentId !== thread.environmentId) {
        if (!scope.cursors.has(thread.id)) {
          scope.unavailable += 1;
          journal.note(key, "not_checked", { reason: "другое рабочее дерево", threadId: thread.id });
          scope.cursors.set(thread.id, 0);
        }
        return;
      }
      scope.childEnvironmentId = thread.environmentId;
      scope.workspace = thread;
    }
    let seq = scope.cursors.get(thread.id) ?? 0;
    let length;
    do {
      const rows = await bb.sdk.threads.events.list({
        threadId: thread.id, types: observedTypes, order: "asc",
        afterSeq: String(seq), limit: String(pageSize),
      });
      length = rows.length;
      for (const row of rows) {
        if (scope.closed) return;
        if (row.seq <= seq) continue;
        await consume(scope, row, thread.id === key);
        seq = row.seq;
        scope.cursors.set(thread.id, seq);
      }
    } while (length === pageSize);
  }

  async function consume(scope: Scope, row: { type: string; data: unknown }, owner: boolean) {
    if (row.type === "client/turn/requested" && owner) {
      const text = ownerText(row.data);
      scope.turn = text === null ? undefined : createTurnReview(text);
      if (text !== null) await scope.engine.owner(text);
    } else if (row.type === "item/completed") {
      const observations = observedTools(row.data);
      scope.unavailable += observations.unavailable;
      for (const observation of observations.items) {
        if (owner) scope.turn?.observe(observation);
        await scope.engine.observe(observation);
      }
    } else if (row.type === "turn/completed") {
      if (owner) {
        const material = scope.turn?.material();
        scope.turn = undefined;
        if (material && record(row.data)?.status === "completed") {
          await scope.engine.call("jev_review", material, scope.abort.signal, true);
          return;
        }
      }
      await scope.engine.complete();
    }
  }

  return {
    mode(thread: Thread) {
      const target = scopeFor(thread);
      if (!target) return "off" as const;
      return target.key === thread.id ? "on" as const : "inherited" as const;
    },
    activity(thread: Thread) {
      const target = scopeFor(thread);
      return target ? {
        scope: target.key, pending: target.scope.pending > 0, last: journal.activity(target.key),
      } : { scope: null, pending: false, last: null };
    },
    async activate(thread: Thread, request?: string) {
      const pending = starting.get(thread.id);
      if (pending) await pending;
      const run = start(thread, request);
      starting.set(thread.id, run);
      try { await run; } finally { if (starting.get(thread.id) === run) starting.delete(thread.id); }
    },
    stop,
    created(thread: Thread) {
      const parent = thread.parentThreadId === null ? undefined : roots.get(thread.parentThreadId);
      if (parent?.projectId === thread.projectId) parent.children.add(thread.id);
    },
    observe(thread: Thread) {
      const target = scopeFor(thread);
      return target ? queue(target.scope, () => ingest(thread, target.scope, target.key)) : undefined;
    },
    call(thread: Thread, name: string, args: unknown, signal: AbortSignal) {
      const target = scopeFor(thread);
      if (!target) return { isError: true, content: [{ type: "text" as const, text: "Jevstice выключен. Владелец включает его через bb jevstice on." }] };
      return queue(target.scope, async () => {
        await ingest(thread, target.scope, target.key);
        if (thread.id !== target.key && target.scope.childEnvironmentId &&
            thread.environmentId !== target.scope.childEnvironmentId) {
          return { isError: true, content: [{ type: "text" as const, text: "Наблюдения из другого рабочего дерева не проверены." }] };
        }
        const before = target.scope.used;
        const result = await target.scope.engine.call(name, args, signal);
        if (thread.id === target.key && target.scope.used > before &&
            (name === "jev_review" || name === "jev_acceptance")) target.scope.turn?.clear();
        return result;
      });
    },
    advertised(thread: { id: string; parentThreadId: string | null }, project: string) {
      const parent = thread.parentThreadId;
      const scope = roots.get(thread.id) ?? (parent ? roots.get(parent) : undefined);
      return scope?.projectId === project && !scope.closed;
    },
    status(thread: Thread) {
      const target = scopeFor(thread);
      return target ? {
        active: true, scope: target.key,
        used: target.scope.used, unavailableObservations: target.scope.unavailable,
        inputTokens: target.scope.inputTokens, outputTokens: target.scope.outputTokens,
        diagnostics: journal.recent(target.key),
      } : { active: false, diagnostics: journal.recent(thread.id) };
    },
    dispose() { for (const threadId of roots.keys()) stop(threadId); },
  };
}

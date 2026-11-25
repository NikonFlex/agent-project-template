import type { ContextArchive } from "jevstice/src/context.js";
import type { PiApi, PiToolDefinition, SchemaBuilder } from "jevstice/src/controller.js";
import type { Judge } from "jevstice/src/judge.js";
import { z } from "zod";
import { boundedEvidence, record } from "./observations.ts";
import { DEFAULT_CONFIG, createJevController, createToolCatalog } from "./upstream.mjs";

const schemaBuilder = z as unknown as SchemaBuilder;
export const catalog = () => createToolCatalog(schemaBuilder);
export const nativeName = (name: string) => name.replace(/^jev_/, "jevstice_");

type Callbacks = {
  judge: Judge;
  scopeId: string;
  archive: ContextArchive;
  note: (kind: string, value: unknown) => void;
  readArtifact: (name: string, maxBytes: number) => Promise<string | null>;
};

export function createEngine(callbacks: Callbacks) {
  const handlers = new Map<string, Parameters<PiApi["on"]>[1]>();
  const tools = new Map<string, PiToolDefinition>();
  const context = {
    sessionManager: { getSessionId: () => callbacks.scopeId },
    ui: { notify: (text: string) => callbacks.note("notice", text) },
  };
  const controller = createJevController({
    judge: callbacks.judge,
    config: DEFAULT_CONFIG,
    zod: schemaBuilder,
    context: callbacks.archive,
    readSessionArtifact: (_context, name, limit) => callbacks.readArtifact(name, limit ?? 64_000),
  });
  controller.register({
    on: (event, handler) => { handlers.set(event, handler); },
    registerTool: tool => { tools.set(tool.name, tool); },
    appendEntry: callbacks.note,
    sendMessage: message => callbacks.note("message", message),
  });
  const emit = (name: string, data: unknown) => handlers.get(name)?.(data, context);
  emit("session_start", {});
  return {
    async owner(text: string) {
      await emit("input", { source: "interactive", text });
      await emit("before_agent_start", { reason: "prompt", prompt: text });
    },
    async observe(event: unknown) { await emit("tool_result", event); },
    async complete() {
      const reply = record(await emit("session_stop", {}));
      if (reply?.continue) callbacks.note("not_checked", "Факт-чек запрошен; нужен jevstice_fact_check");
    },
    close() { emit("session_shutdown", {}); },
    async call(name: string, args: unknown, signal: AbortSignal, automatic = false) {
      const tool = tools.get(name);
      if (tool === undefined) throw new Error(`Неизвестная проверка ${name}`);
      if (boundedEvidence(JSON.stringify(args)) === null) {
        const reason = "Материал не проверен: объём или чувствительные данные.";
        if (automatic) callbacks.note("not_checked", { name, automatic, reason });
        return { content: [{ type: "text" as const, text: reason }], isError: true };
      }
      const result = record(await tool.execute("bb", args, signal, undefined, context));
      const parts = Array.isArray(result?.content) ? result.content : [];
      const texts = parts.flatMap(part => {
        const value = record(part)?.text;
        return typeof value === "string" ? [{ type: "text" as const, text: value }] : [];
      });
      callbacks.note("check", { name, ...(automatic ? { automatic } : {}), details: result?.details, isError: result?.isError === true });
      return { content: texts, isError: result?.isError === true };
    },
  };
}

export type Engine = ReturnType<typeof createEngine>;

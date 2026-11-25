import { cliCommand, defineCli, defineRpcContract, PluginCliError, type BbPluginApi } from "@get-bb/plugin-sdk";
import type { Judge } from "jevstice/src/judge.js";
import { z } from "zod";
import { catalog, nativeName } from "./engine.ts";
import { openJournal } from "./journal.ts";
import { modeRpc, modeTag } from "./mode-rpc.ts";
import { createScopes } from "./scopes.ts";
import { createTypeSafeJudge } from "./upstream.mjs";

const lifecycleRpc = defineRpcContract({
  "jevstice.session.on": {
    input: z.object({ threadId: z.string().min(1), request: z.string().min(1).max(16_000) }),
    output: z.object({ active: z.literal(true) }),
  },
});

export function createPlugin(fakeJudge?: Judge) {
  return function plugin(bb: BbPluginApi) {
    const peerReady = async () => {
      try {
        const value = await bb.sdk.plugins.callRpc({
          pluginId: "bb-jev", method: "jev.judge.available", input: {},
          outputSchema: z.object({ configured: z.boolean() }),
        });
        return value.configured;
      } catch { return false; }
    };
    const judge = fakeJudge ?? createTypeSafeJudge({
      resolveKey: async () => "bb-jev",
      createClient: () => ({
        systemOne: input => bb.sdk.plugins.callRpc({
          pluginId: "bb-jev", method: "jev.judge.system-one",
          input: z.json().parse({ state: JSON.stringify(input.state), questions: input.questions }),
          outputSchema: z.json(),
        }),
      }),
    });
    const scopes = createScopes(bb, openJournal(bb.storage), judge,
      async () => fakeJudge !== undefined || await peerReady());
    const definitions = new Map(catalog().map(tool => [nativeName(tool.name), tool]));
    bb.agents.configure(({ thread, project }) => scopes.advertised(thread, project.id)
      ? { tools: [...definitions.keys()], skills: ["jevstice"] }
      : { tools: [], skills: [] });

    for (const [name, tool] of definitions) {
      bb.agents.registerTool({
        name, description: tool.description.replaceAll(/\bjev_\w+/g, nativeName),
        parameters: tool.parameters as z.ZodType,
        presentation: { label: { pending: tool.label, completed: tool.label } },
        async execute(args, { threadId, signal }) {
          const thread = await bb.sdk.threads.get({ threadId });
          return scopes.call(thread, tool.name, args, signal);
        },
      });
    }
    bb.events.on("thread.created", ({ thread }) => {
      scopes.created(thread);
      if (scopes.mode(thread) === "inherited") bb.rpc.experimental_invalidate([modeTag]);
    });
    bb.events.on("experimental_thread.events", async ({ thread }) => { await scopes.observe(thread); });
    bb.events.on("thread.idle", async ({ thread }) => { await scopes.observe(thread); });
    const mode = registerMode(bb, scopes);
    bb.events.on("thread.deleted", ({ thread }) => mode.stop(thread.id));
    bb.events.on("thread.archived", ({ thread }) => mode.stop(thread.id));
    bb.onDispose(() => scopes.dispose());
    bb.rpc.register(lifecycleRpc, {
      async "jevstice.session.on"({ threadId, request }) {
        await mode.activate(await bb.sdk.threads.get({ threadId }), request);
        return { active: true as const };
      },
    }, { experimental_discoverable: true });
    registerCli(bb, { ...scopes, ...mode }, peerReady, definitions);
  };
}

function registerMode(bb: BbPluginApi, scopes: ReturnType<typeof createScopes>) {
  const activate: typeof scopes.activate = async (...args) => {
    await scopes.activate(...args);
    bb.rpc.experimental_invalidate([modeTag]);
  };
  const stop: typeof scopes.stop = threadId => {
    scopes.stop(threadId);
    bb.rpc.experimental_invalidate([modeTag]);
  };
  bb.rpc.register(modeRpc, {
    async "jevstice.mode.list"({ threadIds }) {
      const modes = await Promise.all(threadIds.map(async threadId => {
        const thread = await bb.sdk.threads.get({ threadId });
        return [threadId, scopes.mode(thread)] as const;
      }));
      return Object.fromEntries(modes);
    },
    async "jevstice.mode.set"({ threadId, active }) {
      const thread = await bb.sdk.threads.get({ threadId });
      const current = scopes.mode(thread);
      if (current === "inherited") return current;
      if (active && current === "off") await activate(thread);
      if (!active && current === "on") stop(threadId);
      return scopes.mode(thread);
    },
  }, { experimental_discoverable: true });
  return { activate, stop };
}

function registerCli(
  bb: BbPluginApi, scopes: ReturnType<typeof createScopes>, keyReady: () => Promise<boolean>,
  tools: Map<string, ReturnType<typeof catalog>[number]>,
) {
  async function current(threadId?: string) {
    if (!threadId) throw new PluginCliError("Запустите команду из треда BB", { code: "thread_required" });
    return bb.sdk.threads.get({ threadId });
  }
  bb.cli.register(defineCli({
    name: "jevstice",
    summary: "Включение и журнал диагностики Jevstice",
    commands: {
      on: cliCommand({
        summary: "Включить диагностику текущего треда и его непосредственных детей",
        async run(_args, { threadId }) {
          const thread = await current(threadId);
          await scopes.activate(thread);
          return { exitCode: 0, stdout: "Jevstice включён.\n" };
        },
      }),
      off: cliCommand({
        summary: "Выключить диагностику треда",
        async run(_args, { threadId }) {
          scopes.stop((await current(threadId)).id);
          return { exitCode: 0, stdout: "Jevstice выключен.\n" };
        },
      }),
      check: cliCommand({
        summary: "Вызвать диагностику из уже запущенного сеанса провайдера",
        positionals: [{ name: "tool", description: "Имя инструмента jevstice_*" },
          { name: "json", description: "Аргументы инструмента в JSON" }],
        async run({ positionals }, { threadId, signal }) {
          const tool = positionals.tool === undefined ? undefined : tools.get(positionals.tool);
          if (!tool) throw new PluginCliError("Неизвестная проверка", { code: "unknown_check" });
          let values: unknown;
          try { values = JSON.parse(positionals.json ?? ""); }
          catch { throw new PluginCliError("Ожидаются аргументы в JSON", { code: "invalid_check_input" }); }
          const parsed = (tool.parameters as z.ZodType).safeParse(values);
          if (!parsed.success) throw new PluginCliError(parsed.error.message, { code: "invalid_check_input" });
          const result = await scopes.call(await current(threadId), tool.name, parsed.data,
            signal ?? new AbortController().signal);
          return { exitCode: Number(result.isError), stdout: result.content.map(part => part.text).join("\n") + "\n" };
        },
      }),
      status: cliCommand({
        summary: "Режим, число запросов, журнал и число недоступных наблюдений",
        async run(_args, { threadId }) {
          const result = { keyConfigured: await keyReady(), ...scopes.status(await current(threadId)) };
          return { exitCode: 0, stdout: JSON.stringify(result, null, 2) + "\n" };
        },
      }),
    },
  }));
}

export default createPlugin();

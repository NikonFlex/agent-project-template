import { cliCommand, defineCli, PluginCliError, type BbPluginApi } from "@get-bb/plugin-sdk";
import { checks, status } from "./checks.ts";
import { judge, requestJev, type Judge } from "./judge.ts";
import { openStats } from "./stats.ts";
import { registerProviderUsage } from "./provider-usage.ts";
import { registerWorkflow } from "./workflow.ts";

export function createPlugin(request: Judge = requestJev, envKey = () => process.env.TYPESAFE_API_KEY) {
  return function plugin(bb: BbPluginApi) {
    const settings = bb.settings.define({
      typesafeApiKey: { type: "string", label: "TypeSafe API key", secret: true },
    });
    const getKey = async () => (await settings.get()).typesafeApiKey?.trim() || envKey()?.trim();
    const stats = openStats(bb.storage);
    for (const [name, check] of Object.entries(checks)) {
      bb.agents.registerTool({
        name,
        description: check.description,
        parameters: check.parameters,
        presentation: { label: { pending: check.label, completed: check.label } },
        async execute(input, { threadId, signal }) {
          const key = await getKey();
          const result = key
            ? await judge(() => request(key, check, input, signal))
            : { status: status.Unavailable, reason: "missing_api_key", inputTokens: null };
          stats.record(threadId, result.status, result.inputTokens, Boolean(key));
          return { content: [{ type: "text" as const, text: JSON.stringify(result) }] };
        },
      });
    }
    registerCli(bb.cli, getKey, stats);
    registerProviderUsage(bb, stats);
    registerWorkflow(bb, getKey, stats);
  };
}

function registerCli(
  cli: BbPluginApi["cli"], getKey: () => Promise<string | undefined>, stats: ReturnType<typeof openStats>,
) {
  cli.register(defineCli({
    name: "jev",
    summary: "Статус Jev",
    commands: {
      "balance set": balanceCommand(stats),
      status: cliCommand({
        summary: "Готовность ключа, вердикты и оценка стоимости по треду и за всё время",
        async run(_input, { threadId }) {
          const key = await getKey();
          const report = { keyConfigured: Boolean(key), ...stats.report(threadId), balance: stats.balance() };
          return { exitCode: 0, stdout: JSON.stringify(report, null, 2) + "\n" };
        },
      }),
    },
  }));
}

function balanceCommand(stats: ReturnType<typeof openStats>) {
  return cliCommand({
    summary: "Записать текущий остаток из TypeSafe Billing",
    positionals: [{ name: "usd", description: "Остаток в долларах США, например 10.00", required: true }],
    async run({ positionals }) {
      const amount = Number(positionals.usd);
      if (!/^[0-9]+(?:\.[0-9]{1,2})?$/.test(positionals.usd) || !Number.isFinite(amount)) {
        throw new PluginCliError("Введите неотрицательную сумму в USD, например 10.00.", {
          code: "invalid_balance",
        });
      }
      stats.setBalance(amount);
      return { exitCode: 0, stdout: JSON.stringify(stats.balance()) + "\n" };
    },
  });
}

export default createPlugin();

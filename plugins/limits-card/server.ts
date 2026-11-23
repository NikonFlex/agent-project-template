// bb-plugin-limits-card — backend entry.
//
// Reads the aggregated usage snapshot from BB's built-in Provider usage plugin
// (it already normalizes Codex, Antigravity/Gemini/GLM, DeepSeek and Jev
// sources) and returns one flat, ordered list for the sidebar card.
import { defineRpcContract, type BbPluginApi } from "@get-bb/plugin-sdk";
import { z } from "zod";

const usageWindowSchema = z.object({
  label: z.string(),
  usedPercent: z.number(),
  resetsAt: z.string().nullable(),
});

const usageProviderSchema = z.object({
  providerId: z.string(),
  displayName: z.string(),
  usage: z.object({
    status: z.string(),
    planLabel: z.string().nullable().optional(),
    message: z.string().optional(),
    windows: z.array(usageWindowSchema).optional(),
  }),
});

const usageSnapshotSchema = z.object({
  machines: z.array(
    z.object({
      id: z.string(),
      displayName: z.string(),
      status: z.string(),
      providers: z.array(usageProviderSchema),
      error: z.string().nullable().optional(),
    }),
  ),
});

const limitRowSchema = z.object({
  id: z.string(),
  label: z.string(),
  kind: z.enum(["quota", "balance"]),
  usedPercent: z.number().nullable(),
  resetsAt: z.string().nullable(),
  value: z.string().nullable(),
  status: z.enum(["ok", "missing", "unavailable"]),
  detail: z.string().nullable(),
});

const limitsOutputSchema = z.object({
  machine: z.string().nullable(),
  refreshedAt: z.number().int().nonnegative(),
  rows: z.array(limitRowSchema),
  error: z.string().nullable(),
});

export type LimitRow = z.infer<typeof limitRowSchema>;
export type LimitsResponse = z.infer<typeof limitsOutputSchema>;

export const rpcContract = defineRpcContract({
  get_limits: {
    input: z.object({ refresh: z.boolean() }),
    output: limitsOutputSchema,
  },
});

type UsageProvider = z.infer<typeof usageProviderSchema>;
type UsageWindow = z.infer<typeof usageWindowSchema>;

const PROVIDER_IDS = [
  "codex",
  "claude-code",
  "acp-antigravity",
  "acp-cursor",
  "acp-opencode",
  "Jev",
];

const FIVE_HOUR = /(^five[- ]?hour|^current session|5\s*h(?:ours?)?\b)/i;
const WEEKLY = /(^weekly|7\s*d(?:ays?)?\b)/i;

function matches(label: string, pattern: RegExp): boolean {
  return pattern.test(label.trim());
}

function exact(label: string) {
  const wanted = label.toLowerCase();
  return (candidate: string) => candidate.trim().toLowerCase() === wanted;
}

function quotaRow(
  id: string,
  label: string,
  provider: UsageProvider | undefined,
  match: (label: string) => boolean,
): LimitRow {
  const base = {
    id,
    label,
    kind: "quota" as const,
    usedPercent: null,
    resetsAt: null,
    value: null,
    status: "missing" as const,
    detail: "No data",
  };
  if (provider === undefined) return base;
  const usage = provider.usage;
  if (usage.status !== "ok") {
    return {
      ...base,
      status: "unavailable",
      detail: usage.message ?? usage.status,
    };
  }
  const window = (usage.windows ?? []).find((candidate) =>
    match(candidate.label),
  );
  if (window === undefined) {
    return { ...base, detail: "Not reported" };
  }
  return {
    ...base,
    usedPercent: window.usedPercent,
    resetsAt: window.resetsAt,
    status: "ok",
    detail: null,
  };
}

function balanceRow(
  id: string,
  label: string,
  provider: UsageProvider | undefined,
  format: (planLabel: string | null) => string | null,
): LimitRow {
  const base = {
    id,
    label,
    kind: "balance" as const,
    usedPercent: null,
    resetsAt: null,
    value: null,
    status: "missing" as const,
    detail: "No data",
  };
  if (provider === undefined) return base;
  const usage = provider.usage;
  if (usage.status !== "ok") {
    return {
      ...base,
      status: "unavailable",
      detail: usage.message ?? usage.status,
    };
  }
  const value = format(usage.planLabel ?? null);
  if (value === null) return { ...base, detail: "No balance reported" };
  return { ...base, value, status: "ok", detail: null };
}

function deepSeekValue(planLabel: string | null): string | null {
  if (planLabel === null) return null;
  const match = /DeepSeek\s+USD\s+\$?([0-9][0-9.,]*)/i.exec(planLabel);
  if (match?.[1] !== undefined) return `$${match[1]}`;
  const trimmed = planLabel.trim();
  return trimmed === "" ? null : trimmed;
}

function jevValue(planLabel: string | null): string | null {
  if (planLabel === null) return null;
  const trimmed = planLabel.trim();
  return trimmed === "" ? null : trimmed;
}

function buildRows(
  providerById: (providerId: string) => UsageProvider | undefined,
): LimitRow[] {
  const codex = providerById("codex");
  const antigravity = providerById("acp-antigravity");
  const jev = providerById("Jev");
  return [
    // Five-hour windows first, preserving the provider order.
    quotaRow("codex-5h", "Codex 5 hours", codex, (l) => matches(l, FIVE_HOUR)),
    quotaRow("glm-5h", "GLM 5 hours", antigravity, exact("GLM 5h")),
    quotaRow("gemini-5h", "Gemini 5 hours", antigravity, exact("Gemini 5h")),
    // Then weekly windows.
    quotaRow("codex-weekly", "Codex weekly", codex, (l) => matches(l, WEEKLY)),
    quotaRow("glm-weekly", "GLM weekly", antigravity, exact("GLM 7d")),
    quotaRow("gemini-weekly", "Gemini weekly", antigravity, exact("Gemini 7d")),
    quotaRow("gemini-argon", "Gemini Argon", antigravity, exact("Gemini Argon")),
    // Balances last.
    balanceRow("deepseek", "DeepSeek", antigravity, deepSeekValue),
    balanceRow("jev", "Jev", jev, jevValue),
  ];
}

function errorMessage(cause: unknown): string {
  if (cause instanceof Error) {
    const message = cause.message.trim();
    if (message !== "") return message;
  }
  return String(cause);
}

export default function plugin(bb: BbPluginApi): void {
  bb.rpc.register(rpcContract, {
    get_limits: async ({ refresh }) => {
      let machineId: string | null = null;
      let machineName: string | null = null;
      try {
        const [hosts, config] = await Promise.all([
          bb.sdk.hosts.list(),
          bb.sdk.system.config(),
        ]);
        const connected = hosts.filter(
          (host) => host.status === "connected" && host.type !== "ephemeral",
        );
        const primary =
          connected.find((host) => host.id === config.primaryHostId) ??
          connected[0];
        if (primary !== undefined) {
          machineId = primary.id;
          machineName = primary.name;
        }
      } catch {
        // Fall back to every machine when host metadata is unavailable.
      }

      try {
        const snapshot = await bb.sdk.plugins.callRpc({
          pluginId: "bb--provider-usage",
          method: "getUsage",
          input: {
            force: refresh,
            machineIds: machineId === null ? null : [machineId],
            providerIds: PROVIDER_IDS,
            maxAgeMs: refresh ? 0 : 60_000,
          },
          outputSchema: usageSnapshotSchema,
          signal: AbortSignal.timeout(45_000),
        });
        const machine =
          snapshot.machines.find((candidate) => candidate.id === machineId) ??
          snapshot.machines.find(
            (candidate) => candidate.providers.length > 0,
          ) ??
          snapshot.machines[0];
        const providers = machine?.providers ?? [];
        const providerById = (providerId: string) =>
          providers.find((provider) => provider.providerId === providerId);
        return {
          machine: machine?.displayName ?? machineName,
          refreshedAt: Date.now(),
          rows: buildRows(providerById),
          error: machine?.error ?? null,
        };
      } catch (cause) {
        return {
          machine: machineName,
          refreshedAt: Date.now(),
          rows: buildRows(() => undefined),
          error: `Provider usage is unavailable: ${errorMessage(cause)}`,
        };
      }
    },
  });
}

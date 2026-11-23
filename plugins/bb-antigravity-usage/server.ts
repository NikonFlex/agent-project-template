import { defineRpcContract, type BbPluginApi } from "@get-bb/plugin-sdk";
import { z } from "zod";
import { usageHostContract } from "./usage-host-contract.js";

const PROVIDER_ID = "acp-antigravity";
const LIST_RESOURCES = "provider-usage.v1.listResources";
const GET_RESOURCE = "provider-usage.v1.getResource";

const rows = [
  { id: "gpt-5h", label: "GPT 5h", model: "Codex", kind: "five-hour" },
  { id: "gpt-7d", label: "GPT 7d", model: "Codex", kind: "weekly" },
  { id: "gemini-5h", label: "Gemini 5h", model: "Gemini", kind: "five-hour" },
  { id: "gemini-7d", label: "Gemini 7d", model: "Gemini", kind: "weekly" },
  { id: "claude-gpt-5h", label: "Claude and GPT 5h", model: "Claude & GPT", kind: "five-hour" },
  { id: "claude-gpt-7d", label: "Claude and GPT 7d", model: "Claude & GPT", kind: "weekly" },
  { id: "gemini-argon", label: "Gemini Argon", model: "Gemini Argon", kind: "weekly" },
] as const;

const glmRows = [
  { id: "glm-5h", label: "GLM 5h", kind: "five-hour" },
  { id: "glm-7d", label: "GLM 7d", kind: "weekly" },
] as const;

const usageWindow = z.object({
  kind: z.enum(["weekly", "five-hour", "custom"]), id: z.string().min(1), label: z.string().min(1),
  usedPercent: z.number().nonnegative(), resetsAt: z.string().nullable(), model: z.string().nullable(), cost: z.null(),
});

const usageState = z.discriminatedUnion("status", [
  z.object({
    status: z.literal("ok"), plan: z.null(), accountEmail: z.string().nullable(), planLabel: z.string().nullable(),
    windows: z.array(usageWindow),
  }),
  ...(["not_installed", "unauthenticated", "expired"] as const).map((status) => z.object({
    status: z.literal(status), plan: z.null(), accountEmail: z.null(), planLabel: z.null(),
  })),
  z.object({ status: z.literal("error"), plan: z.null(), accountEmail: z.null(), planLabel: z.null(), message: z.string() }),
]);

const contract = defineRpcContract({
  [LIST_RESOURCES]: {
    input: z.object({}),
    output: z.object({ label: z.string().min(1).optional(), resources: z.array(z.object({
      accountKey: z.string().min(1).nullable(), id: z.string().min(1), providerId: z.string().min(1), label: z.string().min(1),
      scope: z.object({ kind: z.literal("host"), hostId: z.string().min(1), hostName: z.string().min(1) }),
    })) }),
  },
  [GET_RESOURCE]: {
    input: z.object({ resourceId: z.string().min(1), refresh: z.boolean() }),
    output: z.object({ accountKey: z.string().min(1).nullable(), observedAt: z.number().int().nonnegative().nullable(), usage: usageState }),
  },
});

export default function plugin(bb: BbPluginApi): void {
  const usageHost = bb.hosts.experimental_client({ contract: usageHostContract });
  bb.rpc.register(contract, {
    [LIST_RESOURCES]: async () => ({
      resources: (await bb.sdk.hosts.list()).filter((host) => host.status === "connected").map((host) => ({
        accountKey: null, id: host.id, providerId: PROVIDER_ID, label: "Antigravity",
        scope: { kind: "host" as const, hostId: host.id, hostName: host.name },
      })),
    }),
    [GET_RESOURCE]: async ({ resourceId, refresh }) => {
      const [antigravity, codex, deepseek, zai] = await Promise.allSettled([
        usageHost.call("usage.readAntigravity", {}, { hostId: resourceId, timeoutMs: 30_000 }),
        bb.sdk.system.usageLimits({ hostId: resourceId, providerId: "codex", refresh }),
        usageHost.call("usage.readDeepSeekBalance", {}, { hostId: resourceId, timeoutMs: 10_000 }),
        usageHost.call("usage.readZaiQuota", {}, { hostId: resourceId, timeoutMs: 10_000 }),
      ]);
      const antigravityUsage = antigravity.status === "fulfilled" ? antigravity.value : undefined;
      const codexUsage = codex.status === "fulfilled" ? codex.value.codex : undefined;
      const antigravityWindows = antigravityUsage?.status === "ok" ? antigravityUsage.windows : [];
      const codexWindows = codexUsage?.status === "ok" ? codexUsage.windows : [];
      const windows = rows.flatMap<z.infer<typeof usageWindow>>((row) => {
        const window = row.model === "Codex"
          ? codexWindows.find((candidate) => candidate.kind === row.kind && !candidate.model)
          : antigravityWindows.find((candidate) => candidate.kind === row.kind && candidate.model === row.model);
        if (!window) return [];
        // Provider Usage preserves the provided label for custom windows.
        return [{
          kind: "custom", id: row.id, label: row.label, model: null,
          usedPercent: window.usedPercent, resetsAt: window.resetsAt, cost: null,
        }];
      });

      const zaiWindows = zai.status === "fulfilled" ? zai.value : [];
      for (const row of glmRows) {
        const window = zaiWindows.find((candidate) => candidate.kind === row.kind);
        if (!window) continue;
        windows.push({
          kind: "custom", id: row.id, label: row.label, model: null,
          usedPercent: window.usedPercent, resetsAt: window.resetsAt, cost: null,
        });
      }

      const balanceLabel = deepseek.status === "fulfilled" ? deepseek.value.label : null;
      const observed = windows.length > 0 || balanceLabel !== null;
      const base = { accountKey: null, observedAt: observed ? Date.now() : null };
      const details = { plan: null, accountEmail: null, planLabel: null };
      if (observed) return { ...base, usage: {
        ...details, status: "ok" as const, planLabel: balanceLabel ?? "GPT & Antigravity", windows,
      } };
      if (antigravityUsage && antigravityUsage.status !== "ok" && antigravityUsage.status !== "error") {
        return { ...base, usage: { ...details, status: antigravityUsage.status } };
      }
      return { ...base, usage: {
        ...details, status: "error" as const,
        message: antigravityUsage?.status === "error" ? antigravityUsage.message : "No quota data available.",
      } };
    },
  }, { experimental_discoverable: true, experimental_description: "Shows GPT and Antigravity quotas and the DeepSeek balance in BB Provider Usage." });
}

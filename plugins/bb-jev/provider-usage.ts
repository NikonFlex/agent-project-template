import { defineRpcContract, type BbPluginApi } from "@get-bb/plugin-sdk";
import { z } from "zod";
import type { openStats } from "./stats.ts";

const listResources = "provider-usage.v1.listResources";
const getResource = "provider-usage.v1.getResource";
const resourceId = "jev-balance";
const providerName = "Jev";
const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

const contract = defineRpcContract({
  [listResources]: {
    input: z.object({}),
    output: z.object({ resources: z.array(z.object({
      id: z.literal(resourceId),
      providerId: z.literal(providerName),
      label: z.literal(providerName),
      accountKey: z.null(),
      scope: z.object({ kind: z.literal("host"), hostId: z.string(), hostName: z.string() }),
    })) }),
  },
  [getResource]: {
    input: z.object({ resourceId: z.literal(resourceId), refresh: z.boolean() }),
    output: z.object({
      accountKey: z.null(),
      observedAt: z.number().int().nullable(),
      usage: z.object({
        status: z.literal("ok"),
        plan: z.null(),
        accountEmail: z.null(),
        planLabel: z.string(),
        windows: z.array(z.never()),
      }),
    }),
  },
});

export function registerProviderUsage(bb: BbPluginApi, stats: ReturnType<typeof openStats>) {
  bb.rpc.register(contract, {
    [listResources]: async () => {
      const { primaryHostId } = await bb.sdk.system.config();
      const host = (await bb.sdk.hosts.list()).find(({ id }) => id === primaryHostId);
      return { resources: host ? [{
        id: resourceId,
        providerId: providerName,
        label: providerName,
        accountKey: null,
        scope: { kind: "host", hostId: host.id, hostName: host.name },
      } as const] : [] };
    },
    [getResource]: () => {
      const balance = stats.balance();
      return {
        accountKey: null,
        observedAt: balance?.snapshotAt ?? null,
        usage: {
          status: "ok" as const, plan: null, accountEmail: null, windows: [],
          planLabel: balance ? `≈ ${currency.format(balance.estimatedUsd)}` : "Баланс не задан",
        },
      };
    },
  }, { experimental_discoverable: true });
}

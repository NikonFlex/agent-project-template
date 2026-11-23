import { defineRpcContract } from "@get-bb/plugin-sdk";
import { z } from "zod";

export const hostUsageState = z.discriminatedUnion("status", [
  z.object({
    status: z.literal("ok"),
    accountEmail: z.string().nullable(),
    planLabel: z.string().nullable(),
    windows: z.array(z.object({
      id: z.string().optional(),
      kind: z.enum(["weekly", "five-hour", "custom"]),
      model: z.string().nullable(),
      label: z.string(),
      usedPercent: z.number(),
      resetsAt: z.string().nullable(),
      cost: z.object({ usedUsdCents: z.number(), limitUsdCents: z.number() }).optional(),
    })),
  }),
  z.object({ status: z.literal("not_installed") }),
  z.object({ status: z.literal("unauthenticated") }),
  z.object({ status: z.literal("expired") }),
  z.object({
    status: z.literal("error"),
    message: z.string(),
    accountEmail: z.string().nullable(),
    planLabel: z.string().nullable(),
  }),
]);

export const usageHostContract = defineRpcContract({
  "usage.readAntigravity": {
    input: z.object({}),
    output: hostUsageState,
  },
  "usage.readZaiQuota": {
    input: z.object({}),
    output: z.array(z.object({
      kind: z.enum(["five-hour", "weekly"]),
      usedPercent: z.number().min(0).max(100),
      resetsAt: z.string().nullable(),
    })),
  },
  "usage.readDeepSeekBalance": {
    input: z.object({}),
    output: z.object({ label: z.string().nullable() }),
  },
});

import { defineRpcContract } from "@get-bb/plugin-sdk";
import { z } from "zod";

export const taskSchema = z.object({
  id: z.string(),
  status: z.enum(["queued", "paused", "running", "completed", "failed", "cancelled"]),
  queuePosition: z.number().int().nonnegative().nullish(),
});
export const availabilitySchema = z.object({
  canTakeNumber: z.boolean(),
  nextTakeAt: z.number().int().positive().nullish(),
});
export const desktopStatusSchema = z.object({
  support: z.discriminatedUnion("supported", [
    z.object({ supported: z.literal(true), kind: z.string() }),
    z.object({ supported: z.literal(false), reason: z.string() }),
  ]),
  quota: availabilitySchema.nullable(),
  tasks: z.array(taskSchema).max(20),
});
export const resultSchema = z.discriminatedUnion("ok", [
  taskSchema.extend({ ok: z.literal(true) }),
  z.object({ ok: z.literal(false), reason: z.string(), nextTakeAt: z.number().nullish() }),
]);
const statusSchema = z.object({
  desktop: desktopStatusSchema.nullable(),
  desktopUnavailable: z.enum(["not_configured", "unavailable"]).nullable(),
  openCode: z.object({
    offPeak: z.boolean(),
    nextChangeAt: z.string(),
  }),
});
export type Status = z.infer<typeof statusSchema>;
export const rpcContract = defineRpcContract({
  glm_status: { input: z.null(), output: statusSchema },
});

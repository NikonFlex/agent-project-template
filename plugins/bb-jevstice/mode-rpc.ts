import { defineRpcContract } from "@get-bb/plugin-sdk";
import { z } from "zod";

export const modeTag = "jevstice-mode";
export const modeState = z.enum(["off", "on", "inherited"]);
const threadId = z.string().min(1);
const checkSummary = z.object({
  tool: z.string().max(80),
  state: z.enum(["passed", "findings", "unclear", "unavailable", "completed"]),
  confidence: z.number().min(0).max(1).nullable(),
  automatic: z.boolean(),
});
export type CheckSummary = z.infer<typeof checkSummary>;
export const activityTag = (id: string) => `jevstice-activity:${id}`;

export const modeRpc = defineRpcContract({
  "jevstice.mode.list": {
    input: z.object({ threadIds: z.array(threadId).max(100) }),
    output: z.record(threadId, modeState),
    tags: () => [modeTag],
  },
  "jevstice.mode.set": {
    input: z.object({ threadId, active: z.boolean() }),
    output: modeState,
  },
  "jevstice.activity": {
    input: z.object({ threadId }),
    output: z.object({
      scope: threadId.nullable(), pending: z.boolean(), last: checkSummary.nullable(),
    }),
    tags: ({ threadId: id }, { scope }) => [modeTag, activityTag(scope ?? id)],
  },
});

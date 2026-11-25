import { defineRpcContract } from "@get-bb/plugin-sdk";
import { z } from "zod";

export const modeTag = "jevstice-mode";
export const modeState = z.enum(["off", "on", "inherited"]);
const threadId = z.string().min(1);

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
});

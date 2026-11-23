import { z } from "zod";
import { readOpenCodeKey } from "./opencode-key.js";

const quotaResponse = z.object({
  code: z.literal(200),
  success: z.literal(true),
  data: z.object({ limits: z.array(z.unknown()) }),
});

const creditLimit = z.object({
  type: z.enum(["CREDIT_LIMIT", "TOKENS_LIMIT"]),
  unit: z.number(),
  number: z.number(),
  percentage: z.number().nonnegative(),
  nextResetTime: z.number().int().positive().max(8_640_000_000_000_000).nullish(),
});

export async function readZaiQuota() {
  try {
    const key = readOpenCodeKey("zai-coding-plan");
    if (!key) return [];
    // Z.ai's usage client sends the Coding Plan key directly in Authorization.
    const response = await fetch("https://api.z.ai/api/monitor/usage/quota/limit", {
      headers: { Authorization: key, Accept: "application/json" },
      redirect: "error",
      signal: AbortSignal.timeout(7_000),
    });
    if (!response.ok) return [];

    const { data } = quotaResponse.parse(await response.json());
    return data.limits.flatMap<{ kind: "five-hour" | "weekly"; usedPercent: number; resetsAt: string | null }>((entry) => {
      const limit = creditLimit.safeParse(entry);
      if (!limit.success) return [];
      const { unit, number, percentage, nextResetTime } = limit.data;
      const kind = unit === 3 && number === 5 ? "five-hour"
        : unit === 6 && number === 1 ? "weekly" : null;
      if (!kind) return [];
      return [{
        kind,
        usedPercent: Math.min(100, percentage),
        resetsAt: nextResetTime ? new Date(nextResetTime).toISOString() : null,
      }];
    });
  } catch {
    // Other providers' quotas stay available on Z.ai connection or format errors.
    return [];
  }
}

import { homedir } from "node:os";
import { join } from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { z } from "zod";
import { Agent } from "undici";
import { experimental_defineHostEntry } from "@get-bb/plugin-sdk";
import { readZaiQuota } from "./zai-quota.js";
import { readDeepSeekBalance } from "./deepseek-balance.js";
import { readAntigravityUsage } from "./antigravity-usage.js";
import { hostUsageState, usageHostContract } from "./usage-host-contract.js";

// Google's quota endpoint rejects requests from this host over IPv6.
const ipv4Dispatcher = new Agent({ connect: { family: 4 } });
const fetchIpv4: typeof fetch = (input, init) => {
  const options = { ...init, dispatcher: ipv4Dispatcher };
  return fetch(input, options);
};

const launcher = process.env.BB_AGY_LAUNCHER || "agy";
const quotaHelper = process.env.BB_AGY_QUOTA_HELPER ||
  join(homedir(), ".bb", "local-plugins", "bb-antigravity-usage", "read-cli-quota.py");
const runFile = promisify(execFile);
const quotaBucket = z.object({
  remaining_fraction: z.number().min(0).max(1),
  reset_time: z.iso.datetime({ offset: true }).nullish(),
  disabled: z.boolean().optional(),
});

const quotaWindows = [
  { id: "gemini-5h", kind: "five-hour", model: "Gemini", label: "Gemini five-hour limit" },
  { id: "gemini-weekly", kind: "weekly", model: "Gemini", label: "Gemini weekly limit" },
  { id: "gemini-argon-wtu-weekly", kind: "weekly", model: "Gemini Argon", label: "Gemini Argon weekly limit" },
  { id: "3p-5h", kind: "five-hour", model: "Claude & GPT", label: "Claude & GPT five-hour limit" },
  { id: "3p-weekly", kind: "weekly", model: "Claude & GPT", label: "Claude & GPT weekly limit" },
] as const;

async function readCliWindows() {
  try {
    const { stdout } = await runFile("/usr/bin/python3", [quotaHelper], {
      timeout: 12_000,
      maxBuffer: 64 * 1024,
    });
    const buckets = z.record(z.string(), z.unknown()).parse(JSON.parse(stdout));
    return quotaWindows.flatMap((window) => {
      const bucket = quotaBucket.safeParse(buckets[window.id]);
      if (!bucket.success || bucket.data.disabled) return [];
      return [{
        ...window,
        usedPercent: Math.round((1 - bucket.data.remaining_fraction) * 10_000) / 100,
        resetsAt: bucket.data.reset_time ?? null,
      }];
    });
  } catch {
    return [];
  }
}

async function readUsage() {
  const windows = await readCliWindows();
  if (windows.length) {
    return hostUsageState.parse({
      status: "ok", accountEmail: null, planLabel: "Antigravity", windows,
    });
  }
  const result = await readAntigravityUsage({ agyCli: launcher, fetcher: fetchIpv4 });
  return hostUsageState.parse(result.usage);
}

let pending: ReturnType<typeof readUsage> | undefined;

export default experimental_defineHostEntry({
  contract: usageHostContract,
  handlers: {
    "usage.readZaiQuota": readZaiQuota,
    async "usage.readDeepSeekBalance"() {
      return { label: await readDeepSeekBalance() };
    },
    "usage.readAntigravity"() {
      pending ??= readUsage().finally(() => { pending = undefined; });
      return pending;
    },
  },
});

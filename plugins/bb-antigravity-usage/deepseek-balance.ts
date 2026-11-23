import { z } from "zod";
import { readOpenCodeKey } from "./opencode-key.js";

const balanceResponse = z.object({
  is_available: z.boolean(),
  balance_infos: z.array(z.object({
    currency: z.enum(["USD", "CNY"]),
    total_balance: z.string().regex(/^-?\d+(?:\.\d+)?$/).transform(Number).pipe(z.number()),
  })),
});

export async function readDeepSeekBalance(): Promise<string | null> {
  try {
    const key = readOpenCodeKey("deepseek");
    if (!key) return null;
    const response = await fetch("https://api.deepseek.com/user/balance", {
      headers: { Authorization: `Bearer ${key}` },
      redirect: "error",
      signal: AbortSignal.timeout(7_000),
    });
    if (!response.ok) return "DeepSeek balance unavailable";
    const balance = balanceResponse.parse(await response.json());
    const amounts = balance.balance_infos.map(({ currency, total_balance }) =>
      new Intl.NumberFormat("en-US", {
        style: "currency",
        currency,
        currencyDisplay: "code",
      }).format(total_balance),
    );
    if (!amounts.length) return "DeepSeek balance unavailable";
    return `DeepSeek ${amounts.join(" / ")}${balance.is_available ? "" : " · API unavailable"}`;
  } catch (error) {
    // A machine without OpenCode should keep the original quotas card.
    if (error && typeof error === "object" && "errcode" in error && error.errcode === 14) return null;
    return "DeepSeek balance unavailable";
  }
}

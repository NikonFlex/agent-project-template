import type { BbPluginApi } from "@get-bb/plugin-sdk";
import { status, type Verdict } from "./checks.ts";

const pricePerMillionInputTokensUsd = 0.042;
const tokensPerMillion = 1_000_000;

type Count = { verdict: Verdict; calls: number; input_tokens: number; without_usage: number };
type BalanceSnapshot = { amount_usd: number; input_tokens: number; observed_at: number };

function costUsd(inputTokens: number) {
  return inputTokens * pricePerMillionInputTokensUsd / tokensPerMillion;
}

function summarize(rows: Count[]) {
  const verdicts: Record<Verdict, number> = {
    [status.Passed]: 0, [status.Failed]: 0, [status.Inconclusive]: 0, [status.Unavailable]: 0,
  };
  let calls = 0;
  let inputTokens = 0;
  let requestsWithoutUsage = 0;
  for (const row of rows) {
    verdicts[row.verdict] = row.calls;
    calls += row.calls;
    inputTokens += row.input_tokens;
    requestsWithoutUsage += row.without_usage;
  }
  const estimatedCostUsd = costUsd(inputTokens);
  return { calls, verdicts, inputTokens, requestsWithoutUsage, estimatedCostUsd };
}

export function openStats(storage: BbPluginApi["storage"]) {
  const db = storage.database();
  storage.migrate(db, [`CREATE TABLE jev_counts (
    scope TEXT NOT NULL, verdict TEXT NOT NULL, calls INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL, without_usage INTEGER NOT NULL,
    PRIMARY KEY (scope, verdict)
  )`, `CREATE TABLE jev_balance (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    amount_usd REAL NOT NULL,
    input_tokens INTEGER NOT NULL,
    observed_at INTEGER NOT NULL
  )`]);
  const add = db.prepare(`INSERT INTO jev_counts (scope, verdict, calls, input_tokens, without_usage)
    VALUES (?, ?, 1, ?, ?) ON CONFLICT (scope, verdict) DO UPDATE SET
    calls = calls + 1, input_tokens = input_tokens + excluded.input_tokens,
    without_usage = without_usage + excluded.without_usage`);
  const read = db.prepare<[string], Count>(
    "SELECT verdict, calls, input_tokens, without_usage FROM jev_counts WHERE scope = ?",
  );
  const readTotal = () => summarize(read.all("total"));
  const readBalance = db.prepare<[], BalanceSnapshot>(
    "SELECT amount_usd, input_tokens, observed_at FROM jev_balance WHERE id = 1",
  );
  const saveBalance = db.prepare(`INSERT INTO jev_balance (id, amount_usd, input_tokens, observed_at)
    VALUES (1, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET amount_usd = excluded.amount_usd,
    input_tokens = excluded.input_tokens, observed_at = excluded.observed_at`);
  const record = db.transaction((threadId: string, verdict: Verdict, tokens: number | null, sent: boolean) => {
    const withoutUsage = sent && tokens === null ? 1 : 0;
    add.run("total", verdict, tokens ?? 0, withoutUsage);
    add.run(`thread:${threadId}`, verdict, tokens ?? 0, withoutUsage);
  });
  return {
    record,
    setBalance: db.transaction((amountUsd: number) => {
      saveBalance.run(amountUsd, readTotal().inputTokens, Date.now());
    }),
    balance() {
      const snapshot = readBalance.get();
      if (!snapshot) return null;
      const spentUsd = costUsd(readTotal().inputTokens - snapshot.input_tokens);
      return {
        snapshotUsd: snapshot.amount_usd,
        snapshotAt: snapshot.observed_at,
        estimatedUsd: Math.max(0, snapshot.amount_usd - spentUsd),
      };
    },
    report(threadId?: string) {
      return {
        session: threadId ? summarize(read.all(`thread:${threadId}`)) : null,
        total: readTotal(),
        pricePerMillionInputTokensUsd,
      };
    },
  };
}

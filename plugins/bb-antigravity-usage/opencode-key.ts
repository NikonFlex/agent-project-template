import { homedir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { z } from "zod";

const credentialSchema = z.object({
  type: z.literal("key"),
  key: z.string().min(1),
});

export function readOpenCodeKey(integrationId: "deepseek" | "zai-coding-plan"): string | undefined {
  const database = new DatabaseSync(
    process.env.OPENCODE_DB || join(process.env.XDG_DATA_HOME || join(homedir(), ".local", "share"), "opencode", "opencode.db"),
    { readOnly: true },
  );
  try {
    const row = database.prepare(
      "SELECT value FROM credential WHERE integration_id = ? AND active = 1 ORDER BY time_updated DESC LIMIT 1",
    ).get(integrationId);
    if (typeof row?.value !== "string") return undefined;
    return credentialSchema.parse(JSON.parse(row.value)).key;
  } finally {
    database.close();
  }
}


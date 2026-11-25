import { createHash } from "node:crypto";
import type { BbPluginApi } from "@get-bb/plugin-sdk";
import type { ContextArchive, ContextSource } from "jevstice/src/context.js";
import { summarizeActivity } from "./activity.ts";

function digest(text: string) {
  return createHash("sha256").update(text).digest("hex");
}

export function openJournal(storage: BbPluginApi["storage"]) {
  const db = storage.database();
  storage.migrate(db, [
    "CREATE TABLE diagnostics (scope TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL)",
    "CREATE INDEX diagnostics_scope ON diagnostics (scope)",
    "CREATE TABLE sources (scope TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(scope, id))",
  ]);
  const append = db.prepare("INSERT INTO diagnostics(scope, kind, value) VALUES (?, ?, ?)");
  const latest = db.prepare("SELECT kind, value FROM diagnostics WHERE scope = ? ORDER BY rowid DESC LIMIT 50");
  const latestActivity = db.prepare(`
    SELECT kind, value FROM diagnostics WHERE scope = ? AND (
      kind IN ('check', 'jev.acceptance', 'jev.diagnostic') OR
      (kind = 'not_checked' AND json_extract(value, '$.automatic') = 1)
    ) ORDER BY rowid DESC LIMIT 1`);
  const insert = db.prepare("INSERT OR IGNORE INTO sources(scope, id, value) VALUES (?, ?, ?)");
  const get = db.prepare("SELECT value FROM sources WHERE scope = ? AND id = ?");
  const list = db.prepare("SELECT value FROM sources WHERE scope = ? ORDER BY rowid");

  return {
    note(scope: string, kind: string, value: unknown) {
      append.run(scope, kind, JSON.stringify(value));
    },
    recent(scope: string) {
      return (latest.all(scope) as { kind: string; value: string }[])
        .map(row => ({ kind: row.kind, value: JSON.parse(row.value) as unknown }));
    },
    activity(scope: string) {
      const row = latestActivity.get(scope) as { kind: string; value: string } | undefined;
      return row ? summarizeActivity(row.kind, JSON.parse(row.value)) : null;
    },
    archive(scope: string): ContextArchive {
      return {
        put(input) {
          const checksum = digest(input.content);
          const entry = { ...input, digest: checksum, id: digest(JSON.stringify(input)) };
          insert.run(scope, entry.id, JSON.stringify(entry));
          return entry;
        },
        list() {
          return (list.all(scope) as { value: string }[])
            .map(row => JSON.parse(row.value) as ContextSource);
        },
        read(id) {
          const row = get.get(scope, id) as { value: string } | undefined;
          if (!row) throw new Error("Этот источник отсутствует в истории текущего треда");
          const parsed = JSON.parse(row.value) as ContextSource;
          if (digest(parsed.content) !== parsed.digest) throw new Error("Источник повреждён");
          return parsed;
        },
      };
    },
  };
}

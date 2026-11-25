type RecordValue = Record<string, unknown>;

export function record(value: unknown): RecordValue | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as RecordValue : undefined;
}

const MAX_CHARS = 16_000;
const sensitive = /(?:^|[/\\\s])\.env(?:[.\s/\\]|$)|\b(?:id_rsa|id_ed25519|credentials\.json)\b|-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:ghp_|github_pat_|sk-)[a-z0-9_-]{16,}|\b(?:password|passwd|token|api[_-]?key|secret)\s*[:=]\s*\S{6,}/iu;
const shellSecrets = /\b(?:printenv|env|set)\s*(?:$|[|;]|[^\n]*\b(?:key|token|secret|pass)\b)/iu;

export function boundedEvidence(value: unknown, limit = MAX_CHARS): string | null {
  return typeof value === "string" && value !== "" && value.length <= limit &&
    !sensitive.test(value) ? value : null;
}

export function ownerText(data: unknown): string | null {
  const entry = record(data);
  if (entry?.initiator !== "user" || !Array.isArray(entry.input)) return null;
  return boundedEvidence(entry.input.flatMap((part: unknown) => {
    const block = record(part);
    return block?.type === "text" && typeof block.text === "string" ? [block.text] : [];
  }).join("\n"));
}

export type Observation = {
  toolName: "bash" | "edit" | "read" | "web_search";
  input: RecordValue;
  content: { type: "text"; text: string }[];
  isError: boolean;
};
type Observations = { items: Observation[]; unavailable: number };

function observation(
  toolName: Observation["toolName"], input: RecordValue, content: string, isError = false,
): Observation {
  return { toolName, input, content: [{ type: "text", text: content }], isError };
}

function command(item: RecordValue): Observations {
  const cmd = boundedEvidence(item.command);
  const output = item.aggregatedOutput === "" ? "" : boundedEvidence(item.aggregatedOutput);
  if (!cmd || output === null || shellSecrets.test(cmd) || typeof item.exitCode !== "number" ||
      (item.status !== "completed" && item.status !== "failed")) {
    return { items: [], unavailable: 1 };
  }
  return {
    items: [observation("bash", { command: cmd }, `exit code: ${item.exitCode}\n${output}`,
      item.exitCode !== 0)],
    unavailable: 0,
  };
}

function edits(item: RecordValue): Observations {
  const changes = Array.isArray(item.changes) ? item.changes : [];
  const items = changes.flatMap((change: unknown) => {
    const patch = record(change);
    const path = boundedEvidence(patch?.path);
    const diff = boundedEvidence(patch?.diff);
    if (!path || !diff || path.replaceAll("\\", "/").includes("/tmp/rundo/") ||
        path.startsWith("tmp/rundo/")) return [];
    return [observation("edit", { path, input: diff }, diff, item.status !== "completed")];
  });
  return { items, unavailable: Math.max(1, changes.length) - items.length };
}

export function observedTools(data: unknown): Observations {
  const item = record(record(data)?.item);
  if (item?.type === "commandExecution") return command(item);
  if (item?.type === "fileChange") return edits(item);
  if (item?.type === "fileRead" || item?.type === "webSearch") {
    const locator = boundedEvidence(item.path ?? item.url);
    const output = boundedEvidence(item.content ?? item.text);
    if (!locator || !output || item.status !== "completed") return { items: [], unavailable: 1 };
    return {
      items: [item.type === "fileRead" ? observation("read", { path: locator }, output)
        : observation("read", { url: locator }, output)],
      unavailable: 0,
    };
  }
  return { items: [], unavailable: ["toolCall", "mcpToolCall"].includes(String(item?.type)) ? 1 : 0 };
}

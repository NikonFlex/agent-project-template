import { record } from "./observations.ts";
import type { CheckSummary } from "./mode-rpc.ts";

function resultState(kind: string, entry: Record<string, unknown>, details: Record<string, unknown> | undefined) {
  const verdict = details?.verdict;
  if (kind === "not_checked" || ["unavailable", "unusable", "stale"].includes(String(details?.kind)) ||
      details?.skipped === true || details?.prerequisite === true) return "unavailable";
  if (verdict === "sound" || verdict === "passed" || details?.approved === true) return "passed";
  if (details?.kind === "defect" || verdict === "defect" || verdict === "failed") return "findings";
  if (details?.kind === "unclear" || details?.approved === false || verdict === "inconclusive") return "unclear";
  if (entry.isError === true) return "unavailable";
  return "completed";
}

export function summarizeActivity(kind: string, value: unknown): CheckSummary | null {
  const entry = record(value);
  if (!entry) return null;
  const background = kind === "jev.acceptance" || kind === "jev.diagnostic";
  if (!background && kind !== "check" && !(kind === "not_checked" && entry.automatic === true)) return null;
  const outcome = record(entry.outcome);
  const details = record(background ? outcome?.details : entry.details);
  const tool = background ? (kind === "jev.acceptance" ? "jev_acceptance" : "jev_diagnostic") : entry.name;
  if (typeof tool !== "string") return null;
  const confidence = details?.confidence;
  return {
    tool,
    state: resultState(kind, entry, details),
    confidence: typeof confidence === "number" && confidence >= 0 && confidence <= 1 ? confidence : null,
    automatic: background || entry.automatic === true,
  };
}

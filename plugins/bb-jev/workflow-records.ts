import { createHash, randomUUID } from "node:crypto";
import type { BbPluginApi } from "@get-bb/plugin-sdk";
import { z } from "zod";
import { decision, judgeReason } from "./judge.ts";
import { status } from "./checks.ts";

export const stage = { Requirements: "requirements", Plan: "plan", Acceptance: "acceptance" } as const;
export const freshness = { Current: "current", Stale: "stale", Unverified: "unverified" } as const;
export const refusal = {
  NotFound: "review_not_found",
  WrongStage: "wrong_review_stage",
  Coverage: "invalid_plan_coverage",
} as const;

const maximumItems = 12;
const requirementId = z.string().regex(/^R[1-9]\d?$/);
const evidence = z.string().trim().min(1).max(12_000);
const itemText = evidence.max(1_000);
const requirement = z.object({ id: requirementId, text: itemText });
const step = z.object({ text: itemText, requirementIds: z.array(requirementId).min(1).max(maximumItems) });
const requirements = z.array(requirement).min(1).max(maximumItems)
  .refine((items) => new Set(items.map(({ id }) => id)).size === items.length, "Requirement IDs must be unique");

const receiptId = z.uuid();
const revisionId = z.string().regex(/^(?:[a-f0-9]{40}|[a-f0-9]{64})$/);
const threadId = z.string().min(1).max(128);
export const requirementsInput = z.object({ request: evidence, requirements });
export const planInput = z.object({ requirementsReviewId: receiptId, steps: z.array(step).min(1).max(maximumItems) });
export const acceptanceInput = z.object({ planReviewId: receiptId, revision: revisionId, diff: evidence, testOutput: evidence });
export const lookupInput = z.object({
  reviewId: receiptId, revision: revisionId.optional(), threadId: threadId.optional(),
});

export const receipt = z.object({
  reviewId: receiptId,
  threadId,
  parentReviewId: receiptId.nullable(),
  stage: z.enum(stage),
  status: z.enum(status),
  reason: z.enum(judgeReason).nullable(),
  model: z.string().nullable(),
  decisions: z.record(z.string(), decision),
  inputTokens: z.number().int().nonnegative().nullable(),
  durationMs: z.number().int().nonnegative(),
  revision: revisionId.nullable(),
  evidenceDigest: z.hash("sha256"),
});

const artifact = z.discriminatedUnion("stage", [
  z.object({ stage: z.literal(stage.Requirements), input: requirementsInput }),
  z.object({ stage: z.literal(stage.Plan), input: planInput }),
  z.object({ stage: z.literal(stage.Acceptance), input: acceptanceInput.pick({ revision: true, planReviewId: true }) }),
]);

type Artifact = z.infer<typeof artifact>;
type Receipt = z.infer<typeof receipt>;
const record = z.object({ receipt, artifact });
export type Review = z.infer<typeof record>;

type KeyValue = BbPluginApi["storage"]["kv"];

export function openReviews(kv: KeyValue) {
  const key = (reviewId: string) => `reviews:${reviewId}`;
  return {
    async get(reviewId: string) {
      const stored = await kv.get(key(reviewId));
      return stored === undefined ? null : record.parse(stored);
    },
    async save(threadId: string, artifact: Artifact, details: Omit<Receipt, "stage" | "reviewId" | "threadId">) {
      const stored = receipt.parse({ ...details, threadId, stage: artifact.stage, reviewId: randomUUID() });
      await kv.set(key(stored.reviewId), { receipt: stored, artifact });
      return stored;
    },
  };
}

export function evidenceDigest(input: unknown) {
  return createHash("sha256").update(JSON.stringify(input)).digest("hex");
}

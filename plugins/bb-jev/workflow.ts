import {
  defineRpcContract, type BbPluginApi, type PluginAgentToolContext,
} from "@get-bb/plugin-sdk";
import { z } from "zod";
import { status } from "./checks.ts";
import { judgeBatch, judgeReason, requestJevBatch, type Question } from "./judge.ts";
import type { openStats } from "./stats.ts";
import {
  acceptanceInput, evidenceDigest, freshness, lookupInput, openReviews, planInput,
  receipt, refusal, requirementsInput, stage, type Review,
} from "./workflow-records.ts";

const requirementsQuestion: Question = {
  question: "Do the proposed requirements faithfully cover the user's request?",
  criteria: {
    passed: "All material requests are covered and every proposed requirement follows from the request.",
    failed: "A material request is omitted or a proposed requirement conflicts with or adds to the request.",
    inconclusive: "The request or proposed requirements are ambiguous enough to prevent a decision.",
  },
};
const planQuestion: Question = {
  question: "Do the steps and their requirement links form a feasible plan with verifiable outcomes?",
  criteria: {
    passed: "The links are meaningful and the steps include feasible implementation and verification.",
    failed: "At least one link, step or verification is inadequate for a requirement.",
    inconclusive: "The information is too sparse to judge a material step.",
  },
};
const acceptanceCriteria: Question["criteria"] = {
  passed: "The change and observed execution jointly establish that this requirement is met.",
  failed: "The supplied change or execution demonstrates a violation of this requirement.",
  inconclusive: "The material lacks decisive execution evidence for this requirement.",
};

type Audience = Pick<PluginAgentToolContext, "threadId" | "signal">;
type ReviewStore = ReturnType<typeof openReviews>;
type Invoke = ReturnType<typeof createInvoker>;

function createInvoker(
  getKey: () => Promise<string | undefined>, stats: ReturnType<typeof openStats>,
) {
  return async (ctx: Audience, questions: Record<string, Question>, input: unknown) => {
    const key = await getKey();
    const start = performance.now();
    const result = key
      ? await judgeBatch(() => requestJevBatch(key, questions, input, ctx.signal), Object.keys(questions))
      : { status: status.Unavailable, reason: judgeReason.MissingKey, inputTokens: null };
    const durationMs = Math.round(performance.now() - start);
    stats.record(ctx.threadId, result.status, result.inputTokens, Boolean(key));
    return {
      status: result.status, inputTokens: result.inputTokens, durationMs,
      ...(result.status === status.Unavailable
        ? { reason: result.reason, model: null, decisions: {} }
        : { reason: null, model: result.model, decisions: result.decisions }),
      evidenceDigest: evidenceDigest(input),
    };
  };
}

function textResult(result: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(result) }] };
}

function reviewAtStage<S extends Review["artifact"]["stage"]>(
  review: Review | null, expected: S,
): { artifact: Extract<Review["artifact"], { stage: S }>; receipt: Review["receipt"] }
  | { error: typeof refusal[keyof typeof refusal] };
function reviewAtStage(review: Review | null, expected: Review["artifact"]["stage"]) {
  if (!review) return { error: refusal.NotFound };
  if (review.artifact.stage !== expected) return { error: refusal.WrongStage };
  return review;
}

async function reviewedRequirements(reviews: ReviewStore, reviewId: string) {
  const review = reviewAtStage(await reviews.get(reviewId), stage.Requirements);
  return "error" in review ? review : review.artifact.input.requirements;
}

function checkPlanCoverage(requirements: z.infer<typeof requirementsInput>["requirements"], plan: z.infer<typeof planInput>) {
  const ids = new Set(requirements.map(({ id }) => id));
  const linked = new Set(plan.steps.flatMap(({ requirementIds }) => requirementIds));
  return {
    unknown: [...linked].filter((id) => !ids.has(id)),
    missing: [...ids].filter((id) => !linked.has(id)),
  };
}

function registerRequirements(bb: BbPluginApi, reviews: ReviewStore, invoke: Invoke) {
  bb.agents.registerTool({
    name: "jev_review_requirements",
    description: "Сверяет требования с исходной постановкой и сохраняет результат для плана.",
    presentation: { label: { pending: "Сверка требований Jev", completed: "Сверка требований Jev" } },
    parameters: requirementsInput,
    async execute(input, ctx) {
      const result = await invoke(ctx, { coverage: requirementsQuestion }, input);
      return textResult(await reviews.save(ctx.threadId, { stage: stage.Requirements, input }, {
        ...result, parentReviewId: null, revision: null,
      }));
    },
  });
}

function registerPlan(bb: BbPluginApi, reviews: ReviewStore, invoke: Invoke) {
  bb.agents.registerTool({
    name: "jev_review_plan",
    description: "Сверяет шаги плана с требованиями по ID предыдущей проверки.",
    presentation: { label: { pending: "Сверка плана Jev", completed: "Сверка плана Jev" } },
    parameters: planInput,
    async execute(input, ctx) {
      const requirements = await reviewedRequirements(reviews, input.requirementsReviewId);
      if ("error" in requirements) return textResult(requirements);
      const coverage = checkPlanCoverage(requirements, input);
      if (coverage.unknown.length || coverage.missing.length) {
        return textResult({ error: refusal.Coverage, ...coverage });
      }
      const result = await invoke(ctx, { plan: planQuestion }, { requirements, steps: input.steps });
      return textResult(await reviews.save(ctx.threadId, { stage: stage.Plan, input }, {
        ...result, parentReviewId: input.requirementsReviewId, revision: null,
      }));
    },
  });
}

function registerAcceptance(bb: BbPluginApi, reviews: ReviewStore, invoke: Invoke) {
  bb.agents.registerTool({
    name: "jev_review_acceptance",
    description: "Оценивает каждое требование по фактическому диффу и выводам проверок, привязанным к ревизии.",
    presentation: { label: { pending: "Приёмка по требованиям Jev", completed: "Приёмка по требованиям Jev" } },
    parameters: acceptanceInput,
    async execute(input, ctx) {
      const plan = reviewAtStage(await reviews.get(input.planReviewId), stage.Plan);
      if ("error" in plan) return textResult(plan);
      const requirements = await reviewedRequirements(reviews, plan.artifact.input.requirementsReviewId);
      if ("error" in requirements) return textResult(requirements);
      const questions = Object.fromEntries(requirements.map(({ id }) => [
        id, { question: `Does the observed evidence establish requirement ${id}?`, criteria: acceptanceCriteria },
      ]));
      const result = await invoke(ctx, questions, { ...input, requirements, steps: plan.artifact.input.steps });
      const { revision, planReviewId } = input;
      return textResult(await reviews.save(ctx.threadId, { stage: stage.Acceptance, input: { revision, planReviewId } }, {
        ...result, parentReviewId: planReviewId, revision,
      }));
    },
  });
}

const reportMethod = "jev.workflow.receipt";
const reportContract = defineRpcContract({
  [reportMethod]: {
    input: lookupInput,
    output: z.union([
      receipt.extend({ freshness: z.enum(freshness).nullable() }),
      z.object({ error: z.literal(refusal.NotFound) }),
    ]),
  },
});

function registerLookup(bb: BbPluginApi, reviews: ReviewStore) {
  const report = async (input: z.infer<typeof lookupInput>) => {
    const stored = await reviews.get(input.reviewId);
    if (!stored) return { error: refusal.NotFound };
    const expected = stored.receipt.revision;
    let compared: typeof freshness[keyof typeof freshness] | null = null;
    if (expected !== null) {
      compared = input.revision === undefined ? freshness.Unverified
        : expected === input.revision ? freshness.Current : freshness.Stale;
    }
    return { ...stored.receipt, freshness: compared };
  };
  bb.rpc.register(reportContract, {
    [reportMethod]: report,
  }, { experimental_discoverable: true });
  bb.agents.registerTool({
    name: "jev_review_status",
    description: "Возвращает запись проверки и сверяет ревизию приёмки с текущим снимком.",
    parameters: lookupInput,
    async execute(input) {
      return textResult(await report(input));
    },
  });
}

export function registerWorkflow(bb: BbPluginApi, getKey: () => Promise<string | undefined>, stats: ReturnType<typeof openStats>) {
  const reviews = openReviews(bb.storage.kv);
  const invoke = createInvoker(getKey, stats);
  registerRequirements(bb, reviews, invoke);
  registerPlan(bb, reviews, invoke);
  registerAcceptance(bb, reviews, invoke);
  registerLookup(bb, reviews);
}

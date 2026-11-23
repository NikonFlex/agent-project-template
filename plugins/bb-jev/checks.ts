import { z } from "zod";

export const status = {
  Passed: "passed",
  Failed: "failed",
  Inconclusive: "inconclusive",
  Unavailable: "unavailable",
} as const;

export type Verdict = typeof status[keyof typeof status];
const evidence = z.string().trim().min(1).max(12_000);

export const checks = {
  jev_check_plan: {
    label: "Проверка плана",
    description: "Сверяет план с требованиями до правок.",
    parameters: z.object({ requirements: evidence, plan: evidence }),
    question: "Does the plan cover the requirements with feasible steps and verification?",
    criteria: {
      [status.Passed]: "The plan covers each requirement and explains how to verify it.",
      [status.Failed]: "The plan omits or contradicts a requirement or relies on an infeasible step.",
      [status.Inconclusive]: "The requirements or plan lack the details needed to judge coverage.",
    },
  },
  jev_acceptance: {
    label: "Проверка приёмки",
    description: "Сверяет критерии готовности с диффом и фактическим выводом проверок.",
    parameters: z.object({ criteria: evidence, diff: evidence, testOutput: evidence }),
    question: "Do the actual changes and observed results satisfy all acceptance criteria?",
    criteria: {
      [status.Passed]: "The change and observed results jointly demonstrate every criterion.",
      [status.Failed]: "The diff or test output shows an unmet criterion or a relevant regression.",
      [status.Inconclusive]: "Some criterion lacks relevant execution evidence or the evidence is ambiguous.",
    },
  },
  jev_fact_check: {
    label: "Проверка утверждения",
    description: "Сверяет одно утверждение с цитатами из прочитанных источников.",
    parameters: z.object({
      claim: evidence,
      sources: z.array(z.object({ reference: z.string().trim().min(1).max(500), quote: evidence })).min(1).max(4),
    }),
    question: "Do the quoted passages establish or contradict the entire claim?",
    criteria: {
      [status.Passed]: "The excerpts support the claim, including the relevant entity, units and conditions.",
      [status.Failed]: "The excerpts directly contradict a material part of the claim.",
      [status.Inconclusive]: "The excerpts are insufficient, conflict, or lack needed context.",
    },
  },
} as const;

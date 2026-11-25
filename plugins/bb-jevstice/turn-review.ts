import { boundedEvidence, type Observation } from "./observations.ts";

const verification = /\b(?:pytest|vitest|jest|cargo test|go test|npm (?:run )?test|bun test|tools\/check\.sh)\b/u;
const scratchPath = /(?:^|\/)tmp\/(?:rundo|welta-pipeline|jevstice)\//u;
const maxDiffs = 3;
const quoteSize = 2300;

function excerpt(text: string) {
  return text.length > quoteSize ? text.slice(0, quoteSize) + "\n[… фрагмент сокращён]" : text;
}

export function createTurnReview(request: string) {
  const changes = new Map<string, string>();
  let result: { kind: "execution"; source: string; quote: string } | undefined;
  return {
    observe(observation: Observation) {
      const path = boundedEvidence(observation.input.path);
      const text = observation.content[0]?.text;
      const command = boundedEvidence(observation.input.command);
      if (!text) return;
      if (observation.toolName === "edit" && !observation.isError && path &&
          !scratchPath.test(path.replaceAll("\\", "/"))) {
        changes.delete(path);
        changes.set(path, excerpt(text));
        if (changes.size > maxDiffs) changes.delete(changes.keys().next().value!);
        result = undefined;
      } else if (observation.toolName === "bash" && command && changes.size && verification.test(command)) {
        result = { kind: "execution", source: excerpt(command), quote: excerpt(text) };
      }
    },
    clear() { changes.clear(); result = undefined; },
    material() {
      if (!changes.size) return null;
      return {
        kind: "diff",
        target: "Наблюдаемые правки текущего хода BB",
        claim: "Проверь, соответствует ли показанный дифф постановке. Учитывай, что BB передаёт только наблюдаемые фрагменты.",
        evidence: [
          { kind: "user", quote: excerpt(request) },
          ...Array.from(changes, ([source, quote]) => ({ kind: "code", source: excerpt(source), quote })),
          ...(result ? [result] : []),
        ],
      };
    },
  };
}

export type TurnReview = ReturnType<typeof createTurnReview>;

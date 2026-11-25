import { createJevController as createUpstreamController } from "jevstice/src/controller.js";

export const createJevController: (
  options: Parameters<typeof createUpstreamController>[0] & {
    readSessionArtifact: (ctx: unknown, name: string, maxBytes?: number) => Promise<string | null>;
  },
) => ReturnType<typeof createUpstreamController>;
export { createTypeSafeJudge } from "jevstice/src/judge.js";
export { DEFAULT_CONFIG } from "jevstice/src/config.js";
export { createToolCatalog } from "jevstice/src/guidance.js";

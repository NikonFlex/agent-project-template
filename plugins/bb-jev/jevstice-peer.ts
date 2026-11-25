import { defineRpcContract, type BbPluginApi } from "@get-bb/plugin-sdk";
import { z } from "zod";
import { createJevClient, jevModel, usageMetadata } from "./judge.ts";

const instructions = z.string().optional();
const description = z.string().nullable();
const question = z.discriminatedUnion("type", [
  z.object({ type: z.literal("choice"), instructions, criteria: z.record(z.string(), description) }),
  z.object({ type: z.literal("score"), instructions, criteria: z.tuple([description, description], description) }),
  z.object({ type: z.literal("noul"), instructions,
    criteria: z.object({ true: description.optional(), false: description.optional() }).nullable().optional() }),
]);
const request = z.object({
  state: z.string().min(1).max(96_000),
  questions: z.record(z.string(), question).refine(value => {
    const count = Object.keys(value).length;
    return count > 0 && count <= 200 && JSON.stringify(value).length <= 128_000;
  }),
});
const bridge = defineRpcContract({
  "jev.judge.available": { input: z.object({}), output: z.object({ configured: z.boolean() }) },
  "jev.judge.system-one": { input: request, output: z.json() },
});

function requireJevstice(caller: { kind: "client" } | { kind: "plugin"; pluginId: string }) {
  if (caller.kind !== "plugin" || caller.pluginId !== "bb-jevstice") {
    throw new Error("RPC доступен только загруженному плагину bb-jevstice");
  }
}

type Request = (
  key: string, input: z.infer<typeof request>, signal: AbortSignal,
) => Promise<z.infer<ReturnType<typeof z.json>>>;
const requestSystemOne: Request = async (key, input, signal) =>
  z.json().parse(await createJevClient(key).systemOne({ model: jevModel, ...input }, { signal }));

export function registerJevsticePeer(
  bb: BbPluginApi, dependencies: {
    getKey: () => Promise<string | undefined>;
    record: (tokens: number | null) => void;
  }, send: Request = requestSystemOne,
) {
  const { getKey, record } = dependencies;
  bb.rpc.register(bridge, {
    async "jev.judge.available"(_input, context) {
      requireJevstice(context.experimental_caller);
      return { configured: Boolean(await getKey()) };
    },
    async "jev.judge.system-one"(input, context) {
      requireJevstice(context.experimental_caller);
      const key = await getKey();
      if (!key) throw new Error("TypeSafe API key не настроен для bb-jev");
      let tokens = null;
      try {
        const result = await send(key, input, context.experimental_signal);
        tokens = usageMetadata.safeParse(result).data?.usage?.input_tokens ?? null;
        return result;
      } finally {
        record(tokens);
      }
    },
  });
}

import { experimental_Icon as Icon, experimental_useRpcQuery, useComposer } from "@get-bb/plugin-sdk/app";
import { modeRpc, type CheckSummary } from "./mode-rpc.ts";

const operations: Record<string, string> = {
  jev_review: "Ревью правок",
  jev_requirements: "Проверка требований",
  jev_plan_review: "Проверка плана",
  jev_acceptance: "Приёмка",
  jev_fact_check: "Факт-чек",
};
const outcomes: Record<CheckSummary["state"], string> = {
  passed: "Без замечаний",
  findings: "Есть замечания",
  unclear: "Недостаточно доказательств",
  unavailable: "Проверка недоступна",
  completed: "Проверка завершена",
};
const percent = new Intl.NumberFormat("ru", { style: "percent" });

export function JevActivity() {
  const { scope } = useComposer();
  const threadId = scope.kind === "thread" ? scope.threadId : "";
  const { data } = experimental_useRpcQuery({
    contract: modeRpc, method: "jevstice.activity", input: { threadId }, enabled: Boolean(threadId),
  });
  if (!data?.scope) return null;
  const { last, pending } = data;
  const description = pending ? "Судья проверяет…" : last ? outcomes[last.state] : "Ждёт материала для проверки";
  return (
    <div role="status" className="flex min-h-6 items-center gap-2 px-2 py-1 text-xs text-subtle-foreground">
      <Icon name={pending ? "LoaderCircle" : "ClipboardCheck"}
        className={pending ? "size-4 shrink-0 animate-spin" : "size-4 shrink-0"} />
      <span className="font-medium text-foreground">Jev</span>
      <span>
        {last && !pending && operations[last.tool] && `${operations[last.tool]} · `}
        {description}
        {last?.confidence != null && !pending && ` · ${percent.format(last.confidence)}`}
      </span>
    </div>
  );
}

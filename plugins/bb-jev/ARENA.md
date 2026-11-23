# Проверка Jev в Арене

Для варианта с Jev Арена последовательно вызывает три инструмента. В связке из двух тредов планировщик проверяет требования и план, затем передаёт `planReviewId` кодеру для приёмки.

| Инструмент | Вход |
| --- | --- |
| `jev_review_requirements` | `request`, `requirements: [{id: "R1", text: "..."}]` |
| `jev_review_plan` | `requirementsReviewId`, `steps: [{text: "...", requirementIds: ["R1"]}]` |
| `jev_review_acceptance` | `planReviewId`, `revision`, `diff`, `testOutput` |

Каждый следующий этап получает UUID предыдущей проверки. Квитанции доступны по UUID в пределах одного сервера bb. Поле `threadId` указывает, кто выполнил этап, а расход учитывается в треде, сделавшем запрос. Пропущенные и неизвестные ID требований в плане выявляются локально. В приёмке один запрос к TypeSafe возвращает вердикт по каждому требованию. Агент передаёт в итог прогона `reviewId` приёмки.

Арена задаёт `revision` по фактически проверенному рабочему дереву. Для чистого дерева подходит ID коммита. Для незакоммиченных правок нужен SHA-256 снимка, который учитывает проверенные файлы, включая новые. При сборе отчёта Арена вычисляет отпечаток текущего дерева тем же способом.

Арена запрашивает результат через `bb plugin rpc call bb-jev jev.workflow.receipt --input-file <file> --json`. Формат входа для RPC и `jev_review_status` одинаковый.

```ts
type Input = { reviewId: string; revision?: string; threadId?: string };
type Verdict = "passed" | "failed" | "inconclusive";
type Output = {
  reviewId: string;
  threadId: string;
  parentReviewId: string | null;
  stage: "requirements" | "plan" | "acceptance";
  status: Verdict | "unavailable";
  reason: "invalid_response" | "request_failed" | "missing_api_key" | null;
  model: string | null;
  decisions: Record<string, {
    status: Verdict;
    probabilities: Record<Verdict, number>;
    confidence: number;
  }>;
  inputTokens: number | null;
  durationMs: number;
  revision: string | null;
  evidenceDigest: string;
  freshness: "current" | "stale" | "unverified" | null;
} | { error: "review_not_found" };
```

`reviewId` — UUID, `revision` — 40 или 64 строчных шестнадцатеричных символа. Поле `threadId` на входе остаётся для совместимости и игнорируется при поиске. На выходе оно содержит автора этапа. Для приёмки `freshness` равен `current` при совпадении ревизии, `stale` при расхождении и `unverified` без переданной ревизии. Для плана и требований `freshness` равен `null`. По `parentReviewId` можно собрать метрики всех трёх этапов. RPC отдаёт результаты и отпечатки материалов.

Скрытые тесты и остальные метрики Арены применяются к обоим вариантам прогона. В штатной цепочке три запроса к TypeSafe, по одному на этап. Из треда запускать только с разрешёнными материалами.

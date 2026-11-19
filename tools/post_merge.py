#!/usr/bin/env python3
"""Итог полного набора тестов после мержа → задачи из `Closes` влитых PR.

    python3 tools/post_merge.py prev      в stdout `run_id=` и `sha=` прошлого прогона
    python3 tools/post_merge.py verdict --junit F --prev-failed F --out-failed F \
        --base SHA --head SHA

Диапазон — коммиты от прошлого прогона, оставившего список упавших тестов (артефакт
`failed-tests`), до этого. Новый красный (тест падает сейчас и проходил тогда) переоткрывает
задачи диапазона с перечнем тестов; старый красный только комментируется — его задачи переоткрыл
прошлый прогон. Нет отчёта тестов — задачи не трогаются, код 2. Нет и списка прошлого прогона
(первый прогон, артефакт истёк) — новый красный от старого не отличить: не переоткрывать, а
написать об этом. Закрытую как «не будем делать» не переоткрывать. Список упавших для следующего
прогона пишется только после того, как все задачи обработаны: ошибка API на одной задаче — код 3,
списка нет, и следующий прогон разберёт тот же диапазон заново. Окружение: GITHUB_REPOSITORY,
GITHUB_TOKEN, GITHUB_RUN_ID, RUN_URL.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import urllib.error
import urllib.request
from enum import StrEnum
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backlog import not_planned  # noqa: E402 — tools/ добавлен в путь строкой выше
from pr_link import closes  # noqa: E402

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
WORKFLOW = "post-merge.yml"
ARTIFACT = "failed-tests"
MAX_LISTED = 30
API_FAILED = 3  # код выхода: ошибка API на задаче; список упавших не записан


class Outcome(StrEnum):
    """Что сказать в комментарии задачи; переоткрывает только REOPENED."""

    REOPENED = "переоткрыта"
    STILL_OPEN = "уже открыта"
    NOT_PLANNED = "закрыта как «не будем делать», не переоткрыта"
    NO_BASELINE = "списка прошлого прогона нет, не переоткрыта"
    OLD_RED = "не переоткрыта"
    GREEN = "зелёный"


def repo() -> str:
    return os.environ["GITHUB_REPOSITORY"]


def api(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(  # noqa: S310 — адрес — API GitHub из окружения прогона
        f"{API}{path}", method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
        raw = r.read()
    return json.loads(raw) if raw else None


def prev() -> int:
    """Прошлый завершённый прогон на main, у которого остался список упавших."""
    me = int(os.environ["GITHUB_RUN_ID"])
    runs = api("GET", f"/repos/{repo()}/actions/workflows/{WORKFLOW}/runs"
                      "?branch=main&status=completed&per_page=30")["workflow_runs"]
    for run in runs:
        if run["id"] == me:
            continue
        arts = api("GET", f"/repos/{repo()}/actions/runs/{run['id']}/artifacts")["artifacts"]
        if any(a["name"] == ARTIFACT and not a["expired"] for a in arts):
            print(f"run_id={run['id']}\nsha={run['head_sha']}")
            return 0
    print("run_id=\nsha=")
    return 0


def failed_tests(path: Path) -> set[str] | None:
    """Упавшие тесты отчёта JUnit; отчёта нет — None."""
    if not path.is_file():
        return None
    return {f"{c.get('classname', '')}::{c.get('name')}"
            # отчёт пишет наш же pytest в этом прогоне — не чужие данные
            for c in ElementTree.parse(path).iter("testcase")  # noqa: S314
            if c.find("failure") is not None or c.find("error") is not None}


def merged_tasks(base: str, head: str) -> dict[str, set[int]]:
    """Задачи из `Closes` PR, влитых между base и head: `owner/repo#N` → номера PR."""
    if base:
        shas = [c["sha"] for c in api("GET", f"/repos/{repo()}/compare/{base}...{head}")["commits"]]
    else:
        shas = [head]
    tasks: dict[str, set[int]] = {}
    for sha in shas:
        for pr in api("GET", f"/repos/{repo()}/commits/{sha}/pulls"):
            if pr.get("merged_at") and pr["base"]["ref"] == "main":
                for ref in closes(pr.get("body") or "", repo()):
                    tasks.setdefault(ref, set()).add(pr["number"])
    return tasks


def listed(tests: list[str]) -> str:
    lines = "\n".join(f"- `{t}`" for t in tests[:MAX_LISTED])
    more = f"\n- … и ещё {len(tests) - MAX_LISTED}" if len(tests) > MAX_LISTED else ""
    return f"{lines}{more}"


def comment(outcome: Outcome, new_red: list[str], prs: str, run_url: str) -> str:
    """Текст комментария в задачу; new_red — тесты, которых не было красными в прошлый раз."""
    if outcome is Outcome.REOPENED:
        return (f"Переоткрыта: после мержа {prs} полный набор упал на тестах, которые в прошлом "
                f"прогоне проходили. Прогон: {run_url}\n\n{listed(new_red)}")
    if outcome is Outcome.STILL_OPEN:
        return (f"После мержа {prs} полный набор упал на тестах, которые в прошлом прогоне "
                f"проходили; задача и так открыта. Прогон: {run_url}\n\n{listed(new_red)}")
    if outcome is Outcome.NOT_PLANNED:
        return (f"После мержа {prs} полный набор упал на тестах, которые в прошлом прогоне "
                "проходили, но задача закрыта как «не будем делать» — не переоткрыта. "
                f"Прогон: {run_url}\n\n{listed(new_red)}")
    if outcome is Outcome.NO_BASELINE:
        return (f"Полный набор после мержа {prs}: `main` красный, но списка упавших тестов "
                "прошлого прогона нет (первый прогон или артефакт истёк) — новый красный от "
                f"старого не отличить, задача не переоткрыта. Прогон: {run_url}")
    if outcome is Outcome.OLD_RED:
        return (f"Полный набор после мержа {prs}: `main` красный, но эти тесты падали и до него — "
                f"задача не переоткрыта. Прогон: {run_url}")
    return f"Полный набор после мержа {prs} зелёный. Прогон: {run_url}"


def reopen(num: str) -> Outcome:
    """Открыть задачу, если она закрыта; закрытую как «не будем делать» — нет: это решение
    человека, красный набор его не отменяет. Состояние читается до записи."""
    issue = api("GET", f"/repos/{repo()}/issues/{num}")
    if issue["state"] == "open":
        return Outcome.STILL_OPEN
    if not_planned(issue.get("state_reason")):
        return Outcome.NOT_PLANNED
    api("PATCH", f"/repos/{repo()}/issues/{num}", {"state": "open"})
    return Outcome.REOPENED


def read_baseline(path: Path) -> set[str] | None:
    """Упавшие тесты прошлого прогона; файла нет — None: база неизвестна."""
    return set(path.read_text(encoding="utf-8").splitlines()) if path.is_file() else None


def outcome_of(now: set[str], before: set[str] | None) -> Outcome:
    """Исход до того, как известно состояние задачи: переоткрытие уточнит его."""
    if not now:
        return Outcome.GREEN
    if before is None:
        return Outcome.NO_BASELINE
    return Outcome.REOPENED if now - before else Outcome.OLD_RED


def settle(ref: str, prs: set[int], now: set[str], before: set[str] | None) -> Outcome:
    """Одна задача: переоткрыть при новом красном и оставить комментарий."""
    num = ref.split("#")[1]
    outcome = outcome_of(now, before)
    if outcome is Outcome.REOPENED:
        outcome = reopen(num)
    new_red = sorted(now - (before or set()))
    text = comment(outcome, new_red, ", ".join(f"#{n}" for n in sorted(prs)), os.environ["RUN_URL"])
    api("POST", f"/repos/{repo()}/issues/{num}/comments", {"body": text})
    return outcome


def settle_all(tasks: dict[str, set[int]], now: set[str], before: set[str] | None) -> list[str]:
    """Все задачи диапазона; ошибка API на одной не останавливает остальные. Возвращает
    задачи, на которых она случилась."""
    failed = []
    for ref, prs in sorted(tasks.items()):
        if ref.split("#")[0] != repo():
            print(f"::warning::{ref}: задача другого репозитория, токен прогона её не переоткроет")
            continue
        try:
            print(f"{ref}: {settle(ref, prs, now, before).value}")
        except OSError as error:  # URLError и HTTPError — его потомки
            print(f"::error::{ref}: ошибка API, задача не обработана: {error}")
            failed.append(ref)
    return failed


def verdict(a: argparse.Namespace) -> int:
    now = failed_tests(Path(a.junit))
    if now is None:
        print("::error::отчёта тестов нет — набор не дошёл до конца; задачи не трогаю")
        return 2
    failed = settle_all(merged_tasks(a.base, a.head), now,
                        read_baseline(Path(a.prev_failed)))
    if failed:
        # списка для следующего прогона нет: он возьмёт прошлый и разберёт диапазон заново
        print(f"::error::не обработаны: {', '.join(failed)}; список упавших не записан")
        return API_FAILED
    Path(a.out_failed).write_text("".join(f"{t}\n" for t in sorted(now)), encoding="utf-8")
    return 1 if now else 0


# ---------------------------------------------------------------------------
# selftest: без сети — api и список задач подменены
# ---------------------------------------------------------------------------

RUN_ENV = {"GITHUB_REPOSITORY": "o/r", "RUN_URL": "u"}


class FakeApi:
    """Подмена `api`: журнал вызовов, задачи по номерам; номера из `broken` падают как сеть."""

    def __init__(self, issues: dict[str, dict], broken: frozenset[str] = frozenset()) -> None:
        self.issues, self.broken, self.calls, self.bodies = issues, broken, [], {}

    def __call__(self, method: str, path: str, body: dict | None = None):
        self.calls.append((method, path))
        if method == "POST":
            self.bodies[path.split("/")[5]] = body["body"]
        num = path.split("/")[5]
        if num in self.broken:
            raise urllib.error.URLError("сеть")
        return self.issues[num] if method == "GET" else None

    def wrote(self, method: str, num: str) -> bool:
        return any(m == method and p.startswith(f"/repos/o/r/issues/{num}") for m, p in self.calls)


def run_verdict(api_stub: FakeApi, baseline: str | None, dest: Path) -> tuple[int, bool]:
    """verdict с тестом t::bad в красном; результат — код выхода и записан ли список упавших."""
    from unittest import mock
    junit, prev, out = dest / "j.xml", dest / "prev.txt", dest / "out.txt"
    junit.write_text('<testsuite><testcase classname="t" name="bad"><failure/></testcase>'
                     "</testsuite>", encoding="utf-8")
    if baseline is not None:
        prev.write_text(baseline, encoding="utf-8")
    ns = argparse.Namespace(junit=str(junit), prev_failed=str(prev), out_failed=str(out),
                            base="a", head="b")
    tasks = {f"o/r#{n}": {int(n) + 10} for n in api_stub.issues}
    with mock.patch.dict(os.environ, RUN_ENV), mock.patch(f"{__name__}.api", api_stub), \
            mock.patch(f"{__name__}.merged_tasks", lambda *_: tasks), \
            contextlib.redirect_stdout(io.StringIO()):  # вывод прогона самопроверке не нужен
        return verdict(ns), out.exists()


def closed(reason: str) -> dict:
    return {"state": "closed", "state_reason": reason}


def selftest_verdict() -> list[str]:
    """Переоткрытие: закрытая — открывается; «не будем делать» и неизвестная база — нет; ошибка
    API на первой задаче не отнимает вторую и не пишет базу для следующего прогона."""
    import tempfile
    both = {"1": closed("completed"), "2": closed("completed")}
    cases = {  # название → (задачи, сломанные, база) и ждём (код, список записан, PATCH 1, PATCH 2)
        "новый красный": ((both, frozenset(), ""), (1, True, True, True)),
        "не будем делать": (({**both, "1": closed("not_planned")}, frozenset(), ""),
                            (1, True, False, True)),
        "базы нет": ((both, frozenset(), None), (1, True, False, False)),
        "ошибка API на первой": ((both, frozenset({"1"}), ""), (API_FAILED, False, False, True)),
        "уже открыта": (({**both, "1": {"state": "open"}}, frozenset(), ""),
                        (1, True, False, True)),
    }
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, ((issues, broken, base), want) in cases.items():
            stub, dest = FakeApi(issues, broken), Path(tmp) / name.replace(" ", "-")
            dest.mkdir()
            code, written = run_verdict(stub, base, dest)
            got = (code, written, stub.wrote("PATCH", "1"), stub.wrote("PATCH", "2"))
            if got != want:
                bad.append(f"{name}: код, список, PATCH 1, PATCH 2 = {got}, ждали {want}")
            if name == "уже открыта" and stub.bodies["1"].startswith("Переоткрыта"):
                bad.append("уже открытая — в комментарии «Переоткрыта», хотя её не открывали")
    return bad


def selftest_merged_tasks() -> list[str]:
    """Поиск PR по диапазону: слияние merge-коммитом (так сливает `gh pr merge --merge`) даёт
    в диапазоне и merge-коммит, и коммиты ветки — PR находится по любому, один раз."""
    from unittest import mock
    pr = {"number": 5, "merged_at": "t", "base": {"ref": "main"}, "body": "Closes #12\nPart of #3"}
    open_pr = {"number": 6, "merged_at": None, "base": {"ref": "main"}, "body": "Closes #99"}
    pulls = {"merge1": [pr], "feat1": [pr], "feat2": [pr, open_pr], "plain": []}

    def fake(method: str, path: str, body: dict | None = None):
        if "/compare/" in path:
            return {"commits": [{"sha": s} for s in ("feat1", "feat2", "merge1", "plain")]}
        return pulls[path.split("/")[5]]

    with mock.patch.dict(os.environ, RUN_ENV), mock.patch(f"{__name__}.api", fake):
        got = (merged_tasks("a", "b"), merged_tasks("", "merge1"))
    want = {"o/r#12": {5}}
    return [] if got == (want, want) else [f"поиск PR по merge-коммиту: {got}, ждали {want}"]


def selftest() -> int:
    """Текст комментария по исходам, чтение отчёта JUnit и переоткрытие — без сети."""
    import tempfile
    bad = []
    if not comment(Outcome.REOPENED, ["a::b"], "#1", "u").startswith("Переоткрыта"):
        bad.append("новый красный — не «Переоткрыта»")
    if "падали и до него" not in comment(Outcome.OLD_RED, [], "#1", "u"):
        bad.append("старый красный — не «падали и до него»")
    if "списка упавших" not in comment(Outcome.NO_BASELINE, [], "#1", "u"):
        bad.append("базы нет — не сказано, что списка нет")
    if "не будем делать" not in comment(Outcome.NOT_PLANNED, ["a::b"], "#1", "u"):
        bad.append("закрыта как «не будем делать» — не сказано")
    if comment(Outcome.STILL_OPEN, ["a::b"], "#1", "u").startswith("Переоткрыта"):
        bad.append("уже открытая — названа «Переоткрыта», хотя её не открывали")
    if "зелёный" not in comment(Outcome.GREEN, [], "#1", "u"):
        bad.append("зелёный — не «зелёный»")
    with tempfile.TemporaryDirectory() as d:
        junit = Path(d) / "j.xml"
        junit.write_text('<testsuite><testcase classname="t" name="ok"/><testcase classname="t" '
                         'name="bad"><failure/></testcase></testsuite>', encoding="utf-8")
        if failed_tests(junit) != {"t::bad"} or failed_tests(Path(d) / "нет.xml") is not None:
            bad.append("отчёт JUnit: упавшие или отсутствие отчёта")
    bad += selftest_verdict() + selftest_merged_tasks()
    for line in bad:
        print("✗ " + line)
    if not bad:
        print("самопроверка пройдена: комментарий по исходам, отчёт JUnit, переоткрытие: "
              "«не будем делать», нет базы, ошибка API, PR по merge-коммиту")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prev")
    v = sub.add_parser("verdict")
    v.add_argument("--junit", required=True)
    v.add_argument("--prev-failed", required=True)
    v.add_argument("--out-failed", required=True)
    v.add_argument("--base", default="")
    v.add_argument("--head", required=True)
    args = p.parse_args()
    sys.exit(prev() if args.cmd == "prev" else verdict(args))

#!/usr/bin/env python3
"""Событие GitHub → текст уведомления для темы «Задачи и PR».

    python3 tools/tg_notify.py --event ИМЯ --payload СОБЫТИЕ.json [--comments КОММЕНТАРИИ.json]
    python3 tools/tg_notify.py --selftest

Печатает текст в stdout; молчать (черновик PR, зелёный прогон, PR закрыт без слияния) — пустой
вывод. Отправляет `tools/tg_send.py send --topic tasks`: повторы, деление длинного текста и
выход без токена — там (workflow `tg-notify.yml`; PR и задачи, которые сливает и закрывает
токен CI, — job `automerge` в `check.yml`: события токена CI другие workflow не запускают).

У закрытой задачи в текст идёт последний комментарий-итог — «## Сводка» из пяти пунктов
(`docs/agents/tasks.md`, «Закрытие») или «## Что установлено» из `backlog.py comment`; комментарии
workflow берёт через `gh api` и передаёт файлом.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from backlog import not_planned
from project_config import backlog_config
from script_common import ROOT, report, run_script

SUMMARIES = ("## Сводка", "## Что установлено")
CHECK_WORKFLOW = "post-merge"  # полный набор на main — .github/workflows/post-merge.yml


def summary(comments: list[dict]) -> str:
    """Последний комментарий-итог без заголовка или пусто."""
    for comment in reversed(comments):
        body = comment["body"].strip()
        for head in SUMMARIES:
            if body.startswith(head):
                return "\n\n" + body.removeprefix(head).strip()
    return ""


def issue_text(payload: dict, comments: list[dict], prefix: str) -> str:
    issue = payload["issue"]
    name = f"{prefix}#{issue['number']} {issue['title']}"
    if payload["action"] == "opened":
        return f"📌 Новая задача {name}\n{issue['html_url']}"
    if not_planned(issue.get("state_reason")):
        return f"⏹ Закрыта без выполнения {name}\n{issue['html_url']}"
    return f"✅ Закрыта {name}\n{issue['html_url']}{summary(comments)}"


def pull_text(payload: dict) -> str:
    pull = payload["pull_request"]
    name = f"PR #{pull['number']} {pull['title']}"
    if payload["action"] == "closed":
        return f"🔀 Слит {name}\n{pull['html_url']}" if pull["merged"] else ""
    if pull["draft"]:
        return ""
    return f"👀 {name} ({pull['head']['ref']} → {pull['base']['ref']})\n{pull['html_url']}"


def run_text(payload: dict) -> str:
    run = payload["workflow_run"]
    if run["name"] != CHECK_WORKFLOW or run["conclusion"] != "failure":
        return ""
    title = run["head_commit"]["message"].splitlines()[0]
    return f"🔴 Полный набор красный на {run['head_branch']}: {title}\n{run['html_url']}"


def text(event: str, payload: dict, comments: list[dict], prefix: str) -> str:
    if event == "issues":
        return issue_text(payload, comments, prefix)
    if event == "pull_request":
        return pull_text(payload)
    if event == "workflow_run":
        return run_text(payload)
    return ""


def run(root: Path) -> int:
    cli = argparse.ArgumentParser(description="событие GitHub → текст для Telegram")
    cli.add_argument("--event", required=True)
    cli.add_argument("--payload", type=Path, required=True)
    cli.add_argument("--comments", type=Path)
    args = cli.parse_args(sys.argv[1:])
    payload = json.loads(args.payload.read_text(encoding="utf-8"))
    comments = json.loads(args.comments.read_text(encoding="utf-8")) if args.comments else []
    print(text(args.event, payload, comments, backlog_config(root)["prefix"]), end="")
    return 0


# ---------------------------------------------------------------------------
# selftest: каждое событие — свой текст, молчание там, где шуметь не надо
# ---------------------------------------------------------------------------

URL = "https://github.com/o/r/"
ISSUE = {"number": 7, "title": "Сборщик", "html_url": URL + "issues/7", "state_reason": None}
PULL = {"number": 9, "title": "Сборщик", "html_url": URL + "pull/9", "draft": False,
        "merged": False, "head": {"ref": "agent/7-tg"}, "base": {"ref": "main"}}
RUN = {"name": CHECK_WORKFLOW, "conclusion": "failure", "head_branch": "main",
       "html_url": URL + "actions/runs/1", "head_commit": {"message": "feat: сборщик\n\nтело"}}
COMMENTS = [{"body": "## Сводка\n\n- старое"}, {"body": "просто комментарий"},
            {"body": "## Сводка\n\n1. Просили: сборщик.\n2. Буфер работает."}]
INSTALLED = [{"body": "## Сводка\n\n- старое"}, {"body": "## Что установлено\n\n- новое"}]

CASES = [
    ("issues", {"action": "opened", "issue": ISSUE}, [],
     "📌 Новая задача ab#7 Сборщик\n" + URL + "issues/7"),
    ("issues", {"action": "closed", "issue": ISSUE}, COMMENTS,
     "✅ Закрыта ab#7 Сборщик\n" + URL + "issues/7\n\n1. Просили: сборщик.\n2. Буфер работает."),
    ("issues", {"action": "closed", "issue": ISSUE}, INSTALLED,
     "✅ Закрыта ab#7 Сборщик\n" + URL + "issues/7\n\n- новое"),
    ("issues", {"action": "closed", "issue": ISSUE}, [],
     "✅ Закрыта ab#7 Сборщик\n" + URL + "issues/7"),
    ("issues", {"action": "closed", "issue": ISSUE | {"state_reason": "not_planned"}}, COMMENTS,
     "⏹ Закрыта без выполнения ab#7 Сборщик\n" + URL + "issues/7"),
    ("pull_request", {"action": "opened", "pull_request": PULL}, [],
     "👀 PR #9 Сборщик (agent/7-tg → main)\n" + URL + "pull/9"),
    ("pull_request", {"action": "opened", "pull_request": PULL | {"draft": True}}, [], ""),
    ("pull_request", {"action": "closed", "pull_request": PULL | {"merged": True}}, [],
     "🔀 Слит PR #9 Сборщик\n" + URL + "pull/9"),
    ("pull_request", {"action": "closed", "pull_request": PULL}, [], ""),
    ("workflow_run", {"workflow_run": RUN}, [],
     "🔴 Полный набор красный на main: feat: сборщик\n" + URL + "actions/runs/1"),
    ("workflow_run", {"workflow_run": RUN | {"conclusion": "success"}}, [], ""),
    ("workflow_run", {"workflow_run": RUN | {"name": "tg-notify"}}, [], ""),
]


def workflow_names(root: Path) -> list[str]:
    """Имя полного набора — то же, что в post-merge.yml и в подписке tg-notify.yml."""
    workflows = root / ".github/workflows"
    bad = []
    def read(name: str) -> str:
        return (workflows / name).read_text(encoding="utf-8")

    if f"name: {CHECK_WORKFLOW}\n" not in read("post-merge.yml"):
        bad.append(f"post-merge.yml называется не {CHECK_WORKFLOW!r}")
    if f"workflows: [{CHECK_WORKFLOW}]" not in read("tg-notify.yml"):
        bad.append(f"tg-notify.yml не подписан на {CHECK_WORKFLOW!r}")
    return bad


def selftest() -> int:
    bad = workflow_names(ROOT)
    for number, (event, payload, comments, expected) in enumerate(CASES, 1):
        got = text(event, payload, comments, "ab")
        if got != expected:
            bad.append(f"случай {number} ({event} {payload.get('action', '')}): {got!r}, "
                       f"ждали {expected!r}")
    return report(bad, f"{len(CASES)} событий: задача заведена и закрыта (со сводкой, с «Что "
                       "установлено», без итога, без выполнения), PR открыт, черновик, слит, "
                       "закрыт; полный набор красный и нет; имя полного набора — как в workflow")


if __name__ == "__main__":
    sys.exit(run_script(selftest, run))

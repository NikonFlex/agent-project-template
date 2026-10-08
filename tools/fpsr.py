#!/usr/bin/env python3
"""FPSR и PR на задачу — две меры среды (почему так мерим — docs/WHY.md).

    python3 tools/fpsr.py              посчитать по папке задач из .backlog.json и PR в GitHub
    python3 tools/fpsr.py --selftest

FPSR — доля задач, закрытых с первого промпта. Считаются задачи, у которых в низу есть раздел
«Первый промпт»: первая строка «да» или «нет — почему» (.claude/rules/tasks.md). Задачи без
раздела в долю не входят.

PR на задачу — сколько PR понадобилось задаче, включая закрытые без слияния: больше
одного — работу переделывали или чинили после слияния. PR относится к задаче по ссылке
`<префикс>#N` в заголовке или по номеру в начале ветки (`12-parser`); описание не смотрим —
в нём упоминают и другие задачи. PR учёта — только копии задач и журнал (`BOOKKEEPING`) —
не в счёт: это не переделка. Нет `gh` или сети — строка об этом, FPSR считается.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from backlog import MARK, load_tasks
from project_config import backlog_config
from script_common import report, run_script

SECTION = "Первый промпт"
STATES = {"MERGED": "слит", "CLOSED": "закрыт без слияния", "OPEN": "открыт"}
BOOKKEEPING = ("docs/tasks/", "docs/journal/")


def verdicts(folder: Path) -> list[tuple[str, bool, str]]:
    """Задача → закрыта ли с первого промпта и первая строка раздела."""
    rows = []
    for task in load_tasks(folder):
        body = task.sections().get(SECTION, "").strip()
        if body:
            first = body.splitlines()[0].strip()
            rows.append((task.name, first.strip("`*_ ").lower().startswith("да"), first))
    return rows


def summary(rows: list[tuple[str, bool, str]]) -> str:
    if not rows:
        return f"задач с разделом «{SECTION}» нет"
    yes = sum(ok for _, ok, _ in rows)
    lines = [f"FPSR: {yes} из {len(rows)} = {yes / len(rows):.2f}"]
    lines += [f"  {name}: {first}" for name, ok, first in rows if not ok]
    return "\n".join(lines)


def task_of(pull: dict, prefix: str) -> int | None:
    """Задача PR — по ссылке в заголовке или по номеру в начале ветки."""
    if m := re.search(rf"(?<![\w#]){re.escape(prefix)}#(\d+)", pull["title"]):
        return int(m.group(1))
    if m := re.match(r"(\d+)-", pull["headRefName"]):
        return int(m.group(1))
    return None


def pulls_by_task(pulls: list[dict], prefix: str) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for pull in sorted(pulls, key=lambda p: p["number"]):
        paths = [f["path"] for f in pull.get("files") or []]
        if paths and all(path.startswith(BOOKKEEPING) for path in paths):
            continue
        if (number := task_of(pull, prefix)) is not None:
            out.setdefault(number, []).append(pull)
    return out


def fetch_pulls(root: Path) -> list[dict] | None:
    fields = "number,title,headRefName,state,files"
    done = subprocess.run(["gh", "pr", "list", "--state", "all", "--limit", "1000",  # noqa: S603, S607
                           "--json", fields], cwd=root, capture_output=True, text=True)
    return json.loads(done.stdout) if done.returncode == 0 else None


def pulls_summary(by_task: dict[int, list[dict]], prefix: str) -> str:
    if not by_task:
        return "PR на задачу: PR со ссылкой на задачу нет"
    total = sum(len(prs) for prs in by_task.values())
    lines = [f"PR на задачу: {total / len(by_task):.2f} ({total} PR на {len(by_task)} задач)"]
    lines += [f"  {prefix}#{n}: {len(prs)} — " + ", ".join(
        f"#{p['number']} {STATES.get(p['state'], p['state'])}" for p in prs)
        for n, prs in sorted(by_task.items()) if len(prs) > 1]
    return "\n".join(lines)


def selftest_pulls() -> list[str]:
    pulls = [{"number": 77, "title": "gf#76: аудит", "headRefName": "76-why", "state": "CLOSED"},
             {"number": 80, "title": "gf#76: аудит", "headRefName": "76-why", "state": "MERGED"},
             {"number": 81, "title": "схема", "headRefName": "78-scheme", "state": "MERGED"},
             {"number": 95, "title": "Переименование", "headRefName": "rename", "state": "MERGED"},
             {"number": 96, "title": "ab#5: чужой префикс", "headRefName": "x", "state": "OPEN"},
             {"number": 97, "title": "gf#78: учёт", "headRefName": "78-sync", "state": "MERGED",
              "files": [{"path": "docs/tasks/gf-0078.md"}, {"path": "docs/journal/JOURNAL.md"}]}]
    text = pulls_summary(pulls_by_task(pulls, "gf"), "gf")
    return [why for ok, why in [
        ("1.50 (3 PR на 2 задач)" in text, f"неверное среднее: {text!r}"),
        ("gf#76: 2 — #77 закрыт без слияния, #80 слит" in text, f"нет разбора gf#76: {text!r}"),
        ("gf#78" not in text, "задача с одним PR — не в списке"),
    ] if not ok]


def selftest() -> int:
    bodies = {"g-0001": "да", "g-0002": "`нет` — дыра: где лежат логи", "g-0003": "",
              "g-0004": "Да."}
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        for name, body in bodies.items():
            section = f"## {SECTION}\n\n{body}\n" if body else "## Ход работы\n\nx\n"
            (folder / f"{name}.md").write_text(f"# {name}\n\n{MARK}\n\n{section}", encoding="utf-8")
        rows = verdicts(folder)
    text = summary(rows)
    bad = [why for ok, why in [
        (len(rows) == 3, f"посчитано задач {len(rows)}, а не 3: без раздела — не в счёт"),
        ("2 из 3" in text, f"неверная доля: {text!r}"),
        ("g-0002: `нет` — дыра" in text, "в отчёте нет причины «нет»"),
    ] if not ok] + selftest_pulls()
    return report(bad, "FPSR: доля, задачи без раздела не в счёт, причины «нет»; PR на задачу: "
                       "по заголовку и ветке, среднее, задачи с несколькими PR")


def show(root: Path) -> int:
    cfg = backlog_config(root)
    print(summary(verdicts(cfg["tasks"])))
    pulls = fetch_pulls(root)
    print("PR на задачу: нет данных — `gh pr list` не сработал" if pulls is None
          else pulls_summary(pulls_by_task(pulls, cfg["prefix"] or ""), cfg["prefix"] or ""))
    return 0


if __name__ == "__main__":
    sys.exit(run_script(selftest, show))

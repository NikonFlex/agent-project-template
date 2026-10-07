#!/usr/bin/env python3
"""FPSR — доля задач, закрытых с первого промпта (почему так мерим — docs/WHY.md).

    python3 tools/fpsr.py              посчитать по папке задач из .backlog.json
    python3 tools/fpsr.py --selftest

Считаются задачи, у которых в низу есть раздел «Первый промпт»: первая строка «да» или
«нет — почему» (.claude/rules/tasks.md). Задачи без раздела в долю не входят.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from backlog import MARK, load_tasks
from project_config import backlog_config
from script_common import report, run_script

SECTION = "Первый промпт"


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
    ] if not ok]
    return report(bad, "FPSR: доля, задачи без раздела не в счёт, причины «нет»")


def show(root: Path) -> int:
    print(summary(verdicts(backlog_config(root)["tasks"])))
    return 0


if __name__ == "__main__":
    sys.exit(run_script(selftest, show))

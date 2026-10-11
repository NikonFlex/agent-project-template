"""Бюджет постоянного контекста: файлы, которые агент читает всегда, не растут молча.

    python3 tools/context_budget.py
    python3 tools/context_budget.py --selftest

Чем больше постоянного текста, тем хуже держатся правила. Предел строк каждого файла — в
tools/context_budget.json; поднять его — осознанная правка в PR, а не тихий рост. Строка
длиннее `min_repeat` знаков, которая стоит в двух таких файлах, — повтор: одно правило
должно жить в одном месте (AGENTS.md — общее, CLAUDE.md — только своё для Claude Code).
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

from script_common import ROOT, print_problems, report, write_files

CONFIG = Path("tools/context_budget.json")


def normal(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip(" -*>#|")).lower()


def run(root: Path) -> list[str]:
    cfg = json.loads((root / CONFIG).read_text(encoding="utf-8"))
    problems, seen = [], {}
    for name, limit in cfg["limits"].items():
        path = root / name
        if not path.exists():
            problems.append(f"{name}: файла нет — убрать из {CONFIG} или вернуть файл")
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > limit:
            problems.append(f"{name}: {len(lines)} строк, предел {limit} — сократить или "
                            f"поднять предел в {CONFIG} осознанно")
        for line in lines:
            key = normal(line)
            if len(key) >= cfg["min_repeat"] and seen.setdefault(key, name) != name:
                problems.append(f"{name} повторяет {seen[key]}: «{line.strip()[:60]}…»")
    return problems


def selftest() -> int:
    rule = "Ключи — только в .env; ключ, попавший в чат или коммит, скомпрометирован сразу."
    files = {str(CONFIG): json.dumps({"limits": {"A.md": 3, "B.md": 3}, "min_repeat": 40}),
             "A.md": f"# A\n{rule}\n", "B.md": "# B\nсвоё\n"}
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        root = write_files(Path(tmp), files)
        if problems := run(root):
            bad.append(f"чистый: {problems}")
        (root / "B.md").write_text(f"# B\n- {rule}\n", encoding="utf-8")
        if not any("повторяет A.md" in p for p in run(root)):
            bad.append("повтор правила не пойман")
        (root / "B.md").write_text("# B\n1\n2\n3\n", encoding="utf-8")
        if not any("4 строк, предел 3" in p for p in run(root)):
            bad.append("рост сверх предела не пойман")
    return report(bad, "повтор правила между файлами, рост сверх предела")


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv
             else print_problems(run(ROOT), "постоянный контекст в бюджете"))

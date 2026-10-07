#!/usr/bin/env python3
"""Гейт чистого контекста: инструкции не врут о репозитории, код не оставляет оговорок.

    python3 tools/context_check.py              проверить репозиторий
    python3 tools/context_check.py --selftest

1. Пути со слэшем в `обратных кавычках` и в ссылках [..](путь) из документов, которые читает
   Claude, существуют. Иначе инструкция отправляет в файл, которого нет, — вторая версия
   правды. Пропускаются шаблоны (<…>, {{…}}, *), пути из .gitignore и `ignore_paths`.
2. В комментариях кода нет оговорок вместо проверки («for now», «упрощённо», …): отложенное —
   в «Что осталось» задачи. TODO и закомментированный код ловит ruff (FIX, ERA).

Что проверять и какие слова — tools/context_check.json.
"""

from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import tokenize
from pathlib import Path

from script_common import print_problems, report, run_script

TICK = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"\]\(([^)\s]+)\)")
REPO_PATH = re.compile(r"^[\w.\-]+(/[\w.\-]*)+$")
SKIP_DIRS = {".git", ".venv", "node_modules", "outputs"}


def gitignored(root: Path) -> set[str]:
    path = root / ".gitignore"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return {ln.strip().strip("/") for ln in lines if ln.strip() and not ln.startswith(("#", "!"))}


def paths_in(line: str) -> list[str]:
    found = [c.split("#")[0].rstrip(":.,") for c in TICK.findall(line) + LINK.findall(line)]
    return [c for c in found if REPO_PATH.match(c) and not c.startswith("http")]


def dead_paths(root: Path, cfg: dict) -> list[str]:
    """Пути из документов, которых нет на диске."""
    skip = gitignored(root) | set(cfg["ignore_paths"])
    problems = []
    for doc in sorted({p for pattern in cfg["docs"] for p in root.glob(pattern)}):
        for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            for path in paths_in(line):
                exists = any((base / path).exists() for base in (root, doc.parent))
                if exists or path.strip("/") in skip:
                    continue
                problems.append(f"{doc.relative_to(root)}:{n}: нет пути {path}")
    return problems


def caveats(root: Path, cfg: dict) -> list[str]:
    """Оговорки в комментариях .py-файлов проекта."""
    words = re.compile("|".join(re.escape(w) for w in cfg["caveats"]), re.I)
    me = Path(__file__).resolve()
    problems = []
    for path in sorted(root.rglob("*.py")):
        if SKIP_DIRS & set(path.relative_to(root).parts) or path.resolve() == me:
            continue
        for tok in tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline):
            if tok.type == tokenize.COMMENT and (m := words.search(tok.string)):
                where = f"{path.relative_to(root)}:{tok.start[0]}"
                problems.append(f"{where}: оговорка «{m.group(0)}» в комментарии")
    return problems


def run(root: Path) -> list[str]:
    cfg = json.loads((root / "tools/context_check.json").read_text(encoding="utf-8"))
    return dead_paths(root, cfg) + caveats(root, cfg)


def selftest() -> int:
    cfg = {"docs": ["CLAUDE.md"], "ignore_paths": ["specs/example.md"], "caveats": ["for now"]}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "tools").mkdir()
        (root / "tools/context_check.json").write_text(json.dumps(cfg), encoding="utf-8")
        (root / "tools/real.py").write_text("x = 1  # обычный комментарий\n", encoding="utf-8")
        (root / ".gitignore").write_text("outputs/\n", encoding="utf-8")
        (root / "CLAUDE.md").write_text(
            "Есть `tools/real.py`, [ссылка](tools/real.py), `outputs/`, `specs/example.md`,\n"
            "`docs/tasks/<префикс>-0012.md`, `.claude/hooks/pre-commit.sh`.\n", encoding="utf-8")
        found = run(root)
        (root / "tools/bad.py").write_text("y = 2  # for now так\n", encoding="utf-8")
        found_bad = run(root)
    bad = [why for ok, why in [
        (found == ["CLAUDE.md:2: нет пути .claude/hooks/pre-commit.sh"], f"пути: {found}"),
        (any("оговорка «for now»" in p for p in found_bad), "не поймана оговорка в комментарии"),
    ] if not ok]
    return report(bad, "мёртвый путь пойман, шаблоны и .gitignore пропущены, оговорка поймана")


def check(root: Path) -> int:
    return print_problems(run(root), "контекст чист")


if __name__ == "__main__":
    sys.exit(run_script(selftest, check))

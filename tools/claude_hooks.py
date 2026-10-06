#!/usr/bin/env python3
"""Хуки Claude Code этого проекта (подключены в .claude/settings.json).

    python3 tools/claude_hooks.py after-compact   SessionStart, matcher compact
    python3 tools/claude_hooks.py is-commit       PreToolUse Bash: JSON вызова на stdin
    python3 tools/claude_hooks.py --selftest

after-compact — после сжатия контекста напомнить, над какой задачей работали: номер — из
имени ветки (`12-parser` → 12), из файла задачи — «Чем возобновлять» и «Что осталось».
Что хук печатает в stdout, Claude Code добавляет в контекст.

is-commit — код 0, если команда Bash делает `git commit`, иначе 1. Фильтр `if` в
settings.json приблизительный: на командах с `$VAR` и `$()` Claude Code запускает хук
всегда, и без этой проверки красное дерево блокировало бы любую такую команду.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

from selftest_report import report

SECTIONS = ("Чем возобновлять", "Что осталось")
# git, его ключи (`-C путь`, `--no-pager`), затем подкоманда commit: `git log --grep commit` — нет.
GIT_COMMIT = re.compile(r"\bgit(?:\s+-\S+(?:\s+[^\s-]\S*)?)*\s+commit\b")


def branch(root: Path) -> str:
    """Имя ветки из HEAD; в worktree `.git` — файл со ссылкой на настоящий каталог."""
    git = root / ".git"
    if git.is_file():
        git = Path(git.read_text(encoding="utf-8").split(":", 1)[1].strip())
    head = (git / "HEAD").read_text(encoding="utf-8").strip()
    return head.removeprefix("ref: refs/heads/") if head.startswith("ref:") else ""


def tasks_dir(root: Path) -> Path:
    config = root / ".backlog.json"
    cfg = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    return root / cfg.get("tasks_dir", "docs/tasks")


def section(text: str, name: str) -> str:
    match = re.search(rf"^## {re.escape(name)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return match.group(1).strip() if match else ""


def reminder(root: Path, current: str) -> str:
    number = re.match(r"(\d+)-", current)
    if not number:
        return (f"Контекст сжат. Ветка «{current or '—'}» без номера задачи: спросить хозяина, "
                "над чем работаем, или открыть docs/tasks/INDEX.md. Правила CLAUDE.md в силе.")
    found = sorted(tasks_dir(root).glob(f"*-{int(number.group(1)):04d}.md"))
    if not found:
        return f"Контекст сжат. Ветка {current}: файла задачи нет — `backlog.py sync`."
    text = found[0].read_text(encoding="utf-8")
    title = text.splitlines()[0].lstrip("# ").strip()
    lines = [f"Контекст сжат. Работаем над {title} (ветка {current}, файл {found[0].name})."]
    for name in SECTIONS:
        if body := section(text, name):
            lines += ["", f"{name}:", body]
    lines += ["", "Продолжать по файлу задачи; правила CLAUDE.md в силе."]
    return "\n".join(lines)


def is_commit(call: dict) -> bool:
    return bool(GIT_COMMIT.search((call.get("tool_input") or {}).get("command") or ""))


def selftest() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "docs/tasks").mkdir(parents=True)
        (root / "docs/tasks/ab-0012.md").write_text(
            "# ab#12 — Парсер\n\n## Чем возобновлять\n\nЗапустить тесты.\n\n"
            "## Ход работы\n\nвсякое\n\n## Что осталось\n\n- хвост\n", encoding="utf-8")
        text = reminder(root, "12-parser")
        bad = [why for ok, why in [
            ("ab#12 — Парсер" in text, "нет названия задачи"),
            ("Запустить тесты." in text and "- хвост" in text, "нет нужных разделов"),
            ("всякое" not in text, "попал лишний раздел"),
            ("без номера" in reminder(root, "main"), "ветка без номера"),
            ("файла задачи нет" in reminder(root, "99-x"), "нет файла задачи"),
        ] if not ok]
    commits = ["git commit -m x", "git add a && git commit", "git -C /r commit -q"]
    others = ["git log --grep commit", "echo ${PIPESTATUS[0]}", "git status", "git reset HEAD~1"]
    bad += [f"is-commit ошибся на {c!r}" for c in commits if not is_commit(bash(c))]
    bad += [f"is-commit ошибся на {c!r}" for c in others if is_commit(bash(c))]
    return report(bad, "after-compact: задача по ветке, разделы; is-commit: коммит и не коммит")


def bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    root = Path(__file__).resolve().parents[1]
    if sys.argv[1:] == ["after-compact"]:
        print(reminder(root, branch(root)))
        return 0
    if sys.argv[1:] == ["is-commit"]:
        return 0 if is_commit(json.load(sys.stdin)) else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())

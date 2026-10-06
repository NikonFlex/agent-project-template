#!/usr/bin/env python3
"""Хуки Claude Code этого проекта (подключены в .claude/settings.json).

    python3 tools/claude_hooks.py after-compact   SessionStart, matcher compact
    python3 tools/claude_hooks.py is-commit       PreToolUse Bash: JSON вызова на stdin
    python3 tools/claude_hooks.py context         UserPromptSubmit: JSON на stdin
    python3 tools/claude_hooks.py --selftest

after-compact — после сжатия контекста напомнить, над какой задачей работали: номер — из
имени ветки (`12-parser` → 12), из файла задачи — «Чем возобновлять» и «Что осталось».
Что хук печатает в stdout, Claude Code добавляет в контекст.

is-commit — код 0, если команда Bash делает `git commit`, иначе 1. Фильтр `if` в
settings.json приблизительный: на командах с `$VAR` и `$()` Claude Code запускает хук
всегда, и без этой проверки красное дерево блокировало бы любую такую команду.

context — сколько контекста занято: по `usage` последнего ответа основной сессии в журнале
(`transcript_path`). Ниже первого порога молчит, выше — одна строка; что делать на каждом
пороге — CLAUDE.md, «Контекст». Окно и пороги — `.claude/context.json`. В хук Claude Code
заполненность не передаёт, а сам хук не может запустить /compact — только подсказать.
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


#: Хвост журнала, в котором ищем последний ответ: журнал бывает в десятки мегабайт.
TAIL_BYTES = 4 * 1024 * 1024


def used_tokens(transcript: Path) -> int | None:
    """Токены в окне на последнем ответе основной сессии: вход, запись и чтение кэша."""
    with transcript.open("rb") as fh:
        fh.seek(max(0, transcript.stat().st_size - TAIL_BYTES))
        lines = fh.read().decode("utf-8", errors="ignore").splitlines()
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = (entry.get("message") or {}).get("usage")
        if entry.get("type") == "assistant" and usage and not entry.get("isSidechain"):
            return sum(usage.get(k) or 0 for k in
                       ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return None


def context_line(used: int, cfg: dict) -> str:
    """Строка для Claude или пусто, если ниже всех порогов."""
    window = cfg["window_tokens"]
    # вниз, не round: 39,5% — ещё не порог 40%, и показанное число совпадает со сравнением
    percent = 100 * used // window
    passed = [(p, name) for name, p in cfg["thresholds"].items() if percent >= p]
    if not passed:
        return ""
    limit, name = max(passed)
    line = (f"Контекст: {percent}% ({used // 1000}k из {window // 1000}k), порог «{name}» "
            f"({limit}%) — что делать: CLAUDE.md, «Контекст».")
    if percent > 100:
        line += " Больше 100%: окно больше, чем window_tokens в .claude/context.json — поправить."
    return line


def context_hook(root: Path, call: dict) -> str:
    path = Path(call.get("transcript_path") or "")
    if not path.is_file() or (used := used_tokens(path)) is None:
        return ""
    cfg = json.loads((root / ".claude/context.json").read_text(encoding="utf-8"))
    return context_line(used, cfg)


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
    bad += check_context()
    return report(bad, "after-compact: задача по ветке, разделы; is-commit: коммит и не коммит; "
                       "context: токены основной сессии, пороги")


def check_context() -> list[str]:
    cfg = {"window_tokens": 200000, "thresholds": {"new_chat": 40, "compact": 60, "urgent": 80}}
    usage = {"input_tokens": 2, "cache_creation_input_tokens": 1000,
             "cache_read_input_tokens": 129000}
    side = {"usage": {"input_tokens": 9}}
    entries = [{"type": "assistant", "message": {"usage": usage}},
               {"type": "assistant", "isSidechain": True, "message": side},
               {"type": "user", "message": {"content": "x"}}]
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "t.jsonl"
        log.write_text("\n".join(json.dumps(e) for e in entries) + "\n{битая строка\n",
                       encoding="utf-8")
        used = used_tokens(log)
    line = context_line(130002, cfg)
    return [why for ok, why in [
        (used == 130002, f"context: насчитал {used}, а не 130002 (субагент не считается)"),
        (context_line(70000, cfg) == "", "context: ниже порогов должен молчать"),
        (context_line(79000, cfg) == "", "context: 39,5% округлилось до порога 40%"),
        ("65%" in line and "«compact»" in line, f"context: не тот порог: {line!r}"),
        ("Больше 100%" in context_line(260000, cfg), "context: нет подсказки про окно"),
    ] if not ok]


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
    if sys.argv[1:] == ["context"]:
        if line := context_hook(root, json.load(sys.stdin)):
            print(line)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())

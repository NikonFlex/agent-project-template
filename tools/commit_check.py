"""Номер задачи в каждом коммите (docs/SPEC-DRIVEN.md, «Задача и коммит»).

    git ticket 12                       запомнить задачу для этого клона или worktree
    git ticket                          показать запомненную
    python3 tools/commit_check.py --msg <файл>       хук commit-msg: нет ссылки — дописать
                                                     «Implements <префикс>#N» из ticket или отказать
    python3 tools/commit_check.py range <база>..<голова>   CI: все коммиты ветки со ссылкой
    python3 tools/commit_check.py --selftest

Номер берётся из `git ticket`, а не из имени ветки: имя ветки задают руками и путают.
Задача хранится в git-каталоге клона (`.git/ticket`, у worktree — свой), в репозиторий не
попадает. Обход хука `--no-verify` возможен, поэтому то же проверяет CI. Не нужна ссылка
слиянию и коммиту учёта, сообщение которого начинается с «backlog:» (копии задач, журнал).
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from project_config import backlog_config
from script_common import ROOT, git_env, report

EXEMPT = ("Merge ", "backlog:")
ADVICE = "задача не названа: `git ticket <номер>` или «Implements <префикс>#N» в сообщении"


def git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True,  # noqa: S603, S607
                          text=True, check=True, env=git_env())
    return done.stdout.strip()


def ref_pattern(root: Path) -> re.Pattern:
    prefix = backlog_config(root)["prefix"] or ""
    return re.compile(rf"(?<![\w#]){re.escape(prefix)}#\d+")


def ticket_file(root: Path) -> Path:
    return Path(git(root, "rev-parse", "--absolute-git-dir")) / "ticket"


def cmd_ticket(root: Path, number: str | None) -> int:
    path = ticket_file(root)
    if number is None:
        print(path.read_text().strip() if path.exists() else "не запомнена: git ticket <номер>")
        return 0
    prefix = backlog_config(root)["prefix"]
    path.write_text(f"{prefix}#{int(number.lstrip('#'))}\n")
    print(f"задача клона: {path.read_text().strip()} — хук допишет её в коммиты без ссылки")
    return 0


def fix_message(root: Path, message: str) -> tuple[str, str]:
    """Сообщение коммита → (сообщение, отказ). Ссылки нет — дописать из ticket или отказать."""
    if message.startswith(EXEMPT) or ref_pattern(root).search(message):
        return message, ""
    path = ticket_file(root)
    if not path.exists():
        return message, ADVICE
    return message.rstrip("\n") + f"\n\nImplements {path.read_text().strip()}\n", ""


def cmd_msg(root: Path, file: str) -> int:
    path = Path(file)
    message, refusal = fix_message(root, path.read_text(encoding="utf-8"))
    if refusal:
        print(f"коммит остановлен: {refusal}", file=sys.stderr)
        return 1
    path.write_text(message, encoding="utf-8")
    return 0


def range_problems(root: Path, span: str) -> list[str]:
    """Коммиты диапазона без ссылки на задачу; слияния и учёт не в счёт."""
    raw = git(root, "log", "--format=%h%x00%P%x00%B%x1e", span)
    problems = []
    for entry in filter(None, (e.strip("\n") for e in raw.split("\x1e"))):
        sha, parents, body = entry.split("\x00", 2)
        if len(parents.split()) > 1 or body.startswith(EXEMPT):
            continue
        if not ref_pattern(root).search(body):
            problems.append(f"коммит {sha} «{body.splitlines()[0]}» — {ADVICE}")
    return problems


# ---------------------------------------------------------------------------
# selftest: временный репозиторий, коммиты со ссылкой и без
# ---------------------------------------------------------------------------


def commit(root: Path, message: str) -> None:
    (root / "f.txt").write_text(message)
    git(root, "add", "f.txt")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)


def selftest() -> int:
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        git(root, "init", "-q")
        (root / ".backlog.json").write_text('{"prefix": "ab"}')
        commit(root, "начало")
        base = git(root, "rev-parse", "HEAD")
        if fix_message(root, "без ссылки")[1] != ADVICE:
            bad.append("без ticket коммит без ссылки не остановлен")
        ticket_file(root).write_text("ab#12\n")
        fixed, refusal = fix_message(root, "правка\n")
        if refusal or "Implements ab#12" not in fixed:
            bad.append(f"ticket не дописан: {fixed!r}")
        if fix_message(root, "Implements ab#3")[0] != "Implements ab#3":
            bad.append("сообщение со ссылкой изменено")
        commit(root, "правка\n\nImplements ab#12")
        commit(root, "backlog: копии задач")
        if problems := range_problems(root, f"{base}..HEAD"):
            bad.append(f"чистая ветка: {problems}")
        commit(root, "правка без номера")
        commit(root, "ссылка на чужое cd#4")
        found = range_problems(root, f"{base}..HEAD")
        if len(found) != 2 or "правка без номера" not in found[1]:
            bad.append(f"коммиты без номера не пойманы: {found}")
    return report(bad, "ticket дописывается, без него — отказ; CI ловит коммиты без номера")


def main() -> int:
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    if args[:1] == ["--msg"]:
        return cmd_msg(ROOT, args[1])
    if args[:1] == ["ticket"]:
        return cmd_ticket(ROOT, args[1] if len(args) > 1 else None)
    if args[:1] == ["range"]:
        problems = range_problems(ROOT, args[1])
        for problem in problems:
            print(problem)
        print("все коммиты называют задачу" if not problems else f"нарушений: {len(problems)}")
        return 1 if problems else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())

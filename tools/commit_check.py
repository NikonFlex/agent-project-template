"""Формат и номер задачи в каждом коммите (docs/agents/tasks.md, «Коммит»).

    git ticket 12                       запомнить задачу для этого клона или worktree
    git ticket                          показать запомненную
    python3 tools/commit_check.py --msg <файл>       хук commit-msg: заголовок не по форме —
                     отказ; нет ссылки — дописать «Implements <префикс>#N» из ticket или отказ
    python3 tools/commit_check.py range <база>..<голова>   CI: то же для всех коммитов ветки
    python3 tools/commit_check.py --selftest

Заголовок — Conventional Commits 1.0.0: `<тип>[(область)][!]: <описание>`, типы — TYPES.
Номер берётся из `git ticket`, а не из имени ветки: имя ветки задают руками и путают.
Задача хранится в git-каталоге клона (`.git/ticket`, у worktree — свой), в репозиторий не
попадает. Обход хука `--no-verify` возможен, поэтому то же проверяет CI. Сообщения, которые
пишет сам git (слияние, откат, fixup), не проверяются; коммиту учёта `chore(backlog): …`
(копии задач, журнал) и обновлению зависимостей `build(deps): …` ссылка на задачу не нужна.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

from project_config import backlog_config
from script_common import ROOT, output, report

#: Типы заголовка — Conventional Commits и соглашение Angular. Объявлены один раз: хук, CI и
#: подсказка читают отсюда.
TYPES = ("feat", "fix", "docs", "refactor", "perf", "test", "build", "ci", "chore", "style",
         "revert")
HEADER = re.compile(rf"^(?:{'|'.join(TYPES)})(?:\([\w./-]+\))?!?: \S")
#: Сообщения, которые пишет сам git, — без проверки формата и номера.
GIT_MADE = ("Merge ", 'Revert "', "fixup! ", "squash! ", "amend! ")
#: Без номера задачи: учёт (копии задач, журнал) и обновления зависимостей от Dependabot
#: (.github/dependabot.yml) — их заводит не задача.
WITHOUT_TASK = ("chore(backlog): ", "build(deps): ", "build(deps-dev): ")
ADVICE = "задача не названа: `git ticket <номер>` или «Implements <префикс>#N» в сообщении"
FORMAT = (f"заголовок не по форме «<тип>[(область)][!]: <описание>»; типы: {', '.join(TYPES)} "
          "(docs/agents/tasks.md, «Коммит»)")


def header_problem(message: str) -> str:
    """Пусто, если заголовок по форме или сообщение написал git; иначе — что не так."""
    if message.startswith(GIT_MADE):
        return ""
    return "" if HEADER.match(message.splitlines()[0] if message else "") else FORMAT


def git(root: Path, *args: str) -> str:
    return output(root, "git", *args).strip()


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
    if problem := header_problem(message):
        return message, problem
    if message.startswith((*GIT_MADE, *WITHOUT_TASK)) or ref_pattern(root).search(message):
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


def message_problems(root: Path, message: str) -> list[str]:
    """Что не так с сообщением уже сделанного коммита: формат и ссылка на задачу."""
    problems = [p for p in [header_problem(message)] if p]
    exempt = message.startswith((*GIT_MADE, *WITHOUT_TASK))
    return problems + ([] if exempt or ref_pattern(root).search(message) else [ADVICE])


def range_problems(root: Path, span: str) -> list[str]:
    """Коммиты диапазона с заголовком не по форме или без ссылки на задачу; слияния не в счёт."""
    raw = git(root, "log", "--format=%h%x00%P%x00%B%x1e", span)
    problems = []
    for entry in filter(None, (e.strip("\n") for e in raw.split("\x1e"))):
        sha, parents, body = entry.split("\x00", 2)
        if len(parents.split()) > 1:
            continue
        problems += [f"коммит {sha} «{body.splitlines()[0]}» — {p}"
                     for p in message_problems(root, body)]
    return problems


# ---------------------------------------------------------------------------
# selftest: временный репозиторий, коммиты по форме и не по форме
# ---------------------------------------------------------------------------

#: Заголовок → должен ли он пройти проверку формата.
HEADERS = {
    "feat(assignment)!: режим по умолчанию — «Научить»": True,
    "fix: пустая строка источника": True,
    "chore(backlog): копии задач": True,
    'Revert "feat: x"': True,
    "Merge main": True,
    "правка без типа": False,
    "feature: неизвестный тип": False,
    "fix: ": False,
    "fix:без пробела": False,
    "Fix: заглавный тип": False,
}


def commit(root: Path, message: str) -> None:
    (root / "f.txt").write_text(message)
    git(root, "add", "f.txt")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)


def hook_cases(root: Path) -> list[str]:
    bad = [f"формат: «{h}» — {'остановлен' if ok else 'пропущен'} зря"
           for h, ok in HEADERS.items() if (header_problem(h) == "") != ok]
    if fix_message(root, "fix: без ссылки")[1] != ADVICE:
        bad.append("без ticket коммит без ссылки не остановлен")
    if fix_message(root, "правка\n\nImplements ab#3")[1] != FORMAT:
        bad.append("хук пропустил заголовок без типа")
    ticket_file(root).write_text("ab#12\n")
    fixed, refusal = fix_message(root, "fix: правка\n")
    if refusal or "Implements ab#12" not in fixed:
        bad.append(f"ticket не дописан: {fixed!r}")
    if fix_message(root, "docs: x\n\nImplements ab#3")[0] != "docs: x\n\nImplements ab#3":
        bad.append("сообщение со ссылкой изменено")
    if "Implements" in fix_message(root, "chore(backlog): учёт")[0]:
        bad.append("коммиту учёта дописан номер задачи")
    return bad


def range_cases(root: Path, base: str) -> list[str]:
    commit(root, "feat: правка\n\nImplements ab#12")
    commit(root, "chore(backlog): копии задач")
    commit(root, "build(deps): bump actions/checkout from 6 to 7")
    bad = [f"чистая ветка: {p}" for p in range_problems(root, f"{base}..HEAD")]
    commit(root, "fix: правка без номера")
    commit(root, "правка без типа\n\nImplements ab#12")
    found = range_problems(root, f"{base}..HEAD")
    if len(found) != 2 or ADVICE not in found[1] or FORMAT not in found[0]:
        bad.append(f"коммит без номера и коммит без типа не пойманы: {found}")
    return bad


def selftest() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        git(root, "init", "-q")
        (root / ".backlog.json").write_text('{"prefix": "ab"}')
        commit(root, "chore: начало")
        base = git(root, "rev-parse", "HEAD")
        bad = hook_cases(root) + range_cases(root, base)
    return report(bad, f"формат заголовка ({len(HEADERS)} случаев), ticket дописывается, без "
                       "него — отказ; CI ловит коммит без номера и без типа")


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
        print("все коммиты по форме и с задачей" if not problems else f"нарушений: {len(problems)}")
        return 1 if problems else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())

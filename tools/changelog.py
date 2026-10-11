"""CHANGELOG и номер версии из заголовков коммитов — проверка настроек git-cliff (cliff.toml).

    python3 tools/changelog.py             следующая версия и CHANGELOG невыпущенного
    python3 tools/changelog.py --selftest  набор коммитов → разделы и номер версии

Выпуск делает .github/workflows/release.yml по кнопке (docs/shared/SDD.md, «Выпуск версии»);
здесь — что он соберёт. git-cliff ставится из tools/requirements.txt, версия закреплена там.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from script_common import ROOT, output, report, run_script

CONFIG = "cliff.toml"


def cliff(root: Path, *args: str) -> str:
    return output(root, "git-cliff", "--config", str(ROOT / CONFIG), *args)


def show(root: Path) -> int:
    print(f"следующая версия: {cliff(root, '--bumped-version').strip()}\n")
    print(cliff(root, "--unreleased", "--bump", "--strip", "header"))
    return 0


# ---------------------------------------------------------------------------
# selftest: временный репозиторий с коммитами всех видов
# ---------------------------------------------------------------------------

#: Заголовок коммита (с подвалом) → что о нём должно быть в CHANGELOG: строка или None — нет.
COMMITS = {
    "feat(assignment): режим по умолчанию\n\nImplements ab#3":
        "- **assignment:** режим по умолчанию",
    "fix: пустая строка источника\n\nImplements ab#4": "- пустая строка источника",
    "chore(backlog): копии задач": None,
    "правка без типа": None,
    "backlog: тип не из списка": None,
    "Merge pull request #9 from x": None,
}
SECTIONS = ["### Новое", "### Исправлено"]
#: Последняя метка → набор заголовков → ожидаемая версия.
BUMPS = [("v0.1.0", ["fix: a"], "v0.1.1"), ("v0.1.0", ["feat: a"], "v0.1.1"),
         ("v0.1.0", ["feat!: a"], "v0.2.0"), ("v1.2.3", ["fix: a"], "v1.2.4"),
         ("v1.2.3", ["fix: a", "feat: b"], "v1.3.0"), ("v1.2.3", ["fix!: a"], "v2.0.0")]


def repo(tmp: Path, tag: str, messages: list[str]) -> Path:
    def git(*args: str) -> None:
        output(tmp, "git", "-c", "user.name=t", "-c", "user.email=t@t", *args)
    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "chore: начало")
    git("tag", tag)
    for message in messages:
        git("commit", "-q", "--allow-empty", "-m", message)
    return tmp


def selftest() -> int:
    if shutil.which("git-cliff") is None:
        return report(["git-cliff не стоит: pip install -r tools/requirements.txt"], "")
    with tempfile.TemporaryDirectory() as tmp:
        log = cliff(repo(Path(tmp), "v0.1.0", list(COMMITS)), "--unreleased", "--bump")
    bad = [f"нет раздела «{s}»" for s in SECTIONS if s not in log]
    bad += [f"в CHANGELOG нет «{line}»" for line in COMMITS.values() if line and line not in log]
    # в CHANGELOG печатается описание без типа — его и ищем
    bad += [f"в CHANGELOG попало «{m.splitlines()[0]}»" for m, line in COMMITS.items()
            if line is None and m.splitlines()[0].split(": ", 1)[-1] in log]
    bad += ["в CHANGELOG попал подвал Implements"] if "Implements" in log else []
    for tag, messages, wanted in BUMPS:
        with tempfile.TemporaryDirectory() as tmp:
            got = cliff(repo(Path(tmp), tag, messages), "--bumped-version").strip()
        bad += [f"{tag} + {messages} → {got}, а не {wanted}"] if got != wanted else []
    return report(bad, f"разделы, учёт и слияния не в CHANGELOG, подвал не печатается; "
                       f"версия верна в {len(BUMPS)} случаях")


if __name__ == "__main__":
    sys.exit(run_script(selftest, show))

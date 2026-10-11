"""Общее для скриптов процесса в tools/: точка входа, итог самопроверки, печать нарушений."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_script(selftest: Callable[[], int], run: Callable[[Path], int]) -> int:
    """`--selftest` — самопроверка; иначе — работа на корне репозитория."""
    return selftest() if "--selftest" in sys.argv else run(ROOT)


def report(bad: list[str], passed: str) -> int:
    """Печатает упавшие проверки или строку об успехе; код возврата — 1, если что-то упало."""
    for line in bad:
        print("✗ " + line)
    if not bad:
        print("самопроверка пройдена: " + passed)
    return 1 if bad else 0


def print_problems(problems: list[str], clean: str) -> int:
    """Нарушения по строке и итог; код возврата — 1, если они есть."""
    for problem in problems:
        print(problem)
    print(clean if not problems else f"нарушений: {len(problems)}")
    return 1 if problems else 0


def selftest_project(make: Callable[[Path], Path], run: Callable[[Path], list[str]],
                     defects: dict[str, tuple], spoil: Callable[[Path, object], None]) -> int:
    """Чистый проект из `make` без нарушений; каждая порча из `defects` поймана.

    `defects`: название → (порча, кусок ожидаемого сообщения); `spoil` применяет порчу.
    """
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        if problems := run(make(Path(tmp) / "clean")):
            bad.append(f"чистый проект: {problems}")
        for name, (change, expected) in defects.items():
            root = make(Path(tmp) / name)
            spoil(root, change)
            found = run(root)
            if not any(expected in p for p in found):
                bad.append(f"не поймано: {name} — {found}")
    n = len(defects)
    return report(bad, f"чистый проект чист, поймано нарушений {n} из {n}")


def check_root(run: Callable[[Path], list[str]], clean: str) -> Callable[[Path], int]:
    """Проверка корня: первый аргумент без «-» — другой корень (образец в docs/examples)."""
    def check(root: Path) -> int:
        args = [a for a in sys.argv[1:] if not a.startswith("-")]
        return print_problems(run(Path(args[0]) if args else root), clean)
    return check


def write_files(root: Path, files: dict[str, str]) -> Path:
    """Проект для самопроверки: путь → текст."""
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    return root


def replace_once(root: Path, change: tuple[str, str, str]) -> None:
    """Порча для самопроверки: (файл, было, стало); «было» обязано найтись."""
    path, old, new = change
    file = root / path
    text = file.read_text(encoding="utf-8")
    if old not in text:
        raise ValueError(f"selftest: в {path} нет «{old}» — порча не попадает")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")


def git_env() -> dict[str, str]:
    """Окружение для git без GIT_*: внутри git-хука GIT_DIR и GIT_INDEX_FILE указывают на
    репозиторий, который коммитят, и самопроверка во временном каталоге писала бы в него."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def output(cwd: Path, *args: str) -> str:
    """Вывод команды без GIT_* в окружении; ненулевой код — исключение."""
    done = subprocess.run(args, cwd=cwd, capture_output=True, text=True,  # noqa: S603
                          check=True, env=git_env())
    return done.stdout

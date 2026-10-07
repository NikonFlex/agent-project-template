"""Общее для скриптов процесса в tools/: точка входа, итог самопроверки, печать нарушений."""

from __future__ import annotations

import sys
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

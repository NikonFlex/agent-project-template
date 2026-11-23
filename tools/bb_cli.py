"""Общий вход CLI для инструментов bb (bb-tree, bb-arena, nightly-автоматизация).

Выносит каркас `argparse` + флаг `--selftest` + диспетчер, чтобы три инструмента не
копировали `def main()` друг у друга (копипасту ловит jscpd). Инструмент даёт только
описание, свои флаги (`add_args`) и тело (`body`); `--selftest` добавляется здесь.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable


def main(description: str, selftest: Callable[[], int],
         add_args: Callable[[argparse.ArgumentParser], None],
         body: Callable[[argparse.Namespace], int]) -> int:
    """Разобрать флаги и либо прогнать `selftest()`, либо отдать аргументы в `body`."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--selftest", action="store_true", help="самопроверка без сети")
    add_args(parser)
    args = parser.parse_args()
    return selftest() if args.selftest else body(args)

"""Публичный вход: `python3 -m tools.rundo run <номер> [--request-file FILE] [--jevstice]`.

Запускается из текущего BB-треда; берёт `BB_PROJECT_ID` и `BB_THREAD_ID` из
окружения. Итог — локальные артефакты прогона и короткий отчёт; GitHub не трогается.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from tools.rundo.artifacts import ArtifactError
from tools.rundo.bb import BbFault
from tools.rundo.config import JEVSTICE_ENV
from tools.rundo.evidence import EvidenceError
from tools.rundo.pipeline import Pipeline, PipelineError

FAILURES = (
    PipelineError, BbFault, ArtifactError, EvidenceError, subprocess.TimeoutExpired,
    ValueError, KeyError, OSError,
)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return _run(Path.cwd(), args.number, args.request_file, args.jevstice)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m tools.rundo",
        description="Рундо — агентский пайплайн для задач проекта с проверками Jev v2",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run", help="провести задачу по стадиям и сохранить артефакты",
        epilog=(
            "Для проверки UI задайте RUNDO_BRAILLE_BASE (адрес локального "
            "стенда) и, при необходимости, RUNDO_BRAILLE_STEPS (JSON-список шагов). "
            "Контроллер запускает braille сам, команды из артефактов модели не исполняются."
        ),
    )
    run.add_argument("number", help="номер задачи, например 42")
    run.add_argument("--request-file", help="точная постановка вместо раздела файла задачи")
    run.add_argument("--jevstice", action="store_true", help="включить диагностику Jevstice")
    return parser


def _run(repo: Path, raw_number: str, request_file: str | None, jevstice: bool) -> int:
    number = raw_number.strip().lstrip("#")
    if not number.isdigit():
        raise SystemExit(f"номер задачи должен быть числом, получено {raw_number!r}")
    env = dict(os.environ)
    env.pop(JEVSTICE_ENV, None)
    if jevstice:
        env[JEVSTICE_ENV] = "1"
    pipeline = Pipeline(repo, number, request_file, env)
    try:
        return pipeline.run()
    except FAILURES as error:
        _print_failure(pipeline, error)
        return 1


def _print_failure(pipeline: Pipeline, error: Exception) -> None:
    print(f"#{pipeline.number}: остановлен на стадии {pipeline.state.stage}", file=sys.stderr)
    for label, value in (
        ("роль", getattr(error, "role", None)),
        ("тред", getattr(error, "thread", None)),
        ("файл", getattr(error, "file", None)),
    ):
        if value:
            print(f"{label}: {value}", file=sys.stderr)
    print(f"причина: {error}", file=sys.stderr)
    if pipeline.store.path.is_file():
        print(f"состояние: {pipeline.store.path}", file=sys.stderr)

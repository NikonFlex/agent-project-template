"""Командная строка исполнителя шагов.

    python3 -m braille start --base URL [--session DIR] [--viewport desktop|phone] [--values FILE]
    python3 -m braille step '<json шага>' | look | close  [--session DIR]
    python3 -m braille selftest

Исполнитель живёт в отдельном процессе (`serve`), клиент говорит с ним файлами в каталоге
сессии. Нужен Python с playwright (BRAILLE_PYTHON); клиент и selftest обходятся без него.
Язык шагов и ответ — braille/README.md, раздел «Исполнитель шагов».
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from braille.session import ask, read_json
from braille.settings import ROOT, Settings, load
from braille.steps import HostRefused, check_base

SESSIONS = ROOT / "tmp" / "braille"
POINTER = SESSIONS / "current"
ENV_PYTHON = "BRAILLE_PYTHON"
EXIT_USAGE = 2


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(prog="python3 -m braille", description=__doc__,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = main.add_subparsers(dest="command", required=True)
    for name in ("start", "serve"):
        sub = commands.add_parser(
            name, help="открыть сессию" if name == "start" else argparse.SUPPRESS)
        sub.add_argument("--base", required=True, help="адрес сайта: localhost, 127.0.0.1")
        sub.add_argument("--session", help="каталог сессии (по умолчанию tmp/braille/session-…)")
        sub.add_argument("--viewport", default="desktop", choices=("desktop", "phone"))
        sub.add_argument("--values", help="JSON {имя: значение} для подстановок {имя}")
    step = commands.add_parser("step", help="выполнить один шаг")
    step.add_argument("json", help="шаг, например '{\"goto\": \"/orders\"}'")
    for name in ("look", "close"):
        commands.add_parser(name, help="весь экран без шага" if name == "look"
                            else "закрыть и записать steps.json")
    commands.add_parser("selftest", help="контрольные случаи без браузера и сети")
    for sub in commands.choices.values():
        if sub.prog.split()[-1] in ("step", "look", "close"):
            sub.add_argument("--session", help="каталог сессии (по умолчанию — последняя)")
    return main


def say(body: dict) -> int:
    print(json.dumps(body, ensure_ascii=False, indent=1))
    return 0 if body.get("ok") else 1


def current_session(given: str | None) -> Path | None:
    if given:
        return Path(given).resolve()
    return Path(POINTER.read_text(encoding="utf-8").strip()) if POINTER.exists() else None


def executor_python() -> str:
    """Python, в котором есть playwright: BRAILLE_PYTHON или тот, что запустил команду."""
    if os.environ.get(ENV_PYTHON):
        return os.environ[ENV_PYTHON]
    if importlib.util.find_spec("playwright"):
        return sys.executable
    raise HostRefused("нужен Python с playwright: задайте BRAILLE_PYTHON, например "
                      "<venv>/bin/python")


def start(args: argparse.Namespace, settings: Settings, point: bool = True) -> dict:
    """Открывает сессию; `point` — записать её указателем «последней» для step/look/close.

    Сценарии (tools/scenario.py, шаг ui) указатель не трогают: у человека может идти своя сессия.
    """
    try:
        base = check_base(args.base, settings)
        python = executor_python()
    except HostRefused as error:
        return {"ok": False, "error": str(error)}
    directory = Path(args.session).resolve() if args.session else (
        SESSIONS / f"session-{int(time.time())}")
    directory.mkdir(parents=True, exist_ok=True)
    command = [python, "-m", "braille", "serve", "--base", base, "--session", str(directory),
               "--viewport", args.viewport, *(["--values", args.values] if args.values else [])]
    with (directory / "executor.log").open("w") as log:
        subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log,  # noqa: S603
                         start_new_session=True)
    if point:
        POINTER.parent.mkdir(parents=True, exist_ok=True)
        POINTER.write_text(str(directory), encoding="utf-8")
    return wait_ready(directory, settings)


def wait_ready(directory: Path, settings: Settings) -> dict:
    deadline = time.monotonic() + settings.session["start_timeout_s"]
    while time.monotonic() < deadline:
        state = (read_json(directory / "state.json") or {}).get("state")
        if state == "ready":
            return {"ok": True, "session": str(directory), "next": 'первый шаг: {"goto": "/…"}'}
        if state == "failed":
            break
        time.sleep(settings.session["poll_ms"] / 1000)
    log = (directory / "executor.log").read_text(encoding="utf-8")[-1500:]
    state = read_json(directory / "state.json") or {}
    return {"ok": False, "error": state.get("reason") or "сессия не стартовала", "log": log}


def serve(args: argparse.Namespace, settings: Settings) -> int:
    """Процесс исполнителя: браузер, цикл сессии, закрытие. Запускается командой start."""
    from braille.browser import open_live
    from braille.executor import Executor
    from braille.session import S, set_state
    from braille.session import serve as run_session

    directory = Path(args.session)
    values = read_json(Path(args.values)) if args.values else {}
    secrets = tuple(str(v) for v in (values or {}).values())
    try:
        live = open_live(args.base, args.viewport, settings, secrets)
    except Exception as error:  # noqa: BLE001 — любой отказ запуска виден в state.json
        state = set_state(directory, None, S.STARTING)
        set_state(directory, state, S.FAILED, reason=str(error)[:300])
        return 1
    try:
        run_session(directory, values or {}, Executor(live, directory / "out"), settings)
    finally:
        live.close()
    return 0


def step_timeout(step: dict, settings: Settings) -> float:
    """Сколько клиент ждёт ответа на шаг: срок самого шага и запас на исполнителя."""
    conf = settings.session
    within = (step.get("after") or {}).get("within") or step.get("within") or 0
    return max(conf["step_default_s"], within + conf["step_slack_s"])


def client(args: argparse.Namespace, settings: Settings) -> int:
    directory = current_session(args.session)
    if directory is None:
        return say({"ok": False, "error": "сессии нет: сначала `python3 -m braille start`"})
    if args.command != "step":
        return say(ask(directory, {args.command: True}, 60, settings))
    try:
        step = json.loads(args.json)
    except ValueError as error:
        return say({"ok": False, "error": f"шаг — JSON-объект: {error}"})
    return say(ask(directory, {"step": step}, step_timeout(step, settings), settings))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    settings = load()
    if args.command == "selftest":
        from braille.selftest import selftest
        return selftest(settings)
    if args.command == "start":
        return say(start(args, settings))
    if args.command == "serve":
        return serve(args, settings)
    return client(args, settings)


if __name__ == "__main__":
    raise SystemExit(main())

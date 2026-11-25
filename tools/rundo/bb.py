"""Одна точка вызова `bb`: argv, JSON-разбор, журнал и понятная ошибка.

Все команды пайплайна идут через этот клиент; ничего другого контроллер не запускает.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tools.rundo.config import Actor

SPAWN_TIMEOUT_SECONDS = 900
WAIT_TIMEOUT_SECONDS = 7300
WAIT_STATUS = "7200s"
COMMAND_TIMEOUT_SECONDS = 900


class BbFault(RuntimeError):
    def __init__(self, message: str, argv: Sequence[str] | None = None) -> None:
        super().__init__(message)
        self.argv = list(argv or [])


class Bb:
    def __init__(self, log_path: Path, env: Mapping[str, str]) -> None:
        self.log_path = log_path
        self.env = dict(env)

    def _log(self, argv: Sequence[str]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"argv": list(argv)}, ensure_ascii=False) + "\n"
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(line)

    def _run(self, argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
        self._log(argv)
        return subprocess.run(  # noqa: S603, S607 — argv собирает контроллер, `bb` из PATH
            ["bb", *argv], capture_output=True, text=True, timeout=timeout,  # noqa: S607
            encoding="utf-8", errors="replace", env=self.env, check=False,
        )

    def json(self, argv: Sequence[str], timeout: int = COMMAND_TIMEOUT_SECONDS) -> Any:
        done = self._run(argv, timeout)
        data = _parse_stdout(done)
        failed = done.returncode != 0 or (isinstance(data, dict) and data.get("ok") is False)
        if failed:
            raise BbFault(_fault_message(data, done), argv)
        return data

    def spawn(
        self, actor: Actor, title: str, prompt_file: Path, project: str, parent: str,
        environment: str,
    ) -> dict:
        argv = [
            "thread", "spawn", "--project", project, "--parent-thread", parent,
            "--environment", environment, "--provider", actor.provider,
            "--model", actor.model, "--permission-mode", "accept-edits", "--visibility", "visible",
            "--parent-notifications", "off", "--title", title,
            "--prompt-file", str(prompt_file), "--json",
        ]
        return self.json(argv, SPAWN_TIMEOUT_SECONDS)

    def wait_idle(self, thread_id: str) -> dict:
        return self.json([
            "thread", "wait", thread_id, "--status", "idle",
            "--timeout", WAIT_STATUS, "--json",
        ], WAIT_TIMEOUT_SECONDS)

    def thread_show(self, thread_id: str) -> dict:
        value = self.json(["thread", "show", thread_id, "--json"])
        if not isinstance(value, dict):
            raise BbFault(f"bb thread show {thread_id}: ответ не объект",
                          ["thread", "show", thread_id])
        return value

    def threads_by_id(self, thread_id: str) -> list[dict]:
        value = self.json(["thread", "list", "--thread", thread_id, "--json"])
        if not isinstance(value, list):
            raise BbFault("bb thread list --json вернул не список", ["thread", "list"])
        return value

    def children(self, project: str, parent: str) -> list[dict]:
        value = self.json([
            "thread", "list", "--project", project, "--parent-thread", parent, "--json",
        ])
        if not isinstance(value, list):
            raise BbFault("bb thread list --json вернул не список", ["thread", "list"])
        return value

    def environment(self, environment_id: str) -> dict:
        return self.json(["environment", "show", environment_id, "--json"])

    def rpc(self, method: str, input_file: Path, *, plugin: str = "bb-jev") -> dict:
        value = self.json([
            "plugin", "rpc", "call", plugin, method,
            "--input-file", str(input_file), "--json",
        ])
        if not isinstance(value, dict):
            raise BbFault(f"RPC {method}: ответ не объект", ["plugin", "rpc", "call", method])
        return value


def _parse_stdout(done: subprocess.CompletedProcess[str]) -> Any:
    text = done.stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise BbFault(f"bb вернул не JSON: {error}: {text[:500]}") from error


def _fault_message(data: Any, done: subprocess.CompletedProcess[str]) -> str:
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        error = data["error"]
        code = error.get("code") or "error"
        message = error.get("message") or ""
        hint = error.get("hint") or ""
        return f"bb: {code}: {message} {hint}".strip()
    return f"bb завершился с кодом {done.returncode}: {done.stderr.strip()[:1000]}"

"""Самопроверка раннера сценариев (`python3 tools/scenario.py --selftest`): без стенда и сети.

Поднимает http-заглушку на свободном порту, гоняет на ней сценарий входа и запроса с токеном,
портит ожидание — сценарий обязан покраснеть; проверяет отказы до запуска, контрольный
сценарий и хеш дерева кода (правка теста его не меняет, правка кода — меняет).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from scenario import CONTROL, RECEIPT_CONFIG, Mode, load, run_scenario, tree_hash, validate
from script_common import ROOT, git_env, report


class Stub(BaseHTTPRequestHandler):
    """POST /auth/login → токен; GET /me с этим токеном → профиль; без него — 401."""

    def do_POST(self) -> None:  # noqa: N802 — имя задаёт http.server
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ok = body.get("password") == "secret"
        self.answer(200 if ok else 401, {"access_token": "t-1"} if ok else {"error": "нет"})

    def do_GET(self) -> None:  # noqa: N802
        ok = self.headers.get("Authorization") == "Bearer t-1"
        self.answer(200 if ok else 401, {"email": "anna@example.com", "roles": ["автор"]})

    def answer(self, status: int, data: dict) -> None:
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: object) -> None:
        """Тишина: вывод самопроверки — только итог."""


SCENARIO = {
    "name": "login-me", "зачем": "Вход и профиль по токену.", "требования": ["auth: Вход"],
    "steps": [
        {"kind": "http", "method": "POST", "path": "/auth/login",
         "body": {"email": "{admin_email}", "password": "secret"},
         "expect_status": 200, "extract": {"token": "$.access_token"}},
        {"kind": "http", "method": "GET", "path": "/me",
         "headers": {"Authorization": "Bearer {token}"}, "expect_status": 200},
    ],
    "expect": [
        {"kind": "field", "name": "почта из профиля", "path": "$.email",
         "expect": "anna@example.com"},
        {"kind": "field", "name": "первая роль", "path": "$.roles.0", "expect": "автор"},
        {"kind": "no_field", "name": "пароля в ответе нет", "path": "$.password"},
    ],
}


def against_stub() -> list[str]:
    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stand = {"base_url": f"http://127.0.0.1:{server.server_port}"}
    os.environ["admin_email"] = "anna@example.com"
    bad = []
    try:
        if failure := run_scenario(SCENARIO, stand, Mode.REPLAY):
            bad.append(f"сценарий на заглушке не прошёл: {failure}")
        spoiled = json.loads(json.dumps(SCENARIO))
        spoiled["expect"][0]["expect"] = "boris@example.com"
        if "почта из профиля" not in run_scenario(spoiled, stand, Mode.REPLAY):
            bad.append("испорченное ожидание не покраснело")
        spoiled = json.loads(json.dumps(SCENARIO))
        spoiled["steps"][0]["body"]["password"] = "wrong"  # noqa: S105 — не ключ, порча входа
        if "код 401" not in run_scenario(spoiled, stand, Mode.REPLAY):
            bad.append("неверный код ответа не покраснел")
    finally:
        server.shutdown()
    return bad


def refusals() -> list[str]:
    """Отказ до запуска: незнакомый вид, нет поля, стенд не описывает нужное."""
    cases = {
        "незнакомый вид шага": ({"kind": "ftp"}, "неизвестен"),
        "нет поля шага": ({"kind": "http", "method": "GET"}, "нет поля «path»"),
        "стенд без базы": ({"kind": "wait", "db": "main", "query": "q", "until": ["x"],
                            "timeout_s": 1}, "нет «db»"),
    }
    bad = []
    for name, (step, expected) in cases.items():
        scenario = {**SCENARIO, "steps": [step]}
        if not any(expected in p for p in validate(scenario, {"base_url": "x"})):
            bad.append(f"не отказал: {name}")
    if run_scenario({**SCENARIO, "steps": [{**SCENARIO["steps"][0], "path": "/{nope}"}]},
                    {"base_url": "http://127.0.0.1:9"}, Mode.REPLAY).find("{nope}") < 0:
        bad.append("незаданная переменная не остановила прогон")
    return bad


def control() -> list[str]:
    scenario = load(ROOT, CONTROL)
    failure = run_scenario(scenario, {}, Mode.REPLAY)
    return [] if failure and not validate(scenario, {}) else ["контрольный сценарий не покраснел"]


def receipt_hash() -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "tools").mkdir()
        (root / RECEIPT_CONFIG).write_text(json.dumps({"exclude": ["tests/**", "*.md"]}))
        (root / "app.py").write_text("x = 1\n")
        (root / "tests").mkdir()
        (root / "tests/test_app.py").write_text("assert True\n")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=git_env())  # noqa: S607
        first = tree_hash(root)
        (root / "tests/test_app.py").write_text("assert 1\n")
        (root / "README.md").write_text("доки\n")
        same = tree_hash(root)
        (root / "app.py").write_text("x = 2\n")
        changed = tree_hash(root)
    return ([] if first == same else ["правка теста или документа сбросила квитанцию"]) + (
        [] if changed != first else ["правка кода не сменила хеш дерева"])


def selftest() -> int:
    bad = against_stub() + refusals() + control() + receipt_hash()
    return report(bad, "сценарий на заглушке, порча ожидания и кода ответа, отказы до запуска, "
                       "контрольный сценарий, хеш дерева")

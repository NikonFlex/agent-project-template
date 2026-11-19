#!/usr/bin/env python3
"""Отправка в темы Telegram-группы проекта.

    python3 tools/tg_send.py discover [--save]          id группы, номера тем, id отправителей
    python3 tools/tg_send.py send --topic T --file F    текст файла в тему; --thread N — проба
    python3 tools/tg_send.py send-file --topic T --file F [--caption C]   сам файл в тему
    python3 tools/tg_send.py --selftest

Темы группы — `Topic`: название темы в Telegram и номер в TELEGRAM_THREAD_<ИМЯ>. `discover`
читает `getUpdates` без `offset` (ничего не подтверждает) и показывает, где кто писал;
`--save` по названиям тем пишет в `.env` TELEGRAM_CHAT_ID, TELEGRAM_OWNER_ID и номера тем —
так id не идут через чат и не набираются руками.

Адрес — переменные окружения или `.env` (в worktree — `.env` основного клона):
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_OWNER_ID, TELEGRAM_THREAD_<ИМЯ>;
TELEGRAM_API_BASE — подставной сервер для проверки. Без токена — предупреждение и выход 0:
тот же скрипт запускают CI и машины, где бота нет.
"""

from __future__ import annotations

import argparse
import dataclasses
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.parser import BytesParser
from email.policy import HTTP
from enum import StrEnum
from pathlib import Path

from project_config import git_dir
from script_common import report, run_script

API_BASE = "https://api.telegram.org"
# Паузы между повторами при обрыве, 429 и 5xx — как в telegram-send dgtlU; 429 со своим
# `retry_after` ждёт столько, сколько попросил Telegram.
RETRY_PAUSES = (5, 15, 40)
TIMEOUT = 30
PAGE = 100  # больше getUpdates за вызов не отдаёт
LIMIT = 4096  # длина текста sendMessage: 1–4096 знаков


class Topic(StrEnum):
    """Темы группы; номер темы — в переменной TELEGRAM_THREAD_<ИМЯ>."""

    INBOX = "inbox"
    ANALYSIS = "analysis"
    QUESTIONS = "questions"
    TASKS = "tasks"
    RESULTS = "results"
    CLAUDE = "claude"

    @property
    def env(self) -> str:
        return f"TELEGRAM_THREAD_{self.name}"

    @property
    def title(self) -> str:
        """Название темы в группе — по нему `discover --save` узнаёт номер."""
        return TITLES[self]


TITLES = {Topic.INBOX: "Входящие", Topic.ANALYSIS: "Разбор", Topic.QUESTIONS: "Вопросы",
          Topic.TASKS: "Задачи и PR", Topic.RESULTS: "Результаты", Topic.CLAUDE: "Claude"}


class TelegramError(Exception):
    def __init__(self, code: int, description: str, retry_after: int | None = None):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.code == 429 or self.code >= 500


@dataclasses.dataclass(frozen=True)
class Config:
    token: str
    chat_id: int | None = None
    owner_id: int | None = None
    threads: dict[Topic, int] = dataclasses.field(default_factory=dict)
    base: str = API_BASE
    pauses: tuple[float, ...] = RETRY_PAUSES


def env_file(root: Path) -> Path:
    """`.env` клона; в worktree его нет — тогда `.env` основного клона (ключи одни на всех)."""
    own = root / ".env"
    common = git_dir(root) / "commondir"
    if own.exists() or not common.exists():
        return own
    return (git_dir(root) / common.read_text(encoding="utf-8").strip()).resolve().parent / ".env"


def dotenv(path: Path) -> dict[str, str]:
    """`.env` — строки КЛЮЧ=значение; комментарии и пустые строки пропускаются."""
    if not path.exists():
        return {}
    pairs = (line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
             if "=" in line and not line.lstrip().startswith("#"))
    return {key.strip(): value.strip().strip("'\"") for key, value in pairs}


def save_env(path: Path, values: dict[str, str]) -> None:
    """Заменить или дописать ключи `values` в `.env`; прочие строки — как были."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    left = dict(values)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in left:
            lines[i] = f"{key}={left.pop(key)}"
    lines += [f"{key}={value}" for key, value in left.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def number(env: dict[str, str], key: str) -> int | None:
    return int(env[key]) if env.get(key) else None


def load_config(env: dict[str, str]) -> Config | None:
    if not env.get("TELEGRAM_BOT_TOKEN"):
        return None
    threads = {topic: number(env, topic.env) for topic in Topic if env.get(topic.env)}
    base = env.get("TELEGRAM_API_BASE") or API_BASE
    if urllib.parse.urlsplit(base).scheme not in ("http", "https"):
        raise ValueError(f"TELEGRAM_API_BASE: ожидался http(s), а не {base}")
    return Config(env["TELEGRAM_BOT_TOKEN"], number(env, "TELEGRAM_CHAT_ID"),
                  number(env, "TELEGRAM_OWNER_ID"), threads, base)


# ---------------------------------------------------------------------------
# Bot API
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Upload:
    """Файл для метода с вложением: поле формы, имя и содержимое."""

    field: str
    name: str
    data: bytes


def multipart(params: dict, upload: Upload) -> tuple[bytes, str]:
    """Тело multipart/form-data: поля params строками и файл — так Bot API принимает вложение."""
    boundary = uuid.uuid4().hex
    # кодировка поля явно: подпись по-русски без неё читается как latin-1
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n'
             f'Content-Type: text/plain; charset=utf-8\r\n\r\n{value}\r\n'.encode()
             for key, value in params.items() if value is not None]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{upload.field}"; '
                 f'filename="{upload.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'
                 .encode() + upload.data + b"\r\n")
    return b"".join(parts) + f"--{boundary}--\r\n".encode(), \
        f"multipart/form-data; boundary={boundary}"


def call(cfg: Config, method: str, params: dict, upload: Upload | None = None) -> object:
    if upload is None:
        body, kind = json.dumps(params).encode(), "application/json"
    else:
        body, kind = multipart(params, upload)
    # схема адреса проверена в load_config: только http(s)
    request = urllib.request.Request(f"{cfg.base}/bot{cfg.token}/{method}",  # noqa: S310
                                     body, {"Content-Type": kind})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            body = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            body = json.load(error)
        except ValueError:
            raise TelegramError(error.code, str(error.reason)) from error
    if not body.get("ok"):
        retry_after = body.get("parameters", {}).get("retry_after")
        raise TelegramError(body.get("error_code", 0), body.get("description", ""), retry_after)
    return body["result"]


def call_with_retries(cfg: Config, method: str, params: dict,
                      upload: Upload | None = None) -> object:
    """Обрыв, 429 и 5xx — повтор; отказ вроде «chat not found» — сразу наверх."""
    for pause in (*cfg.pauses, None):
        try:
            return call(cfg, method, params, upload)
        except TelegramError as error:
            if not error.retryable or pause is None:
                raise
            wait, reason = error.retry_after or pause, error
        except OSError as error:
            if pause is None:
                raise
            wait, reason = pause, error
        print(f"повтор через {wait} с: {reason}", file=sys.stderr)
        time.sleep(wait)
    raise AssertionError("цикл повторов кончается return или raise")


def chunks(text: str) -> list[str]:
    """Куски не длиннее LIMIT: режет по строкам, слишком длинную строку — по LIMIT."""
    parts = [""]
    for line in text.splitlines(keepends=True):
        for piece in (line[i:i + LIMIT] for i in range(0, len(line), LIMIT)):
            if len(parts[-1]) + len(piece) > LIMIT:
                parts.append("")
            parts[-1] += piece
    return [part for part in parts if part.strip()]


def send(cfg: Config, thread: int | None, text: str) -> None:
    for part in chunks(text):
        call_with_retries(cfg, "sendMessage",
                          {"chat_id": cfg.chat_id, "message_thread_id": thread, "text": part})


def send_file(cfg: Config, thread: int | None, path: Path, caption: str | None = None) -> None:
    """Файл документом в тему: таблицы и сводки, которые хозяин смотрит с телефона."""
    call_with_retries(cfg, "sendDocument",
                      {"chat_id": cfg.chat_id, "message_thread_id": thread, "caption": caption},
                      Upload("document", path.name, path.read_bytes()))


# ---------------------------------------------------------------------------
# discover: где кто писал
# ---------------------------------------------------------------------------


def topic_name(message: dict) -> str | None:
    """Название темы: у сообщения в теме `reply_to_message` — её корень с `forum_topic_created`;
    у самого корня — `forum_topic_created`, у переименования — `forum_topic_edited`."""
    for source in (message, message.get("reply_to_message") or {}):
        for field in ("forum_topic_edited", "forum_topic_created"):
            if (source.get(field) or {}).get("name"):
                return source[field]["name"]
    return None


def seen(cfg: Config) -> list[dict]:
    """Без `offset` getUpdates ничего не подтверждает — пробу можно гонять сколько угодно."""
    out = []
    for update in call(cfg, "getUpdates", {"timeout": 0, "limit": PAGE}):
        message = update.get("message") or {}
        if message:
            sender = message.get("from", {})
            out.append({"chat": message["chat"]["id"], "title": message["chat"].get("title", ""),
                        "thread": message.get("message_thread_id"), "topic": topic_name(message),
                        "sender": sender.get("id"), "bot": sender.get("is_bot", False),
                        "name": sender.get("first_name", ""),
                        "text": (message.get("text") or "")[:40]})
    return out


def describe(found: list[dict]) -> list[str]:
    return list(dict.fromkeys(
        f"чат {m['chat']} «{m['title']}» · тема {m['thread'] or '—'} «{m['topic'] or '?'}» · "
        f"от {m['sender']} {m['name']} · {m['text']}" for m in found))


def env_values(found: list[dict]) -> tuple[dict[str, str], list[str]]:
    """Что записать в `.env` по увиденному и чего не хватает. Группа — одна; хозяин —
    единственный человек, писавший в неё; тема — по названию из `TITLES`."""
    groups = {m["chat"] for m in found if m["chat"] < 0}
    if len(groups) != 1:
        return {}, [f"групп в апдейтах {len(groups)}, нужна одна: напишите в каждую тему группы"]
    chat = groups.pop()
    mine = [m for m in found if m["chat"] == chat]
    values = {"TELEGRAM_CHAT_ID": str(chat)}
    people = {m["sender"] for m in mine if not m["bot"]}
    if len(people) == 1:
        values["TELEGRAM_OWNER_ID"] = str(people.pop())
    by_name = {m["topic"].strip().casefold(): m["thread"] for m in mine
               if m["topic"] and m["thread"]}
    gaps = [] if "TELEGRAM_OWNER_ID" in values else [f"людей в группе {len(people)}, ждали одного"]
    for topic in Topic:
        thread = by_name.get(topic.title.casefold())
        if thread:
            values[topic.env] = str(thread)
        else:
            gaps.append(f"тема «{topic.title}» не найдена: напишите в неё и повторите")
    return values, gaps


# ---------------------------------------------------------------------------
# Командная строка
# ---------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="отправка в темы Telegram-группы")
    commands = cli.add_subparsers(dest="command", required=True)
    commands.add_parser("discover").add_argument(
        "--save", action="store_true", help="записать id группы, хозяина и номера тем в .env")
    for name in ("send", "send-file"):
        sender = commands.add_parser(name)
        sender.add_argument("--file", type=Path, required=True)
        where = sender.add_mutually_exclusive_group(required=True)
        where.add_argument("--topic", type=Topic, choices=list(Topic))
        where.add_argument("--thread", type=int)
    sender.add_argument("--caption", help="подпись к файлу")
    return cli


def missing(cfg: Config, args: argparse.Namespace) -> list[str]:
    need = {"TELEGRAM_CHAT_ID": cfg.chat_id}
    if getattr(args, "topic", None):
        need[args.topic.env] = cfg.threads.get(args.topic)
    return [] if args.command == "discover" else [k for k, v in need.items() if v is None]


def run_discover(cfg: Config, save_to: Path | None) -> int:
    found = seen(cfg)
    print("\n".join(describe(found)) or "апдейтов нет: напишите в каждую тему и повторите")
    if save_to is None:
        return 0
    values, gaps = env_values(found)
    for gap in gaps:
        print("не хватает: " + gap, file=sys.stderr)
    if values:
        save_env(save_to, values)
        print(f"записано в {save_to}: " + ", ".join(f"{k}={v}" for k, v in values.items()))
    return 1 if gaps else 0


def execute(cfg: Config, args: argparse.Namespace, env_path: Path) -> int:
    if args.command == "discover":
        return run_discover(cfg, env_path if args.save else None)
    thread = cfg.threads[args.topic] if args.topic else args.thread
    if args.command == "send-file":
        send_file(cfg, thread, args.file, args.caption)
    else:
        send(cfg, thread, args.file.read_text(encoding="utf-8"))
    print("отправлено")
    return 0


def cli(argv: list[str], env: dict[str, str], env_path: Path) -> int:
    args = parser().parse_args(argv)
    cfg = load_config(env)
    if cfg is None:
        print("предупреждение: нет TELEGRAM_BOT_TOKEN — ничего не делаю", file=sys.stderr)
        return 0
    if gaps := missing(cfg, args):
        print("не заданы: " + ", ".join(gaps), file=sys.stderr)
        return 1
    try:
        return execute(cfg, args, env_path)
    except TelegramError as error:
        print(f"Telegram отказал: {error}", file=sys.stderr)
        return 1


def run(root: Path) -> int:
    path = env_file(root)
    return cli(sys.argv[1:], {**dotenv(path), **os.environ}, path)


# ---------------------------------------------------------------------------
# selftest: подставной Telegram на 127.0.0.1 — повторы, куски, файл, discover, без токена
# ---------------------------------------------------------------------------

OWNER, CHAT, TASKS, CLAUDE, BOT = 7, -100500, 11, 12, 99
FILE_BYTES = b"%PDF-1.4 fake"


class FakeTelegram:
    """Подставной Bot API для самопроверок `tg_send`, `tg_inbox`, `tg_export`: `offset`
    подтверждает и выбрасывает апдейты, что младше; пишет только в темы `threads`."""

    def __init__(self, threads: tuple[int, ...] = (TASKS, CLAUDE)) -> None:
        self.threads = threads
        self.updates: list[dict] = []
        self.sent: list[dict] = []
        self.calls: list[str] = []
        self.failures: list[tuple[int, dict]] = []  # отдать по очереди до ответа по существу

    def answer(self, method: str, params: dict) -> tuple[int, dict]:
        self.calls.append(method)
        if self.failures:
            return self.failures.pop(0)
        if method == "getFile":
            return self.file_info(params["file_id"])
        if method == "getUpdates":
            if params.get("offset") is not None:
                self.updates = [u for u in self.updates if u["update_id"] >= params["offset"]]
            return 200, {"ok": True, "result": self.updates[:params.get("limit", PAGE)]}
        if params.get("message_thread_id") not in self.threads:
            return 400, {"ok": False, "error_code": 400, "description": "message thread not found"}
        self.sent.append(params)
        return 200, {"ok": True, "result": {}}

    @staticmethod
    def file_info(file_id: str) -> tuple[int, dict]:
        """Файл BIG — больше лимита скачивания, остальные отдаются как FILE_BYTES."""
        if file_id == "BIG":
            return 400, {"ok": False, "error_code": 400, "description": "file is too big"}
        return 200, {"ok": True, "result": {"file_path": f"documents/{file_id}.pdf"}}

    def serve(self) -> http.server.ThreadingHTTPServer:
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # имя метода задаёт http.server
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                kind = self.headers["Content-Type"]
                params = form(kind, raw) if kind.startswith("multipart/") else json.loads(raw)
                status, body = fake.answer(self.path.rsplit("/", 1)[-1], params)
                self.send_response(status)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_GET(self) -> None:  # скачивание файла: /file/bot<token>/<file_path>
                self.send_response(200)
                self.end_headers()
                self.wfile.write(FILE_BYTES)

            def log_message(self, *args: object) -> None:
                """Тише: журнал запросов самопроверке не нужен."""

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


def form(kind: str, raw: bytes) -> dict:
    """Разбор multipart/form-data, как его разбирает Telegram: числа — числами, файл — имя и
    байты."""
    whole = BytesParser(policy=HTTP).parsebytes(f"Content-Type: {kind}\r\n\r\n".encode() + raw)
    out: dict = {}
    for part in whole.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if part.get_filename():
            out[name] = {"name": part.get_filename(), "data": part.get_payload(decode=True)}
        else:
            value = part.get_content().strip()
            out[name] = int(value) if value.lstrip("-").isdigit() else value
    return out


def in_topic(thread: int, name: str, sender: int = OWNER, **root: dict) -> dict:
    """Сообщение в теме: отвечает на её корень, у корня — `forum_topic_created`."""
    return {"update_id": len(name) + thread, "message": {
        "chat": {"id": CHAT, "title": "Проект"}, "message_thread_id": thread, "text": "проба",
        "from": {"id": sender, "first_name": "Никон", "is_bot": sender == BOT},
        "reply_to_message": root or {"forum_topic_created": {"name": name}}}}


def check_retries(cfg: Config, fake: FakeTelegram) -> list[str]:
    bad = []
    flood = {"ok": False, "error_code": 429, "description": "Too Many Requests",
             "parameters": {"retry_after": 0}}
    fake.failures = [(429, flood), (502, {"ok": False, "error_code": 502, "description": "x"})]
    before = len(fake.calls)
    try:
        send(cfg, TASKS, "проба")
    except TelegramError as error:
        bad.append(f"429 и 502 должны повторяться, а дошло до отказа: {error}")
    if len(fake.calls) - before != 3:
        bad.append(f"429 и 502 → повторы и успех: вызовов {len(fake.calls) - before}, ждали 3")
    fake.failures = []  # недоеденные отказы не должны перетечь в следующую проверку
    before = len(fake.sent)
    send(cfg, TASKS, ("строка\n" * 700) + "x" * (LIMIT + 10))
    # 700 строк по 7 знаков режутся по строкам (585 строк = 4095), строка длиннее LIMIT — по LIMIT
    pieces = [len(item["text"]) for item in fake.sent[before:]]
    if pieces != [4095, 805, LIMIT, 10]:
        bad.append(f"длинный текст: куски {pieces}, ждали [4095, 805, {LIMIT}, 10]")
    before = len(fake.calls)
    try:
        send(cfg, 999_999, "проба")
        bad.append("тема 999999 должна дать 400")
    except TelegramError as error:
        if error.code != 400 or len(fake.calls) - before != 1:
            bad.append(f"400 без повторов: код {error.code}, вызовов {len(fake.calls) - before}")
    return bad


def check_send_file(cfg: Config, fake: FakeTelegram, folder: Path) -> list[str]:
    """Файл уходит полем `document` в нужную тему с подписью; 502 — повтор, как у текста."""
    bad = []
    path = folder / "metrics.md"
    path.write_text("| строка | FID |\n", encoding="utf-8")
    fake.failures = [(502, {"ok": False, "error_code": 502, "description": "x"})]
    before = len(fake.calls)
    send_file(cfg, TASKS, path, "таблица")
    want = {"chat_id": CHAT, "message_thread_id": TASKS, "caption": "таблица",
            "document": {"name": "metrics.md", "data": path.read_bytes()}}
    if fake.sent[-1:] != [want] or len(fake.calls) - before != 2:
        bad.append(f"файл: ушло {fake.sent[-1:]} за {len(fake.calls) - before} вызова, "
                   f"ждали {want} за 2")
    if cli(["send-file", "--topic", "results", "--file", str(path)], {}, folder / ".env") != 0:
        bad.append("без токена send-file должен выйти с кодом 0")
    return bad


def check_discover(env: dict[str, str], fake: FakeTelegram, folder: Path) -> list[str]:
    """Темы узнаются по названию, бот не считается хозяином, чужие строки `.env` целы;
    не найдена тема — выход 1, но найденное записано."""
    bad = []
    fake.updates = [in_topic(TASKS, "задачи и PR"), in_topic(CLAUDE, "Claude", sender=BOT),
                    in_topic(CLAUDE, "Claude", forum_topic_edited={"name": "Claude"}),
                    {"update_id": 1, "message": {"chat": {"id": OWNER}, "text": "/start",
                                                 "from": {"id": OWNER}}}]
    path = folder / ".env"
    path.write_text("TELEGRAM_BOT_TOKEN=t\n# комментарий\nTELEGRAM_CHAT_ID=1\n", encoding="utf-8")
    code = cli(["discover", "--save"], env, path)
    want = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": str(CHAT),
            "TELEGRAM_OWNER_ID": str(OWNER), Topic.TASKS.env: str(TASKS),
            Topic.CLAUDE.env: str(CLAUDE)}
    if dotenv(path) != want or "# комментарий" not in path.read_text(encoding="utf-8"):
        bad.append(f"discover --save записал {dotenv(path)}, ждали {want} и комментарий")
    if code != 1:
        bad.append(f"четыре темы не найдены — выход {code}, ждали 1")
    fake.updates = [in_topic(20 + i, topic.title) for i, topic in enumerate(Topic)]
    path.write_text("", encoding="utf-8")
    if cli(["discover", "--save"], env, path) != 0 or len(dotenv(path)) != 2 + len(Topic):
        bad.append(f"все темы на месте — выход 0 и {2 + len(Topic)} ключей, а записано "
                   f"{sorted(dotenv(path))}")
    return bad


def check_env_file(folder: Path) -> list[str]:
    """В worktree `.env` берётся из основного клона, в клоне — свой."""
    main, tree = folder / "main", folder / "tree"
    (main / ".git/worktrees/t").mkdir(parents=True)
    (main / ".git/worktrees/t/commondir").write_text("../..\n", encoding="utf-8")
    tree.mkdir()
    (tree / ".git").write_text(f"gitdir: {main}/.git/worktrees/t\n", encoding="utf-8")
    bad = []
    if env_file(tree) != (main / ".env").resolve():
        bad.append(f"worktree: .env {env_file(tree)}, ждали {main / '.env'}")
    if env_file(main) != main / ".env":
        bad.append(f"клон: .env {env_file(main)}, ждали {main / '.env'}")
    return bad


def fake_config(server: http.server.ThreadingHTTPServer,
                extra: dict[str, str] | None = None) -> tuple[dict[str, str], Config]:
    """Окружение и адрес подставного сервера для самопроверок, повторы без пауз."""
    env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": str(CHAT),
           "TELEGRAM_API_BASE": f"http://127.0.0.1:{server.server_port}"} | (extra or {})
    return env, dataclasses.replace(load_config(env), pauses=(0, 0, 0))


def selftest() -> int:
    fake = FakeTelegram()
    server = fake.serve()
    env, cfg = fake_config(server)
    with tempfile.TemporaryDirectory() as tmp:
        bad = check_retries(cfg, fake)
        bad += check_send_file(cfg, fake, Path(tmp))
        bad += check_discover(env, fake, Path(tmp))
        bad += check_env_file(Path(tmp))
    server.shutdown()
    return report(bad, "повторы 429/5xx, 400 без повтора, длинный текст кусками, файл "
                       "документом в тему, discover --save по названиям тем, .env из основного "
                       "клона в worktree, без токена — выход 0")


if __name__ == "__main__":
    sys.exit(run_script(selftest, run))

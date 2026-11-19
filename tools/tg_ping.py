#!/usr/bin/env python3
"""Пинг в Telegram, когда Claude закончил долгую работу. Хуки `.claude/settings.json`:

    python3 tools/tg_ping.py start   UserPromptSubmit: запомнить, когда хозяин написал
    python3 tools/tg_ping.py stop    Stop: «закончил», если ход длиннее порога
    python3 tools/tg_ping.py wait    Notification permission_prompt: «ждёт разрешения»
    python3 tools/tg_ping.py --selftest

JSON вызова — на stdin. Пинг уходит в тему «Claude» (`TELEGRAM_THREAD_CLAUDE`), только если
с просьбы хозяина прошло не меньше `min_minutes` из `.claude/ping.json`: короткий ответ, пока
хозяин у экрана, не шумит. Начало хода хранится файлом по `session_id` во временной папке —
хук Stop сам не знает, когда ход начался. Нет токена, темы или начала хода — молча выход 0:
хук не должен мешать работе.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import tg_send
from claude_hooks import branch
from script_common import report, run_script

STAMPS = Path(tempfile.gettempdir()) / "agent-turns"
PREVIEW = 600  # сколько знаков последнего сообщения показать в пинге


def stamp(folder: Path, call: dict) -> Path:
    return folder / f"{call.get('session_id', 'unknown')}.txt"


def start(folder: Path, call: dict, now: float) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    stamp(folder, call).write_text(str(now), encoding="utf-8")


def minutes_since_start(folder: Path, call: dict, now: float) -> int | None:
    path = stamp(folder, call)
    return int((now - float(path.read_text(encoding="utf-8"))) // 60) if path.exists() else None


def preview(text: str) -> str:
    """Начало ответа простым текстом: разметка Markdown в Telegram видна как есть."""
    plain = text.replace("**", "").replace("`", "").strip()
    return plain if len(plain) <= PREVIEW else plain[:PREVIEW].rsplit(" ", 1)[0] + " …"


def ping_text(event: str, call: dict, minutes: int, where: str) -> str:
    if event == "stop":
        return f"✅ Claude закончил · {minutes} мин · {where}\n\n" + preview(
            call.get("last_assistant_message") or "")
    return f"⏸ Claude ждёт разрешения · {minutes} мин · {where}\n\n{call.get('message', '')}"


def message(event: str, call: dict, folder: Path, threshold: int) -> str:
    """Текст пинга или пусто — ход короче порога или начало хода неизвестно."""
    minutes = minutes_since_start(folder, call, time.time())
    if minutes is None or minutes < threshold:
        return ""
    where = f"ветка {branch(Path(call['cwd']))}" if call.get("cwd") else "ветка ?"
    return ping_text(event, call, minutes, where).strip()


def deliver(text: str, env: dict[str, str]) -> bool:
    cfg = tg_send.load_config(env)
    thread = cfg.threads.get(tg_send.Topic.CLAUDE) if cfg else None
    if not text or cfg is None or cfg.chat_id is None or thread is None:
        return False
    try:
        tg_send.send(cfg, thread, text)
    except (tg_send.TelegramError, OSError):
        return False  # хук молчит: сорвавшийся пинг не повод мешать работе
    return True


def run(root: Path) -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    call = json.load(sys.stdin)
    if event == "start":
        start(STAMPS, call, time.time())
        return 0
    threshold = json.loads((root / ".claude/ping.json").read_text(encoding="utf-8"))["min_minutes"]
    deliver(message(event, call, STAMPS, threshold),
            {**tg_send.dotenv(tg_send.env_file(root)), **os.environ})
    return 0


# ---------------------------------------------------------------------------
# selftest: порог, текст пинга, неизвестное начало хода, без токена — молча
# ---------------------------------------------------------------------------


def check_messages(folder: Path, root: Path) -> list[str]:
    call = {"session_id": "s1", "cwd": str(root), "message": "Bash: git push",
            "last_assistant_message": "**Готово.** Код в `tools/` " + "слово " * 200}
    start(folder, call, time.time() - 2 * 60)
    bad = [] if message("stop", call, folder, 3) == "" else ["ход 2 мин при пороге 3 — молчать"]
    start(folder, call, time.time() - 12 * 60)
    done = message("stop", call, folder, 3)
    if not done.startswith("✅ Claude закончил · 12 мин · ветка agent/33-тест\n\nГотово. Код в "
                           "tools/"):
        bad.append(f"пинг «закончил»: {done[:80]!r}")
    if len(done) > 80 + PREVIEW or not done.endswith(" …"):
        bad.append(f"начало ответа не обрезано: {len(done)} знаков")
    if message("wait", call, folder, 3) != "⏸ Claude ждёт разрешения · 12 мин · ветка " \
            "agent/33-тест\n\nBash: git push":
        bad.append(f"пинг «ждёт»: {message('wait', call, folder, 3)!r}")
    if message("stop", call | {"session_id": "чужая"}, folder, 3):
        bad.append("начало хода неизвестно — молчать")
    return bad


def selftest() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        (root / ".git").mkdir(parents=True)
        (root / ".git/HEAD").write_text("ref: refs/heads/agent/33-тест\n", encoding="utf-8")
        bad = check_messages(Path(tmp) / "turns", root)
    if deliver("текст", {}):
        bad.append("без токена — не отправлять")
    if deliver("текст", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "-1"}):
        bad.append("без темы «Claude» — не отправлять")
    return report(bad, "короткий ход молчит, длинный — пинг с длительностью, веткой и началом "
                       "ответа, ожидание разрешения, неизвестный ход молчит, без токена или "
                       "темы молча")


if __name__ == "__main__":
    sys.exit(run_script(selftest, run))

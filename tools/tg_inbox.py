#!/usr/bin/env python3
"""Telegram → буфер входящих.

    python3 tools/tg_inbox.py pull --buffer ПАПКА        новое из темы «Входящие» → буфер
    python3 tools/tg_inbox.py --selftest

Хозяин пересылает пачку сообщений из переписки с продактом, заказчиком или инженерами в тему
«Входящие» своей группы. `pull` берёт из `getUpdates` только
сообщения этой группы, этой темы и от хозяина, отсекает уже виденное, дописывает новое в
`ПАПКА/messages.jsonl` и отвечает в теме «принял N, новых M, повторов K». Буфер — ветка
`tg-inbox`; пишет его только Action `tg-pull.yml` (по расписанию и вручную из `/inbox`),
чтобы у буфера был один писатель.

Почему отсев своим ключом: повторную доставку Telegram отсекает сам (`offset`), но пачка,
пересланная второй раз, — это новые сообщения бота. Исходного `message_id` у пересылки от
человека Bot API не отдаёт, поэтому ключ — исходный автор, исходная дата, текст и
`file_unique_id` вложений.

Почему последняя страница не подтверждается: апдейт подтверждается вызовом `getUpdates`
с бо́льшим `offset`. Подтверди сразу — и если коммит буфера сорвётся, сообщения пропадут;
`offset` хранится в `state.json` и подтверждает их следующим сбором, уже после коммита.

Адрес — как у `tools/tg_send.py`; для сбора нужны TELEGRAM_CHAT_ID, TELEGRAM_OWNER_ID,
TELEGRAM_THREAD_INBOX. Без токена — предупреждение и выход 0.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from script_common import report, run_script
from tg_send import (
    PAGE,
    Config,
    FakeTelegram,
    TelegramError,
    Topic,
    call_with_retries,
    dotenv,
    env_file,
    fake_config,
    load_config,
    send,
)

# Поля сообщения Bot API, в которых приходит файл.
MEDIA_FIELDS = ("document", "photo", "video", "audio", "voice", "animation", "video_note")
MESSAGES = "messages.jsonl"
STATE = "state.json"


class Origin(StrEnum):
    """`MessageOrigin.type` Bot API: откуда переслано сообщение."""

    USER = "user"
    HIDDEN_USER = "hidden_user"
    CHAT = "chat"
    CHANNEL = "channel"


# ---------------------------------------------------------------------------
# Сообщение → запись буфера
# ---------------------------------------------------------------------------


def person(user: dict) -> str:
    name = " ".join(filter(None, (user.get("first_name"), user.get("last_name"))))
    return f"{name} (@{user['username']})" if user.get("username") else name


def author(message: dict) -> tuple[str, str]:
    """Исходный автор: ключ для отсева и имя для человека."""
    origin = message.get("forward_origin")
    if origin is None:
        return f"user:{message['from']['id']}", person(message["from"])
    kind = Origin(origin["type"])
    if kind is Origin.USER:
        return f"user:{origin['sender_user']['id']}", person(origin["sender_user"])
    if kind is Origin.HIDDEN_USER:
        return f"hidden:{origin['sender_user_name']}", origin["sender_user_name"]
    chat = origin["chat"] if kind is Origin.CHANNEL else origin["sender_chat"]
    # у пересылки из канала исходный номер есть — он и различает сообщения
    suffix = f":{origin['message_id']}" if kind is Origin.CHANNEL else ""
    return f"{kind}:{chat['id']}{suffix}", chat.get("title", "")


def attachments(message: dict) -> list[dict]:
    found = []
    for name in MEDIA_FIELDS:
        media = message.get(name)
        if isinstance(media, list):  # фото приходит набором размеров — берём крупнейший
            media = max(media, key=lambda size: size.get("width", 0) * size.get("height", 0))
        if media:
            found.append({"kind": name, "file_id": media["file_id"],
                          "file_unique_id": media["file_unique_id"],
                          "name": media.get("file_name", ""), "size": media.get("file_size")})
    return found


def iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def body(message: dict) -> str:
    return message.get("text") or message.get("caption") or ""


def reply_context(message: dict) -> dict | None:
    """На что ответ. Пересланный ответ Telegram пересылает вместе с исходным сообщением и
    кладёт его в `reply_to_message`; каждое сообщение темы ещё и «отвечает» на её корень —
    это не контекст, у корня нет `forward_origin`."""
    parent = message.get("reply_to_message")
    if not parent or "forward_origin" not in parent:
        return None
    return {"author": author(parent)[1], "date": iso(parent["forward_origin"]["date"]),
            "text": body(parent)}


def to_record(message: dict) -> dict:
    """Ключ отсева — без `reply_to`: ответ, пересланный сам по себе и вместе с исходным, —
    одно и то же сообщение."""
    files = attachments(message)
    text = body(message)
    entities = message.get("entities") or message.get("caption_entities") or []
    key_author, name = author(message)
    date = message.get("forward_origin", message)["date"]
    source = "\n".join([key_author, str(date), text, *(f["file_unique_id"] for f in files)])
    return {"key": hashlib.sha256(source.encode()).hexdigest(), "author": name,
            "date": iso(date), "forwarded": "forward_origin" in message, "text": text,
            "links": [entity["url"] for entity in entities if entity.get("url")],
            "reply_to": reply_context(message), "files": files, "received": iso(message["date"])}


def from_inbox(update: dict, cfg: Config) -> dict | None:
    """Сообщение хозяина в теме «Входящие» его группы; всё остальное молча мимо."""
    message = update.get("message")
    if not message or message["chat"]["id"] != cfg.chat_id:
        return None
    if message.get("message_thread_id") != cfg.threads.get(Topic.INBOX):
        return None
    if message.get("from", {}).get("id") != cfg.owner_id:
        return None
    return message if body(message) or attachments(message) else None


# ---------------------------------------------------------------------------
# Буфер и сбор
# ---------------------------------------------------------------------------


def load_buffer(folder: Path) -> tuple[int | None, set[str]]:
    state = folder / STATE
    offset = json.loads(state.read_text(encoding="utf-8"))["offset"] if state.exists() else None
    messages = folder / MESSAGES
    lines = messages.read_text(encoding="utf-8").splitlines() if messages.exists() else []
    return offset, {json.loads(line)["key"] for line in lines}


def save_buffer(folder: Path, offset: int | None, records: list[dict]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / MESSAGES).open("a", encoding="utf-8") as out:
        out.writelines(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    (folder / STATE).write_text(json.dumps({"offset": offset}) + "\n", encoding="utf-8")


def fetch_updates(cfg: Config, offset: int | None) -> tuple[list[dict], int | None]:
    """Все неподтверждённые апдейты; последняя неполная страница остаётся неподтверждённой."""
    updates: list[dict] = []
    while True:
        page = call_with_retries(cfg, "getUpdates", {"offset": offset, "limit": PAGE,
                                                     "timeout": 0, "allowed_updates": ["message"]})
        updates += page
        if page:
            offset = page[-1]["update_id"] + 1
        if len(page) < PAGE:
            return updates, offset


def pull(cfg: Config, folder: Path) -> tuple[int, int]:
    """Новое из «Входящих» — в буфер; возвращает (принято, новых)."""
    offset, seen = load_buffer(folder)
    updates, offset = fetch_updates(cfg, offset)
    taken = [to_record(m) for m in (from_inbox(u, cfg) for u in updates) if m]
    fresh = []
    for record in taken:
        if record["key"] not in seen:
            seen.add(record["key"])
            fresh.append(record)
    save_buffer(folder, offset, fresh)
    if taken:
        repeats = len(taken) - len(fresh)
        send(cfg, cfg.threads[Topic.INBOX],
             f"принял {len(taken)}, новых {len(fresh)}, повторов {repeats}")
    return len(taken), len(fresh)


# ---------------------------------------------------------------------------
# Командная строка
# ---------------------------------------------------------------------------


def cli(argv: list[str], env: dict[str, str]) -> int:
    parser = argparse.ArgumentParser(description="Telegram → буфер входящих")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pull").add_argument("--buffer", type=Path, required=True)
    args = parser.parse_args(argv)
    cfg = load_config(env)
    if cfg is None:
        print("предупреждение: нет TELEGRAM_BOT_TOKEN — ничего не делаю", file=sys.stderr)
        return 0
    need = {"TELEGRAM_CHAT_ID": cfg.chat_id, "TELEGRAM_OWNER_ID": cfg.owner_id,
            Topic.INBOX.env: cfg.threads.get(Topic.INBOX)}
    if gaps := [key for key, value in need.items() if value is None]:
        print("не заданы: " + ", ".join(gaps), file=sys.stderr)
        return 1
    try:
        taken, fresh = pull(cfg, args.buffer)
    except TelegramError as error:
        print(f"Telegram отказал: {error}", file=sys.stderr)
        return 1
    print(f"принято {taken}, новых {fresh} → {args.buffer / MESSAGES}")
    return 0


def run(root: Path) -> int:
    return cli(sys.argv[1:], {**dotenv(env_file(root)), **os.environ})


# ---------------------------------------------------------------------------
# selftest: подставной Telegram на 127.0.0.1 — отсев, фильтр, offset, ответ, без токена
# ---------------------------------------------------------------------------

OWNER, CHAT, INBOX, OTHER_THREAD, STRANGER = 7, -100500, 11, 12, 8


def message(text: str, origin: dict | None = None, **where: int) -> dict:
    """Сообщение в «Входящих» от хозяина; `where` переопределяет чат, тему, отправителя."""
    fields = {"message_id": len(text), "date": 1_800_000_000, "text": text,
              "chat": {"id": where.get("chat", CHAT)},
              "from": {"id": where.get("sender", OWNER), "first_name": "Никон"},
              "message_thread_id": where.get("thread", INBOX),
              # каждое сообщение темы отвечает на её корень — сборщик не должен счесть это
              # контекстом
              "reply_to_message": {"message_id": INBOX,
                                   "forum_topic_created": {"name": "Входящие"}}}
    return fields | ({"forward_origin": origin} if origin else {})


def from_user(user_id: int, date: int) -> dict:
    return {"type": "user", "date": date, "sender_user": {"id": user_id, "first_name": "Иван"}}


def first_batch() -> list[dict]:
    hidden = {"type": "hidden_user", "date": 1_700_000_100, "sender_user_name": "Мария"}
    plan = message("заявка на балки", from_user(1, 1_700_000_000))
    return [plan, message("бланк завода приложу", hidden) | {"reply_to_message": plan},
            message("", from_user(1, 1_700_000_200)) | {
                "caption": "чертёж", "document": {"file_id": "F1", "file_unique_id": "U1",
                                                   "file_name": "a.pdf", "file_size": 10}}]


def second_batch() -> list[dict]:
    """Два повтора первой пачки, три новых и три чужих: не та тема, не тот чат, не хозяин."""
    old = first_batch()
    return [old[1], old[2],
            message("заявка на балки", from_user(2, 1_700_000_000)),  # тот же текст, другой автор
            message("заявка на балки", from_user(1, 1_700_000_300)),  # тот же автор, другая дата
            message("сам добавил"),
            message("мимо", thread=OTHER_THREAD), message("мимо", chat=1),
            message("мимо", sender=STRANGER)]


def as_updates(fake: FakeTelegram, messages: list[dict]) -> None:
    start = fake.updates[-1]["update_id"] + 1 if fake.updates else 100
    fake.updates += [{"update_id": start + i, "message": m} for i, m in enumerate(messages)]


def check_pull(cfg: Config, fake: FakeTelegram, folder: Path) -> list[str]:
    bad = []
    as_updates(fake, first_batch())
    expected = [((3, 3), "принял 3, новых 3, повторов 0"),
                ((5, 3), "принял 5, новых 3, повторов 2")]
    for batch, ((taken, fresh), reply) in enumerate(expected):
        if batch:
            as_updates(fake, second_batch())
        if (got := pull(cfg, folder)) != (taken, fresh):
            bad.append(f"пачка {batch + 1}: (принято, новых) = {got}, ждали {(taken, fresh)}")
        if fake.sent[-1:] != [{"chat_id": CHAT, "message_thread_id": INBOX, "text": reply}]:
            bad.append(f"пачка {batch + 1}: ответ {fake.sent[-1:]}, ждали «{reply}»")
    lines = (folder / MESSAGES).read_text(encoding="utf-8").splitlines()
    files = [r["files"] for r in map(json.loads, lines) if r["files"]]
    if len(lines) != 6 or files != [[{"kind": "document", "file_id": "F1", "file_unique_id": "U1",
                                      "name": "a.pdf", "size": 10}]]:
        bad.append(f"в буфере {len(lines)} строк, вложения {files}; ждали 6 и один a.pdf")
    replies = [r["reply_to"] for r in map(json.loads, lines) if r["reply_to"]]
    if replies != [{"author": "Иван", "date": iso(1_700_000_000), "text": "заявка на балки"}]:
        bad.append(f"контекст ответа: {replies}; ждали один — на «заявка на балки» Ивана")
    sent_before = len(fake.sent)
    if pull(cfg, folder) != (0, 0) or len(fake.sent) != sent_before:
        bad.append("третий сбор без новых апдейтов должен молчать")
    return bad


def selftest() -> int:
    fake = FakeTelegram(threads=(INBOX, OTHER_THREAD))
    server = fake.serve()
    _, cfg = fake_config(server, {"TELEGRAM_OWNER_ID": str(OWNER), Topic.INBOX.env: str(INBOX)})
    with tempfile.TemporaryDirectory() as tmp:
        bad = check_pull(cfg, fake, Path(tmp) / "inbox")
        if cli(["pull", "--buffer", tmp], {}) != 0:
            bad.append("без токена pull должен выйти с кодом 0")
        if cli(["pull", "--buffer", tmp], {"TELEGRAM_BOT_TOKEN": "t"}) != 1:
            bad.append("без группы, хозяина и темы pull должен выйти с кодом 1")
    server.shutdown()
    return report(bad, "отсев повторов и чужих, offset, ответ в теме, без токена — выход 0, "
                       "без адреса — 1")


if __name__ == "__main__":
    sys.exit(run_script(selftest, run))

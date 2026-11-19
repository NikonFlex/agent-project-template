#!/usr/bin/env python3
"""Буфер Telegram → входящие, локальная половина навыка /inbox.

    python3 tools/tg_export.py collect [--buffer ПАПКА]   сбор сейчас: Action tg-pull вручную,
                                                          затем буфер из ветки tg-inbox → ПАПКА
    python3 tools/tg_export.py export [--buffer ПАПКА]    невыгруженное → docs/incoming/,
                                                          запись в index.yaml
    python3 tools/tg_export.py --selftest

ПАПКА по умолчанию — `outputs/tg-inbox/` (вне git). Бот-сборщик и буфер — `tools/tg_inbox.py`,
отправка — `tools/tg_send.py`.

`collect` запускает тот же Action, что и расписание: буфер пишет только он. Сбор
упал — код 1, но буфер всё равно забирается: разобрать то, что уже собрано.

`export` пишет файл `docs/incoming/Переписка в Telegram <даты>.md`
(`[ДД.ММ.ГГГГ ЧЧ:ММ] Автор: текст`, время московское) и запись в `index.yaml`
(`docs/incoming/README.md`). Что уже выгружено, видно по самим входящим: у каждого
сообщения в конце метка `<!-- tg:<ключ> -->`. Отметку в буфер не ставим — у буфера был
бы второй писатель.
Вложения скачиваются в `materials/telegram/` (вне git: чужие файлы в git не кладутся);
без токена или больше 20 МБ — строка с причиной, почему не скачан.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import tg_inbox
import tg_send
import yaml
from script_common import ROOT, report, run_script
from spec_check import check_incoming, sha256

INCOMING = Path("docs/incoming")
MATERIALS = Path("materials/telegram")
BUFFER = Path("outputs/tg-inbox")
WORKFLOW = "tg-pull.yml"
BRANCH = "tg-inbox"
ZONE = ZoneInfo("Europe/Moscow")  # время, как его видит хозяин в клиенте
KEY_LENGTH = 16  # 64 бит ключа для отсева хватает, а метка в тексте короче
MARK = re.compile(rf"<!-- tg:([0-9a-f]{{{KEY_LENGTH}}}) -->")
QUOTE = 80  # сколько знаков исходного сообщения показывать у ответа
# Прогон, запущенный вручную, появляется в списке не сразу; часы GitHub и машины расходятся.
POLL_PAUSE, POLL_TRIES, CLOCK_SLACK = 3, 20, timedelta(seconds=30)


# ---------------------------------------------------------------------------
# collect: Action вручную и буфер из ветки
# ---------------------------------------------------------------------------


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)  # noqa: S603, S607


def find_run(started: datetime) -> str | None:
    for _ in range(POLL_TRIES):
        time.sleep(POLL_PAUSE)
        listed = gh("run", "list", "--workflow", WORKFLOW, "--event", "workflow_dispatch",
                    "--limit", "5", "--json", "databaseId,createdAt").stdout
        fresh = [run for run in json.loads(listed or "[]")
                 if datetime.fromisoformat(run["createdAt"]) >= started - CLOCK_SLACK]
        if fresh:
            return str(fresh[0]["databaseId"])
    return None


def dispatch_and_watch() -> bool:
    started = datetime.now(UTC)
    if gh("workflow", "run", WORKFLOW, "--ref", "main").returncode:
        return False
    run_id = find_run(started)
    return run_id is not None and gh("run", "watch", run_id, "--exit-status").returncode == 0


def fetch_buffer(folder: Path) -> int:
    """Буфер из ветки tg-inbox → ПАПКА; возвращает число сообщений в нём."""
    subprocess.run(["git", "fetch", "-q", "origin", BRANCH], check=True)  # noqa: S603, S607
    shown = subprocess.run(["git", "show", f"origin/{BRANCH}:{tg_inbox.MESSAGES}"],  # noqa: S603, S607
                           capture_output=True, text=True)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / tg_inbox.MESSAGES).write_text(shown.stdout, encoding="utf-8")
    return len(shown.stdout.splitlines())


# ---------------------------------------------------------------------------
# export: записи → файл входящих
# ---------------------------------------------------------------------------


def exported_keys(incoming: Path) -> set[str]:
    return {key for path in incoming.glob("*.md")
            for key in MARK.findall(path.read_text(encoding="utf-8"))}


def local(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp).astimezone(ZONE)


def span(records: list[dict]) -> str:
    """Даты пачки для имени файла, как у прежних выгрузок: «06.10», «26–28.09», «29.09–06.10»."""
    days = sorted(local(record["date"]).date() for record in records)
    first, last = days[0], days[-1]
    if first == last:
        return f"{first:%d.%m}"
    start = f"{first:%d}" if first.month == last.month else f"{first:%d.%m}"
    return f"{start}–{last:%d.%m}"


def free_name(incoming: Path, records: list[dict]) -> Path:
    stem = f"Переписка в Telegram {span(records)}"
    path, copy = incoming / f"{stem}.md", 2
    while path.exists():
        path, copy = incoming / f"{stem} ({copy}).md", copy + 1
    return path


def download(cfg: tg_send.Config | None, media: dict, folder: Path, day: date) -> str:
    """Путь скачанного файла от корня репозитория или причина, почему не скачан."""
    label = media["name"] or media["kind"]
    if cfg is None:
        return f"{label}, не скачан — нет TELEGRAM_BOT_TOKEN"
    try:
        info = tg_send.call_with_retries(cfg, "getFile", {"file_id": media["file_id"]})
        name = media["name"] or f"{media['file_unique_id']}{Path(info['file_path']).suffix}"
        target = folder / f"{day:%Y-%m-%d}-{Path(name).name}"
        # схема адреса проверена в tg_send.load_config
        url = f"{cfg.base}/file/bot{cfg.token}/{info['file_path']}"
        with urllib.request.urlopen(url, timeout=tg_send.TIMEOUT) as response:  # noqa: S310
            data = response.read()
    except (tg_send.TelegramError, OSError) as error:
        return f"{label}, не скачан — {error}"
    folder.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return f"{MATERIALS.as_posix()}/{target.name}, {max(1, len(data) // 1024)} КБ"


def header(record: dict) -> str:
    when = f"[{local(record['date']):%d.%m.%Y %H:%M}] {record['author']}"
    if not record["forwarded"]:
        return f"{when}, заметка во «Входящих»"
    reply = record.get("reply_to")
    if not reply:
        return when
    quote = " ".join(reply["text"].split())
    quote = quote if len(quote) <= QUOTE else quote[:QUOTE] + "…"
    # у ответа на картинку без подписи цитировать нечего — пустые «» только путают
    quote = f" «{quote}»" if quote else ""
    return f"{when}, в ответ {reply['author']} [{local(reply['date']):%d.%m.%Y %H:%M}]{quote}"


def render(record: dict, files: list[str]) -> str:
    lines = [f"{header(record)}: {record['text']}".rstrip()]
    lines += [f"[ссылки: {', '.join(record['links'])}]"] if record["links"] else []
    lines += [f"[файл: {line}]" for line in files]
    return "\n".join(lines) + f" <!-- tg:{record['key'][:KEY_LENGTH]} -->"


def index_entry(path: Path, digest: str, today: date) -> str:
    return (f"- path: {path.as_posix()}\n  received: '{today.isoformat()}'\n  origin: >-\n"
            "    пересланное хозяином во «Входящие» своей Telegram-группы (бот-сборщик);\n"
            f"    выгрузка tools/tg_export.py; вложения — в {MATERIALS.as_posix()}/, вне git\n"
            f"  sha256: {digest}\n")


def append_index(index: Path, entry: str) -> None:
    """Пустой индекс шаблона — строка `[]`; запись после неё YAML не читает, поэтому первая
    запись встаёт на её место."""
    lines = index.read_text(encoding="utf-8").splitlines(keepends=True)
    if lines and lines[-1].strip() == "[]":
        lines.pop()
    index.write_text("".join(lines) + entry, encoding="utf-8")


def export(root: Path, buffer: Path, cfg: tg_send.Config | None) -> Path | None:
    """Невыгруженное из буфера → новый файл входящих и запись в индексе; нечего — None."""
    messages = buffer / tg_inbox.MESSAGES
    lines = messages.read_text(encoding="utf-8").splitlines() if messages.exists() else []
    done = exported_keys(root / INCOMING)
    records = sorted((r for r in map(json.loads, lines) if r["key"][:KEY_LENGTH] not in done),
                     key=lambda record: record["date"])
    if not records:
        return None
    blocks = [render(r, [download(cfg, f, root / MATERIALS, local(r["date"]).date())
                         for f in r["files"]]) for r in records]
    target = free_name(root / INCOMING, records)
    today = datetime.now(ZONE).date()
    target.write_text(f"<!-- Выгрузка tools/tg_export.py {today:%d.%m.%Y}: пересланное во "
                      "«Входящие». Время московское. Метка tg в конце сообщения — по ней его "
                      "не выгружают второй раз. -->\n\n" + "\n\n".join(blocks) + "\n",
                      encoding="utf-8")
    entry = index_entry(target.relative_to(root), sha256(target), today)
    append_index(root / INCOMING / "index.yaml", entry)
    return target


# ---------------------------------------------------------------------------
# Командная строка
# ---------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Буфер Telegram → входящие")
    commands = cli.add_subparsers(dest="command", required=True)
    for name in ("collect", "export"):
        commands.add_parser(name).add_argument("--buffer", type=Path, default=ROOT / BUFFER)
    return cli


def run(root: Path) -> int:
    args = parser().parse_args(sys.argv[1:])
    if args.command == "collect":
        collected = dispatch_and_watch()
        count = fetch_buffer(args.buffer)
        print(f"сбор {'прошёл' if collected else 'УПАЛ — разбираю то, что уже в буфере'}; "
              f"в буфере сообщений: {count}")
        return 0 if collected else 1
    cfg = tg_send.load_config({**tg_send.dotenv(tg_send.env_file(root)), **os.environ})
    target = export(root, args.buffer, cfg)
    print(target.relative_to(root) if target else "нового нет: всё из буфера уже во входящих")
    return 0


# ---------------------------------------------------------------------------
# selftest: буфер → файл входящих, индекс проходит spec_check, повторно — пусто
# ---------------------------------------------------------------------------


def make_buffer(folder: Path) -> None:
    """Пять записей, как их пишет tg_inbox.pull: ответ, вложения (одно больше 20 МБ), заметка."""
    def record(key: str, stamp: str, text: str, **extra: object) -> dict:
        return {"key": key * 64, "author": "Иван Заказчик", "date": stamp, "forwarded": True,
                "text": text, "links": [], "files": [], "reply_to": None,
                "received": "2026-10-07T23:00:00+00:00"} | extra

    pdf = {"kind": "document", "file_id": "F1", "file_unique_id": "U1", "name": "план.pdf",
           "size": 13}
    big = {"kind": "video", "file_id": "BIG", "file_unique_id": "U2", "name": "", "size": 9}
    rows = [record("a", "2026-09-29T15:18:34+00:00", "первое\nвторая строка"),
            record("b", "2026-10-06T15:15:07+00:00", "балки к 20.02?",
                   links=["https://example.org/x"]),
            record("c", "2026-10-06T16:03:02+00:00", "Лучше к 25-му",
                   author="Пётр (@petr)", reply_to={
                       "author": "Иван Заказчик", "date": "2026-10-06T15:15:07+00:00",
                       "text": "балки к 20.02?"}),
            record("d", "2026-10-06T16:10:00+00:00", "чертёж", files=[pdf, big], reply_to={
                "author": "Иван Заказчик", "date": "2026-10-06T16:05:00+00:00", "text": ""}),
            record("e", "2026-10-07T23:12:43+00:00", "к пачке", forwarded=False,
                   author="Никон (@nikon)")]
    folder.mkdir(parents=True)
    (folder / tg_inbox.MESSAGES).write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


EXPECTED_LINES = [
    "[29.09.2026 18:18] Иван Заказчик: первое",
    "вторая строка <!-- tg:aaaaaaaaaaaaaaaa -->",
    "[ссылки: https://example.org/x] <!-- tg:bbbbbbbbbbbbbbbb -->",
    "[06.10.2026 19:03] Пётр (@petr), в ответ Иван Заказчик [06.10.2026 18:15] "
    "«балки к 20.02?»: Лучше к 25-му <!-- tg:cccccccccccccccc -->",
    "[06.10.2026 19:10] Иван Заказчик, в ответ Иван Заказчик [06.10.2026 19:05]: чертёж",
    "[файл: materials/telegram/2026-10-06-план.pdf, 1 КБ]",
    "[файл: video, не скачан — 400: file is too big] <!-- tg:dddddddddddddddd -->",
    "[08.10.2026 02:12] Никон (@nikon), заметка во «Входящих»: к пачке "
    "<!-- tg:eeeeeeeeeeeeeeee -->",
]


def make_project(root: Path) -> None:
    (root / INCOMING).mkdir(parents=True)
    (root / INCOMING / "README.md").write_text("# Входящие\n", encoding="utf-8")
    (root / INCOMING / "index.yaml").write_text("# индекс\n", encoding="utf-8")
    (root / INCOMING / "Переписка в Telegram 29.09–08.10.md").write_text("занято\n",
                                                                       encoding="utf-8")


def check_empty_index(root: Path, cfg: tg_send.Config) -> list[str]:
    """Первая выгрузка в новом репо: индекс шаблона — `[]`."""
    (root / INCOMING).mkdir(parents=True)
    (root / INCOMING / "index.yaml").write_text("# индекс\n[]\n", encoding="utf-8")
    make_buffer(root / "buffer")
    if export(root, root / "buffer", cfg) is None:
        return ["пустой индекс: export ничего не выгрузил"]
    try:
        return [f"пустой индекс, spec_check: {problem}" for problem in check_incoming(root)]
    except yaml.YAMLError as error:
        return [f"пустой индекс: после export index.yaml не читается — {error}"]


def check_export(root: Path, cfg: tg_send.Config) -> list[str]:
    make_project(root)
    make_buffer(root / "buffer")
    (root / INCOMING / "index.yaml").write_text("# индекс\n" + index_entry(
        Path("docs/incoming/Переписка в Telegram 29.09–08.10.md"),
        sha256(root / INCOMING / "Переписка в Telegram 29.09–08.10.md"),
        date(2026, 10, 7)), encoding="utf-8")
    target = export(root, root / "buffer", cfg)
    bad = [] if target and target.name == "Переписка в Telegram 29.09–08.10 (2).md" else [
        f"имя файла: {target}; ждали «… 29.09–08.10 (2).md» — первое занято"]
    text = target.read_text(encoding="utf-8") if target else ""
    bad += [f"в файле нет строки: {line}" for line in EXPECTED_LINES if line not in text]
    if (root / MATERIALS / "2026-10-06-план.pdf").read_bytes() != tg_send.FILE_BYTES:
        bad.append("вложение не скачано в materials/telegram/")
    bad += [f"spec_check: {problem}" for problem in check_incoming(root)]
    if export(root, root / "buffer", cfg) is not None:
        bad.append("повторный export должен быть пустым")
    return bad


def selftest() -> int:
    fake = tg_send.FakeTelegram()
    server = fake.serve()
    cfg = tg_send.Config("t", tg_send.CHAT, base=f"http://127.0.0.1:{server.server_port}",
                          pauses=(0, 0, 0))
    with tempfile.TemporaryDirectory() as tmp:
        bad = check_export(Path(tmp, "full"), cfg) + check_empty_index(Path(tmp, "empty"), cfg)
    server.shutdown()
    return report(bad, "файл входящих в формате выгрузок, ответ, ссылки, вложения (одно — "
                       "отказ 20 МБ), заметка, имя без затирания, spec_check зелёный, "
                       "повторный export пуст, первая запись в пустой индекс `[]`")


if __name__ == "__main__":
    sys.exit(run_script(selftest, run))

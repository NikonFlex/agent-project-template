#!/usr/bin/env python3
"""Задачи GitHub на диске: верх из трекера, низ — наш.

    python3 backlog.py sync              трекер → диск (docs/tasks/), обновить верх у всех
    python3 backlog.py sync --check      ничего не писать, показать, что разошлось
    python3 backlog.py review            что просело: без разбора, застряло, хвосты
    python3 backlog.py resume 12         верх и наш низ одной задачи — чтобы продолжить
    python3 backlog.py new docs/tasks/draft-имя.md             показать, что уйдёт
    python3 backlog.py new docs/tasks/draft-имя.md --confirm   завести задачу
    python3 backlog.py comment 12 --section "Что установлено"            показать
    python3 backlog.py comment 12 --section "Что установлено" --confirm  отправить
    python3 backlog.py --selftest        проверка без сети

Файл задачи docs/tasks/<префикс>-0012.md делится меткой на две половины:

    верх — из GitHub: состояние, метки, исполнитель, описание, комментарии.
           Каждый sync перезаписывает целиком: правки руками сотрутся.
    <!-- ↓↓↓ ниже наше: скрипт эту часть не трогает ↓↓↓ -->
    низ  — наше: чем возобновлять, что установлено, что опровергнуто,
           тупики, ход работы. Скрипт его не меняет никогда.

Наружу пишут только `new` и `comment`, и только с --confirm.
Нужны Python 3.9+ и `gh` (GitHub CLI), вошедший в аккаунт: `gh auth login`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MARK = "<!-- ↓↓↓ ниже наше: скрипт эту часть не трогает ↓↓↓ -->"

TEMPLATE = f"""{MARK}

## Чем возобновлять

_Первое, что делать, вернувшись: команда, файл, следующий шаг._

## Что установлено

_Проверенное — со ссылкой на файл или замер и датой._

## Что опровергнуто

_Гипотеза — чем опровергнута — когда._

## Тупики

_Куда не ходить и почему._

## Ход работы

_Короткая хронология: что делали, что вышло._
"""

#: Раздел низа, который `review` собирает в начало отчёта целиком.
OPEN_ENDS = "Что осталось"
#: Открытая задача без обновлений столько дней считается застрявшей.
STALE_DAYS = 14

FIELDS = ("number,title,state,labels,assignees,milestone,"
          "createdAt,updatedAt,closedAt,author,body,comments,url")

#: Скрипт кладёт чужой текст в репозиторий и отправляет наш наружу — значит,
#: обязан останавливаться на похожем на пароль или ключ. Список неполный.
SECRET_PATTERNS = [
    (r"gh[pousr]_[A-Za-z0-9]{30,}", "токен GitHub"),
    (r"github_pat_[A-Za-z0-9_]{30,}", "токен GitHub"),
    (r"sk-[A-Za-z0-9_-]{24,}", "ключ вида sk-…"),
    (r"AKIA[0-9A-Z]{16}", "ключ AWS"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY", "приватный ключ"),
    (r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", "JWT"),
    (r"(?i)(password|passwd|пароль)\s*[:=]\s*\S{8,}", "пароль в присвоении"),
    (r"(?i)(secret|api[_-]?key|token)\s*[:=]\s*\S{12,}", "секрет в присвоении"),
    (r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b", "токен Telegram-бота"),
]


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------


def find_root() -> Path:
    """Корень git-репозитория, из которого запущен скрипт."""
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit("запускать внутри git-репозитория")
    return Path(out.stdout.strip())


def load_config(root: Path) -> dict:
    """`.backlog.json` в корне необязателен: без него всё берётся из `gh`.

    {"repo": "owner/name", "prefix": "g", "tasks_dir": "docs/tasks", "project": 3}
    """
    path = root / ".backlog.json"
    cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not cfg.get("repo"):
        cfg["repo"] = gh("repo", "view", "--json", "nameWithOwner",
                         "--jq", ".nameWithOwner").strip()
    if not cfg.get("prefix"):
        cfg["prefix"] = re.sub(r"[^a-z]", "", cfg["repo"].split("/")[-1].lower())[:2] or "t"
    cfg["tasks"] = root / cfg.get("tasks_dir", "docs/tasks")
    return cfg


def gh(*args: str) -> str:
    try:
        proc = subprocess.run(("gh",) + args, capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        sys.exit("нет `gh`. Поставить: https://cli.github.com, потом `gh auth login`")
    if proc.returncode != 0:
        sys.exit(f"gh {' '.join(args[:2])}: {proc.stderr.strip()}")
    return proc.stdout


def scan_secrets(label: str, text: str) -> list[str]:
    return [f"{label}: {why}" for pat, why in SECRET_PATTERNS if re.search(pat, text or "")]


# ---------------------------------------------------------------------------
# Файл задачи
# ---------------------------------------------------------------------------


def squash(text: str) -> str:
    return " ".join(text.split())


def split_ours(path: Path) -> str:
    """Низ существующего файла — от первой метки до конца, как есть.

    Нет файла — чистый каркас. Нет метки — файл заведён руками, и он весь
    считается нашим: гадать, где чьё, нельзя, иначе sync сотрёт работу.
    """
    if not path.exists():
        return TEMPLATE
    text = path.read_text(encoding="utf-8")
    idx = text.find(MARK)
    if idx == -1:
        return MARK + "\n\n" + text.strip() + "\n"
    return text[idx:]


def has_ours(path: Path) -> bool:
    """Под меткой есть что-то кроме нетронутого каркаса."""
    return path.exists() and squash(split_ours(path)) != squash(TEMPLATE)


def fetch_state_reasons(repo: str) -> dict[int, str]:
    """Причина закрытия по номеру задачи.

    `gh issue list --json stateReason` появился в gh 2.50; на более старых
    версиях он падает с «Unknown JSON field». REST-эндпоинт отдаёт
    `state_reason` в любой версии, поэтому берём оттуда. Интересны только
    закрытые: у открытых причины нет.
    """
    try:
        raw = gh("api", "--paginate", "-X", "GET",
                 f"repos/{repo}/issues", "-f", "state=closed", "-f", "per_page=100")
    except SystemExit:
        return {}
    out: dict[int, str] = {}
    for chunk in raw.strip().splitlines() or []:
        if not chunk.strip():
            continue
        try:
            items = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        for it in items if isinstance(items, list) else [items]:
            if isinstance(it, dict) and it.get("number") is not None:
                out[it["number"]] = it.get("state_reason") or ""
    return out


def dropped(issue: dict) -> bool:
    """Закрыта как «не будем делать»: её текст на диске читался бы как действующий."""
    return (issue.get("stateReason") or "").upper() == "NOT_PLANNED"


def who(user) -> str:
    if isinstance(user, dict):
        return user.get("login") or user.get("name") or "—"
    return str(user) if user else "—"


def when(value: str | None) -> str:
    return value.replace("T", " ")[:16] if value else "—"


def upper_half(prefix: str, issue: dict, card: dict, stamp: str) -> str:
    """Всё, что приходит из GitHub. Перезаписывается каждым sync."""
    labels = ", ".join(l.get("name", "") for l in issue.get("labels") or []) or "—"
    assignees = ", ".join(who(a) for a in issue.get("assignees") or []) or "—"
    state = issue.get("state", "")
    if issue.get("stateReason"):
        state += f" ({issue['stateReason']})"
    lines = [
        f"# {prefix}#{issue['number']} — {issue['title']}",
        "",
        f"> Верх файла — из GitHub, снят {stamp}, правки руками сотрутся. "
        "Наша работа — ниже метки. Состояние могло устареть: перед тем как "
        "сказать «открыта» — `backlog.py sync`.",
        "",
        "| Поле | Значение |",
        "|------|----------|",
        f"| **Состояние** | {state} |",
        f"| **Доска** | {card.get('status') or '—'} |",
        f"| **Метки** | {labels} |",
        f"| **Веха** | {(issue.get('milestone') or {}).get('title', '—')} |",
        f"| **Автор** | {who(issue.get('author'))} |",
        f"| **Исполнитель** | {assignees} |",
        f"| **Создана** | {when(issue.get('createdAt'))} |",
        f"| **Обновлена** | {when(issue.get('updatedAt'))} |",
        f"| **Закрыта** | {when(issue.get('closedAt'))} |",
        f"| **Ссылка** | {issue.get('url')} |",
        "",
        "## Описание из трекера",
        "",
    ]
    if dropped(issue):
        lines += ["_Задача закрыта как «не будем делать» — текст только в GitHub._", ""]
        return "\n".join(lines)

    body = (issue.get("body") or "").strip()
    # Метка, попавшая в тело задачи, — это чужой низ. Не срезать её — и sync
    # каждый раз приклеивал бы ещё одну копию низа.
    if MARK in body:
        body = body[:body.find(MARK)].rstrip()
    lines += [body or "_пусто_", ""]
    comments = issue.get("comments") or []
    if comments:
        lines += ["## Комментарии из трекера", ""]
        for c in comments:
            lines += [f"### {who(c.get('author'))} — {when(c.get('createdAt'))}", "",
                      (c.get("body") or "").strip(), ""]
    return "\n".join(lines)


class Task:
    """Файл задачи на диске, разобранный на половины."""

    FIELD = re.compile(r"^\|\s*\*\*(?P<k>[^*]+)\*\*\s*\|\s*(?P<v>.*?)\s*\|\s*$")

    def __init__(self, path: Path) -> None:
        self.path, self.name = path, path.stem
        text = path.read_text(encoding="utf-8")
        idx = text.find(MARK)
        self.top = text if idx == -1 else text[:idx]
        self.ours = "" if idx == -1 else text[idx + len(MARK):]
        self.fields = {m["k"].strip(): m["v"].strip()
                       for l in self.top.splitlines() if (m := self.FIELD.match(l))}
        self.title = next((l[2:].strip() for l in self.top.splitlines()
                           if l.startswith("# ")), self.name)

    is_draft = property(lambda self: self.name.startswith("draft-"))
    state = property(lambda self: self.fields.get("Состояние", ""))
    is_open = property(lambda self: self.state.upper().startswith("OPEN"))
    link = property(lambda self: self.fields.get("Ссылка", ""))
    updated = property(lambda self: self.fields.get("Обновлена", ""))
    has_ours = property(lambda self: has_ours(self.path))

    def sections(self) -> dict[str, str]:
        """Разделы низа: заголовок «## …» → текст под ним."""
        out, current, buf = {}, None, []
        for line in self.ours.splitlines():
            if line.startswith("## "):
                if current:
                    out[current] = "\n".join(buf).strip()
                current, buf = line[3:].strip(), []
            elif current:
                buf.append(line)
        if current:
            out[current] = "\n".join(buf).strip()
        return out

    def open_ends(self) -> str:
        return next((b for h, b in self.sections().items()
                     if h.startswith(OPEN_ENDS) and b.strip()), "")


def load_tasks(folder: Path) -> list[Task]:
    return [Task(p) for p in sorted(folder.glob("*.md"))
            if p.name not in {"INDEX.md", "REVIEW.md"}]


def find_task(folder: Path, key: str) -> Task | None:
    """По «g-0012», «g-12», «g12» или «12»."""
    m = re.fullmatch(r"(?:[a-z]+[-_]?)?0*(\d+)", key.strip().lower().removesuffix(".md"))
    if not m:
        return None
    number = int(m.group(1))
    for t in load_tasks(folder):
        mm = re.fullmatch(r"[a-z]+-(\d+)", t.name)
        if mm and int(mm.group(1)) == number:
            return t
    return None


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


def fetch_board(cfg: dict) -> dict[int, dict]:
    """Колонки доски GitHub Projects, если она названа в `.backlog.json`."""
    if not cfg.get("project"):
        return {}
    owner = cfg["repo"].split("/")[0]
    raw = gh("project", "item-list", str(cfg["project"]), "--owner", owner,
             "--limit", "1000", "--format", "json")
    board = {}
    for item in json.loads(raw).get("items", []):
        c = item.get("content") or {}
        if (c.get("repository") or "").endswith(cfg["repo"]) and c.get("number"):
            board[c["number"]] = {"status": item.get("status")}
    return board


def index_page(issues: list[dict], board: dict, cfg: dict, stamp: str) -> str:
    open_n = sum(1 for i in issues if i.get("state") == "OPEN")
    lines = [
        "# Задачи", "",
        f"Снято {stamp} · задач {len(issues)}: открытых {open_n}, закрытых {len(issues) - open_n}.",
        "Обновляет `backlog.py sync`. Искать по задачам — `grep -ri <слово> docs/tasks/`.", "",
        "| Задача | Состояние | Доска | Название | Разбор |",
        "|---|---|---|---|---|",
    ]
    for i in sorted(issues, key=lambda x: x.get("updatedAt") or "", reverse=True):
        fname = f"{cfg['prefix']}-{i['number']:04d}.md"
        target = fname if (cfg["tasks"] / fname).exists() else i.get("url")
        state = "открыта" if i.get("state") == "OPEN" else "закрыта"
        ours = "есть" if has_ours(cfg["tasks"] / fname) else "—"
        title = i["title"].replace("|", "\\|")
        status = board.get(i["number"], {}).get("status") or "—"
        lines.append(f"| [{cfg['prefix']}#{i['number']}]({target}) | {state} | {status} | {title} | {ours} |")
    return "\n".join(lines) + "\n"


def cmd_sync(cfg: dict, check: bool) -> int:
    issues = json.loads(gh("issue", "list", "-R", cfg["repo"], "--state", "all",
                           "--limit", "1000", "--json", FIELDS))
    reasons = fetch_state_reasons(cfg["repo"])
    for i in issues:
        if reasons.get(i["number"]):
            i["stateReason"] = reasons[i["number"]]
    board = fetch_board(cfg)

    leaks = []
    for i in issues:
        leaks += scan_secrets(f"#{i['number']} описание", i.get("body") or "")
        for c in i.get("comments") or []:
            leaks += scan_secrets(f"#{i['number']} комментарий", c.get("body") or "")
    if leaks:
        print("В задачах есть похожее на пароль или ключ — на диск не кладу:")
        for line in leaks[:20]:
            print("  " + line)
        print("Убрать из задачи на GitHub, сменить этот пароль или ключ, повторить sync.")
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cfg["tasks"].mkdir(parents=True, exist_ok=True)
    kept, changed, created = 0, [], []
    for issue in issues:
        path = cfg["tasks"] / f"{cfg['prefix']}-{issue['number']:04d}.md"
        if dropped(issue) and not path.exists():
            continue
        new = upper_half(cfg["prefix"], issue, board.get(issue["number"], {}), stamp) + split_ours(path)
        if path.exists():
            # строка со временем снимка меняется всегда — сравниваем без неё
            drop_stamp = lambda t: "\n".join(l for l in t.splitlines() if not l.startswith("> Верх файла"))
            if drop_stamp(path.read_text(encoding="utf-8")) == drop_stamp(new):
                kept += 1
                continue
            changed.append(path.name)
        else:
            created.append(path.name)
        if not check:
            path.write_text(new, encoding="utf-8")
    if not check:
        (cfg["tasks"] / "INDEX.md").write_text(index_page(issues, board, cfg, stamp), encoding="utf-8")

    print(f"без изменений: {kept}, {'разошлось' if check else 'обновлено'}: {len(changed)}, "
          f"новых: {len(created)}")
    for n in (changed + created)[:20]:
        print("  " + n)
    return 1 if check and (changed or created) else 0


# ---------------------------------------------------------------------------
# review, resume
# ---------------------------------------------------------------------------


def days_since(value: str) -> float | None:
    try:
        t = datetime.strptime(value.strip()[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - t).total_seconds() / 86400


def collect_review(folder: Path, stale_days: int = STALE_DAYS) -> dict[str, list[Task]]:
    b = {k: [] for k in ("open_ends", "no_analysis", "closed_alive", "stale", "orphans", "drafts")}
    for t in load_tasks(folder):
        if t.open_ends():
            b["open_ends"].append(t)
        if t.is_draft:
            b["drafts"].append(t)
            continue
        if not t.link:
            b["orphans"].append(t)
            continue
        if t.is_open and not t.has_ours:
            b["no_analysis"].append(t)
        if not t.is_open and t.has_ours:
            b["closed_alive"].append(t)
        age = days_since(t.updated)
        if t.is_open and age is not None and age >= stale_days:
            b["stale"].append(t)
    return b


def cmd_review(cfg: dict) -> int:
    b = collect_review(cfg["tasks"])

    def table(items: list[Task], note: str) -> list[str]:
        if not items:
            return ["_пусто_", ""]
        rows = ["| Задача | Название |", "|---|---|"]
        rows += [f"| [{t.name}]({t.name}.md) | {t.title.split('—', 1)[-1].strip()} |" for t in items]
        return rows + ["", note, ""]

    ends: list[str] = []
    for t in b["open_ends"]:
        ends += [f"### [{t.name}]({t.name}.md) — {t.title.split('—', 1)[-1].strip()}", "",
                 t.open_ends(), ""]
    page = [
        "# Обзор задач", "",
        f"Снят {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC из файлов на диске, "
        "в сеть не ходит: свежесть — как у последнего sync.", "",
        f"## {OPEN_ENDS}", "", *(ends or ["_пусто_", ""]),
        "## Открыта, разбора нет", "",
        *table(b["no_analysis"], "Низ пуст: если по задаче работали, найденное пропадёт вместе с сессией."),
        "## Закрыта, а разбор живой", "",
        *table(b["closed_alive"], "Проверить: не отправить ли итог комментарием (`comment`)."),
        f"## Открыта и не двигалась {STALE_DAYS}+ дней", "",
        *table(b["stale"], "Либо делается молча, либо забыта."),
        "## Файлы без задачи в GitHub", "",
        *table(b["orphans"], "Нет поля «Ссылка»: задачу удалили, или файл сделан руками."),
        "## Черновики", "",
        *table(b["drafts"], "Черновик — ещё не задача. Заводится `new … --confirm` после «да» владельца."),
    ]
    out = cfg["tasks"] / "REVIEW.md"
    out.write_text("\n".join(page) + "\n", encoding="utf-8")
    labels = {"open_ends": f"с разделом «{OPEN_ENDS}»", "no_analysis": "открыта, разбора нет",
              "closed_alive": "закрыта, разбор живой", "stale": f"стоит {STALE_DAYS}+ дней",
              "orphans": "файлов без задачи", "drafts": "черновиков"}
    for k, label in labels.items():
        print(f"{label:24}: {len(b[k])}")
    print(f"отчёт: {out}")
    return 0


def cmd_resume(cfg: dict, key: str) -> int:
    t = find_task(cfg["tasks"], key)
    if t is None:
        print(f"нет файла задачи {key}. Сначала `backlog.py sync`.", file=sys.stderr)
        return 1
    print(f"# {t.title}\n")
    print(f"файл      : {t.path}")
    print(f"состояние : {t.state or '—'} · обновлена {t.updated or '—'}")
    print(f"ссылка    : {t.link or '—'}")
    m = re.search(r"снят (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", t.top)
    if m and (days_since(m.group(1)) or 0) >= 1:
        print("\n⚠ верх снят больше суток назад — перед выводами о состоянии сделать sync")
    if not t.has_ours:
        print("\nНиз пуст: по задаче ещё ничего не записано.")
        return 0
    print("\n" + "─" * 60 + "\n" + t.ours.strip())
    return 0


# ---------------------------------------------------------------------------
# new, comment — единственные шаги, которые пишут в GitHub
# ---------------------------------------------------------------------------

#: Метка, набранная похоже, но не точно: разделение молча не сработает и весь
#: наш низ уедет в GitHub. Лучше остановиться.
NEAR_MARK = re.compile(r"<!--[^>]*ниже наше[^>]*-->")


def split_draft(path: Path) -> tuple[str, str, str]:
    """Черновик → заголовок, тело для GitHub, низ (с меткой)."""
    text = path.read_text(encoding="utf-8")
    if MARK not in text and NEAR_MARK.search(text):
        sys.exit(f"{path}: метка не та. Скопировать посимвольно:\n{MARK}")
    top, ours = (text.split(MARK, 1)[0], MARK + text.split(MARK, 1)[1]) if MARK in text else (text, TEMPLATE)
    lines = top.strip().splitlines()
    if not lines or not lines[0].startswith("# ") or not lines[0][2:].strip():
        sys.exit(f"{path}: первая строка — заголовок «# Название задачи»")
    return lines[0][2:].strip(), "\n".join(lines[1:]).strip(), ours


def gh_with_body(args: list[str], body: str) -> str:
    """Вызов gh с телом через файл: кавычки и переносы в тексте не ломают команду."""
    with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as fh:
        fh.write(body)
    try:
        return gh(*args, "--body-file", fh.name)
    finally:
        Path(fh.name).unlink(missing_ok=True)


def cmd_new(cfg: dict, draft: str, labels: list[str], confirm: bool) -> int:
    path = Path(draft).resolve()
    if not path.exists():
        sys.exit(f"нет файла {draft}")
    title, body, ours = split_draft(path)
    leaks = scan_secrets("черновик", title + "\n" + body)
    if leaks:
        print("В черновике похоже на пароль или ключ — наружу не отправляю:\n  " + "\n  ".join(leaks))
        return 2
    print(f"репозиторий : {cfg['repo']}\nзаголовок   : {title}\nметки       : {', '.join(labels) or '—'}")
    print("--- тело ---\n" + body + "\n--- конец ---")
    if not confirm:
        print("\nНичего не заведено. Повторить с --confirm, когда владелец сказал «да».")
        return 1

    args = ["issue", "create", "-R", cfg["repo"], "--title", title]
    for lb in labels:
        args += ["--label", lb]
    out = gh_with_body(args, body)
    m = re.search(r"/issues/(\d+)", out)
    if not m:
        sys.exit(f"не понял номер задачи из ответа gh: {out.strip()!r}")
    target = cfg["tasks"] / f"{cfg['prefix']}-{int(m.group(1)):04d}.md"
    target.write_text(ours if ours.endswith("\n") else ours + "\n", encoding="utf-8")
    path.unlink()
    print(out.strip() + f"\nчерновик → {target.name}; верх допишет sync:")
    return cmd_sync(cfg, check=False)


def cmd_comment(cfg: dict, key: str, sections: list[str], confirm: bool) -> int:
    t = find_task(cfg["tasks"], key)
    if t is None:
        print(f"нет файла задачи {key}", file=sys.stderr)
        return 1
    have = t.sections()
    if not sections:
        print("разделы низа:\n" + "\n".join(f"  --section {s!r}" for s in have))
        return 1
    missing = [s for s in sections if s not in have]
    if missing:
        print("таких разделов нет: " + ", ".join(missing), file=sys.stderr)
        return 1
    body = "\n\n".join(f"## {s}\n\n{have[s]}" for s in sections).strip()
    leaks = scan_secrets("комментарий", body)
    if leaks:
        print("В тексте похоже на пароль или ключ — не отправляю:\n  " + "\n  ".join(leaks))
        return 2
    m = re.search(r"/issues/(\d+)", t.link)
    if not m:
        print("в верхе файла нет ссылки на задачу", file=sys.stderr)
        return 1
    print(f"задача: {t.title}\n--- комментарий ---\n{body}\n--- конец ---")
    if not confirm:
        print("\nНичего не отправлено. Повторить с --confirm, когда владелец сказал «да».")
        return 1
    print(gh_with_body(["issue", "comment", m.group(1), "-R", cfg["repo"]], body).strip())
    return 0


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------


def selftest() -> int:
    """Главное обещание — низ не теряется — проверяется на подставных данных."""
    bad: list[str] = []
    issue = {"number": 7, "title": "Прыжок", "state": "OPEN", "url": "https://github.com/u/g/issues/7",
             "body": "Тело.\n\n" + MARK + "\n\nчужой низ", "updatedAt": "2020-01-01T00:00:00Z"}
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        p = d / "g-0007.md"
        p.write_text(MARK + "\n\n## Что установлено\n\nНаш текст.\n", encoding="utf-8")
        for _ in range(2):
            p.write_text(upper_half("g", issue, {}, "тест") + split_ours(p), encoding="utf-8")
        text = p.read_text(encoding="utf-8")
        if text.count(MARK) != 1:
            bad.append(f"после двух sync меток {text.count(MARK)}, а не одна")
        if "Наш текст." not in text or "чужой низ" in text:
            bad.append("низ потерян или в верх попал чужой низ")

        gone = upper_half("g", {**issue, "state": "CLOSED", "stateReason": "NOT_PLANNED"}, {}, "т")
        if "Тело." in gone:
            bad.append("текст отменённой задачи попал на диск")

        (d / "g-0008.md").write_text(upper_half("g", {**issue, "number": 8}, {}, "т") + TEMPLATE, encoding="utf-8")
        (d / "draft-x.md").write_text("# Черновик\n\nтекст\n", encoding="utf-8")
        r = collect_review(d)
        if [t.name for t in r["no_analysis"]] != ["g-0008"] or [t.name for t in r["drafts"]] != ["draft-x"]:
            bad.append("review разложил задачи не туда")
        if [t.name for t in r["stale"]] != ["g-0007", "g-0008"]:
            bad.append("review не заметил застрявшие")
        for key in ("g-0007", "g-7", "g7", "7"):
            if (f := find_task(d, key)) is None or f.name != "g-0007":
                bad.append(f"resume не нашёл задачу по {key!r}")

        title, body, ours = split_draft(d / "draft-x.md")
        if (title, body) != ("Черновик", "текст") or MARK not in ours:
            bad.append("черновик без метки разобран неверно")

    if not scan_secrets("x", "ghp_" + "a" * 36) or scan_secrets("x", "передать token в заголовке"):
        bad.append("сито секретов ошибается")

    for b in bad:
        print("✗ " + b)
    if not bad:
        print("самопроверка пройдена: низ переживает sync, review и resume работают, сито ловит ключи")
    return 1 if bad else 0


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true", help="проверка без сети")
    sub = ap.add_subparsers(dest="step")
    s = sub.add_parser("sync", help="GitHub → docs/tasks/")
    s.add_argument("--check", action="store_true", help="не писать, показать расхождения")
    sub.add_parser("review", help="обзор, отчёт в docs/tasks/REVIEW.md")
    s = sub.add_parser("resume", help="верх и низ одной задачи")
    s.add_argument("task")
    s = sub.add_parser("new", help="черновик → задача (только с --confirm)")
    s.add_argument("draft")
    s.add_argument("--label", action="append", default=[])
    s.add_argument("--confirm", action="store_true")
    s = sub.add_parser("comment", help="разделы низа → комментарий (только с --confirm)")
    s.add_argument("task")
    s.add_argument("--section", action="append", default=[])
    s.add_argument("--confirm", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    if not a.step:
        ap.print_help()
        return 0
    cfg = load_config(find_root())
    if a.step == "sync":
        return cmd_sync(cfg, a.check)
    if not cfg["tasks"].is_dir():
        sys.exit(f"{cfg['tasks']} ещё нет — сначала `backlog.py sync`")
    if a.step == "review":
        return cmd_review(cfg)
    if a.step == "resume":
        return cmd_resume(cfg, a.task)
    if a.step == "new":
        return cmd_new(cfg, a.draft, a.label, a.confirm)
    return cmd_comment(cfg, a.task, a.section, a.confirm)


if __name__ == "__main__":
    raise SystemExit(main())

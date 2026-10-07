"""Проверки порядка «входящие → спеки» перед коммитом (docs/SPEC-DRIVEN.md).

Запуск: python3 tools/spec_check.py [корень репозитория]
        python3 tools/spec_check.py --selftest   ловит ли проверка подложенные нарушения
Код возврата 0 — всё в порядке, 1 — есть нарушения (печатаются по строке).
Нужен PyYAML: pip install -r tools/requirements.txt
"""

from __future__ import annotations

import hashlib
import re
import sys
import tempfile
from pathlib import Path

import yaml
from project_config import backlog_config
from script_common import print_problems, report, run_script

INCOMING = Path("docs/incoming")
SPECS = Path("specs")
SERVICE_FILES = {"README.md", "index.yaml"}
HEADER_FIELDS = ("Статус", "Источники:", "Владелец дельт")
# Спека не хранит состояние задач: рядом со ссылкой на задачу этих слов быть не должно.
TASK_STATE = re.compile(r"\b(открыт[аоы]?|закрыт[аоы]?|сделан[аоы]?|в работе|готов[аоы]?)\b", re.I)
# «Рядом» — между ссылкой и словом только знаки и слово «задача»: «#12 закрыта», «ab#7, сделано»,
# «ab#7, задача закрыта», «закрыта задача #12». То же слово дальше по строке — предметная
# область: «вход закрыт».
GAP = r"[\s,:;()—–-]{0,4}"
TASK_WORD = rf"(?:задач[аиу]?{GAP})?"
# Пометка дельты: *(дельта 28.09, ab#7, ТЗ v2 §3 — было «сразу»)* — содержимое скобок.
DELTA = re.compile(r"\(дельта\b([^)]*)\)")


def task_ref(prefix: str | None) -> re.Pattern:
    """Ссылка на задачу: `#12`, а с префиксом — `ab#12` и `ab-0012`; номер — группа 1."""
    named = rf"|{re.escape(prefix)}#(\d+)|{re.escape(prefix)}-(\d{{4}})" if prefix else ""
    return re.compile(rf"(?<![\w#])(?:#(\d+){named})")


def ref_number(match: re.Match) -> int:
    return int(next(g for g in match.groups() if g))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_incoming(root: Path) -> list[str]:
    """Каждый входящий файл — в индексе, каждая запись — на живой файл с тем же sha256."""
    index = yaml.safe_load((root / INCOMING / "index.yaml").read_text(encoding="utf-8")) or []
    problems = []
    listed = set()
    for entry in index:
        path = root / entry["path"]
        listed.add(path.name)
        if not path.exists():
            problems.append(f"индекс: нет файла {entry['path']}")
            continue
        if sha256(path) != entry.get("sha256"):
            problems.append(f"индекс: sha256 не совпадает у {entry['path']} — входящие не правятся")
        problems += check_distilled(root, entry)
    for path in (root / INCOMING).iterdir():
        if path.is_file() and path.name not in SERVICE_FILES and path.name not in listed:
            problems.append(f"индекс: файла {path.name} нет в index.yaml")
    return problems


def check_distilled(root: Path, entry: dict) -> list[str]:
    if entry.get("status") != "distilled":
        return []
    targets = entry.get("distilled_into") or []
    if not targets:
        return [f"индекс: {entry['path']} — distilled, но distilled_into пуст"]
    return [f"индекс: {entry['path']} ссылается на несуществующую {t}"
            for t in targets if not (root / t).exists()]


def spec_files(root: Path) -> list[Path]:
    return sorted(p for p in (root / SPECS).glob("*.md") if p.name != "README.md")


def check_spec(path: Path, ref: re.Pattern) -> list[str]:
    """Шапка со статусом, источниками и владельцем; непустое «Открыто»; без состояния задач."""
    text = path.read_text(encoding="utf-8")
    head = text.split("\n## ", 1)[0]
    problems = [f"{path.name}: в шапке нет «{field}»" for field in HEADER_FIELDS
                if field not in head]
    if not open_section(text):
        problems.append(f"{path.name}: раздел «## Открыто» пуст или отсутствует")
    for n, line in enumerate(text.splitlines(), 1):
        if state_near_ref(line, ref):
            problems.append(f"{path.name}:{n}: рядом со ссылкой на задачу её состояние")
    return problems


def state_near_ref(line: str, ref: re.Pattern) -> bool:
    state, task = TASK_STATE.pattern, f"(?:{ref.pattern})"
    after = rf"{task}{GAP}{TASK_WORD}{state}"
    before = rf"{state}{GAP}{TASK_WORD}{task}"
    return any(re.search(p, line, re.I) for p in (after, before))


def check_deltas(path: Path, ref: re.Pattern, tasks: Path, incoming: set[str]) -> list[str]:
    """Дельта называет основание: задачу из docs/tasks/ или документ из index.yaml.
    Ищем по всему тексту: пометка бывает перенесена на следующую строку."""
    problems = []
    text = path.read_text(encoding="utf-8")
    for match in DELTA.finditer(text):
        delta, where = match.group(1), f"{path.name}:{text.count(chr(10), 0, match.start()) + 1}"
        refs = list(ref.finditer(delta))
        docs = [name for name in incoming if f"incoming/{name}" in delta]
        if not refs and not docs:
            problems.append(f"{where}: у дельты нет основания — задачи или incoming/<файл>")
        if "incoming/" in delta and not docs:
            problems.append(f"{where}: дельта ссылается на документ, которого нет в index.yaml")
        problems += [f"{where}: дельта ссылается на задачу {m.group(0)}, её нет в {tasks.name}/"
                     for m in refs if not task_file(tasks, ref_number(m))]
    return problems


def task_file(tasks: Path, number: int) -> bool:
    return any(tasks.glob(f"*-{number:04d}.md"))


def incoming_names(root: Path) -> set[str]:
    index = yaml.safe_load((root / INCOMING / "index.yaml").read_text(encoding="utf-8")) or []
    return {Path(entry["path"]).name for entry in index}


def open_section(text: str) -> str:
    match = re.search(r"^## Открыто\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return match.group(1).strip() if match else ""


def domain_entities(root: Path) -> list[str]:
    """Сущности из последней колонки таблицы доменов в specs/README.md."""
    text = (root / SPECS / "README.md").read_text(encoding="utf-8")
    table = text.split("## Домены", 1)[1].split("\n## ", 1)[0]
    names = []
    for row in table.splitlines():
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[2] not in ("—", "Ключевые сущности", "---"):
            names += [n.strip() for n in cells[2].split(",")]
    return names


def check_entities(root: Path) -> list[str]:
    """Каждая сущность из карты объявлена (### Имя) ровно в одной спеке."""
    problems = []
    for name in domain_entities(root):
        owners = [p.name for p in spec_files(root)
                  if re.search(rf"^### {re.escape(name)}\s*$", p.read_text(encoding="utf-8"), re.M)]
        if len(owners) != 1:
            where = ", ".join(owners) or "нигде"
            problems.append(f"сущность {name} объявлена не в одной спеке: {where}")
    return problems


def run(root: Path) -> list[str]:
    problems = check_incoming(root) + check_entities(root)
    cfg = backlog_config(root)
    ref = task_ref(cfg["prefix"])
    incoming = incoming_names(root)
    for path in spec_files(root):
        problems += check_spec(path, ref) + check_deltas(path, ref, cfg["tasks"], incoming)
    return problems


# ---------------------------------------------------------------------------
# selftest: на чистом проекте нарушений нет, каждое подложенное — поймано
# ---------------------------------------------------------------------------

GOOD_SPEC = """# Accounts

> **Статус: provisional.**
> Источники: docs/incoming/тз.md.
> Владелец дельт: хозяин.

## Сущности

### User

Ссылка на задачу ab#3 без состояния.

## Правила

1. Вход по почте *(дельта 28.09, ab#3)*.
2. Почта в нижнем регистре *(дельта 29.09, incoming/тз.md §2)*.
3. После 5 ошибок вход закрыт на 15 минут *(дельта 30.09, ab#3)*.

## Открыто

- вопрос
"""

#: Нарушение → правка чистого проекта, которая его вызывает, и кусок ожидаемого сообщения.
DEFECTS = {
    "входящий файл правили": (lambda r: (r / INCOMING / "тз.md").write_text("другое"), "sha256"),
    "входящий файл без записи": (
        lambda r: (r / INCOMING / "лишний.md").write_text("x"), "нет в index.yaml"),
    "в шапке нет статуса": (lambda r: edit(r, "> **Статус: provisional.**\n", ""), "Статус"),
    "пустое «Открыто»": (lambda r: edit(r, "- вопрос\n", ""), "Открыто"),
    "состояние задачи в спеке": (lambda r: edit(r, "без состояния", "закрыта"), "состояние"),
    "состояние в пометке дельты": (lambda r: edit(r, "28.09, ab#3)", "28.09, ab#3, сделано)"),
                                   "состояние"),
    "состояние перед ссылкой": (
        lambda r: edit(r, "задачу ab#3", "закрыта задача ab#3"), "состояние"),
    "состояние после «задача»": (
        lambda r: edit(r, "задачу ab#3 без состояния", "ab#3, задача закрыта"), "состояние"),
    "сущность не объявлена": (lambda r: edit(r, "### User", "### Person"), "нигде"),
    "сущность объявлена дважды": (
        lambda r: (r / SPECS / "twin.md").write_text(GOOD_SPEC, encoding="utf-8"), "twin.md"),
    "distilled_into на несуществующую спеку": (
        lambda r: (r / SPECS / "accounts.md").rename(r / SPECS / "users.md"), "несуществующую"),
    "дельта без основания": (lambda r: edit(r, "28.09, ab#3", "28.09, по разговору"), "основания"),
    "дельта без основания через перенос": (
        lambda r: edit(r, "28.09, ab#3)", "28.09,\n   по разговору)"),
        "accounts.md:15: у дельты нет"),
    "дельта на несуществующую задачу": (lambda r: edit(r, "28.09, ab#3", "28.09, ab#9"), "ab#9"),
    "дельта на документ не из индекса": (
        lambda r: edit(r, "incoming/тз.md", "incoming/старое.md"), "на документ, которого"),
}


def edit(root: Path, old: str, new: str) -> None:
    path = root / SPECS / "accounts.md"
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def make_project(root: Path) -> Path:
    (root / INCOMING).mkdir(parents=True)
    (root / SPECS).mkdir()
    (root / ".backlog.json").write_text('{"prefix": "ab"}')
    (root / "docs/tasks").mkdir()
    (root / "docs/tasks/ab-0003.md").write_text("# ab#3 — задача", encoding="utf-8")
    doc = root / INCOMING / "тз.md"
    doc.write_text("текст ТЗ", encoding="utf-8")
    entry = {"path": "docs/incoming/тз.md", "status": "distilled", "sha256": sha256(doc),
             "distilled_into": ["specs/accounts.md"]}
    index = yaml.safe_dump([entry], allow_unicode=True)
    (root / INCOMING / "index.yaml").write_text(index, encoding="utf-8")
    (root / SPECS / "README.md").write_text(
        "# Спеки\n\n## Домены\n\n| Спека | Хранит | Ключевые сущности |\n|---|---|---|\n"
        "| accounts.md | пользователи | User |\n", encoding="utf-8")
    (root / SPECS / "accounts.md").write_text(GOOD_SPEC, encoding="utf-8")
    return root


def selftest() -> int:
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        if problems := run(make_project(Path(tmp) / "clean")):
            bad.append(f"чистый проект: {problems}")
        for name, (spoil, expected) in DEFECTS.items():
            root = make_project(Path(tmp) / name)
            spoil(root)
            if not any(expected in p for p in run(root)):
                bad.append(f"не поймано: {name}")
    n = len(DEFECTS)
    return report(bad, f"чистый проект чист, поймано нарушений {n} из {n}")


def check(root: Path) -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else root
    return print_problems(run(root), "спеки в порядке")


if __name__ == "__main__":
    sys.exit(run_script(selftest, check))

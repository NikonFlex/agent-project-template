"""Проверки порядка «входящие → спеки» перед коммитом (docs/SPEC-DRIVEN.md).

Запуск: python3 tools/spec_check.py [корень репозитория]
        python3 tools/spec_check.py --selftest   ловит ли проверка подложенные нарушения
Код возврата 0 — всё в порядке, 1 — есть нарушения (печатаются по строке).
Нужен PyYAML: pip install -r tools/requirements.txt
"""

import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

import yaml

INCOMING = Path("docs/incoming")
SPECS = Path("specs")
SERVICE_FILES = {"README.md", "index.yaml"}
HEADER_FIELDS = ("Статус", "Источники:", "Владелец дельт")
# Спека не хранит состояние задач: рядом со ссылкой на задачу этих слов быть не должно.
TASK_STATE = re.compile(r"\b(открыт[аы]?|закрыт[аы]?|сделан[аы]?|в работе|готов[аы]?)\b", re.I)


def task_ref(root: Path) -> re.Pattern:
    """Ссылка на задачу: `#12`, а с префиксом из .backlog.json — `ab#12` и `ab-0012`."""
    config = root / ".backlog.json"
    prefix = None
    if config.exists():
        prefix = json.loads(config.read_text(encoding="utf-8")).get("prefix")
    named = rf"|{re.escape(prefix)}#\d+|{re.escape(prefix)}-\d{{4}}" if prefix else ""
    return re.compile(rf"(#\d+{named})")


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
        if ref.search(line) and TASK_STATE.search(line):
            problems.append(f"{path.name}:{n}: рядом со ссылкой на задачу её состояние")
    return problems


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
    ref = task_ref(root)
    for path in spec_files(root):
        problems += check_spec(path, ref)
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
    "сущность не объявлена": (lambda r: edit(r, "### User", "### Person"), "нигде"),
}


def edit(root: Path, old: str, new: str) -> None:
    path = root / SPECS / "accounts.md"
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def make_project(root: Path) -> Path:
    (root / INCOMING).mkdir(parents=True)
    (root / SPECS).mkdir()
    (root / ".backlog.json").write_text('{"prefix": "ab"}')
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
    for line in bad:
        print("✗ " + line)
    if not bad:
        n = len(DEFECTS)
        print(f"самопроверка пройдена: чистый проект чист, поймано нарушений {n} из {n}")
    return 1 if bad else 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    problems = run(root)
    for problem in problems:
        print(problem)
    print("спеки в порядке" if not problems else f"нарушений: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

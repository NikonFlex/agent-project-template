"""Проверки входящих и спек OpenSpec, которых нет в валидаторе OpenSpec (openspec/README.md).

Запуск: python3 tools/spec_check.py [корень]   корень — репозиторий или docs/examples/project
        python3 tools/spec_check.py --selftest  ловит ли проверка подложенные нарушения
Код возврата 0 — всё в порядке, 1 — есть нарушения (печатаются по строке).
Форму (Purpose, MUST, Scenario, 500 знаков) проверяет `openspec validate --strict` в check.sh.
Нужен PyYAML: pip install -r tools/requirements.txt
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import yaml
from project_config import backlog_config
from script_common import check_root, run_script, selftest_project

INCOMING = Path("docs/incoming")
SPECS = Path("openspec/specs")
SERVICE_FILES = {"README.md", "index.yaml"}
#: Тип спеки (первая строка Purpose) → разделы, которые у неё обязаны быть и которых быть не должно.
KINDS = {
    "сущность": (("## Сущности", "## Связи"), ("## Тексты",)),
    "экран": (("## Тексты",), ("## Сущности", "## Связи")),
}
KIND_LINE = re.compile(r"^## Purpose\s*\nТип: (\S+?)\.", re.M)
REQUIREMENT = re.compile(r"^### Requirement:\s*(.+?)\s*$", re.M)
SOURCE_LINE = "**Источник:**"
SCENARIO = "#### Scenario:"
# Связь: «Назначение — Сотрудник (`employee`)» — вторая сущность объявлена в названной спеке.
LINK = re.compile(r"^(.+?) — (.+?) \(`([\w-]+)`\)$")
# В спеке нет истории (правило текста 7): было «…», стало «…» — в журнале, а не в норме.
HISTORY = re.compile(r"\(дельта\b|\bподтвердил|\bprovisional\b", re.I)
# В THEN нет оценок вместо наблюдаемого результата (правило текста 4).
VAGUE = re.compile(r"\b(корректно|правильно|как ожидается)\b", re.I)
# Спека не хранит состояние задач: рядом со ссылкой на задачу этих слов быть не должно.
TASK_STATE = re.compile(r"\b(открыт[аоы]?|закрыт[аоы]?|сделан[аоы]?|в работе|готов[аоы]?)\b", re.I)
# «Рядом» — между ссылкой и словом только знаки и слово «задача»: «#12 закрыта», «ab#7, сделано».
# То же слово дальше по строке — предметная область: «вход закрыт».
GAP = r"[\s,:;()—–-]{0,4}"
TASK_WORD = rf"(?:задач[аиу]?{GAP})?"


def task_ref(prefix: str | None) -> re.Pattern:
    """Ссылка на задачу: `#12`, а с префиксом — `ab#12` и `ab-0012`."""
    named = rf"|{re.escape(prefix)}#(\d+)|{re.escape(prefix)}-(\d{{4}})" if prefix else ""
    return re.compile(rf"(?<![\w#])(?:#(\d+){named})")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- входящие


def check_incoming(root: Path) -> list[str]:
    """Каждый входящий файл — в индексе, каждая запись — на живой файл с тем же sha256."""
    index_path = root / INCOMING / "index.yaml"
    if not index_path.exists():
        return []
    index = yaml.safe_load(index_path.read_text(encoding="utf-8")) or []
    problems, listed = [], set()
    for entry in index:
        path = root / entry["path"]
        listed.add(path.name)
        if not path.exists():
            problems.append(f"индекс: нет файла {entry['path']}")
        elif sha256(path) != entry.get("sha256"):
            problems.append(f"индекс: sha256 не совпадает у {entry['path']} — входящие не правятся")
    for path in (root / INCOMING).iterdir():
        if path.is_file() and path.name not in SERVICE_FILES and path.name not in listed:
            problems.append(f"индекс: файла {path.name} нет в index.yaml")
    return problems


# --------------------------------------------------------------------------- спеки


def spec_files(root: Path) -> dict[str, str]:
    """Имя спеки (каталог) → текст."""
    return {p.parent.name: p.read_text(encoding="utf-8")
            for p in sorted((root / SPECS).glob("*/spec.md"))}


def section(text: str, title: str) -> str:
    match = re.search(rf"^{re.escape(title)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return match.group(1) if match else ""


def check_kind(name: str, text: str) -> list[str]:
    """Purpose называет тип; у типа свои обязательные и запрещённые разделы."""
    match = KIND_LINE.search(text)
    if not match or match.group(1) not in KINDS:
        return [f"{name}: Purpose не начинается с «Тип: сущность.» или «Тип: экран.»"]
    must, must_not = KINDS[match.group(1)]
    headings = set(re.findall(r"^## .+?(?=\s*$)", text, re.M))
    return ([f"{name}: у спеки типа «{match.group(1)}» нет раздела «{h}»" for h in must
             if h not in headings]
            + [f"{name}: у спеки типа «{match.group(1)}» не бывает раздела «{h}»" for h in must_not
               if h in headings])


def requirements(text: str) -> list[tuple[str, str]]:
    """Заголовок требования → его текст до следующего требования или раздела."""
    parts = REQUIREMENT.split(section(text, "## Requirements"))
    return list(zip(parts[1::2], parts[2::2], strict=True))


def check_requirements(name: str, text: str) -> list[str]:
    """Заголовки не повторяются; строка источника — до первого сценария; в THEN нет оценок."""
    problems, seen = [], set()
    for title, body in requirements(text):
        if title in seen:
            problems.append(f"{name}: заголовок «{title}» повторяется — адрес не однозначен")
        seen.add(title)
        if SOURCE_LINE not in body.split(SCENARIO, 1)[0]:
            problems.append(f"{name}: «{title}» — нет строки «{SOURCE_LINE}» до первого сценария")
        for line in body.splitlines():
            if line.lstrip("- *").startswith("THEN") and VAGUE.search(line):
                problems.append(f"{name}: «{title}» — оценка в THEN: {line.strip()}")
    return problems


def check_text(name: str, text: str, ref: re.Pattern) -> list[str]:
    """Нет истории версий и нет состояния задач рядом со ссылкой на задачу."""
    problems = []
    for n, line in enumerate(text.splitlines(), 1):
        if HISTORY.search(line):
            problems.append(f"{name}:{n}: история в спеке (дельта, «подтвердил», provisional)")
        if state_near_ref(line, ref):
            problems.append(f"{name}:{n}: рядом со ссылкой на задачу её состояние")
    return problems


def state_near_ref(line: str, ref: re.Pattern) -> bool:
    state, task = TASK_STATE.pattern, f"(?:{ref.pattern})"
    after = rf"{task}{GAP}{TASK_WORD}{state}"
    before = rf"{state}{GAP}{TASK_WORD}{task}"
    return any(re.search(p, line, re.I) for p in (after, before))


def declared(specs: dict[str, str]) -> dict[str, list[str]]:
    """Сущность → спеки, где она объявлена (`### Имя` в «## Сущности»)."""
    owners: dict[str, list[str]] = {}
    for name, text in specs.items():
        for entity in re.findall(r"^### (.+?)\s*$", section(text, "## Сущности"), re.M):
            owners.setdefault(entity, []).append(name)
    return owners


def cells(line: str) -> list[str]:
    """Ячейки строки таблицы; `\\|` внутри ячейки — не граница."""
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def tables(block: str) -> list[tuple[list[str], list[list[str]]]]:
    """Таблицы блока по порядку: заголовок и строки без строки-разделителя."""
    groups: list[list[str]] = [[]]
    for line in block.splitlines():
        if line.strip().startswith("|"):
            groups[-1].append(line)
        elif groups[-1]:
            groups.append([])
    return [(cells(g[0]), [cells(r) for r in g[2:]]) for g in groups if g]


def table_rows(block: str) -> list[list[str]]:
    """Строки первой таблицы блока."""
    found = tables(block)
    return found[0][1] if found else []


def check_links(specs: dict[str, str], owners: dict[str, list[str]]) -> list[str]:
    """Связь начинается с сущности этой спеки и называет владельца второй сущности."""
    problems = []
    for name, text in specs.items():
        for row in table_rows(section(text, "## Связи")):
            match = LINK.match(row[0])
            if not match:
                problems.append(f"{name}: связь «{row[0]}» не по форме «A — B (`спека`)»")
                continue
            own, other, owner = match.groups()
            if name not in owners.get(own, []):
                problems.append(f"{name}: связь «{row[0]}» — {own} не объявлена в этой спеке")
            if owner not in owners.get(other, []):
                problems.append(f"{name}: связь «{row[0]}» — {other} не объявлена в спеке {owner}")
    return problems


def check_specs(root: Path, ref: re.Pattern) -> list[str]:
    specs = spec_files(root)
    owners = declared(specs)
    problems = [f"сущность {entity} объявлена не в одной спеке: {', '.join(where)}"
                for entity, where in owners.items() if len(where) > 1]
    for name, text in specs.items():
        problems += check_kind(name, text) + check_requirements(name, text)
        problems += check_text(name, text, ref)
    return problems + check_links(specs, owners)


def run(root: Path) -> list[str]:
    return check_incoming(root) + check_specs(root, task_ref(backlog_config(root)["prefix"]))


# ---------------------------------------------------------------------------
# selftest: на чистом проекте нарушений нет, каждое подложенное — поймано
# ---------------------------------------------------------------------------

GOOD = {
    "accounts": """# Учётная запись

## Purpose
Тип: сущность. Учётная запись пользователя и вход в неё; задача ab#3 без состояния.

## Сущности

### Учётная запись

| Поле | Значение | Источник |
|---|---|---|
| почта | уникальна | ТЗ A1 (стр. 1) |

## Связи

| Связь | Кратность |
|---|---|
| Учётная запись — Роль (`roles`) | много → 1 |

## Requirements

### Requirement: Блокировка входа
Система MUST закрывать вход на 15 минут после 5 неверных паролей подряд.

**Источник:** ТЗ A2 (стр. 2)

#### Scenario: Пятая ошибка
- GIVEN 4 неверных пароля подряд
- WHEN введён пятый неверный пароль
- THEN вход закрыт на 15 минут
""",
    "roles": """# Роль

## Purpose
Тип: сущность. Роль пользователя.

## Сущности

### Роль

## Связи

| Связь | Кратность |
|---|---|
| Роль — Учётная запись (`accounts`) | 1 → много |
""",
    "login": """# Вход

## Purpose
Тип: экран. Форма входа.

## Тексты

| Текст | Где стоит | Источник |
|---|---|---|
| «Войти» | кнопка | ТЗ A3 (стр. 3) |
""",
}

#: Нарушение → правка чистого проекта, которая его вызывает, и кусок ожидаемого сообщения.
DEFECTS = {
    "входящий файл правили": (lambda r: (r / INCOMING / "тз.md").write_text("другое"), "sha256"),
    "входящий файл без записи": (
        lambda r: (r / INCOMING / "лишний.md").write_text("x"), "нет в index.yaml"),
    "входящего файла нет": (lambda r: (r / INCOMING / "тз.md").unlink(), "нет файла"),
    "нет типа в Purpose": (lambda r: edit(r, "accounts", "Тип: сущность. ", ""), "Тип:"),
    "у сущности нет «Связи»": (lambda r: edit(r, "roles", "## Связи", "## Другое"), "нет раздела"),
    "у экрана «Сущности»": (lambda r: edit(r, "login", "## Тексты", "## Сущности"), "не бывает"),
    "нет строки источника": (
        lambda r: edit(r, "accounts", "**Источник:**", "Источник"), "Источник"),
    "источник после сценария": (
        lambda r: edit(r, "accounts", "**Источник:** ТЗ A2 (стр. 2)", "", "\n**Источник:** x\n"),
        "до первого сценария"),
    "заголовок повторяется": (
        lambda r: edit(r, "accounts", "", "", REQ_COPY), "повторяется"),
    "оценка в THEN": (lambda r: edit(r, "accounts", "вход закрыт", "вход работает корректно"),
                      "оценка"),
    "дельта в спеке": (lambda r: edit(r, "accounts", "подряд.", "подряд *(дельта 28.09)*."),
                       "история"),
    "состояние задачи": (lambda r: edit(r, "accounts", "без состояния", "закрыта"), "состояние"),
    "сущность у двух владельцев": (
        lambda r: edit(r, "roles", "### Роль", "### Роль\n\n### Учётная запись"), "не в одной"),
    "связь не по форме": (
        lambda r: edit(r, "accounts", "Роль (`roles`)", "Роль roles"), "не по форме"),
    "связь на чужую сущность": (
        lambda r: edit(r, "roles", "Роль — Учётная", "Права — Учётная"), "не объявлена в этой"),
    "связь называет не того владельца": (
        lambda r: edit(r, "accounts", "(`roles`)", "(`login`)"), "не объявлена в спеке login"),
}

REQ_COPY = "\n### Requirement: Блокировка входа\nСистема MUST.\n\n**Источник:** ТЗ A2 (стр. 2)\n"


def edit(root: Path, name: str, old: str, new: str, tail: str = "") -> None:
    path = root / SPECS / name / "spec.md"
    path.write_text(path.read_text(encoding="utf-8").replace(old, new) + tail, encoding="utf-8")


def make_project(root: Path) -> Path:
    (root / INCOMING).mkdir(parents=True)
    (root / ".backlog.json").write_text('{"prefix": "ab"}')
    for name, text in GOOD.items():
        (root / SPECS / name).mkdir(parents=True)
        (root / SPECS / name / "spec.md").write_text(text, encoding="utf-8")
    doc = root / INCOMING / "тз.md"
    doc.write_text("текст ТЗ", encoding="utf-8")
    entry = {"path": "docs/incoming/тз.md", "sha256": sha256(doc)}
    index = yaml.safe_dump([entry], allow_unicode=True)
    (root / INCOMING / "index.yaml").write_text(index, encoding="utf-8")
    return root


def selftest() -> int:
    return selftest_project(make_project, run, DEFECTS, lambda root, change: change(root))


if __name__ == "__main__":
    sys.exit(run_script(selftest, check_root(run, "спеки в порядке")))

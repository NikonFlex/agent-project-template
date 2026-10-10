"""Сверка требований с источником в обе стороны (openspec/README.md, «Строка источника»).

Запуск: python3 tools/source_check.py [корень]   корень — репозиторий или docs/examples/project
        python3 tools/source_check.py --selftest
Код возврата 0 — всё в порядке, 1 — есть нарушения (печатаются по строке).

1. Прямая сверка: каждая ссылка в строке `**Источник:**` требования и в столбце «Источник»
   таблиц спеки называет источник из «Источники и старшинство», и её строки на месте —
   как требует вид ссылки (VIA ниже).
2. Решения инженера: строка таблицы называет требование с источником «решение агента», и
   наоборот.
3. Карта `openspec/source-map.md`: перечень спек совпадает со спеками; пункт, записанный за
   спекой, в ней упомянут.
4. Обратная сверка: каждая непустая строка источника названа ссылкой, картой или записью
   реестра `openspec/reverse-ledger.md`; запись реестра — с классом и основанием.

Совпадение смысла требования с текстом строки прибор не проверяет — это делает инженер.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml
from script_common import check_root, replace_once, run_script, selftest_project, write_files
from spec_check import (
    INCOMING,
    SOURCE_LINE,
    addresses,
    declared,
    requirements,
    section,
    spec_files,
    table_rows,
    tables,
)

README = Path("openspec/README.md")
SOURCE_MAP = Path("openspec/source-map.md")
LEDGER = Path("openspec/reverse-ledger.md")
DECISION = re.compile(r"^решение агента \d\d\.\d\d\.\d{4} — \S")
REF = re.compile(r"^(?P<name>.+?) (?P<what>разд\. \S+|№\d+|\d+(?:\.\d+)*|[A-ZА-ЯЁ][\w-]*\d+)"
                 r" \(стр\. (?P<first>\d+)(?:[–-](?P<last>\d+))?\)$")
ADDRESS = re.compile(r"`([\w-]+): ([^`]+)`")
#: Код из ТЗ ищется не дальше стольких строк от названной: номер в ссылке ставят руками.
CODE_REACH = 3
CLASSES = ("служебное", "покрыто", "вне объёма")


@dataclass
class Ref:
    """Ссылка на строки источника: «ТЗ ST02 (стр. 420)»."""

    where: str
    name: str
    what: str
    first: int
    last: int


@dataclass
class Project:
    """Всё, что читает прибор: источники по имени в ссылках, спеки, карта, реестр."""

    root: Path
    sources: dict[str, list[str]]
    specs: dict[str, str]
    items: dict[tuple[str, str], tuple[int, int, str]]

    def lines(self, ref: Ref) -> list[str]:
        return self.sources[ref.name][ref.first - 1:ref.last]


# --------------------------------------------------------------------------- чтение


def readme_rows(root: Path, title: str) -> list[list[str]]:
    path = root / README
    return table_rows(section(path.read_text(encoding="utf-8"), title)) if path.exists() else []


def file_rows(root: Path, path: Path, title: str) -> list[list[str]]:
    full = root / path
    return table_rows(section(full.read_text(encoding="utf-8"), title)) if full.exists() else []


def load_sources(root: Path) -> tuple[dict[str, list[str]], list[str]]:
    """Имя в ссылках → строки файла; файлы — из «Источники и старшинство», только входящие."""
    sources, problems, listed = {}, [], set()
    for row in readme_rows(root, "## Источники и старшинство"):
        relative = row[1].strip("`")
        listed.add(relative)
        if not relative.startswith(f"{INCOMING}/") or not (root / relative).is_file():
            problems.append(f"источник «{row[2]}»: {row[1]} — нет такого файла в {INCOMING}/")
            continue
        sources[row[2]] = (root / relative).read_text(encoding="utf-8").splitlines()
    index = root / INCOMING / "index.yaml"
    entries = (yaml.safe_load(index.read_text(encoding="utf-8")) or []) if index.exists() else []
    problems += [f"источник {e['path']} принят (index.yaml), но не в «Источники и старшинство»"
                 for e in entries if e["path"] not in listed]
    return sources, problems


def span(cell: str) -> tuple[int, int]:
    first, _, last = cell.replace("–", "-").partition("-")
    return int(first), int(last or first)


def load_items(root: Path) -> dict[tuple[str, str], tuple[int, int, str]]:
    """(источник, пункт) → строки и спека (или «не вошло: …») из покрытия карты."""
    return {(row[0], row[1]): (*span(row[2]), row[3])
            for row in file_rows(root, SOURCE_MAP, "## Покрытие")}


def load(root: Path) -> tuple[Project, list[str]]:
    sources, problems = load_sources(root)
    return Project(root, sources, spec_files(root), load_items(root)), problems


def spec_refs(name: str, text: str) -> list[str]:
    """Ссылки спеки: строки «**Источник:**» требований и столбец «Источник» таблиц."""
    found = [(f"{name}: {title}", line.split(SOURCE_LINE, 1)[1])
             for title, body in requirements(text)
             for line in body.splitlines() if line.startswith(SOURCE_LINE)]
    for title in ("## Сущности", "## Тексты"):
        for header, rows in tables(section(text, title)):
            if header[-1] == "Источник":
                found += [(f"{name}: {row[0]}", row[-1]) for row in rows]
    return [(where, part.strip()) for where, cell in found for part in cell.split(";")]


# --------------------------------------------------------------------------- прямая сверка


def lines_exist(ref: Ref, project: Project) -> str:
    lines = project.lines(ref)
    if ref.first < 1 or len(lines) != ref.last - ref.first + 1:
        return "строк нет в источнике"
    if not lines[0].strip() or not lines[-1].strip():
        return "строка пуста"
    return ""


def check_code(ref: Ref, project: Project) -> str:
    if problem := lines_exist(ref, project):
        return problem
    near = project.sources[ref.name][max(ref.first - 1 - CODE_REACH, 0):ref.first + CODE_REACH]
    return "" if any(re.search(rf"\b{re.escape(ref.what)}\b", ln) for ln in near) \
        else f"кода {ref.what} нет в {CODE_REACH} строках от названной"


def check_item(ref: Ref, project: Project) -> str:
    item = project.items.get((ref.name, ref.what))
    if item is None:
        return f"пункта {ref.what} нет в карте источника"
    first, last, _ = item
    inside = first <= ref.first and ref.last <= last
    return lines_exist(ref, project) or ("" if inside else f"строки вне пункта ({first}–{last})")


def answer_block(lines: list[str], number: str) -> tuple[int, int] | None:
    """Строки блока «№K»: от строки с ним до следующего номера вопроса."""
    starts = [i for i, ln in enumerate(lines, 1) if re.search(r"№\d+\b", ln)]
    mine = [i for i in starts if re.search(rf"{number}\b", lines[i - 1])]
    if not mine:
        return None
    after = [i for i in starts if i > mine[0]]
    return mine[0], (after[0] - 1 if after else len(lines))


def check_answer(ref: Ref, project: Project) -> str:
    block = answer_block(project.sources[ref.name], ref.what)
    if block is None:
        return f"вопроса {ref.what} нет в источнике"
    inside = block[0] <= ref.first and ref.last <= block[1]
    return lines_exist(ref, project) or ("" if inside else f"строки вне блока {ref.what}")


#: Вид ссылки → как он узнаётся по тому, что стоит перед «(стр. …)», и чем проверяется.
#: Новый вид — сюда и в selftest: строки нет, строка пуста, строка вне блока.
VIA: dict[str, tuple[re.Pattern, Callable[[Ref, Project], str]]] = {
    "раздел": (re.compile(r"^разд\. "), lines_exist),
    "ответ": (re.compile(r"^№\d+$"), check_answer),
    "пункт": (re.compile(r"^\d+(?:\.\d+)*$"), check_item),
    "код": (re.compile(r"^[A-ZА-ЯЁ][\w-]*\d+$"), check_code),
}


def check_ref(where: str, text: str, project: Project) -> tuple[str, Ref | None]:
    """Нарушение (или пусто) и разобранная ссылка (None у решения агента и ошибки формы)."""
    if DECISION.match(text):
        return "", None
    match = REF.match(text)
    if not match:
        return f"{where}: ссылка «{text}» не по форме openspec/README.md", None
    ref = Ref(where, match["name"], match["what"], int(match["first"]),
              int(match["last"] or match["first"]))
    if ref.name not in project.sources:
        return f"{where}: источника «{ref.name}» нет в «Источники и старшинство»", None
    check = next(fn for pattern, fn in VIA.values() if pattern.match(ref.what))
    problem = check(ref, project)
    return (f"{where}: «{text}» — {problem}" if problem else ""), ref


# --------------------------------------------------------------------------- решения, карта


def decision_addresses(project: Project) -> set[str]:
    return {where for name, text in project.specs.items() for where, part in spec_refs(name, text)
            if DECISION.match(part) and any(where == f"{name}: {t}" for t, _ in requirements(text))}


def check_decisions(project: Project) -> list[str]:
    """Решение агента в требовании ↔ строка таблицы «Решения инженера»."""
    listed = {f"{m[1]}: {m[2]}" for row in readme_rows(project.root, "## Решения инженера")
              for m in ADDRESS.finditer(row[1])}
    needed = decision_addresses(project)
    return ([f"{a}: решение агента без строки в «Решения инженера»"
             for a in sorted(needed - listed)]
            + [f"«Решения инженера»: {a} — нет такого требования с решением агента"
               for a in sorted(listed - needed)])


def check_spec_list(project: Project) -> list[str]:
    """Перечень спек карты: каждая спека один раз, сущности и число требований совпадают."""
    rows = {row[0]: row for row in file_rows(project.root, SOURCE_MAP, "## Спеки")}
    owners = declared(project.specs)
    problems = [f"карта: спеки {name} нет в перечне" for name in project.specs if name not in rows]
    for name, row in rows.items():
        if name not in project.specs:
            problems.append(f"карта: в перечне спека {name}, её нет")
            continue
        mine = sorted(e for e, where in owners.items() if name in where) or ["—"]
        if sorted(e.strip() for e in row[3].split(",")) != mine:
            problems.append(f"карта: {name} объявляет {', '.join(mine)}, в перечне — {row[3]}")
        if row[5] != str(len(requirements(project.specs[name]))):
            problems.append(f"карта: у {name} требований не {row[5]}")
    return problems


def check_items(project: Project) -> list[str]:
    """Пункт, записанный за спекой, в ней упомянут; «не вошло» — с причиной."""
    problems = []
    for (source, item), (_, _, target) in project.items.items():
        if target.startswith("не вошло"):
            if not target.partition(":")[2].strip():
                problems.append(f"карта: {source} {item} — «не вошло» без причины")
        elif target not in project.specs:
            problems.append(f"карта: {source} {item} записан за спекой {target}, её нет")
        elif f"{source} {item} " not in project.specs[target]:
            problems.append(f"карта: {source} {item} записан за {target}, в спеке не упомянут")
    return problems


# --------------------------------------------------------------------------- обратная сверка


def check_ledger_row(row: list[str], project: Project, known: set[str]) -> str:
    source, line, start, cls, basis = row[:5]
    lines = project.sources.get(source)
    if lines is None or not line.isdigit() or not 0 < int(line) <= len(lines):
        return f"реестр: {source} стр. {line} — нет такой строки"
    if not lines[int(line) - 1].startswith(start.strip("«»")):
        return f"реестр: {source} стр. {line} — строка изменилась, запись снята: сверить заново"
    if cls not in CLASSES or not basis.strip():
        return f"реестр: {source} стр. {line} — класс не из {', '.join(CLASSES)} или нет основания"
    if cls == "покрыто" and not any(f"`{a}`" in basis for a in known):
        return f"реестр: {source} стр. {line} — «покрыто», но основание не называет требование"
    if cls == "вне объёма" and "README" not in basis:
        return f"реестр: {source} стр. {line} — «вне объёма», но основание не называет README"
    return ""


def check_reverse(project: Project, named: set[tuple[str, int]]) -> list[str]:
    """Каждая непустая строка источника названа ссылкой, картой или реестром."""
    known = addresses(project.specs)
    problems = []
    for row in ledger_rows(project.root):
        if problem := check_ledger_row(row, project, known):
            problems.append(problem)
        elif row[1].isdigit():
            named.add((row[0], int(row[1])))
    for (source, _), (first, last, _) in project.items.items():
        named.update((source, n) for n in range(first, last + 1))
    for source, lines in project.sources.items():
        problems += [f"обратная сверка: {source} стр. {n} «{ln[:40]}» не названа требованием, "
                     "картой или реестром"
                     for n, ln in enumerate(lines, 1) if ln.strip() and (source, n) not in named]
    return problems


def ledger_rows(root: Path) -> list[list[str]]:
    """Записи реестра — таблица, которая начинается со столбца «Источник»."""
    path = root / LEDGER
    found = tables(path.read_text(encoding="utf-8")) if path.exists() else []
    return [row for header, rows in found if header[0] == "Источник" for row in rows]


# --------------------------------------------------------------------------- сборка


def run(root: Path) -> list[str]:
    project, problems = load(root)
    named: set[tuple[str, int]] = set()
    for name, text in project.specs.items():
        for where, part in spec_refs(name, text):
            problem, ref = check_ref(where, part, project)
            problems += [problem] if problem else []
            if ref:
                named.update((ref.name, n) for n in range(ref.first, ref.last + 1))
    problems += check_decisions(project) + check_spec_list(project) + check_items(project)
    return problems + check_reverse(project, named)


# ---------------------------------------------------------------------------
# selftest: на чистом проекте нарушений нет, каждое подложенное — поймано
# ---------------------------------------------------------------------------

TZ = "# ТЗ\n\nAB01. Вход по почте.\nAB02. Пароль от 8 знаков.\n\nРаздел 2. Тексты\n«Войти»\n"
PRD = "# PRD\n\n2.1 Вход\nПочта и пароль.\n2.2 Выход\nКнопка «Выйти».\n"
ANSWERS = "# Ответы\n\n№1 Нужен ли Google?\nНет.\n№2 Срок сессии?\nСутки.\n"

FILES = {
    "docs/incoming/тз.md": TZ,
    "docs/incoming/prd.md": PRD,
    "docs/incoming/ответы.md": ANSWERS,
    "openspec/README.md": """# Требования

## Источники и старшинство

| № | Источник | В ссылках | Что это |
|---|---|---|---|
| 1 | `docs/incoming/ответы.md` | Ответы продукта | ответы |
| 2 | `docs/incoming/prd.md` | PRD | PRD |
| 3 | `docs/incoming/тз.md` | ТЗ | ТЗ |

## Решения инженера

| № | Спека и требование | Не определено | Принято | Меняет |
|---|---|---|---|---|
| 1 | `login: Выход` | — | — | — |
""",
    "openspec/specs/login/spec.md": """# Вход

## Purpose
Тип: сущность. Вход.

## Сущности

### Сессия

| Поле | Значение | Источник |
|---|---|---|
| срок | сутки | Ответы продукта №2 (стр. 5–6) |

## Requirements

### Requirement: Вход по почте
Система MUST.

**Источник:** ТЗ AB01 (стр. 3); PRD 2.1 (стр. 3–4)

#### Scenario: a

### Requirement: Длина пароля
Система MUST.

**Источник:** ТЗ AB02 (стр. 4); ТЗ разд. 2 (стр. 6–7); Ответы продукта №1 (стр. 3–4)

### Requirement: Выход
Система MUST.

**Источник:** решение агента 10.10.2026 — выход по кнопке; PRD 2.2 (стр. 5–6)
""",
    "openspec/source-map.md": """# Карта

## Спеки

| Спека | Тип | Что | Объявляет сущности | Ссылается на | Требований |
|---|---|---|---|---|---|
| login | сущность | вход | Сессия | — | 3 |

## Покрытие

| Источник | Пункт | Строки | Спека |
|---|---|---|---|
| PRD | 2.1 | 3-4 | login |
| PRD | 2.2 | 5-6 | login |
| ТЗ | AB01 | 3 | login |
""",
    "openspec/reverse-ledger.md": """# Реестр

| Источник | Строка | Начало | Класс | Основание |
|---|---|---|---|---|
| ТЗ | 1 | «# ТЗ» | служебное | заголовок |
| PRD | 1 | «# PRD» | служебное | заголовок |
| Ответы продукта | 1 | «# Ответы» | покрыто | `login: Длина пароля` |
""",
}

SPEC = "openspec/specs/login/spec.md"
#: Нарушение → правка (файл, было, стало) и кусок ожидаемого сообщения.
DEFECTS = {
    "строки нет": ((SPEC, "AB02 (стр. 4)", "AB02 (стр. 40)"), "строк нет"),
    "строка пуста": ((SPEC, "разд. 2 (стр. 6–7)", "разд. 2 (стр. 5–7)"), "строка пуста"),
    "код далеко от строки": ((SPEC, "AB01 (стр. 3)", "AB01 (стр. 7)"), "кода AB01"),
    "строка вне пункта": ((SPEC, "PRD 2.1 (стр. 3–4)", "PRD 2.1 (стр. 3–5)"), "вне пункта"),
    "пункта нет в карте": ((SPEC, "PRD 2.1 (стр. 3–4)", "PRD 2.3 (стр. 3–4)"), "нет в карте"),
    "строка вне блока": ((SPEC, "№2 (стр. 5–6)", "№1 (стр. 5–6)"), "вне блока"),
    "вопроса нет": ((SPEC, "№2 (стр. 5–6)", "№9 (стр. 5–6)"), "вопроса №9"),
    "источник не в таблице": ((SPEC, "ТЗ AB02", "ТЗ2 AB02"), "нет в «Источники"),
    "ссылка не по форме": ((SPEC, "ТЗ AB02 (стр. 4)", "ТЗ AB02 стр. 4"), "не по форме"),
    "решение без строки в таблице": (
        ("openspec/README.md", "`login: Выход`", "—"), "без строки в «Решения"),
    "строка таблицы без решения": (
        ("openspec/README.md", "`login: Выход`", "`login: Вход по почте`"), "нет такого"),
    "строка не названа и не в реестре": (
        ("openspec/reverse-ledger.md", "| ТЗ | 1 |", "| ТЗ | 2 |"), "стр. 1 «# ТЗ» не названа"),
    "принятый источник не в таблице": (
        ("openspec/README.md", "| 2 | `docs/incoming/prd.md` | PRD | PRD |\n", ""),
        "не в «Источники"),
    "источник не из входящих": (
        ("openspec/README.md", "`docs/incoming/prd.md`", "`prd.md`"), "нет такого файла"),
    "строка источника изменилась": (("docs/incoming/prd.md", "# PRD", "# Документ"), "изменилась"),
    "класс не из списка": (("openspec/reverse-ledger.md", "служебное | заголовок |",
                            "лишнее | заголовок |"), "класс"),
    "«покрыто» без требования": (
        ("openspec/reverse-ledger.md", "`login: Длина пароля`", "рядом"), "не называет требование"),
    "пункт карты не упомянут": (("openspec/source-map.md", "| ТЗ | AB01 | 3 | login |",
                                 "| ТЗ | AB09 | 3 | login |"), "не упомянут"),
    "спеки нет в перечне": (("openspec/source-map.md", "| login | сущность", "| auth | сущность"),
                            "нет в перечне"),
    "число требований не то": (("openspec/source-map.md", "| — | 3 |", "| — | 2 |"), "не 2"),
}


def make_project(root: Path) -> Path:
    write_files(root, FILES)
    index = [{"path": p} for p in FILES if p.startswith(f"{INCOMING}/")]
    (root / INCOMING / "index.yaml").write_text(yaml.safe_dump(index, allow_unicode=True))
    return root


def selftest() -> int:
    return selftest_project(make_project, run, DEFECTS, replace_once)


if __name__ == "__main__":
    sys.exit(run_script(selftest, check_root(run, "источники сверены")))

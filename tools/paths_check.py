"""Пользовательские пути: форма файлов и связь с требованиями (openspec/paths/README.md).

Запуск: python3 tools/paths_check.py [корень]   корень — репозиторий или docs/examples/project
        python3 tools/paths_check.py --selftest
Код возврата 0 — всё в порядке, 1 — есть нарушения (печатаются по строке).

1. Файл пути `<роль>-<NN>-<слово>.md`: заголовок, строка «Роль: … Релиз: …», разделы
   «Начальное состояние» и «Шаги»; у каждого шага — `Требования: `<спека>: <заголовок>``
   или «Требований нет», и тогда раздел «Чего нет в спеках» не пуст.
2. Каждое названное требование существует.
3. Каждое требование названо в пути или в `not-in-paths.md` с классом и основанием.
4. Таблица «экран → адрес → пути» README называет существующие пути.
"""

from __future__ import annotations

import re
from pathlib import Path

from script_common import check_root, replace_once, run_script, selftest_project, write_files
from spec_check import addresses, section, spec_files, table_rows

PATHS = Path("openspec/paths")
NOT_IN_PATHS = "not-in-paths.md"
SERVICE = {"README.md", NOT_IN_PATHS}
FILE_NAME = re.compile(r"^[^\W\d_]+-\d{2}-[\w-]+\.md$")
ROLE_LINE = re.compile(r"^Роль: .+?\. Релиз: (входит|не входит)\.$", re.M)
STEP = re.compile(r"^\d+\. ", re.M)
NAMED = re.compile(r"`([\w-]+: [^`]+)`")
NO_REQUIREMENTS = "Требований нет"
#: Почему требование не стоит ни в одном пути (раздел 9 процесса).
CLASSES = ("вне релиза", "хранение", "ответ модели", "миграция")


def path_files(root: Path) -> dict[str, str]:
    folder = root / PATHS
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.md"))
            if p.name not in SERVICE} if folder.is_dir() else {}


def steps(text: str) -> list[str]:
    """Шаги и ветви сбоя — по пункту на шаг, с продолжением на следующих строках."""
    body = section(text, "## Шаги") + section(text, "## Ветви сбоя")
    return [s.strip() for s in STEP.split(body)[1:]]


def check_form(name: str, text: str) -> list[str]:
    problems = [] if FILE_NAME.match(f"{name}.md") else [
        f"{name}: имя не по форме <роль>-<NN>-<слово>.md"]
    if not text.startswith("# ") or not ROLE_LINE.search(text):
        problems.append(f"{name}: нет заголовка или строки «Роль: … Релиз: входит|не входит.»")
    problems += [f"{name}: нет раздела «{t}»" for t in ("## Начальное состояние", "## Шаги")
                 if not section(text, t).strip()]
    marks = [s for s in steps(text) if "Требования:" not in s and NO_REQUIREMENTS not in s]
    problems += [f"{name}: шаг «{s[:40]}» — нет строки «Требования:» или «{NO_REQUIREMENTS}»"
                 for s in marks]
    if any(NO_REQUIREMENTS in s for s in steps(text)) \
            and not section(text, "## Чего нет в спеках").strip():
        problems.append(f"{name}: есть шаг без требований, а «Чего нет в спеках» пуст")
    return problems


def named_in(text: str) -> set[str]:
    return {m for s in steps(text) for m in NAMED.findall(s.split("Требования:", 1)[-1])}


def check_not_in_paths(root: Path, known: set[str]) -> tuple[set[str], list[str]]:
    path = root / PATHS / NOT_IN_PATHS
    rows = table_rows(path.read_text(encoding="utf-8")) if path.exists() else []
    listed, problems = set(), []
    for row in rows:
        address = row[0].strip("`")
        listed.add(address)
        if address not in known:
            problems.append(f"{NOT_IN_PATHS}: требования {address} нет")
        if row[1] not in CLASSES or not row[2].strip():
            problems.append(f"{NOT_IN_PATHS}: {address} — класс не из {', '.join(CLASSES)} "
                            "или нет основания")
    return listed, problems


def check_readme(root: Path, paths: dict[str, str]) -> list[str]:
    readme = root / PATHS / "README.md"
    rows = table_rows(readme.read_text(encoding="utf-8")) if readme.exists() else []
    return [f"README путей: {row[0]} называет путь {name}, его нет" for row in rows
            for name in (n.strip() for n in row[-1].split(",")) if name not in paths]


def run(root: Path) -> list[str]:
    known, paths = addresses(spec_files(root)), path_files(root)
    problems, named = [], set()
    for name, text in paths.items():
        problems += check_form(name, text)
        mine = named_in(text)
        named |= mine
        problems += [f"{name}: требования {a} нет" for a in sorted(mine - known)]
    listed, more = check_not_in_paths(root, known)
    problems += more + check_readme(root, paths)
    return problems + [f"требование {a} не названо ни в пути, ни в {NOT_IN_PATHS}"
                       for a in sorted(known - named - listed)]


# ---------------------------------------------------------------------------
# selftest: на чистом проекте нарушений нет, каждое подложенное — поймано
# ---------------------------------------------------------------------------

SPEC = """# Вход

## Requirements

### Requirement: Вход по почте
Система MUST.

### Requirement: Хранение пароля
Система MUST.
"""

PATH = """# В-01. Вход

Роль: гость. Релиз: входит.

## Начальное состояние

Учётная запись есть.

## Шаги

1. Гость вводит почту и пароль. Ожидаемо: вошёл.
   Требования: `login: Вход по почте`
2. Гость смотрит на часы. Ожидаемо: время.
   Требований нет.

## Чего нет в спеках

- Шаг 2: часы — источник молчит.
"""

FILES = {
    "openspec/specs/login/spec.md": SPEC,
    "openspec/paths/гость-01-вход.md": PATH,
    "openspec/paths/README.md": ("| Экран | Адрес | Пути |\n|---|---|---|\n"
                                 "| вход | / | гость-01-вход |\n"),
    "openspec/paths/not-in-paths.md": ("| Требование | Класс | Основание |\n|---|---|---|\n"
                                       "| `login: Хранение пароля` | хранение | не виден |\n"),
}
P = "openspec/paths/гость-01-вход.md"
NIP = "openspec/paths/not-in-paths.md"
DEFECTS = {
    "требования из пути нет": ((P, "`login: Вход по почте`", "`login: Вход по паролю`"), "нет"),
    "требование нигде не названо": ((NIP, "`login: Хранение пароля`", "`login: Иное`"),
                                    "Хранение пароля не названо"),
    "шаг без строки требований": ((P, "   Требований нет.\n", ""), "нет строки"),
    "«Чего нет в спеках» пуст": ((P, "- Шаг 2: часы — источник молчит.\n", ""), "пуст"),
    "нет строки роли": ((P, "Роль: гость. Релиз: входит.", "Гость."), "Роль"),
    "нет начального состояния": ((P, "## Начальное состояние", "## Начало"), "Начальное"),
    "класс не из списка": ((NIP, "| хранение |", "| прочее |"), "класс"),
    "README называет чужой путь": (("openspec/paths/README.md", "| гость-01-вход |",
                                    "| гость-02-выход |"), "его нет"),
}


def make_project(root: Path) -> Path:
    return write_files(root, FILES)


def selftest() -> int:
    return selftest_project(make_project, run, DEFECTS, replace_once)


if __name__ == "__main__":
    raise SystemExit(run_script(selftest, check_root(run, "пути в порядке")))

"""Связь задач с требованиями по копии трекера в docs/tasks/ (docs/agents/tasks.md).

Запуск: python3 tools/task_links.py [корень]   корень — репозиторий или docs/examples/project
        python3 tools/task_links.py --selftest
Код возврата 1 — задача ссылается на несуществующее требование. Перечни «требование без
задачи» и «открытая задача без ссылки на требование» печатаются сведениями и код не меняют:
до нарезки релиза на задачи они законно не пусты.

Ссылка задачи: `openspec/specs/<спека>/spec.md` — «<заголовок>», «<заголовок>»; задача без
требований говорит это строкой «без сценария — <причина>».

Ещё сведения: открытая задача без строки «Локальный повтор» в низу файла
(docs/agents/tasks.md); её же требует сторож перед созданием PR.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from backlog import MARK
from project_config import backlog_config
from script_common import check_root, replace_once, run_script, selftest_project, write_files
from spec_check import addresses, spec_files

LINK = re.compile(r"`openspec/specs/([\w-]+)/spec\.md` — ((?:«[^»]+»(?:, )?)+)")
TITLE = re.compile(r"«([^»]+)»")
NO_SCENARIO = re.compile(r"без сценария — \S", re.I)
OPEN = re.compile(r"^\|\s*\*\*Состояние\*\*\s*\|\s*OPEN", re.M)
#: Классификация до PR: как воспроизвели или почему повтор не нужен.
LOCAL_REPEAT = re.compile(r"^Локальный повтор: (воспроизведён — (тест|контур|экран): \S"
                          r"|(невозможен локально|не требуется|не бага) — \S)", re.M)
SERVICE = {"INDEX.md", "REVIEW.md"}


def task_texts(root: Path) -> dict[str, str]:
    """Имя файла задачи → верх (описание из трекера); черновики — целиком до метки."""
    folder = backlog_config(root)["tasks"]
    files = sorted(folder.glob("*.md")) if folder.is_dir() else []
    return {p.stem: p.read_text(encoding="utf-8").split(MARK, 1)[0]
            for p in files if p.name not in SERVICE}


def has_local_repeat(task_file: Path) -> bool:
    """В низу файла задачи есть строка «Локальный повтор» по форме."""
    text = task_file.read_text(encoding="utf-8")
    return MARK in text and bool(LOCAL_REPEAT.search(text.split(MARK, 1)[1]))


def links(text: str) -> set[str]:
    return {f"{spec}: {title}" for spec, titles in LINK.findall(text)
            for title in TITLE.findall(titles)}


def is_open(name: str, text: str) -> bool:
    return name.startswith("draft-") or bool(OPEN.search(text))


def collect(root: Path) -> tuple[list[str], list[str]]:
    """Нарушения и сведения."""
    known = addresses(spec_files(root))
    problems, unlinked, named = [], [], set()
    for name, text in task_texts(root).items():
        mine = links(text)
        named |= mine
        problems += [f"{name}: ссылается на требование {a}, его нет" for a in sorted(mine - known)]
        if not mine and not NO_SCENARIO.search(text) and is_open(name, text):
            unlinked.append(f"сведения: открытая задача {name} без требований и «без сценария»")
    orphans = [f"сведения: требование {a} не названо ни одной задачей"
               for a in sorted(known - named)]
    folder = backlog_config(root)["tasks"]
    no_repeat = [f"сведения: открытая задача {name} без «Локальный повтор»"
                 for name, text in task_texts(root).items()
                 if is_open(name, text) and not name.startswith("draft-")
                 and not has_local_repeat(folder / f"{name}.md")]
    return problems, unlinked + orphans + no_repeat


def run(root: Path) -> list[str]:
    problems, notes = collect(root)
    for note in notes:
        print(note)
    return problems


# ---------------------------------------------------------------------------
# selftest: на чистом проекте нарушений нет, каждое подложенное — поймано
# ---------------------------------------------------------------------------

TASK = f"""# ab#1 — Вход

| Поле | Значение |
|------|----------|
| **Состояние** | OPEN |

## Описание из трекера

`openspec/specs/login/spec.md` — «Вход по почте», «Выход»

{MARK}

`openspec/specs/login/spec.md` — «Чужое из низа»

Локальный повтор: воспроизведён — тест: tests/test_login.py::test_wrong_password
"""

FILES = {
    ".backlog.json": '{"prefix": "ab"}',
    "openspec/specs/login/spec.md": ("## Requirements\n\n### Requirement: Вход по почте\nx\n\n"
                                     "### Requirement: Выход\nx\n"),
    "docs/tasks/ab-0001.md": TASK,
    "docs/tasks/ab-0002.md": "# ab#2\n\n| **Состояние** | OPEN |\n\nБез сценария — доступы.\n",
}
T1 = "docs/tasks/ab-0001.md"
DEFECTS = {
    "ссылка на несуществующий заголовок": ((T1, "«Выход»", "«Выйти»"), "Выйти, его нет"),
    "ссылка на несуществующую спеку": (
        (T1, "specs/login/spec.md` — «Вход", "specs/auth/spec.md` — «Вход"), "auth: Вход по почте"),
}


def make_project(root: Path) -> Path:
    return write_files(root, FILES)


def selftest_notes() -> list[str]:
    """Сведения тоже проверяются: без ссылки — задача видна, низ файла ссылкой не считается."""
    with tempfile.TemporaryDirectory() as tmp:
        root = make_project(Path(tmp))
        replace_once(root, ("docs/tasks/ab-0002.md", "Без сценария — доступы.", "Доступы."))
        replace_once(root, (T1, ", «Выход»", ""))
        _, notes = collect(root)
    wanted = ["открытая задача ab-0002 без требований", "требование login: Выход не названо",
              "открытая задача ab-0002 без «Локальный повтор»"]
    return [f"не поймано в сведениях: {w}" for w in wanted if not any(w in n for n in notes)]


def selftest() -> int:
    missed = selftest_notes()
    for line in missed:
        print("✗ " + line)
    return selftest_project(make_project, lambda r: collect(r)[0], DEFECTS, replace_once) or (
        1 if missed else 0)


if __name__ == "__main__":
    raise SystemExit(run_script(selftest, check_root(run, "ссылки задач на требования в порядке")))

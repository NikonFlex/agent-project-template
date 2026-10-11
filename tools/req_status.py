"""Таблица требований и перенос статусов из записей прогона (docs/shared/product/).

Запуск: python3 tools/req_status.py [корень]           проверить таблицу, показать расхождения
        python3 tools/req_status.py [корень] --write   перенести статусы и пересчитать сводку
        python3 tools/req_status.py --selftest
Код 1 — таблица не сходится со спеками (требования нет, лишнее, повтор, статус не из списка,
сводка не та). Расхождение с записями прогонов — сведения, код не меняет.

Перенос: берётся последняя запись `openspec/runs/*.json` не с локального контура, где есть
сценарии, называющие требование. Все прошли — ✅ «проверено: прогон <дата>, <сценарии>»;
хоть один нет — ✅, 🚚 и ⚪ становятся 🟡 с первым несбывшимся ожиданием. Запись с
локального стенда статус не меняет. Чтение кода ✅ не даёт: ✅ ставит только прогон или
человек по испытанию, демонстрации или анализу.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from enum import StrEnum
from pathlib import Path

from script_common import replace_once, report, run_script, write_files
from spec_check import addresses, section, spec_files, tables

TABLE = Path("docs/shared/product/Таблица требований.md")
RUNS = Path("openspec/runs")
LOCAL = "local"
ADDRESS = re.compile(r"^`([\w-]+: [^`]+)`$")
COLUMNS = ["Область", "Требование", "Статус", "Задача", "Исполнитель", "Проверено"]


class Status(StrEnum):
    DONE = "✅"       # сделано и проверено на рабочем контуре
    PARTIAL = "🟡"    # частично, остаток записан
    SHIPPED = "🚚"    # код выложен, проверки на контуре ещё не было
    OTHERWISE = "🔁"  # сделано иначе по решению инженера
    NO = "❌"         # нет
    UNCHECKED = "⚪"  # не сверено


#: Что красный прогон делает со статусом; остальные статусы он не трогает.
RED_TURNS = {Status.DONE, Status.SHIPPED, Status.UNCHECKED}


def read_table(root: Path) -> tuple[list[list[str]], list[str]]:
    path = root / TABLE
    if not path.exists():
        return [], [f"нет файла {TABLE}"]
    found = [rows for header, rows in tables(section(path.read_text(encoding="utf-8"),
                                                     "## Требования")) if header == COLUMNS]
    return (found[0], []) if found else ([], [f"{TABLE}: нет таблицы с колонками {COLUMNS}"])


def check_table(root: Path, rows: list[list[str]]) -> list[str]:
    known = addresses(spec_files(root))
    seen: list[str] = []
    problems = []
    for row in rows:
        match = ADDRESS.match(row[1])
        if not match:
            problems.append(f"{TABLE}: «{row[1]}» — не адрес `<спека>: <заголовок>`")
            continue
        seen.append(match[1])
        if row[2] not in {s.value for s in Status}:
            problems.append(f"{TABLE}: {match[1]} — статус «{row[2]}» не из списка")
    problems += [f"{TABLE}: {a} — требования нет в спеках" for a in sorted(set(seen) - known)]
    problems += [f"{TABLE}: {a} — нет строки" for a in sorted(known - set(seen))]
    problems += [f"{TABLE}: {a} — строка повторяется" for a in sorted(set(seen))
                 if seen.count(a) > 1]
    return problems


def summary(rows: list[list[str]]) -> str:
    areas = sorted({row[0] for row in rows})
    lines = ["| Область | Всего | " + " | ".join(s.value for s in Status) + " |",
             "|---|---|" + "---|" * len(Status)]
    for area in [*areas, "Всего"]:
        mine = [r for r in rows if area in (r[0], "Всего")]
        counts = " | ".join(str(sum(r[2] == s.value for r in mine)) for s in Status)
        lines.append(f"| {area} | {len(mine)} | {counts} |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- перенос


def latest_verdicts(root: Path) -> dict[str, tuple[bool, str]]:
    """Требование → (зелёное?, пометка) по последней не локальной записи, где оно есть."""
    verdicts: dict[str, tuple[bool, str]] = {}
    for path in sorted((root / RUNS).glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["contour"] == LOCAL:
            continue
        mine: dict[str, list[dict]] = {}
        for result in record["scenarios"]:
            for address in result.get("требования", []):
                mine.setdefault(address, []).append(result)
        for address, results in mine.items():
            failed = [r for r in results if r["итог"] != "прошёл"]
            names = ", ".join(r["name"] for r in results)
            verdicts[address] = (not failed, f"прогон {record['time'][:10]} {record['contour']}, "
                                 f"{names}" if not failed else
                                 f"не сбылось ({failed[0]['name']}): {failed[0]['не сбылось']}")
    return verdicts


def transfer(rows: list[list[str]], verdicts: dict[str, tuple[bool, str]]) -> list[str]:
    """Правит строки на месте; возвращает, что поменялось."""
    changes = []
    for row in rows:
        address = row[1].strip("`")
        if address not in verdicts:
            continue
        green, note = verdicts[address]
        status = Status.DONE.value if green else (
            Status.PARTIAL.value if Status(row[2]) in RED_TURNS else row[2])
        note = f"проверено: {note}" if green else note
        if (status, note) != (row[2], row[5]):
            changes.append(f"{address}: {row[2]} → {status} ({note})")
            row[2], row[5] = status, note
    return changes


def write_table(root: Path, rows: list[list[str]]) -> None:
    path = root / TABLE
    text = path.read_text(encoding="utf-8")
    body = "\n".join(["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
                     + ["| " + " | ".join(r) + " |" for r in rows])
    head, _, rest = text.partition("## Требования")
    tail = rest.split("\n## ", 1)[1] if "\n## " in rest else ""
    head = re.sub(r"## Сводка\s*\n.*?(?=\n## |\Z)",
                  lambda _: "## Сводка\n\n" + summary(rows) + "\n\n", head, flags=re.S)
    path.write_text(head + "## Требования\n\n" + body + "\n" + ("\n## " + tail if tail else ""),
                    encoding="utf-8")


def stale_summary(root: Path, rows: list[list[str]]) -> list[str]:
    block = section((root / TABLE).read_text(encoding="utf-8"), "## Сводка").strip()
    return [] if block == summary(rows) else [f"{TABLE}: сводка не сходится — пересчитает --write"]


def run(root: Path, write: bool = False, say=print) -> list[str]:
    rows, problems = read_table(root)
    if problems:
        return problems
    problems = check_table(root, rows)
    if problems:
        return problems
    changes = transfer(rows, latest_verdicts(root))
    for change in changes:
        say(("перенесено: " if write else "сведения: прогон говорит ") + change)
    if write:
        write_table(root, rows)
        return []
    return [] if changes else stale_summary(root, rows)


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------

SPEC = "## Requirements\n\n### Requirement: Вход\nx\n\n### Requirement: Выход\nx\n"
TABLE_TEXT = """# Таблица требований

## Сводка

{summary}

## Требования

| Область | Требование | Статус | Задача | Исполнитель | Проверено |
|---|---|---|---|---|---|
| вход | `auth: Вход` | 🚚 | ab#1 | — | — |
| вход | `auth: Выход` | ✅ | ab#2 | — | проверено: демонстрация 01.10 |
"""


def record(contour: str, results: list[tuple[str, str, str]],
           time: str = "2026-10-11T00:00") -> str:
    return json.dumps({"contour": contour, "time": time, "commits": {}, "scenarios": [
        {"name": n, "требования": [a], "итог": v, "не сбылось": "" if v == "прошёл" else "x≠y"}
        for n, a, v in results]}, ensure_ascii=False)


def make_project(root: Path, runs: dict[str, str]) -> Path:
    rows = [["вход", "`auth: Вход`", "🚚"], ["вход", "`auth: Выход`", "✅"]]
    files = {"openspec/specs/auth/spec.md": SPEC,
             str(TABLE): TABLE_TEXT.format(summary=summary(rows)),
             **{f"{RUNS}/{name}.json": text for name, text in runs.items()}}
    return write_files(root, files)


def statuses(root: Path) -> list[str]:
    return [r[2] for r in read_table(root)[0]]


def quiet(_: str) -> None:
    """Самопроверка печатает только итог."""


def selftest() -> int:
    green = record("dev", [("s1", "auth: Вход", "прошёл")])
    red = record("dev", [("s2", "auth: Выход", "не прошёл")], "2026-10-11T01:00")
    local = record(LOCAL, [("s1", "auth: Вход", "не прошёл")], "2026-10-11T02:00")
    cases = {
        "зелёная запись → ✅": ({"1": green}, ["✅", "✅"]),
        "красная запись → 🟡": ({"1": red}, ["🚚", "🟡"]),
        "локальная запись не меняет": ({"1": local}, ["🚚", "✅"]),
        "последняя запись важнее": ({"1": red, "2": record("dev", [("s2", "auth: Выход", "прошёл")],
                                                            "2026-10-11T03:00")}, ["🚚", "✅"]),
    }
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, (runs, wanted) in cases.items():
            root = make_project(Path(tmp) / name, runs)
            run(root, write=True, say=quiet)
            after = run(root, say=quiet)
            if statuses(root) != wanted or after:
                bad.append(f"{name}: {statuses(root)}, ждали {wanted}; после записи: {after}")
        for name, change, expected in [
            ("требования нет в таблице", ("| вход | `auth: Выход` |", "| вход | `auth: Вход` |"),
             "Выход — нет строки"),
            ("статус не из списка", ("| `auth: Вход` | 🚚 |", "| `auth: Вход` | ok |"),
             "не из списка"),
            ("сводка не та", ("| вход | 2 |", "| вход | 3 |"), "сводка"),
        ]:
            root = make_project(Path(tmp) / name, {})
            replace_once(root, (str(TABLE), *change))
            found = run(root, say=quiet)
            if not any(expected in p for p in found):
                bad.append(f"не поймано: {name} — {found}")
    return report(bad, "перенос: зелёная, красная, локальная, последняя запись; "
                       "таблица: пропуск, статус, сводка")


def check(root: Path) -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    problems = run(Path(args[0]) if args else root, write="--write" in sys.argv)
    for problem in problems:
        print(problem)
    print("таблица требований в порядке" if not problems else f"нарушений: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(run_script(selftest, check))

#!/usr/bin/env python3
"""Аудит спек: не пора ли сжать историю — дельты, «подтвердил», источники (навык /spec-audit).

    python3 tools/spec_audit.py                       по каждой спеке «в порядке» или «пора сжать»
                                                      и почему: чем мешает признак, строки, прогноз
    python3 tools/spec_audit.py --compare old new     после сжатия смысл тот же?
    python3 tools/spec_audit.py --selftest

Это датчик, а не гейт: код возврата аудита всегда 0. Пороги — tools/spec_audit.json.
`--compare` сверяет сущности, правила и критерии приёмки без пометок: сжатие убирает
пометки, а формулировок не меняет. Код 1 — смысл разошёлся.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml
from backlog import MARK, load_tasks
from project_config import backlog_config
from script_common import report, run_script
from spec_check import DELTA, INCOMING, ref_number, spec_files, task_ref

# Пометки происхождения целиком: *(дельта …)* и *(подтвердил …)*.
MARKER = re.compile(r"\s*\*\((?:дельта|подтвердил)\b[^)]*\)\*")
ITEM = re.compile(r"^\s*(?:\d+\.|-|###|\|)\s?")
CANON = ("Сущности", "Правила", "Критерии приёмки")
DEAD = ("superseded", "rejected")


@dataclass
class Item:
    """Пункт списка, строка таблицы или заголовок ###: строки first..last и текст."""

    first: int
    last: int
    text: str


def numbered_items(lines: list[tuple[int, str]]) -> list[Item]:
    """Пункты с номерами строк; продолжение пункта приклеивается к нему до заголовка `## `."""
    out: list[Item] = []
    glue = False
    for n, line in lines:
        if ITEM.match(line):
            out.append(Item(n, n, line.strip()))
            glue = True
        elif line.startswith("## "):
            glue = False
        elif line.strip() and glue:
            out[-1] = Item(out[-1].first, n, out[-1].text + " " + line.strip())
    return out


def all_items(text: str) -> list[Item]:
    return numbered_items(list(enumerate(text.splitlines(), 1)))


def items(text: str) -> list[str]:
    return [item.text for item in all_items(text)]


def section(text: str, name: str) -> str:
    match = re.search(rf"^## {re.escape(name)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return match.group(1) if match else ""


def section_lines(text: str, name: str) -> list[tuple[int, str]]:
    """Строки раздела `## name` с номерами строк файла."""
    out, inside = [], False
    for n, line in enumerate(text.splitlines(), 1):
        if line.startswith("## "):
            inside = line[3:].strip() == name
        elif inside:
            out.append((n, line))
    return out


@dataclass
class Tasks:
    """Что аудит знает о задачах: какие открыты, какие закрыты давно и когда."""

    ref: re.Pattern
    prefix: str
    open: set[int]
    stale: dict[int, str]

    def name(self, number: int) -> str:
        return f"{self.prefix}#{number}" if self.prefix else f"#{number}"


def load_states(root: Path, days: int) -> Tasks:
    cfg = backlog_config(root)
    opened, stale = set(), {}
    for task in load_tasks(cfg["tasks"]):
        number = re.search(r"-(\d+)$", task.name)
        closed = task.fields.get("Закрыта", "—")
        if not number:
            continue
        if task.is_open:
            opened.add(int(number.group(1)))
        elif closed != "—":
            if (datetime.now() - datetime.strptime(closed[:10], "%Y-%m-%d")).days > days:
                stale[int(number.group(1))] = closed[:10]
    return Tasks(task_ref(cfg["prefix"]), cfg["prefix"] or "", opened, stale)


@dataclass
class Delta:
    line: int
    date: str
    tasks: list[int]


def deltas_in(text: str, ref: re.Pattern) -> list[Delta]:
    """Дельты по всему тексту: пометка бывает перенесена на следующую строку."""
    out = []
    for m in DELTA.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        date = m.group(1).strip().split(",")[0].strip()
        out.append(Delta(line, date, [ref_number(r) for r in ref.finditer(m.group(1))]))
    return out


def chain_groups(text: str, deltas: list[Delta]) -> list[tuple[Item, list[Delta]]]:
    """Пункты, на которых две дельты и больше: пункт и его дельты по порядку."""
    out = []
    for item in all_items(text):
        group = [d for d in deltas if item.first <= d.line <= item.last]
        if len(group) >= 2:
            out.append((item, group))
    return out


def dead_sources(root: Path, head: str) -> list[str]:
    path = root / INCOMING / "index.yaml"
    index = yaml.safe_load(path.read_text(encoding="utf-8")) or [] if path.exists() else []
    return [e["path"] for e in index
            if e.get("status") in DEAD and Path(e["path"]).name in head]


@dataclass
class Sign:
    """Признак: значение и строки-доказательства — где в спеке он сработал."""

    value: float
    where: list[str] = field(default_factory=list)


#: Чем мешает признак — почему из-за него стоит сжать спеку.
WHY = {
    "deltas": "история правок перемешана с требованиями: правило читается сквозь пометки",
    "provenance_share": "пометки занимают заметную часть текста — требования тонут в истории",
    "chains": "пункт правили несколько раз: действует последняя правка, а читать приходится все",
    "stale": "задачи давно закрыты — пометка уже ничего не говорит, история есть в задаче",
    "dead_sources": "в «Источниках» заменённый или отклонённый документ — ведёт в неактуальное",
    "closed_questions": "вопрос уже решён и ответ стоит в правилах, а в «Открыто» он сбивает",
}

LABELS = {"deltas": "дельт", "provenance_share": "доля пометок", "chains": "цепочек дельт",
          "stale": "дельт по задачам, закрытым давно", "dead_sources": "мёртвых источников",
          "closed_questions": "закрытых вопросов в «Открыто»"}


def lines_by_task(deltas: list[Delta], tasks: Tasks, note: dict[int, str]) -> list[str]:
    """«ab#38 (заметка): строки 27, 28» — по задаче на строку; дельты без задачи — отдельно."""
    groups: dict[int | None, list[int]] = {}
    for d in deltas:
        for number in d.tasks or [None]:
            groups.setdefault(number, []).append(d.line)
    out = []
    for number, lines in sorted(groups.items(), key=lambda g: -len(g[1])):
        who = tasks.name(number) + note.get(number, "") if number else "без задачи"
        out.append(f"{who}: строки {', '.join(map(str, sorted(set(lines))))}")
    return out


def chains(text: str, deltas: list[Delta], tasks: Tasks) -> list[str]:
    """Цепочки словами: «пункт на строках 58–59: 28.09 ab#38 → 29.09 ab#11»."""
    out = []
    for item, group in chain_groups(text, deltas):
        where = f"строках {item.first}–{item.last}" if item.last > item.first \
            else f"строке {item.first}"
        steps = " → ".join(" ".join([d.date, *map(tasks.name, d.tasks)]) for d in group)
        out.append(f"пункт на {where}: {steps}")
    return out


def measure(root: Path, path: Path, tasks: Tasks) -> dict[str, Sign]:
    text = path.read_text(encoding="utf-8")
    deltas = deltas_in(text, tasks.ref)
    stale = [d for d in deltas if any(t in tasks.stale for t in d.tasks)]
    marked = sum(len(m.group(0)) for m in MARKER.finditer(text))
    questions = [f"{i.first}: {i.text[:70]}" for i in
                 numbered_items(section_lines(text, "Открыто")) if "подтвердил" in i.text]
    dead = dead_sources(root, text.split("\n## ", 1)[0])
    closed_note = {n: f" (закрыта {day})" for n, day in tasks.stale.items()}
    return {
        "deltas": Sign(len(deltas), lines_by_task(deltas, tasks, {})),
        "provenance_share": Sign(round(marked / max(len(text), 1), 3),
                                 [f"{marked} знаков пометок из {len(text)}"]),
        "chains": Sign(len(chain_groups(text, deltas)), chains(text, deltas, tasks)),
        "stale": Sign(len(stale), lines_by_task(stale, tasks, closed_note)),
        "dead_sources": Sign(len(dead), dead),
        "closed_questions": Sign(len(questions), questions),
    }


def survivors(text: str, tasks: Tasks) -> list[Delta]:
    """Дельты, которые сжатие оставит (навык /spec-audit): по открытым задачам и без задачи;
    у цепочки — только последняя, если её задача открыта."""
    deltas = deltas_in(text, tasks.ref)
    not_last = {id(d) for _, group in chain_groups(text, deltas) for d in group[:-1]}
    return [d for d in deltas
            if (not d.tasks or any(t in tasks.open for t in d.tasks)) and id(d) not in not_last]


def outcome(text: str, tasks: Tasks) -> str:
    """Сколько пометок дельт уйдёт при сжатии и какие останутся."""
    total, kept = len(deltas_in(text, tasks.ref)), survivors(text, tasks)
    why = sorted({tasks.name(t) for d in kept for t in d.tasks if t in tasks.open})
    reasons = ([f"задачи открыты: {', '.join(why)}"] if why else []) + (
        [f"без задачи: {sum(not d.tasks for d in kept)}"] if any(not d.tasks for d in kept) else [])
    left = f", останется {len(kept)} ({'; '.join(reasons)})" if kept else ", останется 0"
    return f"при сжатии уйдёт пометок дельт: {total - len(kept)} из {total}{left}"


def shown(key: str, value: float) -> str:
    return f"{value:.0%}" if key == "provenance_share" else str(value)


def explain(key: str, sign: Sign, cfg: dict) -> list[str]:
    head = f"  · {LABELS[key]} {shown(key, sign.value)} (порог {shown(key, cfg[key + '_from'])})"
    return [f"{head} — {WHY[key]}", *(f"      {line}" for line in sign.where)]


def audit(root: Path, cfg: dict) -> list[str]:
    """По спеке: «в порядке» или «пора сжать» — и под ней почему: что значит каждый
    превышенный признак, где он в спеке и что уйдёт при сжатии."""
    tasks = load_states(root, cfg["stale_after_days"])
    lines = []
    for path in spec_files(root):
        signs = measure(root, path, tasks)
        over = {k: s for k, s in signs.items() if s.value >= cfg[k + "_from"]}
        if not over:
            lines.append(f"{path.name}: в порядке")
            continue
        names = "; ".join(f"{LABELS[k]} {shown(k, s.value)}" for k, s in over.items())
        lines.append(f"{path.name}: пора сжать — {names}")
        for key, sign in over.items():
            lines += explain(key, sign, cfg)
        lines.append("  · " + outcome(path.read_text(encoding="utf-8"), tasks))
    return lines


def canon(text: str) -> list[str]:
    """Сущности, правила, критерии без пометок происхождения и лишних пробелов."""
    clean = MARKER.sub("", text)
    return [" ".join(i.split()) for name in CANON for i in items(section(clean, name))]


def compare(old: str, new: str) -> list[str]:
    before, after = canon(old), canon(new)
    return ([f"пропало: {i}" for i in before if i not in after]
            + [f"появилось: {i}" for i in after if i not in before])


def run(root: Path) -> int:
    args = sys.argv[1:]
    if args[:1] == ["--compare"] and len(args) == 3:
        diff = compare(*(Path(a).read_text(encoding="utf-8") for a in args[1:]))
        print("\n".join(diff) or "смысл тот же: сущности, правила, критерии совпали")
        return 1 if diff else 0
    cfg = json.loads((root / "tools/spec_audit.json").read_text(encoding="utf-8"))
    print("\n".join(audit(root, cfg)) or "спек нет")
    return 0



# ---------------------------------------------------------------------------
# selftest: чистая спека — «в порядке», каждый признак ловится, --compare видит правку
# ---------------------------------------------------------------------------

CLEAN = """# Accounts

> **Статус: provisional.**
> Источники: docs/incoming/тз.md.
> Владелец дельт: хозяин.

## Сущности

### User

## Правила

1. Вход по почте *(дельта 28.09, ab#3)*.
2. Почта в нижнем регистре.

## Критерии приёмки

- Неверный пароль 5 раз — вход закрыт на 15 минут.

## Открыто

- Нужен ли вход через Google.
"""

CFG = {"deltas_from": 3, "provenance_share_from": 0.15, "chains_from": 1, "stale_after_days": 30,
       "stale_from": 2, "dead_sources_from": 1, "closed_questions_from": 1}

#: Признак → правка чистой спеки, которая его вызывает, и доказательство, которое объяснение
#: должно показать (стар — `ab#3`, закрыта в 2020).
CHAIN = ("2. Почта в нижнем регистре.", "2. Почта *(дельта 1.10, ab#5)* *(дельта 2.10, ab#5)*.")
SPOILS = {
    "deltas": (*CHAIN, "ab#5: строки 14"),
    "chains": (*CHAIN, "пункт на строке 14: 1.10 ab#5 → 2.10 ab#5"),
    "provenance_share": ("2. Почта в нижнем регистре.", "2. Почта *(подтвердил хозяин 30.09, по "
                         "письму от заказчика и созвону, ТЗ v2 §3, было «в любом регистре»)*.",
                         "знаков пометок из"),
    "stale": ("2. Почта в нижнем регистре.", "2. Почта *(дельта 1.10, ab#3)*.",
              "ab#3 (закрыта 2020-01-01): строки 13, 14"),
    "dead_sources": ("docs/incoming/тз.md.", "docs/incoming/тз.md, docs/incoming/старое.md.",
                     "docs/incoming/старое.md"),
    "closed_questions": ("- Нужен ли вход через Google.", "- Нужен ли вход через Google.\n"
                         "- Пароль? *(подтвердил хозяин 30.09)*", "23: - Пароль?"),
}
#: Прогноз сжатия: уходят дельта по закрытой ab#3 (п. 1) и начало цепочки п. 2 — хоть её
#: задача ab#7 открыта; остаются последняя в цепочке, одиночная по ab#7 и дельта без задачи.
OUTCOME = ("2. Почта в нижнем регистре.", "2. Почта *(дельта 1.10, ab#7)* *(дельта 2.10, ab#7)*.\n"
           "3. Пароль *(дельта 3.10, ab#7)*.\n4. Код *(дельта 4.10, incoming/тз.md)*.",
           "уйдёт пометок дельт: 2 из 5, останется 3 (задачи открыты: ab#7; без задачи: 1)")


def make_project(root: Path, spec: str) -> Path:
    for folder in ("specs", "docs/incoming", "docs/tasks", "tools"):
        (root / folder).mkdir(parents=True)
    (root / ".backlog.json").write_text('{"prefix": "ab"}', encoding="utf-8")
    (root / "tools/spec_audit.json").write_text(json.dumps(CFG), encoding="utf-8")
    index = [{"path": "docs/incoming/тз.md", "status": "distilled"},
             {"path": "docs/incoming/старое.md", "status": "superseded"}]
    (root / INCOMING / "index.yaml").write_text(yaml.safe_dump(index), encoding="utf-8")
    (root / "docs/tasks/ab-0003.md").write_text(
        "# ab#3 — старое\n\n| **Состояние** | CLOSED |\n| **Закрыта** | 2020-01-01 10:00 |\n"
        f"{MARK}\n", encoding="utf-8")
    (root / "docs/tasks/ab-0007.md").write_text(
        f"# ab#7 — в работе\n\n| **Состояние** | OPEN |\n| **Закрыта** | — |\n{MARK}\n",
        encoding="utf-8")
    (root / "specs/accounts.md").write_text(spec, encoding="utf-8")
    return root


def selftest() -> int:
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        clean = audit(make_project(Path(tmp) / "clean", CLEAN), CFG)
        if clean != ["accounts.md: в порядке"]:
            bad.append(f"чистая спека: {clean}")
        for key, (old, new, evidence) in [*SPOILS.items(), ("outcome", OUTCOME)]:
            out = "\n".join(audit(make_project(Path(tmp) / key, CLEAN.replace(old, new)), CFG))
            wanted = (LABELS.get(key, ""), WHY.get(key, ""), evidence)
            bad += [f"{key}: в выводе нет «{w}»:\n{out}" for w in wanted if w not in out]
    stripped = CLEAN.replace(" *(дельта 28.09, ab#3)*", "")
    reworded = CLEAN.replace("Почта в нижнем регистре", "Почта в любом регистре")
    if compare(CLEAN, stripped) or not compare(CLEAN, reworded):
        bad.append("--compare: снятие пометки — не правка смысла, переформулировка — правка")
    passed = (f"чистая спека в порядке, признаков поймано и объяснено {len(SPOILS)}, "
              "прогноз сжатия верен, --compare различает")
    return report(bad, passed)


if __name__ == "__main__":
    sys.exit(run_script(selftest, run))

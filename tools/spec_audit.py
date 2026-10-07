#!/usr/bin/env python3
"""Аудит спек: не пора ли сжать историю — дельты, «подтвердил», источники (навык /spec-audit).

    python3 tools/spec_audit.py                       числа по каждой спеке и «пора сжать»
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
from datetime import datetime
from pathlib import Path

import yaml
from backlog import MARK, load_tasks
from project_config import backlog_config
from script_common import report, run_script
from spec_check import DELTA, INCOMING, open_section, ref_number, spec_files, task_ref

# Пометки происхождения целиком: *(дельта …)* и *(подтвердил …)*.
MARKER = re.compile(r"\s*\*\((?:дельта|подтвердил)\b[^)]*\)\*")
ITEM = re.compile(r"^\s*(?:\d+\.|-|###|\|)\s?")
CANON = ("Сущности", "Правила", "Критерии приёмки")
DEAD = ("superseded", "rejected")


def items(text: str) -> list[str]:
    """Пункты списка, строки таблиц и заголовки ###: строка с началом пункта и её продолжение."""
    out: list[str] = []
    for line in text.splitlines():
        if ITEM.match(line):
            out.append(line.strip())
        elif line.strip() and out:
            out[-1] += " " + line.strip()
    return out


def section(text: str, name: str) -> str:
    match = re.search(rf"^## {re.escape(name)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return match.group(1) if match else ""


def closed_tasks(root: Path, days: int) -> set[int]:
    """Номера задач, закрытых больше `days` дней назад."""
    out = set()
    for task in load_tasks(backlog_config(root)["tasks"]):
        number = re.search(r"-(\d+)$", task.name)
        closed = task.fields.get("Закрыта", "—")
        if number and not task.is_open and closed != "—":
            age = datetime.now() - datetime.strptime(closed[:10], "%Y-%m-%d")
            if age.days > days:
                out.add(int(number.group(1)))
    return out


def dead_sources(root: Path, head: str) -> list[str]:
    path = root / INCOMING / "index.yaml"
    index = yaml.safe_load(path.read_text(encoding="utf-8")) or [] if path.exists() else []
    return [e["path"] for e in index
            if e.get("status") in DEAD and Path(e["path"]).name in head]


def measure(root: Path, path: Path, stale: set[int], ref: re.Pattern) -> dict[str, float]:
    text = path.read_text(encoding="utf-8")
    deltas = DELTA.findall(text)
    marked = sum(len(m.group(0)) for m in MARKER.finditer(text))
    return {
        "deltas": len(deltas),
        "provenance_share": round(marked / max(len(text), 1), 3),
        "chains": sum(1 for item in items(text) if len(DELTA.findall(item)) >= 2),
        "stale": sum(1 for d in deltas for m in ref.finditer(d) if ref_number(m) in stale),
        "dead_sources": len(dead_sources(root, text.split("\n## ", 1)[0])),
        "closed_questions": sum("подтвердил" in i for i in items(open_section(text))),
    }


LABELS = {"deltas": "дельт", "provenance_share": "доля пометок", "chains": "цепочек дельт",
          "stale": "дельт по задачам, закрытым давно", "dead_sources": "мёртвых источников",
          "closed_questions": "закрытых вопросов в «Открыто»"}


def shown(key: str, value: float) -> str:
    return f"{value:.0%}" if key == "provenance_share" else str(value)


def audit(root: Path, cfg: dict) -> list[str]:
    """Строка на спеку: «пора сжать» с причинами или «в порядке»."""
    stale = closed_tasks(root, cfg["stale_after_days"])
    ref = task_ref(backlog_config(root)["prefix"])
    lines = []
    for path in spec_files(root):
        numbers = measure(root, path, stale, ref)
        over = [f"{LABELS[k]} {shown(k, v)} (порог {shown(k, cfg[k + '_from'])})"
                for k, v in numbers.items() if v >= cfg[k + "_from"]]
        verdict = "пора сжать — " + "; ".join(over) if over else "в порядке"
        lines.append(f"{path.name}: {verdict}")
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

#: Признак → правка чистой спеки, которая его вызывает (стар — `ab#3`, закрыта в 2020).
SPOILS = {
    "дельт": ("2. Почта в нижнем регистре.",
              "2. Почта *(дельта 1.10, ab#5)* *(дельта 2.10, ab#5)*."),
    "цепочек дельт": ("2. Почта в нижнем регистре.",
                      "2. Почта *(дельта 1.10, ab#5)* *(дельта 2.10, ab#5)*."),
    "доля пометок": ("2. Почта в нижнем регистре.", "2. Почта *(подтвердил хозяин 30.09, по письму "
                     "от заказчика и созвону, ТЗ v2 §3, было «в любом регистре»)*."),
    "закрытым давно": ("2. Почта в нижнем регистре.", "2. Почта *(дельта 1.10, ab#3)*."),
    "мёртвых источников": ("docs/incoming/тз.md.", "docs/incoming/тз.md, docs/incoming/старое.md."),
    "закрытых вопросов": ("- Нужен ли вход через Google.",
                          "- Нужен ли вход через Google.\n- Пароль? *(подтвердил хозяин 30.09)*"),
}


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
    (root / "specs/accounts.md").write_text(spec, encoding="utf-8")
    return root


def selftest() -> int:
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        clean = audit(make_project(Path(tmp) / "clean", CLEAN), CFG)
        if clean != ["accounts.md: в порядке"]:
            bad.append(f"чистая спека: {clean}")
        for i, (label, (old, new)) in enumerate(SPOILS.items()):
            line = audit(make_project(Path(tmp) / str(i), CLEAN.replace(old, new)), CFG)[0]
            if label not in line:
                bad.append(f"не пойман признак «{label}»: {line}")
    stripped = CLEAN.replace(" *(дельта 28.09, ab#3)*", "")
    reworded = CLEAN.replace("Почта в нижнем регистре", "Почта в любом регистре")
    if compare(CLEAN, stripped) or not compare(CLEAN, reworded):
        bad.append("--compare: снятие пометки — не правка смысла, переформулировка — правка")
    passed = f"чистая спека в порядке, признаков поймано {len(SPOILS)}, --compare различает"
    return report(bad, passed)


if __name__ == "__main__":
    sys.exit(run_script(selftest, run))

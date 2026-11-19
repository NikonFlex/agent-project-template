#!/usr/bin/env python3
"""Слова, по которым GitHub закрывает задачу, — только строкой `Closes #N` в теле PR.

    python3 tools/closing_words.py --hook           PreToolUse Bash: JSON вызова на stdin
    python3 tools/closing_words.py --pr PR.json     CI: заголовок, описание и коммиты PR
                                                    (`gh pr view N --json title,body,commits`)
    python3 tools/closing_words.py --selftest

GitHub закрывает задачу, когда PR или коммит, попавший в основную ветку, содержит close, fix
или resolve в любой форме рядом со ссылкой на неё. Задачу закрывает мерж PR со строкой
`Closes #N` в теле (tools/pr_link.py — там же отказ другим словам в теле); в коммите и в
заголовке PR такие слова — отказ: при squash заголовок PR становится заголовком коммита в main,
а ссылка в коммите — «Implements <префикс>#12».
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from commit_check import WITHOUT_TASK
from pr_link import RE_GITHUB_CLOSE
from pr_link import problem as body_problem
from script_common import print_problems, report, run_script

# Команды, текст которых уходит в историю или в PR: коммит, создание, правка и слияние PR —
# в начале команды или после ; && | и перевода строки, а не упоминание в тексте правки.
START = r"(?:^|[;&|\n])\s*"
# Тело и заголовок `gh pr create` смотрит сторож по tools/pr_link.py — здесь только коммит.
WATCHED = re.compile(rf"{START}git(?:\s+-\S+(?:\s+[^\s-]\S*)?)*\s+commit\b")
# PR обновления зависимостей (Dependabot, `build(deps): …`) — без задачи, как и его коммиты
# (tools/commit_check.py): строки `Closes`/`Part of` в теле от него не ждём.
DEPS = tuple(prefix for prefix in WITHOUT_TASK if prefix.startswith("build(deps"))
ADVICE = "в коммите и заголовке PR — «Implements <префикс>#12»; задачу закрывает строка " \
         "`Closes #N` в теле PR"


def found(text: str) -> list[str]:
    return [m.group(0) for m in RE_GITHUB_CLOSE.finditer(text)]


def closing_hits(command: str) -> list[str]:
    """Закрывающие слова в коммите, который делает команда (сторож .agents/guards/)."""
    # Смотрим с первого коммита до конца: сообщение идёт после команды, а текст до неё (правка
    # файла, заметки) в историю не попадает.
    start = WATCHED.search(command)
    return found(command[start.start():]) if start else []


def hook(call: dict) -> int:
    """Код 2 — Claude Code остановит команду и покажет причину."""
    hits = closing_hits((call.get("tool_input") or {}).get("command") or "")
    if hits:
        print(f"Остановлено: {', '.join(hits)} — GitHub закроет задачу при слиянии; {ADVICE}.",
              file=sys.stderr)
    return 2 if hits else 0


def pr_problems(pr: dict) -> list[str]:
    """Заголовок и коммиты — без слов закрытия; тело — по tools/pr_link.py."""
    texts = {"заголовок": pr.get("title") or ""}
    for c in pr.get("commits") or []:
        texts[f"коммит {c.get('oid', '')[:7]}"] = f"{c.get('messageHeadline', '')}\n" \
                                                  f"{c.get('messageBody', '')}"
    out = [f"{where}: {hit} — {ADVICE}" for where, text in texts.items() for hit in found(text)]
    if texts["заголовок"].startswith(DEPS):
        return out
    body = body_problem(pr.get("body") or "")
    return out + ([f"описание: {body}"] if body else [])


CAUGHT = ["Closes #12", "fixes gf#7", "Resolved: owner/repo#3", "FIX #1",
          "close https://github.com/a/b/issues/5", "Задача сделана.\n\nCloses gf#96",
          "fixes https://github.com/a/b/issues/5"]
SPARED = ["Implements gf#12", "Задача gf#12", "fix: пустая строка источника",
          "fix(parser): разбор\n\nImplements gf#12", "fix the bug in #12", "closed issue",
          "fixture #1", "prefix #3",
          "Merge pull request #80 from NikonFlex/76-x"]


def selftest() -> int:
    bad = [f"не поймано: {t!r}" for t in CAUGHT if not found(t)]
    bad += [f"ложное срабатывание: {t!r}" for t in SPARED if found(t)]
    bad += [f"хук: {cmd!r} → {code}, ждали {want}" for cmd, want in [
        ('git commit -m "gf#96: итог\n\nCloses #96"', 2),
        ("gh pr create --title t --body 'Closes #96'", 0),
        ('git commit -m "gf#96: итог\n\nЗадача gf#96"', 0),
        ("echo 'Closes #96'", 0),
        ("python3 - <<'EOF'\nтекст: хук перед `git commit`; было `Closes #12`\nEOF", 0),
        ("cd repo && git -C x commit -m 'Fixes #3'", 2),
        ("python3 - <<'EOF'\nзаметка: было «Closes #12»\nEOF\ngit commit -q -m 'Задача gf#96'", 0),
    ] if (code := hook({"tool_input": {"command": cmd}})) != want]
    # тело PR и заголовок с коммитами понимают слово закрытия и ссылку одинаково (одно объявление)
    bad += [f"тело PR и заголовок разошлись в {t!r}" for t in [*CAUGHT, *SPARED]
            if (body_problem(f"Closes #1\nтекст {t}") is None) == bool(found(t))]
    pr = {"title": "gf#96: порядок", "body": "Closes #96",
          "commits": [{"oid": "abc1234def", "messageHeadline": "x", "messageBody": "closes #96"}]}
    if [p[:15] for p in pr_problems(pr)] != ["коммит abc1234:"]:
        bad.append(f"PR: {pr_problems(pr)}")
    if [p[:9] for p in pr_problems({"title": "Fix #3 x", "body": "Fixes #3"})] != \
            ["заголовок", "описание:"]:
        bad.append("PR: слово закрытия в заголовке и тело без строки Closes")
    deps = {"title": "build(deps): Bump actions/upload-artifact from 4 to 7", "body": "Bumps x."}
    if pr_problems(deps) or not pr_problems({**deps, "title": "build: Bump x"}):
        bad.append("PR обновления зависимостей: тело без ссылки — годится, без префикса — нет")
    return report(bad, f"поймано {len(CAUGHT)}, ложных нет из {len(SPARED)}, хук и PR — верно")


def run(root: Path) -> int:
    if sys.argv[1:] == ["--hook"]:
        return hook(json.load(sys.stdin))
    if sys.argv[1:2] == ["--pr"] and len(sys.argv) == 3:
        pr = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        return print_problems(pr_problems(pr), "слов, закрывающих задачу, нет")
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(run_script(selftest, run))

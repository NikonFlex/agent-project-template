#!/usr/bin/env python3
"""Слова, по которым GitHub закрывает задачу, — запрещены в коммитах и PR.

    python3 tools/closing_words.py --hook           PreToolUse Bash: JSON вызова на stdin
    python3 tools/closing_words.py --pr PR.json     CI: заголовок, описание и коммиты PR
                                                    (`gh pr view N --json title,body,commits`)
    python3 tools/closing_words.py --selftest

GitHub закрывает задачу, когда PR или коммит, попавший в основную ветку, содержит close, fix
или resolve в любой форме рядом со ссылкой на неё. Задачу закрывает только хозяин своим
словом (CLAUDE.md, «Задачи»), поэтому ссылка пишется «Задача <префикс>#12», а не «Closes #12».
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from script_common import print_problems, report, run_script

# Ссылка на задачу: #12, gf#12, owner/repo#12 или адрес задачи на GitHub.
REF = r"(?:[\w.-]+/[\w.-]+)?[\w-]*#\d+|https?://github\.com/[^\s/]+/[^\s/]+/issues/\d+"
KEYWORD = re.compile(rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+(?:{REF})", re.I)
# Команды, текст которых уходит в историю или в PR: коммит, создание, правка и слияние PR —
# в начале команды или после ; && | и перевода строки, а не упоминание в тексте правки.
START = r"(?:^|[;&|\n])\s*"
WATCHED = re.compile(rf"{START}git(?:\s+-\S+(?:\s+[^\s-]\S*)?)*\s+commit\b"
                     rf"|{START}gh\s+pr\s+(?:create|edit|merge)\b")
ADVICE = "задачу закрывает только хозяин; ссылка — «Задача <префикс>#12»"


def found(text: str) -> list[str]:
    return [m.group(0) for m in KEYWORD.finditer(text)]


def hook(call: dict) -> int:
    """Код 2 — Claude Code остановит команду и покажет причину."""
    command = (call.get("tool_input") or {}).get("command") or ""
    # Смотрим с первого коммита или PR до конца: сообщение и описание идут после команды, а
    # текст до неё (правка файла, заметки) в историю не попадает.
    start = WATCHED.search(command)
    hits = found(command[start.start():]) if start else []
    if hits:
        print(f"Остановлено: {', '.join(hits)} — GitHub закроет задачу при слиянии; {ADVICE}.",
              file=sys.stderr)
    return 2 if hits else 0


def pr_problems(pr: dict) -> list[str]:
    texts = {"заголовок": pr.get("title") or "", "описание": pr.get("body") or ""}
    for c in pr.get("commits") or []:
        texts[f"коммит {c.get('oid', '')[:7]}"] = f"{c.get('messageHeadline', '')}\n" \
                                                  f"{c.get('messageBody', '')}"
    return [f"{where}: {hit} — {ADVICE}" for where, text in texts.items() for hit in found(text)]


CAUGHT = ["Closes #12", "fixes gf#7", "Resolved: owner/repo#3", "FIX #1",
          "close https://github.com/a/b/issues/5", "Задача сделана.\n\nCloses gf#96"]
SPARED = ["Задача gf#12", "fix the bug in #12", "closed issue", "fixture #1", "prefix #3",
          "Merge pull request #80 from NikonFlex/76-x"]


def selftest() -> int:
    bad = [f"не поймано: {t!r}" for t in CAUGHT if not found(t)]
    bad += [f"ложное срабатывание: {t!r}" for t in SPARED if found(t)]
    bad += [f"хук: {cmd!r} → {code}, ждали {want}" for cmd, want in [
        ('git commit -m "gf#96: итог\n\nCloses #96"', 2),
        ("gh pr create --title t --body 'Fixes #96'", 2),
        ('git commit -m "gf#96: итог\n\nЗадача gf#96"', 0),
        ("echo 'Closes #96'", 0),
        ("python3 - <<'EOF'\nтекст: хук перед `git commit`; было `Closes #12`\nEOF", 0),
        ("cd repo && git -C x commit -m 'Fixes #3'", 2),
        ("python3 - <<'EOF'\nзаметка: было «Closes #12»\nEOF\ngit commit -q -m 'Задача gf#96'", 0),
    ] if (code := hook({"tool_input": {"command": cmd}})) != want]
    pr = {"title": "gf#96: порядок", "body": "Задача gf#96",
          "commits": [{"oid": "abc1234def", "messageHeadline": "x", "messageBody": "closes #96"}]}
    if [p[:15] for p in pr_problems(pr)] != ["коммит abc1234:"]:
        bad.append(f"PR: {pr_problems(pr)}")
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

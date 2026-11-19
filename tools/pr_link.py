#!/usr/bin/env python3
"""Ссылка PR на задачу — одно объявление для CI, сторожа и прогона после мержа.

    python3 tools/pr_link.py check < body    0 — годится; 1 — отказ, причина в stderr
    python3 tools/pr_link.py closes owner/repo < body    задачи из `Closes`, по строке
    python3 tools/pr_link.py --selftest

В теле PR отдельной строкой `Closes #N` (PR доделывает задачу — слияние её закроет) или
`Part of #N` (промежуточный или ждёт проверки хозяином — задача остаётся открытой). Слияние
токеном CI GitHub задачи не закрывает — job `automerge` закрывает их по `closes`; `#N` без
репозитория — задача этого репозитория, с ним — `owner/repo#N`. Другие слова, по которым GitHub
закрывает задачу (Fixes, Resolves, Close …), и PR без строки — отказ: иначе задача закроется
не тем PR или не закроется вовсе.
"""

from __future__ import annotations

import re
import sys

LINK_WORDS = ("Closes", "Part of")
_REF = r"(?P<repo>[\w.-]+/[\w.-]+)?#(?P<num>\d+)"
RE_LINK = re.compile(rf"^(?P<word>{'|'.join(LINK_WORDS)}) {_REF}[ \t]*$", re.M)
# Ссылка на задачу в любой форме, по которой GitHub её закроет: #12, ab#12, owner/repo#12 или
# адрес задачи. Слово закрытия и такая ссылка — единственное объявление: tools/closing_words.py
# и сторож берут его отсюда, чтобы тело PR и заголовок с коммитами не расходились в формах.
ISSUE_REF = r"(?:[\w.-]+/[\w.-]+)?[\w-]*#\d+|https?://github\.com/[^\s/]+/[^\s/]+/issues/\d+"
# Слова, по которым GitHub закрывает задачу сам.
RE_GITHUB_CLOSE = re.compile(rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+(?:{ISSUE_REF})",
                             re.I)


def problem(body: str) -> str | None:
    """Чем тело PR не годится, или None."""
    if not RE_LINK.search(body):
        return "в теле PR нет строки `Closes #N` или `Part of #N`"
    m = RE_GITHUB_CLOSE.search(RE_LINK.sub("", body))
    if m:
        return (f"«{m.group(0)}»: по этому слову GitHub закроет задачу; ссылка — только "
                "отдельной строкой `Closes #N` или `Part of #N`")
    return None


def closes(body: str, default_repo: str) -> list[str]:
    """Задачи, которые PR закрывает: `owner/repo#N`."""
    return [f"{m['repo'] or default_repo}#{m['num']}"
            for m in RE_LINK.finditer(body) if m["word"] == "Closes"]


def linked(body: str) -> list[int]:
    """Номера задач этого репозитория из строк `Closes` и `Part of` — для подсчёта PR на задачу."""
    return [int(m["num"]) for m in RE_LINK.finditer(body) if m["repo"] is None]


CASES = [  # тело, годится ли, задачи из Closes
    ("Closes acme/app#12", True, ["acme/app#12"]),
    ("Part of #7", True, []),
    ("Текст\n\nCloses #3\nPart of acme/app#4\n", True, ["acme/app#3"]),
    ("Fixes #12", False, []),
    ("Closes #1\nfixes #2", False, ["acme/app#1"]),
    ("closes #5", False, []),
    ("Closes: #5", False, []),
    ("Closes #3\nfixes https://github.com/a/b/issues/5", False, ["acme/app#3"]),
    ("Closes #3\nResolves ab#7", False, ["acme/app#3"]),
    ("Closes #3\nСм. https://github.com/a/b/issues/5", True, ["acme/app#3"]),
    ("ссылки нет", False, []),
    ("", False, []),
]


def selftest() -> int:
    bad = 0
    for body, ok, want in CASES:
        got_ok = problem(body) is None
        got = closes(body, "acme/app")
        if got_ok != ok or got != want:
            print(f"✗ {body!r}: годится={got_ok}, Closes={got}; ждали {ok}, {want}")
            bad += 1
    if linked("Part of #7\nCloses #3\nCloses acme/app#4") != [7, 3]:
        print("✗ linked: номера задач этого репозитория из Closes и Part of")
        bad += 1
    print(f"самопроверка пройдена: {len(CASES)} тел PR и номера задач" if not bad
          else f"{len(CASES) - bad}/{len(CASES)}")
    return 1 if bad else 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "--selftest":
        sys.exit(selftest())
    if cmd == "check":
        p = problem(sys.stdin.read())
        if p:
            print(p, file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    if cmd == "closes" and len(sys.argv) == 3:
        for ref in closes(sys.stdin.read(), sys.argv[2]):
            print(ref)
        sys.exit(0)
    print(__doc__)
    sys.exit(2)

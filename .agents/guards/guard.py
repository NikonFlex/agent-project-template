"""Сторожа: команду агента проверяют до выполнения — одна логика на Claude Code, Codex, Cursor.

    python3 .agents/guards/guard.py claude    PreToolUse (.claude/settings.json)
    python3 .agents/guards/guard.py codex     PreToolUse (.codex/hooks.json)
    python3 .agents/guards/guard.py cursor    beforeShellExecution, beforeReadFile (.cursor/)
    python3 .agents/guards/guard.py --write-codex-rules   пересобрать .codex/rules/default.rules
    python3 .agents/guards/guard.py --selftest

Решение — разрешить, спросить человека или отказать с причиной. Вход среды переводит его в
её формат. «Спросить» Codex в PreToolUse не поддерживает (такой ответ считается сбоем хука и
команда идёт без вопроса — openai/codex, hooks/src/events/pre_tool_use.rs, тест
unsupported_permission_decision_fails_open): для Codex «спросить» — правила execpolicy с
decision = "prompt", собранные из того же списка ASK (.codex/rules/default.rules).
Настройки, которые меняет проект, — .agents/guards/config.json.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from closing_words import closing_hits  # noqa: E402 — tools/ добавлен в путь строкой выше
from project_config import backlog_config, ticket_number  # noqa: E402
from task_links import has_local_repeat  # noqa: E402

CONFIG = Path(".agents/guards/config.json")
CODEX_RULES = Path(".codex/rules/default.rules")
START = r"(?:^|[;&|\n(]|&&|\|\|)\s*"


class Verdict(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class Call:
    """Что агент собирается сделать: команда оболочки или чтение файла."""

    command: str = ""
    path: str = ""


#: Запись в GitHub и push — только по «да» человека. Префиксы команд (как в execpolicy Codex):
#: элемент-список — варианты. Одно объявление на сторож и на правила Codex.
ASK = [
    ["git", "push"],
    ["gh", "issue", ["create", "edit", "close", "reopen", "delete", "comment", "transfer"]],
    ["gh", "pr", ["create", "edit", "close", "reopen", "merge", "comment", "review", "ready"]],
    ["gh", "project"],
    ["gh", "release", ["create", "edit", "delete", "upload"]],
    ["gh", "api"],
]


def config(root: Path) -> dict:
    path = root / CONFIG
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def commands(command: str) -> list[list[str]]:
    """Простые команды строки: `a && b; c | d` → [[a…], [b…], [c…], [d…]]."""
    out = []
    for part in re.split(r"&&|\|\||[;|\n]", command):
        try:
            words = shlex.split(part, comments=True)
        except ValueError:
            words = part.split()
        while words and re.match(r"^\w+=", words[0]):  # VAR=1 git push
            words = words[1:]
        if words:
            out.append(words)
    return out


def matches(words: list[str], prefix: list) -> bool:
    if len(words) < len(prefix):
        return False
    pairs = zip(words, prefix, strict=False)
    return all(w == p or (isinstance(p, list) and w in p) for w, p in pairs)


# --------------------------------------------------------------------------- сторожа


def github_write(call: Call, root: Path) -> tuple[Verdict, str]:
    hit = next((w for w in commands(call.command) if any(matches(w, p) for p in ASK)), None)
    return (Verdict.ASK, f"запись в GitHub: «{' '.join(hit)}» — только по «да» человека") \
        if hit else (Verdict.ALLOW, "")


def prod(call: Call, root: Path) -> tuple[Verdict, str]:
    hit = next((p for p in config(root).get("prod", []) if re.search(p, call.command)), None)
    return (Verdict.ASK, f"запись на прод-контур ({hit}) — только по «да» человека, с откатом") \
        if hit else (Verdict.ALLOW, "")


def secret_path(path: str, root: Path) -> bool:
    name = Path(path).name
    allowed = config(root).get("secret_allow", [".env.example"])
    return name not in allowed and any(re.search(p, name) for p in
                                       config(root).get("secret_files", [r"^\.env(\..+)?$"]))


def secrets(call: Call, root: Path) -> tuple[Verdict, str]:
    paths = [call.path] if call.path else [w for c in commands(call.command) for w in c[1:]]
    hit = next((p for p in paths if p and secret_path(p, root)), None)
    return (Verdict.DENY, f"чтение файла с ключами ({hit}) запрещено; имена переменных — "
                          ".env.example") if hit else (Verdict.ALLOW, "")


def closing(call: Call, root: Path) -> tuple[Verdict, str]:
    hits = closing_hits(call.command)
    return (Verdict.DENY, f"{', '.join(hits)} — GitHub закроет задачу при слиянии; задачу "
                          "закрывает человек, ссылка — «Implements <префикс>#N»") \
        if hits else (Verdict.ALLOW, "")


def ticket_task(root: Path) -> Path | None:
    """Файл задачи из `git ticket`."""
    number = ticket_number(root)
    if number is None:
        return None
    found = sorted(backlog_config(root)["tasks"].glob(f"*-{number:04d}.md"))
    return found[0] if found else None


def local_repeat(call: Call, root: Path) -> tuple[Verdict, str]:
    if not any(matches(w, ["gh", "pr", "create"]) for w in commands(call.command)):
        return Verdict.ALLOW, ""
    task = ticket_task(root)
    if task is None:
        return Verdict.DENY, "PR без задачи: `git ticket <номер>` и файл задачи в docs/tasks/"
    if not has_local_repeat(task):
        return Verdict.DENY, (f"в {task.name} нет строки «Локальный повтор» — записать до PR "
                              "(docs/agents/tasks.md)")
    return Verdict.ALLOW, ""


def receipt(call: Call, root: Path) -> tuple[Verdict, str]:
    """Push, где гейт включён (tools/receipt.json, «gated»), — только с квитанцией прогона
    live или record с исходом «прошло» на том же дереве кода."""
    if not any(matches(w, ["git", "push"]) for w in commands(call.command)):
        return Verdict.ALLOW, ""
    gate = json.loads((root / "tools/receipt.json").read_text(encoding="utf-8"))
    if not gate.get("gated"):
        return Verdict.ALLOW, ""
    from scenario import state_dir, tree_hash
    tree = tree_hash(root)
    log = state_dir() / "receipts.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    ok = any(r["tree"] == tree and r["outcome"] == "прошло" and r["mode"] in ("live", "record")
             for r in map(json.loads, lines))
    return (Verdict.ALLOW, "") if ok else (
        Verdict.DENY, f"нет квитанции live/record «прошло» на дереве {tree}: "
                      "python3 tools/scenario.py run --mode live")


#: Порядок не важен: итог — самый строгий ответ (отказ > вопрос > разрешение).
GUARDS: list[Callable[[Call, Path], tuple[Verdict, str]]] = [
    github_write, prod, secrets, closing, local_repeat, receipt]
RANK = {Verdict.ALLOW: 0, Verdict.ASK: 1, Verdict.DENY: 2}


def decide(call: Call, root: Path = ROOT) -> tuple[Verdict, str]:
    answers = [guard(call, root) for guard in GUARDS]
    worst = max((a[0] for a in answers), key=RANK.__getitem__)
    return worst, "; ".join(reason for verdict, reason in answers if verdict == worst and reason)


# --------------------------------------------------------------------------- входы сред


def from_claude(event: dict) -> Call:
    tool_input = event.get("tool_input") or {}
    return Call(command=tool_input.get("command", ""), path=tool_input.get("file_path", ""))


def to_claude(verdict: Verdict, reason: str) -> int:
    """Отказ — код 2 и причина в stderr; вопрос — permissionDecision ask.

    Формат — code.claude.com/docs/en/hooks, PreToolUse."""
    if verdict == Verdict.DENY:
        print(f"Сторож: {reason}", file=sys.stderr)
        return 2
    if verdict == Verdict.ASK:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "ask",
                                                 "permissionDecisionReason": reason}},
                         ensure_ascii=False))
    return 0


def to_codex(verdict: Verdict, reason: str) -> int:
    """Отказ — код 2 и причина в stderr (как у Claude Code). Вопрос задаёт execpolicy."""
    if verdict == Verdict.DENY:
        print(f"Сторож: {reason}", file=sys.stderr)
        return 2
    return 0


def from_cursor(event: dict) -> Call:
    return Call(command=event.get("command", ""),
                path=event.get("file_path", "") or event.get("path", ""))


def to_cursor(verdict: Verdict, reason: str) -> int:
    """`{"permission": allow|ask|deny, "userMessage", "agentMessage"}` на stdout [не проверено]."""
    print(json.dumps({"permission": verdict.value, "userMessage": reason, "agentMessage": reason},
                     ensure_ascii=False))
    return 0


ENVS = {"claude": (from_claude, to_claude), "codex": (from_claude, to_codex),
        "cursor": (from_cursor, to_cursor)}


def codex_rules() -> str:
    lines = ["# Собрано из ASK в .agents/guards/guard.py — правится там:",
             "#   python3 .agents/guards/guard.py --write-codex-rules", ""]
    for prefix in ASK:
        lines.append(f"prefix_rule(\n    pattern = {json.dumps(prefix, ensure_ascii=False)},\n"
                     '    decision = "prompt",\n'
                     '    justification = "запись в GitHub или push — только по «да» человека",\n)')
    return "\n".join(lines) + "\n"


def main() -> int:
    if "--selftest" in sys.argv:
        from guard_selftest import selftest
        return selftest()
    if "--write-codex-rules" in sys.argv:
        (ROOT / CODEX_RULES).parent.mkdir(parents=True, exist_ok=True)
        (ROOT / CODEX_RULES).write_text(codex_rules(), encoding="utf-8")
        return 0
    source, target = ENVS[sys.argv[1]]
    return target(*decide(source(json.load(sys.stdin))))


if __name__ == "__main__":
    sys.exit(main())

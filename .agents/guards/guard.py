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
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from closing_words import closing_hits, found  # noqa: E402 — tools/ добавлен в путь строкой выше
from pr_link import problem as body_problem  # noqa: E402
from project_config import backlog_config, ticket_number  # noqa: E402
from script_common import git_env  # noqa: E402
from task_links import has_clean_contour, has_local_repeat  # noqa: E402

CONFIG = Path(".agents/guards/config.json")
CODEX_RULES = Path(".codex/rules/default.rules")
START = r"(?:^|[;&|\n(]|&&|\|\|)\s*"
PR_CREATE = ["gh", "pr", "create"]
PUSH = ["git", "push"]
OPERATORS = ";&|\n"  # разделители простых команд: ; && || | и перевод строки


class Verdict(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class Call:
    """Что агент собирается сделать: команда оболочки или чтение файла."""

    command: str = ""
    path: str = ""
    cwd: str = ""  # папка сессии из события среды; пусто — основной клон


#: Запись в GitHub и push — только по «да» человека. Префиксы команд (как в execpolicy Codex):
#: элемент-список — варианты. Одно объявление на сторож и на правила Codex.
ASK = [
    ["git", "push"],
    ["gh", "issue", ["create", "edit", "close", "reopen", "delete", "comment", "transfer"]],
    ["gh", "pr", ["create", "edit", "close", "reopen", "merge", "comment", "review", "ready"]],
    ["gh", "project"],
    ["gh", "release", ["create", "edit", "delete", "upload"]],
    ["gh", "api"],
    # видимость репо и доступ людей
    ["gh", "repo", ["edit", "delete", "rename", "archive", "unarchive"]],
]


def config(root: Path) -> dict:
    path = root / CONFIG
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def commands(command: str) -> list[list[str]]:
    """Простые команды строки: `a && b; c | d` → [[a…], [b…], [c…], [d…]]. Кавычки строка
    держит целиком (тело PR бывает многострочным); не разобрать — по разделителям грубо.
    `git -C <путь> push` — тоже `git push`."""
    return [words for _, words in placed(Call(command=command), Path())]


def placed(call: Call, root: Path) -> list[tuple[Path, list[str]]]:
    """Простые команды с папкой, где каждая выполнится. Сессия агента стоит в основном
    клоне, а работа — в дереве задачи (`cd <дерево> && git push`): ticket, файл задачи и дифф
    берутся оттуда. `cd` меняет папку следующих команд, `git -C` — своей; папки нет — `cd`
    не сработает, остаётся прежняя."""
    here, out = Path(call.cwd) if call.cwd else root, []
    for words in lexed(call.command) or _rough(call.command):
        if words[0] == "cd" and len(words) == 2:
            target = here / Path(words[1]).expanduser()
            here = target if target.is_dir() else here
        elif words[:2] == ["git", "-C"] and len(words) > 3:
            out.append((here / Path(words[2]).expanduser(), ["git", *words[3:]]))
        else:
            out.append((here, words))
    return out


def where(call: Call, root: Path, prefix: list) -> Path:
    """Корень клона, где выполнится первая команда с префиксом; такой нет — root."""
    here = next((d for d, words in placed(call, root) if matches(words, prefix)), root)
    return next((d for d in (here, *here.parents) if (d / ".git").exists()), here)


def lexed(command: str) -> list[list[str]] | None:
    """Простые команды по shlex всей строки; `;` вплотную к слову (`create;ls`) — тоже
    разделитель. Незакрытая кавычка — None."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=OPERATORS)
    lexer.whitespace, lexer.whitespace_split = " \t\r", True
    try:
        words = list(lexer)
    except ValueError:
        return None
    out, current = [], []
    for word in [*words, ";"]:
        if word and set(word) <= set(OPERATORS):
            out.append(current)
            current = []
        else:
            current.append(word)
    return [w for w in map(_without_env, out) if w]


def _rough(command: str) -> list[list[str]]:
    parts = (part.split() for part in re.split(r"&&|\|\||[;|\n]", command))
    return [w for w in map(_without_env, parts) if w]


def _without_env(words: list[str]) -> list[str]:
    while words and re.match(r"^\w+=", words[0]):  # VAR=1 git push
        words = words[1:]
    return words


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
    return (Verdict.DENY, f"{', '.join(hits)} в коммите — GitHub закроет задачу при попадании в "
                          "main; в коммите — «Implements <префикс>#N», задачу закрывает строка "
                          "`Closes #N` в теле PR") if hits else (Verdict.ALLOW, "")


def creates_pr(command: str) -> bool:
    """Есть ли в строке `gh pr create` — одна проверка для всех сторожей PR."""
    return any(matches(w, PR_CREATE) for w in commands(command))


def pushes(command: str) -> bool:
    """Есть ли в строке `git push` — одна проверка для сторожей push."""
    return any(matches(w, PUSH) for w in commands(command))


def _flag(args: list[str], names: tuple[str, ...]) -> str | None:
    for j, w in enumerate(args):
        if w in names:
            return args[j + 1] if j + 1 < len(args) else ""
        for name in names:
            if name.startswith("--") and w.startswith(name + "="):
                return w.split("=", 1)[1]
    return None


def _body_of(args: list[str], root: Path) -> tuple[str | None, str | None]:
    """Тело PR из `--body` или файла `--body-file`; (тело, причина отказа). `--body-file -`
    (stdin) прочитать нельзя — тело None без отказа: поймает CI (job pr-link)."""
    file = _flag(args, ("--body-file", "-F"))
    if file is None:
        return _flag(args, ("--body", "-b")), None
    if file == "-":
        return None, None
    path = Path(file) if Path(file).is_absolute() else root / file
    if not path.is_file():
        return None, f"файл тела PR не найден: {file}"
    return path.read_text(encoding="utf-8"), None


def pr_link(call: Call, root: Path) -> tuple[Verdict, str]:
    """Тело `gh pr create` — со строкой `Closes #N` или `Part of #N` и без других слов закрытия,
    заголовок — без слов закрытия (tools/pr_link.py)."""
    # только сама команда, а не те же слова в тексте коммита или заметки
    if not creates_pr(call.command):
        return Verdict.ALLOW, ""
    parsed = lexed(call.command)
    if parsed is None:
        return Verdict.DENY, "не разобрал `gh pr create`; тело PR — через --body-file <файл>"
    for args in (w[len(PR_CREATE):] for w in parsed if matches(w, PR_CREATE)):
        if "--fill" in args:
            return Verdict.DENY, "тело PR — явно, через --body-file: в нём Closes или Part of"
        body, refusal = _body_of(args, where(call, root, PR_CREATE))
        if refusal:
            return Verdict.DENY, refusal
        if body is None and _flag(args, ("--body-file", "-F")) == "-":
            continue  # stdin не прочитать; CI поймает
        if body is None:
            return Verdict.DENY, "тело PR — через --body или --body-file: в нём строка Closes " \
                                 "или Part of"
        title_hits = found(_flag(args, ("--title", "-t")) or "")
        reason = body_problem(body) or (f"«{title_hits[0]}» в заголовке PR — GitHub закроет "
                                        "задачу при слиянии" if title_hits else None)
        if reason:
            return Verdict.DENY, reason
    return Verdict.ALLOW, ""


def ticket_task(root: Path) -> Path | None:
    """Файл задачи из `git ticket`."""
    number = ticket_number(root)
    if number is None:
        return None
    found = sorted(backlog_config(root)["tasks"].glob(f"*-{number:04d}.md"))
    return found[0] if found else None


def local_repeat(call: Call, root: Path) -> tuple[Verdict, str]:
    if not creates_pr(call.command):
        return Verdict.ALLOW, ""
    task = ticket_task(where(call, root, PR_CREATE))
    if task is None:
        return Verdict.DENY, "PR без задачи: `git ticket <номер>` и файл задачи в docs/tasks/"
    if not has_local_repeat(task):
        return Verdict.DENY, (f"в {task.name} нет строки «Локальный повтор» — записать до PR "
                              "(docs/agents/tasks.md)")
    return Verdict.ALLOW, ""


#: Правка вне кода: текст, спеки и задачи. Остальное — код (чистый контур смотрит его).
NOT_CODE = re.compile(r"\.md$|^(?:docs|openspec)/")


def changed_files(root: Path) -> list[str] | None:
    """Файлы ветки относительно `origin/main` (от общего предка); None — сравнить не вышло."""
    run = subprocess.run(  # noqa: S603 — своя команда git, без оболочки
        ["git", "-C", str(root), "-c", "core.quotePath=false", "diff", "--name-only",  # noqa: S607
         "origin/main...HEAD"], capture_output=True, text=True, check=False, env=git_env())
    return run.stdout.splitlines() if run.returncode == 0 else None


def clean_contour(call: Call, root: Path) -> tuple[Verdict, str]:
    """`git push` ветки с изменённым кодом — только со строкой «Чистый контур:» в низу задачи:
    проверка по своему диффу, навык clean-contour-check (docs/agents/code.md)."""
    if not pushes(call.command):
        return Verdict.ALLOW, ""
    root = where(call, root, PUSH)
    files = changed_files(root)
    if files is None:
        return Verdict.DENY, "не удалось сравнить ветку с origin/main — `git fetch origin main`"
    if all(NOT_CODE.search(f) for f in files):
        return Verdict.ALLOW, ""
    task = ticket_task(root)
    if task is None:
        return Verdict.DENY, "push без задачи: `git ticket <номер>` и файл задачи в docs/tasks/"
    if not has_clean_contour(task):
        return Verdict.DENY, (
            "проверка чистого контура по своему диффу — навык clean-contour-check, "
            f"строка «Чистый контур: …» в низу {task.name} (docs/agents/code.md)")
    return Verdict.ALLOW, ""


def receipt(call: Call, root: Path) -> tuple[Verdict, str]:
    """Push, где гейт включён (tools/receipt.json, «gated»), — только с квитанцией прогона
    live или record с исходом «прошло» на том же дереве кода."""
    if not pushes(call.command):
        return Verdict.ALLOW, ""
    root = where(call, root, PUSH)
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
    github_write, prod, secrets, closing, pr_link, local_repeat, clean_contour, receipt]
RANK = {Verdict.ALLOW: 0, Verdict.ASK: 1, Verdict.DENY: 2}


def decide(call: Call, root: Path = ROOT) -> tuple[Verdict, str]:
    answers = [guard(call, root) for guard in GUARDS]
    worst = max((a[0] for a in answers), key=RANK.__getitem__)
    return worst, "; ".join(reason for verdict, reason in answers if verdict == worst and reason)


# --------------------------------------------------------------------------- входы сред


def from_claude(event: dict) -> Call:
    tool_input = event.get("tool_input") or {}
    return Call(command=tool_input.get("command", ""), path=tool_input.get("file_path", ""),
                cwd=event.get("cwd", ""))


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
                path=event.get("file_path", "") or event.get("path", ""), cwd=event.get("cwd", ""))


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

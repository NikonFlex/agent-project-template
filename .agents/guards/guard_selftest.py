"""Самопроверка сторожей (`python3 .agents/guards/guard.py --selftest`): без сети.

Каждый сторож отказывает или спрашивает на своём запрещённом входе и молчит на соседнем
безобидном; входы сред дают свой формат; правила Codex совпадают со списком ASK.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from guard import (
    CODEX_RULES,
    ROOT,
    Call,
    Verdict,
    codex_rules,
    decide,
    to_claude,
    to_codex,
    to_cursor,
)
from script_common import git_env, report, write_files

#: (вызов, ожидаемый ответ, кусок причины) — по запрещённому и безобидному входу на сторож.
CASES = [
    (Call(command="git push origin main"), Verdict.ASK, "запись в GitHub"),
    (Call(command="cd repo && gh pr merge 12"), Verdict.ASK, "gh pr merge"),
    (Call(command="gh issue view 12"), Verdict.ALLOW, ""),
    (Call(command="git log --oneline"), Verdict.ALLOW, ""),
    (Call(command="ssh deploy@prod-1 systemctl restart app"), Verdict.ASK, "прод"),
    (Call(command="cat .env"), Verdict.DENY, "ключами"),
    (Call(path="/repo/.env.local"), Verdict.DENY, "ключами"),
    (Call(command="cat .env.example"), Verdict.ALLOW, ""),
    (Call(command="git commit -m 'Closes #12'"), Verdict.DENY, "Closes #12"),
    (Call(command="git commit -m 'Implements ab#12'"), Verdict.ALLOW, ""),
]


def project(root: Path, repeat: bool, gated: bool) -> Path:
    bottom = "Локальный повтор: не требуется — настройка\n" if repeat else ""
    files = {
        ".backlog.json": '{"prefix": "ab"}',
        ".agents/guards/config.json": json.dumps({"prod": [r"\bssh\s+\S*prod"]}),
        "tools/receipt.json": json.dumps({"exclude": [], "gated": gated}),
        "docs/tasks/ab-0012.md": f"# ab#12\n\n<!-- ↓↓↓ ниже наше: скрипт эту часть не трогает "
                                 f"↓↓↓ -->\n\n{bottom}",
        "app.py": "x = 1\n",
    }
    write_files(root, files)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=git_env())  # noqa: S607
    (root / ".git/ticket").write_text("ab#12\n")
    return root


def cases(root: Path) -> list[str]:
    bad = []
    for call, wanted, reason in CASES:
        verdict, why = decide(call, root)
        if verdict != wanted or reason not in why:
            bad.append(f"{call}: {verdict} «{why}», ждали {wanted} «{reason}»")
    return bad


def pr_and_push(tmp: Path) -> list[str]:
    bad = []
    pr = Call(command="gh pr create --title x --body 'Implements ab#12'")
    if decide(pr, project(tmp / "no-repeat", repeat=False, gated=False))[0] != Verdict.DENY:
        bad.append("PR без «Локальный повтор» не остановлен")
    if decide(pr, project(tmp / "repeat", repeat=True, gated=False))[0] != Verdict.ASK:
        bad.append("PR с «Локальный повтор» не дошёл до вопроса человеку")
    push = Call(command="git push")
    root = project(tmp / "gated", repeat=True, gated=True)
    os.environ["STAND_STATE_DIR"] = str(tmp / "state")
    if decide(push, root)[0] != Verdict.DENY:
        bad.append("push без квитанции при включённом гейте не остановлен")
    from scenario import Mode, write_receipt
    write_receipt(root, Mode.REPLAY, passed=True)
    if decide(push, root)[0] != Verdict.DENY:
        bad.append("квитанция replay пропустила push — нужна live или record")
    write_receipt(root, Mode.LIVE, passed=True)
    if decide(push, root)[0] != Verdict.ASK:
        bad.append("квитанция live «прошло» не пропустила push к вопросу человеку")
    (root / "app.py").write_text("x = 2\n")
    if decide(push, root)[0] != Verdict.DENY:
        bad.append("правка кода после квитанции не сбросила её")
    return bad


def formats() -> list[str]:
    bad = []
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = to_claude(Verdict.DENY, "нельзя")
    if code != 2 or "нельзя" not in err.getvalue():
        bad.append("Claude: отказ — не код 2 с причиной в stderr")
    out = io.StringIO()
    with redirect_stdout(out):
        to_claude(Verdict.ASK, "спроси")
    if json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] != "ask":
        bad.append("Claude: вопрос — не permissionDecision ask")
    with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
        codes = (to_codex(Verdict.DENY, "x"), to_codex(Verdict.ASK, "x"))
    if codes != (2, 0) or out.getvalue():
        bad.append("Codex: отказ — код 2, вопрос — без вывода (его задаёт execpolicy)")
    out = io.StringIO()
    with redirect_stdout(out):
        to_cursor(Verdict.DENY, "нельзя")
    if json.loads(out.getvalue()) != {"permission": "deny", "userMessage": "нельзя",
                                      "agentMessage": "нельзя"}:
        bad.append("Cursor: формат ответа не тот")
    if (ROOT / CODEX_RULES).read_text(encoding="utf-8") != codex_rules():
        bad.append(f"{CODEX_RULES} не совпадает с ASK — guard.py --write-codex-rules")
    return bad


def selftest() -> int:
    state = os.environ.get("STAND_STATE_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        root = project(Path(tmp) / "base", repeat=True, gated=False)
        bad = cases(root) + pr_and_push(Path(tmp)) + formats()
    if state is None:
        os.environ.pop("STAND_STATE_DIR", None)
    else:
        os.environ["STAND_STATE_DIR"] = state
    return report(bad, f"{len(CASES)} входов сторожей, PR без «Локального повтора», квитанция "
                       "push, форматы Claude Code / Codex / Cursor, правила Codex")

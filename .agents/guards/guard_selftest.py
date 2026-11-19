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
from script_common import git_env, output, report, write_files

#: (вызов, ожидаемый ответ, кусок причины) — по запрещённому и безобидному входу на сторож.
CASES = [
    (Call(command="git push origin main"), Verdict.ASK, "запись в GitHub"),
    (Call(command="cd repo && gh pr merge 12"), Verdict.ASK, "gh pr merge"),
    (Call(command="gh issue view 12"), Verdict.ALLOW, ""),
    (Call(command="gh repo edit --visibility public"), Verdict.ASK, "gh repo edit"),
    (Call(command="gh repo view owner/repo"), Verdict.ALLOW, ""),
    (Call(command="git -C ../tree push"), Verdict.ASK, "git push"),
    # разделитель внутри кавычек — текст, а не вторая команда
    (Call(command="gh issue view 12 --comments 'a; git push origin x'"), Verdict.ALLOW, ""),
    (Call(command="git log --oneline"), Verdict.ALLOW, ""),
    (Call(command="ssh deploy@prod-1 systemctl restart app"), Verdict.ASK, "прод"),
    (Call(command="cat .env"), Verdict.DENY, "ключами"),
    (Call(path="/repo/.env.local"), Verdict.DENY, "ключами"),
    (Call(command="cat .env.example"), Verdict.ALLOW, ""),
    (Call(command="git commit -m 'Closes #12'"), Verdict.DENY, "Closes #12"),
    (Call(command="git commit -m 'Implements ab#12'"), Verdict.ALLOW, ""),
    # тело PR: Closes или Part of обязательны, другие слова закрытия — отказ (tools/pr_link.py)
    (Call(command="gh pr create --title t --body 'Fixes #12'"), Verdict.DENY, "Closes #N"),
    (Call(command="gh pr create --title t --body 'Closes #12\nfixes https://github.com/a/b/issues/5'"),
     Verdict.DENY, "по этому слову"),
    (Call(command="gh pr create --title 'Fix #12 x' --body 'Closes #12'"), Verdict.DENY,
     "заголовке"),
    (Call(command="gh pr create --fill"), Verdict.DENY, "--body-file"),
    (Call(command="gh pr create;ls"), Verdict.DENY, "--body"),
    (Call(command="gh pr create --title t --body-file нет.md"), Verdict.DENY, "не найден"),
    (Call(command="gh pr create --title t --body-file body-ok.md"), Verdict.ASK, "gh pr create"),
    (Call(command="gh pr create --title t --body-file body-bad.md"), Verdict.DENY, "Closes #N"),
    (Call(command="gh pr create --title t --body-file -"), Verdict.ASK, "gh pr create"),
    (Call(command="gh pr create --title t --body 'Part of #12'"), Verdict.ASK, "gh pr create"),
    (Call(command="git commit -q -F - <<'EOF'\nci: x\n\nСторож отбивает gh pr create\nEOF"),
     Verdict.ALLOW, ""),
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
        "body-ok.md": "Описание\n\nCloses #12\n",
        "body-bad.md": "Описание\n\nFixes #12\n",
    }
    write_files(root, files)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=git_env())  # noqa: S607
    commit(root, "основа")
    # origin/main — база, от которой сторож push считает изменённое
    output(root, "git", "update-ref", "refs/remotes/origin/main", "HEAD")
    (root / ".git/ticket").write_text("ab#12\n")
    return root


def commit(root: Path, message: str) -> None:
    """Всё рабочее дерево — одним коммитом (самопроверке нужна история, а не автор)."""
    output(root, "git", "add", "-A")
    output(root, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)


def cases(root: Path) -> list[str]:
    bad = []
    for call, wanted, reason in CASES:
        verdict, why = decide(call, root)
        if verdict != wanted or reason not in why:
            bad.append(f"{call}: {verdict} «{why}», ждали {wanted} «{reason}»")
    return bad


def pr_and_push(tmp: Path) -> list[str]:
    bad = []
    pr = Call(command="gh pr create --title x --body 'Closes #12'")
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


def clean_contour(tmp: Path) -> list[str]:
    """Push ветки: только текст — проходит; код без строки «Чистый контур:» — стоп; со строкой —
    проходит (до вопроса человеку); нет origin/main — стоп."""
    root = project(tmp / "contour", repeat=True, gated=False)
    push, bad = Call(command="git push"), []
    (root / "docs/note.md").write_text("текст\n")
    (root / "openspec").mkdir()
    (root / "openspec/spec.txt").write_text("спека\n")
    commit(root, "docs: текст")
    if decide(push, root)[0] != Verdict.ASK:
        bad.append("push одних docs/openspec/.md остановлен сторожем чистого контура")
    (root / "app.py").write_text("x = 2\n")
    commit(root, "feat: код")
    verdict, why = decide(push, root)
    if verdict != Verdict.DENY or "clean-contour-check" not in why:
        bad.append(f"push с кодом без «Чистый контур:» не остановлен: {verdict} «{why}»")
    task = root / "docs/tasks/ab-0012.md"
    task.write_text(task.read_text() + "Чистый контур: чисто — дифф проверен\n")
    if decide(push, root)[0] != Verdict.ASK:
        bad.append("строка «Чистый контур:» в низу задачи не пропустила push")
    task.write_text(task.read_text().replace("Чистый контур:", "Заметка о чистом контуре:")
                    .replace("<!-- ↓", "Чистый контур: выше метки\n<!-- ↓", 1))
    if decide(push, root)[0] != Verdict.DENY:
        bad.append("строка «Чистый контур:» выше метки засчитана")
    output(root, "git", "update-ref", "-d", "refs/remotes/origin/main")
    if decide(push, root)[0] != Verdict.DENY:
        bad.append("push без origin/main для сравнения пропущен")
    return bad


def command_folder(tmp: Path) -> list[str]:
    """Сессия — в клоне без ticket, работа — в дереве задачи: PR, push и тело PR смотрятся
    в папке команды (`cd`, `git -C`, папка сессии из события), а не в папке сессии."""
    session = project(tmp / "session", repeat=True, gated=False)
    (session / ".git/ticket").unlink()
    tree = project(tmp / "tree", repeat=True, gated=False)
    pr = "gh pr create --title t --body-file body-ok.md"
    wanted = [
        (Call(command=pr), Verdict.DENY, "PR без задачи"),
        (Call(command=f"cd {tree} && {pr}"), Verdict.ASK, "gh pr create"),
        (Call(command=pr, cwd=str(tree)), Verdict.ASK, "gh pr create"),
        (Call(command=f"cd {tmp}/нет && {pr}"), Verdict.DENY, "PR без задачи"),
    ]
    (tree / "app.py").write_text("x = 2\n")
    commit(tree, "feat: код")
    wanted += [(Call(command=f"cd {tree} && git push"), Verdict.DENY, "clean-contour-check"),
               (Call(command=f"git -C {tree} push"), Verdict.DENY, "clean-contour-check"),
               (Call(command="git push"), Verdict.ASK, "git push")]
    return [f"{call}: {verdict} «{why}», ждали {verdict_wanted} «{reason}»"
            for call, verdict_wanted, reason in wanted
            for verdict, why in [decide(call, session)]
            if verdict != verdict_wanted or reason not in why]


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
        bad = (cases(root) + pr_and_push(Path(tmp)) + clean_contour(Path(tmp))
               + command_folder(Path(tmp)) + formats())
    if state is None:
        os.environ.pop("STAND_STATE_DIR", None)
    else:
        os.environ["STAND_STATE_DIR"] = state
    return report(bad, f"{len(CASES)} входов сторожей, PR без «Локального повтора», квитанция "
                       "push, push с кодом без «Чистого контура», папка команды (cd, git -C, папка "
                       "сессии), форматы Claude Code / Codex / Cursor, правила Codex")

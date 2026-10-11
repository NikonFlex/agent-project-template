"""Состояние обвязки агентов и все контрольные случаи — одной командой, без сети.

    python3 tools/harness.py

Печатает: разложены ли git-хуки и `git ticket`, подключены ли сторожа в Claude Code, Codex
и Cursor, совпадают ли собранные правила, стоит ли gitleaks; затем гоняет `--selftest`
каждого прибора и сторожа. Код 1 — что-то из этого не так. Сетевые проверки (валидатор
OpenSpec, jscpd) — в `sh tools/check.sh`.
"""

from __future__ import annotations

import subprocess
import sys

from project_config import ticket_number
from script_common import ROOT, git_env

#: Где каждая среда подключает сторож — файл и строка, которая обязана в нём быть (кавычка
#: перед именем среды в JSON экранирована).
WIRING = {
    "Claude Code": (".claude/settings.json", 'guard.py\\" claude'),
    "Codex": (".codex/hooks.json", 'guard.py\\" codex'),
    "Cursor": (".cursor/hooks.json", 'guard.py\\" cursor'),
}
SELFTESTS = [
    "tools/backlog.py", "tools/spec_check.py", "tools/source_check.py", "tools/paths_check.py",
    "tools/task_links.py", "tools/scenario.py", "tools/req_status.py", "tools/commit_check.py",
    "tools/closing_words.py", "tools/claude_hooks.py", "tools/fpsr.py", "tools/context_check.py",
    "tools/context_budget.py", "tools/agent_rules.py", "tools/secret_scan.py", "tools/changelog.py",
    ".agents/guards/guard.py",
]
STATE_CHECKS = [["tools/agent_rules.py"], ["tools/context_budget.py"],
                ["tools/scenario.py", "--control"], ["tools/secret_scan.py", "version"]]


def run(args: list[str]) -> tuple[bool, str]:
    """Код возврата и последняя строка вывода прибора."""
    done = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True,  # noqa: S603
                          text=True, check=False, env=git_env())
    lines = (done.stdout + done.stderr).strip().splitlines()
    return done.returncode == 0, lines[-1] if lines else ""


def state() -> list[tuple[bool, str]]:
    hooks = subprocess.run(["git", "config", "core.hooksPath"], cwd=ROOT, capture_output=True,  # noqa: S607
                           text=True, check=False, env=git_env()).stdout.strip()
    rows = [(hooks == "templates/githooks", f"git-хуки: core.hooksPath = {hooks or '—'} "
             "(sh tools/install_hooks.sh)")]
    number = ticket_number(ROOT)
    rows.append((True, f"git ticket: {f'#{number}' if number else '— (git ticket <номер>)'}"))
    for env, (path, needle) in WIRING.items():
        file = ROOT / path
        wired = file.exists() and needle in file.read_text(encoding="utf-8")
        rows.append((wired, f"сторож в {env}: {path}"))
    for args in STATE_CHECKS:
        ok, last = run(args)
        rows.append((ok, f"{' '.join(args)}: {last}"))
    return rows


def main() -> int:
    rows = state() + [(ok, f"{path} --selftest: {last}")
                      for path in SELFTESTS for ok, last in [run([path, "--selftest"])]]
    for ok, line in rows:
        print(f"{'ok  ' if ok else 'FAIL'}  {line}")
    failed = sum(not ok for ok, _ in rows)
    print("обвязка в порядке" if not failed else f"не в порядке: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

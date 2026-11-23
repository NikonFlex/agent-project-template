"""Автоматизация ночного аудита для bb (script mode): тихий пропуск без коммитов, иначе фаза 1.

    python3 tools/nightly_automation.py [--hours 24] [--command tools/nightly_audit.py]
    python3 tools/nightly_automation.py --selftest

Для `bb automation create --script-file tools/nightly_automation.py --interpreter python3`:
работает в папке проекта (`--working-directory project`). За последние `--hours` не было
first-parent коммитов — последняя строка `{"wakeAgent": false}`, выход 0 (bb считает тихим
пропуском). Были — запускает фазу 1 (`--command`, по умолчанию `tools/nightly_audit.py`) с
вычисленной first-parent базой в `--base` и отдаёт её код выхода и вывод. Ошибка git —
явный сбой. Постоянное расписание создаётся отдельно, по авторизации.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

import bb_cli
from nightly_audit import is_app_path
from script_common import git_env, output, report


def first_parent_base(root: Path, hours: int) -> str | None:
    """База фазы 1: первый родитель самого старого first-parent коммита за `hours` ч (как
    `default_base` в tools/nightly_audit.py). Нет коммитов — None; ошибка git — исключение
    (явный сбой, не тихий пропуск)."""
    daily = output(root, "git", "rev-list", "--first-parent",
                   f"--since-as-filter={hours}.hours.ago", "HEAD").splitlines()
    if not daily:
        return None
    parents = output(root, "git", "rev-list", "--parents", "-n", "1", daily[-1]).split()
    if len(parents) > 1:
        return parents[1]
    return output(root, "git", "hash-object", "-t", "tree", "/dev/null").strip()


def run_phase1(root: Path, command: list[str], base: str) -> int:
    """Фаза 1 (`tools/nightly_audit.py --base <base>`) — её вывод и код выхода насквозь."""
    done = subprocess.run([sys.executable, *command, "--base", base], cwd=root,  # noqa: S603
                          capture_output=True, text=True, check=False, env=git_env())
    if done.stdout:
        print(done.stdout, end="")
    if done.stderr:
        print(done.stderr, end="", file=sys.stderr)
    return done.returncode


def has_app_changes(root: Path, base: str) -> bool:
    """Менялась ли основная логика приложения между `base` и `HEAD`."""
    changed = output(root, "git", "diff", "--name-only", base, "HEAD").splitlines()
    return any(is_app_path(path) for path in changed if path)


def report_or_run(root: Path, hours: int, command: list[str]) -> int:
    """Тихий пропуск (`{"wakeAgent": false}`) без правок приложения; иначе фаза 1."""
    try:
        base = first_parent_base(root, hours)
        app_touched = base is not None and has_app_changes(root, base)
    except subprocess.CalledProcessError as exc:
        print(f"ночной аудит: git не ответил: {exc}", file=sys.stderr)
        return 1
    if not app_touched or base is None:
        print('{"wakeAgent": false}')
        return 0
    return run_phase1(root, command, base)


# --------------------------------------------------------------------------- selftest

STUB = ("import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "Path('tmp/nightly-report.json').parent.mkdir(exist_ok=True)\n"
        "Path('tmp/nightly-report.json').write_text(json.dumps({'argv': sys.argv[1:]}))\n"
        "print('фаза 1 пройдена')\n")


def _commit(root: Path, message: str, days_ago: int) -> None:
    env = git_env()
    stamp = (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S%z")
    env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "add", "-A"], check=True, env=env)  # noqa: S607
    subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",  # noqa: S607
         "commit", "-q", "-m", message], check=True, env=env)


def _repo(root: Path, app_file: str = "src/app.py") -> Path:
    """Временный репозиторий с заглушкой фазы 1."""
    (root / "tools").mkdir(parents=True)
    (root / "tools/nightly_audit.py").write_text(STUB, encoding="utf-8")
    target = root / app_file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "init", "-q"], check=True, env=git_env())  # noqa: S607
    return root


def selftest() -> int:
    bad: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        quiet = _repo(base / "quiet")
        _commit(quiet, "старый коммит", days_ago=2)
        active = _repo(base / "active")
        _commit(active, "свежий коммит", days_ago=0)
        infra = _repo(base / "infra", app_file="tools/bb_arena.py")
        _commit(infra, "только арена", days_ago=0)
        if first_parent_base(quiet, 24) is not None:
            bad.append("старый коммит засчитан свежим")
        if first_parent_base(active, 24) is None:
            bad.append("свежий коммит не дал базу")
        out = io.StringIO()
        with redirect_stdout(out):
            code = report_or_run(quiet, 24, ["tools/nightly_audit.py"])
        if code != 0 or out.getvalue().strip() != '{"wakeAgent": false}':
            bad.append("тихий пропуск не дал «{\"wakeAgent\": false}»")
        out_infra = io.StringIO()
        with redirect_stdout(out_infra):
            code_infra = report_or_run(infra, 24, ["tools/nightly_audit.py"])
        if code_infra != 0 or out_infra.getvalue().strip() != '{"wakeAgent": false}':
            bad.append("коммит только в tools/bb_arena.py должен давать тихий пропуск")
        out = io.StringIO()
        with redirect_stdout(out):
            code = report_or_run(active, 24, ["tools/nightly_audit.py"])
        if code != 0:
            bad.append("активный путь вернул не ноль")
        if "фаза 1 пройдена" not in out.getvalue():
            bad.append("вывод фазы 1 не прошёл насквозь")
        argv = json.loads((active / "tmp/nightly-report.json").read_text(encoding="utf-8"))["argv"]
        if len(argv) != 2 or argv[0] != "--base" or not argv[1]:
            bad.append(f"фазе 1 не передан --base: {argv}")
        if (quiet / "tmp/nightly-report.json").exists():
            bad.append("фаза 1 запущена при отсутствии коммитов")
        non_git = base / "not-a-repo"
        non_git.mkdir()
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            code = report_or_run(non_git, 24, ["tools/nightly_audit.py"])
        if code == 0:
            bad.append("ошибка git не стала явным сбоем")
    return report(bad, "тихий пропуск без коммитов, активный путь с --base, ошибка git — сбой")


def _add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hours", type=int, default=24, help="окно свежих коммитов")
    parser.add_argument("--command", default="tools/nightly_audit.py",
                        help="скрипт фазы 1 относительно корня")


def _body(args: argparse.Namespace) -> int:
    return report_or_run(Path.cwd(), args.hours, args.command.split())


def main() -> int:
    return bb_cli.main("Автоматизация ночного аудита bb", selftest, _add_args, _body)


if __name__ == "__main__":
    sys.exit(main())

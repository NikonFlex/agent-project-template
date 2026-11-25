"""Сквозная проверка Рундо с подставным BB и синтетической задачей."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tools.script_common import git_env

REPO_ROOT = Path(__file__).resolve().parents[1]
NUMBER = "42"
PROJECT = "proj_fake"
PARENT = "thr_parent"
RED_MARKER = "RED-MARKER: demo.add must exist"

BASE_FILES = {
    ".backlog.json": '{"prefix": "demo", "tasks_dir": "issues"}\n',
    ".gitignore": "tmp/\n.pytest_cache/\n__pycache__/\n*.pyc\n",
    "issues/demo-0042.md": (
        "# demo#42 — демо\n\n"
        "## Описание из трекера\n\n"
        "Добавить `demo.add(a, b)`, возвращающую сумму.\n\n"
        "## Готово, когда\n\nРезультат проверен без сети.\n\n"
        "## Комментарии из трекера\n\nЧужой комментарий.\n\n"
        "<!-- ↓↓↓ ниже наше: скрипт эту часть не трогает ↓↓↓ -->\n\n"
        "## Чем возобновлять\n\nДобавить функцию и тест.\n"
    ),
    "docs/dev-plan.md": "# План разработки\n",
    "openspec/specs/demo/spec.md": "# Демо\n\nТребование: `add` складывает два числа.\n",
    # Быстрая проверка должна быть настоящим вызовом: маркер в tmp/ доказывает запуск.
    "tools/check.sh": (
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" >> \"$(dirname \"$0\")/../tmp/quick-ran.log\"\n"
        "exit 0\n"
    ),
}

THREE_PATHS = [
    {"path": "demo.py", "purpose": "implementation"},
    {"path": "tests/test_demo.py", "purpose": "test"},
    {"path": "issues/demo-0042.md", "purpose": "task"},
]

FOUR_PATHS_WITH_DUPLICATE = [
    *THREE_PATHS,
    {"path": "openspec/specs/demo/spec.md", "purpose": "spec"},
    {"path": "tests/test_demo.py", "purpose": "test"},
]

THREE_KNOWN = {"paths": THREE_PATHS, "contract": "known"}
THREE_NEW = {"paths": THREE_PATHS, "contract": "new"}
FOUR_KNOWN = {"paths": FOUR_PATHS_WITH_DUPLICATE, "contract": "known"}


def _write(path: Path, text: str) -> None:
    """Записать текст в файл, создав родительские каталоги."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def _git(repo: Path, *args: str) -> str:
    """Вывод git-команды; ненулевой код — естественное исключение check_output."""
    return subprocess.check_output(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repo, text=True, encoding="utf-8", env=git_env(),
    )


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    """Чистая база: один коммит, `tmp/` в .gitignore, локальная задача demo#42."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for path, text in BASE_FILES.items():
        _write(repo / path, text)
    (repo / "tools/check.sh").chmod(0o755)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-q", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD").strip()


def _install_fake(tmp_path: Path, knobs: dict | None = None) -> tuple[Path, Path, Path, Path]:
    """Исполняемый `bb` и `gh` в отдельном каталоге PATH; журналы — рядом с тестом."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "fake-state.json"
    log = tmp_path / "fake-log.jsonl"
    gh_log = tmp_path / "gh-log.txt"
    settings = {"project": PROJECT, "parent": PARENT, **(knobs or {})}
    bb = bin_dir / "bb"
    bb.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'tests')!r})\n"
        "from rundo_fake import main\n"
        f"sys.exit(main(sys.argv[1:], {str(state)!r}, {str(log)!r}, {settings!r}))\n",
        encoding="utf-8",
    )
    bb.chmod(0o755)
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {str(gh_log)!r}\n"
        "exit 1\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    return bin_dir, state, log, gh_log


def _env(bin_dir: Path) -> dict[str, str]:
    env = git_env()
    env.update({
        "PYTHONPATH": str(REPO_ROOT),
        "BB_PROJECT_ID": PROJECT,
        "BB_THREAD_ID": PARENT,
        "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
    })
    return env


def _run(repo: Path, env: dict[str, str], *flags: str) -> subprocess.CompletedProcess[str]:
    """Публичный вход из cwd временного репозитория: `run <номер>` без --request-file."""
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "tools.rundo", "run", NUMBER, *flags],
        cwd=repo, env=env, check=False, capture_output=True, text=True, timeout=240,
    )


def _ok(done: subprocess.CompletedProcess[str]) -> None:
    assert done.returncode == 0, f"stdout:\n{done.stdout}\nstderr:\n{done.stderr}"


def _entries(log: Path) -> list[dict]:
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def _spawns(entries: list[dict]) -> list[list[str]]:
    return [entry["argv"] for entry in entries if entry["argv"][:2] == ["thread", "spawn"]]


def _rpcs(entries: list[dict]) -> list[list[str]]:
    return [entry["argv"] for entry in entries if entry["argv"][:3] == ["plugin", "rpc", "call"]]


def _opt(argv: list[str], flag: str) -> str | None:
    if flag not in argv:
        return None
    index = argv.index(flag)
    return argv[index + 1] if index + 1 < len(argv) else None


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _exchange(repo: Path) -> tuple[Path, Path, dict]:
    """Путь worktree, каталог обмена и state.json контроллера (в дереве контроллера)."""
    state = _json(repo / "tmp" / "rundo" / NUMBER / "state.json")
    worktree = Path(state["worktree"])
    return worktree, worktree / "tmp" / "rundo" / NUMBER, state


def _case(tmp_path: Path, knobs: dict | None = None, *, success: bool = True
          ) -> tuple[Path, Path, Path, subprocess.CompletedProcess[str], list[list[str]]]:
    """Один прогон на чистой базе: репозиторий, fake bb, журнал, результат, spawn."""
    repo, _ = _make_repo(tmp_path)
    bin_dir, _, log_path, _ = _install_fake(tmp_path, knobs)
    done = _run(repo, _env(bin_dir))
    if success:
        _ok(done)
    return repo, bin_dir, log_path, done, _spawns(_entries(log_path))


def _gemini_case(tmp_path: Path, knobs: dict) -> dict:
    """Прогон, где Gemini обязателен: проверяет порядок и возвращает gate.json."""
    repo, *_, spawns = _case(tmp_path, knobs)
    assert len(spawns) == 5, f"ожидались пять тредов, их {len(spawns)}"
    assert _opt(spawns[1], "--model") == "gemini-4-neon", spawns[1]
    _, exchange, _ = _exchange(repo)
    return _json(exchange / "gate.json")


def _assert_spawn_contract(spawns: list[list[str]], worktree: Path, environment_id: str) -> None:
    """Каждый spawn — Full Access, visible, от родителя и в одном окружении."""
    assert spawns, "не было ни одного spawn"
    for argv in spawns:
        assert argv[-1] == "--json" or "--json" in argv, f"spawn без --json: {argv}"
        assert _opt(argv, "--project") == PROJECT, f"чужой проект: {argv}"
        assert _opt(argv, "--parent-thread") == PARENT, f"чужой родитель: {argv}"
        assert _opt(argv, "--permission-mode") == "accept-edits", f"не accept-edits: {argv}"
        assert _opt(argv, "--visibility") == "visible", f"не видимый тред: {argv}"
        assert _opt(argv, "--prompt-file"), f"spawn без --prompt-file: {argv}"
        assert _opt(argv, "--environment"), f"spawn без окружения: {argv}"
    assert Path(_opt(spawns[0], "--environment") or "").resolve() == worktree.resolve()
    for argv in spawns[1:]:
        assert _opt(argv, "--environment") == environment_id, f"другое окружение: {argv}"


def _assert_junit(path: Path, *, failed: bool, marker: str) -> None:
    assert path.is_file(), f"нет {path}"
    cases = ET.parse(path).getroot().findall(".//testcase")  # noqa: S314
    assert len(cases) == 1, f"{path}: ожидался один testcase, их {len(cases)}"
    case = cases[0]
    assert case.get("name", "").endswith("test_add_sums"), f"{path}: чужой testcase {case.attrib}"
    assert case.find("error") is None, f"{path}: error вместо failure"
    assert case.find("skipped") is None, f"{path}: тест пропущен"
    failure = case.find("failure")
    if not failed:
        assert failure is None, f"{path}: зелёный прогон содержит failure"
        return
    assert failure is not None, f"{path}: красный прогон без failure"
    text = (failure.get("message") or "") + (failure.text or "")
    assert marker in text, f"{path}: в failure нет маркера {marker!r}"


def _assert_log_counter(text: str, code: int) -> None:
    assert re.search(rf"(?:exit|код)[^\n]*\b{code}\b", text, re.IGNORECASE), \
        f"в логе нет кода выхода {code}:\n{text}"


def _acceptance_revision(worktree: Path) -> str:
    """Алгоритм Арены: SHA-256 `git rev-parse HEAD` + `git diff --binary HEAD` после add -N."""
    _git(worktree, "add", "-N", "tests/test_demo.py", "demo.py")
    head = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"],  # noqa: S603, S607
                          check=True, capture_output=True, env=git_env()).stdout
    diff = subprocess.run(["git", "-C", str(worktree), "diff", "--binary", "HEAD", "--"],  # noqa: S603, S607
                          check=True, capture_output=True, env=git_env()).stdout
    return hashlib.sha256(head + diff).hexdigest()


def test_full_pipeline_e2e_with_fake_bb(tmp_path):
    """Один полный путь: plan → Gemini → red → implementation → приёмка → draft."""
    repo, base = _make_repo(tmp_path)
    bin_dir, _, log_path, gh_log = _install_fake(tmp_path)

    done = _run(repo, _env(bin_dir))
    _ok(done)

    entries = _entries(log_path)
    assert all("bb-jevstice" not in call["argv"] for call in entries)
    spawns = _spawns(entries)
    models = [_opt(argv, "--model") for argv in spawns]
    assert models == ["gpt-6.2-sol", "gemini-4-neon",
                      "deepseek/deepseek-flash", "deepseek/deepseek-flash",
                      "deepseek/deepseek-flash"], f"порядок ролей: {models}"

    worktree, exchange, state = _exchange(repo)
    request = (exchange / "request.md").read_text(encoding="utf-8")
    assert "## Готово, когда\n\nРезультат проверен без сети." in request
    assert "Чужой комментарий" not in request
    assert "Чем возобновлять" not in request
    environment_id = state["environmentId"]
    _assert_spawn_contract(spawns, worktree, environment_id)

    assert state["base"] == base
    assert state["project"] == PROJECT
    assert state["parentId"] == PARENT
    assert state["stage"] == "done"
    assert state["pending"] is None
    roles = state["roles"]
    assert set(roles) == {"plan", "gemini", "red", "implementation", "draft"}
    assert len(set(roles.values())) == 5, f"роли делят тред: {roles}"
    assert _git(worktree, "rev-parse", "HEAD").strip() == base

    gate = _json(exchange / "gate.json")
    plan_sha = hashlib.sha256((exchange / "plan.json").read_bytes()).hexdigest()
    assert gate["planSha256"] == plan_sha
    assert gate["uniquePaths"] == 5
    assert gate["geminiRequired"] is True
    assert gate["reasons"]
    assert state["planSha256"] == plan_sha
    gemini = _json(exchange / "gemini.json")
    assert gemini["planSha256"] == plan_sha
    assert gemini["verdict"] == "approved"

    methods = [_opt(argv, "--input-file") and argv[4] for argv in _rpcs(entries)]
    assert "jev.workflow.requirements" in methods
    assert "jev.workflow.plan" in methods
    assert "jev.workflow.acceptance" in methods

    def _when(method: str) -> int:
        return next(index for index, entry in enumerate(entries)
                    if entry["argv"][:5] == ["plugin", "rpc", "call", "bb-jev", method])

    requirements_at = _when("jev.workflow.requirements")
    plan_at = _when("jev.workflow.plan")
    acceptance_at = _when("jev.workflow.acceptance")
    red_spawn_at = entries.index({"argv": spawns[2]})
    implementation_spawn_at = entries.index({"argv": spawns[3]})
    draft_spawn_at = entries.index({"argv": spawns[4]})
    assert requirements_at < plan_at < red_spawn_at
    assert implementation_spawn_at < acceptance_at < draft_spawn_at

    for name in ("requirements", "plan", "acceptance"):
        payload = _json(exchange / f"{name}.input.json")
        assert payload["threadId"]
        assert isinstance(payload["input"], dict)
        receipt = _json(exchange / f"{name}.receipt.json")
        assert receipt["stage"] == name
        assert receipt["status"] == "passed"
    requirements = _json(exchange / "requirements.receipt.json")
    plan = _json(exchange / "plan.receipt.json")
    acceptance = _json(exchange / "acceptance.receipt.json")
    assert plan["parentReviewId"] == requirements["reviewId"]
    assert acceptance["parentReviewId"] == plan["reviewId"]
    assert acceptance["freshness"] == "current"
    assert acceptance["revision"] == _acceptance_revision(worktree)

    red_log = (exchange / "red.log").read_text(encoding="utf-8")
    assert "test_add_sums" in red_log
    assert RED_MARKER in red_log
    _assert_log_counter(red_log, 1)
    _assert_junit(exchange / "red.xml", failed=True, marker=RED_MARKER)
    green_log = (exchange / "green.log").read_text(encoding="utf-8")
    assert "test_add_sums" in green_log
    _assert_log_counter(green_log, 0)
    _assert_junit(exchange / "green.xml", failed=False, marker=RED_MARKER)
    quick_log = (exchange / "quick.log").read_text(encoding="utf-8")
    assert "check.sh" in quick_log and "--quick" in quick_log
    _assert_log_counter(quick_log, 0)
    assert (worktree / "tmp" / "quick-ran.log").is_file(), "check.sh не запускался в worktree"

    pr = _json(exchange / "pr.json")
    assert pr["title"].strip()
    assert pr["body"].strip()

    changed = {line[3:] for line in _git(worktree, "status", "--porcelain").splitlines()
               if line.strip()}
    assert changed <= {"demo.py", "tests/test_demo.py"}, f"лишние правки: {changed}"
    assert _git(repo, "rev-parse", "HEAD").strip() == base, "контроллер сделал коммит"
    assert not gh_log.exists(), "пайплайн трогал GitHub"


def test_gate_counts_four_unique_paths_with_duplicate(tmp_path):
    """Порог 4/5: дубль не считается, Gemini не нужен, путь продолжается."""
    repo, *_, spawns = _case(tmp_path, FOUR_KNOWN)
    assert len(spawns) == 4, f"Gemini не нужен, а тредов {len(spawns)}"
    assert all("gemini" not in (_opt(argv, "--model") or "") for argv in spawns)
    _, exchange, _ = _exchange(repo)
    gate = _json(exchange / "gate.json")
    assert gate["uniquePaths"] == 4
    assert gate["geminiRequired"] is False
    assert gate["reasons"] == []
    assert _json(exchange / "pr.json")["body"].strip()


def test_gate_new_contract_requires_gemini(tmp_path):
    """Новый контракт запускает Gemini даже при трёх путях, причина названа."""
    gate = _gemini_case(tmp_path, THREE_NEW)
    assert gate["uniquePaths"] == 3
    assert gate["geminiRequired"] is True
    assert any("contract" in reason.lower() or "контракт" in reason.lower()
               for reason in gate["reasons"]), gate["reasons"]


def test_gate_spec_gap_requires_gemini(tmp_path):
    """Пробел спеки запускает Gemini независимо от числа путей."""
    gate = _gemini_case(tmp_path, {**THREE_KNOWN, "spec_gap": True})
    assert gate["geminiRequired"] is True
    assert any("gap" in reason.lower() or "пробел" in reason.lower()
               or "spec" in reason.lower() for reason in gate["reasons"]), gate["reasons"]


def test_foreign_gemini_hash_blocks_red_thread(tmp_path):
    """Чужой SHA плана в gemini.json: тред теста не создаётся, прогон падает."""
    _, _, _, done, spawns = _case(tmp_path, {**THREE_NEW, "gemini_sha": "wrong"},
                                  success=False)
    assert done.returncode != 0, f"чужой хеш принят:\n{done.stdout}"
    assert len(spawns) == 2, f"после Gemini не должно быть тредов: {len(spawns)}"
    assert all("deepseek" not in (_opt(argv, "--model") or "") for argv in spawns)


def test_plan_without_red_blocks_test_thread(tmp_path):
    """Неполный план без red: тред теста не создаётся."""
    _, _, _, done, spawns = _case(tmp_path, {**THREE_KNOWN, "incomplete": "red"},
                                  success=False)
    assert done.returncode != 0, "неполный план принят"
    assert spawns, "тред плана не создавался: проверка не дошла до плана"
    assert not any("deepseek" in (_opt(argv, "--model") or "") for argv in spawns), \
        f"тред теста создан по неполному плану: {spawns}"


@pytest.mark.parametrize("mode", ["stale", "foreign"])
def test_acceptance_must_be_current_and_linked(tmp_path, mode):
    """Устаревшая или чужая квитанция приёмки: черновик PR не пишется."""
    repo, _, _, done, spawns = _case(tmp_path, {**THREE_KNOWN, "acceptance_mode": mode},
                                     success=False)
    assert done.returncode != 0, f"квитанция {mode} принята:\n{done.stdout}"
    deepseek = [argv for argv in spawns if "deepseek" in (_opt(argv, "--model") or "")]
    assert len(deepseek) == 2, f"должны быть только тест и реализация: {spawns}"
    _, exchange, _ = _exchange(repo)
    assert not (exchange / "pr.json").exists()


def test_second_run_resumes_without_new_threads(tmp_path):
    """Повтор команды продолжает сохранённый прогон и не поднимает новых тредов."""
    repo, bin_dir, log_path, *_ = _case(tmp_path)
    first = len(_spawns(_entries(log_path)))
    assert first == 5

    _ok(_run(repo, _env(bin_dir)))
    assert len(_spawns(_entries(log_path))) == first, "повтор поднял новые треды"
    worktree, exchange, state = _exchange(repo)
    assert state["stage"] == "done"
    assert _json(exchange / "pr.json")["body"].strip()

    code = worktree / "demo.py"
    _write(code, code.read_text(encoding="utf-8") + "\n# Worktree changed after acceptance.\n")
    changed = _run(repo, _env(bin_dir))
    assert changed.returncode == 1
    assert "ревизия изменилась после приёмки" in changed.stderr
    assert len(_spawns(_entries(log_path))) == first


def test_jevstice_is_opt_in_and_binds_before_planner(tmp_path):
    repo, _ = _make_repo(tmp_path)
    bin_dir, _, log, _ = _install_fake(tmp_path)
    _ok(_run(repo, _env(bin_dir), "--jevstice"))

    entries = _entries(log)
    activation = next(index for index, call in enumerate(entries)
                      if call["argv"][3:5] == ["bb-jevstice", "jevstice.session.on"])
    first_role = entries.index({"argv": _spawns(entries)[0]})
    assert activation < first_role
    _, exchange, state = _exchange(repo)
    assert state["jevstice"] is True
    assert set(_json(exchange / "jevstice.input.json")) == {"threadId", "request"}
    plan_prompt = Path(_opt(_spawns(entries)[0], "--prompt-file") or "")
    assert "jevstice_requirements" in plan_prompt.read_text(encoding="utf-8")

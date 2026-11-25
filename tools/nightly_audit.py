"""Фаза 1 ночного аудита проекта. Выход — tmp/nightly-report.json."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from importlib.util import find_spec
from pathlib import Path
from time import monotonic
from unittest.mock import patch

from script_common import ROOT, git_env, output, report

REPORT = Path("tmp/nightly-report.json")
APP_DIFF = Path("tmp/nightly-app.diff")
APP_PREFIXES = ("src/", "openspec/specs/", "tests/")
INFRA_CONFIG_PREFIXES = ("config/bb_", "config/arena/")
CONFIG = Path("pyproject.toml")
COVERAGE = "coverage.json"
E2E = "tests/e2e"
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)
PYTEST = [sys.executable, "-m", "pytest"]
TAIL_LINES = 16
FAILED_TAIL_LINES = 40


class Status(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    FINDINGS = "findings"


def skipped(reason: str) -> dict:
    return {"status": Status.SKIPPED, "reason": reason}


def stage(root: Path, command: list[str], tail_lines: int = TAIL_LINES) -> dict:
    started = monotonic()
    env = git_env()
    env["PATH"] = os.pathsep.join((str(Path(sys.executable).parent), env.get("PATH", "")))
    env["PYTHONPATH"] = str(root)
    print(shlex.join(command), flush=True)
    try:
        result = subprocess.run(  # noqa: S603 — команды заданы этапами аудита
            command,
            cwd=root,
            env=env,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
    except OSError as exc:
        return {"status": Status.FAILED, "command": command, "reason": str(exc)}
    if result.returncode:
        tail_lines = max(tail_lines, FAILED_TAIL_LINES)
    return {
        "status": Status.PASSED if result.returncode == 0 else Status.FAILED,
        "command": command,
        "exit_code": result.returncode,
        "seconds": round(monotonic() - started, 2),
        "tail": "\n".join(result.stdout.strip().splitlines()[-tail_lines:]),
    }


def git(root: Path, *args: str) -> str:
    return output(root, "git", *args)


def default_base(root: Path) -> str:
    daily = git(root, "rev-list", "--first-parent", "--since-as-filter=24.hours.ago", "HEAD")
    if not daily:
        return git(root, "rev-parse", "HEAD").strip()
    oldest = daily.splitlines()[-1]
    parents = git(root, "rev-list", "--parents", "-n", "1", oldest).split()
    return (
        parents[1]
        if len(parents) > 1
        else git(root, "hash-object", "-t", "tree", "/dev/null").strip()
    )


def is_app_path(path: str) -> bool:
    """Основная логика приложения, её спеки и тесты; обвязка (`tools/`, `config/bb_*`) — нет."""
    if path.startswith(APP_PREFIXES):
        return True
    return path.startswith("config/") and not path.startswith(INFRA_CONFIG_PREFIXES)


def changed_files(root: Path, base: str) -> tuple[list[str], set[str]]:
    tracked = git(root, "diff", "--no-renames", "--name-only", "-z", base, "--")
    new = git(root, "ls-files", "--others", "--exclude-standard", "-z")
    untracked = {p for p in new.strip("\0").split("\0") if p and is_app_path(p)}
    all_app = {p for p in tracked.strip("\0").split("\0") if p and is_app_path(p)} | untracked
    return sorted(all_app), untracked


def save_app_diff(root: Path, base: str, files: list[str]) -> None:
    """Сохраняет детерминированный дифф только по файлам приложения для Фазы 2."""
    path = root / APP_DIFF
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (git(root, "diff", "--no-ext-diff", "--no-color", base, "--", *files)
            if files else "")
    path.write_text(text, encoding="utf-8")


def ranges(diff: str) -> list[tuple[int, int]]:
    return [
        (int(start), int(start) + max(1, int(count or 1)) - 1)
        for start, count in HUNK.findall(diff)
    ]


def changed_ranges(root: Path, base: str, path: str, untracked: set[str]) -> list[tuple[int, int]]:
    if path in untracked:
        return [(1, len((root / path).read_bytes().splitlines()))]
    return ranges(
        git(
            root,
            "diff",
            "--no-ext-diff",
            "--no-color",
            "--no-renames",
            "--unified=0",
            base,
            "--",
            path,
        )
    )


def crap(complexity: int, coverage: float) -> float:
    return complexity**2 * (1 - coverage) ** 3 + complexity


def covered_fraction(summary: dict) -> float:
    if summary["num_branches"]:
        return summary["covered_branches"] / summary["num_branches"]
    return summary["covered_lines"] / max(1, summary["num_statements"])


def functions(blocks, prefix=""):
    for block in blocks:
        name = prefix + block.name
        if hasattr(block, "methods"):
            yield from functions([*block.methods, *block.inner_classes], name + ".")
        else:
            yield name, block
            yield from functions(block.closures, name + ".")


def file_scores(source: str, changes: list[tuple[int, int]], coverage: dict) -> list[dict]:
    from radon.visitors import ComplexityVisitor

    visitor = ComplexityVisitor.from_code(source)
    scores = []
    for name, block in functions([*visitor.functions, *visitor.classes]):
        if not any(start <= block.endline and end >= block.lineno for start, end in changes):
            continue
        region = coverage.get(name)
        fraction = covered_fraction(region["summary"]) if region else None
        scores.append(
            {
                "function": name,
                "line": block.lineno,
                "complexity": block.complexity,
                "coverage": fraction,
                "crap": round(crap(block.complexity, fraction), 2)
                if fraction is not None
                else None,
            }
        )
    return scores


def crap_stage(root: Path, files: dict, coverage_path: Path, threshold: int) -> dict:
    if not files:
        return skipped("нет изменений в существующих файлах src/*.py")
    if not coverage_path.exists():
        return skipped("pytest не записал покрытие")
    if find_spec("radon") is None:
        return {"status": Status.FAILED, "reason": "нужен radon из tools/nightly-requirements.txt"}
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    scores = [
        {"file": path, **score}
        for path, changes in files.items()
        for score in file_scores(
            (root / path).read_text(encoding="utf-8"),
            changes,
            coverage["files"].get(path, {}).get("functions", {}),
        )
    ]
    scores.sort(
        key=lambda row: (row["crap"] is None, -(row["crap"] or 0), row["file"], row["line"])
    )
    above = sum(row["crap"] is not None and row["crap"] >= threshold for row in scores)
    missing = sum(row["crap"] is None for row in scores)
    return {
        "status": Status.FINDINGS if above or missing else Status.PASSED,
        "threshold": threshold,
        "above": above,
        "missing_coverage": missing,
        "functions": scores,
    }


def e2e_result(result: dict, junit: Path) -> dict:
    if result.get("exit_code") == 5:
        return result | skipped("нет тестов с маркером e2e")
    if result["status"] == Status.PASSED and junit.is_file():
        cases = ET.parse(junit).findall(".//testcase")  # noqa: S314 — файл pytest этого прогона
        if cases and all(case.find("skipped") is not None for case in cases):
            return result | skipped("все E2E пропущены pytest, см. причины в tail")
    return result


def e2e_stage(root: Path, junit: Path) -> dict:
    if not (root / E2E).is_dir():
        return skipped("каталог tests/e2e отсутствует")
    tests = (root / E2E).rglob("*.py")
    if not any(path.match("test_*.py") or path.match("*_test.py") for path in tests):
        return skipped("в tests/e2e нет файлов тестов")
    if find_spec("playwright") is None:
        return skipped("Playwright отсутствует в Python-окружении")
    command = [
        "env", "PROJECT_E2E=1", *PYTEST, E2E, "-m", "e2e", "-q", "-rs", f"--junitxml={junit}",
    ]
    return e2e_result(stage(root, command), junit)


def mutation_project(root: Path, work: Path, targets: list[str]) -> None:
    roots = sorted({Path(target).parts[0] for target in targets})
    support = ["tests", "tools", "config", "openspec"]
    for directory in sorted(set(roots + support)):
        if (root / directory).is_dir():
            shutil.copytree(
                root / directory, work / directory,
                ignore=shutil.ignore_patterns("__pycache__", "e2e", ".env", ".env.*"),
            )
    if (root / "pytest.ini").is_file():
        shutil.copy2(root / "pytest.ini", work)
    options = [path for path in support + ["pytest.ini"] if (work / path).exists()]
    (work / CONFIG).write_text(
        "[tool.mutmut]\n"
        f"source_paths = {json.dumps(roots)}\n"
        f"only_mutate = {json.dumps(targets)}\n"
        f"also_copy = {json.dumps(options)}\n"
        'pytest_add_cli_args_test_selection = ["-m", "not slow and not e2e"]\n'
        "use_setproctitle = false\n",
        encoding="utf-8",
    )


def mutation_stage(root: Path, work: Path, targets: list[str], workers: int) -> dict:
    if not targets:
        return skipped("нет изменений в настроенных целях мутации")
    if find_spec("mutmut") is None:
        return {
            "status": Status.FAILED,
            "files": targets,
            "reason": "нужен mutmut из tools/nightly-requirements.txt",
        }
    mutation_project(root, work, targets)
    result = stage(work, ["mutmut", "run", "--max-children", str(workers)]) | {"files": targets}
    if result["status"] == Status.FAILED:
        return result
    export = stage(work, ["mutmut", "export-cicd-stats"])
    if export["status"] == Status.FAILED:
        return export | {"files": targets}
    stats_path = work / "mutants/mutmut-cicd-stats.json"
    if not stats_path.exists():
        return result | {"status": Status.FAILED, "reason": "mutmut не записал статистику"}
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    if stats["killed"] < stats["total"]:
        result["status"] = Status.FINDINGS
    return result | {"counts": stats}


def artifact_stage(run: Callable[[], dict]) -> dict:
    """Проблема отдельного артефакта оставляет в отчёте результаты остальных этапов."""
    try:
        return run()
    except (OSError, ValueError, KeyError, ET.ParseError, SyntaxError) as exc:
        return {"status": Status.FAILED, "reason": f"{type(exc).__name__}: {exc}"}


def coverage_pytest(root: Path, work: Path) -> dict:
    return stage(root, [
        "env", f"COVERAGE_FILE={work / '.coverage'}", *PYTEST, "-q", "--tb=short",
        f"--ignore={E2E}", "--cov=src", "--cov-branch", f"--cov-report=json:{work / COVERAGE}",
        f"--cov-report=lcov:{work / 'coverage.lcov'}",
    ])


def deep_hypothesis(root: Path, profile: str | None) -> dict:
    if not profile:
        return skipped("профиль Hypothesis не задан")
    seed = secrets.randbits(32)
    result = stage(root, [
        *PYTEST, "-q", "--tb=short", f"--ignore={E2E}", f"--hypothesis-profile={profile}",
        f"--hypothesis-seed={seed}", "-m", "property and slow",
    ])
    if result.get("exit_code") == 5:
        result |= skipped("нет тестов с маркерами property и slow")
    return {"seed": seed, **result}


def project_tools(root: Path) -> dict:
    path = root / CONFIG
    return tomllib.loads(path.read_text(encoding="utf-8")).get("tool", {}) if path.exists() else {}


def import_linter(root: Path, tools: dict) -> dict:
    if "importlinter" not in tools and not (root / ".importlinter").is_file():
        return skipped("нет конфигурации import-linter")
    return stage(root, ["lint-imports", "--cache-dir", "tmp/import-linter", "--no-logo"])


def run_stages(root: Path, work: Path, changes: dict, targets: list[str]) -> dict:
    tools = project_tools(root)
    config = tools.get("agent_project", {}).get("nightly", {})
    cov = work / COVERAGE
    selected = sorted(set(config.get("mutation_targets", ())) & set(targets))
    return {
        "pytest": coverage_pytest(root, work),
        "hypothesis": deep_hypothesis(root, config.get("hypothesis_profile")),
        "e2e": artifact_stage(lambda: e2e_stage(root, work / "e2e.xml")),
        "crap": artifact_stage(
            lambda: crap_stage(root, changes, cov, config.get("crap_threshold", 30)),
        ),
        "mutmut": artifact_stage(
            lambda: mutation_stage(root, work / "mutation", selected,
                                   config.get("mutation_workers", 2)),
        ),
        "import-linter": import_linter(root, tools),
        "check": stage(root, ["sh", "tools/check.sh"]),
    }


def report_code(stages: dict) -> int:
    return int(any(stage["status"] == Status.FAILED for stage in stages.values()))


def save(root: Path, data: dict) -> None:
    path = root / REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix="nightly-", delete=False
    ) as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    Path(stream.name).replace(path)


def audit(root: Path, base: str | None) -> int:
    if base is None:
        base = default_base(root)
    else:
        base = git(root, "rev-parse", "--verify", "--end-of-options", base).strip()
    files, untracked = changed_files(root, base)
    save_app_diff(root, base, files)
    sources = {
        path: changed_ranges(root, base, path, untracked)
        for path in files
        if path.startswith("src/") and path.endswith(".py") and (root / path).is_file()
    }
    commits = f"{base}..HEAD" if git(root, "cat-file", "-t", base).strip() == "commit" else "HEAD"
    data = {
        "version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "base": base,
        "head": git(root, "rev-parse", "HEAD").strip(),
        "commits": git(root, "log", "--first-parent", "--oneline", commits).splitlines(),
        "changed_files": files,
        "app_diff": str(APP_DIFF),
    }
    with tempfile.TemporaryDirectory(prefix="nightly-") as path:
        targets = [path for path in files if (root / path).is_file()]
        data["stages"] = run_stages(root, Path(path), sources, targets)
    save(root, data)
    print(REPORT)
    for name, result in data["stages"].items():
        print(f"{name}: {result['status']}")
    return report_code(data["stages"])


def e2e_opt_in_selftest() -> bool:
    """Синтетический pytest проверяет окружение внутри обоих дочерних процессов."""
    with tempfile.TemporaryDirectory() as path, patch.dict(os.environ):
        os.environ.pop("PROJECT_E2E", None)
        root = Path(path)
        tests = root / E2E
        tests.mkdir(parents=True)
        (tests / "test_example.py").touch()
        (root / "pytest.py").write_text(
            "import os, sys\n"
            "args = sys.argv[1:]\n"
            f"wanted = '1' if args[:3] == [{E2E!r}, '-m', 'e2e'] else None\n"
            "actual = os.getenv('PROJECT_E2E')\n"
            "print(f'PROJECT_E2E: ожидалось {wanted!r}, получено {actual!r}')\n"
            "sys.exit(int(actual != wanted))\n",
            encoding="utf-8",
        )
        with patch(f"{__name__}.find_spec", return_value=object()):
            e2e = e2e_stage(root, root / "e2e.xml")
        ordinary = stage(root, PYTEST)
        return e2e["status"] == ordinary["status"] == Status.PASSED


def selftest() -> int:
    with tempfile.TemporaryDirectory() as path:
        root = Path(path)
        success = stage(root, [sys.executable, "-c", "print('ok')"])
        red = stage(root, [sys.executable, "-c", "raise SystemExit(7)"])
        empty = e2e_result(red | {"exit_code": 5}, root / "none.xml")
        junit = root / "junit.xml"
        junit.write_text(
            "<testsuite><testcase><skipped /></testcase></testsuite>", encoding="utf-8"
        )
        unready = e2e_result(success, junit)
        stages = {"clean": success, "red": red, "empty": empty, "unready": unready}
        save(root, {"stages": stages})
        data = json.loads((root / REPORT).read_text(encoding="utf-8"))
    checks = [
        (data["stages"] == stages, "JSON теряет результат этапа"),
        (success["status"] == Status.PASSED and red["exit_code"] == 7, "команды в отчёте"),
        (report_code(stages) == 1 and report_code({"clean": success}) == 0, "красный код выхода"),
        (empty["status"] == unready["status"] == Status.SKIPPED, "пропуск E2E"),
        (project_tools(root) == {} and import_linter(root, {})["status"] == Status.SKIPPED,
         "без конфигурации шаблон продолжает аудит"),
        (e2e_opt_in_selftest(), "PROJECT_E2E=1 должен получить только E2E-процесс pytest"),
        (crap(5, 0) == 30 and crap(5, 1) == 5 and crap(4, 0.5) == 6, "формула CRAP"),
        (ranges("@@ -4,3 +5,2 @@\n@@ -12 +12,0 @@") == [(5, 6), (12, 12)], "диапазоны"),
        (is_app_path("src/posting.py") and is_app_path("config/app.json")
         and not is_app_path("tools/bb_arena.py") and not is_app_path("config/bb_arena.json")
         and not is_app_path("docs/work-log.md"), "фильтр области приложения"),
    ]
    return report(
        [message for ok, message in checks if not ok],
        "красный этап, отчёт, пропуски и окружение E2E, CRAP и диапазоны изменений",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", help="С какого коммита считать дифф (по умолчанию последние 24 ч)"
    )
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    return selftest() if args.selftest else audit(ROOT, args.base)


if __name__ == "__main__":
    sys.exit(main())

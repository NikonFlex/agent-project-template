"""Фактические проверки прогона: git-ревизия, JUnit red/green, быстрая проверка и Braille.

Контроллер запускает только собственные команды: node ID берётся из принятого плана,
`sh tools/check.sh --quick` — константа проекта, Braille — фиксированная последовательность
`start`/`step`/`look`/`close`. Ни одна команда не приходит из артефакта модели.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tools.rundo.artifacts import RedNode, require, truncate
from tools.rundo.config import (
    BRAILLE_BASE_ENV,
    BRAILLE_STEPS_ENV,
    BRAILLE_VALUES_ENV,
    CHECK_COMMAND,
    MAX_EVIDENCE,
    normalize_path,
)
from tools.script_common import git_env

PYTEST_TIMEOUT_SECONDS = 1800
QUICK_TIMEOUT_SECONDS = 1800
BRAILLE_TIMEOUT_SECONDS = 300
MIN_FRAGMENT = 200
SKIP_RESERVE_MAX = 4000
BRIEF_LINES = 7
BRIEF_LINE_CHARS = 220


class EvidenceError(RuntimeError):
    pass


class Evidence:
    """Проверки в одном worktree; артефакты пишутся в каталог обмена прогона."""

    def __init__(self, worktree: Path, exchange: Path) -> None:
        self.worktree = worktree
        self.exchange = exchange
        self.python = _python_for(worktree)

    def changed(self) -> set[str]:
        return changed_paths(self.worktree)

    def revision(self, planned: Sequence[str]) -> str:
        for path in planned:
            if (self.worktree / path).is_file() and not _tracked(self.worktree, path):
                _git_text(self.worktree, "add", "-N", "--", path)
        head = _git_bytes(self.worktree, "rev-parse", "HEAD")
        diff = _git_bytes(self.worktree, "diff", "--binary", "HEAD", "--")
        return hashlib.sha256(head + diff).hexdigest()

    def diff(self, plan: dict[str, Any]) -> str:
        """Реализации — полными -U0 фрагментами; остаток — test и spec/task с подписями.

        SHA привязан к полному диффу (`revision`), здесь меняется только выдержка для Jev.
        """
        groups = _diff_groups(plan)
        full = self._diff_text(())
        (self.exchange / "diff.patch").write_text(full, encoding="utf-8")
        notes = {
            path: _skip_note(path) for path in (*groups["test"], *groups["other"])
        }
        reserve = min(sum(len(note) for note in notes.values()), SKIP_RESERVE_MAX)
        implementation = self._implementation_diff(
            groups["implementation"], MAX_EVIDENCE - reserve,
        )
        remaining = MAX_EVIDENCE - len(implementation)
        other_reserve = sum(len(notes[path]) for path in groups["other"])
        tests = self._group_diff(groups["test"], remaining, notes, other_reserve)
        remaining -= len(tests)
        rest = self._group_diff(groups["other"], remaining, notes, 0)
        return (implementation + tests + rest)[:MAX_EVIDENCE]

    def file_sha(self, relative: str) -> str:
        path = self.worktree / relative
        if not path.is_file():
            raise EvidenceError(f"нет объявленного файла {relative} в worktree")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def run_red(self, node: RedNode, revision: str) -> None:
        code, cases = self._run_test(node, "red", revision)
        if code != 1:
            raise EvidenceError(f"красный прогон завершился с кодом {code}, ожидался 1")
        assert_red(cases, node)

    def run_green(self, node: RedNode, revision: str) -> None:
        code, cases = self._run_test(node, "green", revision)
        if code != 0:
            raise EvidenceError(f"зелёный прогон завершился с кодом {code}, ожидался 0")
        assert_green(cases, node)

    def run_tests(self, paths: Sequence[str], revision: str) -> None:
        """Все объявленные пути test: отдельный прогон после зелёного JUnit."""
        argv = [self.python, "-m", "pytest", "-v", *paths, "-p", "no:cacheprovider"]
        done = run_command(argv, self.worktree, QUICK_TIMEOUT_SECONDS)
        _write_log(self.exchange / "tests.log", argv, self.worktree, revision, done)
        if done.returncode != 0:
            raise EvidenceError(f"pytest по путям test: код {done.returncode}")

    def run_quick(self, revision: str) -> None:
        argv = list(CHECK_COMMAND)
        done = run_command(argv, self.worktree, QUICK_TIMEOUT_SECONDS)
        _write_log(self.exchange / "quick.log", argv, self.worktree, revision, done)
        if done.returncode != 0:
            raise EvidenceError(f"sh tools/check.sh --quick: код {done.returncode}")

    def _run_test(self, node: RedNode, label: str, revision: str) -> tuple[int, list]:
        report = self.exchange / f"{label}.xml"
        argv = [
            self.python, "-m", "pytest", "-v", node.node_id,
            f"--junitxml={report}", "-p", "no:cacheprovider",
        ]
        done = run_command(argv, self.worktree, PYTEST_TIMEOUT_SECONDS)
        _write_log(self.exchange / f"{label}.log", argv, self.worktree, revision, done)
        return done.returncode, parse_junit(report)

    def test_output(self, plan: dict[str, Any]) -> str:
        parts: list[str] = []
        for label in ("red", "green", "quick"):
            parts.append(f"[{label}]")
            parts.extend(self._brief_log(self.exchange / f"{label}.log", label))
        parts.append("[braille]")
        parts.extend(self._braille_lines(limit=10))
        prefix = "\n".join(parts)
        planned = sum(1 for item in plan["paths"] if item["purpose"] == "test")
        header = f"[tests] плановых путей: {planned}"
        budget = MAX_EVIDENCE - len(prefix) - len(header) - 2
        tests = _fit_lines(self._tests_lines(limit=400), budget)
        return truncate("\n".join([prefix, header, *tests]))

    def _diff_text(self, paths: Sequence[str]) -> str:
        return _git_bytes(
            self.worktree, "diff", "--binary", "-U0", "HEAD", "--", *paths,
        ).decode("utf-8", errors="replace")

    def _implementation_diff(self, paths: list[str], budget: int) -> str:
        fragments = [(path, self._diff_text((path,))) for path in paths]
        fragments = [(path, text) for path, text in fragments if text]
        if not fragments:
            return ""
        total = sum(len(text) for _, text in fragments)
        if total <= budget:
            return "".join(text for _, text in fragments)
        share = max(MIN_FRAGMENT, budget // len(fragments))
        parts: list[str] = []
        used = 0
        for path, text in fragments:
            note = _shorten_note(path)
            available = budget - used
            if available <= len(note):
                parts.append(note[:available])
                break
            room = max(0, min(share, available) - len(note))
            part = text[:room] + note
            parts.append(part)
            used += len(part)
        return "".join(parts)

    def _group_diff(self, paths: list[str], budget: int, notes: dict[str, str],
                    future_reserve: int) -> str:
        reserve = sum(len(notes[path]) for path in paths) + future_reserve
        available = max(0, budget - reserve)
        parts: list[str] = []
        used = 0
        for path in paths:
            fragment = self._diff_text((path,))
            if not fragment:
                continue
            if used + len(fragment) <= available:
                parts.append(fragment)
                used += len(fragment)
            elif used + len(notes[path]) <= budget:
                parts.append(notes[path])
                used += len(notes[path])
        return "".join(parts)

    def _brief_log(self, path: Path, label: str) -> list[str]:
        if not path.is_file():
            return ["нет файла"]
        lines: list[str] = []
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line.startswith("exit code:"):
                lines.append(line)
            elif label == "red" and (line.startswith("FAILED ") or "AssertionError" in line):
                lines.append(line)
            elif label == "green" and "PASSED" in line:
                lines.append(line)
            elif label == "quick" and (line.startswith("ok ") or line.startswith("FAIL ")):
                lines.append(line)
        if not lines:
            tail = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
                    if line.strip()][-2:]
            lines = [line.strip() for line in tail]
        return [line[:BRIEF_LINE_CHARS] for line in lines[:BRIEF_LINES]]

    def _tests_lines(self, limit: int = 80) -> list[str]:
        path = self.exchange / "tests.log"
        if not path.is_file():
            return ["нет прогона по путям test"]
        outcome = re.compile(r"^(?P<node>\S+::\S+)\s+(?P<state>PASSED|FAILED|ERROR|SKIPPED)\b")
        lines: list[str] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = outcome.match(line.strip())
            if match:
                lines.append(f"{match['node']} {match['state']}")
        if not lines:
            return ["названий в tests.log нет"]
        if len(lines) > limit:
            return [*lines[:limit], f"... ещё {len(lines) - limit}"]
        return lines

    def _braille_lines(self, limit: int = 10) -> list[str]:
        path = self.exchange / "braille.jsonl"
        if not path.is_file():
            return ["нет UI-наблюдений"]
        lines: list[str] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            command = " ".join(str(part) for part in record.get("command") or ())[:80]
            observed = record.get("observed") or {}
            text = observed.get("text") if isinstance(observed, dict) else None
            lines.append(f"{command}: ok={record.get('ok')} text={str(text or '')[:200]}")
        if (self.exchange / "braille.png").is_file():
            lines.append("снимок: braille.png")
        if len(lines) > limit:
            return [*lines[:limit], f"... ещё {len(lines) - limit}"]
        return lines

    def collect_braille(self, env: Mapping[str, str], revision: str) -> dict[str, Any]:
        skip = self._braille_skip(env)
        if skip is not None:
            return skip
        session = self.exchange / "braille-session"
        records: list[dict[str, Any]] = []
        base = env[BRAILLE_BASE_ENV]
        start = ["start", "--base", base, "--session", str(session)]
        if env.get(BRAILLE_VALUES_ENV):
            start += ["--values", env[BRAILLE_VALUES_ENV]]
        try:
            self._braille_run(records, start)
            for step in _load_steps(env.get(BRAILLE_STEPS_ENV)):
                self._braille_run(records, [
                    "step", json.dumps(step, ensure_ascii=False), "--session", str(session),
                ])
            look = self._braille_run(records, ["look", "--session", str(session)])
            if look.get("ok") is not True:
                raise EvidenceError("braille look: страница не наблюдена (ok=false)")
            self._save_snapshot(look, session)
        finally:
            self._braille_run(
                records, ["close", "--session", str(session)], required=False,
            )
            self._write_braille(records, revision)
        return {"status": "passed", "revision": revision, "records": len(records)}

    def _braille_skip(self, env: Mapping[str, str]) -> dict[str, Any] | None:
        if not env.get(BRAILLE_BASE_ENV):
            return {"status": "skipped", "reason": f"не задан {BRAILLE_BASE_ENV}"}
        return None

    def _braille_run(self, records: list[dict[str, Any]], argv: Sequence[str],
                     required: bool = True) -> dict[str, Any]:
        command = [self.python, "-m", "braille", *argv]
        done = run_command(command, self.worktree, BRAILLE_TIMEOUT_SECONDS)
        body = _json_or_empty(done.stdout)
        records.append({
            "command": list(argv), "exitCode": done.returncode,
            "ok": body.get("ok"), "observed": _observed(body),
        })
        if done.returncode != 0 and required:
            raise EvidenceError(f"braille {argv[0]}: код {done.returncode}: {done.stderr[:500]}")
        return body

    def _save_snapshot(self, look: dict[str, Any], session: Path) -> None:
        shot = look.get("screenshot")
        if isinstance(shot, str) and Path(shot).is_file():
            shutil.copyfile(shot, self.exchange / "braille.png")
            return
        shots = sorted((session / "out").glob("*.png"), key=lambda item: item.stat().st_mtime)
        if shots:
            shutil.copyfile(shots[-1], self.exchange / "braille.png")

    def _write_braille(self, records: list[dict[str, Any]], revision: str) -> None:
        lines = [
            json.dumps({**record, "revision": revision}, ensure_ascii=False) for record in records
        ]
        (self.exchange / "braille.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _observed(body: dict[str, Any]) -> dict[str, Any] | None:
    observed = body.get("observed")
    if not isinstance(observed, dict):
        return None
    return {"text": observed.get("text")}


def worktree_add(repo: Path, path: Path, base: str) -> None:
    """Чистый worktree от закреплённой базы; единственный git-помощник создания."""
    _git_text(repo, "worktree", "add", "--detach", str(path), base)


def _python_for(worktree: Path) -> str:
    """Python дерева: окружение worktree, иначе тот, что запустил контроллер."""
    candidate = worktree / ".venv" / "bin" / "python3"
    return str(candidate) if candidate.is_file() else sys.executable


def changed_paths(worktree: Path) -> set[str]:
    return _porcelain_paths(
        _git_text(worktree, "status", "--porcelain", "--untracked-files=all", "-z"),
    )


def head(worktree: Path) -> str:
    return _git_text(worktree, "rev-parse", "HEAD").strip()


def parse_junit(path: Path) -> list:
    try:
        root = ET.parse(path).getroot()  # noqa: S314 — отчёт своего прогона pytest
    except (OSError, ET.ParseError) as error:
        raise EvidenceError(f"{path}: JUnit не разобран ({error})") from error
    return list(root.findall(".//testcase"))


def assert_red(cases: list, node: RedNode) -> None:
    case = _single_case(cases, node)
    failure = case.find("failure")
    if failure is None:
        raise EvidenceError("красный JUnit: testcase без failure")
    text = (failure.get("message") or "") + (failure.text or "")
    if node.marker not in text:
        raise EvidenceError(f"красный JUnit: в failure нет маркера {node.marker!r}")


def assert_green(cases: list, node: RedNode) -> None:
    case = _single_case(cases, node)
    if case.find("failure") is not None:
        raise EvidenceError("зелёный JUnit: testcase с failure")


def _single_case(cases: list, node: RedNode) -> Any:
    name = node.node_id.rsplit("::", 1)[-1]
    if len(cases) != 1:
        raise EvidenceError(f"JUnit: ожидался один testcase, их {len(cases)}")
    case = cases[0]
    if not str(case.get("name", "")).endswith(name):
        raise EvidenceError(f"JUnit: чужой testcase {case.get('name')!r}")
    if case.find("error") is not None or case.find("skipped") is not None:
        raise EvidenceError("JUnit: error или skipped вместо ожидаемого исхода")
    return case


def _porcelain_paths(text: str) -> set[str]:
    paths: set[str] = set()
    skip_next = False
    for entry in text.split("\0"):
        if skip_next:
            skip_next = False
            continue
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths.add(path)
        skip_next = "R" in status or "C" in status
    return paths


def _tracked(worktree: Path, relative: str) -> bool:
    done = subprocess.run(  # noqa: S603, S607
        ["git", "-C", str(worktree), "ls-files", "--error-unmatch", "--", relative],  # noqa: S607
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        env=git_env(),
    )
    return done.returncode == 0


def _git_text(worktree: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603, S607
        ["git", "-C", str(worktree), *args], capture_output=True, text=True,  # noqa: S607
        encoding="utf-8", errors="replace", check=False, env=git_env(),
    )
    if done.returncode != 0:
        raise EvidenceError(f"git {args[0]}: {done.stderr.strip()}")
    return done.stdout


def _git_bytes(worktree: Path, *args: str) -> bytes:
    done = subprocess.run(  # noqa: S603, S607
        ["git", "-C", str(worktree), *args], capture_output=True, check=False,  # noqa: S607
        env=git_env(),
    )
    if done.returncode != 0:
        raise EvidenceError(f"git {args[0]}: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def run_command(argv: Sequence[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — argv собран контроллером, не моделью
        list(argv), cwd=cwd, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace", env=git_env(), check=False,
    )


def _write_log(path: Path, argv: Sequence[str], cwd: Path, revision: str,
               done: subprocess.CompletedProcess[str]) -> None:
    header = [
        "command: " + " ".join(argv),
        f"cwd: {cwd}",
        f"tree: {revision}",
        f"exit code: {done.returncode}",
        "---",
    ]
    path.write_text("\n".join([*header, done.stdout, done.stderr]), encoding="utf-8")


def _diff_groups(plan: dict[str, Any]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {"implementation": [], "test": [], "other": []}
    for item in plan["paths"]:
        purpose = item["purpose"]
        key = "implementation" if purpose == "implementation" else (
            "test" if purpose == "test" else "other"
        )
        path = normalize_path(item["path"])
        if path not in groups[key]:
            groups[key].append(path)
    return groups


def _skip_note(path: str) -> str:
    return f"[пропуск {path}: не вошёл в лимит Jev; полный -U0 diff в diff.patch]\n"


def _shorten_note(path: str) -> str:
    return f"[сокращено {path}: полный -U0 diff в diff.patch]\n"


def _fit_lines(lines: list[str], budget: int) -> list[str]:
    included: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget:
            included.append(f"... ещё {len(lines) - len(included)}")
            break
        included.append(line)
        used += len(line) + 1
    return included


def _load_steps(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    require(isinstance(value, list) and all(isinstance(item, dict) for item in value),
            f"{path}: нужен JSON-список шагов Braille")
    return value


def _json_or_empty(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}

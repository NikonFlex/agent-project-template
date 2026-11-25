"""Один `state.json` прогона: атомарная запись под блокировкой и восстановление pending.

Поля — то, что нужно для продолжения: закреплённая база, path worktree, окружение, ID ролей,
стадия, хеши артефактов и одна незавершённая операция (`pending`).
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class State:
    number: str
    project: str
    parent: str
    token: str
    request_file: str | None = None
    base: str | None = None
    worktree: str | None = None
    stage: str = "worktree"
    host: str | None = None
    environment_id: str | None = None
    roles: dict[str, str] = field(default_factory=dict)
    pending: dict[str, Any] | None = None
    plan_sha256: str | None = None
    plan_tree: str | None = None
    plan_changed: list[str] = field(default_factory=list)
    gate: dict[str, Any] | None = None
    test_sha256: str | None = None
    checks_revision: str | None = None
    accepted_revision: str | None = None
    receipts: dict[str, str] = field(default_factory=dict)
    braille: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "requestFile": self.request_file,
            "project": self.project,
            "parentId": self.parent,
            "token": self.token,
            "base": self.base,
            "worktree": self.worktree,
            "stage": self.stage,
            "host": self.host,
            "environmentId": self.environment_id,
            "roles": self.roles,
            "pending": self.pending,
            "planSha256": self.plan_sha256,
            "planTree": self.plan_tree,
            "planChanged": self.plan_changed,
            "gate": self.gate,
            "testSha256": self.test_sha256,
            "checksRevision": self.checks_revision,
            "acceptedRevision": self.accepted_revision,
            "receipts": self.receipts,
            "braille": self.braille,
            "warnings": self.warnings,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> State:
        return cls(
            number=str(data["number"]),
            project=str(data["project"]),
            parent=str(data["parentId"]),
            token=str(data["token"]),
            request_file=data.get("requestFile"),
            base=data.get("base"),
            worktree=data.get("worktree"),
            stage=str(data.get("stage", "worktree")),
            host=data.get("host"),
            environment_id=data.get("environmentId"),
            roles=dict(data.get("roles") or {}),
            pending=data.get("pending"),
            plan_sha256=data.get("planSha256"),
            plan_tree=data.get("planTree"),
            plan_changed=list(data.get("planChanged") or []),
            gate=data.get("gate"),
            test_sha256=data.get("testSha256"),
            checks_revision=data.get("checksRevision"),
            accepted_revision=data.get("acceptedRevision"),
            receipts=dict(data.get("receipts") or {}),
            braille=data.get("braille"),
            warnings=list(data.get("warnings") or []),
            error=data.get("error"),
        )


class StateStore:
    """Каталог прогона в дереве контроллера: state.json, state.lock, журнал команд."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "state.json"
        self.lock_path = directory / "state.lock"

    def load(self) -> State | None:
        if not self.path.is_file():
            return None
        return State.from_dict(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, state: State) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        os.replace(temporary, self.path)

    @contextmanager
    def locked(self):
        """Один прогон на каталог: параллельный запуск ждёт, затем видит готовое состояние."""
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

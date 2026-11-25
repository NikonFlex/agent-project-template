"""Роли, стадии и условия ревью плана Рундо."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

PROJECT_ENV = "BB_PROJECT_ID"
PARENT_ENV = "BB_THREAD_ID"
JEVSTICE_ENV = "RUNDO_JEVSTICE"
PIPELINE_DIR = Path("tmp/rundo")
GATE_PATH_THRESHOLD = 5
CONTRACTS = frozenset({"known", "new", "unknown"})
GEMINI_CONTRACTS = frozenset({"new", "unknown"})
PURPOSES = frozenset({"spec", "task", "test", "implementation"})
CHECK_COMMAND = ("sh", "tools/check.sh", "--quick")
BRAILLE_BASE_ENV = "RUNDO_BRAILLE_BASE"
BRAILLE_STEPS_ENV = "RUNDO_BRAILLE_STEPS"
BRAILLE_VALUES_ENV = "RUNDO_BRAILLE_VALUES"
TEST_ID = re.compile(r"^R[1-9]\d?$")
MAX_EVIDENCE = 12_000


class Role(StrEnum):
    PLAN = "plan"
    GEMINI = "gemini"
    RED = "red"
    IMPLEMENTATION = "implementation"
    DRAFT = "draft"


class Stage(StrEnum):
    WORKTREE = "worktree"
    PLAN = "plan"
    GEMINI = "gemini"
    RED = "red"
    GREEN = "green"
    ACCEPTANCE = "acceptance"
    DRAFT = "draft"
    DONE = "done"


@dataclass(frozen=True)
class Actor:
    role: Role
    provider: str
    model: str


ACTORS = {
    Role.PLAN: Actor(Role.PLAN, "codex", "gpt-6.2-sol"),
    Role.GEMINI: Actor(Role.GEMINI, "acp-antigravity", "gemini-4-neon"),
    Role.RED: Actor(Role.RED, "acp-opencode", "deepseek/deepseek-flash"),
    Role.IMPLEMENTATION: Actor(
        Role.IMPLEMENTATION, "acp-opencode", "deepseek/deepseek-flash",
    ),
    Role.DRAFT: Actor(Role.DRAFT, "acp-opencode", "deepseek/deepseek-flash"),
}


def normalize_path(raw: object) -> str:
    """Относительный путь внутри дерева задачи; `../` и абсолютные — отказ."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("путь пуст")
    value = posixpath.normpath(raw.strip().replace("\\", "/"))
    if value == ".." or value.startswith(("/", "../", "~")):
        raise ValueError(f"путь вне дерева задачи: {raw!r}")
    return value

"""Настройки проекта, которые читают несколько скриптов: префикс и папка задач."""

from __future__ import annotations

import json
from pathlib import Path


def backlog_config(root: Path) -> dict:
    """Префикс и папка задач — из .backlog.json, как у tools/backlog.py."""
    config = root / ".backlog.json"
    cfg = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    return {"prefix": cfg.get("prefix"), "tasks": root / cfg.get("tasks_dir", "docs/tasks")}


def git_dir(root: Path) -> Path:
    """Git-каталог клона; в worktree `.git` — файл со ссылкой на настоящий каталог."""
    git = root / ".git"
    if git.is_file():
        git = Path(git.read_text(encoding="utf-8").split(":", 1)[1].strip())
    return git


def ticket_number(root: Path) -> int | None:
    """Задача клона из `git ticket` (tools/commit_check.py) или None."""
    ticket = git_dir(root) / "ticket"
    return int(ticket.read_text().strip().split("#")[-1]) if ticket.exists() else None

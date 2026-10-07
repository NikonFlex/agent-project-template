"""Настройки проекта, которые читают несколько скриптов: префикс и папка задач."""

from __future__ import annotations

import json
from pathlib import Path


def backlog_config(root: Path) -> dict:
    """Префикс и папка задач — из .backlog.json, как у tools/backlog.py."""
    config = root / ".backlog.json"
    cfg = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    return {"prefix": cfg.get("prefix"), "tasks": root / cfg.get("tasks_dir", "docs/tasks")}

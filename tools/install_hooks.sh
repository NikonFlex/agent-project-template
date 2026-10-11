#!/bin/sh
# Разложить git-хуки и команду `git ticket` в этом клоне. Повторный запуск ничего не ломает.
# Запускает хук SessionStart Claude Code; Codex, Cursor и человек — один раз после клона.
cd "$(dirname "$0")/.." || exit 1
git config core.hooksPath templates/githooks
git config alias.ticket '!python3 "$(git rev-parse --show-toplevel)/tools/commit_check.py" ticket'
echo "хуки: templates/githooks (pre-commit, commit-msg); git ticket <номер> — задача клона"

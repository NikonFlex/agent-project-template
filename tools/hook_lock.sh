#!/bin/sh
# Общая очередь тяжёлых хуков: на машине идёт один за раз (flock на файле в общем git-каталоге
# всех worktree репозитория). Ждущий пишет одну строку. Нет flock — команда идёт без замка.
# Использование: sh tools/hook_lock.sh <команда…>
if command -v flock >/dev/null 2>&1; then
    lock="${HOOK_LOCK_FILE:-$(git rev-parse --git-common-dir)/hook.lock}"
    exec 9>"$lock"
    flock -n 9 || { echo "жду очередь хуков…" >&2; flock 9; }
fi
"$@"

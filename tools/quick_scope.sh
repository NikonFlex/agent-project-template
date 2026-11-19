# Что гонять в хуке перед коммитом (`check.sh --quick`). Подключается через `.`.
#
# База testmon (.testmondata) — своя у каждого рабочего дерева: новое дерево без неё гоняет все
# быстрые тесты (~10 мин; 09.11 шесть новых деревьев агентов дали очередь хуков почти на час).
# seed_testmon берёт самую свежую базу среди деревьев клона: testmon сам перегонит тесты, чей код
# с тех пор поменялся, — отбор остаётся верным (замер 09.11: копия базы — 15 с вместо 10 мин).
seed_testmon() {
    [ -e .testmondata ] && return 0
    newest=$(git worktree list --porcelain | sed -n 's/^worktree //p' | while read -r dir; do
        [ -f "$dir/.testmondata" ] && echo "$dir/.testmondata"
    done | xargs -r ls -t 2>/dev/null | head -n 1)
    [ -n "$newest" ] && cp "$newest" .testmondata
    return 0
}

# Коммит, где все файлы — .md: тестам нечего проверять, pytest не гоняется (ruff, gitleaks и
# проверки задач остаются; полный набор — в CI).
docs_only() {
    files=$(git -c core.quotePath=false diff --cached --name-only)
    [ -n "$files" ] && ! printf '%s\n' "$files" | grep -qv '\.md$'
}

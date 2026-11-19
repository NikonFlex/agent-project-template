#!/bin/sh
# Все проверки проекта одним списком. Запускают CI и хук перед `git commit`
# (templates/githooks/pre-commit) — список живёт в одном месте. Другой стек — /bootstrap
# меняет здесь линтер и тесты.
#   sh tools/check.sh           полный прогон: все тесты, и долгие (post-merge.yml после слияния)
#   sh tools/check.sh --fast    CI на PR (`fast`): все тесты без долгих (метка slow, pytest.ini)
#   sh tools/check.sh --quick   хук перед коммитом: из быстрых тестов только те, что затрагивает
#                               изменённый код (pytest-testmon, база — .testmondata)
# Код 0 — всё прошло, 1 — что-то упало; у упавших печатается хвост вывода.
cd "$(dirname "$0")/.." || exit 1
failed=0
quick=""
fast=""
[ "${1:-}" = "--quick" ] && quick=1
[ "${1:-}" = "--fast" ] && fast=1
. tools/quick_scope.sh

run() {
    name=$1
    shift
    if out=$("$@" 2>&1); then
        echo "ok    $name"
    else
        echo "FAIL  $name"
        printf '%s\n' "$out" | tail -n 30 | sed 's/^/      /'
        failed=1
    fi
}

# Код 5 у pytest — «тестов не найдено»: пока их нет (или testmon не нашёл затронутых), это
# не ошибка. `python3 -m pytest`, а не `pytest`: в worktree команда `pytest` берёт
# editable-пакет из другой папки. testmon следит за кодом на Python, а не за конфигами и
# данными, — их ловит полный прогон в CI. `--testmon-forceselect`: с `-m` testmon сам отключает
# отбор и гоняет все быстрые тесты — минуты на коммит вместо секунд.
pytest_or_none() {
    if [ -n "$quick" ]; then
        docs_only && return 0    # одни .md — тестам нечего проверять (tools/quick_scope.sh)
        seed_testmon             # новое дерево — база testmon соседнего дерева, а не все тесты
        python3 -m pytest -q --testmon --testmon-forceselect -m "not slow"
    elif [ -n "$fast" ]; then
        python3 -m pytest -q -m "not slow"
    else
        python3 -m pytest -q
    fi
    code=$?
    [ "$code" -eq 0 ] || [ "$code" -eq 5 ]
}

# Строгий валидатор формы спек (openspec/README.md). Версия закреплена: обновлять осознанно,
# прогнав образец docs/examples/project/. Корень OpenSpec ищется от папки запуска.
openspec_validate() {
    (cd "$1" && OPENSPEC_TELEMETRY=0 npx -y @fission-ai/openspec@1.14.1 \
        validate --specs --strict --no-interactive)
}

run "ruff" ruff check .
run "pytest" pytest_or_none
run "backlog.py --selftest" python3 tools/backlog.py --selftest
run "secret_scan.py --selftest" python3 tools/secret_scan.py --selftest
run "changelog.py --selftest" python3 tools/changelog.py --selftest
run "spec_check.py --selftest" python3 tools/spec_check.py --selftest
run "claude_hooks.py --selftest" python3 tools/claude_hooks.py --selftest
run "fpsr.py --selftest" python3 tools/fpsr.py --selftest
run "context_check.py --selftest" python3 tools/context_check.py --selftest
run "pr_link.py --selftest" python3 tools/pr_link.py --selftest
run "post_merge.py --selftest" python3 tools/post_merge.py --selftest
run "closing_words.py --selftest" python3 tools/closing_words.py --selftest
run "commit_check.py --selftest" python3 tools/commit_check.py --selftest
run "guard.py --selftest" python3 .agents/guards/guard.py --selftest
run "agent_rules.py --selftest" python3 tools/agent_rules.py --selftest
run "agent_rules.py" python3 tools/agent_rules.py
run "context_budget.py --selftest" python3 tools/context_budget.py --selftest
run "context_budget.py" python3 tools/context_budget.py
run "spec_check.py" python3 tools/spec_check.py
run "spec_check.py: docs/examples/project" python3 tools/spec_check.py docs/examples/project
run "source_check.py --selftest" python3 tools/source_check.py --selftest
run "source_check.py" python3 tools/source_check.py
run "source_check.py: docs/examples/project" python3 tools/source_check.py docs/examples/project
run "paths_check.py --selftest" python3 tools/paths_check.py --selftest
run "paths_check.py" python3 tools/paths_check.py
run "paths_check.py: docs/examples/project" python3 tools/paths_check.py docs/examples/project
run "task_links.py --selftest" python3 tools/task_links.py --selftest
run "task_links.py" python3 tools/task_links.py
run "task_links.py: docs/examples/project" python3 tools/task_links.py docs/examples/project
run "scenario.py --selftest" python3 tools/scenario.py --selftest
run "scenario.py --control" python3 tools/scenario.py --control
run "req_status.py --selftest" python3 tools/req_status.py --selftest
run "req_status.py" python3 tools/req_status.py
run "req_status.py: docs/examples/project" python3 tools/req_status.py docs/examples/project
run "openspec validate --strict" openspec_validate .
run "openspec validate --strict: docs/examples/project" openspec_validate docs/examples/project
run "context_check.py" python3 tools/context_check.py
run "jscpd" npx -y jscpd@5.3.3 -c .jscpd.json .
exit $failed

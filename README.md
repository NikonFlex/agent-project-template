# Шаблон проекта для работы с агентами: Claude Code, Codex, Cursor

Каркас репозитория, в котором инженер и агенты (Claude Code, Codex, Cursor) работают по одному порядку:
требования — в спеках, работа — по задачам, находки записываются сразу, проверки — в CI. Стек по умолчанию — Python (ruff, pytest); другой язык
подставляется при первом заходе.

## Начать новый проект

```
gh repo create <имя> --private --template NikonFlex/agent-project-template --clone
cd <имя>
sh tools/install_hooks.sh   # git-хуки и `git ticket`; Claude Code делает это сам при старте
claude            # и в чате: /bootstrap
```

`/bootstrap` расспросит, что строим, заполнит места `{{…}}`, уберёт задачи самого шаблона,
подставит линтер и тесты стека и заведёт первую задачу. Дальше в каждом новом чате — `/catchup`.

Нужны: `git`, `gh` (вошедший в аккаунт: `gh auth login`), `python3` с `pyyaml`
(`pip install -r tools/requirements.txt`) — на нём написаны скрипты процесса, даже если
проект на другом языке; `ruff`, `pytest` и `pytest-testmon` для Python-стека; `node` — для проверки копипасты и валидатора спек OpenSpec.

## Что внутри

| Путь | Зачем |
|---|---|
| `AGENTS.md` | общие правила для Claude Code, Codex и Cursor — то, что нужно до первого действия |
| `CLAUDE.md` | импорт `AGENTS.md` и только своё для Claude Code: навыки, хуки, субагенты, контекст |
| `docs/agents/` | правила по темам (код, спеки, задачи); копии для сред — `.claude/rules/`, `.cursor/rules/` (`tools/agent_rules.py`) |
| `.agents/guards/` | сторожа: команду агента проверяют до выполнения; входы — `.claude/settings.json`, `.codex/hooks.json` (+ `.codex/rules/`), `.cursor/hooks.json` |
| `tools/harness.py` | состояние обвязки и все самопроверки одной командой, без сети |
| `tools/context_budget.py` | бюджет постоянного контекста: рост `AGENTS.md`/`CLAUDE.md` и повторы между ними |
| `.claude/settings.json` | `.env` не читается; хуки: сторож, напоминание о задаче, контекст, git-хуки при старте |
| `.claude/hooks/task-reminder.sh` | перед каждым сообщением напоминает: сначала задача |
| `.claude/skills/` | `/bootstrap` — первый заход, `/catchup` — где остановились, `/task` — задачи на диске, `/journal` — журнал и уроки, `/contradictions` — противоречия и дыры в контексте, `/fpsr` — доля задач с первого промпта и что мешает, `/accept` — приёмка задач на рабочем контуре |
| `docs/shared/SDD.md` | порядок «входящие → спеки OpenSpec → код» |
| `docs/architecture/` | проверенные факты о системе, с полем «когда и чем проверено»; гипотезы — в `tmp/` (не в git) |
| `docs/WHY.md` | почему правила и задачи устроены так |
| `docs/examples/tasks/` | устройство файла задачи и образцы: в работе, закрытая, черновик |
| `docs/examples/project/` | образец проекта: источник, спеки, карта и реестр, которые проходят все приборы |
| `docs/incoming/` | присланные документы как есть + `index.yaml` |
| `openspec/` | требования в формате OpenSpec: `README.md` — формат, источники, глоссарий, решения, вопросы; `specs/<имя>/spec.md` — спеки |
| `docs/tasks/` | копия задач GitHub; низ файла — заметки агента между сессиями |
| `docs/journal/` | журнал эпизодов и уроки (`INSIGHTS.md` уже засеян) |
| `docs/decisions.md`, `docs/dev-plan.md` | почему так выбрали; как строим |
| `tools/backlog.py` | GitHub Issues ↔ `docs/tasks/`: sync, review, resume, new, comment |
| `tools/spec_check.py` | проверки входящих и спек сверх валидатора OpenSpec, с самопроверкой |
| `tools/source_check.py` | сверка требований с источником в обе стороны: строки источника, карта, реестр, решения |
| `tools/paths_check.py`, `tools/task_links.py` | пути ↔ требования; задачи ↔ требования |
| `tools/scenario.py`, `tests/scenarios/`, `tools/receipt.json` | раннер сценариев проверки: шаги через API, ожидания, контрольный сценарий, квитанция, запись прогона |
| `tools/req_status.py`, `docs/shared/product/` | таблица требований со статусами; перенос статусов из записей прогона |
| `tools/commit_check.py`, `templates/githooks/`, `tools/install_hooks.sh` | номер задачи в каждом коммите: `git ticket`, хук `commit-msg`, проверка ветки в CI; `pre-commit` — `check.sh --quick` |
| `cliff.toml`, `tools/changelog.py`, `.github/workflows/release.yml` | номер версии и CHANGELOG из заголовков коммитов (git-cliff); выпуск — по кнопке хозяина |
| `tools/secret_scan.py`, `.gitleaks.toml` | ключи в коммитах: gitleaks закреплённой версии в хуке `pre-commit` и по коммитам PR в CI; исключения — с причиной |
| `tools/check.sh` | все проверки одним списком: запускают CI и хук перед `git commit` |
| `tools/claude_hooks.py` | хуки Claude Code: занятость контекста, задача после сжатия, коммит ли это |
| `tools/context_check.py` | гейт: пути в инструкциях существуют, в коде нет оговорок (`context_check.json`) |
| `tools/fpsr.py` | FPSR — доля задач, закрытых с первого промпта, и PR на задачу |
| `tools/closing_words.py` | close/fix/resolve рядом с номером задачи не пропускают хук и CI: закрывает хозяин |
| `tools/project_config.py`, `tools/script_common.py` | общее для скриптов: настройки задач, точка входа, отчёт |
| `.claude/context.json` | размер окна и пороги, на которых Claude предлагает `/compact` или новый чат |
| `.github/workflows/check.yml` | CI: ставит зависимости и запускает `tools/check.sh` |
| `ruff.toml` | настройки линтера; `tools/backlog.py` исключён — готовый инструмент |

## Как идёт работа

```
PRD или ΔP продакта → docs/incoming/ (как есть) + «Источники и старшинство»
  → требования в openspec/specs/ со строкой источника; сверка в обе стороны (source_check)
  → пути (openspec/paths/) → задача на требования → сценарий проверки до кода
  → своё дерево + git ticket → код → PR «Implements» с automerge → CI сливает
  → выкладка → прогон сценариев на контуре → таблица требований (req_status)
  → продакт проверяет путь → журнал решений → следующий ΔP
находки по ходу → низ файла задачи; общее → docs/*.md; выбор → docs/decisions.md
```

Порядок целиком — `docs/shared/SDD.md`.

## Улучшать шаблон

Правило, которое прижилось в проекте, переносится сюда отдельной задачей в этом
репозитории. В проектах, уже созданных из шаблона, его правят руками.

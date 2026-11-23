# Шаблон проекта для работы с агентами: Claude Code, Codex, Cursor

Каркас репозитория, в котором инженер и агенты (Claude Code, Codex, Cursor) работают по одному порядку:
требования — в спеках, работа — по задачам, находки записываются сразу, проверки — в CI. Стек по умолчанию — Python (ruff, pytest); другой язык
подставляется при первом заходе.

## Текущий рабочий сетап в BB

Помимо базового шаблона, в репозиторий добавлен текущий рабочий сетап для разработки с агентами в BB:

- [Рабочий процесс в BB](docs/workflow.md) — роли (`Sol`, `Neon`, `Flash`, `Jev`), формат короткого плана, передача между тредами на одном worktree, приёмка по квитанциям и ночной аудит.
- [Плагины BB](docs/plugins.md) — исходники и манифесты `plugins/bb-jev`, `plugins/limits-card`, `plugins/bb-antigravity-usage`, `plugins/glm-idle`.
- Инструменты в `tools/` и конфиги в `config/`:
  - `tools/bb_arena.py`, `config/bb_arena.json`, `config/arena/review-01.patch` — Арена моделей, скоринг скрытых тестов и цепочка `Jev v2`.
  - `tools/bb_tree.py`, `config/bb_models.json` — дерево тредов и метрики токенов/цены.
  - `tools/nightly_audit.py`, `tools/nightly_automation.py`, `tools/nightly-requirements.txt` — ночной аудит изменений приложения и тихий пропуск.
  - `tools/demo.py` — автономная проверка скорёра на синтетической задаче `quote-rounding`.

Быстрая проверка сетапа без сети и BB:

```sh
python3 tools/demo.py
python3 tools/bb_arena.py --plan --suite smart --variant jev-v2
```

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
| `tools/closing_words.py` | close/fix/resolve у номера задачи в коммите и заголовке PR не пропускают хук и CI |
| `tools/pr_link.py` | тело PR — строкой `Closes #N` (слияние закроет задачу) или `Part of #N`; сторож перед `gh pr create` и CI `pr-link` |
| `tools/post_merge.py`, `.github/workflows/post-merge.yml` | полный набор тестов после слияния; новый красный переоткрывает задачи из `Closes` |
| `tools/quick_scope.sh`, `tools/hook_lock.sh`, `pytest.ini` | хук перед коммитом: одни `.md` — без тестов, база testmon соседнего дерева, хуки деревьев по одному; метка `slow` — только после слияния |
| `docs/clean-contour.md`, `.claude/skills/clean-contour-check/` | чистый контур: правила кода сущностей с жизненным циклом и проверка по диффу перед push (Apache 2.0, `docs/clean-contour.LICENSE`) |
| `tools/project_config.py`, `tools/script_common.py` | общее для скриптов: настройки задач, точка входа, отчёт |
| `.claude/context.json` | размер окна и пороги, на которых Claude предлагает `/compact` или новый чат |
| `.github/workflows/check.yml` | CI на PR: `fast` (`check.sh --fast`), `pr-link`, `automerge` — сливает, закрывает задачи из `Closes`, запускает `post-merge` |
| `ruff.toml` | настройки линтера; `tools/backlog.py` исключён — готовый инструмент |

## Модули по желанию

`/bootstrap` спрашивает, какие включить; выключенный модуль удаляется вместе со своими файлами
и строками в `tools/check.sh` и `.claude/settings.json`.

### Telegram

Бот процесса инженеров, не продукта. Группа хозяина с темами «Входящие», «Разбор»,
«Вопросы», «Задачи и PR», «Результаты», «Claude». События репозитория идут в «Задачи и PR»:
новая и закрытая задача (со сводкой), PR открыт и слит, красный полный набор после слияния
(Action [tg-notify](.github/workflows/tg-notify.yml), текст — [tools/tg_notify.py](tools/tg_notify.py);
PR и задачи, которые сливает и закрывает CI, шлёт job `automerge` — токен CI других workflow
не запускает). Claude закончил ход дольше `min_minutes` из [.claude/ping.json](.claude/ping.json)
или ждёт разрешения — пинг в «Claude» ([tools/tg_ping.py](tools/tg_ping.py), хуки
`.claude/settings.json`). Отправка — [tools/tg_send.py](tools/tg_send.py). Без токена в `.env`
скрипты молчат и выходят с кодом 0.

Настройка, один раз:

1. @BotFather → `/newbot`; токен — в `.env` как `TELEGRAM_BOT_TOKEN`.
2. Группа с включёнными темами, названия — как выше; бот — админ группы.
3. Написать что-нибудь в каждую тему и выполнить `python3 tools/tg_send.py discover --save` —
   id группы, хозяина и номера тем запишутся в `.env`.
4. Проба: `python3 tools/tg_send.py send --topic tasks --file <текст>` — сообщение в теме;
   `--thread 999999` — отказ 400.
5. Секреты репозитория для Action: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
   `TELEGRAM_THREAD_TASKS`, для сбора ещё `TELEGRAM_OWNER_ID`, `TELEGRAM_THREAD_INBOX`
   (`gh secret set <имя> --repo <владелец>/<репо>`, значение — с клавиатуры).

Переписку с продактом или инженерами хозяин пересылает пачкой в тему «Входящие». Бот забирает
новое в буфер — ветку `tg-inbox`: раз в 4 часа и по запросу `/inbox` (Action
[tg-pull](.github/workflows/tg-pull.yml), скрипт [tools/tg_inbox.py](tools/tg_inbox.py)); в
теме он отвечает «принял N, новых M, повторов K». `/inbox` собирает новое сейчас, выгружает
в `docs/incoming/` ([tools/tg_export.py](tools/tg_export.py); вложения — в
`materials/telegram/`, вне git), разбирает по спекам и пишет итог в «Разбор». Вопросы
продакту или инженерам — `/questions`, только по просьбе хозяина: в «Вопросы» сообщение
уходит по «отправь», адресату его пересылает хозяин. Файлы и сводки — в «Результаты»:
`python3 tools/tg_send.py send-file --topic results --file <файл>`.

### Экраны для агентов (braille)

Объект `window.__agent` в странице: что на экране, чем она занята, что человек не увидит, какие
запросы упали — JSON вместо снимков. Включается переменной `AGENT_API=1` на стенде; у
пользователей и на проде объекта нет. Агент идёт по экрану шагами: `python3 -m braille start |
step | look | close` (Playwright, только `localhost` и разрешённые хосты). Устройство —
[braille/README.md](braille/README.md), правила для агентов —
[docs/agents/screens.md](docs/agents/screens.md), настройки — `config/braille.json`. Своя копия
идеи [agent-braille](https://github.com/blurman-ai/agent-braille) (Apache-2.0): что взято и
изменено — `braille/NOTICE`.

## Как идёт работа

```
PRD или ΔP продакта → docs/incoming/ (как есть) + «Источники и старшинство»
  → требования в openspec/specs/ со строкой источника; сверка в обе стороны (source_check)
  → пути (openspec/paths/) → задача на требования → сценарий проверки до кода
  → своё дерево + git ticket → код → чистый контур → PR «Closes» с automerge → CI сливает
  → post-merge: полный набор, красный переоткрывает задачу
  → выкладка → прогон сценариев на контуре → таблица требований (req_status)
  → продакт проверяет путь → журнал решений → следующий ΔP
находки по ходу → низ файла задачи; общее → docs/*.md; выбор → docs/decisions.md
```

Порядок целиком — `docs/shared/SDD.md`.

## Улучшать шаблон

Правило, которое прижилось в проекте, переносится сюда отдельной задачей в этом
репозитории. В проектах, уже созданных из шаблона, его правят руками.

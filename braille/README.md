# braille — экраны для агентов

Объект `window.__agent` в странице: что на экране, чем она занята, что человек не увидит, какие
запросы упали — JSON вместо снимков. Своя копия идеи agent-braille под наш стек (Jinja, простой JS,
Python); откуда взято и что изменено — `NOTICE`, лицензия — `LICENSE` (Apache-2.0). Почему так —
`docs/decisions.md`, «Экраны для агентов: своя копия agent-braille, модуль по желанию».

Пользователь объект не видит: без переменной сайт не отдаёт `agent.js` и не ставит его в страницы.

## Как включить

На сервере сайта — `AGENT_API=1` (любое другое значение — выключено). Читается при сборке
приложения, поэтому после смены переменной сайт перезапускают. Включённый сайт отдаёт скрипт по
`/braille/agent.js`, а `base.html` сайта подключает его первым в `<head>`, без `defer`: объект
должен успеть до первого запроса страницы.

Где включено: {{стенд и контуры, где экранные сценарии дают записи прогона}}. Прод — выключено.

Новому сайту — `braille.install(app, templates)` рядом с монтированием `/static` и строка
`{% if agent_api %}<script src="/braille/agent.js"></script>{% endif %}` в `<head>`. Образ
контейнера копирует пакет: `COPY braille braille` в Dockerfile.

## Что отдаёт объект

`window.__agent`: `version`, `screen()`, `busy()`, `find(query)`, `act(testid, action, value)`,
`layout(a, b)`, `visual()`, `errors({clear})`, `registerBusySource(name, fn)`.

`screen()` — как ответ шага в agent-braille. Пустое не выводится, кроме `elements` и `fields`:

```json
{"screen": {"name": "orders", "state": "ready"},
 "url": "http://…/orders",
 "elements": [{"testid": "orders.add", "role": "button", "name": "Добавить"}],
 "fields": [{"field": "Поиск заказа", "role": "textbox"}],
 "labels": [{"label": "Без разметки", "role": "button"}],
 "headings": ["Заказы"], "facts": {"filter": "all"}, "text": "…"}
```

- `screen` — корень `[data-testid^="screen."]` и его `data-state` (`loading`, `ready`, `empty`,
  `error`); корня нет — `null`, остальное всё равно отдаётся.
- `elements` — видимые элементы с `testid` вида `<область>.<действие>`; `disabled`, а у закрытых
  другим элементом — `occludedBy`.
- `fields` — поля по подписи (`label`, `aria-label`); `labels` — кнопки и ссылки без такого
  `testid`, по подписи: на страницах без разметки видно хотя бы их; `options` — пункты
  открытого списка; `headings`; `alerts` — тексты `role="alert"`.
- `overlays` — открытые диалоги и списки поверх экрана; `facts` — `data-fact` и
  `data-fact-value`, то, чего не видно в элементах.
- `text` — видимый текст экрана, до 1500 знаков.

`busy()` — незавершённое по источникам: `fetch`, `xhr`, `navigation` (после `beforeunload`
и отправки формы), `animations` (конечные; бесконечный индикатор не в счёт) и имена из
`registerBusySource`. Пустой объект — страница в покое. Пока страница уходит, `evaluate`
Playwright стоит до новой страницы, поэтому `navigation` видно только изнутри (обработчиком
`beforeunload`).

`visual()` — то, что есть в дереве доступности, но человек не прочтёт: текст цвета фона,
прозрачный, обрезанный, за краем контейнера или экрана, закрытый другим элементом (`by` — каким),
имя в дереве не совпадает с видимым. Элемент — `{kind, text, message}`; пусто — изъянов нет.

`errors()` — ошибки страницы, отклонённые промисы, `console.error` и упавшие запросы
(`status` 400 и выше, `0` — сети нет; `code` — `detail.code` из тела отказа).

## Исполнитель шагов

Агент идёт по экрану шагами, а не снимками: `python3 -m braille` открывает Chromium (Playwright), после каждого шага ждёт покоя страницы и отвечает JSON — что видно,
какие запросы ушли, что страница говорит о себе. Сайт должен быть запущен с `AGENT_API=1`.

```
python3 -m braille start --base http://127.0.0.1:18147 [--session DIR] [--viewport desktop|phone] [--values FILE]
python3 -m braille step '{"goto": "/orders", "after": {"screen": "orders", "state": "ready"}}'
python3 -m braille look      # весь экран без шага, вместе с замечаниями о виде страницы
python3 -m braille close     # пишет steps.json сессии
python3 -m braille selftest  # контрольные случаи без браузера и сети
```

- **Python.** Клиент идёт и под системным `python3`; браузер запускается тем Python, где есть
  playwright: `BRAILLE_PYTHON=<venv>/bin/python`.
- **Окна.** `desktop` — 1440×1000, `phone` — 390×844 (`config/braille.json`, `viewports`).
- **Хосты.** Только `localhost`, `127.0.0.1` и перечисленные в `BRAILLE_ALLOWED_HOSTS` (через
  запятую); любой другой адрес — отказ до запуска браузера, прод не открывается.
- **Сессия.** Каталог (по умолчанию `tmp/braille/session-…`, указатель — `tmp/braille/current`):
  `request.json` → `reply-<id>.json`, `state.json` (`starting`, `ready`, `busy`, `dirty`, `closed`,
  `failed`), `heartbeat` живого исполнителя, `steps.json` прошедших шагов, `out/` со снимками и
  уликами красных шагов, `executor.log`. Простой в полчаса закрывает сессию.
- **Значения.** `--values FILE` — JSON `{"password": "…"}`; в шаге пишут `{password}`, в
  `steps.json` остаётся подстановка, значение туда не попадает и в ответах закрыто.

Язык шагов — как в agent-braille (`NOTICE`); разбор не требует браузера: `braille/steps.py`.

| Нужно | Шаг |
|---|---|
| открыть | `{"goto": "/orders", "after": {"screen": "orders", "state": "ready"}}` |
| нажать | `{"click": {"testid": "orders.order", "item": "ЖБ-000001"}, "after": {"screen": "order"}}`; по подписи — `{"click": "Показать"}` |
| ввести | `{"fill": "Имя", "value": "Иван"}` — поле по подписи или `{"testid": …}`; у `<select>` значение — текст пункта |
| клавиша, файл | `{"press": "Escape"}`, `{"upload": "scan.pdf"}` (каталог — `BRAILLE_FILES_DIR`) |
| ждать | `{"wait": "Заказы"}`, `{"wait": {"testid": "orders.order"}, "within": 30}` |
| проверить | `absent`, `order`, `enabled`, `disabled`, `checked`, `unchecked`, `focused`, `field`+`has`, `filled`, `fact`, `visible`, `hidden`, `requested`+`times`+`method` |
| ещё | `hover`, `repeat`+`until`+`max`, `tab` (`open`, `first`, `second`) |

- **`after` обязателен у действий** (`goto`, `click`, `fill`, `press`, `upload`) в сценарии
  (`scenario_problems`); в сессии его можно не писать — исполнитель запишет увиденное в
  `steps.json` (`"after": "взят из наблюдения"`). Поля: `screen`, `state`, `shown`, `gone`,
  `within` (секунды, по умолчанию 20).
- **Пауз нет.** `{"wait": 2000}` отказ; не перенесены `hold`, `cut`, `advance`, `visibility`,
  `download`, `described`, `same-href`, `active` (причины — `NOT_CARRIED` в `steps.py`).
- Лишний или чужой ключ шага — отказ с причиной, а не молчаливое игнорирование.

### Ответ шага

```json
{"ok": true, "recorded": {…}, "after": "дан", "api": ["POST /login 303", "GET / 200"],
 "sent": [{"call": "POST /login 303", "sent": "name=Иван&password=***", "answer": ""}],
 "notices": ["Неверный пароль"], "contradictions": ["…"], "visual": ["…"], "ms": 1850,
 "observed": {"screen": {"name": "home", "state": "ready"}, "url": "…", "elements": […], "unchanged": 12}}
```

- `ok`; `error` — причина красного шага; `next` — после действия, у которого не наступил `after`,
  принимается только `goto`: страница могла измениться, записанное иначе разойдётся с увиденным.
- `api` — запросы шага: метод, путь и код. На наших страницах HTML собирает сервер, поэтому в
  списке и переходы страницы (`document`), и отправки форм, и fetch/XHR; скрипты, стили и
  картинки не считаются. Пустой список — шаг ничего не запросил. Упавший запрос — код 400+ или
  причина обрыва (`GET /api/down net::ERR_FAILED`).
- `sent` — тело изменений (POST, PUT, PATCH, DELETE) и ответ на них; поля `password`, `token`,
  `secret`, `cookie`, `authorization`, `csrf` и значения из `--values` закрыты `***`.
- `observed` — `screen()` из `window.__agent`; в ответе шага только изменившееся с прошлого
  ответа (`elements`, `fields`, `labels`, а `text`, если тот же), `unchanged` — сколько
  осталось прежним; `look` отдаёт всё. `screen` и `url` есть всегда.
- `notices` — сообщения (`role=alert`, `role=status`, всплывашки), что появились за шаг, даже
  если уже исчезли; они переживают переход страницы.
- `visual` — что в дереве доступности есть, а человек не прочтёт (`visual()`); то же замечание
  на той же странице второй раз не повторяется, `look` покажет всё.
- `contradictions` — где страница расходится с сетью: состояние `ready`, а запрос шага упал;
  `loading`, а страница ничем не занята; `error` без упавшего запроса; `empty`, а факт больше
  нуля; ответ 200 с отказом в теле JSON (`ok: false`, `error`, `status: failed`); один POST
  дважды с тем же телом. Это зацепки, а не приговор.
- `evidence` — у красного шага: причина, упавшие запросы шага, ошибки страницы и консоль,
  экран, чем занята страница, цель шага (роль, имя, закрыта ли) и путь снимка. Снимок и полный
  файл `<номер>-<шаг>.json` лежат в `out/` сессии; снимок обрезан по цели, если она известна.

### Ожидание

После действия исполнитель ждёт покоя: нет документа и запросов в пути, `window.__agent.busy()`
пуст два опроса подряд, экран вышел из `loading`. Пока страница уходит (клик по ссылке, отправка
формы), `evaluate` Playwright стоит до новой страницы, поэтому в этот момент `busy()` не
опрашивается: исполнитель ждёт загрузки. Упавший запрос заканчивает ожидание: когда ничего не
идёт и сеть тиха `BRAILLE_API_FAIL_GRACE_MS` (5000), шаг краснеет и называет запрос. Закрытая
кнопка ждёт `BRAILLE_OCCLUSION_GRACE_MS` (1000) — панель может ещё выезжать — и краснеет с
именем и `testid` того, что её закрывает. Сроки — `config/braille.json`, `timing_ms`.

### Чему верить

`state` и `busy` говорят, что страница закончила; результат доказывает только видимое
(`after.shown`, `after.gone`, шаги-проверки). `fact` и `requested` — отчёт страницы и сети, один
они сценарий не доказывают. `state: error` — собственный отчёт страницы: причину читают в
`api` и `notices`. Красный шаг без `next` ничего не изменил — продолжайте с того же места.

## Проверка

`python -m pytest tests/braille` — `test_enable.py` (включение на маленьком приложении FastAPI)
идёт, когда есть fastapi и jinja2; `test_agent.py` (объект) и `test_runner.py` (исполнитель) — на
страницах-фикстурах в Playwright и пропускаются без пакета или Chromium (в CI браузера нет).
`python3 -m braille selftest` — контрольные случаи без браузера, он в `tools/check.sh`. Правила
для агентов — `docs/agents/screens.md`. Шаг `ui` раннера сценариев (`tools/scenario.py`) в
шаблон не перенесён: язык шагов и проверка сценария (`braille/steps.py`, `scenario_problems`)
готовы, подключение к раннеру — по стеку проекта.

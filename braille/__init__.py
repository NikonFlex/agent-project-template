"""Экраны для агентов: `window.__agent` в странице, включается переменной окружения.

Решение — docs/decisions.md, «Экраны для агентов: своя копия agent-braille, модуль по желанию»;
устройство и ответ `screen()` — braille/README.md. Без `AGENT_API=1` сайт не отдаёт
`agent.js` и не ставит его в страницы: у менеджера и на проде этого объекта нет.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # fastapi нужен только сайтам: `python3 -m braille` идёт и без него
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates

#: переменная окружения, включающая объект (значение "1")
ENV_FLAG = "AGENT_API"
#: адрес скрипта на сайте; шаблоны base.html подключают его без defer: объект нужен до
#: первого запроса страницы, иначе её fetch останется незамеченным
SCRIPT_URL = "/braille/agent.js"
SCRIPT = Path(__file__).resolve().parent / "agent.js"
#: скрипт правят вместе с кодом, поэтому браузер перепроверяет его при каждой загрузке
NO_CACHE = {"Cache-Control": "no-cache"}


def enabled() -> bool:
    return os.environ.get(ENV_FLAG) == "1"


def install(app: FastAPI, templates: Jinja2Templates) -> None:
    """Включено — отдаёт скрипт и ставит `agent_api` в шаблоны; выключено — только `False`.

    Глобал ставится всегда: шаблоны общие на модуль, и приложение, собранное позже с другим
    значением переменной, не должно унаследовать чужое.
    """
    on = enabled()
    templates.env.globals["agent_api"] = on
    if on:
        app.add_api_route(SCRIPT_URL, script, methods=["GET"], include_in_schema=False)


def script():  # без аннотации: FastAPI не должен искать FileResponse в этом модуле
    from fastapi.responses import FileResponse

    return FileResponse(SCRIPT, media_type="text/javascript", headers=NO_CACHE)

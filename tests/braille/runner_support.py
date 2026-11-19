"""Помощники проверок исполнителя шагов (braille/): сессия поверх страницы-фикстуры.

Страницы отдаёт сам Playwright по адресу fixture.test (agent_pages.py), сервера нет. «Медленные»
ответы держит обработчик маршрута: он ждёт внутри вызова, поэтому исполнитель ждёт сеть так же,
как у настоящего сервера.
"""
from __future__ import annotations

import json
from pathlib import Path

from agent_pages import ORIGIN, serve

from braille.browser import Live
from braille.evidence import Masker
from braille.executor import Executor
from braille.session import S, Session, set_state
from braille.settings import Settings, load

#: сроки тестов короче рабочих: упавший запрос и закрытая кнопка должны краснеть за секунды
FAST = {"api_fail_grace": 400, "occlusion_grace": 500, "disabled_grace": 600,
        "screen_ready": 6000, "appear_testid": 2000}
SLOW_ANSWER_MS = 600
SLOW_PAGE_MS = 800


def json_route(body: dict, delay_ms: int = 0):
    """Маршрут с JSON-ответом; задержка стоит внутри обработчика, пока страница ждёт."""
    def answer(route) -> None:
        if delay_ms:
            route.request.frame.page.wait_for_timeout(delay_ms)
        route.fulfill(status=200, json=body)
    return answer


def slow_page(route) -> None:
    route.request.frame.page.wait_for_timeout(SLOW_PAGE_MS)
    serve(route)


class Stepper:
    """Исполнитель на странице теста: `run(шаг)` отвечает как `python3 -m braille step`."""

    def __init__(self, page, out: Path, settings: Settings | None = None) -> None:
        self.settings = settings or load().tuned(ms=FAST)
        self.live = Live(page, ORIGIN, self.settings, Masker(self.settings, ("hunter-22-pass",)))
        self.executor = Executor(self.live, out / "out")
        self.out = out
        self.session = Session(out, {"password": "hunter-22-pass"}, self.executor)
        self.session.state = set_state(out, self.session.state, S.READY)

    def hurry(self, expect: float) -> None:
        """Проверки «что видно» краснеют за `expect` секунд, а не за двадцать."""
        self.settings = self.live.settings = self.settings.tuned(s={"expect": expect})

    def run(self, raw: dict) -> dict:
        return self.session.handle({"step": raw})

    def steps(self) -> list[dict]:
        return json.loads((self.out / "steps.json").read_text(encoding="utf-8"))

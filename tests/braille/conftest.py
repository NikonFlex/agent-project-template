"""Chromium и страница для проверок window.__agent (braille/agent.js).

Браузерные тесты пропускаются, когда нет пакета playwright или Chromium не запускается (так в
CI); тест включения (test_enable.py) браузера не просит и идёт всегда. Страницы отдаёт сам
Playwright по адресу fixture.test, сервера нет; запросы /api/slow держит тест — так «медленный»
ответ не зависит от таймеров.
"""
from __future__ import annotations

import pytest
from agent_pages import ORIGIN, Held, serve


@pytest.fixture(scope="session")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        try:
            found = playwright.chromium.launch()
        except sync_api.Error as error:
            pytest.skip(f"Chromium не запускается: {str(error).splitlines()[0]}")
        yield found
        found.close()


@pytest.fixture
def held(agent_page) -> Held:
    return agent_page.held


@pytest.fixture
def agent_page(browser):
    context = browser.new_context(viewport={"width": 1000, "height": 800})
    found = context.new_page()
    found.held = Held(found)
    context.route(f"{ORIGIN}/**", serve)
    context.route(f"{ORIGIN}/api/slow", lambda route: found.held.arrive(route))
    context.route(f"{ORIGIN}/api/broken", lambda route: route.fulfill(
        status=500, json={"detail": {"code": "stand_down"}}))
    context.route(f"{ORIGIN}/api/down", lambda route: route.abort())
    yield found
    context.close()


@pytest.fixture
def stepper(agent_page, tmp_path):
    """Исполнитель шагов на странице теста; запросы /api/delayed, /api/refused, /api/accepted."""
    from runner_support import SLOW_ANSWER_MS, Stepper, json_route, slow_page

    context = agent_page.context
    context.route(f"{ORIGIN}/api/delayed", json_route({"ok": True}, SLOW_ANSWER_MS))
    context.route(f"{ORIGIN}/api/refused", json_route({"ok": False, "error": "нет остатка"}))
    context.route(f"{ORIGIN}/api/accepted", json_route({"ok": True}))
    context.route(f"{ORIGIN}/steps_next.html", slow_page)
    return Stepper(agent_page, tmp_path)

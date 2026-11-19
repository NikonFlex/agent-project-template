"""Браузер: запуск Chromium, window.__agent из Python, ожидание покоя, наблюдение за страницей.

Перенос lib/browser.mjs agent-braille (см. NOTICE) на Playwright sync API. Логику экрана
(`screen`, `find`, `busy`, `visual`) исполнитель не повторяет: он зовёт `window.__agent`
из braille/agent.js. Требует playwright; разбор шагов и самопроверка его не требуют.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from braille.evidence import Masker
from braille.network import Watcher
from braille.settings import Settings
from braille.steps import StepError
from braille.verdicts import screen_pending, wait_is_over

#: сообщения Playwright о том, что страница ушла посреди вызова: вызов повторяют на новой
NAVIGATED = ("Execution context was destroyed", "navigation", "Cannot find context")
#: сколько спокойных опросов подряд нужно, чтобы считать страницу в покое
IDLE_POLLS = 2
CALL = "([name, args]) => window.__agent[name](...args)"
BODY_TEXT = "document.body ? document.body.innerText.replace(/\\n{2,}/g, '\\n').trim() : ''"

#: ставится до загрузки страницы: сообщения (role=alert/status, всплывашки) исчезают сами,
#: поэтому текст запоминается; sessionStorage переносит записанное через уход страницы
NOTICES_JS = """
(() => {
  const KEY = "__brailleNotices";
  const SELECTOR = "[role=alert], [role=status], [data-toast], [class*='toast' i]";
  const load = () => { try { return JSON.parse(sessionStorage.getItem(KEY) || "[]"); }
    catch { return []; } };
  const remember = (el) => setTimeout(() => {
    const text = (el.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 200);
    const list = load();
    if (!text || list.includes(text)) return;
    list.push(text);
    try { sessionStorage.setItem(KEY, JSON.stringify(list)); } catch {}
  }, 50);
  const added = (change) => change.addedNodes.forEach((node) => {
    if (node.nodeType !== 1) return;
    (node.matches(SELECTOR) ? [node] : [...node.querySelectorAll(SELECTOR)]).forEach(remember);
  });
  new MutationObserver((changes) => changes.forEach(added))
    .observe(document, { childList: true, subtree: true });
})();
"""
TAKE_NOTICES_JS = """() => {
  try {
    const got = JSON.parse(sessionStorage.getItem('__brailleNotices') || '[]');
    sessionStorage.setItem('__brailleNotices', '[]');
    return got;
  } catch { return []; }
}"""


def eval_page(page: Page, script: str, arg: Any = None, default: Any = None) -> Any:
    """page.evaluate, переживающий уход страницы: на новой странице вызов повторяется."""
    for _ in range(3):
        try:
            return page.evaluate(script, arg)
        except PlaywrightError as error:
            if not any(mark in str(error) for mark in NAVIGATED):
                return default
            wait_document(page)
    return default


def wait_document(page: Page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded")
    except PlaywrightError:
        return


class Live:
    """Страница (и вторая вкладка) с подпиской на сеть и вызовами window.__agent."""

    def __init__(self, page: Page, origin: str, settings: Settings, masker: Masker) -> None:
        self.settings = settings
        self.context = page.context
        self.context.add_init_script(NOTICES_JS)
        self.pages = [page]
        self.current = 0
        self.watcher = Watcher(origin, masker)
        self.watcher.watch(page)
        self.origin = origin
        self.target_rect: dict | None = None
        self._closers: list[Callable[[], None]] = []

    @property
    def page(self) -> Page:
        return self.pages[self.current]

    def adopt(self, page: Page) -> None:
        self.watcher.watch(page)
        self.pages.append(page)
        self.current = len(self.pages) - 1

    def own(self, *closers: Callable[[], None]) -> None:
        """Что закрыть вместе с этой страницей: браузер и сам Playwright."""
        self._closers.extend(closers)

    def close(self) -> None:
        for closer in reversed(self._closers):
            closer()

    # ---- window.__agent

    def present(self) -> bool:
        return bool(eval_page(self.page, "typeof window.__agent === 'object' && "
                             "window.__agent !== null", default=False))

    def agent(self, name: str, *args: Any, default: Any = None) -> Any:
        return eval_page(self.page, CALL, [name, list(args)], default)

    def find(self, query: dict) -> list[dict]:
        return self.agent("find", query, default=[]) or []

    def find_shown(self, query: dict) -> list[dict]:
        """Элементы, что человек видит: у скрытых рамка нулевая."""
        return [e for e in self.find(query)
                if not e.get("rect") or (e["rect"]["w"] > 0 and e["rect"]["h"] > 0)]

    def busy(self) -> dict | None:
        """Незавершённое по источникам; None — объекта на странице нет."""
        return self.agent("busy")

    def visual(self) -> list[str]:
        return [item["message"] for item in self.agent("visual", default=[]) or []]

    def screen(self) -> dict | None:
        found = self.agent("screen") or {}
        return found.get("screen")

    def observe(self) -> dict:
        """Экран как отдаёт window.__agent.screen(), плюс список testid для проверок after."""
        seen = self.agent("screen")
        if seen is None:
            text = eval_page(self.page, BODY_TEXT, default="")[: self.settings.limits["text"]]
            return {"screen": None, "url": self.page.url, "agent": False, "text": text,
                    "testids": []}
        seen["testids"] = sorted({e["testid"] for e in seen.get("elements", ())})
        return seen

    def take_notices(self) -> list[str]:
        return eval_page(self.page, TAKE_NOTICES_JS, default=[]) or []

    # ---- ожидание

    def pause(self, name: str = "poll") -> None:
        self.page.wait_for_timeout(self.settings.ms(name))

    def settle(self) -> None:
        """До покоя: нет запросов в пути, `busy()` пуст два опроса подряд; без фиксированных пауз.

        Пока страница уходит (документ в пути), её не опрашивают: evaluate стоит до новой
        страницы. Потом ждём, пока экран выйдет из loading.
        """
        deadline = time.monotonic() + self.settings.ms("screen_ready") / 1000
        calm = 0
        while time.monotonic() < deadline and calm < IDLE_POLLS:
            if self.watcher.in_flight:
                calm = 0
            else:
                busy = self.busy()
                calm = calm + 1 if not busy else 0
            if calm < IDLE_POLLS:
                self.pause()
        self._await_ready(deadline)

    def _await_ready(self, deadline: float) -> None:
        while time.monotonic() < deadline and not self.watcher.in_flight:
            seen = self.agent("screen") or {}
            if not screen_pending(seen) or self.gave_up():
                return
            self.pause()

    def gave_up(self) -> bool:
        """Запрос упал и ничего больше не придёт: сеть тиха весь срок BRAILLE_API_FAIL_GRACE_MS."""
        watcher = self.watcher
        if not watcher.failed or watcher.in_flight:
            return False
        return wait_is_over(len(watcher.failed), len(watcher.in_flight),
                            len(self.busy() or {}), watcher.quiet_for_ms(),
                            self.settings.ms("api_fail_grace"))

    def until(self, check: Callable[[], str | None], within_s: float, patient: bool = True) -> None:
        """Повторяет check (причина или None), пока не сбудется; срок вышел — StepError с причиной.

        Упавший запрос заканчивает ожидание раньше срока и называется в причине.
        """
        deadline = time.monotonic() + within_s
        while True:
            problem = check()
            if problem is None:
                return
            if time.monotonic() >= deadline or (patient and self.gave_up()):
                raise StepError(problem)
            self.pause("after_poll")

    def wait_agent(self, target: str) -> None:
        """После перехода: объект window.__agent появляется вместе с головой страницы."""
        try:
            self.page.wait_for_function("window.__agent", timeout=self.settings.ms("document_load"))
        except PlaywrightError as error:
            raise StepError(
                f"на странице {target} нет window.__agent: сайт запущен без AGENT_API=1, "
                "или страница не загрузилась") from error


def open_live(base: str, viewport: str, settings: Settings, secrets: tuple[str, ...] = (),
              ) -> Live:
    """Chromium без окна на одном из двух окон из config/braille.json."""
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(args=["--no-sandbox"])
    except PlaywrightError as error:
        playwright.stop()
        raise StepError("Chromium не запустился: `playwright install chromium` в окружении "
                        f"исполнителя. {str(error).splitlines()[0]}") from error
    context = browser.new_context(viewport=settings.viewports[viewport])
    live = Live(context.new_page(), base, settings, Masker(settings, secrets))
    live.own(playwright.stop, browser.close)
    return live

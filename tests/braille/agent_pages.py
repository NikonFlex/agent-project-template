"""Помощники проверок window.__agent: фикстуры-страницы по адресу сайта и вызов объекта."""
from __future__ import annotations

import mimetypes
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
ORIGIN = "http://fixture.test"


class Held:
    """Запросы к /api/slow, которые страница отправила, а тест ещё не ответил."""

    def __init__(self, page) -> None:
        self.page = page
        self.routes: list = []

    def arrive(self, route) -> None:
        self.routes.append(route)

    def wait(self, count: int = 1) -> None:
        for _ in range(100):
            if len(self.routes) >= count:
                return
            self.page.wait_for_timeout(50)
        raise AssertionError(f"к /api/slow пришло {len(self.routes)} запросов из {count}")

    def answer(self) -> None:
        for route in self.routes:
            route.fulfill(status=200, body="{}", content_type="application/json")
        self.routes.clear()


def serve(route) -> None:
    """Файл фикстуры или agent.js по адресу сайта; чего нет — 404."""
    import braille  # здесь, а не сверху: без fastapi каталог тестов должен хотя бы собираться

    path = route.request.url.removeprefix(ORIGIN)
    source = braille.SCRIPT if path == braille.SCRIPT_URL else FIXTURES / path.lstrip("/")
    if not source.is_file():
        route.fulfill(status=404, body="нет такого файла")
        return
    kind = mimetypes.guess_type(source.name)[0] or "text/plain"
    route.fulfill(body=source.read_bytes(), content_type=f"{kind}; charset=utf-8")


def open_page(page, name: str) -> None:
    page.goto(f"{ORIGIN}/{name}")


def agent(page, name: str, *args):
    """Вызов window.__agent.<name>(*args) в странице."""
    return page.evaluate("([name, args]) => window.__agent[name](...args)", [name, list(args)])

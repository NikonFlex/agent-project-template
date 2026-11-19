"""Что страница спрашивала у сервера в шаге: запросы, их коды, ошибки страницы.

На наших страницах HTML собирает сервер, поэтому в запросы шага идут и переходы (document),
и отправки форм, и fetch/XHR. Скрипты, стили, картинки и шрифты в список не попадают.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from braille.evidence import Masker
from braille.verdicts import call_label, is_failed

#: виды запросов страницы, что считаются запросами шага
TRACKED = ("document", "fetch", "xhr")
#: отмена самой страницей (уход на другую, прерванный fetch) — не улика
CANCELLED = "net::ERR_ABORTED"
#: «не удалось загрузить ресурс» дублирует строку с адресом из запроса
DUPLICATE_CONSOLE = "Failed to load resource"


def now_ms() -> float:
    return time.monotonic() * 1000


@dataclass
class Call:
    method: str
    path: str
    query: str
    status: int | str
    sent: str
    at: float
    response: object | None = None
    body: str | None = field(default=None, repr=False)

    def read_body(self) -> str:
        """Тело ответа; после ухода страницы его уже не прочесть — тогда пусто."""
        if self.body is None:
            try:
                self.body = self.response.text() if self.response is not None else ""
            except Exception:  # noqa: BLE001 — любой отказ чтения значит «тела нет»
                self.body = ""
        return self.body


class Watcher:
    """Подписка на события страниц: запросы шага, висящие в пути, упавшие, ошибки страницы."""

    def __init__(self, origin: str, masker: Masker) -> None:
        self.origin = origin
        self.masker = masker
        self.calls: list[Call] = []
        self.in_flight: set = set()
        self.failed: dict[str, int | str] = {}
        self.log: list[tuple[str, str]] = []
        self.console: list[str] = []
        self.pageerrors: list[str] = []
        self.last_at = 0.0
        self.step_at = now_ms()

    def watch(self, page) -> None:
        page.on("request", self._request)
        page.on("requestfinished", self._finished)
        page.on("requestfailed", self._failed)
        page.on("response", self._response)
        page.on("console", self._console)
        page.on("pageerror", lambda error: self.pageerrors.append(
            str(getattr(error, "stack", None) or error).replace("\n", " | ")[:300]))

    def start_step(self, fresh_page: bool) -> None:
        """Новый шаг: список запросов и улики пусты; переход на страницу забывает упавшее."""
        self.calls, self.console, self.pageerrors = [], [], []
        if fresh_page:
            self.failed.clear()
        self.step_at = now_ms()

    def quiet_for_ms(self) -> float:
        return now_ms() - max(self.last_at, self.step_at)

    def labels(self) -> list[str]:
        return [call_label(self._shown(call)) for call in self.calls]

    def failed_labels(self) -> list[str]:
        return [call_label(self._shown(call)) for call in self.calls if is_failed(call.status)]

    def bodies(self) -> list[dict]:
        """Запросы шага с телами: по ним ищут отказ при коде 200 и двойную отправку."""
        out = []
        for call in self.calls:
            changing = call.method != "GET"
            out.append({"method": call.method, "path": call.path, "status": call.status,
                        "sent": call.sent, "body": call.read_body() if changing else ""})
        return out

    def sent(self) -> list[dict]:
        """Тело изменений (не GET) и ответ на них; пароли и токены закрыты."""
        return [{"call": f"{c.method} {c.path} {c.status}",
                 "sent": self.masker.body(c.sent), "answer": self.masker.body(c.read_body())}
                for c in self.calls if c.method != "GET"]

    def count(self, suffix: str, method: str | None) -> int:
        return sum(1 for m, path in self.log
                   if path.endswith(suffix) and (method is None or m == method))

    # ---- события

    def _tracked(self, request) -> bool:
        return request.resource_type in TRACKED and request.url.startswith(self.origin)

    def _request(self, request) -> None:
        if self._tracked(request):
            self.in_flight.add(request)
            self.last_at = now_ms()
            self.log.append((request.method, urlsplit(request.url).path))

    def _finished(self, request) -> None:
        self.in_flight.discard(request)
        self.last_at = now_ms()

    def _response(self, response) -> None:
        request = response.request
        if self._tracked(request):
            self._record(request, response.status, response)

    def _failed(self, request) -> None:
        self.in_flight.discard(request)
        reason = request.failure or "failed"
        if self._tracked(request) and reason != CANCELLED:
            self._record(request, reason, None)

    def _record(self, request, status: int | str, response) -> None:
        parts = urlsplit(request.url)
        try:
            sent = request.post_data or ""
        except UnicodeDecodeError:
            sent = ""
        self.calls.append(Call(request.method, parts.path, self.masker.query(parts.query),
                               status, sent, now_ms(), response))
        key = f"{request.method} {parts.path}"
        if is_failed(status):
            self.failed[key] = status
        else:
            self.failed.pop(key, None)
        self.last_at = now_ms()

    def _console(self, message) -> None:
        text = message.text
        if message.type in ("error", "warning") and not text.startswith(DUPLICATE_CONSOLE):
            self.console.append(f"{message.type} {text}"[:300])

    @staticmethod
    def _shown(call: Call) -> dict:
        return {"method": call.method, "path": call.path, "query": call.query,
                "status": call.status}

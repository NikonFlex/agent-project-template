"""Один шаг от начала до улик: действие, запросы шага, противоречия, красный шаг со снимком.

Перенос runStep из lib/executor.mjs agent-braille (см. NOTICE). Ответ собирает session.py.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError

from braille.actions import perform
from braille.browser import Live, eval_page
from braille.evidence import build_evidence, target_line, timeout_reason
from braille.steps import AfterMissed, Kind, Step, StepError, target_form
from braille.verdicts import clip_for, contradictions_of, network_contradictions, step_name

PAGE_FACTS = """() => ({title: document.title, readyState: document.readyState,
  text: document.body ? document.body.innerText : ''})"""
#: строка журнала Playwright, в которой он называет того, кто перехватил нажатие
BLOCKER = "intercepts pointer events"


class Executor:
    def __init__(self, live: Live, out: Path) -> None:
        self.live = live
        self.out = out
        out.mkdir(parents=True, exist_ok=True)

    def observe(self) -> dict:
        return self.live.observe()

    def run_step(self, index: int, step: Step) -> dict:
        """Шаг и всё, что о нём известно: запросы, противоречия, видимое, при красном — улики."""
        live = self.live
        name = step_name(index, step.kind, step.target if step.kind is not Kind.REPEAT
                         else step.until)
        live.watcher.start_step(step.kind is Kind.GOTO)
        live.target_rect = None
        live.take_notices()
        started = time.monotonic()
        error, dirty = self._perform(step)
        evidence = self._evidence(name, step, error) if error else None
        if evidence:
            error = self._reason(error, step, evidence)
        observed = live.observe()
        entry = {"step": name, "path": self._path(), "error": error, "dirty": dirty,
                 "calls": live.watcher.labels(), "notices": live.take_notices(),
                 "observed": observed, "ms": round((time.monotonic() - started) * 1000)}
        entry.update(self.reading(observed, sent=True))
        if evidence:
            entry["evidence"] = evidence
            (self.out / f"{name}.json").write_text(
                json.dumps({k: v for k, v in entry.items() if k != "observed"},
                           ensure_ascii=False, indent=2), encoding="utf-8")
        return entry

    def _perform(self, step: Step) -> tuple[str | None, bool]:
        try:
            perform(self.live, step)
        except AfterMissed as missed:
            return str(missed), True
        except StepError as error:
            return str(error), False
        except PlaywrightError as error:
            return self._playwright_reason(str(error)), False
        return None, False

    @staticmethod
    def _playwright_reason(message: str) -> str:
        """Первая строка ошибки Playwright и, если есть, кто перехватил нажатие."""
        lines = message.splitlines()
        blocker = next((line.strip() for line in lines if BLOCKER in line), None)
        return " — ".join(part for part in (lines[0], blocker) if part)[:400]

    def _reason(self, error: str, step: Step, evidence: dict) -> str:
        what = f"{step.kind} {json.dumps(step.target, ensure_ascii=False)}"
        reason = timeout_reason(error, what, evidence)
        failed = self.live.watcher.failed_labels()
        if failed and not any(call in reason for call in failed):
            reason += " — упали запросы шага: " + ", ".join(failed)
        return reason[:600]

    def _path(self) -> str:
        parts = urlsplit(self.live.page.url)
        return parts.path + (f"?{parts.query}" if parts.query else "")

    def reading(self, observed: dict, sent: bool = False) -> dict:
        """Что страница говорит после шага: противоречия сети и вид, невидимый человеку."""
        live, watcher = self.live, self.live.watcher
        busy = list(live.busy() or {})
        found = contradictions_of(observed, busy, watcher.failed_labels())
        found += network_contradictions(watcher.bodies())
        reading: dict = {"sent": watcher.sent()} if sent else {}
        if found:
            reading["contradictions"] = found
        visual = live.visual()
        if visual:
            reading["visual"] = visual
        return reading

    def _evidence(self, name: str, step: Step, error: str) -> dict:
        live = self.live
        shot = self.out / f"{name}.png"
        self._snapshot(shot)
        facts = eval_page(live.page, PAGE_FACTS, default={}) or {}
        screen = (live.agent("screen") or {}) if live.present() else {}
        raw = {
            "url": live.page.url, "title": facts.get("title"), "text": facts.get("text"),
            "console": live.watcher.console, "pageerrors": live.watcher.pageerrors,
            "requests": live.watcher.failed_labels(), "screenshot": str(shot),
            "load": {"readyState": facts.get("readyState"), "agent": live.present()},
            "busy": live.busy() or {}, "target": self._target(step),
            "screen": self._screen_of(screen),
        }
        return build_evidence(raw, live.watcher.masker)

    @staticmethod
    def _screen_of(screen: dict) -> dict | None:
        root = screen.get("screen")
        if not root:
            return None
        return {"name": root["name"], "state": root["state"],
                "overlays": [o.get("name") or o.get("testid") or o.get("role", "")
                             for o in screen.get("overlays", ())],
                "alerts": screen.get("alerts", [])}

    def _target(self, step: Step) -> list[str] | None:
        """Цель шага глазами window.__agent: роль, имя, недоступна, закрыта."""
        if not self.live.present() or target_form(step.target) is None:
            return None
        form = target_form(step.target)
        if form.value == "testid":
            query = {"testid": step.target["testid"]}
        elif form.value == "text":
            query = {"name": step.target}
        else:
            return None
        return target_line(self.live.find(query))

    def _snapshot(self, file: Path) -> None:
        """Снимок красного шага: область цели, а без неё — видимая часть страницы."""
        live, rect = self.live, self.live.target_rect
        box = None
        if rect:
            scroll = eval_page(live.page, "() => [scrollX, scrollY]", default=[0, 0])
            box = {"x": rect["x"] + scroll[0], "y": rect["y"] + scroll[1],
                   "width": rect["w"], "height": rect["h"]}
        clip = clip_for(box)
        try:
            live.page.screenshot(path=str(file), **({"clip": clip, "full_page": True}
                                                    if clip else {}))
        except PlaywrightError:
            return

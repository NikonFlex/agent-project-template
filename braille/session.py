"""Сессия файлами в каталоге: шаги приходят в request.json, ответы — в reply-<id>.json.

Перенос lib/session.mjs и клиентской части bin/braille.mjs agent-braille (см. NOTICE).
Живой исполнитель пишет heartbeat; состояние сессии — state.json, переходы объявлены в MOVES.
Браузера модуль не требует: исполнитель приходит как объект с `observe()` и `run_step()`.
"""
from __future__ import annotations

import json
import os
import threading
import time
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from braille.settings import Settings
from braille.steps import ACTIONS, Kind, StepError, fill_values, parse_step
from braille.verdicts import compact_observed, observed_after

HEARTBEAT = "heartbeat"
STATE = "state.json"
STEPS = "steps.json"
REQUEST = "request.json"
#: улики, что остаются в файле шага рядом со снимком: текст экрана уже есть в observed
BULKY_EVIDENCE = ("text", "title")


class SessionState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    #: действие не прошло, а страница могла измениться: следующий записанный шаг — goto
    DIRTY = "dirty"
    CLOSED = "closed"
    FAILED = "failed"


S = SessionState
MOVES = {
    S.STARTING: (S.READY, S.FAILED),
    S.READY: (S.BUSY, S.CLOSED, S.FAILED),
    S.BUSY: (S.READY, S.DIRTY, S.FAILED),
    S.DIRTY: (S.BUSY, S.CLOSED, S.FAILED),
    S.CLOSED: (),
    S.FAILED: (),
}
DIRTY_NEXT = 'прошлое действие не прошло, а страница могла измениться: дальше только {"goto": "/…"}'
NO_REQUEST = 'запрос — {"step": …}, {"look": true} или {"close": true}'


class Runner(Protocol):
    def observe(self) -> dict: ...

    def run_step(self, index: int, step: Any) -> dict: ...

    def reading(self, observed: dict) -> dict: ...


def move_problem(current: SessionState | None, to: SessionState) -> str | None:
    """Почему переход запрещён; None — объявлен. Сессия начинается только со starting."""
    if current is None:
        return None if to is S.STARTING else f"сессия начинается со starting, а не с {to}"
    return None if to in MOVES[current] else f"сессия: переход {current} -> {to} не объявлен"


def set_state(directory: Path, current: SessionState | None, to: SessionState,
              **extra: Any) -> SessionState:
    """Пишет state.json; недозволенный переход — исключение."""
    problem = move_problem(current, to)
    if problem:
        raise StepError(problem)
    target = directory / STATE
    scratch = target.with_suffix(".tmp")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    scratch.write_text(json.dumps({"state": to, "pid": os.getpid(), "at": stamp, **extra}),
                       encoding="utf-8")
    scratch.replace(target)
    return to


def write_json(target: Path, body: Any) -> None:
    scratch = target.with_suffix(".tmp")
    scratch.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    scratch.replace(target)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class Heartbeat:
    """Пока исполнитель жив, раз в несколько секунд обновляет файл heartbeat."""

    def __init__(self, directory: Path, period_ms: int) -> None:
        self.file = directory / HEARTBEAT
        self.period = period_ms / 1000
        self.stop = threading.Event()
        self.beat()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def beat(self) -> None:
        self.file.write_text(str(int(time.time() * 1000)), encoding="utf-8")

    def _run(self) -> None:
        while not self.stop.wait(self.period):
            self.beat()


# ---------------------------------------------------------------- ответ шага


def step_reply(raw: dict, entry: dict, before: dict, now: dict) -> dict:
    """Ответ шага: ok, api (запросы шага), sent, notices, contradictions, visual, observed."""
    extra = {key: entry[key] for key in ("sent", "notices", "contradictions", "visual")
             if entry.get(key)}
    if entry["error"]:
        reply = {"ok": False, "error": entry["error"], "api": entry["calls"], **extra,
                 "ms": entry["ms"]}
        if entry["dirty"]:
            reply["next"] = "шаг не записан; страница могла измениться — дальше только goto"
        reply["observed"] = compact_observed(now, before)
        if entry.get("evidence"):
            reply["evidence"] = {key: value for key, value in entry["evidence"].items()
                                 if key not in BULKY_EVIDENCE}
        return reply
    return {"ok": True, "recorded": raw, "api": entry["calls"], **extra, "ms": entry["ms"],
            "observed": compact_observed(now, before)}


class Session:
    """Состояние живой сессии на стороне исполнителя."""

    def __init__(self, directory: Path, values: dict, runner: Runner) -> None:
        self.directory = directory
        self.values = values
        self.runner = runner
        self.recorded: list[dict] = []
        self.said: set[tuple[str, str]] = set()
        self.seen: dict | None = None
        self.state = set_state(directory, None, S.STARTING)

    def handle(self, request: dict) -> dict:
        """Ответ на один запрос; запись шага в steps.json — только у прошедшего."""
        if request.get("close"):
            write_json(self.directory / STEPS, self.recorded)
            return {"ok": True, "closed": True, "steps": len(self.recorded),
                    "file": str(self.directory / STEPS)}
        if request.get("look"):
            return self._look()
        if "step" not in request:
            return {"ok": False, "error": NO_REQUEST}
        try:
            step = parse_step(fill_values(request["step"], self.values))
        except StepError as error:
            return {"ok": False, "error": f"шаг не разобран: {error}"}
        if self.state is S.DIRTY and step.kind is not Kind.GOTO:
            return {"ok": False, "error": DIRTY_NEXT}
        return self._run(request["step"], step)

    def _look(self) -> dict:
        now = self.runner.observe()
        self.seen = now
        return {"ok": True, "observed": compact_observed(now), **self.runner.reading(now)}

    def _unsaid(self, now: dict, visual: list[str]) -> list[str]:
        """Замечание о виде страницы говорится один раз на странице; `look` повторяет всё."""
        where = now.get("url", "")
        fresh = [line for line in visual if (where, line) not in self.said]
        self.said.update((where, line) for line in visual)
        return fresh

    def _run(self, raw: dict, step: Any) -> dict:
        self.state = set_state(self.directory, self.state, S.BUSY)
        before = self.seen if self.seen is not None else self.runner.observe()
        entry = self.runner.run_step(len(self.recorded), step)
        now = self.seen = entry["observed"]
        entry["visual"] = self._unsaid(now, entry.get("visual", []))
        recorded, how = dict(raw), "дан"
        if not entry["error"]:
            if step.kind in ACTIONS and "after" not in recorded:
                how = "взят из наблюдения"
                seen = observed_after(before, now)
                if seen:
                    recorded["after"] = seen
            self.recorded.append(recorded)
            write_json(self.directory / STEPS, self.recorded)
        reply = step_reply(recorded, entry, before, now)
        if reply["ok"]:
            reply["after"] = how if step.kind in ACTIONS else "—"
        self.state = set_state(self.directory, self.state,
                               S.DIRTY if entry["dirty"] else S.READY)
        return reply


def serve(directory: Path, values: dict, runner: Runner, settings: Settings) -> None:
    """Цикл исполнителя: ждёт request.json, отвечает reply-<id>.json, закрывается по close."""
    conf = settings.session
    session = Session(directory, values, runner)
    beat = Heartbeat(directory, conf["heartbeat_ms"])
    beat.thread.start()
    session.state = set_state(directory, session.state, S.READY)
    last = time.monotonic()
    try:
        while True:
            request = take_request(directory)
            if request is None:
                if (time.monotonic() - last) * 1000 > conf["idle_ms"]:
                    set_state(directory, session.state, S.FAILED,
                              reason=f"запросов нет {conf['idle_ms'] // 60000} мин")
                    return
                time.sleep(conf["poll_ms"] / 1000)
                continue
            last = time.monotonic()
            reply = session.handle(request)
            write_json(directory / f"reply-{request.get('id', 'x')}.json", reply)
            if request.get("close"):
                set_state(directory, session.state, S.CLOSED)
                return
    finally:
        beat.stop.set()


def take_request(directory: Path) -> dict | None:
    """Забирает request.json (файл удаляется); битый файл пропускается."""
    file = directory / REQUEST
    if not file.exists():
        return None
    body = read_json(file)
    file.unlink(missing_ok=True)
    return body if isinstance(body, dict) else None


# ---------------------------------------------------------------- клиент


def session_problem(directory: Path, settings: Settings) -> str | None:
    """Почему сессия не отвечает; None — жива."""
    state = read_json(directory / STATE)
    if not state:
        return f"сессии в {directory} нет: сначала `python3 -m braille start`"
    if state["state"] in (S.CLOSED, S.FAILED):
        return f"сессия {state['state']}" + (f": {state['reason']}" if state.get("reason") else "")
    beat = read_json(directory / HEARTBEAT)
    stale = settings.session["stale_heartbeat_s"] * 1000
    if not isinstance(beat, int) or time.time() * 1000 - beat > stale:
        return f"исполнитель не отвечает; журнал: {directory / 'executor.log'}"
    return None


def ask(directory: Path, body: dict, timeout_s: float, settings: Settings) -> dict:
    """Кладёт запрос в сессию и ждёт ответ."""
    problem = session_problem(directory, settings)
    if problem:
        return {"ok": False, "error": problem}
    ident = f"{int(time.time() * 1000)}{os.getpid()}"
    scratch = directory / f"{REQUEST}.tmp"
    scratch.write_text(json.dumps({**body, "id": ident}), encoding="utf-8")
    scratch.replace(directory / REQUEST)
    answer = directory / f"reply-{ident}.json"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        reply = read_json(answer) if answer.exists() else None
        if reply is not None:
            answer.unlink(missing_ok=True)
            return reply
        stopped = session_problem(directory, settings)
        if stopped:
            return {"ok": False, "error": stopped}
        time.sleep(settings.session["poll_ms"] / 1000 / 1.5)
    return {"ok": False, "error": f"ответа нет за {timeout_s:.0f} с; журнал: "
                                  f"{directory / 'executor.log'}"}

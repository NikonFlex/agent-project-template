"""Самопроверка исполнителя: контрольные случаи без браузера и сети.

    python3 -m braille selftest

Каждый случай обязан дать ожидаемый отказ (или чистый ответ там, где ждали чистый). Сломанный
контрольный случай делает прогон красным: так видно, что проверка не пустая. Браузерные
проверки исполнителя — tests/braille/test_runner.py (нужны playwright и Chromium).
"""
from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from braille.evidence import Masker, build_evidence, evidence_problems
from braille.session import MOVES, S, Session, move_problem, session_problem, set_state
from braille.settings import Settings
from braille.steps import (
    After,
    HostRefused,
    StepError,
    check_base,
    fill_values,
    parse_step,
    scenario_problems,
)
from braille.verdicts import (
    after_problem,
    compact_observed,
    contradictions_of,
    network_contradictions,
    occlusion_problem,
    order_problem,
    wait_is_over,
)


@dataclass
class Case:
    title: str
    got: Callable[[], Any]
    #: подстрока ожидаемого отказа; None — ответ должен быть чистым (пусто)
    want: str | None


def refusal(function: Callable[[], Any]) -> Callable[[], str]:
    """Отказ, с которым упал вызов: текст исключения; не упал — пусто."""
    def run() -> str:
        try:
            function()
        except (StepError, HostRefused) as error:
            return str(error)
        return ""
    return run


def scenario(steps: list, **fields: Any) -> dict:
    base = {"name": "a", "why": "b", "verifies": ["c"],
            "steps": [{"goto": "/", "after": {"state": "ready"}}, *steps]}
    return {**base, **fields}


def step_cases() -> list[Case]:
    bad = lambda raw: refusal(lambda: parse_step(raw))  # noqa: E731
    return [
        Case("шаг без вида", bad({"tap": 1}), "ровно один"),
        Case("пауза по времени", bad({"wait": 2000}), "пауза по времени запрещена"),
        Case("шаг, что не переносим", bad({"hold": "/x", "ms": 800}), "не перенесён"),
        Case("testid не вида область.действие", bad({"click": {"testid": "Save"}}),
             "цель шага click"),
        Case("goto без слэша", bad({"goto": "orders"}), "путь от корня"),
        Case("fill без value", bad({"fill": "Поиск"}), "нет value"),
        Case("чужой ключ шага", bad({"goto": "/x", "value": "1"}), "не относится к шагу goto"),
        Case("пустой after", bad({"click": {"testid": "a.b"}, "after": {}}), "хотя бы одно"),
        Case("goto внутри repeat", bad({"repeat": [{"goto": "/x"}], "until": "Готово"}),
             "нельзя repeat и goto"),
        Case("times вне границ", bad({"requested": "/x", "times": 99}), "times"),
        Case("верный шаг разбирается",
             refusal(lambda: parse_step({"click": {"testid": "orders.add"},
                                         "after": {"shown": ["orders.title"]}})), None),
    ]


def scenario_cases() -> list[Case]:
    return [
        Case("действие без after",
             lambda: scenario_problems(scenario([{"click": {"testid": "a.b"}}, {"wait": "x"}])),
             "без after"),
        Case("click по подписи", lambda: scenario_problems(scenario(
            [{"click": "Сохранить", "after": {"state": "ready"}}, {"wait": "x"}])),
             "click по подписи"),
        Case("только отчёт приложения", lambda: scenario_problems(scenario(
            [{"fact": {"count": "1"}}])), "нет прямой проверки"),
        Case("ничего не проверяет", lambda: scenario_problems(scenario([])), "ничего не проверяет"),
        Case("verifies пуст", lambda: scenario_problems(scenario([{"wait": "x"}], verifies=[])),
             "не называет требование"),
        Case("имя не по файлу", lambda: scenario_problems(scenario([{"wait": "x"}]), "b.json"),
             "не совпадает с именем файла"),
        Case("чистый сценарий", lambda: scenario_problems(scenario([{"wait": "x"}]), "a.json"),
             None),
    ]


def host_cases(settings: Settings) -> list[Case]:
    check = lambda base: refusal(lambda: check_base(base, settings))  # noqa: E731
    return [
        Case("прод не открывается", check("https://prod.example/orders"), "ходит только на"),
        Case("не адрес", check("localhost"), "не адрес"),
        Case("localhost с портом", check("http://localhost:18147"), None),
        Case("127.0.0.1", check("http://127.0.0.1:8030"), None),
    ]


def session_cases() -> list[Case]:
    moves = lambda a, b: move_problem(a, b)  # noqa: E731
    declared = [(a, b) for a, tos in MOVES.items() for b in tos]
    return [
        Case("начало не со starting", lambda: moves(None, S.READY), "начинается со starting"),
        Case("закрытая сессия не оживает", lambda: moves(S.CLOSED, S.READY), "не объявлен"),
        Case("из ready сразу в dirty", lambda: moves(S.READY, S.DIRTY), "не объявлен"),
        Case("грязная сессия ведёт в busy", lambda: moves(S.DIRTY, S.BUSY), None),
        Case("все объявленные переходы чисты",
             lambda: [p for a, b in declared if (p := move_problem(a, b))], None),
        Case("сессия файлами: шаги и грязная страница", session_trace, None),
    ]


class FakeRunner:
    """Исполнитель без браузера: путь «/bad» — действие, у которого не наступил after."""

    def observe(self) -> dict:
        return {"screen": {"name": "orders", "state": "ready"}, "url": "u",
                "elements": [{"testid": "orders.add", "role": "button", "name": "Добавить"}],
                "testids": ["orders.add"]}

    def run_step(self, index: int, step: Any) -> dict:
        bad = step.target == "/bad"
        return {"step": f"{index}", "error": "after не наступил" if bad else None,
                "dirty": bad, "calls": ["GET /orders 200"], "ms": 0, "observed": self.observe()}

    def reading(self, observed: dict) -> dict:
        return {}


def session_trace() -> list[str]:
    """Что обещает сессия: ok записывается, красный после действия просит goto, пароль в steps.json
    остаётся подстановкой."""
    problems = []
    with tempfile.TemporaryDirectory() as raw:
        session = Session(Path(raw), {"password": "s3cret-value"}, FakeRunner())
        session.state = set_state(Path(raw), session.state, S.READY)
        ok = session.handle({"step": {"fill": "Пароль", "value": "{password}",
                                      "after": {"state": "ready"}}})
        problems += [] if ok["ok"] else [f"ok-шаг красный: {ok}"]
        red = session.handle({"step": {"goto": "/bad"}})
        problems += [] if not red["ok"] and "next" in red else ["красный after не просит goto"]
        blocked = session.handle({"step": {"press": "Escape"}})
        problems += [] if not blocked["ok"] and "goto" in blocked["error"] else [
            "грязная сессия приняла не goto"]
        saved = (Path(raw) / "steps.json").read_text(encoding="utf-8")
        problems += ["пароль попал в steps.json"] if "s3cret-value" in saved else []
        problems += [] if "{password}" in saved else ["подстановка не сохранена"]
    return problems


def stale_session(settings: Settings, state: S, age_ms: int) -> str:
    """Что клиент скажет о сессии, чей исполнитель писал heartbeat `age_ms` назад."""
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        set_state(directory, None, S.STARTING)
        (directory / "state.json").write_text(json.dumps({"state": state}), encoding="utf-8")
        (directory / "heartbeat").write_text(str(int(time.time() * 1000) - age_ms))
        return session_problem(directory, settings) or ""


def client_cases(settings: Settings) -> list[Case]:
    stale = settings.session["stale_heartbeat_s"] * 1000 * 2
    return [
        Case("исполнитель замолчал", lambda: stale_session(settings, S.READY, stale),
             "не отвечает"),
        Case("закрытая сессия", lambda: stale_session(settings, S.CLOSED, 0), "closed"),
        Case("живая сессия", lambda: stale_session(settings, S.READY, 0), None),
        Case("окна из конфига", lambda: [f"{v['width']}x{v['height']}"
                                         for v in settings.viewports.values()],
             "1440x1000"),
        Case("окно телефона", lambda: json.dumps(settings.viewports["phone"]), '"width": 390'),
    ]


def verdict_cases() -> list[Case]:
    call = lambda method, path, status, sent="", body="": {  # noqa: E731
        "method": method, "path": path, "status": status, "sent": sent, "body": body}
    ready = {"screen": {"name": "orders", "state": "ready"}}
    return [
        Case("ready, а запрос упал", lambda: contradictions_of(
            ready, [], ["POST /api/orders 500"]), "состояние ready, а POST /api/orders 500"),
        Case("loading, а страница не занята", lambda: contradictions_of(
            {"screen": {"name": "orders", "state": "loading"}}, [], []), "ничем не занята"),
        Case("200 с отказом в теле", lambda: network_contradictions(
            [call("POST", "/api/save", 200, "{}", '{"ok": false}')]), "ok: false"),
        Case("один POST дважды", lambda: network_contradictions([
            call("POST", "/api/save", 200, "a=1"), call("POST", "/api/save", 200, "a=1")]),
             "отправлен 2 раза"),
        Case("error без упавшего запроса", lambda: contradictions_of(
            {"screen": {"name": "x", "state": "error"}}, [], []), "запросов с отказом"),
        Case("empty при факте больше нуля", lambda: contradictions_of(
            {"screen": {"name": "x", "state": "empty"}, "facts": {"orders": "3"}}, [], []),
             "факт orders=3"),
        Case("честный ответ без противоречий", lambda: contradictions_of(ready, [], []), None),
        Case("after: кнопка не появилась", lambda: after_problem(
            After(None, "ready", ("orders.add",), (), 20),
            {"screen": {"name": "orders", "state": "ready"}, "testids": []}),
             "не появились: orders.add"),
        Case("кнопку закрывает панель", lambda: occlusion_problem(
            {"name": "Добавить", "occludedBy": [{"testid": "panel.body", "name": "Панель"}]}),
             '"Панель" (panel.body)'),
        Case("порядок текстов нарушен", lambda: order_problem(
            [[5], [3]], ["Первый", "Второй"], {}), "раньше предыдущего"),
        Case("запрос упал, сеть тиха",
             lambda: "ждать нечего" * wait_is_over(1, 0, 0, 6000, 5000), "ждать нечего"),
        Case("пока запрос в пути — ждём",
             lambda: "ждать нечего" * wait_is_over(1, 1, 0, 6000, 5000), None),
        Case("в ответе шага остаётся счёт прежнего", lambda: compact_observed(
            {"screen": None, "elements": [{"testid": "a.b"}], "testids": []},
            {"elements": [{"testid": "a.b"}]}), '"unchanged": 1'),
    ]


def secret_cases(settings: Settings) -> list[Case]:
    masker = Masker(settings, ("login-secret-9",))
    return [
        Case("пароль в теле формы", lambda: masker.body("login=a&password=hunter22"),
             "password=***"),
        Case("пароль в теле JSON", lambda: masker.body('{"user": "a", "password": "hunter22"}'),
             '"password": "***"'),
        Case("токен в запросе адреса", lambda: masker.query("order=1&token=abc12345"),
             "token=***"),
        Case("Bearer в тексте", lambda: masker.text("Authorization: Bearer abcdefgh12345678"),
             "***"),
        Case("секрет входа где угодно", lambda: masker.text("ошибка у login-secret-9"), "***"),
        Case("значение не из секретов цело", lambda: "ЖБ-000001" not in masker.body(
            '{"order": "ЖБ-000001"}'), None),
        Case("улики без снимка неполны", lambda: evidence_problems(build_evidence(
            {"url": "http://x/", "text": "t", "load": {"readyState": "complete", "agent": True}},
            masker)), "нет поля screenshot"),
        Case("значения подставляются", lambda: fill_values(
            {"fill": "Пароль", "value": "{password}"}, {"password": "p"})["value"] != "p", None),
    ]


def check(cases: list[Case]) -> list[str]:
    bad = []
    for case in cases:
        got = case.got()
        text = json.dumps(got, ensure_ascii=False) if not isinstance(got, str) else got
        passed = (not got) if case.want is None else case.want in text
        if not passed:
            bad.append(f"{case.title}: ждали {case.want!r}, получили {text[:200]!r}")
    return bad


def selftest(settings: Settings) -> int:
    cases = (step_cases() + scenario_cases() + host_cases(settings) + session_cases()
             + client_cases(settings) + verdict_cases() + secret_cases(settings))
    bad = check(cases)
    for line in bad:
        print(f"✗ {line}")
    print(f"braille selftest: {len(cases) - len(bad)} из {len(cases)}")
    return 1 if bad else 0

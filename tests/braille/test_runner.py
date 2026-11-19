"""Исполнитель шагов (braille/) на страницах-фикстурах: закрытая кнопка, упавший запрос, переход,
`after`, сообщения, противоречия, проверки «что видно».

Нужны playwright и Chromium; без них тесты пропускаются (conftest.py). Каждая проверка краснеет,
если сломать соответствующее место исполнителя (запись — в файле задачи, «Что установлено»).
"""
from __future__ import annotations

import time

import pytest

from braille.steps import Kind, parse_step


def go(stepper, page: str, after: dict | None = None) -> dict:
    """Открыть страницу фикстуры; на ней уже есть корень с состоянием."""
    reply = stepper.run({"goto": f"/{page}", "after": after or {"state": "ready"}})
    assert reply["ok"], reply
    return reply


def test_click_on_covered_button_is_red_and_names_the_panel(stepper):
    go(stepper, "steps_cover.html")
    started = time.monotonic()
    reply = stepper.run({"click": {"testid": "cover.add"}, "after": {"state": "ready"}})
    assert not reply["ok"]
    assert '"Добавить" закрыт: "Панель фильтров" (panel.filters)' in reply["error"]
    assert "в центре цели: aside" in reply["error"]
    assert time.monotonic() - started < 20
    assert (stepper.out / "out").glob("*.png"), "снимок красного шага лежит рядом"
    assert "next" not in reply, "нажатие не произошло: страница не менялась, goto не нужен"


def test_disabled_button_is_red_without_waiting_for_playwright(stepper):
    go(stepper, "steps_cover.html")
    started = time.monotonic()
    reply = stepper.run({"click": {"testid": "cover.off"}, "after": {"state": "ready"}})
    assert not reply["ok"] and "недоступна" in reply["error"]
    assert time.monotonic() - started < 20


def test_failed_fetch_ends_the_wait_in_seconds_and_names_the_request(stepper):
    go(stepper, "steps_api.html")
    started = time.monotonic()
    reply = stepper.run({"click": {"testid": "api.save"},
                         "after": {"shown": ["api.never"], "within": 30}})
    assert not reply["ok"]
    assert time.monotonic() - started < 20, "упавший запрос не должен ждать срока after"
    assert "POST /api/broken 500" in reply["error"]
    assert reply["api"] == ["POST /api/broken 500"]
    assert any("состояние ready, а POST /api/broken 500" in c for c in reply["contradictions"])


def test_dropped_request_is_named(stepper):
    go(stepper, "steps_api.html")
    reply = stepper.run({"click": {"testid": "api.down"},
                         "after": {"shown": ["api.never"], "within": 30}})
    assert not reply["ok"]
    assert "GET /api/down net::ERR_FAILED" in reply["error"]


def test_link_waits_for_the_new_page(stepper):
    go(stepper, "steps_api.html")
    reply = stepper.run({"click": {"testid": "api.next"}})
    assert reply["ok"], reply
    assert reply["observed"]["screen"] == {"name": "next", "state": "ready"}
    assert reply["api"] == ["GET /steps_next.html 200"]
    assert reply["recorded"]["after"]["screen"] == "next", "after взят из наблюдения"
    assert reply["after"] == "взят из наблюдения"


def test_form_post_is_listed_and_its_password_is_masked(stepper):
    go(stepper, "steps_form.html")
    assert stepper.run({"fill": "Имя", "value": "Иван"})["ok"]
    assert stepper.run({"fill": "Пароль", "value": "{password}"})["ok"]
    reply = stepper.run({"click": {"testid": "form.submit"}, "after": {"screen": "next"}})
    assert reply["ok"], reply
    assert reply["api"][0] == "POST /steps_next.html 200"
    [sent] = reply["sent"]
    assert "password=***" in sent["sent"] and "hunter-22-pass" not in str(reply)
    assert "hunter-22-pass" not in (stepper.out / "steps.json").read_text(encoding="utf-8")
    assert stepper.steps()[2]["value"] == "{password}"


def test_after_shown_waits_for_a_late_element(stepper):
    go(stepper, "steps_api.html")
    reply = stepper.run({"click": {"testid": "api.later"}, "after": {"shown": ["api.late"]}})
    assert reply["ok"], reply
    assert any(e["testid"] == "api.late" for e in reply["observed"]["elements"])


def test_after_that_does_not_come_is_red_and_asks_for_goto(stepper):
    go(stepper, "steps_api.html")
    reply = stepper.run({"click": {"testid": "api.later"},
                         "after": {"shown": ["api.never"], "within": 1}})
    assert not reply["ok"] and "не появились: api.never" in reply["error"]
    assert "goto" in reply["next"]
    refused = stepper.run({"press": "Escape"})
    assert not refused["ok"] and "goto" in refused["error"]
    assert stepper.run({"goto": "/steps_api.html", "after": {"state": "ready"}})["ok"]


def test_step_answer_waits_for_a_fetch_it_started(stepper):
    go(stepper, "steps_api.html")
    reply = stepper.run({"click": {"testid": "api.load"}})
    assert reply["ok"], reply
    assert reply["api"] == ["GET /api/delayed 200"]
    assert any(e["testid"] == "api.done" for e in reply["observed"]["elements"])


def test_notices_keep_a_message_that_has_already_gone(stepper):
    go(stepper, "steps_api.html")
    reply = stepper.run({"click": {"testid": "api.notify"}})
    assert reply["notices"] == ["Заказ сохранён"]
    assert "notices" not in stepper.run({"click": {"testid": "api.later"}})


def test_contradictions_name_a_refusal_in_a_200_body_and_a_double_post(stepper):
    go(stepper, "steps_api.html")
    refused = stepper.run({"click": {"testid": "api.refuse"}})
    assert any("POST /api/refused 200, но тело говорит ok: false" in c
               for c in refused["contradictions"])
    twice = stepper.run({"click": {"testid": "api.twice"}})
    assert any("POST /api/accepted отправлен 2 раза" in c for c in twice["contradictions"])


def test_observed_lists_only_what_changed(stepper):
    first = go(stepper, "steps_api.html")
    again = stepper.run({"click": {"testid": "api.later"}, "after": {"shown": ["api.late"]}})
    assert len(first["observed"]["elements"]) > 5
    assert [e["testid"] for e in again["observed"]["elements"]] == ["api.late"]
    assert again["observed"]["unchanged"] >= 5
    assert "text" in again["observed"] and again["observed"]["screen"]["name"] == "api"


# Каждая пара: шаг и ожидание «прошёл» (True) или «красный с этим текстом».
EXPECTATIONS = [
    ({"wait": "Проверки"}, True),
    ({"wait": "Нет такого", "within": 1}, "текста \"Нет такого\" нет"),
    ({"absent": "Нет такого"}, True),
    ({"absent": "Первый"}, "есть на странице"),
    ({"order": ["Первый", "Второй"]}, True),
    ({"order": ["Второй", "Первый"]}, "раньше предыдущего"),
    ({"fact": {"mode": "preview"}}, True),
    ({"fact": {"mode": "final"}}, "факт экрана"),
    ({"enabled": {"testid": "expect.on"}}, True),
    ({"disabled": {"testid": "expect.on"}}, "доступен"),
    ({"disabled": {"testid": "expect.off"}}, True),
    ({"field": "Заметка", "has": "ЖБ-17"}, True),
    ({"field": "Заметка", "has": "ЖБ-99"}, "в поле"),
    ({"filled": "Заметка"}, True),
    ({"filled": "Пустое"}, "поле пусто"),
    ({"checked": {"testid": "expect.agree"}}, True),
    ({"unchecked": {"testid": "expect.agree"}}, "отмечен"),
    ({"visible": {"selector": ".seen"}}, True),
    ({"visible": {"selector": ".gone"}}, "нет видимого"),
    ({"hidden": {"selector": ".gone"}}, True),
]


def test_expectations_pass_and_fail(stepper):
    """Каждая проверка «что видно» и в прошедшем, и в красном виде, на одной странице."""
    go(stepper, "steps_expect.html")
    stepper.hurry(expect=1)
    wrong = []
    for raw, want in EXPECTATIONS:
        reply = stepper.run(raw)
        passed = reply["ok"] if want is True else (not reply["ok"] and want in reply["error"])
        if not passed:
            wrong.append((raw, want, reply.get("error")))
    assert not wrong


def test_repeat_presses_until_the_text_appears(stepper):
    go(stepper, "steps_expect.html")
    reply = stepper.run({"repeat": [{"click": {"testid": "expect.step"}}], "until": "3"})
    assert reply["ok"], reply
    assert stepper.live.page.inner_text("#steps") == "3"


def test_requested_counts_page_requests(stepper):
    go(stepper, "steps_api.html")
    stepper.hurry(expect=1)
    stepper.run({"click": {"testid": "api.load"}})
    assert stepper.run({"requested": "/api/delayed", "times": 1})["ok"]
    assert not stepper.run({"requested": "/api/delayed", "times": 2})["ok"]


def test_second_tab_keeps_the_first_one(stepper):
    go(stepper, "steps_api.html")
    assert stepper.run({"tab": "open"})["ok"]
    assert stepper.run({"goto": "/steps_expect.html", "after": {"screen": "expect"}})["ok"]
    assert stepper.run({"tab": "first"})["observed"]["screen"]["name"] == "api"


def test_every_step_kind_has_a_handler():
    pytest.importorskip("playwright.sync_api")
    from braille.actions import HANDLERS
    assert set(HANDLERS) == set(Kind)
    assert parse_step({"wait": "x"}).kind is Kind.WAIT


def test_evaluate_is_repeated_on_the_new_page_when_the_old_one_left():
    """Страница ушла посреди вызова: ошибка Playwright не краснит шаг, вызов идёт на новой."""
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import Error

    from braille.browser import eval_page

    class Leaving:
        calls = 0
        loaded = 0

        def evaluate(self, script, arg=None):
            self.calls += 1
            if self.calls == 1:
                raise Error("Execution context was destroyed, most likely because of a navigation")
            return "новая страница"

        def wait_for_load_state(self, state):
            self.loaded += 1

    page = Leaving()
    assert eval_page(page, "1") == "новая страница"
    assert (page.calls, page.loaded) == (2, 1)

    class Broken:
        def evaluate(self, script, arg=None):
            raise Error("Target page, context or browser has been closed")

    assert eval_page(Broken(), "1", default="пусто") == "пусто"

"""window.__agent на страницах-фикстурах (tests/braille/fixtures): ответы screen, find, act,
layout, busy, errors и visual. Решение — docs/decisions.md, «Экраны для агентов…».

Нужны playwright и Chromium; без них тесты пропускаются (conftest.py).
"""
from __future__ import annotations

import json

from agent_pages import agent, open_page


def act(page, *call):
    return agent(page, "act", *call)


NORMAL = ("Нормальный текст", "Нормальная кнопка")


def test_screen_answers_like_the_executor(agent_page):
    open_page(agent_page, "page.html")
    seen = agent(agent_page, "screen")
    assert seen["screen"] == {"name": "orders", "state": "ready"}
    assert {"testid": "orders.add", "role": "button", "name": "Добавить"} in seen["elements"]
    assert {"testid": "orders.export", "role": "button", "name": "Выгрузить",
            "disabled": True} in seen["elements"]
    assert {"field": "Поиск заказа", "role": "textbox"} in seen["fields"]
    assert {"label": "Без разметки", "role": "button"} in seen["labels"]
    assert seen["headings"] == ["Заказы"]
    assert seen["facts"] == {"filter": "all"}
    assert "Добавить" in seen["text"]
    assert "overlays" not in seen


def test_screen_without_root_still_lists_labels(agent_page):
    open_page(agent_page, "plain.html")
    seen = agent(agent_page, "screen")
    assert seen["screen"] is None
    assert {"label": "Сохранить", "role": "button"} in seen["labels"]
    assert {"field": "Имя", "role": "textbox"} in seen["fields"]


def test_screen_shows_open_dialog_as_overlay(agent_page):
    open_page(agent_page, "page.html")
    agent_page.evaluate("""() => document.body.insertAdjacentHTML("beforeend",
        '<div role="dialog" aria-label="Подтверждение" style="position:fixed;right:0">ok</div>')""")
    assert agent(agent_page, "screen")["overlays"] == [
        {"role": "dialog", "name": "Подтверждение", "testid": None}]


def test_find_by_testid_prefix_and_text(agent_page):
    open_page(agent_page, "page.html")
    ids = {item["testid"] for item in agent(agent_page, "find", {"testid": "orders.*"})}
    assert {"orders.add", "orders.refresh", "orders.export"} <= ids
    [by_text] = agent(agent_page, "find", {"text": "Обновить"})
    assert by_text["testid"] == "orders.refresh"
    assert by_text["occludedBy"] == []


def test_names_ignore_aria_hidden_markup(agent_page):
    open_page(agent_page, "plain.html")
    agent_page.evaluate("""() => document.body.insertAdjacentHTML("beforeend", `
        <label for="external">Имя<span aria-hidden="true">*</span></label>
        <input id="external" data-testid="names.external">
        <label>Город<span aria-hidden="true">*</span><input data-testid="names.wrapped"></label>
        <span id="role">Роль<span aria-hidden="true">*</span></span>
        <input aria-labelledby="role" data-testid="names.reference">
        <button data-testid="names.button"><span aria-hidden="true">✓</span>Готово</button>
    `)""")
    names = {
        item["testid"]: item["name"]
        for item in agent(agent_page, "find", {"testid": "names.*"})
    }
    assert names == {
        "names.external": "Имя", "names.wrapped": "Город",
        "names.reference": "Роль", "names.button": "Готово",
    }


def test_act_click_type_select_and_refusals(agent_page):
    open_page(agent_page, "page.html")
    assert act(agent_page, "orders.add", "click") == {"ok": True}
    assert agent_page.inner_text("#out") == "добавлено"
    act(agent_page, "q", "type", "ЖБ-17")
    assert agent_page.inner_text("#out") == "поиск: ЖБ-17"
    act(agent_page, "q", "clear")
    assert agent_page.input_value("#q") == ""
    act(agent_page, "status", "select", "open")
    assert agent_page.inner_text("#out") == "статус: open"
    assert act(agent_page, "orders.export", "click") == {"ok": False, "error": "disabled"}
    assert act(agent_page, "нет.такого", "click") == {"ok": False, "error": "not_found"}
    assert act(agent_page, "orders.add", "select", "x") == {"ok": False, "error": "unsupported"}


def test_act_cannot_modify_readonly_fields(agent_page):
    open_page(agent_page, "plain.html")
    agent_page.evaluate("""() => document.body.insertAdjacentHTML("beforeend", `
        <input readonly value="Имя" data-testid="readonly.name">
        <textarea readonly data-testid="readonly.comment">Комментарий</textarea>
    `)""")
    for testid, value in (("readonly.name", "Имя"), ("readonly.comment", "Комментарий")):
        target = agent_page.get_by_test_id(testid)
        assert act(agent_page, testid, "focus") == {"ok": True}
        for call in (("type", "Новый текст"), ("clear",)):
            assert act(agent_page, testid, *call) == {"ok": False, "error": "unsupported"}
            assert target.input_value() == value


def test_act_click_on_covered_button_names_the_cover(agent_page):
    open_page(agent_page, "visual.html")
    answer = act(agent_page, "cases.covered", "click")
    assert answer["error"] == "occluded"
    assert answer["occludedBy"][0]["testid"] == "cases.veil"
    [info] = agent(agent_page, "find", {"testid": "cases.covered"})
    assert [o["testid"] for o in info["occludedBy"]] == ["cases.veil"]


def test_layout_of_two_buttons(agent_page):
    open_page(agent_page, "page.html")
    found = agent(agent_page, "layout", "orders.add", "orders.refresh")
    assert found["aLeftOf"] and not found["overlaps"]
    assert agent(agent_page, "layout", "orders.add", "нет.такого") is None


def test_busy_counts_unfinished_fetch(agent_page, held):
    open_page(agent_page, "page.html")
    agent_page.click("[data-testid='orders.slow']")
    held.wait()
    assert agent(agent_page, "busy")["fetch"] == 1
    held.answer()
    agent_page.wait_for_function("() => !window.__agent.busy().fetch")
    assert "fetch" not in agent(agent_page, "busy")


def test_busy_counts_xhr(agent_page, held):
    open_page(agent_page, "page.html")
    agent_page.click("[data-testid='orders.xhr']")
    held.wait()
    assert agent(agent_page, "busy")["xhr"] == 1
    held.answer()
    agent_page.wait_for_function("() => !window.__agent.busy().xhr")


def left_busy(page) -> dict:
    """Ответ busy(), снятый страницей в момент ухода (на странице идёт и конечная анимация)."""
    return json.loads(page.evaluate("() => sessionStorage.getItem('leaving')"))


def test_busy_counts_navigation_until_the_new_page(agent_page):
    """Пока страница уходит, evaluate в Playwright стоит, поэтому ответ снимает сама страница."""
    open_page(agent_page, "page.html")
    agent_page.evaluate("""() => window.addEventListener("beforeunload", () =>
        sessionStorage.setItem("leaving", JSON.stringify(window.__agent.busy())))""")
    agent_page.click("[data-testid='orders.other']")
    agent_page.wait_for_url("**/other.html")
    assert left_busy(agent_page)["navigation"] == 1
    assert "navigation" not in agent(agent_page, "busy")


def test_busy_counts_submitted_form(agent_page):
    open_page(agent_page, "page.html")
    agent_page.evaluate("""() => document.querySelector("form").addEventListener("submit", () =>
        setTimeout(() => sessionStorage.setItem("leaving", JSON.stringify(window.__agent.busy())))
    )""")
    agent_page.click("[data-testid='orders.send']")
    agent_page.wait_for_url("**/other.html*")
    assert left_busy(agent_page)["navigation"] == 1


def test_busy_counts_finite_animation_not_spinner(agent_page):
    open_page(agent_page, "page.html")
    assert agent(agent_page, "busy")["animations"] == 1


def test_busy_takes_sources_declared_by_page_js(agent_page):
    open_page(agent_page, "page.html")
    agent_page.evaluate("""() => {
        window.queued = 2;
        window.__agent.registerBusySource("autosave", () => window.queued);
    }""")
    assert agent(agent_page, "busy")["autosave"] == 2
    agent_page.evaluate("() => { window.queued = 0; }")
    assert "autosave" not in agent(agent_page, "busy")


def test_errors_catch_failed_requests_and_page_errors(agent_page):
    open_page(agent_page, "page.html")
    agent_page.click("[data-testid='orders.broken']")
    agent_page.click("[data-testid='orders.down']")
    agent_page.evaluate("""() => {
        console.error("плохо", {a: 1});
        Promise.reject(new Error("отказ"));
        setTimeout(() => { throw new Error("сбой"); });
    }""")
    agent_page.wait_for_function("() => window.__agent.errors().length >= 5")
    by_kind = {}
    for item in agent(agent_page, "errors"):
        by_kind.setdefault(item["kind"], []).append(item)
    broken = next(i for i in by_kind["request"] if i["status"] == 500)
    assert (broken["method"], broken["code"]) == ("GET", "stand_down")
    assert broken["url"].endswith("/api/broken") and broken["path"] == "/page.html"
    assert any(i["status"] == 0 for i in by_kind["request"])
    assert "плохо" in by_kind["console"][0]["message"]
    assert "отказ" in by_kind["rejection"][0]["message"]
    assert "сбой" in by_kind["exception"][0]["message"]


def test_errors_clear_empties_the_log(agent_page):
    open_page(agent_page, "page.html")
    agent_page.click("[data-testid='orders.broken']")
    agent_page.wait_for_function("() => window.__agent.errors().length")
    assert agent(agent_page, "errors", {"clear": True})
    assert agent(agent_page, "errors") == []


VISUAL_CASES = {
    "Текст цвета фона": "background",
    "Текст цвета фона в oklch": "background",
    "Прозрачный текст": "transparent",
    "Длинная обрезанная строка про заказ": "clipped",
    "Текст за краем рамки": "container",
    "Кнопка за краем экрана": "screen",
    "Закрытая кнопка": "covered",
    "Сохранить заказ": "name",
}


def test_visual_finds_every_control_case_and_only_them(agent_page):
    open_page(agent_page, "visual.html")
    found = agent(agent_page, "visual")
    seen = {item["text"].lower(): item for item in found}
    for text, kind in VISUAL_CASES.items():
        assert seen[text[:40].lower()]["kind"] == kind, text
    assert len(found) == len(VISUAL_CASES), [item["message"] for item in found]
    assert found[0]["message"].startswith("человек не прочтёт «Текст цвета фона»")
    covered = next(item for item in found if item["kind"] == "covered")
    assert "cases.veil" in covered["by"] and "cases.veil" in covered["message"]


def test_visual_is_silent_on_normal_elements(agent_page):
    open_page(agent_page, "clean.html")
    assert agent(agent_page, "visual") == []
    open_page(agent_page, "visual.html")
    texts = [item["text"] for item in agent(agent_page, "visual")]
    assert not [text for text in texts if text in NORMAL]


def test_secret_field_values_never_leave_the_page(agent_page):
    open_page(agent_page, "secret.html")
    agent_page.fill("[name=name]", "Иван")
    agent_page.fill("[name=password]", "пароль-стенда")
    agent_page.fill("[name=api_key]", "ключ-стенда")
    fields = {f["field"]: f.get("value") for f in agent(agent_page, "screen")["fields"]}
    assert fields == {"Имя": "Иван", "Пароль": "***", "Ключ": "***"}
    assert "пароль-стенда" not in json.dumps(agent(agent_page, "screen"), ensure_ascii=False)

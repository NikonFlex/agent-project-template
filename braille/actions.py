"""Исполнение шагов: цель находится через window.__agent.find(), действие делает Playwright.

Перенос performStep и проверок из lib/browser.mjs agent-braille (см. NOTICE). После действия —
ожидание покоя (`Live.settle`), затем `after`: что должно стать видно.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from playwright.sync_api import ElementHandle
from playwright.sync_api import Error as PlaywrightError

from braille.browser import Live, eval_page
from braille.steps import (
    ACTIONS,
    AfterMissed,
    Form,
    Kind,
    Step,
    StepError,
    target_form,
    target_label,
)
from braille.verdicts import (
    absent_problem,
    after_problem,
    cover_line,
    fact_problem,
    field_problem,
    filled_problem,
    label_problem,
    norm,
    occlusion_problem,
    order_lines,
    order_problem,
    pick_by_testid,
    requested_problem,
    screen_problem,
    seen_problem,
    state_problem,
    tick_problem,
)

#: ответ обработчика «цели нет, шаг необязательный»: `after` такого шага не проверяется
SKIPPED = "skipped"
TEXT_ROLES = ("button", "link", "tab", "menuitem", "option", "combobox", "textbox", "checkbox",
              "radio", "switch")
FIELD_ROLES = ("textbox", "combobox", "searchbox", "spinbutton")
TICK_ROLES = ("checkbox", "radio", "switch")
#: шаги, после которых страница могла измениться и надо дождаться покоя
SETTLING = (*ACTIONS, Kind.HOVER, Kind.WAIT, Kind.TAB, Kind.REPEAT)

ELEMENT_BY_INDEX = "(index) => document.querySelectorAll('*')[index]"
TICK_OF = """(el) => {
  const input = el.matches('input') ? el
    : (el.querySelector('input[type=checkbox], input[type=radio]')
       ?? el.closest('label')?.control ?? null);
  return input ? {checked: input.checked === true || input.getAttribute('aria-checked') === 'true',
                  focused: input === document.activeElement} : null;
}"""
VISIBLE_COUNT = """(nodes) => nodes.filter((n) => n.checkVisibility
  ? n.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})
  : n.offsetParent !== null).length"""
CENTER_OWNER = """({x, y, w, h}) => {
  const hit = document.elementFromPoint(x + w / 2, y + h / 2);
  const owner = hit && hit.closest(
    'button, a, [role=tab], [role=menuitem], [role=option], [role=button], [role=link]');
  if (!owner) return null;
  return (owner.getAttribute('aria-label') ?? owner.textContent ?? '')
    .replace(/\\s+/g, ' ').trim();
}"""
COVER_DETAILS = """({x, y, w, h}) => {
  const hit = document.elementFromPoint(x + w / 2, y + h / 2);
  const title = (e) => (e.getAttribute('aria-label')
    ?? document.getElementById(e.getAttribute('aria-labelledby') ?? '')?.textContent
    ?? e.querySelector('h1, h2, h3, [role=heading]')?.textContent ?? e.textContent ?? '')
    .replace(/\\s+/g, ' ').trim().slice(0, 60);
  const dialogs = [...document.querySelectorAll('[role=dialog], [aria-modal=true]')]
    .filter((e) => e.getBoundingClientRect().width > 0).map(title);
  if (!hit) return {dialogs};
  const style = getComputedStyle(hit), box = hit.getBoundingClientRect();
  const owner = hit.closest('[data-testid], [role=dialog], [aria-label]');
  return {element: [hit.tagName.toLowerCase(), ...[...hit.classList].slice(0, 2)].join('.'),
    position: style.position, size: Math.round(box.width) + 'x' + Math.round(box.height),
    zIndex: style.zIndex,
    inside: owner ? (owner.getAttribute('data-testid') ?? title(owner)) : null, dialogs};
}"""

Handler = Callable[[Live, Step], Any]
HANDLERS: dict[Kind, Handler] = {}


def handles(*kinds: Kind) -> Callable[[Handler], Handler]:
    def register(function: Handler) -> Handler:
        for kind in kinds:
            HANDLERS[kind] = function
        return function
    return register


# ---------------------------------------------------------------- поиск цели


def candidates(live: Live, target: Any, roles: tuple[str, ...] = TEXT_ROLES) -> list[dict]:
    """Видимые элементы цели, как их видит window.__agent.find(); первый — тот, что берётся."""
    form = target_form(target)
    if form is Form.TESTID:
        return pick_by_testid(live.find_shown({"testid": target["testid"]}), target)
    if form is Form.TEXT:
        named = [e for e in live.find_shown({"name": target})
                 if norm(e.get("name")) == norm(target) and e.get("role") in roles]
        return named or [e for e in live.find_shown({"text": target})
                         if norm(e.get("text")) == norm(target)]
    if form is Form.NAMED:
        return named_candidates(live, target)
    return []  # селектор CSS без данных window.__agent


def named_candidates(live: Live, target: dict) -> list[dict]:
    if "pressed" in target:
        found = live.find_shown({"role": target.get("role", "button")})
        return [e for e in found if e.get("aria", {}).get("pressed") is False]
    query = {key: target[key] for key in ("role", "name") if key in target}
    found = live.find_shown(query)
    if target.get("contains"):
        return found
    return [e for e in found if norm(e.get("name")) == norm(target.get("name"))]


def missing_reason(live: Live, target: Any) -> str:
    if isinstance(target, dict) and "testid" in target:
        return label_problem(live.find_shown({"testid": target["testid"]}), target) or ""
    return f'"{target_label(target)}" нет на странице'


def element_of(live: Live, info: dict) -> ElementHandle:
    handle = live.page.evaluate_handle(ELEMENT_BY_INDEX, info["domIndex"]).as_element()
    if handle is None:
        raise StepError(f'элемент "{info.get("name")}" исчез со страницы')
    return handle


def locate(live: Live, target: Any, timeout_ms: int,
           roles: tuple[str, ...] = TEXT_ROLES) -> tuple[ElementHandle, dict | None]:
    """Цель шага: ждём, пока она появится; селектор CSS — без данных window.__agent."""
    if isinstance(target, dict) and "selector" in target:
        return live.page.locator(target["selector"]).first.element_handle(timeout=timeout_ms), None
    found: list[dict] = []

    def check() -> str | None:
        found[:] = candidates(live, target, roles)
        return None if found else missing_reason(live, target)

    live.until(check, timeout_ms / 1000)
    return element_of(live, found[0]), found[0]


# ---------------------------------------------------------------- нажатие


def center_hits(live: Live, rect: dict, name: str) -> bool:
    """Центр рамки принадлежит элементу с этим именем: нажатие дойдёт, углы закрыты скруглением."""
    owner = eval_page(live.page, CENTER_OWNER, rect, default=None)
    return owner is not None and (owner == name or owner.startswith(name))


def fresh(live: Live, target: Any) -> dict | None:
    found = candidates(live, target)
    return found[0] if found else None


def ensure_enabled(live: Live, target: Any, info: dict) -> None:
    deadline = time.monotonic() + live.settings.ms("disabled_grace") / 1000
    while info.get("aria", {}).get("disabled"):
        if time.monotonic() >= deadline:
            raise StepError(f'"{target_label(target)}" недоступна — нажать нельзя')
        live.pause()
        info = fresh(live, target) or info


def ensure_uncovered(live: Live, handle: ElementHandle, target: Any, info: dict) -> None:
    """Цель закрыта (панель выезжает) — ждём BRAILLE_OCCLUSION_GRACE_MS; не ушла — красный шаг."""
    try:
        handle.scroll_into_view_if_needed(timeout=5000)
    except PlaywrightError:
        pass
    settings = live.settings
    attempts = max(1, settings.ms("occlusion_grace") // settings.ms("occlusion_poll"))
    problem = None
    for _ in range(attempts):
        info = fresh(live, target) or info
        problem = occlusion_problem(info)
        if problem is None or (info.get("rect") and center_hits(live, info["rect"],
                                                                 info.get("name", ""))):
            return
        live.target_rect = info.get("rect")
        live.pause("occlusion_poll")
    details = eval_page(live.page, COVER_DETAILS, live.target_rect, default=None)
    raise StepError(f"{problem}{cover_line(details)}")


@handles(Kind.CLICK)
def click(live: Live, step: Step) -> str | None:
    target = step.target
    timeout = live.settings.ms("optional" if step.optional else "appear_testid")
    try:
        handle, info = locate(live, target, timeout)
    except StepError:
        if step.optional:
            return SKIPPED
        raise
    if info is not None:
        ensure_enabled(live, target, info)
        ensure_uncovered(live, handle, target, info)
    handle.click(timeout=live.settings.ms("click"))
    return None


@handles(Kind.HOVER)
def hover(live: Live, step: Step) -> None:
    handle, _ = locate(live, step.target, live.settings.ms("appear_testid"))
    handle.hover(timeout=live.settings.ms("click"))


# ---------------------------------------------------------------- ввод


@handles(Kind.FILL)
def fill(live: Live, step: Step) -> str | None:
    timeout = live.settings.ms("optional" if step.optional else "appear_testid")
    try:
        handle, _ = locate(live, step.target, timeout, FIELD_ROLES)
    except StepError:
        if step.optional:
            return SKIPPED
        raise
    kind = handle.evaluate("(el) => el.tagName.toLowerCase()")
    if kind == "select":
        handle.select_option(label=step.value, timeout=live.settings.ms("click"))
    elif handle.get_attribute("readonly") is not None:
        raise StepError(f'{target_label(step.target)} — список, не поле ввода: нажмите на него, '
                        'затем {"click": {"role": "option", "name": "…"}}')
    else:
        handle.fill(step.value, timeout=(step.within or 15) * 1000)
    return None


@handles(Kind.PRESS)
def press(live: Live, step: Step) -> None:
    live.page.keyboard.press(step.target)


@handles(Kind.UPLOAD)
def upload(live: Live, step: Step) -> None:
    file = live.settings.files_dir / step.target
    if not file.is_file():
        raise StepError(f"файла {step.target} нет в каталоге файлов сценариев "
                        f"{live.settings.files_dir}")
    selector = (step.to or {}).get("selector", "input[type=file]")
    live.page.locator(selector).first.set_input_files(str(file), timeout=live.settings.ms("click"))


@handles(Kind.GOTO)
def goto(live: Live, step: Step) -> None:
    live.page.goto(live.origin + step.target, wait_until="domcontentloaded")
    live.wait_agent(step.target)


@handles(Kind.TAB)
def tab(live: Live, step: Step) -> None:
    if step.target == "open":
        if len(live.pages) > 1:
            raise StepError("вторая вкладка уже открыта")
        here = live.page.url
        live.adopt(live.context.new_page())
        live.page.goto(here, wait_until="domcontentloaded")
        live.wait_agent(here)
        return
    index = 0 if step.target == "first" else 1
    if index >= len(live.pages):
        raise StepError(f'вкладки "{step.target}" нет: сначала {{"tab": "open"}}')
    live.current = index
    live.page.bring_to_front()


# ---------------------------------------------------------------- ожидания и проверки


def seconds(live: Live, step: Step) -> float:
    return step.within or live.settings.seconds("wait_text")


@handles(Kind.WAIT)
def wait(live: Live, step: Step) -> str | None:
    target = step.target

    def check() -> str | None:
        if isinstance(target, dict):
            problem = label_problem(live.find_shown({"testid": target["testid"]}), target)
        else:
            problem = None if live.find_shown({"text": target}) else f'текста "{target}" нет'
        if problem and (stop := screen_problem(live.observe())):
            raise StepError(stop)
        return problem

    try:
        live.until(check, seconds(live, step))
    except StepError:
        if step.optional:
            return SKIPPED
        raise
    return None


def expect(live: Live, check: Callable[[], str | None]) -> None:
    """Ожидание «что видно»: пока не сбудется, но не дольше expect секунд."""
    live.settle()
    live.until(check, live.settings.seconds("expect"))


def shown_for(live: Live, target: Any, roles: tuple[str, ...] = ()) -> list[dict]:
    """Элементы цели для проверок: по testid, по тексту или по имени."""
    if isinstance(target, dict):
        return pick_by_testid(live.find_shown({"testid": target["testid"]}), target)
    found = live.find_shown({"name": target})
    exact = [e for e in found if norm(e.get("name")) == norm(target)]
    return [e for e in (exact or found) if not roles or e.get("role") in roles]


@handles(Kind.ABSENT)
def absent(live: Live, step: Step) -> None:
    def check() -> str | None:
        target = step.target
        found = (pick_by_testid(live.find_shown({"testid": target["testid"]}), target)
                 if isinstance(target, dict) else live.find_shown({"text": target}))
        return absent_problem(len(found), bool(found or live.find({})), target_label(target))

    expect(live, check)


@handles(Kind.ORDER)
def order(live: Live, step: Step) -> None:
    parts = [[item] if isinstance(item, dict) else order_lines([item]) for item in step.target]
    lines = [line for part in parts for line in part]

    def check() -> str | None:
        lists = [pick_by_testid(live.find_shown({"testid": line["testid"]}), line)
                 if isinstance(line, dict) else live.find_shown({"text": line}) for line in lines]
        texts = {e["domIndex"]: e["text"] for found in lists for e in found}
        labels = [target_label(line) for line in lines]
        return order_problem([[e["domIndex"] for e in found] for found in lists], labels, texts)

    expect(live, check)


@handles(Kind.DISABLED, Kind.ENABLED)
def available(live: Live, step: Step) -> None:
    target = step.target

    def check() -> str | None:
        if isinstance(target, dict):
            found = pick_by_testid(live.find_shown({"testid": target["testid"]}), target)
        else:
            found = [e for e in live.find({"role": "button", "name": target})
                     if e.get("name") == target]
        states = [e.get("aria", {}).get("disabled") is True for e in found]
        return state_problem(states, target_label(target), step.kind is Kind.DISABLED)

    expect(live, check)


@handles(Kind.CHECKED, Kind.UNCHECKED, Kind.FOCUSED)
def ticked(live: Live, step: Step) -> None:
    def check() -> str | None:
        found = shown_for(live, step.target, TICK_ROLES)
        ticks = [tick for e in found if (tick := element_of(live, e).evaluate(TICK_OF))]
        return tick_problem(ticks, target_label(step.target), step.kind.value)

    expect(live, check)


def field_values(live: Live, target: Any) -> list[str]:
    if isinstance(target, dict) and "selector" in target:
        return live.page.locator(target["selector"]).evaluate_all(
            "(nodes) => nodes.map((n) => String(n.value ?? ''))")
    return [str(e.get("value", "")) for e in shown_for(live, target, FIELD_ROLES)]


@handles(Kind.FIELD, Kind.FILLED)
def field(live: Live, step: Step) -> None:
    def check() -> str | None:
        values = field_values(live, step.target)
        if step.kind is Kind.FIELD:
            return field_problem(values, step.has)
        return filled_problem(values)

    expect(live, check)


@handles(Kind.VISIBLE, Kind.HIDDEN)
def seen(live: Live, step: Step) -> None:
    def check() -> str | None:
        count = live.page.locator(step.target["selector"]).evaluate_all(VISIBLE_COUNT)
        return seen_problem(count, step.kind.value)

    expect(live, check)


@handles(Kind.FACT)
def fact(live: Live, step: Step) -> None:
    expect(live, lambda: fact_problem((live.agent("screen") or {}).get("facts", {}), step.target))


@handles(Kind.REQUESTED)
def requested(live: Live, step: Step) -> None:
    watcher = live.watcher

    def check() -> str | None:
        return requested_problem(watcher.count(step.target, step.method), step.times, step.target)

    live.until(check, live.settings.seconds("expect"), patient=False)
    if step.times is not None:
        live.page.wait_for_timeout(int(live.settings.seconds("requested_settle") * 1000))
        problem = check()
        if problem:
            raise StepError(problem)


@handles(Kind.REPEAT)
def repeat(live: Live, step: Step) -> None:
    def reached() -> bool:
        return step.until in eval_page(live.page, "document.body.innerText", default="")

    for _ in range(step.limit):
        if reached():
            return
        for inner in step.inner:
            perform(live, inner)
        live.settle()
    if not reached():
        raise StepError(f'текста "{step.until}" нет после {step.limit} повторов')


# ---------------------------------------------------------------- after и вход


def await_after(live: Live, step: Step) -> None:
    """Ждёт `after` шага; не наступил — красный шаг с тем, что видно вместо."""
    after = step.after
    if not live.present():
        raise AfterMissed("after требует window.__agent на странице")

    def check() -> str | None:
        seen_now = live.observe()
        problem = after_problem(after, seen_now)
        if problem and (stop := screen_problem(seen_now)):
            raise AfterMissed(f"{problem}; {stop}")
        return problem

    try:
        live.until(check, after.within)
    except AfterMissed:
        raise
    except StepError as error:
        raise AfterMissed(str(error)) from error


def perform(live: Live, step: Step) -> None:
    """Один шаг: действие или проверка, покой страницы, затем `after`."""
    outcome = HANDLERS[step.kind](live, step)
    if outcome == SKIPPED:
        return
    if step.kind in SETTLING:
        live.settle()
    if step.after is not None:
        await_after(live, step)

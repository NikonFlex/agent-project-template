"""Язык шагов: что шаг может сказать, как его разобрать и какие сценарии принять. Без браузера.

Перенос lib/steps.mjs из agent-braille (см. NOTICE): тот же язык, что в docs/STEPS.md и
docs/SCENARIOS.md оригинала. Модуль не импортирует playwright: им пользуются самопроверка,
`python3 -m braille step` на стороне клиента и сценарные проверки.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from braille.settings import Settings


class StepError(ValueError):
    """Шаг не разобран или не сошёлся; текст — причина для читателя ответа."""


class AfterMissed(StepError):  # noqa: N818 — это не ошибка кода, а исход шага
    """Действие выполнено, а его `after` не наступил: страница могла измениться."""


class HostRefused(ValueError):  # noqa: N818
    """Адрес вне разрешённых хостов: исполнитель не запускается."""


class Kind(StrEnum):
    GOTO = "goto"
    CLICK = "click"
    FILL = "fill"
    PRESS = "press"
    UPLOAD = "upload"
    WAIT = "wait"
    ORDER = "order"
    ABSENT = "absent"
    DISABLED = "disabled"
    ENABLED = "enabled"
    FIELD = "field"
    FILLED = "filled"
    CHECKED = "checked"
    UNCHECKED = "unchecked"
    FOCUSED = "focused"
    HOVER = "hover"
    VISIBLE = "visible"
    HIDDEN = "hidden"
    REPEAT = "repeat"
    TAB = "tab"
    REQUESTED = "requested"
    FACT = "fact"


class Form(StrEnum):
    """Как названа цель шага."""

    TEXT = "text"
    TESTID = "testid"
    SELECTOR = "selector"
    NAMED = "named"


#: шаги, после которых страница меняется: им нужен `after`
ACTIONS = (Kind.GOTO, Kind.CLICK, Kind.FILL, Kind.PRESS, Kind.UPLOAD)
#: шаги оригинала, которые мы не переносим, и почему
NOT_CARRIED = {
    "active": "меню с aria-current проверяют шагом wait/order по тексту",
    "described": "описание поля для чтения с экрана не нужно нашим страницам",
    "download": "скачивание файлов не нужно проходу экранов",
    "hold": "задержку ответа ставит тест, а не сценарий (решение при переносе)",
    "same-href": "две ссылки на одно место проверяет тест страницы",
    "cut": "обрыв ответа относится к записи API, которую мы не переносим",
    "advance": "относится к воспроизведению записи API, которое мы не переносим",
    "visibility": "скрытие вкладки не нужно нашим экранам",
}
ALL = (Form.TEXT, Form.TESTID, Form.SELECTOR, Form.NAMED)
FORMS = {
    Kind.CLICK: ALL, Kind.HOVER: ALL, Kind.FILL: ALL,
    Kind.WAIT: (Form.TEXT, Form.TESTID), Kind.ABSENT: (Form.TEXT, Form.TESTID),
    Kind.DISABLED: (Form.TEXT, Form.TESTID), Kind.ENABLED: (Form.TEXT, Form.TESTID),
    Kind.CHECKED: (Form.TEXT, Form.TESTID), Kind.UNCHECKED: (Form.TEXT, Form.TESTID),
    Kind.FOCUSED: (Form.TEXT, Form.TESTID),
    Kind.FIELD: (Form.TEXT, Form.TESTID, Form.SELECTOR),
    Kind.FILLED: (Form.TEXT, Form.TESTID, Form.SELECTOR),
    Kind.VISIBLE: (Form.SELECTOR,), Kind.HIDDEN: (Form.SELECTOR,),
}
#: какие ключи, кроме самого шага, допустимы у каждого вида
EXTRA_KEYS = {
    Kind.GOTO: {"after"}, Kind.CLICK: {"after", "optional"},
    Kind.FILL: {"after", "value", "within", "optional"}, Kind.PRESS: {"after"},
    Kind.UPLOAD: {"after", "to"}, Kind.WAIT: {"within", "optional"}, Kind.FIELD: {"has"},
    Kind.REPEAT: {"until", "max"}, Kind.REQUESTED: {"times", "method"},
}
#: шаги, что смотрят на экран и отвечают без действия; им не нужен `after`
TICKS = (Kind.CHECKED, Kind.UNCHECKED, Kind.FOCUSED)
#: ожидания «что видно», кроме `fact` и `requested`: они не доказывают результат
EXPECTATIONS = (Kind.WAIT, Kind.ORDER, Kind.ABSENT, Kind.DISABLED, Kind.ENABLED, Kind.FIELD,
                Kind.FILLED, Kind.VISIBLE, Kind.HIDDEN, Kind.FACT, Kind.REQUESTED, *TICKS)
REPORTS_ONLY = (Kind.FACT, Kind.REQUESTED)

RE_TESTID = re.compile(r"^[a-z][a-z0-9-]*(\.[a-z0-9-]+)+$")
RE_FILE = re.compile(r"^\w[\w.-]*$")
AFTER_KEYS = ("screen", "state", "shown", "gone", "within")
WITHIN_MAX_S = 1800
TAB_ACTIONS = ("open", "first", "second")
REQUEST_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
REQUESTED_TIMES_MAX = 50
REPEAT_MAX = 40
REPEAT_LIMIT = 200
READY_STATES = ("ready", "empty")
REQUIRED_SCENARIO_FIELDS = ("name", "why", "verifies", "steps")


@dataclass(frozen=True)
class After:
    """Что становится наблюдаемым после действия."""

    screen: str | None
    state: str | None
    shown: tuple[str, ...]
    gone: tuple[str, ...]
    within: int


@dataclass(frozen=True)
class Step:
    kind: Kind
    target: Any
    value: str | None = None
    has: str | None = None
    to: dict | None = None
    optional: bool = False
    within: int | None = None
    times: int | None = None
    method: str | None = None
    until: str | None = None
    limit: int = REPEAT_MAX
    inner: tuple[Step, ...] = ()
    after: After | None = None

    def form(self) -> Form | None:
        return target_form(self.target)


def refuse(reason: str, raw: Any) -> StepError:
    return StepError(f"{reason}: {json.dumps(raw, ensure_ascii=False)}")


# ---------------------------------------------------------------- цели


def is_testid_target(target: Any) -> bool:
    """`{"testid": …}`; ключи testid и один из name, item."""
    if not isinstance(target, dict) or "testid" not in target:
        return False
    if set(target) - {"testid", "name", "item"} or ("name" in target and "item" in target):
        return False
    if not (isinstance(target["testid"], str) and RE_TESTID.match(target["testid"])):
        return False
    return all(isinstance(target[key], str) and target[key].strip()
               for key in ("name", "item") if key in target)


def is_selector_target(target: Any) -> bool:
    return (isinstance(target, dict) and set(target) == {"selector"}
            and isinstance(target["selector"], str) and bool(target["selector"]))


def is_named_target(target: Any) -> bool:
    """По роли и имени, не по CSS: `{"role": "link", "name": "Иванов", "contains": true}`."""
    if not isinstance(target, dict) or {"selector", "testid"} & set(target):
        return False
    if set(target) - {"role", "name", "contains", "pressed"} or not set(target) & {
            "role", "name", "pressed"}:
        return False
    texts = all(isinstance(target[key], str) and target[key] for key in ("role", "name")
                if key in target)
    contains_ok = "contains" not in target or (target["contains"] is True and "name" in target)
    pressed_ok = "pressed" not in target or (target["pressed"] is False and "name" not in target)
    return texts and contains_ok and pressed_ok


def target_form(target: Any) -> Form | None:
    if isinstance(target, str):
        return Form.TEXT if target else None
    if is_testid_target(target):
        return Form.TESTID
    if is_selector_target(target):
        return Form.SELECTOR
    return Form.NAMED if is_named_target(target) else None


def target_label(target: Any) -> str:
    """Как цель названа в сообщении о красном шаге."""
    if isinstance(target, str):
        return target
    if is_testid_target(target):
        extra = f' "{target["name"]}"' if "name" in target else (
            f" [{target['item']}]" if "item" in target else "")
        return target["testid"] + extra
    return str(target.get("name") or target.get("selector") or target.get("role") or target)


# ---------------------------------------------------------------- after


def parse_after(after: Any, raw: Any, default_within: int) -> After:
    if not isinstance(after, dict):
        raise refuse("after — объект", raw)
    if set(after) - set(AFTER_KEYS):
        raise refuse(f"after — только ключи {', '.join(AFTER_KEYS)}", raw)
    if not set(after) & {"screen", "state", "shown", "gone"}:
        raise refuse("after — хотя бы одно из screen, state, shown, gone", raw)
    for key in ("screen", "state"):
        if key in after and not (isinstance(after[key], str) and after[key]):
            raise refuse(f"after.{key} — непустая строка", raw)
    for key in ("shown", "gone"):
        ids = after.get(key)
        if key in after and not (isinstance(ids, list) and ids and all(
                isinstance(i, str) and RE_TESTID.match(i) for i in ids)):
            raise refuse(f"after.{key} — непустой список testid вида <область>.<действие>", raw)
    within = after.get("within", default_within)
    if not (isinstance(within, int) and not isinstance(within, bool)
            and 1 <= within <= WITHIN_MAX_S):
        raise refuse(f"after.within — целое число секунд от 1 до {WITHIN_MAX_S}", raw)
    return After(after.get("screen"), after.get("state"), tuple(after.get("shown", ())),
                 tuple(after.get("gone", ())), within)


# ---------------------------------------------------------------- разбор шага


def _alias_label(raw: Any) -> Any:
    """{"fill": {"label": "Имя"}} — первое, что пишет модель: то же, что имя строкой."""
    if not isinstance(raw, dict):
        return raw
    changed = dict(raw)
    for key in (Kind.CLICK, Kind.HOVER, Kind.FILL, Kind.FIELD, Kind.FILLED):
        target = raw.get(key)
        if isinstance(target, dict) and set(target) == {"label"}:
            changed[key] = target["label"]
    return changed


def _kind_of(raw: Any) -> Kind:
    if not isinstance(raw, dict):
        raise refuse("шаг — объект", raw)
    known = [kind for kind in Kind if kind in raw]
    if not known:
        for name, why in NOT_CARRIED.items():
            if name in raw:
                raise refuse(f"шаг «{name}» не перенесён: {why}", raw)
    if len(known) != 1:
        raise refuse(f"в шаге ровно один из {', '.join(Kind)}", raw)
    extra = set(raw) - {known[0]} - EXTRA_KEYS.get(known[0], set())
    if extra:
        allowed = ", ".join(sorted(EXTRA_KEYS.get(known[0], set()))) or "нет"
        raise refuse(f"ключ {', '.join(sorted(extra))} не относится к шагу {known[0]}; "
                     f"допустимы: {allowed}", raw)
    return known[0]


def _check_target(kind: Kind, target: Any, raw: Any) -> None:
    if kind is Kind.WAIT and isinstance(target, (int, float)) and not isinstance(target, bool):
        raise refuse("пауза по времени запрещена: ждите наблюдаемого — after или wait по тексту",
                     raw)
    allowed = FORMS.get(kind)
    if allowed is not None and target_form(target) not in allowed:
        names = ", ".join(form.value for form in allowed)
        raise refuse(f"цель шага {kind}: {names}", raw)


def _check_text_kinds(kind: Kind, target: Any, raw: Any) -> None:
    if kind is Kind.GOTO and not (isinstance(target, str) and target.startswith("/")):
        raise refuse('goto — путь от корня, с "/"', raw)
    if kind is Kind.PRESS and not (isinstance(target, str) and target):
        raise refuse("press — название клавиши", raw)
    if kind is Kind.UPLOAD and not (isinstance(target, str) and RE_FILE.match(target)):
        raise refuse("upload — имя файла из каталога файлов сценариев, без каталогов", raw)
    if kind is Kind.REQUESTED and not (isinstance(target, str) and target.startswith("/")):
        raise refuse('requested — конец пути запроса с "/"', raw)
    if kind is Kind.TAB and target not in TAB_ACTIONS:
        raise refuse(f"tab — одно из {', '.join(TAB_ACTIONS)}", raw)


def _check_structured(kind: Kind, target: Any, raw: dict) -> None:
    if kind is Kind.ORDER and not (isinstance(target, list) and len(target) >= 2 and all(
            (isinstance(t, str) and t) or is_testid_target(t) for t in target)):
        raise refuse('order — список из двух и больше текстов или {"testid": …}', raw)
    if kind is Kind.FACT and not (isinstance(target, dict) and target and all(
            isinstance(k, str) and isinstance(v, str) and v for k, v in target.items())):
        raise refuse('fact — объект «факт экрана: значение», строки, например {"mode": "preview"}',
                     raw)
    if kind is Kind.FIELD and not (isinstance(raw.get("has"), str) and raw["has"]):
        raise refuse("у field нет has", raw)
    if kind is Kind.FILL and not isinstance(raw.get("value"), str):
        raise refuse("у fill нет value", raw)


def _check_numbers(raw: dict) -> None:
    def whole(key: str, low: int, high: int) -> bool:
        value = raw[key]
        return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high

    if "within" in raw and not whole("within", 1, WITHIN_MAX_S):
        raise refuse(f"within — целое число секунд от 1 до {WITHIN_MAX_S}", raw)
    if "times" in raw and not whole("times", 0, REQUESTED_TIMES_MAX):
        raise refuse(f"times — целое число от 0 до {REQUESTED_TIMES_MAX}", raw)
    if "method" in raw and raw["method"] not in REQUEST_METHODS:
        raise refuse(f"method — одно из {', '.join(REQUEST_METHODS)}", raw)
    if "max" in raw and not whole("max", 1, REPEAT_LIMIT):
        raise refuse(f"max у repeat — целое число от 1 до {REPEAT_LIMIT}", raw)
    if "optional" in raw and raw["optional"] is not True:
        raise refuse("optional — только true", raw)
    if "to" in raw and not is_selector_target(raw["to"]):
        raise refuse('to у upload — только {"selector": …}', raw)


def parse_step(raw: Any, default_within: int = 20) -> Step:
    """Шаг из JSON; нарушение языка — StepError с причиной."""
    raw = _alias_label(raw)
    kind = _kind_of(raw)
    target = raw[kind]
    _check_target(kind, target, raw)
    _check_text_kinds(kind, target, raw)
    _check_structured(kind, target, raw)
    _check_numbers(raw)
    after = parse_after(raw["after"], raw, default_within) if "after" in raw else None
    if kind is Kind.REPEAT:
        return _parse_repeat(target, raw, default_within)
    return Step(kind, target, value=raw.get("value"), has=raw.get("has"), to=raw.get("to"),
                optional=raw.get("optional") is True, within=raw.get("within"),
                times=raw.get("times"), method=raw.get("method"), after=after)


def _parse_repeat(target: Any, raw: dict, default_within: int) -> Step:
    if not (isinstance(target, list) and target and isinstance(raw.get("until"), str)
            and raw["until"]):
        raise refuse("repeat — непустой список шагов и текст в until", raw)
    inner = tuple(parse_step(item, default_within) for item in target)
    if any(step.kind in (Kind.REPEAT, Kind.GOTO) for step in inner):
        raise refuse("внутри repeat нельзя repeat и goto", raw)
    return Step(Kind.REPEAT, target, until=raw["until"], limit=raw.get("max", REPEAT_MAX),
                inner=inner)


# ---------------------------------------------------------------- подстановки


def fill_values(raw: Any, values: dict[str, Any]) -> Any:
    """Шаг с подстановками `{имя}` из --values; в steps.json остаётся запись без значений."""
    text = json.dumps(raw, ensure_ascii=False)
    for name, value in values.items():
        text = text.replace("{" + name + "}", json.dumps(str(value), ensure_ascii=False)[1:-1])
    return json.loads(text)


# ---------------------------------------------------------------- хосты


def check_base(base: str, settings: Settings) -> str:
    """Начало адреса сайта; хост не из разрешённых — отказ до запуска браузера."""
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise HostRefused(f"BASE — не адрес сайта: {base}")
    if parts.hostname not in settings.allowed_hosts:
        raise HostRefused(
            f"BASE={base}: инструмент ходит только на {', '.join(settings.allowed_hosts)}; "
            "прод не открывается, чужой стенд добавляют в BRAILLE_ALLOWED_HOSTS")
    return f"{parts.scheme}://{parts.netloc}"


# ---------------------------------------------------------------- сценарии


def _step_problems(index: int, raw: Any, strict: bool) -> list[str]:
    try:
        step = parse_step(raw)
    except StepError as error:
        return [f"шаг {index}: {error}"]
    problems = []
    if strict and step.kind in ACTIONS and step.after is None:
        problems.append(f"шаг {index}: {step.kind} без after — нужно, что становится видно")
    if strict and step.kind is Kind.CLICK and step.form() is Form.TEXT:
        problems.append(f"шаг {index}: click по подписи; используйте {{\"testid\": …}}")
    return problems


def _shows_directly(steps: list) -> bool:
    """Есть проверка видимого: `fact` и `requested` — отчёт самого приложения и сети."""
    for raw in steps:
        after = raw.get("after") or {}
        if after.get("shown") or after.get("gone"):
            return True
        if any(kind in raw and kind not in REPORTS_ONLY for kind in EXPECTATIONS):
            return True
        if isinstance(raw.get(Kind.REPEAT), list) and _shows_directly(raw[Kind.REPEAT]):
            return True
    return False


def steps_problems(steps: list, strict: bool = True, direct: bool = True) -> list[str]:
    """Причины, по которым список шагов не принять в сценарий; пусто — годен.

    `direct` — требовать проверку видимого; её нет у контрольного сценария, которому
    достаточно ожидания. Так же шаги `ui` читает tools/scenario.py.
    """
    problems = []
    for index, raw in enumerate(steps, 1):
        problems.extend(_step_problems(index, raw, strict))
    dicts = [raw for raw in steps if isinstance(raw, dict)]
    if not any(kind in raw for raw in dicts for kind in EXPECTATIONS):
        problems.append("нет ожидания: сценарий ничего не проверяет")
    elif direct and not _shows_directly(dicts):
        problems.append("нет прямой проверки: fact и state — отчёт самого приложения; "
                        "проверьте видимое через after.shown, after.gone или шаг-проверку")
    return problems


def scenario_problems(scenario: dict, file_name: str | None = None, strict: bool = True,
                      ) -> list[str]:
    """Причины, по которым файл сценария не принять; пусто — сценарий годен."""
    problems = [f"нет поля {name}" for name in REQUIRED_SCENARIO_FIELDS
                if scenario.get(name) is None]
    name = Path(file_name).stem if file_name else None
    if name and scenario.get("name") and scenario["name"] != name:
        problems.append("name не совпадает с именем файла")
    if not isinstance(scenario.get("steps"), list):
        return problems
    control = bool(name) and name.startswith("_")
    if not control and isinstance(scenario.get("verifies"), list) and not scenario["verifies"]:
        problems.append("verifies не называет требование")
    return problems + steps_problems(scenario["steps"], strict, direct=not control)

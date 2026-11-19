"""Вердикты исполнителя: почему ожидание не сбылось и где отчёт страницы расходится с сетью.

Чистые функции без браузера: на них держится самопроверка (`python3 -m braille selftest`).
Перенос из lib/steps.mjs и lib/session.mjs agent-braille (см. NOTICE); тексты — на русском.
"""
from __future__ import annotations

import json
import re
from typing import Any

from braille.steps import READY_STATES, After, target_label

#: слова в теле JSON, которыми ответ с кодом 200 говорит об отказе
FAILED_WORDS = re.compile(r"^(failed|failure|error|rejected|denied|отказ|ошибка)$", re.I)
NAME_LIMIT = 80


def norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


# ---------------------------------------------------------------- цели


def pick_by_testid(infos: list[dict], target: dict) -> list[dict]:
    """Элементы с testid: по имени (точно) или по данным item (имя или текст содержит)."""
    if target.get("name"):
        return [e for e in infos if norm(e.get("name")) == norm(target["name"])]
    if target.get("item"):
        want = norm(target["item"])
        return [e for e in infos if want in norm(e.get("name")) or want in norm(e.get("text"))]
    return infos


def label_problem(infos: list[dict], target: dict) -> str | None:
    """Почему цели с testid нет; None — есть. `infos` — все видимые элементы с этим testid."""
    if pick_by_testid(infos, target):
        return None
    if not infos:
        return f"элемента {target['testid']} нет на странице"
    seen = ", ".join(sorted({f'"{norm(e.get("name"))}"' for e in infos})[:5])
    if target.get("name"):
        return f'подпись {target["testid"]}: {seen}, требование называет "{target["name"]}"'
    return f'{target["testid"]} с "{target["item"]}" нет; есть: {seen}'


def occlusion_problem(info: dict | None) -> str | None:
    """Почему по элементу не нажать: его закрывает другой; None — не закрыт."""
    over = (info or {}).get("occludedBy") or []
    if not over:
        return None
    names = []
    for item in over:
        shown = item.get("name") or item.get("testid") or item.get("tag") or "?"
        names.append(f'"{shown}"' + (f" ({item['testid']})" if item.get("testid") else ""))
    return f'"{info.get("name") or info.get("testid")}" закрыт: {", ".join(names)}'


def cover_line(details: dict | None) -> str:
    """Чем закрыта цель, прочитанное со страницы; пусто, когда данных нет."""
    if not details:
        return ""
    parts = []
    if details.get("element"):
        parts.append(f"в центре цели: {details['element']}, position {details['position']}, "
                     f"{details['size']}, z-index {details['zIndex']}")
    if details.get("inside"):
        parts.append(f'внутри "{details["inside"]}"')
    if details.get("dialogs"):
        parts.append("открыты диалоги: " + ", ".join(f'"{d}"' for d in details["dialogs"]))
    return " — " + "; ".join(parts) if parts else ""


def clip_for(box: dict | None, pad: int = 16) -> dict | None:
    """Область снимка красного шага: рамка элемента с полем, не левее и не выше края."""
    if not box or not (box["width"] > 0 and box["height"] > 0):
        return None
    x, y = max(0, box["x"] - pad), max(0, box["y"] - pad)
    return {"x": x, "y": y, "width": box["width"] + (box["x"] - x) + pad,
            "height": box["height"] + (box["y"] - y) + pad}


# ---------------------------------------------------------------- after и экран


def after_problem(after: After, seen: dict) -> str | None:
    """Почему `after` не наступил; None — наступил. `seen`: screen и testids после шага."""
    misses = []
    screen = seen.get("screen")
    if after.screen and (screen or {}).get("name") != after.screen:
        misses.append(f"экран {after.screen}, а открыт {(screen or {}).get('name') or 'без корня'}")
    if after.state and (screen or {}).get("state") != after.state:
        misses.append(f"состояние {after.state}, а {(screen or {}).get('state') or 'нет'}")
    shown = set(seen.get("testids", ()))
    absent = [i for i in after.shown if i not in shown]
    stays = [i for i in after.gone if i in shown]
    if absent:
        misses.append("не появились: " + ", ".join(absent))
    if stays:
        misses.append("не исчезли: " + ", ".join(stays))
    return f"after не наступил: {'; '.join(misses)}" if misses else None


def observed_after(before: dict, now: dict) -> dict:
    """`after`, взятый из наблюдения: экран, состояние и до трёх появившихся testid."""
    had = set(before.get("testids", ()))
    name = (now.get("screen") or {}).get("name")
    appeared = [i for i in now.get("testids", ()) if i not in had]
    appeared.sort(key=lambda i: 0 if name and i.startswith(f"{name}.") else 1)
    after: dict = {}
    if name:
        after["screen"] = name
    if (now.get("screen") or {}).get("state"):
        after["state"] = now["screen"]["state"]
    if appeared:
        after["shown"] = appeared[:3]
    return after


def screen_problem(observed: dict) -> str | None:
    """Экран в состоянии error — отчёт самой страницы; текст берётся из её сообщений."""
    screen = observed.get("screen")
    if not screen or screen.get("state") != "error":
        return None
    alerts = "; ".join(observed.get("alerts") or ())
    return f"экран {screen['name']} в состоянии error" + (f": {alerts}" if alerts else "")


def screen_pending(observed: dict) -> bool:
    """Ждать дальше: экран есть и ещё не вышел из loading."""
    screen = observed.get("screen")
    return bool(screen) and screen.get("state") not in (*READY_STATES, "error")


# ---------------------------------------------------------------- проверки шагов


def order_lines(items: list[str]) -> list[str]:
    """Многострочный текст — несколько элементов подряд: строки идут отдельными пунктами."""
    return [part.strip() for item in items for part in re.split(r"\n+| · ", item) if part.strip()]


def order_problem(hits: list[list[int]], items: list[str], texts: dict[int, str]) -> str | None:
    """Почему тексты идут не по порядку; None — по порядку. `hits` — domIndex по каждому."""
    start, prev = -1, None
    for index, item in enumerate(items):
        found = hits[index] if index < len(hits) else []
        if not found:
            return f'текста "{item}" нет на странице'
        body = texts.get(start)
        if start in found and isinstance(body, str) and prev is not None:
            if body.find(item, body.find(prev) + len(prev)) >= 0:
                prev = item
                continue
        after = sorted(i for i in found if i > start)
        if not after:
            return f'"{item}" стоит раньше предыдущего текста списка'
        start, prev = after[0], item
    return None


def absent_problem(count: int, alive: bool, item: str) -> str | None:
    if not alive:
        return "страница пуста: отсутствие текста не доказано"
    return f'текст "{item}" есть на странице' if count else None


def state_problem(states: list[bool], item: str, want_disabled: bool) -> str | None:
    """disabled/enabled: `states` — «выключен» у каждого найденного элемента."""
    if not states:
        return f'элемента "{item}" нет на странице'
    if want_disabled:
        return None if all(states) else f'"{item}" доступен'
    return f'"{item}" недоступен' if any(states) else None


def field_problem(values: list[str], text: str) -> str | None:
    if not values:
        return "поля нет на странице"
    if text in values[0]:
        return None
    return f'в поле "{values[0][:80]}", текста "{text}" в нём нет'


def filled_problem(values: list[str]) -> str | None:
    if not values:
        return "поля нет на странице"
    return None if values[0].strip() else "поле пусто"


def tick_problem(ticks: list[dict], item: str, kind: str) -> str | None:
    """checked/unchecked/focused: `ticks` — {checked, focused} у найденных флажков."""
    if not ticks:
        return f'флажка "{item}" нет на странице'
    if len(ticks) > 1:
        return f'флажков "{item}" на странице {len(ticks)}'
    tick = ticks[0]
    if kind == "checked":
        return None if tick["checked"] else f'флажок "{item}" не отмечен'
    if kind == "unchecked":
        return f'флажок "{item}" отмечен' if tick["checked"] else None
    return None if tick["focused"] else f'фокус не на "{item}"'


def seen_problem(count: int, kind: str) -> str | None:
    if kind == "visible":
        return None if count else "по селектору нет видимого элемента"
    return None if count == 0 else f"видимых элементов по селектору: {count}"


def requested_problem(count: int, times: int | None, suffix: str) -> str | None:
    """requested: без `times` нужен хотя бы один запрос, с `times` — ровно столько."""
    if times is None:
        return None if count >= 1 else f'запросов на "{suffix}" не было'
    return None if count == times else f'запросов на "{suffix}": {count}, ждали {times}'


def fact_problem(facts: dict[str, str], wanted: dict[str, str]) -> str | None:
    wrong = {name: value for name, value in wanted.items() if facts.get(name) != value}
    if not wrong:
        return None
    pairs = ", ".join(f"{name}={value}" for name, value in wrong.items())
    return f"факт экрана: ждали {pairs}, а на экране {json.dumps(facts, ensure_ascii=False)}"


# ---------------------------------------------------------------- ожидание сети


def wait_is_over(failed: int, in_flight: int, busy: int, quiet_ms: float, grace_ms: int) -> bool:
    """Больше ничего не случится: запрос упал и не повторён, в пути ничего, сеть тиха весь срок."""
    return failed > 0 and in_flight == 0 and busy == 0 and quiet_ms >= grace_ms


def call_label(call: dict) -> str:
    return f"{call['method']} {call['path']}{call.get('query', '')} {call['status']}"


def is_failed(status: Any) -> bool:
    """Код 400 и выше или обрыв сети (статус — текст причины)."""
    return not isinstance(status, int) or status >= 400


# ---------------------------------------------------------------- противоречия


def body_says_failed(text: str) -> str | None:
    """Ответ 200, что сам говорит об отказе; только верхний уровень JSON."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("ok") is False:
        return "ok: false"
    if data.get("success") is False:
        return "success: false"
    if isinstance(data.get("status"), str) and FAILED_WORDS.match(data["status"]):
        return f"status: {data['status']}"
    if data.get("error"):
        return "error: " + json.dumps(data["error"], ensure_ascii=False)[:80]
    for key in ("errors", "failed", "skipped"):
        if isinstance(data.get(key), list) and data[key]:
            return f"{key}: {len(data[key])}"
    return None


def network_contradictions(calls: list[dict]) -> list[str]:
    """Что сеть шага говорит сама: отказ в теле при коде 200, одно и то же тело дважды."""
    found, sent = [], {}
    for call in calls:
        if call["method"] == "GET":
            continue
        name = f"{call['method']} {call['path']}"
        key = (name, call.get("sent", ""))
        sent[key] = sent.get(key, 0) + 1
        verdict = body_says_failed(call.get("body", "")) if not is_failed(call["status"]) else None
        if verdict:
            found.append(f"{name} {call['status']}, но тело говорит {verdict}")
    found.extend(f"{name} отправлен {count} раза с одним телом"
                 for (name, _), count in sent.items() if count > 1)
    return found


def contradictions_of(observed: dict, busy: list[str], failed: list[str]) -> list[str]:
    """Где отчёт страницы расходится с сетью и с собой. `failed` — запросы шага с кодом 400+."""
    screen = observed.get("screen")
    if not screen:
        return []
    state, found = screen.get("state"), []
    if state in READY_STATES:
        found.extend(f"состояние {state}, а {call}" for call in failed)
    if state == "error":
        found.append(
            f"состояние error на весь экран; упали только: {', '.join(failed)}. "
            "Проверьте, относятся ли они к тому, что вы проверяете" if failed
            else "состояние error, а запросов с отказом в шаге не было")
    if state == "loading" and not busy:
        found.append("состояние loading, а страница ничем не занята")
    if state == "empty":
        for name, value in (observed.get("facts") or {}).items():
            if re.fullmatch(r"\d+", str(value)) and int(value) > 0:
                found.append(f"состояние empty, а факт {name}={value}")
    return found


# ---------------------------------------------------------------- ответ шага


def compact_observed(now: dict, before: dict | None = None) -> dict:
    """Ответ шага: что изменилось с прошлого раза; `unchanged` — сколько осталось прежним."""
    rest = {key: value for key, value in now.items() if key != "testids"}
    if before is None:
        return rest
    unchanged = 0
    for key in ("elements", "fields", "labels"):
        if key not in rest:
            continue
        old = {json.dumps(item, sort_keys=True) for item in before.get(key, ())}
        fresh = [i for i in rest[key] if json.dumps(i, sort_keys=True) not in old]
        unchanged += len(rest[key]) - len(fresh)
        rest[key] = fresh
    if rest.get("text") and rest["text"] == before.get("text"):
        del rest["text"]
    return {**rest, "unchanged": unchanged} if unchanged else rest


def step_name(index: int, kind: str, target: Any) -> str:
    """Имя шага для файлов и отчёта: `03-click-orders-tab`."""
    label = ", ".join(map(str, target)) if isinstance(target, list) else target_label(target)
    tail = re.sub(r"[^\w]+", "-", str(label), flags=re.U).strip("-")[:40]
    return f"{index + 1:02d}-{kind}-{tail or 'x'}"


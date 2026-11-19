"""Что оставляет красный шаг: причина, запросы, ошибки страницы, снимок рядом; маскировка секретов.

Перенос lib/evidence.mjs agent-braille (см. NOTICE). Модуль чистый: данные страницы приходят
готовыми, браузера и сети здесь нет, поэтому его проверяет самопроверка.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode

from braille.settings import Settings

#: поля, без которых набор улик неполон
REQUIRED = ("url", "text", "screenshot", "load")
TIMEOUT = re.compile(r"Timeout (\d+)ms exceeded")


class Masker:
    """Прячет пароли и токены: по имени поля, по виду значения и по самому секрету входа."""

    def __init__(self, settings: Settings, secrets: tuple[str, ...] = ()) -> None:
        masks = settings.masks
        self.replacement = masks["replacement"]
        self.names = settings.mask_names()
        self.patterns = [re.compile(source, re.I) for source in masks["patterns"]]
        self.secrets = [secret for secret in secrets if len(secret) >= 6]
        self.limits = settings.limits

    def text(self, text: Any) -> str:
        out = str(text if text is not None else "")
        for secret in self.secrets:
            out = out.replace(secret, self.replacement)
        for pattern in self.patterns:
            out = pattern.sub(self.replacement, out)
        return out

    def fields(self, data: Any) -> Any:
        """Значения полей вида password/token/secret/cookie заменены, остальное цело."""
        if isinstance(data, dict):
            return {key: self.replacement if self.names.search(str(key)) else self.fields(value)
                    for key, value in data.items()}
        if isinstance(data, list):
            return [self.fields(item) for item in data]
        return self.text(data) if isinstance(data, str) else data

    def body(self, text: str | None) -> str:
        """Тело запроса или ответа для отчёта: JSON и форма — по полям, прочее — по виду."""
        raw = text or ""
        limit = self.limits["body"]
        try:
            return json.dumps(self.fields(json.loads(raw)), ensure_ascii=False)[:limit]
        except ValueError:
            pass
        if re.fullmatch(r"[^\s=&]+=[^\s]*(&[^\s=&]+=[^\s]*)*", raw):
            pairs = parse_qsl(raw, keep_blank_values=True)
            return urlencode(self.fields(dict(pairs)), safe="*")[:limit]
        if raw.startswith("--") or "\x00" in raw:
            return f"(файл или составное тело, {len(raw)} знаков)"
        return self.text(raw)[:limit]

    def query(self, query: str) -> str:
        """Строка запроса адреса: `?a=b`, секреты закрыты; без запроса — пусто."""
        pairs = parse_qsl(query, keep_blank_values=True)
        if not pairs:
            return ""
        shown = "&".join(f"{k}={v}" for k, v in self.fields(dict(pairs)).items())
        return "?" + shown[: self.limits["query"]]


def bare_url(url: str) -> str:
    """Адрес без запроса и якоря: в них бывают токены."""
    return re.sub(r"[?#].*$", "", str(url or ""))


def build_evidence(raw: dict, masker: Masker) -> dict:
    """Улики из сырых данных страницы: по пределам и под маской. Чистая функция."""
    limits = masker.limits

    def line(value: Any) -> str:
        return masker.text(value)[: limits["line"]]

    def lines(items: Any, cap: int) -> list[str]:
        return [line(item) for item in list(items or ())[:cap]]

    return {
        "url": masker.text(bare_url(raw.get("url"))),
        "title": line(raw.get("title")),
        "text": masker.text(re.sub(r"\s+\n", "\n", str(raw.get("text") or "")).strip())[
            : limits["text"]],
        "console": lines(raw.get("console"), limits["console"]),
        "pageerrors": lines(raw.get("pageerrors"), limits["pageerrors"]),
        "requests": [
            {"call": line(call)} for call in list(raw.get("requests") or ())[: limits["requests"]]],
        "screenshot": raw.get("screenshot") or "",
        "screen": raw.get("screen"),
        "busy": lines([f"{k}={n}" for k, n in (raw.get("busy") or {}).items() if n],
                      limits["console"]),
        "target": raw.get("target"),
        "load": {"readyState": (raw.get("load") or {}).get("readyState"),
                 "agent": (raw.get("load") or {}).get("agent")},
    }


def evidence_problems(evidence: dict | None) -> list[str]:
    """Чего не хватает в уликах; пусто — набор полон."""
    if not isinstance(evidence, dict):
        return ["улик нет"]
    problems = [f"нет поля {name}" for name in REQUIRED
                if evidence.get(name) in (None, "", {}, [])]
    load = evidence.get("load") or {}
    problems.extend(f"нет load.{name}" for name in ("readyState", "agent") if name not in load)
    return problems


def screen_summary(evidence: dict) -> str:
    """Экран одной строкой: имя, состояние, слои поверх, факты, чем занята страница."""
    screen = evidence.get("screen")
    if not screen:
        return ("window.__agent есть, экрана с data-state нет" if evidence["load"].get("agent")
                else "window.__agent нет")
    parts = [f"экран {screen['name']} data-state={screen['state']}"]
    if screen.get("overlays"):
        parts.append("поверх: " + ", ".join(screen["overlays"]))
    if screen.get("alerts"):
        parts.append("сообщения: " + "; ".join(screen["alerts"]))
    if evidence.get("busy"):
        parts.append("занято: " + ", ".join(evidence["busy"]))
    if evidence.get("target") is not None:
        parts.append("цель: " + ("; ".join(evidence["target"]) or "на экране нет"))
    return " | ".join(parts)


def timeout_reason(error: str, what: str, evidence: dict) -> str:
    """Таймаут Playwright — не причина: причину называет экран и то, чего ждал шаг."""
    found = TIMEOUT.search(error or "")
    if not found:
        return error
    rest = " — ".join(error.split(" — ")[1:])
    head = (f"{what} не сбылось за {round(int(found.group(1)) / 1000)} с; "
            f"{screen_summary(evidence)}")
    return " — ".join(part for part in (head, rest) if part)[:600]


def target_line(infos: list[dict]) -> list[str]:
    """Цель шага глазами window.__agent.find: роль, имя, недоступна ли, закрыта ли."""
    lines = []
    for info in infos[:3]:
        flags = [info.get("aria", {}).get("disabled") and "недоступна",
                 info.get("inViewport") is False and "вне окна",
                 info.get("occludedBy") and "закрыта: " + ", ".join(
                     o.get("name") or o.get("testid") or o.get("tag", "?")
                     for o in info["occludedBy"])]
        head = f'{info.get("role") or "?"} "{info.get("name")}"'
        if info.get("testid"):
            head += f" {info['testid']}"
        lines.append(", ".join(str(x) for x in [head, *flags] if x))
    return lines

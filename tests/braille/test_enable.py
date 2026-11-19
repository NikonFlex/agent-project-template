"""Включение window.__agent: без AGENT_API=1 сайт не отдаёт agent.js и не ставит его в
страницу, с ним — отдаёт и ставит (braille/__init__.py).

Браузер не нужен: маленькое приложение FastAPI со страницей из шаблона Jinja, как в README
пакета («Новому сайту»). Без fastapi и jinja2 тест пропускается.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("jinja2")
pytest.importorskip("httpx")  # TestClient

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import braille  # noqa: E402

SCRIPT_TAG = f'<script src="{braille.SCRIPT_URL}"></script>'
BASE = ("<html><head>{% if agent_api %}" + SCRIPT_TAG + "{% endif %}</head>"
        "<body>страница</body></html>")


def site(tmp_path) -> TestClient:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "base.html").write_text(BASE, encoding="utf-8")
    app, templates = FastAPI(), Jinja2Templates(directory=str(tmp_path))
    braille.install(app, templates)

    @app.get("/")
    def page(request: Request):
        return templates.TemplateResponse(request, "base.html")

    return TestClient(app)


@pytest.mark.parametrize("value", [None, "0", "true", ""])
def test_off(tmp_path, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(braille.ENV_FLAG, raising=False)
    else:
        monkeypatch.setenv(braille.ENV_FLAG, value)
    client = site(tmp_path)
    assert SCRIPT_TAG not in client.get("/").text
    assert client.get(braille.SCRIPT_URL).status_code == 404


def test_on(tmp_path, monkeypatch):
    monkeypatch.setenv(braille.ENV_FLAG, "1")
    client = site(tmp_path)
    assert SCRIPT_TAG in client.get("/").text
    script = client.get(braille.SCRIPT_URL)
    assert script.status_code == 200
    assert "window.__agent" in script.text
    assert script.headers["cache-control"] == "no-cache"


def test_off_after_on(tmp_path, monkeypatch):
    """Шаблоны общие: приложение, собранное позже без переменной, не наследует включённое."""
    monkeypatch.setenv(braille.ENV_FLAG, "1")
    site(tmp_path / "on")
    monkeypatch.delenv(braille.ENV_FLAG)
    assert SCRIPT_TAG not in site(tmp_path).get("/").text

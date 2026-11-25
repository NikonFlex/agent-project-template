"""Промпты ролей: границы записи, формат артефакта и запрет публикации.

Промпт — часть протокола прогона; контроллер проверяет результат файлом, а не словами модели.
"""

from __future__ import annotations

from dataclasses import dataclass

from tools.rundo.config import Role

PLAN_SCHEMA = """{
  "requirements": [{"id": "R1", "text": "..."}],
  "steps": [{"text": "...", "requirementIds": ["R1"]}],
  "spec": {"refs": ["openspec/specs/..."], "gap": false},
  "paths": [{"path": "src/example.py", "purpose": "implementation"}],
  "contract": "known | new | unknown",
  "red": {"nodeId": "tests/test_....py::test_name", "marker": "буквальный текст падения"}
}"""


@dataclass(frozen=True)
class PromptContext:
    number: str
    worktree: str
    exchange: str
    request: str


def _preamble(role: Role, context: PromptContext) -> str:
    return (
        f"Ты — роль «{role.value}» Рундо для задачи #{context.number}. "
        f"Работай только в дереве {context.worktree}.\n"
        f"Каталог обмена прогона: {context.exchange}.\n"
        "Запрещено: GitHub, коммиты, push, выкладка, чтение секретов, правки вне своей роли.\n\n"
    )


def plan_prompt(context: PromptContext) -> str:
    return _preamble(Role.PLAN, context) + (
        "Прочитай постановку и применимую спеку, затем запиши plan.json ровно такой формы:\n"
        f"{PLAN_SCHEMA}\n\n"
        "Требования — из постановки, каждый шаг ссылается на их R-ID и покрывает все.\n"
        "paths — точные относительные пути с назначением spec, task, test или implementation.\n"
        "red.nodeId — точный pytest node ID существующего или будущего теста, который\n"
        "напишет следующий DeepSeek на этапе красной проверки; red.marker — непустая\n"
        "буквальная строка, которую тестировщик заложит в ожидаемое падение. Оба поля\n"
        "обязательны и не могут быть пустыми. Если решение подтверждено и спека требует\n"
        "правки — приведи спеку в соответствие по docs/shared/SDD.md и укажи её в paths.\n"
        "Не пиши тест и реализацию. Постановка:\n"
        f"{context.request}\n"
    )


def gemini_prompt(context: PromptContext) -> str:
    return _preamble(Role.GEMINI, context) + (
        "Проверь plan.json по правилам проекта: требования из постановки, исполнимость шагов,\n"
        "достаточность проверок, честность spec.gap и границы путей. Прочитай gate.json:\n"
        "в нём причина, по которой тебя позвали, и SHA-256 плана.\n"
        "Запиши gemini.json: {\"planSha256\": <из gate.json>, \"verdict\": \"approved\" |\n"
        "\"changes_requested\" | \"rejected\", \"findings\": [{\"severity\", \"area\",\n"
        "\"summary\", \"detail\"}]}. Одобряй только тот план, который действительно видел.\n"
        "Файлы дерева не меняй — только gemini.json.\n"
    )


def red_prompt(context: PromptContext) -> str:
    return _preamble(Role.RED, context) + (
        "Прочитай plan.json и напиши только объявленный тест для red.nodeId.\n"
        "Тест должен падать до реализации с буквальной строкой red.marker в сообщении.\n"
        "Не создавай и не правь реализацию, спеку или задачу; не подгоняй тест под код.\n"
    )


def implementation_prompt(context: PromptContext) -> str:
    return _preamble(Role.IMPLEMENTATION, context) + (
        "Прочитай plan.json и gemini.json, если он существует. Реализуй шаги плана в "
        "объявленных путях. Перед однотипными правками проверь готовые генераторы.\n"
        "Файл теста не меняй ни на байт: контроллер сверяет его хеш.\n"
        "Тест должен пройти, `sh tools/check.sh --quick` — завершиться успехом.\n"
        "Ничего не коммить; правки — только в worktree.\n"
    )


def draft_prompt(context: PromptContext) -> str:
    return _preamble(Role.DRAFT, context) + (
        "Прочитай plan.json, приёмку и логи проверок. Запиши pr.json:\n"
        "{\"title\": \"<Conventional Commits>\", \"body\": \"<Markdown>\"}.\n"
        f"В теле отдельной строкой — `Part of #{context.number}` или "
        f"`Closes #{context.number}` по\n"
        "docs/agents/tasks.md; другие слова закрытия запрещены. Ничего не публикуй:\n"
        "черновик остаётся локальным файлом.\n"
    )


BUILDERS = {
    Role.PLAN: plan_prompt,
    Role.GEMINI: gemini_prompt,
    Role.RED: red_prompt,
    Role.IMPLEMENTATION: implementation_prompt,
    Role.DRAFT: draft_prompt,
}


def build(role: Role, context: PromptContext) -> str:
    return BUILDERS[role](context)

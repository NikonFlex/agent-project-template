"""Правила по темам — одним текстом для трёх сред (AGENTS.md, «Код, спеки, задачи»).

    python3 tools/agent_rules.py            проверить: копии совпадают с docs/agents/
    python3 tools/agent_rules.py --write    пересобрать копии
    python3 tools/agent_rules.py --selftest

Источник — `docs/agents/<тема>.md`. Копии с заголовком своей среды:
`.claude/rules/<тема>.md` (Claude Code подгружает по `paths`) и `.cursor/rules/<тема>.mdc`
(Cursor — по `globs`). Codex подгружать по путям не умеет: AGENTS.md велит прочитать сам
файл из `docs/agents/`. Копии руками не правят — правят источник и пересобирают.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from script_common import ROOT, report, write_files

SOURCE = Path("docs/agents")
#: Тема → пути, при работе с которыми правило подгружается. Одно объявление на обе среды.
TOPICS = {
    "code": ["**/*.py", "**/*.toml", "requirements*.txt"],
    "specs": ["openspec/**", "docs/incoming/**", "docs/examples/project/**"],
    "tasks": ["docs/tasks/**"],
}
NOTE = ("<!-- Копия docs/agents/{topic}.md — правится там, "
        "пересборка: tools/agent_rules.py --write -->")


def describe(body: str) -> str:
    """Первый абзац после заголовка — описание правила для Cursor."""
    paragraphs = [p for p in body.split("\n\n")[1:] if p.strip()]
    return " ".join(paragraphs[0].split()) if paragraphs else ""


def claude_copy(topic: str, body: str) -> str:
    paths = "\n".join(f'  - "{p}"' for p in TOPICS[topic])
    return f"---\npaths:\n{paths}\n---\n\n{NOTE.format(topic=topic)}\n\n{body}"


def cursor_copy(topic: str, body: str) -> str:
    description = describe(body).replace('"', "'")
    globs = ",".join(TOPICS[topic])
    return (f'---\ndescription: "{description}"\nglobs: {globs}\nalwaysApply: false\n---\n\n'
            f"{NOTE.format(topic=topic)}\n\n{body}")


def expected(root: Path) -> dict[Path, str]:
    out = {}
    for topic in TOPICS:
        body = (root / SOURCE / f"{topic}.md").read_text(encoding="utf-8")
        out[Path(".claude/rules") / f"{topic}.md"] = claude_copy(topic, body)
        out[Path(".cursor/rules") / f"{topic}.mdc"] = cursor_copy(topic, body)
    return out


def run(root: Path, write: bool) -> list[str]:
    problems = []
    for path, text in expected(root).items():
        target = root / path
        if write:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        elif not target.exists() or target.read_text(encoding="utf-8") != text:
            problems.append(f"{path} не совпадает с {SOURCE}/ — пересобрать: --write")
    return problems


def selftest() -> int:
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        root = write_files(Path(tmp), {f"{SOURCE}/{t}.md": f"# {t}\n\nПро {t}.\n" for t in TOPICS})
        run(root, write=True)
        if problems := run(root, write=False):
            bad.append(f"после сборки копии не совпадают: {problems}")
        claude = (root / ".claude/rules/code.md").read_text(encoding="utf-8")
        cursor = (root / ".cursor/rules/code.mdc").read_text(encoding="utf-8")
        if '  - "**/*.py"' not in claude or "globs: **/*.py," not in cursor \
                or 'description: "Про code."' not in cursor:
            bad.append("заголовок копии не тот")
        (root / ".cursor/rules/tasks.mdc").write_text("правка руками", encoding="utf-8")
        (root / SOURCE / "specs.md").write_text("# specs\n\nИначе.\n", encoding="utf-8")
        found = run(root, write=False)
        if len(found) != 3:
            bad.append(f"правка копии и источника не пойманы: {found}")
    return report(bad, "сборка, заголовки сред, правка копии руками и правка источника")


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    problems = run(ROOT, write="--write" in sys.argv)
    for problem in problems:
        print(problem)
    print("правила сред совпадают с источником" if not problems else f"нарушений: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

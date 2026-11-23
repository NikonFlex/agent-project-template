"""Дерево тредов bb: роли, статусы, сумма токенов по веткам, примерная цена ($ / % окна).

    python3 tools/bb_tree.py                  треды текущего проекта (read-only: bb thread list,
                                              bb thread context)
    python3 tools/bb_tree.py --project <id>   треды одного проекта
    python3 tools/bb_tree.py --input <json>   фикстура вместо живого bb (офлайн/тест)
    python3 tools/bb_tree.py --html <out>     дополнительно записать HTML (inline-vis)
    python3 tools/bb_tree.py --json           дерево машиночитаемым JSON вместо текста
    python3 tools/bb_tree.py --selftest       проверка на фиктивном JSON, без сети

Роль — из префикса заголовка `[coder]`, `[tester]`, `[reviewer]`, `[research]`. Токены —
`bb thread context <id> --json` (`usedTokens`), окно — `modelContextWindow`. Цена: API-модели
(DeepSeek/GLM) — `$` по таблице `config/bb_models.json` (примерная, из каталога провайдера),
подписочные (Codex/Antigravity) — `%` окна. Цена считается по сумме токенов ветки.

Фикстура `--input` — JSON: `{"threads": [<как `bb thread list --json`>, ...],
"usage": {"<threadId>": {"usedTokens": N, "modelContextWindow": N, "model": "…"}}}`.
`model` у треда/в usage не обязателен — без него цена по провайдеру.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import bb_cli
from script_common import ROOT, report

CONFIG_PATH = ROOT / "config/bb_models.json"
ROLE_RE = re.compile(r"^\[(\w+)\]")


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def run(args: list[str]) -> tuple[bool, dict]:
    """Read-only команда `bb … --json`; (ок, разобранный JSON)."""
    done = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603
    try:
        data = json.loads(done.stdout)
    except json.JSONDecodeError:
        return False, {}
    return done.returncode == 0 and not (isinstance(data, dict) and data.get("ok") is False), data


def load_live(project_id: str | None) -> dict:
    args = ["bb", "thread", "list", "--json"]
    if project_id:
        args += ["--project", project_id]
    ok, threads = run(args)
    if not ok or not isinstance(threads, list):
        raise SystemExit("bb thread list --json не дал список тредов")
    usage: dict[str, dict] = {}
    for thread in threads:
        _, context = run(["bb", "thread", "context", thread["id"], "--json"])
        if isinstance(context.get("usage"), dict):
            usage[thread["id"]] = context["usage"]
    return {"threads": threads, "usage": usage}


def load_fixture(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data.get("threads"), list):
        raise SystemExit("фикстура без списка «threads»")
    return data


def role(title: str | None) -> str:
    """Роль из префикса `[coder]`; без префикса — «—»."""
    match = ROLE_RE.match(title or "")
    return match.group(1) if match else "—"


def label(node: Node) -> str:
    """Заголовок без префикса роли; пустой — id."""
    return ROLE_RE.sub("", node.title).strip() or node.id


@dataclass
class Node:
    id: str
    title: str
    role: str
    status: str
    provider: str
    model: str
    tokens: int
    window: int
    children: list[Node] = field(default_factory=list)

    def subtree_tokens(self) -> int:
        return self.tokens + sum(c.subtree_tokens() for c in self.children)


def price(config: dict, provider: str, model: str, tokens: int, window: int) -> str:
    """`$N` для API-моделей, `N%` для подписочных, «—» если цена неизвестна."""
    if provider in config["subscription_providers"]:
        w = window or config["window_default_tokens"]
        return f"{tokens / w:.0%}"
    per_1m = (config["api_price_usd_per_1m"].get(model)
              or config["api_price_usd_per_1m"].get(provider))
    if not per_1m:
        return "—"
    return f"${tokens * per_1m / 1_000_000:.2f}"


def build(threads: list[dict], usage: dict, config: dict) -> list[Node]:
    """Лес деревьев: узлы на каждый тред, родители связаны, корни — без родителя в наборе."""
    nodes = {}
    for t in threads:
        u = usage.get(t["id"], {}) or {}
        nodes[t["id"]] = Node(
            id=t["id"], title=t.get("title") or "", role=role(t.get("title")),
            status=t.get("status") or "—", provider=t.get("providerId") or "—",
            model=t.get("model") or u.get("model") or "",
            tokens=int(u.get("usedTokens") or 0), window=int(u.get("modelContextWindow") or 0))
    roots = []
    for t in threads:
        node, parent = nodes[t["id"]], nodes.get(t.get("parentThreadId"))
        roots.append(node) if parent is None else parent.children.append(node)
    return roots


def _lines(node: Node, config: dict, prefix: str, is_last: bool, out: list[str]) -> None:
    total = node.subtree_tokens()
    out.append(f"{prefix}{'└── ' if is_last else '├── '}{node.role} {label(node)} · "
               f"{node.status} · {node.provider} · {total:,} ток · "
               f"{price(config, node.provider, node.model, total, node.window)}")
    child_prefix = prefix + ("    " if is_last else "│   ")
    for i, child in enumerate(node.children):
        _lines(child, config, child_prefix, i == len(node.children) - 1, out)


def render_text(roots: list[Node], config: dict) -> str:
    lines: list[str] = []
    for i, node in enumerate(roots):
        _lines(node, config, "", i == len(roots) - 1, lines)
    return "\n".join(lines)


def _dict(node: Node, config: dict) -> dict:
    total = node.subtree_tokens()
    return {
        "id": node.id, "title": label(node), "role": node.role, "status": node.status,
        "provider": node.provider, "model": node.model, "tokens": node.tokens,
        "subtree_tokens": total,
        "price": price(config, node.provider, node.model, total, node.window),
        "children": [_dict(c, config) for c in node.children],
    }


def render_json(roots: list[Node], config: dict) -> str:
    return json.dumps([_dict(n, config) for n in roots], ensure_ascii=False, indent=2)


def _html(node: Node, config: dict, rows: list, depth: int) -> None:
    total = node.subtree_tokens()
    rows.append((depth, node.role, label(node),
                 f"{node.status} · {total:,} ток · "
                 f"{price(config, node.provider, node.model, total, node.window)}"))
    for child in node.children:
        _html(child, config, rows, depth + 1)


def render_html(roots: list[Node], config: dict) -> str:
    rows: list[tuple[int, str, str, str]] = []
    for node in roots:
        _html(node, config, rows, 0)
    body = "".join(f"<div style='padding-left:{d * 24}px'><b>{r}</b> {t} · "
                   f"<code>{m}</code></div>" for d, r, t, m in rows)
    return f"<!doctype html><meta charset='utf-8'><body style='font-family:sans-serif'>{body}"


# --------------------------------------------------------------------------- selftest

FIXTURE = {
    "threads": [
        {"id": "root", "providerId": "codex", "title": "[coder] корень", "status": "active",
         "parentThreadId": None},
        {"id": "kid", "providerId": "acp-opencode", "title": "[tester] ребёнок", "status": "idle",
         "parentThreadId": "root", "model": "deepseek/deepseek-v4-pro"},
        {"id": "lone", "providerId": "acp-antigravity", "title": "без роли", "status": "active",
         "parentThreadId": "missing"},
    ],
    "usage": {
        "root": {"usedTokens": 50000, "modelContextWindow": 200000},
        "kid": {"usedTokens": 1_000_000, "modelContextWindow": 200000},
        "lone": {"usedTokens": 100_000, "modelContextWindow": 400000},
    },
}


def _role_cases() -> list[str]:
    bad = []
    if role("[coder] Волна") != "coder" or role("без префикса") != "—":
        bad.append("роль из префикса не та")
    return bad


def _price_cases(config: dict) -> list[str]:
    bad = []
    for provider, model, tokens, window, wanted in [
        ("codex", "", 50000, 200000, "25%"),
        ("acp-opencode", "deepseek/deepseek-v4-pro", 1_000_000, 200000, "$1.98"),
        ("acp-opencode", "", 1_000_000, 200000, "$1.32"),
        ("unknown", "", 1000, 200000, "—"),
    ]:
        got = price(config, provider, model, tokens, window)
        if got != wanted:
            bad.append(f"цена {provider}/{model} = {got}, ждали {wanted}")
    return bad


def selftest() -> int:
    config = load_config()
    bad = _role_cases() + _price_cases(config)
    roots = build(FIXTURE["threads"], FIXTURE["usage"], config)
    if len(roots) != 2:
        bad.append(f"корней ждали 2 (root и lone), вышло {len(roots)}")
    root = next(n for n in roots if n.id == "root")
    if len(root.children) != 1 or root.children[0].id != "kid":
        bad.append("ребёнок kid не привязан к root")
    if root.subtree_tokens() != 1_050_000 or root.children[0].subtree_tokens() != 1_000_000:
        bad.append("сумма токенов по ветке не сошлась")
    text = render_text(roots, config)
    if "tester ребёнок" not in text or "1,050,000 ток" not in text:
        bad.append("текстовое дерево без роли/суммы токенов")
    tree = json.loads(render_json(roots, config))
    if tree[0]["subtree_tokens"] != 1_050_000:
        bad.append("JSON-дерево без суммы по ветке")
    if "<!doctype html>" not in render_html(roots, config):
        bad.append("HTML без каркаса")
    return report(bad, "роль, дерево, сумма токенов, цена API ($) и подписки (%), текст/JSON/HTML")


def _add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default=os.environ.get("BB_PROJECT_ID"),
                        help="id проекта (по умолчанию — текущий)")
    parser.add_argument("--input", help="фикстура JSON вместо живого bb")
    parser.add_argument("--html", metavar="OUT", help="записать HTML")
    parser.add_argument("--json", action="store_true", help="машиночитаемый вывод")


def _body(args: argparse.Namespace) -> int:
    data = load_fixture(args.input) if args.input else load_live(args.project)
    config = load_config()
    roots = build(data["threads"], data.get("usage", {}), config)
    print(render_json(roots, config) if args.json else render_text(roots, config))
    if args.html:
        Path(args.html).write_text(render_html(roots, config), encoding="utf-8")
        print(f"HTML: {args.html}")
    return 0


def main() -> int:
    return bb_cli.main("Дерево тредов bb", selftest, _add_args, _body)


if __name__ == "__main__":
    sys.exit(main())

"""Подставной `bb` для красных проверок Рундо (`tests/test_rundo.py`).

Контракт фикстуры: тест кладёт в PATH исполняемый файл `bb`, который вызывает
`main(argv, state_path, log_path, knobs)`. Все команды пишутся строкой JSON в `log_path`.

Подставной BB:
- `thread spawn` создаёт отдельный тред, запоминает окружение и делает работу роли:
  `plan` → `plan.json`, `gemini` → `gemini.json`, `red` → объявленный тест,
  `implementation` → объявленный файл, `draft` → `pr.json` в каталоге обмена
  `<worktree>/tmp/rundo/<номер>/`;
- `plugin rpc call bb-jev … --input-file` возвращает квитанцию Jev v2;
- `thread list/show/wait/context/log`, `environment show`, `provider models` отдают
  то, что спросит контроллер.

Ручки (`knobs`) для коротких контрольных случаев:
  `paths` — список `{"path", "purpose"}` вместо плана по умолчанию;
  `contract` — `"known"|"new"|"unknown"`;
  `spec_gap` — `bool`; `incomplete` — `"red"` (план без красной проверки);
  `gemini_sha` — `"wrong"` (чужой хеш плана);
  `acceptance_mode` — `"stale"|"foreign"` (устаревшая или чужая квитанция);
  `pr_body` — подмена тела черновика.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path


def _path(name: str, purpose: str) -> dict:
    """Элемент плана `paths`: относительный путь и его назначение."""
    return {"path": name, "purpose": purpose}


DEFAULT_PATHS = [
    _path("demo.py", "implementation"),
    _path("tests/test_demo.py", "test"),
    _path("issues/demo-0042.md", "task"),
    _path("openspec/specs/demo/spec.md", "spec"),
    _path("docs/dev-plan.md", "spec"),
]

REQUIREMENTS = [
    {"id": "R1", "text": "Одна команда запускает прогон в чистом worktree."},
    {"id": "R2", "text": "План фиксирует требования, спеку, пути и красную проверку."},
    {"id": "R3", "text": "Gemini ревьюит план при сработавшем условии и на том же хеше."},
    {"id": "R4", "text": "Контроллер подтверждает ожидаемое красное падение."},
    {"id": "R5", "text": "Реализация проходит ту же проверку и быструю проверку проекта."},
    {"id": "R6", "text": "Jev v2 связывает требования, план и приёмку с ревизией."},
    {"id": "R7", "text": "Черновик PR пишет отдельная роль после текущей приёмки."},
]

STEPS = [
    {"text": "Собрать план и проверить его структуру.", "requirementIds": ["R1", "R2"]},
    {"text": "Объяснить необходимость Gemini и сверить хеш.", "requirementIds": ["R3"]},
    {"text": "Подтвердить красное падение до кода.", "requirementIds": ["R4"]},
    {"text": "Дождаться зелёной проверки и быстрой проверки.", "requirementIds": ["R5"]},
    {"text": "Связать стадии Jev и написать черновик PR.", "requirementIds": ["R6", "R7"]},
]

RED_NODE = "tests/test_demo.py::test_add_sums"
RED_MARKER = "RED-MARKER: demo.add must exist"

TEST_FILE = '''import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def {name}() -> None:
    try:
        from demo import add
    except ImportError:
        add = None
    assert add is not None, {marker!r}
    assert add(2, 3) == 5
'''

IMPLEMENTATION_FILE = "def add(a: int, b: int) -> int:\n    return a + b\n"

PR_BODY = "## Что\n\nДобавить demo.add.\n\nPart of #42\n"


def _load(path: str) -> dict:
    file = Path(path)
    return json.loads(file.read_text(encoding="utf-8")) if file.is_file() else {}


def _save(path: str, state: dict) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_suffix(file.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temporary.replace(file)


def _log(path: str, entry: dict) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _opt(argv: list[str], flag: str) -> str | None:
    """Значение флага или None, если флага нет или за ним пусто."""
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def _write(path: Path, text: str) -> None:
    """Записать текст, создав родительские каталоги."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _write_json(path: Path, value: dict) -> None:
    _write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _exchange(worktree: Path) -> Path:
    """Каталог обмена, согласованный с планом: `<worktree>/tmp/rundo/<номер>/`."""
    root = worktree / "tmp" / "rundo"
    runs = sorted(path for path in root.glob("*") if path.is_dir())
    if not runs:
        raise SystemExit(f"fake bb: нет каталога обмена {root}/<номер>")
    return runs[0]


def _plan(knobs: dict) -> dict:
    paths = knobs.get("paths") or DEFAULT_PATHS
    plan = {
        "requirements": REQUIREMENTS,
        "steps": STEPS,
        "spec": {
            "refs": [item["path"] for item in paths if item["purpose"] == "spec"],
            "gap": bool(knobs.get("spec_gap", False)),
        },
        "paths": paths,
        "contract": knobs.get("contract", "known"),
        "red": {"nodeId": RED_NODE, "marker": RED_MARKER},
    }
    if knobs.get("incomplete") == "red":
        plan.pop("red")
    return plan


def _role(state: dict, provider: str, model: str) -> str:
    if "gemini" in model:
        return "gemini"
    if provider == "codex" or "gpt" in model:
        return "plan"
    if "deepseek" in model:
        number = int(state.get("deepseek_spawns") or 0) + 1
        state["deepseek_spawns"] = number
        return {1: "red", 2: "implementation", 3: "draft"}.get(number, f"deepseek_{number}")
    return "unknown"


def _do_role(role: str, exchange: Path, worktree: Path, knobs: dict) -> None:
    plan_file = exchange / "plan.json"
    if role == "plan":
        _write_json(plan_file, _plan(knobs))
    elif role == "gemini":
        plan_sha = hashlib.sha256(plan_file.read_bytes()).hexdigest()
        if knobs.get("gemini_sha") == "wrong":
            plan_sha = "0" * 64
        _write_json(exchange / "gemini.json", {
            "planSha256": plan_sha,
            "verdict": knobs.get("gemini_verdict", "approved"),
            "findings": [],
        })
    elif role == "red":
        red = _read_json(plan_file)["red"]
        relative, _, name = red["nodeId"].partition("::")
        _write(worktree / relative, TEST_FILE.format(name=name, marker=red["marker"]))
    elif role == "implementation":
        for item in _read_json(plan_file)["paths"]:
            if item["purpose"] == "implementation":
                _write(worktree / item["path"], IMPLEMENTATION_FILE)
    elif role == "draft":
        _write_json(exchange / "pr.json", {
            "title": "feat(demo): добавить demo.add",
            "body": knobs.get("pr_body", PR_BODY),
        })


def _parent_thread(knobs: dict) -> dict:
    return {
        "id": knobs.get("parent", "thr_parent"),
        "status": "active",
        "title": "[main] родитель",
        "projectId": knobs.get("project", "proj_fake"),
        "environmentId": "env_parent",
        "providerId": "codex",
        "parentThreadId": None,
        "visibility": "visible",
        "archivedAt": None,
    }


def _spawn(argv: list[str], state: dict, knobs: dict) -> dict:
    value = _opt(argv, "--environment")
    if not value:
        raise SystemExit("fake bb: у spawn нет --environment")
    resolved = str(Path(value).resolve())
    environments = state.setdefault("environments", {})
    by_path = state.setdefault("environment_paths", {})
    environment_id = by_path.get(resolved)
    if environment_id is None and Path(value).is_dir():
        environment_id = f"env_{len(environments) + 1}"
        environments[environment_id] = resolved
        by_path[resolved] = environment_id
    if environment_id is None:
        environment_id = value
    worktree = Path(environments.get(environment_id, value))

    provider = _opt(argv, "--provider") or ""
    model = _opt(argv, "--model") or ""
    role = _role(state, provider, model)
    _do_role(role, _exchange(worktree), worktree, knobs)

    number = int(state.get("next_thread") or 0) + 1
    state["next_thread"] = number
    thread = {
        "id": f"thr_child_{number}",
        "status": "idle",
        "title": _opt(argv, "--title") or "",
        "projectId": _opt(argv, "--project") or "",
        "environmentId": environment_id,
        "providerId": provider,
        "model": model,
        "parentThreadId": _opt(argv, "--parent-thread"),
        "visibility": _opt(argv, "--visibility"),
        "permissionMode": _opt(argv, "--permission-mode"),
        "role": role,
        "archivedAt": None,
    }
    state.setdefault("threads", []).append(thread)
    return {**thread, "environment": {"id": environment_id, "path": str(worktree)}}


def _threads(state: dict, knobs: dict) -> list[dict]:
    return [*state.get("threads", []), _parent_thread(knobs)]


def _thread_list(argv: list[str], state: dict, knobs: dict) -> list[dict]:
    threads = _threads(state, knobs)
    project = _opt(argv, "--project")
    parent = _opt(argv, "--parent-thread")
    wanted: list[str] = []
    for index, item in enumerate(argv):
        if item == "--thread" and index + 1 < len(argv):
            wanted.append(argv[index + 1])
    if project:
        threads = [thread for thread in threads if thread["projectId"] == project]
    if parent:
        threads = [thread for thread in threads if thread["parentThreadId"] == parent]
    if wanted:
        threads = [thread for thread in threads if thread["id"] in wanted]
    return threads


def _thread_show(thread_id: str, state: dict, knobs: dict) -> dict | None:
    thread = next((item for item in _threads(state, knobs) if item["id"] == thread_id), None)
    if thread is None:
        return None
    environment_id = thread.get("environmentId")
    path = state.get("environments", {}).get(environment_id or "")
    environment = ({"id": environment_id, "path": path, "projectId": thread["projectId"],
                    "status": "ready"} if path else None)
    return {"thread": thread, "environment": environment, "pendingTodos": []}


def _environment_show(value: str, state: dict) -> dict | None:
    environments = state.get("environments", {})
    if value in environments:
        return {"id": value, "path": environments[value], "status": "ready"}
    resolved = str(Path(value).resolve())
    for environment_id, path in environments.items():
        if path == resolved:
            return {"id": environment_id, "path": path, "status": "ready"}
    return None


def _models(provider: str | None) -> list[dict]:
    catalog = {
        "codex": ["gpt-6.2-sol"],
        "acp-antigravity": ["gemini-4-neon"],
        "acp-opencode": ["deepseek/deepseek-flash"],
    }
    models = catalog.get(provider or "", [])
    return [{"id": model, "model": model, "displayName": model, "isDefault": index == 0}
            for index, model in enumerate(models)]


def _receipt(method: str, payload: dict, state: dict, knobs: dict) -> dict:
    stage = method.rsplit(".", 1)[-1]
    inner = payload.get("input") or {}
    counts = state.setdefault("rpc_counts", {})
    number = int(counts.get(stage) or 0) + 1
    counts[stage] = number
    review_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                               f"rundo-fake-jev:{stage}:{number}:{payload.get('threadId')}"))
    parent = None
    revision = None
    freshness = "current"
    if stage == "plan":
        parent = inner.get("requirementsReviewId")
    elif stage == "acceptance":
        parent = inner.get("planReviewId")
        revision = inner.get("revision")
        mode = knobs.get("acceptance_mode")
        if mode == "stale":
            freshness = "stale"
        elif mode == "foreign":
            parent = "11111111-1111-4111-8111-111111111111"
    digest = hashlib.sha256(json.dumps(inner, ensure_ascii=False, sort_keys=True)
                            .encode("utf-8")).hexdigest()
    receipt = {
        "reviewId": review_id,
        "threadId": payload.get("threadId"),
        "parentReviewId": parent,
        "stage": stage,
        "status": "passed",
        "reason": None,
        "model": "jev-1.14.0",
        "decisions": {stage: {"status": "passed", "confidence": 0.9,
                              "probabilities": {"passed": 0.9, "failed": 0.1}}},
        "inputTokens": 1,
        "durationMs": 1,
        "revision": revision,
        "evidenceDigest": digest,
        "freshness": freshness,
    }
    state.setdefault("receipts", {})[review_id] = receipt
    return receipt


def _receipt_lookup(payload: dict, state: dict, knobs: dict) -> dict:
    review_id = payload.get("reviewId")
    receipt = state.get("receipts", {}).get(review_id)
    if receipt is None:
        return {"error": {"code": "not_found", "message": review_id}}
    current = dict(receipt)
    if current["stage"] == "acceptance":
        mode = knobs.get("acceptance_mode")
        current["freshness"] = "stale" if mode == "stale" else "current"
        if mode == "foreign":
            current["parentReviewId"] = "11111111-1111-4111-8111-111111111111"
    return current


def _rpc(argv: list[str], state: dict, knobs: dict) -> dict:
    plugin, method = argv[3], argv[4]
    if plugin not in {"bb-jev", "bb-jevstice"}:
        raise SystemExit(f"fake bb: неизвестный плагин {plugin!r}")
    input_file = _opt(argv, "--input-file")
    if not input_file:
        raise SystemExit("fake bb: rpc без --input-file")
    payload = json.loads(Path(input_file).read_text(encoding="utf-8"))
    if plugin == "bb-jevstice" and method == "jevstice.session.on":
        assert payload["threadId"] == knobs["parent"]
        assert "demo.add" in payload["request"]
        assert not state.get("threads")
        return {"active": True}
    if method == "jev.workflow.receipt":
        return _receipt_lookup(payload, state, knobs)
    return _receipt(method, payload, state, knobs)


_UNKNOWN = object()


def _thread_command(argv: list[str], state: dict, knobs: dict) -> object:
    if argv[:2] == ["thread", "spawn"]:
        return _spawn(argv, state, knobs)
    if argv[:2] == ["thread", "list"]:
        return _thread_list(argv, state, knobs)
    if argv[:2] == ["thread", "show"]:
        return _thread_show(argv[2] if len(argv) > 2 else "", state, knobs)
    if argv[:2] == ["thread", "wait"]:
        thread_id = argv[2] if len(argv) > 2 else ""
        return {"threadId": thread_id, "matched": True,
                "target": _opt(argv, "--status") or "idle"}
    if argv[:2] == ["thread", "log"]:
        thread_id = argv[2] if len(argv) > 2 else ""
        return [{"id": f"evt_{thread_id}", "seq": 1, "type": "turn/completed",
                 "threadId": thread_id, "scope": "thread", "data": {}}]
    if argv[:2] == ["thread", "context"]:
        return {"usage": {"usedTokens": 0, "modelContextWindow": 200000}}
    return _UNKNOWN


def _dispatch(argv: list[str], state: dict, knobs: dict) -> object:
    result = _thread_command(argv, state, knobs)
    if result is not _UNKNOWN:
        return result
    if argv[:2] == ["environment", "show"]:
        value = argv[2] if len(argv) > 2 else ""
        environment = _environment_show(value, state)
        if environment is None:
            raise SystemExit(f"fake bb: нет окружения {value!r}")
        return environment
    if argv[:2] == ["provider", "models"]:
        provider = argv[2] if len(argv) > 2 and not argv[2].startswith("-") else None
        return _models(provider)
    if argv[:3] == ["plugin", "rpc", "call"]:
        return _rpc(argv, state, knobs)
    raise SystemExit(f"fake bb: неизвестная команда {argv}")


def main(argv: list[str], state_path: str, log_path: str, knobs: dict | None = None) -> int:
    """Точка входа подставного BB: журнал, состояние и разбор одной команды."""
    settings = dict(knobs or {})
    state = _load(state_path)
    _log(log_path, {"argv": list(argv)})
    try:
        result = _dispatch(list(argv), state, settings)
    finally:
        _save(state_path, state)
    if result is not None:
        print(json.dumps(result, ensure_ascii=False))
    return 0

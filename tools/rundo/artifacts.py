"""Артефакты ролей: план, gate.json, gemini.json, pr.json и квитанции Jev.

Сверка структуры — здесь и только здесь: переход не принимается, пока файл не разобран.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.rundo.config import (
    CONTRACTS,
    GATE_PATH_THRESHOLD,
    GEMINI_CONTRACTS,
    MAX_EVIDENCE,
    PURPOSES,
    TEST_ID,
    normalize_path,
)

MAX_ITEMS = 12
MAX_ITEM_TEXT = 1_000
GEMINI_VERDICTS = frozenset({"approved", "changes_requested", "rejected"})
PR_LINKS = ("Part of ", "Closes ")


class ArtifactError(ValueError):
    pass


@dataclass(frozen=True)
class RedNode:
    node_id: str
    path: str
    marker: str


def require(condition: object, message: str) -> None:
    if not condition:
        raise ArtifactError(message)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ArtifactError(f"нет файла {path}") from error
    except json.JSONDecodeError as error:
        raise ArtifactError(f"{path}: не JSON ({error})") from error
    require(isinstance(value, dict), f"{path}: нужен JSON-объект")
    return value


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_plan(plan: dict[str, Any]) -> None:
    ids = _plan_requirements(plan)
    _plan_steps(plan, ids)
    _plan_spec(plan)
    _plan_paths(plan)
    require(plan.get("contract") in CONTRACTS, f"contract={plan.get('contract')!r} вне набора")
    _plan_red(plan)


def _plan_requirements(plan: dict[str, Any]) -> set[str]:
    items = plan.get("requirements")
    require(isinstance(items, list) and items, "plan.requirements пуст")
    require(len(items) <= MAX_ITEMS, f"plan.requirements: больше {MAX_ITEMS} пунктов")
    ids: set[str] = set()
    for item in items:
        require(isinstance(item, dict), "plan.requirements: пункт не объект")
        identifier = item.get("id")
        require(isinstance(identifier, str) and bool(TEST_ID.fullmatch(identifier)),
                f"plan.requirements: чужой id {identifier!r}")
        require(isinstance(item.get("text"), str) and bool(item["text"].strip()),
                f"plan.requirements[{identifier}]: пустой текст")
        require(len(item["text"]) <= MAX_ITEM_TEXT,
                f"plan.requirements[{identifier}]: текст длиннее {MAX_ITEM_TEXT}")
        require(identifier not in ids, f"plan.requirements: повтор {identifier}")
        ids.add(identifier)
    return ids


def _plan_steps(plan: dict[str, Any], ids: set[str]) -> None:
    steps = plan.get("steps")
    require(isinstance(steps, list) and steps, "plan.steps пуст")
    require(len(steps) <= MAX_ITEMS, f"plan.steps: больше {MAX_ITEMS} шагов")
    linked: set[str] = set()
    for step in steps:
        require(isinstance(step, dict), "plan.steps: шаг не объект")
        require(isinstance(step.get("text"), str) and bool(step["text"].strip()),
                "plan.steps: пустой текст шага")
        require(len(step["text"]) <= MAX_ITEM_TEXT,
                f"plan.steps: текст длиннее {MAX_ITEM_TEXT}")
        refs = step.get("requirementIds")
        require(isinstance(refs, list) and refs, "plan.steps: шаг без requirementIds")
        require(len(refs) <= MAX_ITEMS, f"plan.steps: больше {MAX_ITEMS} ссылок")
        for reference in refs:
            require(reference in ids, f"plan.steps: ссылка на чужое требование {reference!r}")
            linked.add(reference)
    missing = sorted(ids - linked)
    require(not missing, f"plan.steps не покрывают требования {missing}")


def _plan_spec(plan: dict[str, Any]) -> None:
    spec = plan.get("spec")
    require(isinstance(spec, dict), "plan.spec не объект")
    refs = spec.get("refs")
    require(isinstance(refs, list), "plan.spec.refs не список")
    require(all(isinstance(ref, str) and bool(ref.strip()) for ref in refs),
            "plan.spec.refs: пустая ссылка")
    require(isinstance(spec.get("gap"), bool), "plan.spec.gap не bool")


def _plan_paths(plan: dict[str, Any]) -> None:
    paths = plan.get("paths")
    require(isinstance(paths, list) and paths, "plan.paths пуст")
    purposes: set[str] = set()
    for item in paths:
        require(isinstance(item, dict), "plan.paths: пункт не объект")
        try:
            normalize_path(item.get("path"))
        except ValueError as error:
            raise ArtifactError(f"plan.paths: {error}") from error
        require(item.get("purpose") in PURPOSES,
                f"plan.paths: назначение {item.get('purpose')!r} вне набора")
        purposes.add(item["purpose"])
    require("test" in purposes, "plan.paths: нет пути с назначением test")
    require("implementation" in purposes, "plan.paths: нет пути с назначением implementation")


def _plan_red(plan: dict[str, Any]) -> None:
    red = plan.get("red")
    require(isinstance(red, dict), "plan.red нет — красная проверка не объявлена")
    require(isinstance(red.get("nodeId"), str) and "::" in red["nodeId"],
            "plan.red.nodeId не вида path::test")
    require(isinstance(red.get("marker"), str) and bool(red["marker"].strip()),
            "plan.red.marker пуст")


def red_node(plan: dict[str, Any]) -> RedNode:
    _plan_red(plan)
    red = plan["red"]
    relative, _, name = red["nodeId"].partition("::")
    try:
        path = normalize_path(relative)
    except ValueError as error:
        raise ArtifactError(f"plan.red.nodeId: {error}") from error
    require(bool(name.strip()), "plan.red.nodeId: нет имени теста")
    return RedNode(node_id=red["nodeId"], path=path, marker=red["marker"])


def unique_path_count(paths: list[dict[str, Any]]) -> int:
    return len({normalize_path(item["path"]) for item in paths})


def planned_paths(plan: dict[str, Any]) -> list[str]:
    return [normalize_path(item["path"]) for item in plan["paths"]]


def paths_with_purpose(plan: dict[str, Any], purpose: str) -> set[str]:
    return {
        normalize_path(item["path"])
        for item in plan["paths"] if item["purpose"] == purpose
    }


def build_gate(plan: dict[str, Any], plan_sha256: str) -> dict[str, Any]:
    count = unique_path_count(plan["paths"])
    reasons: list[str] = []
    if count >= GATE_PATH_THRESHOLD:
        reasons.append(f"paths>={GATE_PATH_THRESHOLD}: уникальных путей {count}")
    if plan["contract"] in GEMINI_CONTRACTS:
        reasons.append(f"contract={plan['contract']}")
    if plan["spec"]["gap"]:
        reasons.append("spec.gap=true")
    return {
        "planSha256": plan_sha256,
        "uniquePaths": count,
        "contract": plan["contract"],
        "specGap": plan["spec"]["gap"],
        "geminiRequired": bool(reasons),
        "reasons": reasons,
    }


def validate_gemini(document: dict[str, Any], plan_sha256: str) -> str:
    require(document.get("planSha256") == plan_sha256,
            "gemini.json относится к другому плану: чужой planSha256")
    verdict = document.get("verdict")
    require(verdict in GEMINI_VERDICTS, f"gemini.json: вердикт {verdict!r} вне набора")
    require(isinstance(document.get("findings"), list), "gemini.json: findings не список")
    return str(verdict)


def validate_pr(document: dict[str, Any], number: str) -> None:
    title = document.get("title")
    body = document.get("body")
    require(isinstance(title, str) and bool(title.strip()), "pr.json: пустой title")
    require(isinstance(body, str) and bool(body.strip()), "pr.json: пустое тело")
    link = f"#{number}"
    linked = any(
        line.strip().startswith(PR_LINKS) and link in line for line in body.splitlines()
    )
    require(linked, f"pr.json: в теле нет строки Part of {link} или Closes {link}")


def validate_receipt(
    receipt: dict[str, Any], stage_name: str, parent: str | None,
    revision: str | None = None,
) -> None:
    require(receipt.get("reviewId"), f"Jev {stage_name}: нет reviewId")
    require(receipt.get("stage") == stage_name,
            f"Jev {stage_name}: квитанция стадии {receipt.get('stage')!r}")
    require(receipt.get("status") == "passed",
            f"Jev {stage_name}: статус {receipt.get('status')!r} ({receipt.get('reason')!r})")
    require(receipt.get("parentReviewId") == parent,
            f"Jev {stage_name}: чужой родитель {receipt.get('parentReviewId')!r}")
    if revision is not None:
        require(receipt.get("revision") == revision,
                f"Jev {stage_name}: ревизия {receipt.get('revision')!r} не принятая {revision}")
        freshness = receipt.get("freshness")
        require(freshness in (None, "current"),
                f"Jev {stage_name}: свежесть {freshness!r}, нужна current")


TRUNCATION_NOTE = "\n... evidence truncated; full log stays in the run directory\n"


def truncate(text: str) -> str:
    """Выдержка в пределах схемы Jev; полный лог остаётся в файле прогона."""
    value = text.strip()
    if len(value) <= MAX_EVIDENCE:
        return value
    return value[: MAX_EVIDENCE - len(TRUNCATION_NOTE)].rstrip() + TRUNCATION_NOTE


def canonical_requirements(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Требования после zod `.trim()` — как их видит TypeSafe."""
    return [{"id": str(item["id"]), "text": str(item["text"]).strip()} for item in items]


def canonical_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"text": str(step["text"]).strip(),
         "requirementIds": [str(value) for value in step["requirementIds"]]}
        for step in steps
    ]


def canonical_digest(value: object) -> str:
    """SHA-256 JSON.stringify из плагина: порядок ключей, без пробелов, Unicode как есть."""
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def submitted_digest(value: object) -> str:
    """Отпечаток отправленного конверта: так его считает офлайн-подставной bb-jev."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_evidence(
    receipt: dict[str, Any], stage_name: str, canonical: object, envelope: object,
) -> None:
    actual = receipt.get("evidenceDigest")
    expected = {canonical_digest(canonical), submitted_digest(envelope)}
    require(actual in expected,
            f"Jev {stage_name}: evidenceDigest не совпал с нормализованным входом")

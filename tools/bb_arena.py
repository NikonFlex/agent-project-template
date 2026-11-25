"""Арена моделей на задачах репозитория: план прогона (в т.ч. A/B с Jev), скорёр worktree и матрица.

    python3 tools/bb_arena.py --plan [--suite smart|full] [--variant baseline|jev] [--json]
    python3 tools/bb_arena.py --run --variant baseline [--suite smart|full] [--task <id>]
        [--provider <p>] [--model <m>] [--limit N] [--jobs N] [--out-dir tmp/arena-runs]
    python3 tools/bb_arena.py --score --worktree <dir> --task <id> --provider <p> --model <m> \\
        [--variant baseline] [--crap] --out <result.json>
    python3 tools/bb_arena.py --collect <dir> [--json]
    python3 tools/bb_arena.py --selftest

Три этапа: планирование (задачи × модели × варианты `baseline`/`jev`), прогон (две волны:
`baseline` при `bb plugin disable bb-jev`, затем `jev` при `bb plugin enable bb-jev`), скоринг
worktree (`--score` пишет `<run>.json`) и сбор сводной матрицы (`--collect`).

Набор ячеек задаёт `--suite`: `smart` (по умолчанию, если в конфиге есть `suites.smart`) —
правила по ролям из `suites`, `full` — декартово произведение `variants × tasks × providers`.
Связка `pipeline` — два шага в одном worktree: планировщик пишет `tmp/plan.md`, кодер правит
по плану; в матрице связка — `provider="pipeline"`, `model=<id связки>`, в result-json `steps`.

Способ оценки задаёт `mode` задачи в `config/bb_arena.json`:

- `code` (по умолчанию) — скрытые тесты из `reference_commit`, `ruff`, `import-linter`,
  дисциплина зоны (правки только в `zone`, тесты, задача и спеки — всегда можно);
- `tests` — тесты пишет агент; проверяются на верном коде (зелёные) и на мутантах из
  `mutants` (каждый обязан упасть), успех — все мутанты убиты;
- `review` — агент ревьюит `patch` и пишет `report_path` с разделами «Находки» и «Снятые
  подозрения»; успех — все `expected` в находках, ни одна `decoy` не выдана за находку.

`--crap` добавляет CRAP изменённых функций: прогоняет тесты с покрытием филиалов в копии
worktree и считает по radon; без флага `crap_max` — `None`, чтобы не выдавать ноль за качество.

`--run` поднимает треды выбранной волны (`bb thread spawn`), ждёт `idle`, оценивает worktree
через `--score` и печатает матрицу. Волны не смешиваются: перед `baseline` нужен
`bb plugin disable bb-jev`, перед `+jev` — `enable`. Платный прогон — сначала `--limit 1`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from time import monotonic

import bb_cli
from script_common import ROOT, git_env, output, report

CONFIG_PATH = ROOT / "config/bb_arena.json"
# Провайдер связки в плане и матрице: у прогона два треда, поэтому один «псевдопровайдер».
PIPELINE_PROVIDER = "pipeline"
DEFAULT_VARIANTS = [{"id": "baseline", "jev": False, "prompt_suffix": ""}]
REQUIRED = ["task", "type", "provider", "model", "success", "hidden_tests_passed",
            "hidden_tests_total", "ruff_clean", "import_linter_clean",
            "diff_lines", "discipline_ok", "duration_seconds", "turns", "tokens", "cost_usd"]
PYTEST_PASSED = re.compile(r"(\d+)\s+passed")
PYTEST_FAILED = re.compile(r"(\d+)\s+(?:failed|error)")


def load_config(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _spawn(provider: str, model: str, prompt: str, title: str, *,
           base_commit: str | None = None, environment: str | None = None) -> list[str]:
    """`bb thread spawn`: новый worktree от базового коммита или тред в готовом окружении."""
    project = os.environ.get("BB_PROJECT_ID", "<project-id>")
    cmd = ["bb", "thread", "spawn", "--project", project,
           "--provider", provider, "--model", model]
    if environment:
        cmd += ["--environment", environment]
    else:
        cmd += ["--base-branch", base_commit, "--new-environment", "worktree"]
    return [*cmd, "--title", title, "--prompt", prompt]


def _variant_suffix(run: dict) -> str:
    variant = run.get("variant", "baseline")
    return "" if variant == "baseline" else f" +{variant}"


def spawn_cmd(run: dict) -> list[str]:
    """`bb thread spawn` для одного прогона: чистый worktree от базового коммита."""
    return _spawn(run["provider"], run["model"], run["prompt"],
                  f"arena {run['task']} {run['provider']}{_variant_suffix(run)}",
                  base_commit=run["base_commit"])


def pipeline_code_spawn(run: dict, environment: str) -> list[str]:
    """Шаг 2 связки: кодер в окружении планировщика, без нового worktree и базовой ветки."""
    pipeline = run["pipeline"]
    coder = pipeline["coder"]
    return _spawn(coder["provider"], coder["model"], run["code_prompt"],
                  f"arena {run['task']} {pipeline['id']} code{_variant_suffix(run)}",
                  environment=environment)


def _task_prompt(task: dict) -> str:
    """Промпт задачи; патч ревью — текстом внутри: в worktree от base_commit файла патча нет."""
    prompt = task["prompt"]
    if task.get("patch"):
        diff = (ROOT / task["patch"]).read_text(encoding="utf-8")
        prompt += f"\n\nПатч:\n```diff\n{diff}```\n"
    return prompt


PLAN_TASK = ("Изучи задачу, спеки и код и запиши короткий план в tmp/plan.md. Укажи результат, "
             "источники требований, обязательные ограничения, рекомендуемый путь и проверки. "
             "Для каждого неоднозначного правила приведи вход и ожидаемый результат, "
             "который отличит выбранное поведение от другой трактовки. "
             "Файлы и функции — подсказки для старта. Детализируй рискованные контракты, сбои "
             "и совместимость данных. Файлы репозитория не меняй — только план.")
PLAN_JEV = " Перед записью плана вызови jev_check_plan по требованиям и плану."
PLAN_JEV_V2 = (" Перед реализацией вызови jev_review_requirements (запрос и список требований "
               "R1..Rn), затем jev_review_plan с полученным requirementsReviewId и шагами, "
               "привязанными к ID требований.")
JEV_PLAN_FILE = " Запиши planReviewId строкой `planReviewId: <UUID>` в начале tmp/plan.md."
CODE_TAIL = ("\n\nПрочитай tmp/plan.md. Требования и критерии приёмки обязательны. Сверь "
             "предположения плана с кодом. Если они расходятся, выбери другой путь реализации "
             "в рамках требований и объясни существенное отклонение в отчёте. Если требования "
             "противоречат друг другу или нужно менять наблюдаемое поведение, остановись на этом "
             "месте и сообщи конкретное расхождение со ссылками. "
             "По выполненным правкам прогони тесты.")
CODE_JEV = " Перед завершением вызови jev_acceptance по диффу и выводу тестов."
CODE_JEV_V2 = (" После правок и прогона тестов вызови jev_review_acceptance с planReviewId. "
               "В diff дай реальные фрагменты патча по каждому R1..Rn, в testOutput — названия "
               "проверок и фактический вывод по этим же ID. В каждом поле максимум 12000 символов. "
               "Если доказательства нет или фрагмент опущен, отметь это рядом с ID. Используй уже "
               "полученный вывод тестов для текущего диффа. Для revision сначала включи новые "
               "файлы в git diff через git add -N, затем возьми SHA-256 из команды "
               "`{ git rev-parse HEAD && git diff --binary HEAD --; } | sha256sum`. "
               "Запиши итоговый acceptance reviewId в tmp/jev-review-id.txt.")


def _plan_prompt(task: dict, variant: dict) -> str:
    suffix = (PLAN_JEV_V2 + JEV_PLAN_FILE if variant.get("jev_v2")
              else (PLAN_JEV if variant.get("jev") else ""))
    return _task_prompt(task) + "\n\n" + PLAN_TASK + suffix


def _code_prompt(task: dict, variant: dict) -> str:
    suffix = (" Возьми planReviewId из tmp/plan.md." + CODE_JEV_V2 if variant.get("jev_v2")
              else (CODE_JEV if variant.get("jev") else ""))
    return _task_prompt(task) + CODE_TAIL + suffix


def _run_base(task: dict, variant: dict, base: str | None) -> dict:
    return {"task": task["id"], "type": task.get("type", "—"),
            "variant": variant["id"], "jev": bool(variant.get("jev", False)),
            "base_commit": task.get("base_commit", base),
            "reference_commit": task.get("reference_commit"),
            "hidden_tests": task.get("hidden_tests", []),
            "zone": task.get("zone", [])}


def _single_run(task: dict, variant: dict, entry: dict, base: str | None) -> dict:
    suffix = (PLAN_JEV_V2 + CODE_JEV_V2 if variant.get("jev_v2")
              else variant.get("prompt_suffix", ""))
    run = {**_run_base(task, variant, base), "provider": entry["provider"],
           "model": entry["model"], "tier": entry.get("tier", ""),
           "prompt": _task_prompt(task) + suffix}
    run["spawn"] = spawn_cmd(run)
    return run


def _pipeline_run(task: dict, variant: dict, pipeline: dict, base: str | None) -> dict:
    """Связка: в плане — `provider="pipeline"`, `model=<id>`, оба промпта и спавн шага 1."""
    plan_prompt = _plan_prompt(task, variant)
    run = {**_run_base(task, variant, base), "provider": PIPELINE_PROVIDER,
           "model": pipeline["id"], "tier": "связка", "prompt": plan_prompt,
           "pipeline": pipeline, "plan_prompt": plan_prompt,
           "code_prompt": _code_prompt(task, variant)}
    planner = pipeline["planner"]
    run["spawn"] = _spawn(planner["provider"], planner["model"], plan_prompt,
                          f"arena {run['task']} {pipeline['id']} plan{_variant_suffix(run)}",
                          base_commit=run["base_commit"])
    return run


def _select_variants(config: dict, variant_filter: str | None) -> list[dict]:
    variants = config.get("variants") or DEFAULT_VARIANTS
    if not variant_filter:
        return variants
    selected = [v for v in variants if v["id"] == variant_filter]
    if not selected:
        known = ", ".join(v["id"] for v in variants)
        raise ValueError(f"нет варианта {variant_filter!r}: в конфиге {known}")
    return selected


def _resolve_suite(config: dict, suite: str | None) -> str | None:
    """Набор ячеек; `None` — декартово произведение, то есть набор `full`."""
    suites = config.get("suites") or {}
    if suite is None:
        return "smart" if "smart" in suites else None
    if suite == "full":
        return None
    if suite in suites:
        return suite
    raise ValueError(f"нет набора {suite!r}: доступны full, {', '.join(suites)}")


def _lookup(items: dict[str, dict], key: str, what: str) -> dict:
    if key not in items:
        raise ValueError(f"в {what} нет {key!r}")
    return items[key]


def _suite_plan(config: dict, suite: str, variants: list[dict],
                base: str | None) -> list[dict]:
    """Правила набора: задача × (модели и связки) × свои варианты, без декартова раздувания."""
    tasks = {t["id"]: t for t in config.get("tasks", [])}
    models = {e["model"]: e for e in config.get("providers", [])}
    pipelines = {p["id"]: p for p in config.get("pipelines", [])}
    runs: list[dict] = []
    for rule in config["suites"][suite]:
        rule_variants = [v for v in variants if v["id"] in rule.get("variants", [])]
        for task_id in rule["tasks"]:
            task = _lookup(tasks, task_id, "tasks")
            for model in rule.get("models", []):
                entry = _lookup(models, model, "providers")
                runs += [_single_run(task, v, entry, base) for v in rule_variants]
            for pipeline_id in rule.get("pipelines", []):
                pipeline = _lookup(pipelines, pipeline_id, "pipelines")
                runs += [_pipeline_run(task, v, pipeline, base) for v in rule_variants]
    return runs


def plan(config: dict, variant_filter: str | None = None,
         suite: str | None = None) -> list[dict]:
    """Прогоны выбранного набора (`smart` по умолчанию при наличии, иначе `full`).

    `full` — задачи × модели × варианты; у каждого прогона команда spawn, у связки — шаг 1."""
    variants = _select_variants(config, variant_filter)
    base = config.get("base_commit")
    resolved = _resolve_suite(config, suite)
    if resolved is not None:
        return _suite_plan(config, resolved, variants, base)
    return [_single_run(task, v, entry, base)
            for v in variants
            for task in config.get("tasks", [])
            for entry in config.get("providers", [])]


def validate(result: dict) -> list[str]:
    return [field for field in REQUIRED if field not in result]


def summary(results: list[dict]) -> dict:
    """Сводка по ячейке матрицы; пустая ячейка — нули, без деления на ноль."""
    n = len(results)
    if n == 0:
        return {"runs": 0, "success_rate": 0.0, "hidden_tests_passed": 0,
                "hidden_tests_total": 0, "avg_diff_lines": 0.0,
                "avg_duration_seconds": 0.0, "total_cost_usd": 0.0,
                "jev_calls": 0, "jev_cost_usd": 0.0}
    return {
        "runs": n,
        "success_rate": round(sum(1 for r in results if r["success"]) / n, 2),
        "hidden_tests_passed": sum(r["hidden_tests_passed"] for r in results),
        "hidden_tests_total": sum(r["hidden_tests_total"] for r in results),
        "avg_diff_lines": round(sum(r["diff_lines"] for r in results) / n, 1),
        "avg_duration_seconds": round(sum(r["duration_seconds"] for r in results) / n, 1),
        "total_cost_usd": round(sum(r["cost_usd"] for r in results), 4),
        "jev_calls": sum(int(r.get("jev_calls", 0)) for r in results),
        "jev_cost_usd": round(sum(float(r.get("jev_cost_usd", 0.0)) for r in results), 6),
    }


def _model_key(result: dict) -> str:
    base = f"{result['provider']}:{result['model']}"
    variant = result.get("variant", "baseline")
    return base if variant in ("", "baseline") else f"{base}+{variant}"


def collect(directory: str) -> tuple[dict, list[str]]:
    """Матрица «тип задачи → модель[+вариант] → сводка» и список битых result-файлов."""
    matrix: dict[str, dict[str, list[dict]]] = {}
    problems: list[str] = []
    folder = Path(directory)
    if not folder.is_dir():
        return matrix, [f"{directory}: нет такой папки"]
    for path in sorted(folder.glob("*.json")):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            problems.append(f"{path.name}: не JSON")
            continue
        missing = validate(result)
        if missing:
            problems.append(f"{path.name}: нет полей {', '.join(missing)}")
            continue
        matrix.setdefault(result["type"], {}).setdefault(_model_key(result), []).append(result)
    return matrix, problems


def matrix_json(matrix: dict) -> str:
    out = {t: {m: summary(runs) for m, runs in cells.items()}
           for t, cells in matrix.items()}
    return json.dumps(out, ensure_ascii=False, indent=2)


def matrix_table(matrix: dict) -> str:
    models = sorted({m for cells in matrix.values() for m in cells})
    lines = [["тип", *models]]
    for task_type in sorted(matrix):
        row = [task_type]
        for model in models:
            s = summary(matrix.get(task_type, {}).get(model, []))
            row.append("—" if not s["runs"] else
                       f"{s['success_rate']:.0%} · {s['hidden_tests_passed']}/"
                       f"{s['hidden_tests_total']} тест · {s['avg_duration_seconds']:.0f}с")
        lines.append(row)
    widths = [max(len(row[i]) for row in lines) for i in range(len(models) + 1)]
    return "\n".join(" | ".join(c.ljust(w) for c, w in zip(row, widths, strict=True))
                     for row in lines)


def render_plan(runs: list[dict], as_json: bool) -> str:
    """План прогона: JSON-список (валидный для json.loads) или построчный текст."""
    if as_json:
        return json.dumps(runs, ensure_ascii=False)
    lines = []
    variants = sorted({run.get("variant", "baseline") for run in runs})
    if len(variants) > 1:
        lines.append("Волны: baseline — при `bb plugin disable bb-jev`; "
                     f"{', '.join(v for v in variants if v != 'baseline')} — при "
                     "`bb plugin enable bb-jev`. Строки разных волн вместе не запускать: "
                     "тулы Jev не должны попадать в контекст baseline.")
    for run in runs:
        tag = "" if run.get("variant", "baseline") == "baseline" else f" [{run['variant']}]"
        tier = f" · {run['tier']}" if run.get("tier") else ""
        lines.append(f"{run['type']}{tag} · {run['provider']}:{run['model']}{tier} · "
                     f"base {run['base_commit']}")
        lines.append("  " + " ".join(run["spawn"]))
    return "\n".join(lines)


# --------------------------------------------------------------------------- scorer

def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = git_env()
    env["PYTHONPATH"] = str(cwd)
    return subprocess.run(cmd, cwd=cwd, env=env, check=False,  # noqa: S603
                          capture_output=True, text=True, errors="replace")


def _python() -> str:
    """Интерпретатор тестов: окружение worktree, если оно есть, иначе текущий."""
    candidate = ROOT / ".venv/bin/python3"
    return str(candidate) if candidate.is_file() else sys.executable


def _diff_stats(worktree: Path, base: str) -> tuple[int, list[str]]:
    numstat = output(worktree, "git", "-c", "core.quotepath=false",
                     "diff", "--numstat", base, "--").splitlines()
    lines = 0
    changed: set[str] = set()
    for row in numstat:
        parts = row.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, path = parts[0], parts[1], parts[2]
        if added.isdigit():
            lines += int(added)
        if deleted.isdigit():
            lines += int(deleted)
        changed.add(path)
    untracked = output(worktree, "git", "-c", "core.quotepath=false",
                       "ls-files", "--others", "--exclude-standard").splitlines()
    for path in untracked:
        if path:
            changed.add(path)
            lines += len((worktree / path).read_bytes().splitlines())
    return lines, sorted(changed)


def _zone_ok(changed: list[str], zone: list[str]) -> bool:
    """Правки кода только в зоне задачи; тесты, документация и спеки — по правилам репозитория.

    Документация не меняет поведение продукта, а базовые коммиты фикстур жили по своим правилам
    SDD (требования в `docs/shared/product/`, спека, файл задачи) — наказывать за них нельзя."""
    if not zone:
        return True
    allowed = set(zone)
    for path in changed:
        if path in allowed or path.startswith(("tests/", "docs/", "openspec/")):
            continue
        return False
    return True


COPY_IGNORE = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "tmp")


def _copy(worktree: Path, parent: Path) -> Path:
    """Копия worktree без истории и временных каталогов: оригинал агента не трогается."""
    copy = parent / "repo"
    shutil.copytree(worktree, copy, symlinks=True, ignore=COPY_IGNORE)
    return copy


def _mutants(task: dict) -> list[dict]:
    """Мутанты задачи: список словарей или путь к JSON с ними (файл модуля в `file`)."""
    value = task.get("mutants")
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return json.loads((ROOT / value).read_text(encoding="utf-8"))
    return []


def _apply_mutant(copy: Path, mutant: dict) -> bool:
    """Подменяет ровно одно вхождение `old` на `new`; нет или несколько — мутант не применился."""
    path = copy / mutant["file"]
    text = path.read_text(encoding="utf-8")
    if text.count(mutant["old"]) != 1:
        return False
    path.write_text(text.replace(mutant["old"], mutant["new"]), encoding="utf-8")
    return True


def _run_hidden_tests(worktree: Path, task: dict) -> tuple[int, int]:
    """Прогоняет скрытые тесты из reference_commit на копии worktree (оригинал не трогается).

    Из reference_commit подменяются только файлы `tests/` — это и есть тесты PR. Файл вне
    `tests/` запускается как `<файл> --selftest` в том виде, какой сделал агент: подменять его
    версией из PR нельзя, иначе правка решения засчитывается сама себе."""
    tests = task.get("hidden_tests", [])
    if not tests:
        return 0, 0
    ref = task.get("reference_commit")
    with tempfile.TemporaryDirectory(prefix="arena-score-") as tmp:
        copy = _copy(worktree, Path(tmp))
        pytests = [t for t in tests if t.startswith("tests/")]
        selftests = [t for t in tests if not t.startswith("tests/")]
        if ref:
            for rel in pytests:
                blob = subprocess.run(  # noqa: S603
                    ["git", "-C", str(worktree), "show", f"{ref}:{rel}"],  # noqa: S607
                    check=False, capture_output=True, env=git_env())
                if blob.returncode == 0:
                    target = copy / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(blob.stdout)
        passed = 0
        total = 0
        if pytests:
            done = _run([_python(), "-m", "pytest", "-q", *pytests], copy)
            tail = done.stdout + "\n" + done.stderr
            p = sum(int(m) for m in PYTEST_PASSED.findall(tail))
            f = sum(int(m) for m in PYTEST_FAILED.findall(tail))
            if p + f == 0:
                total += 1
                passed += 1 if done.returncode == 0 else 0
            else:
                passed += p
                total += p + f
        for tool in selftests:
            done = _run([_python(), tool, "--selftest"], copy)
            total += 1
            if done.returncode == 0:
                passed += 1
        return passed, total


def _signal_hit(text: str, item: dict) -> bool:
    """Совпала ли находка: каждая группа `signals` требует хотя бы одного своего варианта."""
    groups = item.get("signals", [])
    return bool(groups) and all(
        any(alt.lower() in text for alt in group) for group in groups)


HEADING = re.compile(r"^(#{1,6})\s+(\S.*)$")


def _findings_section(text: str) -> str:
    """Тело раздела «Находки»: от заголовка markdown до следующего заголовка того же или
    высшего уровня. Без заголовка находок нет — слово в тексте разделом не считается, а
    «снята/снятие» внутри находки не обрывает раздел."""
    lines = text.splitlines()
    heads = [(index, len(match.group(1)), match.group(2).strip().lower())
             for index, line in enumerate(lines)
             if (match := HEADING.match(line))]
    start = next(((index, level) for index, level, title in heads if "находк" in title), None)
    if start is None:
        return ""
    at, level = start
    end = next((index for index, other, title in heads
                if index > at and (other <= level or "снят" in title)), len(lines))
    return "\n".join(lines[at + 1:end]).lower()


def _score_review(worktree: Path, task: dict) -> tuple[int, int, int]:
    """Ожидаемых находок в разделе «Находки», сколько их всего, сколько выдано обманок."""
    report = worktree / task.get("report_path", "tmp/review.md")
    text = report.read_text(encoding="utf-8", errors="replace") if report.is_file() else ""
    findings = _findings_section(text)
    expected = task.get("expected", [])
    hits = sum(1 for item in expected if _signal_hit(findings, item))
    false = sum(1 for item in task.get("decoys", []) if _signal_hit(findings, item))
    return hits, len(expected), false


def _score_tests(worktree: Path, task: dict) -> tuple[int, int, bool]:
    """Сколько мутантов поймали тесты агента, сколько их всего, зелены ли тесты на верном коде."""
    tests = task.get("tests", [])
    mutants = _mutants(task)
    if not tests or not mutants:
        return 0, len(mutants), False
    with tempfile.TemporaryDirectory(prefix="arena-tests-") as tmp:
        parent = Path(tmp)
        green = _run([_python(), "-m", "pytest", "-q", *tests],
                     _copy(worktree, parent / "base")).returncode == 0
        if not green:
            return 0, len(mutants), False
        killed = 0
        for index, mutant in enumerate(mutants):
            copy = _copy(worktree, parent / f"m{index}")
            if not _apply_mutant(copy, mutant):
                continue
            if _run([_python(), "-m", "pytest", "-q", *tests], copy).returncode != 0:
                killed += 1
        return killed, len(mutants), True


HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _changed_ranges(worktree: Path, base: str, path: str) -> list[tuple[int, int]]:
    diff = output(worktree, "git", "-c", "core.quotepath=false",
                  "diff", "-U0", base, "--", path)
    ranges = []
    for match in HUNK.finditer(diff):
        start, count = int(match.group(1)), int(match.group(2) or 1)
        if count:
            ranges.append((start, start + count - 1))
    return ranges


def _crap_max(worktree: Path, base: str, changed: list[str]) -> float | None:
    """Максимальный CRAP изменённых функций Python: тесты с покрытием филиалов + radon."""
    from importlib.util import find_spec

    targets = [p for p in changed
               if p.endswith(".py") and (worktree / p).is_file() and not p.startswith("tests/")]
    if not targets or find_spec("radon") is None:
        return None
    from nightly_audit import file_scores

    with tempfile.TemporaryDirectory(prefix="arena-crap-") as tmp:
        copy = _copy(worktree, Path(tmp))
        coverage_path = Path(tmp) / "coverage.json"
        _run([_python(), "-m", "pytest", "-q", "--ignore=tests/e2e", "-m", "not slow",
              "--cov", "--cov-branch",
              f"--cov-report=json:{coverage_path}"], copy)
        if not coverage_path.exists():
            return None
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
        scores = [
            score["crap"]
            for path in targets
            for score in file_scores(
                (copy / path).read_text(encoding="utf-8"),
                _changed_ranges(worktree, base, path),
                coverage["files"].get(path, {}).get("functions", {}))
            if score["crap"] is not None
        ]
    return round(max(scores), 2) if scores else None


def score_worktree(
    worktree: Path,
    task: dict,
    provider: str,
    model: str,
    *,
    variant: str = "baseline",
    duration_seconds: float = 0.0,
    turns: int = 0,
    tokens: int = 0,
    cost_usd: float = 0.0,
    jev_calls: int = 0,
    jev_cost_usd: float = 0.0,
    crap: bool = False,
) -> dict:
    """Объективно оценивает worktree агента по задаче и возвращает валидный result-словарь."""
    base = task["base_commit"]
    mode = task.get("mode", "code")
    diff_lines, changed = _diff_stats(worktree, base)
    discipline_ok = _zone_ok(changed, task.get("zone", []))
    ruff_clean = (not shutil.which("ruff")
                  or _run(["ruff", "check", "."], worktree).returncode == 0)
    has_il = (worktree / "pyproject.toml").is_file() and "[tool.importlinter]" in (
        worktree / "pyproject.toml").read_text(encoding="utf-8")
    il_clean = (_run(["lint-imports", "--no-logo"], worktree).returncode == 0
                if has_il and shutil.which("lint-imports") else True)
    extra = {}
    if mode == "review":
        passed, total, false_findings = _score_review(worktree, task)
        extra["false_findings"] = false_findings
        # ревью не правит код: отчёт лежит в tmp/ и в дифф не попадает
        success = total > 0 and passed == total and false_findings == 0 and diff_lines == 0
    elif mode == "tests":
        passed, total, green = _score_tests(worktree, task)
        extra["tests_green"] = green
        success = green and total > 0 and passed == total and discipline_ok
    else:
        passed, total = _run_hidden_tests(worktree, task)
        success = total > 0 and passed == total and ruff_clean and il_clean and discipline_ok
    result = {
        "task": task["id"],
        "type": task.get("type", "—"),
        "provider": provider,
        "model": model,
        "variant": variant,
        "success": success,
        "hidden_tests_passed": passed,
        "hidden_tests_total": total,
        "ruff_clean": ruff_clean,
        "import_linter_clean": il_clean,
        "crap_max": _crap_max(worktree, base, changed) if crap else None,
        "diff_lines": diff_lines,
        "discipline_ok": discipline_ok,
        "duration_seconds": duration_seconds,
        "turns": turns,
        "tokens": tokens,
        "cost_usd": cost_usd,
        "jev_calls": jev_calls,
        "jev_cost_usd": jev_cost_usd,
        **extra,
    }
    _attach_jev_v2(result, worktree, variant)
    return result


# --------------------------------------------------------------------------- run

MODELS_PATH = ROOT / "config/bb_models.json"


def _bb_json(args: list[str]) -> dict | None:
    """Ответ `bb ... --json`; ошибка запуска или `{"ok": false}` — `None`."""
    done = subprocess.run(["bb", *args, "--json"], check=False,  # noqa: S603, S607
                          capture_output=True, text=True, errors="replace")
    if done.returncode != 0:
        return None
    try:
        data = json.loads(done.stdout)
    except json.JSONDecodeError:
        return None
    return None if isinstance(data, dict) and data.get("ok") is False else data


def _usage(thread_id: str) -> dict:
    """Токены и цена из `bb thread context`; после idle расход может не отдаться сразу."""
    for _ in range(3):
        usage = (_bb_json(["thread", "context", thread_id]) or {}).get("usage") or {}
        if usage:
            return usage
        time.sleep(2)
    return {}


def _count_jev(events: list[dict]) -> int:
    """Сколько вызовов тулов Jev в событиях треда: `item/completed` с `tool` = `jev_*`.

    Считаем именно `item/completed`: `acp-opencode` на `item/started` пишет `execute`
    и подставляет реальное имя `jev_*` только при завершении вызова."""
    return sum(
        1 for event in events
        if event.get("type") == "item/completed"
        and ((event.get("data") or {}).get("item") or {}).get("type") == "toolCall"
        and str(((event.get("data") or {}).get("item") or {}).get("tool") or "").startswith("jev_"))


def _jev_calls(thread_id: str) -> int:
    """Все вызовы Jev за тред: журнал отдаёт по 100 событий, листаем по `--after-seq`."""
    total, after = 0, 0
    while True:
        page = _bb_json(["thread", "log", thread_id, "--after-seq", str(after)]) or []
        if not isinstance(page, list) or not page:
            return total
        total += _count_jev(page)
        after = max(int(event.get("seq") or 0) for event in page)
        if len(page) < 100:
            return total


def cost_usd(provider: str, model: str, tokens: int, models: dict) -> float:
    """`$` по каталогу API-моделей; подписка — 0, там расход в % окна."""
    if provider in models.get("subscription_providers", []):
        return 0.0
    per_1m = (models.get("api_price_usd_per_1m", {}).get(model)
              or models.get("api_price_usd_per_1m", {}).get(provider))
    return round(tokens * per_1m / 1_000_000, 4) if per_1m else 0.0


def select_runs(runs: list[dict], *, task: str | None = None, provider: str | None = None,
                model: str | None = None, limit: int = 0) -> list[dict]:
    """Отбор прогонов для `--run`: по задаче, провайдеру, модели и числу."""
    selected = runs
    for value, field in ((task, "task"), (provider, "provider"), (model, "model")):
        if value:
            selected = [run for run in selected if run[field] == value]
    return selected[:limit] if limit else selected


def _wave(runs: list[dict]) -> str:
    """Имя волны; разные волны в одном запуске недопустимы — Jev не должен утечь в baseline."""
    variants = sorted({run.get("variant", "baseline") for run in runs})
    if len(variants) != 1:
        raise ValueError("в одном запуске только одна волна: укажите --variant "
                         f"({', '.join(variants)})")
    return variants[0]


def _spawn_wait(cmd: list[str], timeout: int) -> tuple[str | None, str | None]:
    """Спавн и ожидание idle: id треда (может не создаться) и причина сбоя."""
    thread_id = (_bb_json(cmd[1:]) or {}).get("id")
    if not thread_id:
        return None, "тред не создан"
    if not _bb_json(["thread", "wait", thread_id, "--timeout", f"{timeout}s"]):
        return thread_id, f"тред не дошёл до idle за {timeout}s"
    return thread_id, None


def _usage_metrics(thread_id: str, provider: str, model: str,
                   models: dict) -> tuple[int, float]:
    """Токены и цена треда; без отданной цены — по каталогу API-моделей."""
    usage = _usage(thread_id)
    tokens = int(usage.get("usedTokens") or 0)
    reported = float((usage.get("cost") or {}).get("amount") or 0.0)
    return tokens, round(reported, 4) if reported else cost_usd(provider, model, tokens, models)


QUOTA_ROWS = {
    "codex": ("codex-5h", "codex-weekly"),
    "acp-antigravity": ("gemini-5h", "gemini-weekly"),
    "glm": ("glm-5h", "glm-weekly"),
}


def _limits_snapshot() -> dict[str, float]:
    """Живой снимок % окон подписок через RPC `limits-card get_limits`."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        f.write('{"refresh": true}\n')
        req = Path(f.name)
    try:
        data = _bb_json(["plugin", "rpc", "call", "limits-card", "get_limits",
                         "--input-file", str(req)]) or {}
    finally:
        req.unlink(missing_ok=True)
    return {
        str(row["id"]): float(row["usedPercent"])
        for row in data.get("rows") or []
        if row.get("status") == "ok" and row.get("usedPercent") is not None
    }


def _quota_buckets(actors: list[tuple[str, str]]) -> list[str]:
    """Какие окна подписок (5h/weekly) относятся к участвующим провайдерам и моделям."""
    keys: list[str] = []
    for provider, model in actors:
        for row in QUOTA_ROWS.get(provider, ()):
            if row not in keys:
                keys.append(row)
        if "glm" in model.lower():
            for row in QUOTA_ROWS["glm"]:
                if row not in keys:
                    keys.append(row)
    return keys


def _quota_delta(before: dict[str, float], after: dict[str, float],
                 actors: list[tuple[str, str]]) -> dict[str, float]:
    """Δ% расхода окон подписок за один прогон (отрицательное — окно сбросилось в пути)."""
    return {key: round(after[key] - before[key], 2)
            for key in _quota_buckets(actors) if key in before and key in after}


UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _jev_receipt_one(review_id: str, revision: str | None = None) -> dict | None:
    """Одна квитанция нового bb-jev через RPC `jev.workflow.receipt`."""
    payload: dict = {"reviewId": review_id}
    if revision:
        payload["revision"] = revision
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(payload, f)
        req = Path(f.name)
    try:
        data = _bb_json(["plugin", "rpc", "call", "bb-jev", "jev.workflow.receipt",
                         "--input-file", str(req)])
    finally:
        req.unlink(missing_ok=True)
    return data if isinstance(data, dict) and not data.get("error") else None


def _jev_revision(worktree: Path) -> str | None:
    """SHA-256 коммита и полного рабочего диффа, включая файлы с git add -N."""
    untracked = _run(["git", "ls-files", "--others", "--exclude-standard"], worktree)
    if untracked.returncode or untracked.stdout:
        return None
    head = _run(["git", "rev-parse", "HEAD"], worktree)
    diff = subprocess.run(["git", "diff", "--binary", "HEAD", "--"], cwd=worktree,  # noqa: S603, S607
                          env=git_env(), capture_output=True, check=False)
    if head.returncode or diff.returncode:
        return None
    return hashlib.sha256(head.stdout.encode("ascii") + diff.stdout).hexdigest()


def _jev_receipt_chain(worktree: Path) -> list[dict]:
    """Цепочка квитанций `acceptance -> plan -> requirements` по `tmp/jev-review-id.txt`."""
    marker = worktree / "tmp/jev-review-id.txt"
    if not marker.is_file():
        return []
    match = UUID_RE.search(marker.read_text(encoding="utf-8", errors="replace"))
    if not match:
        return []
    revision = _jev_revision(worktree)
    chain: list[dict] = []
    current: str | None = match.group(0).lower()
    seen: set[str] = set()
    while current and current not in seen and len(chain) < 5:
        seen.add(current)
        rec = _jev_receipt_one(current, revision if not chain else None)
        if not rec or rec.get("reviewId") != current:
            break
        chain.append(rec)
        current = rec.get("parentReviewId")
    return chain


def _attach_jev_v2(result: dict, worktree: Path, variant: str) -> None:
    """Для Jev v2 успех требует зелёных проверок и актуальной зелёной цепочки."""
    if variant != "jev-v2":
        return
    chain = _jev_receipt_chain(worktree)
    stages = tuple(receipt.get("stage") for receipt in chain)
    jev_passed = (stages == ("acceptance", "plan", "requirements")
                  and chain[0].get("freshness") == "current"
                  and all(receipt.get("status") == "passed" for receipt in chain))
    result["checks_passed"] = result["success"]
    result["jev_v2_passed"] = jev_passed
    result["success"] = result["checks_passed"] and jev_passed
    result["jev_receipts"] = chain
    result["jev_v2_tokens"] = sum(int(r.get("inputTokens") or 0) for r in chain)
    result["jev_v2_ms"] = sum(int(r.get("durationMs") or 0) for r in chain)


def _save_result(out_dir: Path, name: str, result: dict) -> None:
    _write(out_dir / name.replace("/", "-"),
           json.dumps(result, ensure_ascii=False, indent=2) + "\n")


def _step(role: str, actor: dict, cmd: list[str], timeout: int, *,
          models: dict) -> tuple[dict, str | None]:
    """Шаг связки: тред, ожидание и метрики — токены, цена, вызовы Jev и время шага."""
    started = monotonic()
    thread_id, error = _spawn_wait(cmd, timeout)
    step: dict = {"role": role, "thread": thread_id, "provider": actor["provider"],
                  "model": actor["model"], "tokens": 0, "cost_usd": 0.0, "jev_calls": 0,
                  "duration_seconds": 0.0}
    if thread_id and not error:
        tokens, price = _usage_metrics(thread_id, actor["provider"], actor["model"], models)
        step.update(tokens=tokens, cost_usd=price, jev_calls=_jev_calls(thread_id))
    step["duration_seconds"] = round(monotonic() - started, 1)
    return step, error


def _run_error(run: dict, error: str, steps: list[dict],
               thread: str | None = None) -> dict:
    last = thread or next((step["thread"] for step in reversed(steps) if step.get("thread")), None)
    return {"task": run["task"], "provider": run["provider"], "model": run["model"],
            "variant": run["variant"], "thread": last, "steps": steps, "error": error}


def _pipeline_result(run: dict, task: dict, path: str, steps: list[dict]) -> dict:
    """Скоринг worktree связки: провайдер `pipeline`, метрики — сумма двух шагов."""
    result = score_worktree(
        Path(path), task, PIPELINE_PROVIDER, run["pipeline"]["id"],
        variant=run.get("variant", "baseline"),
        duration_seconds=round(sum(s["duration_seconds"] for s in steps), 1),
        tokens=sum(s["tokens"] for s in steps),
        cost_usd=round(sum(s["cost_usd"] for s in steps), 4),
        jev_calls=sum(s["jev_calls"] for s in steps))
    result["thread"] = steps[-1]["thread"]
    result["steps"] = steps
    return result


def run_pipeline(run: dict, task: dict, out_dir: Path, timeout: int, models: dict, *,
                 measure_quota: bool = False) -> dict:
    """Связка plan -> code: планировщик пишет tmp/plan.md, кодер правит в том же worktree."""
    planner, coder = run["pipeline"]["planner"], run["pipeline"]["coder"]
    actors = [(planner["provider"], planner["model"]), (coder["provider"], coder["model"])]
    before = _limits_snapshot() if measure_quota else {}
    step_plan, error = _step("plan", planner, run["spawn"], timeout, models=models)
    steps = [step_plan]
    if error:
        return _run_error(run, error, steps)
    environment = (_bb_json(["thread", "show", step_plan["thread"]]) or {}).get("environment") or {}
    path = environment.get("path")
    if not path:
        return _run_error(run, "у планировщика нет папки", steps)
    plan_file = Path(path) / "tmp/plan.md"
    if not plan_file.is_file() or not plan_file.read_text(encoding="utf-8").strip():
        return _run_error(run, "планировщик не записал tmp/plan.md", steps)
    step_code, error = _step("code", coder, pipeline_code_spawn(run, environment.get("id") or path),
                             timeout, models=models)
    steps.append(step_code)
    if error:
        return _run_error(run, error, steps)
    result = _pipeline_result(run, task, path, steps)
    if measure_quota:
        result["quota_delta"] = _quota_delta(before, _limits_snapshot(), actors)
    name = f"{run['task']}-{run['model']}-{run['variant']}-{step_code['thread']}.json"
    _save_result(out_dir, name, result)
    return result


def run_cell(run: dict, task: dict, out_dir: Path, timeout: int, models: dict, *,
             measure_quota: bool = False) -> dict:
    """Один прогон: тред от базового коммита, ожидание, скоринг, result-json в `out_dir`."""
    if run.get("pipeline"):
        return run_pipeline(run, task, out_dir, timeout, models, measure_quota=measure_quota)
    before = _limits_snapshot() if measure_quota else {}
    started = monotonic()
    thread_id, error = _spawn_wait(run["spawn"], timeout)
    if error:
        return _run_error(run, error, [], thread_id)
    shown = _bb_json(["thread", "show", thread_id]) or {}
    path = (shown.get("environment") or {}).get("path")
    if not path:
        return _run_error(run, "у треда нет папки", [], thread_id)
    tokens, price = _usage_metrics(thread_id, run["provider"], run["model"], models)
    result = score_worktree(
        Path(path), task, run["provider"], run["model"], variant=run.get("variant", "baseline"),
        duration_seconds=round(monotonic() - started, 1), tokens=tokens, cost_usd=price,
        jev_calls=_jev_calls(thread_id))
    result["thread"] = thread_id
    if measure_quota:
        result["quota_delta"] = _quota_delta(
            before, _limits_snapshot(), [(run["provider"], run["model"])])
    _save_result(out_dir, f"{run['task']}-{run['provider']}-{run['variant']}-{thread_id}.json",
                 result)
    return result


def run_wave(runs: list[dict], tasks: dict[str, dict], out_dir: Path, jobs: int,
             timeout: int, models: dict, *,
             measure_quota: bool = False) -> tuple[list[dict], list[dict]]:
    """Параллельно поднимает и оценивает прогоны одной волны."""
    from concurrent.futures import ThreadPoolExecutor

    out_dir.mkdir(parents=True, exist_ok=True)
    workers = 1 if measure_quota else max(1, jobs)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_cell, run, tasks[run["task"]], out_dir, timeout, models,
                               measure_quota=measure_quota)
                   for run in runs]
        results = [future.result() for future in futures]
    errors = [result for result in results if result.get("error")]
    return results, errors


# --------------------------------------------------------------------------- selftest

def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


APP_RIGHT = "def double(x: int) -> int:\n    return x * 2\n"
APP_BROKEN = "def double(x: int) -> int:\n    return x\n"


def _selftest_repo(parent: Path, files: dict[str, str]) -> tuple[Path, str]:
    """Временный git-репозиторий с одним коммитом: путь и базовый коммит для скоринга."""
    repo = parent / "repo"
    repo.mkdir()
    env = git_env()
    _run(["git", "init", "-q"], repo)
    for name, text in files.items():
        _write(repo / name, text)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)  # noqa: S603, S607
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",  # noqa: S603, S607
                    "commit", "-q", "-m", "base"], check=True, env=env)
    return repo, output(repo, "git", "rev-parse", "HEAD").strip()


def _selftest_score(bad: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="arena-st-") as tmp:
        repo, base = _selftest_repo(Path(tmp), {
            "app.py": APP_BROKEN,
            "tests/test_app.py": "from app import double\n\n\ndef test_ok() -> None:\n"
                                 "    assert double(2) == 4\n",
        })
        task = {"id": "t1", "type": "багфикс", "base_commit": base,
                "hidden_tests": ["tests/test_app.py"], "zone": ["app.py"]}
        red = score_worktree(repo, task, "codex", "gpt-6.2-sol", variant="baseline")
        if red["success"] or red["hidden_tests_passed"] != 0 or red["hidden_tests_total"] != 1:
            bad.append(f"скорёр не поймал сломанный код: {red}")
        _write(repo / "app.py", APP_RIGHT)
        green = score_worktree(repo, task, "codex", "gpt-6.2-sol", variant="jev",
                               jev_calls=2, jev_cost_usd=0.0001)
        if not green["success"] or green["hidden_tests_passed"] != 1 or not green["discipline_ok"]:
            bad.append(f"скорёр не принял исправленный код: {green}")
        _write(repo / "outside.py", "x = 1\n")
        dirty = score_worktree(repo, task, "codex", "gpt-6.2-sol")
        if dirty["discipline_ok"] or dirty["success"]:
            bad.append("скорёр не поймал правку вне зоны")
        (repo / "outside.py").unlink()
        _write(repo / "docs/notes.md", "требования\n")
        notes = score_worktree(repo, task, "codex", "gpt-6.2-sol")
        if not notes["discipline_ok"]:
            bad.append("правка документации сочтена нарушением зоны")


def _selftest_review(bad: list[str]) -> None:
    """Разделы отчёта: находка в «Находках» засчитана, обманка там же — ложная находка."""
    task = {"expected": [{"signals": [["posting.py"], ["unknown_line", "строки нет"]]}],
            "decoys": [{"signals": [["ready"], ["both_known"]]}]}
    cases = {
        "good": (
            "## Находки\n- posting.py: пропавшая строка даёт TOO_MANY, нужен UNKNOWN_LINE\n"
            "## Снятые подозрения\n- ready: both_known — переписка эквивалентна\n", (1, 1, 0)),
        "decoy": (
            "## Находки\n- ready: both_known опасная переписка\n"
            "## Снятые подозрения\n- posting.py — всё в порядке\n", (0, 1, 1)),
        "empty": ("ревью не сделано\n", (0, 1, 0)),
        # «снята» внутри находки — часть формулировки, а не начало раздела «Снятые подозрения»
        "снят-в-находке": (
            "## Находки\n- posting.py: строка снята из реализации, и вместо UNKNOWN_LINE\n"
            "  менеджер видит «количество больше остатка»\n"
            "## Снятые подозрения\n- ready: both_known — переписка эквивалентна\n", (1, 1, 0)),
        # вводная фраза со словом «находки» до заголовка не должна сдвигать начало раздела
        "вводная": (
            "Ревью ниже. Находки идут по убыванию критичности.\n\n"
            "## Находки\n- posting.py: пропавшая строка даёт TOO_MANY, нужен UNKNOWN_LINE\n"
            "### Детали\n- пропуск виден в check()\n"
            "## Снятые подозрения\n- ready: both_known — переписка эквивалентна\n", (1, 1, 0)),
        # обманка после слова «снята» в находке всё равно находка, а не снятое подозрение
        "обманка-после-снят": (
            "## Находки\n- posting.py: строка снята из реализации, нужен UNKNOWN_LINE\n"
            "- ready: both_known опасная переписка\n"
            "## Снятые подозрения\n- пусто\n", (1, 1, 1)),
        # без заголовка «Находки» находок нет, даже если слово встречается в тексте
        "без-заголовка": (
            "Находки: posting.py, UNKNOWN_LINE — но разделов нет, это одна строка\n", (0, 1, 0)),
        # «Снятые подозрения» вложены глубже находок — раздел всё равно кончается на них
        "снятые-глубже": (
            "## Находки\n- posting.py: пропавшая строка даёт TOO_MANY, нужен UNKNOWN_LINE\n"
            "### Снятые подозрения\n- ready: both_known — переписка эквивалентна\n", (1, 1, 0)),
    }
    with tempfile.TemporaryDirectory(prefix="arena-rev-") as tmp:
        for name, (text, expected) in cases.items():
            _write(Path(tmp) / name / "tmp/review.md", text)
            got = _score_review(Path(tmp) / name, task)
            if got != expected:
                bad.append(f"ревью-отчёт «{name}»: ждали {expected}, вышло {got}")


def _selftest_run(bad: list[str]) -> None:
    """Отбор прогонов, волна, цена и интерпретатор: без сети и без спавна."""
    runs = [
        {"task": "a", "provider": "acp-opencode", "model": "deepseek/x", "variant": "baseline"},
        {"task": "a", "provider": "codex", "model": "gpt", "variant": "baseline"},
        {"task": "b", "provider": "acp-opencode", "model": "deepseek/x", "variant": "baseline"},
    ]
    if len(select_runs(runs)) != 3:
        bad.append("--run без фильтров потерял прогоны")
    if len(select_runs(runs, task="a", provider="acp-opencode")) != 1:
        bad.append("--run не отобрал прогон по задаче и провайдеру")
    if len(select_runs(runs, limit=2)) != 2:
        bad.append("--limit не ограничил число прогонов")
    if _wave(runs[:1]) != "baseline":
        bad.append("_wave не назвала волну baseline")
    try:
        _wave(runs[:1] + [runs[0] | {"variant": "jev"}])
    except ValueError:
        pass
    else:
        bad.append("смешение волн baseline и jev не отвергнуто")
    _selftest_misc(bad)


def _selftest_misc(bad: list[str]) -> None:
    """Цена по каталогу, интерпретатор тестов и подсчёт вызовов Jev в журнале треда."""
    models = {"subscription_providers": ["codex"],
              "api_price_usd_per_1m": {"deepseek/x": 2.0}}
    if cost_usd("acp-opencode", "deepseek/x", 1_000_000, models) != 2.0:
        bad.append("цена API-модели посчитана неверно")
    if cost_usd("codex", "gpt", 1_000_000, models) != 0.0:
        bad.append("подписка не должна давать $")
    if not Path(_python()).is_file():
        bad.append(f"интерпретатор тестов не найден: {_python()}")
    events = [
        {"type": "item/started", "data": {"item": {"type": "toolCall", "tool": "jev_check_plan"}}},
        {"type": "item/completed",
         "data": {"item": {"type": "toolCall", "tool": "jev_check_plan"}}},
        # acp-opencode на started пишет `execute` и подставляет `jev_*` только на completed
        {"type": "item/started", "data": {"item": {"type": "toolCall", "tool": "execute"}}},
        {"type": "item/completed",
         "data": {"item": {"type": "toolCall", "tool": "jev_acceptance"}}},
        {"type": "item/completed", "data": {"item": {"type": "toolCall", "tool": "bash"}}},
        {"type": "item/completed", "data": {"item": {"type": "agentMessage"}}},
    ]
    if _count_jev(events) != 2:
        bad.append(f"вызовы Jev посчитаны неверно: {_count_jev(events)} вместо 2")
    delta = _quota_delta(
        {"codex-5h": 20.0, "codex-weekly": 30.0, "glm-5h": 5.0},
        {"codex-5h": 24.5, "codex-weekly": 31.2, "glm-5h": 9.0},
        [("codex", "gpt-6.2-sol"), ("acp-opencode", "zai-coding-plan/glm-5.3-flash")])
    if delta != {"codex-5h": 4.5, "codex-weekly": 1.2, "glm-5h": 4.0}:
        bad.append(f"Δ% окон подписок посчитан неверно: {delta}")


def _selftest_tests(bad: list[str]) -> None:
    """Тесты агента: слабые не убивают мутанта, сильные убивают, падающие на верном коде — нет."""
    with tempfile.TemporaryDirectory(prefix="arena-tests-st-") as tmp:
        repo, base = _selftest_repo(Path(tmp), {
            "app.py": APP_RIGHT,
            "tests/test_spec.py": "from app import double\n\n\ndef test_double() -> None:\n"
                                  "    assert double(2) == 4\n",
        })
        task = {"id": "t1", "type": "тесты по спеке", "mode": "tests", "base_commit": base,
                "tests": ["tests/test_spec.py"],
                "mutants": [{"id": "x3", "file": "app.py",
                             "old": "    return x * 2\n", "new": "    return x * 3\n"}]}
        strong = score_worktree(repo, task, "codex", "gpt-6.2-sol")
        if not strong["success"] or strong["hidden_tests_passed"] != 1:
            bad.append(f"сильные тесты не убили мутанта: {strong}")
        _write(repo / "tests/test_spec.py", "def test_nothing() -> None:\n    assert True\n")
        weak = score_worktree(repo, task, "codex", "gpt-6.2-sol")
        if weak["success"] or weak["hidden_tests_passed"] != 0 or not weak["tests_green"]:
            bad.append(f"слабые тесты засчитаны: {weak}")
        _write(repo / "tests/test_spec.py",
               "def test_broken() -> None:\n    assert False\n")
        broken = score_worktree(repo, task, "codex", "gpt-6.2-sol")
        if broken["success"] or broken["tests_green"]:
            bad.append(f"падающие на верном коде тесты засчитаны: {broken}")


def _selftest_plan(bad: list[str], config: dict) -> list[dict]:
    runs = plan(config)
    if len(runs) != 8:
        bad.append(f"прогонов ждали 8 (2 задачи × 2 модели × 2 варианта), вышло {len(runs)}")
    if len(plan(config, variant_filter="jev")) != 4:
        bad.append("фильтр --variant jev не отобрал 4 прогона")
    try:
        plan(config, variant_filter="нет-такого")
    except ValueError:
        pass
    else:
        bad.append("неизвестный вариант не отвергнут")
    if runs[0]["spawn"][:3] != ["bb", "thread", "spawn"] \
            or "--new-environment" not in runs[0]["spawn"]:
        bad.append("команда spawn не собрана")
    review = plan({"providers": config["providers"][:1], "variants": config["variants"][:1],
                   "tasks": [{"id": "r", "base_commit": "abc", "prompt": "отревьюй",
                              "patch": "config/arena/review-01.patch"}]})
    if "```diff\ndiff --git" not in review[0]["prompt"]:
        bad.append("патч ревью не попал в промпт: в worktree от base_commit его нет")
    return runs


def _selftest_suite(bad: list[str]) -> None:
    """Матрица и промпты для демонстрационного конфига."""
    config = load_config(str(CONFIG_PATH))
    runs = plan(config)
    if len(runs) != 3 or any(not run.get("pipeline") for run in runs):
        bad.append("пример должен выдавать три варианта одной связки")
    if len(plan(config, suite="full")) != 6:
        bad.append("полная матрица примера должна содержать шесть одиночных ячеек")
    v2_pipe = _pipeline_run(config["tasks"][0],
                            next(v for v in config["variants"] if v["id"] == "jev-v2"),
                            config["pipelines"][0], config.get("base_commit"))
    if ("jev_review_requirements" not in v2_pipe["plan_prompt"]
            or "planReviewId" not in v2_pipe["plan_prompt"]
            or "jev_review_acceptance" not in v2_pipe["code_prompt"]):
        bad.append("промпты связки jev-v2 не связывают шаги через planReviewId")
    try:
        plan(config, suite="нет-такого")
    except ValueError:
        pass
    else:
        bad.append("неизвестный набор не отвергнут")


def _check_pipeline_prompts(run: dict, with_jev: bool, bad: list[str]) -> None:
    """Промпты шагов: короткий план, свобода реализации в рамках требований, добавки Jev."""
    if run["provider"] != PIPELINE_PROVIDER or run["model"] != run["pipeline"]["id"]:
        bad.append("связка в плане без provider=pipeline / model=<id связки>")
    if "tmp/plan.md" not in run["plan_prompt"] or "не меняй" not in run["plan_prompt"]:
        bad.append("промпт планировщика не требует план в tmp/plan.md без правок кода")
    if "tmp/plan.md" not in run["code_prompt"] or "тесты" not in run["code_prompt"]:
        bad.append("промпт кодера не ссылается на tmp/plan.md и тесты")
    if "выбери другой путь реализации" not in run["code_prompt"]:
        bad.append("промпт кодера не разрешает отклониться от плана в рамках требований")
    for tool, text in (("jev_check_plan", run["plan_prompt"]),
                       ("jev_acceptance", run["code_prompt"])):
        if (tool in text) != with_jev:
            bad.append(f"добавка {tool} не совпала с вариантом {run['variant']}")


def _check_pipeline_spawn(run: dict, bad: list[str]) -> None:
    """Шаг 1 — новый worktree от базового коммита, шаг 2 — окружение планировщика."""
    if "--new-environment" not in run["spawn"] or "--base-branch" not in run["spawn"]:
        bad.append("шаг 1 связки не поднимает worktree от базового коммита")
    cmd = pipeline_code_spawn(run, "env_1")
    if "--environment" not in cmd or "env_1" not in cmd \
            or {"--new-environment", "--base-branch"} & set(cmd):
        bad.append(f"шаг 2 связки собран не в окружении планировщика: {cmd}")
    if cmd[cmd.index("--model") + 1] != run["pipeline"]["coder"]["model"]:
        bad.append("шаг 2 связки запускает не кодера")


def _selftest_pipeline(bad: list[str]) -> None:
    """Связка: промпты шагов, добавки Jev по варианту и спавн кодера в том же окружении."""
    config = load_config(str(CONFIG_PATH))
    baseline = next((run for run in plan(config, variant_filter="baseline")
                     if run.get("pipeline")), None)
    jev = next((run for run in plan(config, variant_filter="jev") if run.get("pipeline")), None)
    if baseline is None or jev is None:
        bad.append("в наборе smart нет связок plan->code — проверять нечего")
        return
    for run, with_jev in ((baseline, False), (jev, True)):
        _check_pipeline_prompts(run, with_jev, bad)
        _check_pipeline_spawn(run, bad)


def _selftest_jev_v2(bad: list[str]) -> None:
    from unittest.mock import patch

    acceptance = {"stage": "acceptance", "status": "passed", "freshness": "current"}
    plan_receipt = {"stage": "plan", "status": "passed"}
    requirements = {"stage": "requirements", "status": "passed"}
    cases = [
        ([acceptance, plan_receipt, requirements], True, True),
        ([acceptance, plan_receipt, requirements], False, False),
        ([acceptance, plan_receipt, {**requirements, "status": "failed"}], True, False),
        ([{**acceptance, "freshness": "stale"}, plan_receipt, requirements], True, False),
        ([{**acceptance, "status": "inconclusive"}, plan_receipt, requirements], True, False),
        ([acceptance, plan_receipt], True, False),
    ]
    for chain, checks_passed, expected in cases:
        result = {"success": checks_passed}
        with patch(f"{__name__}._jev_receipt_chain", return_value=chain):
            _attach_jev_v2(result, ROOT, "jev-v2")
        if result["success"] != expected or result["checks_passed"] != checks_passed:
            bad.append(f"ошибка сводного вердикта Jev v2: {result}")


def _selftest_collect(bad: list[str]) -> None:
    results = {
        "r1": {"task": "parser", "type": "парсер", "provider": "acp-opencode",
               "model": "deepseek/deepseek-v4-pro", "variant": "baseline", "success": True,
               "hidden_tests_passed": 5, "hidden_tests_total": 5, "ruff_clean": True,
               "import_linter_clean": True, "crap_max": 8, "diff_lines": 90,
               "discipline_ok": True, "duration_seconds": 300, "turns": 6,
               "tokens": 100000, "cost_usd": 0.13},
        "r1j": {"task": "parser", "type": "парсер", "provider": "acp-opencode",
                "model": "deepseek/deepseek-v4-pro", "variant": "jev", "success": True,
                "hidden_tests_passed": 5, "hidden_tests_total": 5, "ruff_clean": True,
                "import_linter_clean": True, "crap_max": 8, "diff_lines": 85,
                "discipline_ok": True, "duration_seconds": 310, "turns": 6,
                "tokens": 102000, "cost_usd": 0.13, "jev_calls": 2, "jev_cost_usd": 0.0001},
        "r2": {"task": "parser", "type": "парсер", "provider": "codex", "model": "gpt-6.2-sol",
               "variant": "baseline", "success": False, "hidden_tests_passed": 2,
               "hidden_tests_total": 5, "ruff_clean": False, "import_linter_clean": True,
               "crap_max": 20, "diff_lines": 210, "discipline_ok": False, "duration_seconds": 600,
               "turns": 9, "tokens": 400000, "cost_usd": 0.0},
        "r3": {"task": "ui", "type": "фича + UI", "provider": "acp-opencode",
               "model": "deepseek/deepseek-v4-pro", "variant": "baseline", "success": True,
               "hidden_tests_passed": 3, "hidden_tests_total": 4, "ruff_clean": True,
               "import_linter_clean": True, "crap_max": 10, "diff_lines": 120,
               "discipline_ok": True, "duration_seconds": 240, "turns": 5,
               "tokens": 90000, "cost_usd": 0.12},
        "p1": {"task": "parser", "type": "парсер", "provider": "pipeline",
               "model": "gpt-6.2-sol->deepseek-flash", "variant": "baseline", "success": True,
               "hidden_tests_passed": 5, "hidden_tests_total": 5, "ruff_clean": True,
               "import_linter_clean": True, "crap_max": 9, "diff_lines": 70,
               "discipline_ok": True, "duration_seconds": 420, "turns": 0,
               "tokens": 150000, "cost_usd": 0.02,
               "steps": [{"role": "plan", "thread": "thr_p", "provider": "codex"},
                         {"role": "code", "thread": "thr_c", "provider": "acp-opencode"}]},
        "bad": {"task": "ui", "type": "фича + UI"},
    }
    with tempfile.TemporaryDirectory() as tmp:
        for name, result in results.items():
            _write(Path(tmp) / f"{name}.json", json.dumps(result))
        matrix, problems = collect(tmp)
    if not problems or "bad.json: нет полей" not in problems[0]:
        bad.append("битый result-файл не пойман")
    parser = matrix.get("парсер", {})
    cell = summary(parser.get("acp-opencode:deepseek/deepseek-v4-pro+jev", []))
    if cell["success_rate"] != 1.0 or cell["jev_calls"] != 2:
        bad.append("сводка +jev ячейки не сошлась")
    missing = summary(matrix.get("фича + UI", {}).get("codex:gpt-6.2-sol", []))
    if missing["runs"] != 0 or missing["success_rate"] != 0.0:
        bad.append("пропущенная ячейка не дала нули (деление на ноль)")
    table = matrix_table(matrix)
    if "+jev" not in table or "—" not in table:
        bad.append("таблица матрицы без столбца +jev или пропущенной ячейки")
    pipe_key = "pipeline:gpt-6.2-sol->deepseek-flash"
    if summary(parser.get(pipe_key, []))["runs"] != 1 or pipe_key not in table:
        bad.append("связка не попала в матрицу как pipeline:<id связки>")


def selftest() -> int:
    bad: list[str] = []
    config = {
        "base_commit": "abc123",
        "providers": [
            {"provider": "acp-opencode", "model": "deepseek/deepseek-v4-pro"},
            {"provider": "codex", "model": "gpt-6.2-sol"},
        ],
        "variants": [
            {"id": "baseline", "jev": False, "prompt_suffix": ""},
            {"id": "jev", "jev": True, "prompt_suffix": " +jev"},
        ],
        "tasks": [
            {"id": "parser", "type": "парсер", "prompt": "сделай разбор", "zone": ["src/x.py"]},
            {"id": "ui", "type": "фича + UI", "prompt": "сделай страницу", "zone": ["src/y.py"]},
        ],
    }
    runs = _selftest_plan(bad, config)
    plan_json = json.loads(render_plan(runs, as_json=True))
    if not isinstance(plan_json, list) or len(plan_json) != 8:
        bad.append("--plan --json не валидный JSON-список прогонов")
    _selftest_collect(bad)
    _selftest_score(bad)
    _selftest_review(bad)
    _selftest_tests(bad)
    _selftest_run(bad)
    _selftest_suite(bad)
    _selftest_pipeline(bad)
    _selftest_jev_v2(bad)
    return report(bad, "план (умная матрица и full, связки plan->code), скорёр кода, тестов "
                       "и ревью, запуск волны, матрица и битый файл")


def _add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plan", action="store_true", help="план прогона, без spawn")
    parser.add_argument("--run", action="store_true",
                        help="поднять треды волны, оценить их и напечатать матрицу")
    parser.add_argument("--variant", help="фильтр варианта (baseline / jev)")
    parser.add_argument("--suite", help="набор ячеек: smart (по умолчанию при наличии) или full")
    parser.add_argument("--limit", type=int, default=0, help="сколько прогонов запустить (0 — все)")
    parser.add_argument("--jobs", type=int, default=4, help="сколько тредов держать одновременно")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="сколько ждать тред до idle, с")
    parser.add_argument("--out-dir", default="tmp/arena-runs",
                        help="куда складывать result-json прогонов")
    parser.add_argument("--config", default=str(CONFIG_PATH),
                        help="фикстуры (config/bb_arena.json)")
    parser.add_argument("--collect", metavar="DIR", help="собрать матрицу из result-файлов")
    parser.add_argument("--score", action="store_true",
                        help="оценить worktree и записать result-json")
    parser.add_argument("--worktree", help="путь к worktree для --score")
    parser.add_argument("--task", help="id задачи из config (--score и --run)")
    parser.add_argument("--provider", help="провайдер (--score и --run)")
    parser.add_argument("--model", help="модель (--score и --run)")
    parser.add_argument("--duration", type=float, default=0.0, help="время прогона, с")
    parser.add_argument("--turns", type=int, default=0, help="число ходов")
    parser.add_argument("--tokens", type=int, default=0, help="суммарные токены")
    parser.add_argument("--cost-usd", type=float, default=0.0, help="стоимость модели, $")
    parser.add_argument("--jev-calls", type=int, default=0, help="число вызовов Jev")
    parser.add_argument("--jev-cost-usd", type=float, default=0.0, help="стоимость Jev, $")
    parser.add_argument("--crap", action="store_true",
                        help="посчитать CRAP изменённых функций (дольше: тесты с покрытием)")
    parser.add_argument("--measure-quota", action="store_true",
                        help="снимать Δ%% окон подписок через limits-card до/после ячейки (jobs=1)")
    parser.add_argument("--out", help="куда записать result-json для --score")
    parser.add_argument("--json", action="store_true", help="машиночитаемый вывод")


def _run_body(args: argparse.Namespace) -> int:
    """Одна волна прогонов: отбор, спавн, оценка, матрица."""
    cfg = load_config(args.config)
    tasks = {t["id"]: t for t in cfg.get("tasks", [])}
    try:
        runs = select_runs(plan(cfg, args.variant, args.suite), task=args.task,
                           provider=args.provider, model=args.model, limit=args.limit)
        wave = _wave(runs)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    if not runs:
        print("нет прогонов под этот отбор", file=sys.stderr)
        return 2
    jobs = 1 if args.measure_quota else args.jobs
    print(f"волна {wave}: прогонов {len(runs)}, одновременно {jobs}, "
          f"результаты — {args.out_dir}")
    if wave != "baseline":
        print("проверьте: тулы Jev должны быть доступны треду (bb plugin enable bb-jev)")
    models = json.loads(MODELS_PATH.read_text(encoding="utf-8"))
    _, errors = run_wave(runs, tasks, Path(args.out_dir), jobs, args.timeout, models,
                         measure_quota=args.measure_quota)
    for error in errors:
        print(f"✗ {error['task']} · {error['provider']}:{error['model']} — {error['error']}")
    matrix, problems = collect(args.out_dir)
    for problem in problems:
        print(problem)
    print(matrix_json(matrix) if args.json else matrix_table(matrix))
    return 1 if errors or problems else 0


def _body(args: argparse.Namespace) -> int:
    if args.run:
        return _run_body(args)
    if args.score:
        cfg = load_config(args.config)
        tasks = {t["id"]: t for t in cfg.get("tasks", [])}
        if not args.worktree or args.task not in tasks or not args.provider or not args.model:
            print("для --score нужны --worktree, --task (из config), --provider и --model",
                  file=sys.stderr)
            return 2
        res = score_worktree(
            Path(args.worktree), tasks[args.task], args.provider, args.model,
            variant=args.variant or "baseline", duration_seconds=args.duration,
            turns=args.turns, tokens=args.tokens, cost_usd=args.cost_usd,
            jev_calls=args.jev_calls, jev_cost_usd=args.jev_cost_usd, crap=args.crap)
        text = json.dumps(res, ensure_ascii=False, indent=2)
        if args.out:
            _write(Path(args.out), text + "\n")
        else:
            print(text)
        return 0
    if args.collect:
        matrix, problems = collect(args.collect)
        for problem in problems:
            print(problem)
        print(matrix_json(matrix) if args.json else matrix_table(matrix))
        return 1 if problems else 0
    if args.plan:
        try:
            runs = plan(load_config(args.config), args.variant, args.suite)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 2
        print(render_plan(runs, args.json))
        return 0
    print("укажите --plan, --run, --score или --collect")
    return 2


def main() -> int:
    return bb_cli.main("Арена моделей", selftest, _add_args, _body)


if __name__ == "__main__":
    sys.exit(main())

"""Раннер сценариев проверки: шаги через API поднятой системы и ожидания.

Формат и правила — tests/scenarios/README.md.

Запуск:
    python3 tools/scenario.py run [имя …] --mode replay [--contour local] [--record-run]
    python3 tools/scenario.py --control     контрольный сценарий обязан покраснеть (код 0)
    python3 tools/scenario.py --selftest    раннер против локальной http-заглушки, без стенда

Сценарий — `tests/scenarios/<имя>/scenario.json`. Как обращаться к стенду (адрес API, база,
compose, подставной провайдер) — `tests/scenarios/stand.json`; шаг, которому нужен
неописанный кусок стенда, — отказ до запуска. Последняя строка отчёта — квитанция: хеш
дерева кода, режим, исход; она же дописывается в каталог состояния стенда.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from script_common import git_env

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = Path("tests/scenarios")
STAND = SCENARIOS / "stand.json"
CONTROL = "control"
RUNS = Path("openspec/runs")
RECEIPT_CONFIG = Path("tools/receipt.json")
VAR = re.compile(r"\{(\w+)\}")


class Mode(StrEnum):
    """Режим обращений к языковой модели: стенд читает его из SCENARIO_MODE."""

    REPLAY = "replay"   # подставной провайдер отвечает кассетой; по умолчанию
    RECORD = "record"   # настоящий провайдер, ответы пишутся в кассету
    LIVE = "live"       # настоящий провайдер без записи; приёмка перед push


class Failed(Exception):
    """Ожидание не сбылось или шаг не прошёл: текст — что именно."""


@dataclass
class Run:
    """Состояние одного прогона сценария."""

    stand: dict
    mode: Mode
    vars: dict[str, str] = field(default_factory=dict)
    responses: dict[str, object] = field(default_factory=dict)


# --------------------------------------------------------------------------- подстановки


def fill(value: object, run: Run) -> object:
    """`{имя}` в строках — из переменных прогона (окружение + извлечённое шагами)."""
    if isinstance(value, str):
        def one(m: re.Match) -> str:
            if m[1] not in run.vars:
                raise Failed(f"переменная {{{m[1]}}} не задана")
            return run.vars[m[1]]
        return VAR.sub(one, value)
    if isinstance(value, dict):
        return {k: fill(v, run) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, run) for v in value]
    return value


def pick(data: object, path: str) -> object:
    """Подмножество JSONPath: `$.a.b.0`. Нет такого поля — KeyError."""
    for part in path.removeprefix("$").strip(".").split("."):
        if not part:
            continue
        data = data[int(part)] if isinstance(data, list) else data[part]  # type: ignore[index]
    return data


# --------------------------------------------------------------------------- шаги


def fetch(url: str, method: str, body: object = None, headers: dict | None = None
          ) -> tuple[int, bytes]:
    """HTTP к стенду; адрес — из stand.json, схема только http(s)."""
    if not url.startswith(("http://", "https://")):
        raise Failed(f"адрес {url} — не http(s)")
    request = urllib.request.Request(  # noqa: S310 — схема проверена выше
        url, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def http(run: Run, step: dict) -> None:
    status, raw = fetch(run.stand["base_url"] + step["path"], step["method"], step.get("body"),
                        step.get("headers"))
    data = json.loads(raw) if raw else None
    if "expect_status" in step and status != step["expect_status"]:
        raise Failed(f"{step['method']} {step['path']}: код {status}, "
                     f"ждали {step['expect_status']}")
    run.responses[step.get("save", "last")] = data
    for name, path in step.get("extract", {}).items():
        run.vars[name] = str(pick(data, path))


def shell(command: list[str], query: str = "") -> str:
    argv = [part.replace("{query}", query) for part in command]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)  # noqa: S603
    if done.returncode:
        raise Failed(f"{argv[0]}: код {done.returncode}: {done.stderr.strip()[:200]}")
    return done.stdout.strip()


def sql(run: Run, db: str, query: str) -> str:
    return shell(run.stand["db"][db], query)


def wait(run: Run, step: dict) -> None:
    deadline = time.monotonic() + step["timeout_s"]
    while True:
        value = sql(run, step["db"], step["query"])
        if value in step["until"]:
            run.vars[step.get("as", "waited")] = value
            return
        if time.monotonic() > deadline:
            raise Failed(f"wait: за {step['timeout_s']} с значение «{value}», "
                         f"ждали {step['until']}")
        time.sleep(step.get("poll_s", 5))


def container(run: Run, step: dict) -> None:
    shell([*run.stand["compose"], step["action"], step["service"]])


def stub_call(run: Run, path: str, body: dict | None = None) -> str:
    return fetch(run.stand["stub_url"] + path, "POST" if body else "GET", body)[1].decode()


def stub(run: Run, step: dict) -> None:
    stub_call(run, "/__state", {"scenario": step["scenario"], "state": step["state"]})


# --------------------------------------------------------------------------- ожидания


def expect_sql(run: Run, e: dict) -> object:
    return sql(run, e["db"], e["query"])


def expect_field(run: Run, e: dict) -> object:
    try:
        return pick(run.responses[e.get("response", "last")], e["path"])
    except (KeyError, IndexError, TypeError) as error:
        raise Failed(f"поля {e['path']} нет в ответе") from error


def expect_no_field(run: Run, e: dict) -> object:
    try:
        pick(run.responses[e.get("response", "last")], e["path"])
    except (KeyError, IndexError, TypeError):
        return "нет"
    return "есть"


def expect_log(run: Run, e: dict) -> object:
    logs = shell([*run.stand["compose"], "logs", "--no-color", e["service"]])
    return "есть" if e["contains"] in logs else "нет"


def expect_stub_log(run: Run, e: dict) -> object:
    return "есть" if e["contains"] in stub_call(run, "/__log") else "нет"


def expect_egress(run: Run, e: dict) -> object:
    return stub_call(run, "/__egress").strip()


def expect_const(run: Run, e: dict) -> object:
    return e["value"]


#: Вид шага → (что нужно от stand.json, обязательные поля, исполнение). Объявлено один раз:
#: проверка до запуска и сам прогон читают отсюда.
STEPS = {
    "http": ("base_url", ("method", "path"), http),
    "wait": ("db", ("db", "query", "until", "timeout_s"), wait),
    "container": ("compose", ("action", "service"), container),
    "stub": ("stub_url", ("scenario", "state"), stub),
}
#: Вид ожидания → (что нужно от stand.json, обязательные поля, получение фактического).
#: Ожидаемое — поле `expect`; «есть»/«нет» — у ожиданий наличия.
EXPECTS = {
    "sql": ("db", ("db", "query", "expect"), expect_sql),
    "field": (None, ("path", "expect"), expect_field),
    "no_field": (None, ("path",), expect_no_field),
    "log": ("compose", ("service", "contains"), expect_log),
    "stub_log": ("stub_url", ("contains",), expect_stub_log),
    "no_provider_egress": ("stub_url", (), expect_egress),
    "const": (None, ("value", "expect"), expect_const),
}
#: Чего ждать, если `expect` не задан: отсутствие поля — «нет», запись в журнале — «есть».
DEFAULT_EXPECT = {"no_field": "нет", "log": "есть", "stub_log": "есть", "no_provider_egress": "ok"}


# --------------------------------------------------------------------------- проверка до запуска


def problems_of(items: list[dict], kinds: dict, stand: dict, where: str) -> list[str]:
    problems = []
    for n, item in enumerate(items, 1):
        kind = item.get("kind")
        if kind not in kinds:
            problems.append(f"{where} {n}: вид «{kind}» неизвестен — есть {', '.join(kinds)}")
            continue
        need, fields, _ = kinds[kind]
        if need and need not in stand:
            problems.append(f"{where} {n} ({kind}): в {STAND} нет «{need}»")
        problems += [f"{where} {n} ({kind}): нет поля «{f}»" for f in fields if f not in item]
    return problems


def validate(scenario: dict, stand: dict) -> list[str]:
    """Отказ до запуска: незнакомый вид, нет поля, стенд не описывает нужное."""
    required = ("name", "зачем", "steps", "expect")
    problems = [f"нет поля «{f}»" for f in required if f not in scenario]
    if not problems:
        problems += problems_of(scenario["steps"], STEPS, stand, "шаг")
        problems += problems_of(scenario["expect"], EXPECTS, stand, "ожидание")
    return problems


# --------------------------------------------------------------------------- прогон


def check_expect(run: Run, e: dict) -> None:
    actual = fill(EXPECTS[e["kind"]][2](run, fill(e, run)), run)
    wanted = e.get("expect", DEFAULT_EXPECT.get(e["kind"]))
    if str(actual) != str(wanted):
        raise Failed(f"«{e.get('name', e['kind'])}»: {actual!r}, ждали {wanted!r}")


def run_scenario(scenario: dict, stand: dict, mode: Mode) -> str:
    """Пусто — прошёл; иначе — первое несбывшееся ожидание или упавший шаг."""
    run = Run(stand, mode, vars=dict(os.environ))
    try:
        for step in scenario["steps"]:
            STEPS[step["kind"]][2](run, fill(step, run))
        for e in scenario["expect"]:
            check_expect(run, e)
    except Failed as failure:
        return str(failure)
    except (urllib.error.URLError, OSError, subprocess.TimeoutExpired) as error:
        return f"стенд недоступен: {error}"
    return ""


def load(root: Path, name: str) -> dict:
    return json.loads((root / SCENARIOS / name / "scenario.json").read_text(encoding="utf-8"))


def scenario_names(root: Path) -> list[str]:
    folder = root / SCENARIOS
    return sorted(p.parent.name for p in folder.glob("*/scenario.json") if p.parent.name != CONTROL)


# --------------------------------------------------------------------------- квитанция, запись


def tree_hash(root: Path) -> str:
    """Хеш дерева кода: файлы git, кроме неисполняемого (tools/receipt.json, «exclude»)."""
    exclude = json.loads((root / RECEIPT_CONFIG).read_text(encoding="utf-8"))["exclude"]
    listed = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],  # noqa: S607
                            cwd=root, capture_output=True, text=True, check=True,
                            env=git_env()).stdout.split("\n")
    digest = hashlib.sha256()
    for path in sorted(p for p in listed if p and not any(fnmatch.fnmatch(p, x) for x in exclude)):
        file = root / path
        if file.is_file():
            digest.update(path.encode() + b"\0" + hashlib.sha256(file.read_bytes()).digest())
    return digest.hexdigest()[:16]


def state_dir() -> Path:
    """Каталог состояния стенда — вне репозитория: квитанцию читает сторож push."""
    return Path(os.environ.get("STAND_STATE_DIR", Path.home() / ".local/state/stand"))


def write_receipt(root: Path, mode: Mode, passed: bool) -> str:
    receipt = {"tree": tree_hash(root), "mode": mode.value, "outcome": "прошло" if passed
               else "не прошло", "time": datetime.now(UTC).isoformat(timespec="seconds")}
    state_dir().mkdir(parents=True, exist_ok=True)
    with (state_dir() / "receipts.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(receipt, ensure_ascii=False) + "\n")
    return f"квитанция: дерево {receipt['tree']} режим {mode.value} исход {receipt['outcome']}"


def head(root: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,  # noqa: S607
                          text=True, check=False, env=git_env()).stdout.strip()


def record_run(root: Path, contour: str, results: list[dict]) -> Path:
    """Запись прогона для переноса статусов (tools/req_status.py)."""
    now = datetime.now(UTC)
    record = {"commits": {"repo": head(root)}, "contour": contour,
              "time": now.isoformat(timespec="seconds"), "scenarios": results}
    path = root / RUNS / f"{now:%Y%m%d-%H%M%S}-{contour}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- команды


def cmd_run(root: Path, names: list[str], mode: Mode, contour: str, record: bool) -> int:
    stand = json.loads((root / STAND).read_text(encoding="utf-8"))
    scenarios = {n: load(root, n) for n in names or scenario_names(root)}
    refused = {n: validate(s, stand) for n, s in scenarios.items()}
    if any(refused.values()):
        for name, problems in refused.items():
            for problem in problems:
                print(f"отказ до запуска: {name}: {problem}")
        return 2
    os.environ["SCENARIO_MODE"] = mode.value
    results = []
    for name, scenario in scenarios.items():
        failure = run_scenario(scenario, stand, mode)
        print(f"{'ok  ' if not failure else 'FAIL'}  {name}" + (f" — {failure}" if failure else ""))
        results.append({"name": name, "требования": scenario.get("требования", []),
                        "итог": "прошёл" if not failure else "не прошёл", "не сбылось": failure})
    if record:
        print(f"запись прогона: {record_run(root, contour, results).relative_to(root)}")
    passed = all(r["итог"] == "прошёл" for r in results)
    print(write_receipt(root, mode, passed))
    return 0 if passed else 1


def cmd_control(root: Path) -> int:
    """Контрольный сценарий с невыполнимым ожиданием: раннер обязан на нём покраснеть."""
    scenario = load(root, CONTROL)
    failure = run_scenario(scenario, {}, Mode.REPLAY)
    if validate(scenario, {}) or not failure:
        print("раннер не покраснел на контрольном сценарии — ему нельзя верить")
        return 1
    print(f"контрольный сценарий красный, как и должен: {failure}")
    return 0


def main(root: Path = ROOT) -> int:
    if "--selftest" in sys.argv:
        from scenario_selftest import selftest
        return selftest()
    if "--control" in sys.argv:
        return cmd_control(root)
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("names", nargs="*")
    p.add_argument("--mode", required=True, choices=[m.value for m in Mode])
    p.add_argument("--contour", default="local")
    p.add_argument("--record-run", action="store_true")
    args = parser.parse_args()
    return cmd_run(root, args.names, Mode(args.mode), args.contour, args.record_run)


if __name__ == "__main__":
    sys.exit(main())

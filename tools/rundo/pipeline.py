"""Стадии прогона Рундо: чистый worktree → план → Gemini → red → green → приёмка → черновик.

Контроллер не принимает результат роли по словам: только файл, хеш, квитанция и фактический
вывод проверок. Каждый переход сохраняется в `state.json`; повтор команды продолжает прогон.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tools.backlog import MARK
from tools.project_config import backlog_config
from tools.rundo import artifacts, evidence, prompts
from tools.rundo.artifacts import ArtifactError
from tools.rundo.bb import Bb, BbFault
from tools.rundo.config import (
    ACTORS,
    PARENT_ENV,
    PIPELINE_DIR,
    PROJECT_ENV,
    Role,
    Stage,
)
from tools.rundo.evidence import EvidenceError
from tools.rundo.state import State, StateStore

#: Первый spawn возвращает ID раньше, чем environmentId: provisioning идёт после ответа.
ENVIRONMENT_ATTEMPTS = 90
ENVIRONMENT_POLL_SECONDS = 2.0
READY_ATTEMPTS = 10
READY_POLL_SECONDS = 1.0

ROLE_ARTIFACTS = {
    Role.PLAN: ("plan.json",),
    Role.GEMINI: ("gemini.json",),
    Role.DRAFT: ("pr.json",),
}


class PipelineError(RuntimeError):
    def __init__(self, reason: str, *, role: str | None = None, thread: str | None = None,
                 file: str | None = None) -> None:
        super().__init__(reason)
        self.role = role
        self.thread = thread
        self.file = file


class Pipeline:
    def __init__(self, repo: Path, number: str, request_file: str | None,
                 env: Mapping[str, str]) -> None:
        self.repo = repo.resolve()
        self.number = number
        self.request_file = request_file
        self.env = dict(env)
        self.state_dir = self.repo / PIPELINE_DIR / number
        self.store = StateStore(self.state_dir)
        self.bb = Bb(self.state_dir / "commands.log", self.env)
        self.state = State(number="", project="", parent="", token="")
        self.request = ""

    @property
    def exchange(self) -> Path:
        return Path(self.state.worktree or "") / PIPELINE_DIR / self.number

    def run(self) -> int:
        with self.store.locked():
            try:
                self._open()
                self._recover()
                while self.state.stage != Stage.DONE.value:
                    self._advance()
                self._verify_completed()
            except Exception as error:
                self._record_failure(error)
                raise
        self._report()
        return 0

    def _record_failure(self, error: Exception) -> None:
        if not self.state.token:
            return
        reason = str(error)
        self.state.error = reason
        self.store.save(self.state)
        artifacts.write_json(self.state_dir / "error.json", {
            "stage": self.state.stage,
            "role": getattr(error, "role", None),
            "thread": getattr(error, "thread", None),
            "file": getattr(error, "file", None),
            "reason": reason,
        })

    def _open(self) -> None:
        stored = self.store.load()
        if stored is not None:
            self.state = stored
            return
        self.state = self._create()
        self.store.save(self.state)

    def _create(self) -> State:
        project = self.env.get(PROJECT_ENV, "").strip()
        parent = self.env.get(PARENT_ENV, "").strip()
        if not project or not parent:
            raise PipelineError(f"нужны {PROJECT_ENV} и {PARENT_ENV} — запуск из BB-треда")
        token = uuid.uuid4().hex[:12]
        # Вне дерева контроллера: иначе pytest в его `check.sh --quick` заходит в worktree/tests
        # и падает на ImportPathMismatchError. Путь закрепляется в state.json и не меняется.
        worktree = self.repo.parent / f".rundo-{self.number}-{token}"
        return State(
            number=self.number, project=project, parent=parent, token=token,
            base=evidence.head(self.repo), worktree=str(worktree),
        )

    def _advance(self) -> None:
        if self._plan_drift():
            self._invalidate()
            self.state.stage = Stage.PLAN.value
            self._commit()
        stage = Stage(self.state.stage)
        handler = self._handlers().get(stage)
        if handler is None:
            raise PipelineError(f"неизвестная стадия {stage.value}")
        try:
            handler()
        except (PipelineError, ArtifactError, EvidenceError, BbFault) as error:
            self._annotate(error, stage)
            raise
        finally:
            self.store.save(self.state)

    def _annotate(self, error: Exception, stage: Stage) -> None:
        role = self._stage_role(stage)
        if role is None:
            return
        if getattr(error, "role", None) is None:
            error.role = role.value  # type: ignore[attr-defined]
        if getattr(error, "thread", None) is None:
            error.thread = self.state.roles.get(role.value)  # type: ignore[attr-defined]
        if getattr(error, "file", None) is None:
            error.file = str(self.exchange)  # type: ignore[attr-defined]

    def _stage_role(self, stage: Stage) -> Role | None:
        return {
            Stage.PLAN: Role.PLAN,
            Stage.GEMINI: Role.GEMINI,
            Stage.RED: Role.RED,
            Stage.GREEN: Role.IMPLEMENTATION,
            Stage.DRAFT: Role.DRAFT,
        }.get(stage)

    def _handlers(self):
        return {
            Stage.WORKTREE: self._stage_worktree,
            Stage.PLAN: self._stage_plan,
            Stage.GEMINI: self._stage_gemini,
            Stage.RED: self._stage_red,
            Stage.GREEN: self._stage_green,
            Stage.ACCEPTANCE: self._stage_acceptance,
            Stage.DRAFT: self._stage_draft,
        }

    def _commit(self) -> None:
        self.store.save(self.state)

    def _plan_drift(self) -> bool:
        path = self.exchange / "plan.json"
        if not self.state.plan_sha256 or not path.is_file():
            return False
        return artifacts.sha256_file(path) != self.state.plan_sha256

    def _stage_worktree(self) -> None:
        state = self.state
        if not state.base:
            state.base = evidence.head(self.repo)
        self._ensure_worktree()
        self.request = self._request_text()
        self.exchange.mkdir(parents=True, exist_ok=True)
        (self.exchange / "request.md").write_text(self.request, encoding="utf-8")
        state.stage = Stage.PLAN.value
        self._commit()

    def _ensure_worktree(self) -> None:
        path = Path(self.state.worktree or "")
        if path.is_dir() and (path / ".git").exists():
            return
        if path.exists():
            raise PipelineError(f"путь worktree занят: {path}")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            evidence.worktree_add(self.repo, path, self.state.base or "HEAD")
        except evidence.EvidenceError as error:
            raise PipelineError(str(error), file=str(path)) from error
        if evidence.changed_paths(path):
            raise PipelineError(f"свежий worktree не чист: {sorted(evidence.changed_paths(path))}",
                                file=str(path))
        _link_venv(self.repo, path)
        self._prepare_environment(path)
        if evidence.head(path) != self.state.base:
            raise PipelineError(f"worktree создан не от закреплённой базы {self.state.base}")

    def _prepare_environment(self, worktree: Path) -> None:
        """Окружение worktree: ссылки и доверие Codex штатным скриптом проекта."""
        script = worktree / ".bb-env-setup.sh"
        if not script.is_file():
            return
        done = evidence.run_command(
            ["sh", str(script)], worktree, evidence.QUICK_TIMEOUT_SECONDS,
        )
        if done.returncode != 0:
            self.state.warnings.append(
                f".bb-env-setup.sh: код {done.returncode}: {done.stderr.strip()[-500:]}",
            )

    def _request_text(self) -> str:
        if self.request:
            return self.request
        saved = self.exchange / "request.md"
        if saved.is_file():
            self.request = saved.read_text(encoding="utf-8")
            return self.request
        if self.request_file:
            self.request = Path(self.request_file).read_text(encoding="utf-8").strip()
            return self.request
        config = backlog_config(self.repo)
        if not config["prefix"]:
            raise PipelineError("в .backlog.json требуется prefix")
        task = config["tasks"] / f"{config['prefix']}-{self.number.zfill(4)}.md"
        if not task.is_file():
            raise PipelineError(f"нет файла задачи {task}; передайте --request-file")
        self.request = _description(task.read_text(encoding="utf-8"))
        return self.request

    def _spawn(self, role: Role) -> str:
        state = self.state
        existing = state.roles.get(role.value)
        if existing:
            self._wait_ready(existing, role)
            return existing
        for artifact in ROLE_ARTIFACTS.get(role, ()):
            (self.exchange / artifact).unlink(missing_ok=True)
        prompt_file = self._write_prompt(role)
        token = f"{state.token}-{role.value}"
        title = f"[{role.value}] #{self.number} {token}"
        state.pending = {"kind": "spawn", "role": role.value, "token": token, "title": title}
        self._commit()
        environment = state.worktree if state.environment_id is None else state.environment_id
        response = self.bb.spawn(
            ACTORS[role], title, prompt_file, state.project, state.parent, str(environment),
        )
        thread_id = _thread_id(response)
        first = state.environment_id is None
        state.roles[role.value] = thread_id
        self._commit()
        self._adopt_environment(thread_id, require_path=first, initial=response)
        if role is Role.PLAN:
            self._verify_environment()
        state.pending = None
        self._commit()
        self._verify_child(thread_id)
        self._wait_ready(thread_id, role)
        return thread_id

    def _write_prompt(self, role: Role) -> Path:
        context = prompts.PromptContext(
            self.number, str(self.state.worktree), str(self.exchange), self._request_text(),
        )
        path = self.state_dir / "prompts" / f"{role.value}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prompts.build(role, context), encoding="utf-8")
        return path

    def _adopt_environment(self, thread_id: str, require_path: bool,
                           initial: dict[str, Any] | None = None) -> str:
        """Environment ID появляется после provisioning: ждём его в треде, не создавая дубль."""
        state = self.state
        for attempt in range(ENVIRONMENT_ATTEMPTS):
            data = initial if attempt == 0 and initial else self.bb.thread_show(thread_id)
            environment_id, path, host = _environment_of(data)
            if environment_id:
                if state.environment_id and environment_id != state.environment_id:
                    raise PipelineError("тред привязан к другому окружению",
                                        thread=thread_id, file=path)
                if state.environment_id or not require_path or _same_path(path, state.worktree):
                    state.environment_id = environment_id
                    state.host = state.host or host
                    return environment_id
                if path:
                    raise PipelineError(
                        f"окружение ведёт на {path}, ожидался worktree {state.worktree}",
                        thread=thread_id, file=path,
                    )
            if attempt + 1 < ENVIRONMENT_ATTEMPTS:
                time.sleep(ENVIRONMENT_POLL_SECONDS)
        raise PipelineError("окружение треда не подтвердилось после provisioning",
                            thread=thread_id)

    def _wait_ready(self, thread_id: str, role: Role) -> None:
        """Idle без очереди: иначе два хода роли гоняются за один артефакт."""
        status: object = None
        queued: object = None
        for attempt in range(READY_ATTEMPTS):
            self.bb.wait_idle(thread_id)
            data = self.bb.thread_show(thread_id)
            thread = data.get("thread") if isinstance(data.get("thread"), dict) else data
            status = thread.get("status")
            queued = thread.get("queuedMessageCount") or 0
            if status == "idle" and queued == 0:
                return
            if attempt + 1 < READY_ATTEMPTS:
                time.sleep(READY_POLL_SECONDS)
        raise PipelineError(
            f"тред {thread_id} не готов: status={status!r}, queuedMessageCount={queued}",
            role=role.value, thread=thread_id,
        )

    def _verify_environment(self) -> None:
        try:
            value = self.bb.environment(str(self.state.environment_id))
        except BbFault:
            return
        path = value.get("path")
        self.state.host = self.state.host or value.get("hostId")
        if not path or Path(path).resolve() != Path(self.state.worktree or "").resolve():
            raise PipelineError(
                f"окружение {self.state.environment_id} указывает на {path}, "
                f"ожидался worktree {self.state.worktree}", file=str(path),
            )

    def _verify_child(self, thread_id: str) -> None:
        state = self.state
        matches = [item for item in self.bb.threads_by_id(thread_id)
                   if item.get("id") == thread_id]
        if not matches:
            raise PipelineError("дочерний тред не виден в bb thread list", thread=thread_id)
        thread = matches[0]
        if thread.get("parentThreadId") != state.parent:
            raise PipelineError("у дочернего треда чужой родитель", thread=thread_id)
        if thread.get("projectId") != state.project:
            raise PipelineError("у дочернего треда чужой проект", thread=thread_id)
        if thread.get("visibility") not in (None, "visible"):
            raise PipelineError("дочерний тред невидим", thread=thread_id)
        environment_id = thread.get("environmentId")
        if environment_id and environment_id != state.environment_id:
            raise PipelineError("у дочернего треда другое окружение", thread=thread_id)

    def _recover(self) -> None:
        pending = self.state.pending
        if not pending:
            return
        if pending.get("kind") == "spawn":
            self._recover_spawn(pending)
        elif pending.get("kind") == "rpc":
            self._recover_rpc(pending)
        else:
            raise PipelineError(f"неизвестный pending: {pending.get('kind')!r}")

    def _recover_spawn(self, pending: dict[str, Any]) -> None:
        state = self.state
        title = pending.get("title")
        matches = [
            item for item in self.bb.children(state.project, state.parent)
            if title and item.get("title") == title
            and item.get("parentThreadId") == state.parent
        ]
        if len(matches) > 1:
            raise PipelineError(f"потерянный spawn: {len(matches)} совпадающих тредов")
        if matches:
            thread_id = str(matches[0]["id"])
            role = str(pending.get("role"))
            if state.roles.get(role) not in (None, thread_id):
                raise PipelineError("у роли уже другой тред, совпадение неоднозначно",
                                    role=role, thread=thread_id)
            state.roles[role] = thread_id
            self._commit()
            self._adopt_environment(
                thread_id, require_path=state.environment_id is None, initial=matches[0],
            )
            if role == Role.PLAN.value:
                self._verify_environment()
        state.pending = None
        self._commit()

    def _recover_rpc(self, pending: dict[str, Any]) -> None:
        receipt = Path(str(pending.get("receipt") or ""))
        if receipt.is_file():
            self.state.pending = None
            self._commit()
            return
        raise PipelineError(
            f"RPC {pending.get('method')} мог быть отправлен, ответ не сохранён; "
            "повтор запрещён: идемпотентность не подтверждена",
            file=str(pending.get("input") or ""),
        )

    def _stage_plan(self) -> None:
        state = self.state
        self._spawn(Role.PLAN)
        plan = self._plan()
        plan_sha = artifacts.sha256_file(self.exchange / "plan.json")
        self._adopt_plan(plan_sha)
        state.plan_sha256 = plan_sha
        self._requirements_review(plan)
        self._plan_review(plan)
        gate = artifacts.build_gate(plan, plan_sha)
        artifacts.write_json(self.exchange / "gate.json", gate)
        state.gate = gate
        state.plan_tree = self._evidence().revision(artifacts.planned_paths(plan))
        state.plan_changed = sorted(self._evidence().changed())
        state.stage = Stage.GEMINI.value if gate["geminiRequired"] else Stage.RED.value
        self._commit()

    def _adopt_plan(self, plan_sha: str) -> None:
        if self.state.plan_sha256 and self.state.plan_sha256 != plan_sha:
            self._invalidate()

    def _invalidate(self) -> None:
        state = self.state
        state.gate = None
        state.plan_tree = None
        state.plan_changed = []
        state.test_sha256 = None
        state.checks_revision = None
        state.accepted_revision = None
        state.receipts = {}
        state.braille = None
        for name in ("requirements", "plan", "acceptance"):
            (self.exchange / f"{name}.receipt.json").unlink(missing_ok=True)
        for role in (Role.GEMINI, Role.RED, Role.IMPLEMENTATION, Role.DRAFT):
            state.roles.pop(role.value, None)

    def _requirements_review(self, plan: dict[str, Any]) -> None:
        parent = None
        receipt = self._load_receipt("requirements", parent)
        if receipt is None:
            receipt = self._rpc("requirements", {
                "threadId": self.state.parent,
                "input": {"request": artifacts.truncate(self._request_text()),
                          "requirements": plan["requirements"]},
            }, parent)
        self._verify_digest("requirements", receipt)
        self.state.receipts["requirements"] = str(receipt["reviewId"])

    def _plan_review(self, plan: dict[str, Any]) -> None:
        parent = self.state.receipts["requirements"]
        receipt = self._load_receipt("plan", parent)
        if receipt is None:
            receipt = self._rpc("plan", {
                "threadId": self.state.parent,
                "input": {"requirementsReviewId": parent, "steps": plan["steps"]},
            }, parent)
        self._verify_digest("plan", receipt)
        self.state.receipts["plan"] = str(receipt["reviewId"])

    def _load_receipt(self, name: str, parent: str | None,
                      revision: str | None = None) -> dict[str, Any] | None:
        path = self.exchange / f"{name}.receipt.json"
        if not path.is_file():
            return None
        document = artifacts.read_json(path)
        artifacts.validate_receipt(document, name, parent, revision)
        return document

    def _rpc(self, name: str, payload: dict[str, Any], parent: str | None,
             revision: str | None = None) -> dict[str, Any]:
        method = f"jev.workflow.{name}"
        input_path = self.exchange / f"{name}.input.json"
        receipt_path = self.exchange / f"{name}.receipt.json"
        artifacts.write_json(input_path, payload)
        self.state.pending = {"kind": "rpc", "method": method,
                              "input": str(input_path), "receipt": str(receipt_path)}
        self._commit()
        receipt = self.bb.rpc(method, input_path)
        artifacts.write_json(receipt_path, receipt)
        artifacts.validate_receipt(receipt, name, parent, revision)
        self.state.pending = None
        self._commit()
        return receipt

    def _verify_digest(self, name: str, receipt: dict[str, Any]) -> None:
        envelope = artifacts.read_json(self.exchange / f"{name}.input.json")["input"]
        artifacts.verify_evidence(receipt, name, self._canonical_input(name), envelope)

    def _canonical_input(self, name: str) -> dict[str, Any]:
        sent = artifacts.read_json(self.exchange / "requirements.input.json")["input"]
        requirements = artifacts.canonical_requirements(sent["requirements"])
        if name == "requirements":
            return {"request": str(sent["request"]).strip(), "requirements": requirements}
        steps = artifacts.canonical_steps(self._plan()["steps"])
        if name == "plan":
            return {"requirements": requirements, "steps": steps}
        inner = artifacts.read_json(self.exchange / "acceptance.input.json")["input"]
        return {
            "planReviewId": inner["planReviewId"],
            "revision": inner["revision"],
            "diff": str(inner["diff"]).strip(),
            "testOutput": str(inner["testOutput"]).strip(),
            "requirements": requirements,
            "steps": steps,
        }

    def _stage_gemini(self) -> None:
        state = self.state
        gate = state.gate or {}
        if not gate.get("geminiRequired"):
            state.stage = Stage.RED.value
            return
        self._spawn(Role.GEMINI)
        document = artifacts.read_json(self.exchange / "gemini.json")
        verdict = artifacts.validate_gemini(document, str(state.plan_sha256))
        plan = self._plan()
        if state.plan_tree and self._tree(plan) != state.plan_tree:
            raise PipelineError("дерево изменилось после плана: Gemini видел другой снимок",
                                role=Role.GEMINI.value)
        if verdict != "approved":
            raise PipelineError(f"Gemini не одобрил план: {verdict}",
                                role=Role.GEMINI.value, file=str(self.exchange / "gemini.json"))
        state.stage = Stage.RED.value
        self._commit()

    def _stage_red(self) -> None:
        state = self.state
        self._spawn(Role.RED)
        plan = self._plan()
        node = artifacts.red_node(plan)
        changed = self._evidence().changed()
        unexpected = (
            (changed - set(state.plan_changed))
            - artifacts.paths_with_purpose(plan, "test")
        )
        if unexpected:
            raise PipelineError(f"роль теста правила лишнее: {sorted(unexpected)}",
                                role=Role.RED.value)
        if node.path not in changed:
            raise PipelineError(f"объявленного теста {node.path} нет среди изменений",
                                role=Role.RED.value, file=node.path)
        state.test_sha256 = self._evidence().file_sha(node.path)
        self._commit()
        self._evidence().run_red(node, self._tree(plan))
        state.stage = Stage.GREEN.value
        self._commit()

    def _stage_green(self) -> None:
        state = self.state
        self._spawn(Role.IMPLEMENTATION)
        plan = self._plan()
        node = artifacts.red_node(plan)
        self._require_test_unchanged(node)
        self._require_within_plan(plan)
        tree = self._tree(plan)
        self._evidence().run_green(node, tree)
        test_paths = sorted(artifacts.paths_with_purpose(plan, "test"))
        self._evidence().run_tests(test_paths, tree)
        self._evidence().run_quick(tree)
        state.braille = self._evidence().collect_braille(self.env, tree)
        after = self._tree(plan)
        if after != tree:
            raise PipelineError("снимок изменился во время проверок",
                                role=Role.IMPLEMENTATION.value, file=str(self.exchange))
        state.checks_revision = tree
        state.stage = Stage.ACCEPTANCE.value
        self._commit()

    def _require_test_unchanged(self, node: artifacts.RedNode) -> None:
        current = self._evidence().file_sha(node.path)
        if current != self.state.test_sha256:
            raise PipelineError("файл теста изменён после красного прогона",
                                role=Role.IMPLEMENTATION.value, file=node.path)

    def _require_within_plan(self, plan: dict[str, Any]) -> None:
        changed = self._evidence().changed()
        unexpected = changed - set(artifacts.planned_paths(plan))
        if unexpected:
            raise PipelineError(f"реализация вышла за план: {sorted(unexpected)}",
                                role=Role.IMPLEMENTATION.value)
        if not changed & artifacts.paths_with_purpose(plan, "implementation"):
            raise PipelineError("реализация не изменила ни одного пути implementation",
                                role=Role.IMPLEMENTATION.value)

    def _stage_acceptance(self) -> None:
        state = self.state
        plan = self._plan()
        revision = self._tree(plan)
        if state.checks_revision and revision != state.checks_revision:
            raise PipelineError("ревизия не совпала с проверенным снимком",
                                file=str(self.exchange))
        self._commit()
        parent = state.receipts["plan"]
        receipt = self._load_receipt("acceptance", parent, revision)
        if receipt is None:
            receipt = self._rpc("acceptance", {
                "threadId": state.parent,
                "input": {
                    "planReviewId": parent, "revision": revision,
                    "diff": self._evidence().diff(plan),
                    "testOutput": self._evidence().test_output(plan),
                },
            }, parent, revision)
        self._verify_digest("acceptance", receipt)
        self._lookup_current(receipt, revision, parent)
        state.receipts["acceptance"] = str(receipt["reviewId"])
        state.accepted_revision = revision
        state.stage = Stage.DRAFT.value
        self._commit()

    def _lookup_current(self, receipt: dict[str, Any], revision: str, parent: str) -> None:
        path = self.exchange / "acceptance.lookup.json"
        artifacts.write_json(path, {"reviewId": receipt["reviewId"], "revision": revision})
        value = self.bb.rpc("jev.workflow.receipt", path)
        if value.get("error") or value.get("freshness") != "current":
            raise PipelineError(f"приёмка не current: {value.get('freshness')!r}",
                                file=str(path))
        if value.get("reviewId") != receipt.get("reviewId"):
            raise PipelineError("jev.workflow.receipt вернул чужую квитанцию", file=str(path))
        if value.get("stage") != "acceptance" or value.get("status") != "passed":
            raise PipelineError("jev.workflow.receipt: не зелёная приёмка", file=str(path))
        if value.get("evidenceDigest") != receipt.get("evidenceDigest"):
            raise PipelineError("jev.workflow.receipt: другой evidenceDigest", file=str(path))
        if value.get("revision") != revision:
            raise PipelineError("приёмка относится к другой ревизии", file=str(path))
        if value.get("parentReviewId") != parent:
            raise PipelineError("приёмка ссылается на чужой план", file=str(path))

    def _stage_draft(self) -> None:
        state = self.state
        plan = self._plan()
        self._require_accepted(plan)
        self._spawn(Role.DRAFT)
        document = artifacts.read_json(self.exchange / "pr.json")
        artifacts.validate_pr(document, self.number)
        self._require_accepted(plan)
        state.stage = Stage.DONE.value
        self._commit()

    def _verify_completed(self) -> None:
        if self._plan_drift():
            raise PipelineError(
                "план изменился после приёмки", file=str(self.exchange / "plan.json"),
            )
        plan = self._plan()
        self._require_accepted(plan)
        self._require_within_plan(plan)
        document = artifacts.read_json(self.exchange / "pr.json")
        artifacts.validate_pr(document, self.number)

    def _require_accepted(self, plan: dict[str, Any]) -> None:
        current = self._tree(plan)
        if current != self.state.accepted_revision:
            raise PipelineError("ревизия изменилась после приёмки",
                                role=Role.DRAFT.value, file=str(self.exchange / "pr.json"))

    def _plan(self) -> dict[str, Any]:
        plan = artifacts.read_json(self.exchange / "plan.json")
        artifacts.validate_plan(plan)
        return plan

    def _evidence(self) -> evidence.Evidence:
        return evidence.Evidence(Path(self.state.worktree or ""), self.exchange)

    def _tree(self, plan: dict[str, Any]) -> str:
        return self._evidence().revision(artifacts.planned_paths(plan))

    def _report(self) -> None:
        state = self.state
        print(f"#{self.number}: {state.stage}")
        print(f"worktree: {state.worktree}")
        print(f"exchange: {self.exchange}")
        for role, thread in state.roles.items():
            print(f"роль {role}: {thread}")
        if state.plan_sha256:
            print(f"plan sha256: {state.plan_sha256}")
        if state.gate:
            print(f"gate: {state.gate}")
        for stage, receipt in state.receipts.items():
            print(f"Jev v2 {stage}: {receipt}")
        if state.accepted_revision:
            print(f"принятая ревизия: {state.accepted_revision}")
        if state.braille:
            print(f"braille: {state.braille}")
        for warning in state.warnings:
            print(f"предупреждение: {warning}")
        print(f"черновик PR: {self.exchange / 'pr.json'}")


def _environment_of(data: object) -> tuple[str | None, str | None, str | None]:
    if not isinstance(data, dict):
        return None, None, None
    thread = data.get("thread") if isinstance(data.get("thread"), dict) else data
    environment = data.get("environment") if isinstance(data.get("environment"), dict) else {}
    environment_id = thread.get("environmentId") or environment.get("id")
    host = environment.get("hostId") or thread.get("hostId")
    return (
        str(environment_id) if environment_id else None,
        environment.get("path"),
        str(host) if host else None,
    )


def _same_path(value: object, worktree: str | None) -> bool:
    return isinstance(value, str) and bool(value) and Path(value).resolve() == Path(
        worktree or "",
    ).resolve()


def _thread_id(response: object) -> str:
    if not isinstance(response, dict):
        raise PipelineError("bb thread spawn не вернул объект")
    thread = response.get("thread") if isinstance(response.get("thread"), dict) else response
    identifier = thread.get("id")
    if not identifier:
        raise PipelineError("bb thread spawn не вернул id треда")
    return str(identifier)


def _link_venv(repo: Path, worktree: Path) -> None:
    """Ссылка на окружение проекта: `.venv` игнорируется git, но нужна `tools/check.sh`."""
    if (worktree / ".venv").exists() or (worktree / ".venv").is_symlink():
        return
    source = repo / ".venv"
    if (source / "bin" / "python3").is_file():
        (worktree / ".venv").symlink_to(source.resolve(), target_is_directory=True)


def _description(text: str) -> str:
    marker = "## Описание из трекера"
    index = text.find(marker)
    if index < 0:
        raise PipelineError("в файле задачи нет раздела «Описание из трекера»")
    body = text[index + len(marker):].partition(MARK)[0]
    value = body.partition("\n## Комментарии из трекера\n")[0].strip()
    if not value:
        raise PipelineError("раздел «Описание из трекера» пуст")
    return value

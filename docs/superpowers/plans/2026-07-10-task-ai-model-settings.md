# Task AI Model Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add workspace-level and per-task AI model/reasoning controls for Agent Desk while defaulting to GPT-5.5 with `xhigh` reasoning.

**Architecture:** Add a focused `agent_desk.ai_settings` module that owns the model catalog, normalization, and Codex CLI argument construction. Repo config and scheduler runtime settings provide workspace defaults; each run stores its effective `ai_model` and `ai_reasoning_effort`; worker, continuation, and AI review commands read those run fields. The dashboard exposes the catalog and update APIs, then renders linked model/reasoning controls in Workspace Settings and task cards.

**Tech Stack:** Python 3.11 standard library, stdlib `unittest`, stdlib `http.server`, static HTML/CSS/JavaScript, `codex exec` CLI flags.

## Global Constraints

- No new runtime dependencies; `pyproject.toml` intentionally keeps `dependencies = []`.
- Use stdlib `unittest` exclusively.
- Default model must be exactly `gpt-5.5`.
- Default reasoning effort must be exactly `xhigh`.
- Workspace Settings saves runtime settings for the running service; persistent startup defaults live in `config/repos.toml`.
- Existing run JSON and config files must load without migration.
- Running tasks must not be editable in place.
- Dependency-extraction model settings are out of scope.
- Use `apply_patch` for manual edits.

---

## File Structure

- Create `agent_desk/ai_settings.py`: model catalog, default constants, normalization helpers, payload helper, Codex argument helper.
- Modify `agent_desk/config.py`: repo defaults, TOML loading, generated example config, appended repo config.
- Modify `agent_desk/store.py`: read-time normalization and new-record defaults for run-level AI settings.
- Modify `agent_desk/scheduler.py`: scheduler settings fields, runtime update plumbing, run inheritance, task-level update method.
- Modify `agent_desk/worker.py`: pass run AI settings to initial `codex exec`.
- Modify `agent_desk/continuation.py`: pass run AI settings to `codex exec resume`.
- Modify `agent_desk/ai_review.py`: pass run AI settings to independent review `codex exec`.
- Modify `agent_desk/dashboard.py`: state payload catalog, settings route, run AI settings route.
- Modify `agent_desk/static/dashboard.html`: Workspace Settings controls and compact task AI styling.
- Modify `agent_desk/static/dashboard.js`: model/reasoning linked controls, save payloads, per-task update calls.
- Modify tests under `tests/`: focused unittest coverage for each layer above.

---

### Task 1: AI Catalog, Config Defaults, And Store Normalization

**Files:**
- Create: `agent_desk/ai_settings.py`
- Modify: `agent_desk/config.py`
- Modify: `agent_desk/store.py`
- Test: `tests/test_config.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `DEFAULT_AI_MODEL: str`
- Produces: `DEFAULT_AI_REASONING_EFFORT: str`
- Produces: `AI_MODEL_CATALOG: tuple[AIModelOption, ...]`
- Produces: `ai_model_catalog_payload() -> list[dict[str, Any]]`
- Produces: `normalize_ai_settings(model: str | None, reasoning_effort: str | None) -> tuple[str, str]`
- Produces: `codex_ai_args(run: Mapping[str, Any]) -> list[str]`
- Consumes: existing `RepoConfig`, `Store._normalize_record`, and `Store._new_record`

- [ ] **Step 1: Write failing config tests**

Add these assertions to `tests/test_config.py`:

```python
def test_ai_settings_default_to_gpt55_xhigh(self):
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "repos.toml"
        config_path.write_text(
            """
[agent_desk]
data_dir = ".agent-desk"

[[repos]]
name = "octo/example"
local_path = "/repo"
""".strip(),
            encoding="utf-8",
        )

        repo = load_config(config_path).repos[0]

    self.assertEqual(repo.default_ai_model, "gpt-5.5")
    self.assertEqual(repo.default_ai_reasoning_effort, "xhigh")

def test_loads_explicit_ai_settings_from_toml(self):
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "repos.toml"
        config_path.write_text(
            """
[agent_desk]
data_dir = ".agent-desk"

[[repos]]
name = "octo/example"
local_path = "/repo"
default_ai_model = "gpt-5.6-terra"
default_ai_reasoning_effort = "high"
""".strip(),
            encoding="utf-8",
        )

        repo = load_config(config_path).repos[0]

    self.assertEqual(repo.default_ai_model, "gpt-5.6-terra")
    self.assertEqual(repo.default_ai_reasoning_effort, "high")
```

Extend `test_add_project_to_config_appends_repo_from_folder`:

```python
self.assertEqual(config.repos[1].default_ai_model, "gpt-5.5")
self.assertEqual(config.repos[1].default_ai_reasoning_effort, "xhigh")
self.assertIn('default_ai_model = "gpt-5.5"', appended_block)
self.assertIn('default_ai_reasoning_effort = "xhigh"', appended_block)
```

Extend `test_example_config_omits_issue_label_mutation_settings`:

```python
self.assertIn('default_ai_model = "gpt-5.5"', text)
self.assertIn('default_ai_reasoning_effort = "xhigh"', text)
```

- [ ] **Step 2: Write failing store tests**

Add these tests to `tests/test_store.py`:

```python
def test_create_run_defaults_ai_settings(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "desk.sqlite")

        run_id = store.create_run(
            repo_name="octo/example",
            issue_number=5,
            issue_title="AI defaults",
            issue_url="https://github.com/octo/example/issues/5",
            branch_name="agent/issue-5-ai-defaults",
        )
        run = store.get_run(run_id)

    self.assertEqual(run["ai_model"], "gpt-5.5")
    self.assertEqual(run["ai_reasoning_effort"], "xhigh")

def test_existing_run_records_normalize_missing_ai_settings(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = Store(root / "desk.sqlite")
        run_id = store.create_run(
            repo_name="octo/example",
            issue_number=6,
            issue_title="Old record",
            issue_url="https://github.com/octo/example/issues/6",
            branch_name="agent/issue-6-old-record",
        )
        path = store._find_path(run_id)
        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("ai_model", None)
        record.pop("ai_reasoning_effort", None)
        path.write_text(json.dumps(record), encoding="utf-8")

        run = store.get_run(run_id)

    self.assertEqual(run["ai_model"], "gpt-5.5")
    self.assertEqual(run["ai_reasoning_effort"], "xhigh")
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_config tests.test_store -v
```

Expected: FAIL with `AttributeError: 'RepoConfig' object has no attribute 'default_ai_model'` and missing `ai_model`/`ai_reasoning_effort` fields.

- [ ] **Step 4: Implement catalog and config/store defaults**

Create `agent_desk/ai_settings.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping


DEFAULT_AI_MODEL = "gpt-5.5"
DEFAULT_AI_REASONING_EFFORT = "xhigh"


@dataclass(frozen=True)
class AIModelOption:
    id: str
    label: str
    default_reasoning_effort: str
    reasoning_efforts: tuple[str, ...]


AI_MODEL_CATALOG = (
    AIModelOption("gpt-5.6-sol", "GPT-5.6 Sol", "low", ("low", "medium", "high", "xhigh", "max", "ultra")),
    AIModelOption("gpt-5.6-terra", "GPT-5.6 Terra", "medium", ("low", "medium", "high", "xhigh", "max", "ultra")),
    AIModelOption("gpt-5.6-luna", "GPT-5.6 Luna", "medium", ("low", "medium", "high", "xhigh", "max")),
    AIModelOption("gpt-5.5", "GPT-5.5", "medium", ("low", "medium", "high", "xhigh")),
    AIModelOption("gpt-5.4", "GPT-5.4", "medium", ("low", "medium", "high", "xhigh")),
    AIModelOption("gpt-5.4-mini", "GPT-5.4 Mini", "medium", ("low", "medium", "high", "xhigh")),
    AIModelOption("gpt-5.3-codex-spark", "GPT-5.3 Codex Spark", "high", ("low", "medium", "high", "xhigh")),
)
AI_MODEL_BY_ID = {item.id: item for item in AI_MODEL_CATALOG}


def normalize_ai_settings(
    model: str | None,
    reasoning_effort: str | None,
) -> tuple[str, str]:
    model_value = str(model or DEFAULT_AI_MODEL).strip() or DEFAULT_AI_MODEL
    effort_value = str(reasoning_effort or DEFAULT_AI_REASONING_EFFORT).strip()
    option = AI_MODEL_BY_ID.get(model_value)
    if option and effort_value not in option.reasoning_efforts:
        effort_value = option.default_reasoning_effort
    if not effort_value:
        effort_value = option.default_reasoning_effort if option else DEFAULT_AI_REASONING_EFFORT
    return model_value, effort_value


def ai_model_catalog_payload() -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "label": item.label,
            "default_reasoning_effort": item.default_reasoning_effort,
            "reasoning_efforts": list(item.reasoning_efforts),
        }
        for item in AI_MODEL_CATALOG
    ]


def codex_ai_args(run: Mapping[str, Any]) -> list[str]:
    model = str(run.get("ai_model") or "").strip()
    effort = str(run.get("ai_reasoning_effort") or "").strip()
    args: list[str] = []
    if model:
        args.extend(["-m", model])
    if effort:
        args.extend(["-c", f"model_reasoning_effort={json.dumps(effort)}"])
    return args
```

Modify `RepoConfig` in `agent_desk/config.py`:

```python
from .ai_settings import DEFAULT_AI_MODEL, DEFAULT_AI_REASONING_EFFORT

@dataclass(frozen=True)
class RepoConfig:
    ...
    closeout_sandbox: str = "workspace-write"
    default_ai_model: str = DEFAULT_AI_MODEL
    default_ai_reasoning_effort: str = DEFAULT_AI_REASONING_EFFORT
```

Load these fields in `load_config()`:

```python
default_ai_model=repo_raw.get("default_ai_model", DEFAULT_AI_MODEL),
default_ai_reasoning_effort=repo_raw.get(
    "default_ai_reasoning_effort",
    DEFAULT_AI_REASONING_EFFORT,
),
```

Copy them in `add_project_to_config()`:

```python
default_ai_model=template.default_ai_model,
default_ai_reasoning_effort=template.default_ai_reasoning_effort,
```

Append them in `_repo_config_toml()`:

```python
f"default_ai_model = {_toml_string(repo.default_ai_model)}",
f"default_ai_reasoning_effort = {_toml_string(repo.default_ai_reasoning_effort)}",
```

Add them to `example_config()` after `closeout_sandbox`:

```toml
default_ai_model = "gpt-5.5"
default_ai_reasoning_effort = "xhigh"
```

Modify `Store._normalize_record()` and `_new_record()` in `agent_desk/store.py`:

```python
from .ai_settings import DEFAULT_AI_MODEL, DEFAULT_AI_REASONING_EFFORT

normalized.setdefault("ai_model", DEFAULT_AI_MODEL)
normalized.setdefault("ai_reasoning_effort", DEFAULT_AI_REASONING_EFFORT)
```

```python
"ai_model": DEFAULT_AI_MODEL,
"ai_reasoning_effort": DEFAULT_AI_REASONING_EFFORT,
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
python3 -m unittest tests.test_config tests.test_store -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_desk/ai_settings.py agent_desk/config.py agent_desk/store.py tests/test_config.py tests/test_store.py
git commit -m "feat: add ai model settings defaults"
```

---

### Task 2: Scheduler Workspace Defaults And Run Inheritance

**Files:**
- Modify: `agent_desk/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `normalize_ai_settings(model, reasoning_effort) -> tuple[str, str]`
- Produces: `SchedulerSettings.default_ai_model: str`
- Produces: `SchedulerSettings.default_ai_reasoning_effort: str`
- Produces: `Scheduler.update_run_ai_settings(run_id: int, ai_model: str, ai_reasoning_effort: str) -> RunNextResult`

- [ ] **Step 1: Write failing scheduler settings tests**

Add tests near the existing workspace settings tests in `tests/test_scheduler.py`:

```python
def test_workspace_settings_include_ai_defaults(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = Store(root / "desk.sqlite")
        scheduler = NoopScheduler(
            AgentDeskConfig(
                data_dir=root / "data",
                repos=[
                    RepoConfig(
                        name="octo/one",
                        local_path=root / "one",
                        default_ai_model="gpt-5.6-terra",
                        default_ai_reasoning_effort="high",
                    )
                ],
            ),
            store,
            github=FakeGitHub(),
        )

        settings = scheduler.settings_payload(root / "one")

    self.assertEqual(settings["default_ai_model"], "gpt-5.6-terra")
    self.assertEqual(settings["default_ai_reasoning_effort"], "high")

def test_update_settings_changes_ai_runtime_defaults(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = Store(root / "desk.sqlite")
        scheduler = NoopScheduler(
            AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            ),
            store,
            github=FakeGitHub(),
        )

        updated = scheduler.update_settings(
            workspace_path=root / "one",
            default_ai_model="gpt-5.6-luna",
            default_ai_reasoning_effort="max",
        )

    self.assertEqual(updated["default_ai_model"], "gpt-5.6-luna")
    self.assertEqual(updated["default_ai_reasoning_effort"], "max")
```

- [ ] **Step 2: Write failing run inheritance and update tests**

Add these tests in `tests/test_scheduler.py`:

```python
def test_mark_issue_ready_uses_workspace_ai_settings(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = Store(root / "desk.sqlite")
        github = RecordingGitHub()
        scheduler = NoopScheduler(
            AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            ),
            store,
            github=github,
        )
        scheduler.update_settings(
            workspace_path=root / "one",
            default_ai_model="gpt-5.6-terra",
            default_ai_reasoning_effort="high",
        )

        result = scheduler.mark_issue_ready("octo/one", 7)
        run = store.get_run(result.run_id)

    self.assertTrue(result.started)
    self.assertEqual(run["ai_model"], "gpt-5.6-terra")
    self.assertEqual(run["ai_reasoning_effort"], "high")

def test_update_run_ai_settings_rejects_running_run(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = Store(root / "desk.sqlite")
        scheduler = NoopScheduler(
            AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            ),
            store,
            github=FakeGitHub(),
        )
        run_id = store.create_run(
            repo_name="octo/one",
            issue_number=8,
            issue_title="Running",
            issue_url="https://example.test/8",
            branch_name="agent/issue-8-running",
        )
        store.update_run(run_id, state="running", stage="running codex")

        result = scheduler.update_run_ai_settings(run_id, "gpt-5.6-sol", "max")

    self.assertFalse(result.started)
    self.assertEqual(store.get_run(run_id)["ai_model"], "gpt-5.5")

def test_update_run_ai_settings_validates_known_model_effort(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = Store(root / "desk.sqlite")
        scheduler = NoopScheduler(
            AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/one", local_path=root / "one")],
            ),
            store,
            github=FakeGitHub(),
        )
        run_id = store.create_run(
            repo_name="octo/one",
            issue_number=9,
            issue_title="Ready",
            issue_url="https://example.test/9",
            branch_name="agent/issue-9-ready",
        )
        store.update_run(run_id, state="ready", stage="waiting for human run")

        result = scheduler.update_run_ai_settings(run_id, "gpt-5.5", "max")
        run = store.get_run(run_id)

    self.assertTrue(result.started)
    self.assertEqual(run["ai_model"], "gpt-5.5")
    self.assertEqual(run["ai_reasoning_effort"], "medium")
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_scheduler -v
```

Expected: FAIL because `SchedulerSettings` lacks the new fields and `Scheduler.update_run_ai_settings` is undefined.

- [ ] **Step 4: Implement scheduler settings and inheritance**

Modify `SchedulerSettings` in `agent_desk/scheduler.py`:

```python
from .ai_settings import DEFAULT_AI_MODEL, DEFAULT_AI_REASONING_EFFORT, normalize_ai_settings

@dataclass
class SchedulerSettings:
    ...
    default_ai_model: str = DEFAULT_AI_MODEL
    default_ai_reasoning_effort: str = DEFAULT_AI_REASONING_EFFORT
```

In `from_repo()`:

```python
model, effort = normalize_ai_settings(repo.default_ai_model, repo.default_ai_reasoning_effort)
return cls(
    ...
    default_ai_model=model,
    default_ai_reasoning_effort=effort,
)
```

In `as_payload()`:

```python
"default_ai_model": self.default_ai_model,
"default_ai_reasoning_effort": self.default_ai_reasoning_effort,
```

Extend `update_settings()` signature and implementation:

```python
default_ai_model: str | None = None,
default_ai_reasoning_effort: str | None = None,
```

```python
if default_ai_model is not None or default_ai_reasoning_effort is not None:
    model, effort = normalize_ai_settings(
        default_ai_model if default_ai_model is not None else settings.default_ai_model,
        default_ai_reasoning_effort
        if default_ai_reasoning_effort is not None
        else settings.default_ai_reasoning_effort,
    )
    settings.default_ai_model = model
    settings.default_ai_reasoning_effort = effort
```

Add a helper:

```python
def _ai_settings_fields_for_repo(self, repo: RepoConfig) -> dict[str, str]:
    settings = self._settings_for_repo(repo)
    model, effort = normalize_ai_settings(
        settings.default_ai_model,
        settings.default_ai_reasoning_effort,
    )
    return {"ai_model": model, "ai_reasoning_effort": effort}
```

Use that helper in `_promote_to_ready()`, `_promote_to_dependency_waiting()`, `_create_ready_run()`, and `_create_dependency_waiting_run()` by merging it before caller-provided `fields`:

```python
update_fields = {**self._ai_settings_fields_for_repo(repo), **fields}
```

In `_promote_to_ready()` and `_promote_to_dependency_waiting()`, pass
`**update_fields` to the existing `store.update_run()` call.

In `_create_ready_run()` and `_create_dependency_waiting_run()`, keep the
existing `store.create_run()` call unchanged, then pass `**update_fields` to the
existing `store.update_run()` call that moves the new record into its target
state.

Add `update_run_ai_settings()`:

```python
def update_run_ai_settings(
    self,
    run_id: int,
    ai_model: str,
    ai_reasoning_effort: str,
) -> RunNextResult:
    with self._lock:
        run = self.store.get_run(run_id)
        if run["state"] == "running":
            return RunNextResult(False, f"Run #{run_id} is running and cannot change AI settings", run_id)
        model, effort = normalize_ai_settings(ai_model, ai_reasoning_effort)
        self.store.update_run(run_id, ai_model=model, ai_reasoning_effort=effort)
        self.store.add_event(
            run_id,
            "info",
            "ai-settings",
            f"AI settings updated to {model} / {effort}",
            {"ai_model": model, "ai_reasoning_effort": effort},
        )
        return RunNextResult(True, "AI settings updated", run_id)
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
python3 -m unittest tests.test_scheduler -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_desk/scheduler.py tests/test_scheduler.py
git commit -m "feat: inherit ai settings for runs"
```

---

### Task 3: Pass AI Settings To Codex Commands

**Files:**
- Modify: `agent_desk/worker.py`
- Modify: `agent_desk/continuation.py`
- Modify: `agent_desk/ai_review.py`
- Test: `tests/test_worker.py`
- Test: `tests/test_continuation.py`
- Test: `tests/test_ai_review.py`

**Interfaces:**
- Consumes: `codex_ai_args(run: Mapping[str, Any]) -> list[str]`
- Produces: Codex argv includes `-m <model>` and `-c model_reasoning_effort="<effort>"` before `exec`

- [ ] **Step 1: Write failing worker test**

Add this test to `tests/test_worker.py` near `test_worker_invokes_codex_non_interactively_and_writes_transcript`:

```python
def test_worker_passes_run_ai_settings_to_codex_exec(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo_path = root / "repo"
        repo_path.mkdir()
        config = AgentDeskConfig(data_dir=root / "data")
        repo = RepoConfig(name="octo/example", local_path=repo_path, push_pr=False)
        store = Store(root / "desk.sqlite")
        run_id = store.create_run(
            repo_name=repo.name,
            issue_number=30,
            issue_title="Use model",
            issue_url="https://github.com/octo/example/issues/30",
            branch_name="agent/issue-30-use-model",
        )
        store.update_run(run_id, ai_model="gpt-5.6-terra", ai_reasoning_effort="high")
        runner = FakeCommandRunner(
            [
                CommandResult(["git", "fetch"], 0, "", ""),
                CommandResult(["git", "worktree"], 0, "", ""),
                CommandResult(["codex", "exec"], 0, '{"status":"done","summary":"ok","tests":[],"questions":[]}', ""),
            ]
        )

        Worker(config, store, runner).run_issue(
            run_id=run_id,
            repo=repo,
            issue_number=30,
            issue_title="Use model",
            issue_body="Body",
            issue_url="https://github.com/octo/example/issues/30",
            branch_name="agent/issue-30-use-model",
        )

        argv = runner.calls[2].argv

    self.assertEqual(argv[0], "codex")
    self.assertIn("-m", argv)
    self.assertEqual(argv[argv.index("-m") + 1], "gpt-5.6-terra")
    self.assertIn("-c", argv)
    self.assertIn('model_reasoning_effort="high"', argv)
    self.assertLess(argv.index("-m"), argv.index("exec"))
```

- [ ] **Step 2: Write failing continuation and AI review tests**

Add to `tests/test_continuation.py`:

```python
def test_request_changes_passes_run_ai_settings_to_codex_resume(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = Store(root / "desk.sqlite")
        worktree = root / "worktree"
        worktree.mkdir()
        config = AgentDeskConfig(
            data_dir=root / "data",
            repos=[RepoConfig(name="octo/example", local_path=root / "repo")],
        )
        run_id = store.create_run(
            repo_name="octo/example",
            issue_number=5,
            issue_title="PR",
            issue_url="https://github.com/octo/example/issues/5",
            branch_name="agent/issue-5-pr",
        )
        store.update_run(
            run_id,
            state="pr_open",
            stage="pull request opened",
            worktree_path=str(worktree),
            codex_thread_id="thread-1",
            ai_model="gpt-5.6-sol",
            ai_reasoning_effort="max",
        )
        runner = FakeCommandRunner(
            [
                CommandResult(
                    ["codex", "exec", "resume"],
                    0,
                    '{"status":"done","summary":"ok","tests":[],"questions":[],"pr_url":"https://github.com/octo/example/pull/1"}',
                    "",
                )
            ]
        )

        ContinuationRunner(config, store, runner=runner).request_changes(run_id, "Please revise")
        argv = runner.calls[0].argv

    self.assertIn("-m", argv)
    self.assertEqual(argv[argv.index("-m") + 1], "gpt-5.6-sol")
    self.assertIn('model_reasoning_effort="max"', argv)
    self.assertLess(argv.index("-m"), argv.index("exec"))
```

Add to `tests/test_ai_review.py`:

```python
def test_ai_review_passes_run_ai_settings_to_codex_exec(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        worktree = root / "worktree"
        worktree.mkdir()
        store = Store(root / "desk.sqlite")
        config = AgentDeskConfig(data_dir=root / "data")
        run_id = store.create_run(
            repo_name="octo/example",
            issue_number=5,
            issue_title="Review",
            issue_url="https://github.com/octo/example/issues/5",
            branch_name="agent/issue-5-review",
        )
        store.update_run(
            run_id,
            state="pr_open",
            stage="pull request opened",
            pr_url="https://github.com/octo/example/pull/1",
            worktree_path=str(worktree),
            ai_model="gpt-5.6-luna",
            ai_reasoning_effort="max",
        )
        runner = FakeCommandRunner(
            [
                CommandResult(
                    ["codex", "exec"],
                    0,
                    '{"status":"approved","summary":"ok","findings":[],"feedback":"","risks":[],"pr_url":"https://github.com/octo/example/pull/1"}',
                    "",
                )
            ]
        )

        AIReviewRunner(config, store, runner=runner).review(
            run_id,
            PullRequestChecksStatus(state="success", summary="passed", head_sha="abc"),
        )
        argv = runner.calls[0].argv

    self.assertIn("-m", argv)
    self.assertEqual(argv[argv.index("-m") + 1], "gpt-5.6-luna")
    self.assertIn('model_reasoning_effort="max"', argv)
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_worker tests.test_continuation tests.test_ai_review -v
```

Expected: FAIL because Codex argv lacks `-m` and `model_reasoning_effort`.

- [ ] **Step 4: Implement Codex argv integration**

Modify imports and argv construction:

In `agent_desk/worker.py`:

```python
from .ai_settings import codex_ai_args
```

```python
argv = [
    "codex",
    *codex_ai_args(self.store.get_run(run_id)),
    "--ask-for-approval",
    "never",
    "exec",
    "--json",
    "--sandbox",
    "workspace-write",
    "-C",
    str(worktree_path),
]
```

In `agent_desk/continuation.py`:

```python
from .ai_settings import codex_ai_args
```

```python
argv = [
    "codex",
    *codex_ai_args(run),
    "--ask-for-approval",
    "never",
    "--sandbox",
    sandbox,
    "-C",
    str(worktree_path),
    "exec",
    "resume",
    "--json",
]
```

In `agent_desk/ai_review.py`:

```python
from .ai_settings import codex_ai_args
```

```python
argv = [
    "codex",
    *codex_ai_args(run),
    "--ask-for-approval",
    "never",
    "--sandbox",
    "read-only",
    "-C",
    str(worktree_path),
    "exec",
    "--json",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
python3 -m unittest tests.test_worker tests.test_continuation tests.test_ai_review -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent_desk/worker.py agent_desk/continuation.py agent_desk/ai_review.py tests/test_worker.py tests/test_continuation.py tests/test_ai_review.py
git commit -m "feat: pass ai settings to codex"
```

---

### Task 4: Dashboard State And API Routes

**Files:**
- Modify: `agent_desk/dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `ai_model_catalog_payload() -> list[dict[str, Any]]`
- Consumes: `Scheduler.update_settings(..., default_ai_model, default_ai_reasoning_effort) -> dict[str, bool | int | str]`
- Consumes: `Scheduler.update_run_ai_settings(run_id, ai_model, ai_reasoning_effort) -> RunNextResult`
- Produces: `/api/state` field `ai_models`
- Produces: `POST /api/run/<id>/ai-settings`

- [ ] **Step 1: Write failing state payload test**

Update `test_state_payload_includes_workspace_scheduler_settings` expected settings:

```python
"default_ai_model": "gpt-5.5",
"default_ai_reasoning_effort": "xhigh",
```

Add:

```python
self.assertIn("ai_models", payload)
self.assertIn(
    {
        "id": "gpt-5.5",
        "label": "GPT-5.5",
        "default_reasoning_effort": "medium",
        "reasoning_efforts": ["low", "medium", "high", "xhigh"],
    },
    payload["ai_models"],
)
```

- [ ] **Step 2: Write failing AI settings route tests**

Add this scheduler test double in `tests/test_dashboard.py`:

```python
class _AISettingsScheduler:
    paused = False

    def __init__(self):
        self.calls = []

    def update_run_ai_settings(self, run_id, ai_model, ai_reasoning_effort):
        self.calls.append((run_id, ai_model, ai_reasoning_effort))
        return RunNextResult(True, "AI settings updated", run_id)
```

Add a route test:

```python
def test_run_ai_settings_route_dispatches_scheduler(self):
    host = "127.0.0.1"
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "desk.sqlite")
        scheduler = _AISettingsScheduler()
        bound: dict[str, int] = {}
        ready = threading.Event()
        thread = threading.Thread(
            target=serve_dashboard,
            kwargs={
                "host": host,
                "port": 0,
                "store": store,
                "scheduler": scheduler,
                "on_serving": lambda _h, port: (bound.update(port=port), ready.set()),
            },
            daemon=True,
        )
        thread.start()
        self.assertTrue(ready.wait(timeout=5), "dashboard never reported a bound port")

        request = urllib.request.Request(
            f"http://{host}:{bound['port']}/api/run/7/ai-settings",
            data=json.dumps(
                {"ai_model": "gpt-5.6-terra", "ai_reasoning_effort": "high"}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())

    self.assertEqual(response.status, 200)
    self.assertTrue(payload["started"])
    self.assertEqual(scheduler.calls, [(7, "gpt-5.6-terra", "high")])
```

- [ ] **Step 3: Write failing settings route payload test**

Use an existing settings route test or add one that posts `default_ai_model` and
`default_ai_reasoning_effort` to a real `Scheduler`, then checks
`scheduler.settings_payload()`:

```python
def test_settings_route_updates_ai_runtime_defaults(self):
    host = "127.0.0.1"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo_path = root / "repo"
        repo_path.mkdir()
        store = Store(root / "desk.sqlite")
        scheduler = Scheduler(
            AgentDeskConfig(
                data_dir=root / "data",
                repos=[RepoConfig(name="octo/example", local_path=repo_path)],
            ),
            store,
        )
        bound: dict[str, int] = {}
        ready = threading.Event()
        thread = threading.Thread(
            target=serve_dashboard,
            kwargs={
                "host": host,
                "port": 0,
                "store": store,
                "scheduler": scheduler,
                "on_serving": lambda _h, port: (bound.update(port=port), ready.set()),
            },
            daemon=True,
        )
        thread.start()
        self.assertTrue(ready.wait(timeout=5), "dashboard never reported a bound port")

        request = urllib.request.Request(
            f"http://{host}:{bound['port']}/api/settings",
            data=json.dumps(
                {
                    "workspace_path": str(repo_path),
                    "default_ai_model": "gpt-5.6-sol",
                    "default_ai_reasoning_effort": "max",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())

    self.assertEqual(payload["settings"]["default_ai_model"], "gpt-5.6-sol")
    self.assertEqual(payload["settings"]["default_ai_reasoning_effort"], "max")
```

- [ ] **Step 4: Run tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_dashboard -v
```

Expected: FAIL because `ai_models` is absent and the route is missing.

- [ ] **Step 5: Implement dashboard payload and routes**

Modify imports in `agent_desk/dashboard.py`:

```python
from .ai_settings import ai_model_catalog_payload
```

In `build_state_payload()`:

```python
payload["ai_models"] = ai_model_catalog_payload()
```

In the `/api/settings` route, pass the two optional fields:

```python
default_ai_model=payload.get("default_ai_model")
if "default_ai_model" in payload
else None,
default_ai_reasoning_effort=payload.get("default_ai_reasoning_effort")
if "default_ai_reasoning_effort" in payload
else None,
```

Add a route before the approve-finish route:

```python
if path.startswith("/api/run/") and path.endswith("/ai-settings"):
    run_id = int(path.split("/")[3])
    payload = self._read_json()
    result = scheduler.update_run_ai_settings(
        run_id,
        str(payload.get("ai_model") or ""),
        str(payload.get("ai_reasoning_effort") or ""),
    )
    self._send_json(result.__dict__)
    return
```

- [ ] **Step 6: Run tests to verify they pass**

Run:

```bash
python3 -m unittest tests.test_dashboard -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agent_desk/dashboard.py tests/test_dashboard.py
git commit -m "feat: expose ai settings dashboard api"
```

---

### Task 5: Static Dashboard Controls

**Files:**
- Modify: `agent_desk/static/dashboard.html`
- Modify: `agent_desk/static/dashboard.js`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `/api/state` `ai_models`
- Consumes: project `settings.default_ai_model`
- Consumes: project `settings.default_ai_reasoning_effort`
- Consumes: run `ai_model`
- Consumes: run `ai_reasoning_effort`
- Produces: Workspace Settings controls `default-ai-model` and `default-ai-reasoning-effort`
- Produces: task card controls that call `/api/run/<id>/ai-settings`

- [ ] **Step 1: Write failing static dashboard tests**

Add to `tests/test_dashboard.py`:

```python
def test_dashboard_html_renders_workspace_ai_settings_controls(self):
    self.assertIn('id="default-ai-model"', HTML)
    self.assertIn('id="default-ai-reasoning-effort"', HTML)
    self.assertIn("default_ai_model", HTML)
    self.assertIn("default_ai_reasoning_effort", HTML)

def test_dashboard_html_renders_task_ai_settings_controls(self):
    self.assertIn("aiSettingsHtml(run)", HTML)
    self.assertIn("/api/run/${runId}/ai-settings", HTML)
    self.assertIn("saveRunAiSettings(", HTML)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_dashboard.DashboardTests.test_dashboard_html_renders_workspace_ai_settings_controls tests.test_dashboard.DashboardTests.test_dashboard_html_renders_task_ai_settings_controls -v
```

Expected: FAIL because the static HTML/JS strings are absent.

- [ ] **Step 3: Add Workspace Settings controls**

In `agent_desk/static/dashboard.html`, add CSS:

```css
.setting-row select,
.ai-settings select,
.ai-settings input {
  min-width: 112px;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 5px 6px;
  font: inherit;
  background: #fff;
}
.ai-settings {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  margin-top: 8px;
  padding: 6px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #f8fafc;
  font-size: 12px;
}
.ai-settings button {
  padding: 4px 7px;
  font-size: 12px;
}
```

Add controls inside `.settings-panel` after the timeout row:

```html
<label class="setting-row" for="default-ai-model">
  <span>Default model</span>
  <select id="default-ai-model" onchange="onWorkspaceModelChange(); markSettingsDirty()"></select>
</label>
<label class="setting-row" for="default-ai-reasoning-effort">
  <span>Default reasoning</span>
  <select id="default-ai-reasoning-effort" onchange="markSettingsDirty()"></select>
</label>
```

- [ ] **Step 4: Add JavaScript helpers for linked selects**

In `agent_desk/static/dashboard.js`, add helpers near `esc()`:

```javascript
function aiCatalog(state) {
  return state.ai_models || [];
}
function aiOption(state, model) {
  return aiCatalog(state).find(item => item.id === model);
}
function modelOptionsHtml(state, selected) {
  const options = aiCatalog(state).map(item =>
    `<option value="${esc(item.id)}" ${item.id === selected ? 'selected' : ''}>${esc(item.label || item.id)}</option>`
  ).join('');
  const customSelected = selected && !aiOption(state, selected) ? 'selected' : '';
  return `${options}<option value="${esc(selected && customSelected ? selected : 'custom')}" ${customSelected}>${customSelected ? esc(selected) : 'Custom...'}</option>`;
}
function reasoningOptionsHtml(state, model, selected) {
  const option = aiOption(state, model);
  const efforts = option ? option.reasoning_efforts || [] : ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
  const current = selected || (option ? option.default_reasoning_effort : 'xhigh');
  return efforts.map(effort =>
    `<option value="${esc(effort)}" ${effort === current ? 'selected' : ''}>${esc(effort)}</option>`
  ).join('');
}
```

Update `settingsControls()` to include:

```javascript
document.getElementById('default-ai-model'),
document.getElementById('default-ai-reasoning-effort'),
```

Update the default `settings` object in `renderSettings()`:

```javascript
default_ai_model: 'gpt-5.5',
default_ai_reasoning_effort: 'xhigh'
```

In `renderSettings(state)`, after timeout fields:

```javascript
document.getElementById('default-ai-model').innerHTML = modelOptionsHtml(state, settings.default_ai_model || 'gpt-5.5');
document.getElementById('default-ai-model').value = settings.default_ai_model || 'gpt-5.5';
document.getElementById('default-ai-reasoning-effort').innerHTML = reasoningOptionsHtml(
  state,
  settings.default_ai_model || 'gpt-5.5',
  settings.default_ai_reasoning_effort || 'xhigh'
);
document.getElementById('default-ai-reasoning-effort').value = settings.default_ai_reasoning_effort || 'xhigh';
```

Add:

```javascript
function onWorkspaceModelChange() {
  const state = latestState || { ai_models: [] };
  const model = document.getElementById('default-ai-model').value;
  const option = aiOption(state, model);
  const effort = option ? option.default_reasoning_effort : 'xhigh';
  const select = document.getElementById('default-ai-reasoning-effort');
  select.innerHTML = reasoningOptionsHtml(state, model, effort);
  select.value = effort;
}
```

Add settings payload fields in `saveSettings()`:

```javascript
default_ai_model: document.getElementById('default-ai-model').value,
default_ai_reasoning_effort: document.getElementById('default-ai-reasoning-effort').value,
```

- [ ] **Step 5: Add task AI settings controls**

Add functions near `runActions()`:

```javascript
function canEditRunAiSettings(run) {
  return run.state !== 'running';
}
function aiSettingsHtml(run) {
  const state = latestState || { ai_models: [] };
  const disabled = canEditRunAiSettings(run) ? '' : 'disabled';
  const model = run.ai_model || 'gpt-5.5';
  const effort = run.ai_reasoning_effort || 'xhigh';
  return `<div class="ai-settings">
    <span>AI</span>
    <select id="run-ai-model-${run.id}" ${disabled} onchange="onRunModelChange(${run.id})">
      ${modelOptionsHtml(state, model)}
    </select>
    <select id="run-ai-reasoning-${run.id}" ${disabled}>
      ${reasoningOptionsHtml(state, model, effort)}
    </select>
    ${canEditRunAiSettings(run) ? `<button onclick="saveRunAiSettings(${run.id})">Save</button>` : '<span class="muted">running</span>'}
  </div>`;
}
function onRunModelChange(runId) {
  const state = latestState || { ai_models: [] };
  const modelSelect = document.getElementById(`run-ai-model-${runId}`);
  const reasoningSelect = document.getElementById(`run-ai-reasoning-${runId}`);
  const option = aiOption(state, modelSelect.value);
  const effort = option ? option.default_reasoning_effort : (reasoningSelect.value || 'xhigh');
  reasoningSelect.innerHTML = reasoningOptionsHtml(state, modelSelect.value, effort);
  reasoningSelect.value = effort;
}
async function saveRunAiSettings(runId) {
  const model = document.getElementById(`run-ai-model-${runId}`).value;
  const effort = document.getElementById(`run-ai-reasoning-${runId}`).value;
  return postJson(`/api/run/${runId}/ai-settings`, {
    ai_model: model,
    ai_reasoning_effort: effort
  });
}
```

Add `${aiSettingsHtml(run)}` in `runHtml(run)` after the Stage row:

```javascript
    <div>Stage: ${esc(run.stage)}</div>
    ${aiSettingsHtml(run)}
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
python3 -m unittest tests.test_dashboard.DashboardTests.test_dashboard_html_renders_workspace_ai_settings_controls tests.test_dashboard.DashboardTests.test_dashboard_html_renders_task_ai_settings_controls -v
```

Expected: PASS.

- [ ] **Step 7: Run the full dashboard tests**

Run:

```bash
python3 -m unittest tests.test_dashboard -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add agent_desk/static/dashboard.html agent_desk/static/dashboard.js tests/test_dashboard.py
git commit -m "feat: add dashboard ai settings controls"
```

---

### Task 6: Full Verification And Documentation Touch-Ups

**Files:**
- Modify: `README.md` if the existing dashboard settings section mentions all settings.
- Modify: `CLAUDE.md` if command/config guidance needs the two new config keys.

**Interfaces:**
- Consumes: all prior task interfaces.
- Produces: verified full test suite and concise docs for the new settings.

- [ ] **Step 1: Inspect docs for settings references**

Run:

```bash
rg -n "Workspace Settings|auto_start_ready|max_concurrent_runs|closeout_sandbox|worker_timeout|repos.toml|settings" README.md CLAUDE.md config/repos.example.toml
```

Expected: output identifies any user-facing config examples that need the new keys.

- [ ] **Step 2: Write doc updates if needed**

If `README.md` lists workspace settings or config keys, add this paragraph near that section:

```markdown
Workspace Settings also controls the default AI model and reasoning effort for
new tasks. New repositories default to `gpt-5.5` with `xhigh` reasoning. You can
override those defaults in `config/repos.toml` with `default_ai_model` and
`default_ai_reasoning_effort`, and you can override an individual task from its
dashboard card before the next Codex invocation starts.
```

If `CLAUDE.md` still describes keeping `config/repos.example.toml` and
`example_config()` in sync, verify the previous tasks updated both. No prose
change is required when the guidance already covers this.

- [ ] **Step 3: Run full verification**

Run:

```bash
make test
```

Expected: `python3 -m unittest discover -s tests -v` completes with `OK`.

- [ ] **Step 4: Check working tree**

Run:

```bash
git status --short
```

Expected: only intentional files are modified.

- [ ] **Step 5: Commit docs if changed**

If `README.md` or `CLAUDE.md` changed:

```bash
git add README.md CLAUDE.md
git commit -m "docs: document ai model settings"
```

If no docs changed, record in the final handoff that no doc update was needed because the config example and dashboard UI are self-describing.

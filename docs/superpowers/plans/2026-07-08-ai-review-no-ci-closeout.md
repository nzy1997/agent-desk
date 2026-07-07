# AI Review No-CI Closeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach Agent Desk to distinguish `no_ci` from `unknown`, optionally run an independent Codex CLI AI review before automatic closeout, and send failed review feedback back to the original worker thread.

**Architecture:** Add `no_ci` as a first-class PR check state, add `enable_ai_review` as a workspace setting, and introduce a focused `AIReviewRunner` that runs a non-resume Codex review prompt. The scheduler treats `success` and `no_ci` as closeout-ready gates; when AI review is enabled it dispatches the review worker first, then either auto-finishes or request-changes through the existing continuation path.

**Tech Stack:** Python 3.11 standard library only, `unittest`, local `gh`, `git`, and `codex` CLI commands, existing filesystem JSON store.

## Global Constraints

- Do not add Python package dependencies.
- Do not integrate an external review service or GitHub App.
- Do not replace manual `Request changes` or `Approve & finish` controls.
- Do not make AI review mandatory for every workspace.
- Do not teach the reviewer worker to push, merge, or edit files.
- The prompt sent to Codex CLI must be in English.
- `no_ci` is a concrete repository condition, not an error.
- `unknown` must not trigger automatic closeout or AI review.
- Existing records default new AI review fields to empty strings.
- Use stdlib `unittest`; do not introduce pytest as a project dependency.

---

## File Structure

- Modify `agent_desk/github_client.py`
  - Owns GitHub PR check normalization.
  - Add `no_ci` detection and keep ambiguous failures as `unknown`.
- Modify `agent_desk/continuation.py`
  - Keep existing continuation API stable.
  - Make automatic closeout prompt describe the recorded PR gate instead of claiming CI always passed.
- Modify `agent_desk/config.py`
  - Persist `enable_ai_review` in repo config, generated config, and appended repo blocks.
- Modify `agent_desk/scheduler.py`
  - Add runtime setting, AI review factory injection, scheduler gate logic, detached job kind, and post-review follow-up.
- Create `agent_desk/ai_review.py`
  - Render the English reviewer prompt.
  - Run independent `codex exec`.
  - Parse and store review results.
- Create `schemas/ai-review-result.schema.json`
  - Optional output schema for Codex CLI review results.
- Modify `agent_desk/store.py`
  - Add default AI review fields to new records.
- Modify `agent_desk/dashboard.py`
  - Include AI review log files in run log ordering.
  - Settings API automatically passes through new scheduler setting once added.
- Modify `agent_desk/static/dashboard.html`
  - Add Workspace Settings checkbox.
- Modify `agent_desk/static/dashboard.js`
  - Save/render the new setting and show `No CI` plus AI review status in PR cards.
- Modify `config/repos.example.toml`, `README.md`, and `CLAUDE.md`
  - Document the new setting, no-CI behavior, and AI review lifecycle.
- Modify tests under `tests/`
  - Add targeted `unittest` coverage for each behavior.

---

### Task 1: Add `no_ci` PR Check State And Closeout Prompt Readiness

**Files:**
- Modify: `agent_desk/github_client.py`
- Modify: `agent_desk/continuation.py`
- Test: `tests/test_github_client.py`
- Test: `tests/test_continuation.py`

**Interfaces:**
- Consumes: existing `PullRequestChecksStatus(state: str, summary: str, head_sha: str, checks: list[dict[str, Any]])`
- Produces: `PullRequestChecksStatus.state == "no_ci"` for explicit no-checks responses
- Produces: `summarize_checks([]) -> ("no_ci", "No checks reported")`
- Produces: automatic closeout prompt that uses `run["pr_ci_status"]` and `run["pr_ci_summary"]`

- [ ] **Step 1: Add failing GitHub client tests for explicit no-CI responses**

Append these tests to `tests/test_github_client.py` inside `GitHubClientTests`:

```python
    def test_pr_checks_status_reports_no_ci_for_empty_checks_json(self):
        with patch("agent_desk.github_client.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(["gh", "pr", "view"], 0, '{"headRefOid":"abc123"}', ""),
                subprocess.CompletedProcess(["gh", "pr", "checks"], 0, "[]", ""),
            ]

            status = GitHubClient().pr_checks_status(
                "octo/example",
                "https://github.com/octo/example/pull/9",
            )

        self.assertEqual(status.state, "no_ci")
        self.assertEqual(status.summary, "No checks reported")
        self.assertEqual(status.head_sha, "abc123")
        self.assertEqual(status.checks, [])

    def test_pr_checks_status_reports_no_ci_for_gh_no_checks_message(self):
        with patch("agent_desk.github_client.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(["gh", "pr", "view"], 0, '{"headRefOid":"def456"}', ""),
                subprocess.CompletedProcess(
                    ["gh", "pr", "checks"],
                    1,
                    "",
                    "no checks reported on the 'agent/issue-44' branch",
                ),
            ]

            status = GitHubClient().pr_checks_status(
                "octo/example",
                "https://github.com/octo/example/pull/10",
            )

        self.assertEqual(status.state, "no_ci")
        self.assertEqual(status.summary, "no checks reported on the 'agent/issue-44' branch")
        self.assertEqual(status.head_sha, "def456")
        self.assertEqual(status.checks, [])

    def test_pr_checks_status_keeps_ambiguous_empty_output_unknown(self):
        with patch("agent_desk.github_client.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(["gh", "pr", "view"], 0, '{"headRefOid":"abc123"}', ""),
                subprocess.CompletedProcess(["gh", "pr", "checks"], 1, "", "GraphQL: timeout"),
            ]

            status = GitHubClient().pr_checks_status(
                "octo/example",
                "https://github.com/octo/example/pull/11",
            )

        self.assertEqual(status.state, "unknown")
        self.assertEqual(status.summary, "GraphQL: timeout")
```

- [ ] **Step 2: Run GitHub client tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_github_client.GitHubClientTests.test_pr_checks_status_reports_no_ci_for_empty_checks_json tests.test_github_client.GitHubClientTests.test_pr_checks_status_reports_no_ci_for_gh_no_checks_message tests.test_github_client.GitHubClientTests.test_pr_checks_status_keeps_ambiguous_empty_output_unknown -v
```

Expected: first two tests fail because `no_ci` is not implemented yet.

- [ ] **Step 3: Implement `no_ci` normalization**

In `agent_desk/github_client.py`, add this helper near `summarize_checks`:

```python
def no_checks_reported(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    return any(
        phrase in normalized
        for phrase in (
            "no checks reported",
            "no checks were reported",
            "no checks found",
            "no check runs found",
        )
    )
```

Update the empty-stdout branch in `GitHubClient.pr_checks_status()`:

```python
        if not completed.stdout.strip():
            detail = completed.stderr.strip() or "No checks reported"
            if no_checks_reported(detail):
                return PullRequestChecksStatus(
                    state="no_ci",
                    summary=detail,
                    head_sha=pr_view.head_sha,
                    checks=[],
                )
            return PullRequestChecksStatus(state="unknown", summary=detail, head_sha=pr_view.head_sha, checks=[])
```

Update `summarize_checks()`:

```python
def summarize_checks(checks: list[dict[str, Any]]) -> tuple[str, str]:
    if not checks:
        return "no_ci", "No checks reported"
    failed = sum(1 for check in checks if check_failed(check))
    pending = sum(1 for check in checks if check_pending(check))
    passed = sum(1 for check in checks if check_passed(check))
    skipped = sum(1 for check in checks if check_skipped(check))
    if failed:
        state = "failure"
    elif pending:
        state = "pending"
    else:
        state = "success"
    parts = []
    if failed:
        parts.append(count_phrase(failed, "failed"))
    if passed:
        parts.append(count_phrase(passed, "passed"))
    if pending:
        parts.append(count_phrase(pending, "pending"))
    if skipped:
        parts.append(count_phrase(skipped, "skipped"))
    return state, ", ".join(parts) or "No checks reported"
```

- [ ] **Step 4: Run GitHub client tests to verify they pass**

Run:

```bash
python3 -m unittest tests.test_github_client -v
```

Expected: all `tests.test_github_client` tests pass.

- [ ] **Step 5: Add failing continuation prompt test for no-CI auto closeout**

Append this test to `tests/test_continuation.py` inside `ContinuationTests`:

```python
    def test_auto_finish_prompt_accepts_recorded_no_ci_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, worktree = self._store_with_pr_run(root)
            store.update_run(
                run_id,
                pr_ci_status="no_ci",
                pr_ci_summary="no checks reported on the 'agent/issue-7' branch",
            )
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec", "resume"],
                        0,
                        '{"status":"done","summary":"merged","tests":[],"questions":[],"risks":[],"pr_url":"https://github.com/octo/example/pull/9","decision_log":[]}',
                        "",
                    )
                ]
            )

            result = ContinuationRunner(config, store, runner).finish_after_ci_success(run_id)
            call = runner.calls[0]

        self.assertTrue(result.ok)
        self.assertEqual(call.cwd, worktree)
        self.assertIn("Agent Desk reports this pull request is eligible for automatic closeout", call.stdin)
        self.assertIn("PR gate status: no_ci", call.stdin)
        self.assertIn("no checks reported on the 'agent/issue-7' branch", call.stdin)
        self.assertIn("If the gate is no_ci, confirm there are no required GitHub checks", call.stdin)
        self.assertNotIn("GitHub CI has passed", call.stdin)
```

- [ ] **Step 6: Run continuation prompt test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_continuation.ContinuationTests.test_auto_finish_prompt_accepts_recorded_no_ci_gate -v
```

Expected: FAIL because the prompt still says `GitHub CI has passed`.

- [ ] **Step 7: Replace auto-finish prompt text with gate-aware prompt**

In `agent_desk/continuation.py`, replace `render_finish_after_ci_success_prompt()` with:

```python
def render_finish_after_ci_success_prompt(run: dict[str, Any], *, ready_label: str, blocked_label: str) -> str:
    gate_status = str(run.get("pr_ci_status") or "unknown")
    gate_summary = str(run.get("pr_ci_summary") or "No PR gate summary recorded")
    return f"""Human review is disabled for this Agent Desk pull request, and Agent Desk reports this pull request is eligible for automatic closeout.

Repository: {run['repo_name']}
Issue: #{run['issue_number']} {run['issue_title']}
Issue URL: {run['issue_url']}
Pull request: {run.get('pr_url') or '(missing PR URL)'}
Branch: {run['branch_name']}
PR gate status: {gate_status}
PR gate summary: {gate_summary}

Continue from the existing Codex thread context and perform the closeout workflow:
1. Inspect the pull request status and checks. Do not merge while checks are pending or failing.
2. If the gate is success, verify there are no pending or failing checks before merging.
3. If the gate is no_ci, confirm there are no required GitHub checks and the pull request is otherwise mergeable before merging.
4. If the pull request is not safe to merge, return status "blocked" with the concrete reason.
5. If the pull request is safe to merge, merge the PR using the repository's normal merge method.
6. Sync the local base branch with origin.
7. Remove the local worktree and prune stale worktree metadata when it is safe.
8. Close or update the completed issue if GitHub did not do it automatically.
9. Do not inspect or modify follow-up issue labels during closeout. Agent Desk manages dependency unlocking locally from its dependency graph.
10. Report exactly which PR, worktree, branch, and completed issue were changed.

Return JSON with status, summary, tests, questions, risks, pr_url, and decision_log.
"""
```

- [ ] **Step 8: Run continuation tests**

Run:

```bash
python3 -m unittest tests.test_continuation -v
```

Expected: all continuation tests pass.

- [ ] **Step 9: Commit Task 1**

Run:

```bash
git add agent_desk/github_client.py agent_desk/continuation.py tests/test_github_client.py tests/test_continuation.py
git commit -m "Add no-CI PR gate handling"
```

Expected: commit succeeds.

---

### Task 2: Add `enable_ai_review` Workspace Setting

**Files:**
- Modify: `agent_desk/config.py`
- Modify: `agent_desk/scheduler.py`
- Modify: `agent_desk/dashboard.py`
- Modify: `agent_desk/static/dashboard.html`
- Modify: `agent_desk/static/dashboard.js`
- Modify: `config/repos.example.toml`
- Test: `tests/test_config.py`
- Test: `tests/test_scheduler.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: existing `RepoConfig`, `SchedulerSettings`, `/api/settings`
- Produces: `RepoConfig.enable_ai_review: bool`
- Produces: `SchedulerSettings.enable_ai_review: bool`
- Produces: settings payload key `enable_ai_review`
- Produces: dashboard checkbox id `enable-ai-review`

- [ ] **Step 1: Add failing config test**

In `tests/test_config.py`, update `test_loads_repo_defaults_from_toml` TOML block to include:

```toml
enable_ai_review = true
```

Then add this assertion after `self.assertFalse(repo.requires_human_review)`:

```python
        self.assertTrue(repo.enable_ai_review)
```

In `test_repo_scheduler_settings_default_to_manual_single_worker_and_review`, add:

```python
        self.assertFalse(repo.enable_ai_review)
```

- [ ] **Step 2: Run config tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_config.ConfigTests.test_loads_repo_defaults_from_toml tests.test_config.ConfigTests.test_repo_scheduler_settings_default_to_manual_single_worker_and_review -v
```

Expected: FAIL because `RepoConfig.enable_ai_review` does not exist.

- [ ] **Step 3: Implement config field and TOML roundtrip**

In `agent_desk/config.py`, add the field to `RepoConfig` after `requires_human_review`:

```python
    enable_ai_review: bool = False
```

In `load_config()`, pass:

```python
                enable_ai_review=bool(repo_raw.get("enable_ai_review", False)),
```

In `add_project_to_config()`, copy the template value:

```python
        enable_ai_review=template.enable_ai_review,
```

In `_repo_config_toml()`, write it after `requires_human_review`:

```python
            f"enable_ai_review = {_toml_bool(repo.enable_ai_review)}",
```

In `example_config()`, add:

```toml
enable_ai_review = false
```

after `requires_human_review = true`.

In `config/repos.example.toml`, add the same line after `requires_human_review = true`.

- [ ] **Step 4: Run config tests**

Run:

```bash
python3 -m unittest tests.test_config -v
```

Expected: all config tests pass.

- [ ] **Step 5: Add failing scheduler settings tests**

In `tests/test_scheduler.py`, update `test_workspace_settings_default_to_manual_single_worker_with_human_review`:

```python
        self.assertFalse(settings["enable_ai_review"])
```

Update `test_workspace_settings_load_repo_auto_start_on_scheduler_start` repo config:

```python
                            enable_ai_review=True,
```

and add:

```python
        self.assertTrue(settings["enable_ai_review"])
```

Add a new test to `SchedulerTests`:

```python
    def test_update_settings_toggles_ai_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            scheduler = NoopScheduler(
                AgentDeskConfig(data_dir=root / "data", repos=[RepoConfig(name="octo/one", local_path=root / "one")]),
                store,
                github=FakeGitHub(),
            )

            updated = scheduler.update_settings(workspace_path=root / "one", enable_ai_review=True)

        self.assertTrue(updated["enable_ai_review"])
        self.assertTrue(scheduler.settings_payload(root / "one")["enable_ai_review"])
```

- [ ] **Step 6: Run scheduler settings tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_scheduler.SchedulerTests.test_workspace_settings_default_to_manual_single_worker_with_human_review tests.test_scheduler.SchedulerTests.test_workspace_settings_load_repo_auto_start_on_scheduler_start tests.test_scheduler.SchedulerTests.test_update_settings_toggles_ai_review -v
```

Expected: FAIL because scheduler settings do not expose `enable_ai_review`.

- [ ] **Step 7: Implement scheduler setting**

In `agent_desk/scheduler.py`, add `enable_ai_review` to `SchedulerSettings`:

```python
    enable_ai_review: bool = False
```

In `SchedulerSettings.from_repo()`:

```python
            enable_ai_review=repo.enable_ai_review,
```

In `SchedulerSettings.as_payload()`:

```python
            "enable_ai_review": self.enable_ai_review,
```

In `Scheduler.update_settings()` signature, add:

```python
        enable_ai_review: bool | None = None,
```

In the method body after `requires_human_review`:

```python
            if enable_ai_review is not None:
                settings.enable_ai_review = bool(enable_ai_review)
```

In `agent_desk/dashboard.py`, pass the JSON field into `scheduler.update_settings()`:

```python
                        enable_ai_review=payload.get("enable_ai_review")
                        if "enable_ai_review" in payload
                        else None,
```

- [ ] **Step 8: Run scheduler tests**

Run:

```bash
python3 -m unittest tests.test_scheduler.SchedulerTests.test_workspace_settings_default_to_manual_single_worker_with_human_review tests.test_scheduler.SchedulerTests.test_workspace_settings_load_repo_auto_start_on_scheduler_start tests.test_scheduler.SchedulerTests.test_update_settings_toggles_ai_review -v
```

Expected: PASS.

- [ ] **Step 9: Add failing dashboard settings tests**

In `tests/test_dashboard.py`, update `test_state_payload_includes_workspace_scheduler_settings` expected settings dict:

```python
                "enable_ai_review": False,
```

In `test_dashboard_html_renders_workspace_settings_controls`, add:

```python
        self.assertIn("enable-ai-review", HTML)
        self.assertIn("enable_ai_review", HTML)
```

- [ ] **Step 10: Run dashboard tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_dashboard.DashboardTests.test_state_payload_includes_workspace_scheduler_settings tests.test_dashboard.DashboardTests.test_dashboard_html_renders_workspace_settings_controls -v
```

Expected: FAIL because HTML/JS do not include `enable_ai_review`.

- [ ] **Step 11: Implement dashboard setting control**

In `agent_desk/static/dashboard.html`, add this label after `requires-human-review`:

```html
        <label class="setting-row" for="enable-ai-review">
          <span>AI review before closeout</span>
          <input id="enable-ai-review" type="checkbox" onchange="markSettingsDirty()">
        </label>
```

In `agent_desk/static/dashboard.js`, add `enable-ai-review` to `settingsControls()`:

```javascript
    document.getElementById('enable-ai-review'),
```

In `renderSettings()`, add the default:

```javascript
    enable_ai_review: false,
```

and set the checkbox:

```javascript
  document.getElementById('enable-ai-review').checked = !!settings.enable_ai_review;
```

In `saveSettings()`, add:

```javascript
      enable_ai_review: document.getElementById('enable-ai-review').checked,
```

- [ ] **Step 12: Run settings tests**

Run:

```bash
python3 -m unittest tests.test_config tests.test_scheduler.SchedulerTests.test_workspace_settings_default_to_manual_single_worker_with_human_review tests.test_scheduler.SchedulerTests.test_workspace_settings_load_repo_auto_start_on_scheduler_start tests.test_scheduler.SchedulerTests.test_update_settings_toggles_ai_review tests.test_dashboard.DashboardTests.test_state_payload_includes_workspace_scheduler_settings tests.test_dashboard.DashboardTests.test_dashboard_html_renders_workspace_settings_controls -v
```

Expected: all listed tests pass.

- [ ] **Step 13: Commit Task 2**

Run:

```bash
git add agent_desk/config.py agent_desk/scheduler.py agent_desk/dashboard.py agent_desk/static/dashboard.html agent_desk/static/dashboard.js config/repos.example.toml tests/test_config.py tests/test_scheduler.py tests/test_dashboard.py
git commit -m "Add AI review workspace setting"
```

Expected: commit succeeds.

---

### Task 3: Add Independent AI Review Runner

**Files:**
- Create: `agent_desk/ai_review.py`
- Create: `schemas/ai-review-result.schema.json`
- Modify: `agent_desk/store.py`
- Modify: `agent_desk/dashboard.py`
- Test: `tests/test_ai_review.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `AgentDeskConfig`, `Store`, `CommandRunner`, `CommandResult`, `PullRequestChecksStatus`
- Produces: `AIReviewRunResult(ok: bool, status: str, message: str, run_id: int)`
- Produces: `AIReviewPayload(status: str, summary: str, findings: list[str], feedback: str, risks: list[str], pr_url: str)`
- Produces: `AIReviewRunner.review(run_id: int, pr_status: PullRequestChecksStatus) -> AIReviewRunResult`
- Produces: `render_ai_review_prompt(run: dict[str, Any], pr_status: PullRequestChecksStatus) -> str`
- Produces: `parse_ai_review_result(result_path: Path, stdout: str) -> AIReviewPayload`

- [ ] **Step 1: Add failing store defaults test**

In `tests/test_store.py`, add to the existing store test class:

```python
    def test_new_records_include_ai_review_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/example",
                issue_number=1,
                issue_title="Title",
                issue_url="https://example.test/1",
                branch_name="agent/issue-1",
            )

            run = store.get_run(run_id)

        self.assertEqual(run["ai_review_status"], "")
        self.assertEqual(run["ai_review_summary"], "")
        self.assertEqual(run["ai_review_feedback"], "")
        self.assertEqual(run["ai_review_checked_at"], "")
        self.assertEqual(run["ai_review_head_sha"], "")
```

- [ ] **Step 2: Run store test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_store.StoreTests.test_new_records_include_ai_review_fields -v
```

Expected: FAIL with missing `ai_review_status`.

- [ ] **Step 3: Add AI review defaults to store**

In `agent_desk/store.py`, add these fields to `_new_record()` after `ci_fix_last_sha`:

```python
            "ai_review_status": "",
            "ai_review_summary": "",
            "ai_review_feedback": "",
            "ai_review_checked_at": "",
            "ai_review_head_sha": "",
```

- [ ] **Step 4: Run store tests**

Run:

```bash
python3 -m unittest tests.test_store -v
```

Expected: all store tests pass.

- [ ] **Step 5: Create failing AI review runner tests**

Create `tests/test_ai_review.py`:

```python
import tempfile
import unittest
from pathlib import Path

from agent_desk.ai_review import AIReviewRunner, parse_ai_review_result, render_ai_review_prompt
from agent_desk.config import AgentDeskConfig, RepoConfig
from agent_desk.github_client import PullRequestChecksStatus
from agent_desk.store import Store
from agent_desk.worker import CommandResult, FakeCommandRunner


class AIReviewTests(unittest.TestCase):
    def _config_store_run(self, root: Path):
        worktree = root / "worktree"
        worktree.mkdir()
        run_dir = root / "run"
        run_dir.mkdir()
        config = AgentDeskConfig(
            data_dir=root / "data",
            repos=[RepoConfig(name="octo/example", local_path=root / "repo", base_branch="main")],
        )
        store = Store(root / "desk.sqlite")
        run_id = store.create_run(
            repo_name="octo/example",
            issue_number=7,
            issue_title="Fix parser",
            issue_url="https://github.com/octo/example/issues/7",
            branch_name="agent/issue-7-fix-parser-run-1",
            issue_body="Parser drops escaped commas.",
        )
        store.update_run(
            run_id,
            state="pr_open",
            stage="pull request opened",
            run_dir=str(run_dir),
            worktree_path=str(worktree),
            codex_thread_id="thread-1",
            pr_url="https://github.com/octo/example/pull/9",
        )
        store.add_event(
            run_id,
            "info",
            "worker-result",
            "Worker finished with status pr_open",
            {
                "summary": "Implemented parser fix.",
                "tests": ["python3 -m unittest tests.test_parser -v"],
                "questions": [],
                "risks": ["No remote CI is configured."],
                "decision_log": ["Kept the change scoped to parser escaping."],
            },
        )
        return config, store, run_id, worktree, run_dir

    def test_render_ai_review_prompt_is_english_and_read_only(self):
        run = {
            "repo_name": "octo/example",
            "issue_number": 7,
            "issue_title": "Fix parser",
            "issue_url": "https://github.com/octo/example/issues/7",
            "issue_body": "Parser drops escaped commas.",
            "branch_name": "agent/issue-7",
            "pr_url": "https://github.com/octo/example/pull/9",
            "run_dir": "/tmp/run",
            "worktree_path": "/tmp/worktree",
            "events": [],
        }
        pr_status = PullRequestChecksStatus(
            state="no_ci",
            summary="No checks reported",
            head_sha="abc123",
            checks=[],
        )

        prompt = render_ai_review_prompt(run, pr_status)

        self.assertIn("You are an independent AI reviewer", prompt)
        self.assertIn("Do not edit files, commit, push, or merge", prompt)
        self.assertIn("Treat no_ci as a real absence of GitHub CI", prompt)
        self.assertIn('"status": "approved | changes_requested | blocked"', prompt)
        self.assertIn("Return only JSON", prompt)

    def test_parse_ai_review_result_reads_result_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "ai-review-result.json"
            result_path.write_text(
                '{"status":"changes_requested","summary":"Needs tests","findings":["Missing test"],"feedback":"Please add a parser regression test.","risks":["Untested edge case"],"pr_url":"https://github.com/octo/example/pull/9"}',
                encoding="utf-8",
            )

            payload = parse_ai_review_result(result_path, "")

        self.assertEqual(payload.status, "changes_requested")
        self.assertEqual(payload.feedback, "Please add a parser regression test.")
        self.assertEqual(payload.findings, ["Missing test"])

    def test_review_approved_records_review_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, worktree, run_dir = self._config_store_run(root)
            pr_status = PullRequestChecksStatus(
                state="success",
                summary="2 passed",
                head_sha="abc123",
                checks=[{"name": "unit", "state": "SUCCESS"}],
            )
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec"],
                        0,
                        '{"status":"approved","summary":"Looks good","findings":[],"feedback":"","risks":[],"pr_url":"https://github.com/octo/example/pull/9"}',
                        "",
                    )
                ]
            )

            result = AIReviewRunner(config, store, runner=runner).review(run_id, pr_status)
            run = store.get_run(run_id)
            call = runner.calls[0]

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "approved")
        self.assertEqual(call.cwd, worktree)
        self.assertIn("codex", call.argv)
        self.assertIn("--output-last-message", call.argv)
        self.assertEqual(run["state"], "pr_open")
        self.assertEqual(run["stage"], "ai-review approved")
        self.assertEqual(run["ai_review_status"], "approved")
        self.assertEqual(run["ai_review_summary"], "Looks good")
        self.assertEqual(run["ai_review_head_sha"], "abc123")
        self.assertTrue((run_dir / "ai-review-prompt.md").exists())
        self.assertTrue((run_dir / "ai-review-result.json").exists())

    def test_review_changes_requested_requires_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, _worktree, _run_dir = self._config_store_run(root)
            pr_status = PullRequestChecksStatus(state="no_ci", summary="No checks reported", head_sha="abc123", checks=[])
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec"],
                        0,
                        '{"status":"changes_requested","summary":"Needs tests","findings":["Missing regression"],"feedback":"Please add a regression test.","risks":[],"pr_url":"https://github.com/octo/example/pull/9"}',
                        "",
                    )
                ]
            )

            result = AIReviewRunner(config, store, runner=runner).review(run_id, pr_status)
            run = store.get_run(run_id)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "changes_requested")
        self.assertEqual(run["state"], "pr_open")
        self.assertEqual(run["stage"], "ai-review changes requested")
        self.assertEqual(run["ai_review_feedback"], "Please add a regression test.")

    def test_review_blocks_when_changes_requested_has_no_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, _worktree, _run_dir = self._config_store_run(root)
            pr_status = PullRequestChecksStatus(state="success", summary="1 passed", head_sha="abc123", checks=[])
            runner = FakeCommandRunner(
                [
                    CommandResult(
                        ["codex", "exec"],
                        0,
                        '{"status":"changes_requested","summary":"Needs work","findings":["Missing regression"],"feedback":"","risks":[],"pr_url":"https://github.com/octo/example/pull/9"}',
                        "",
                    )
                ]
            )

            result = AIReviewRunner(config, store, runner=runner).review(run_id, pr_status)
            run = store.get_run(run_id)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(run["state"], "blocked")
        self.assertIn("feedback is required", run["last_error"])

    def test_review_blocks_without_worktree_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, store, run_id, _worktree, _run_dir = self._config_store_run(root)
            store.update_run(run_id, worktree_path="")
            pr_status = PullRequestChecksStatus(state="success", summary="1 passed", head_sha="abc123", checks=[])

            result = AIReviewRunner(config, store, runner=FakeCommandRunner([])).review(run_id, pr_status)
            run = store.get_run(run_id)

        self.assertFalse(result.ok)
        self.assertEqual(run["state"], "blocked")
        self.assertIn("ai-review requires worktree_path", run["last_error"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 6: Run AI review tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_ai_review -v
```

Expected: ERROR because `agent_desk.ai_review` does not exist.

- [ ] **Step 7: Create AI review result schema**

Create `schemas/ai-review-result.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["status", "summary", "findings", "feedback", "risks", "pr_url"],
  "properties": {
    "status": {
      "type": "string",
      "enum": ["approved", "changes_requested", "blocked"]
    },
    "summary": {
      "type": "string"
    },
    "findings": {
      "type": "array",
      "items": { "type": "string" }
    },
    "feedback": {
      "type": "string"
    },
    "risks": {
      "type": "array",
      "items": { "type": "string" }
    },
    "pr_url": {
      "type": "string"
    }
  }
}
```

- [ ] **Step 8: Implement `agent_desk/ai_review.py`**

Create `agent_desk/ai_review.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AgentDeskConfig
from .github_client import PullRequestChecksStatus
from .store import Store, utc_now
from .worker import CommandRunner, parse_json_object, run_directory


@dataclass(frozen=True)
class AIReviewPayload:
    status: str
    summary: str
    findings: list[str]
    feedback: str
    risks: list[str]
    pr_url: str


@dataclass(frozen=True)
class AIReviewRunResult:
    ok: bool
    status: str
    message: str
    run_id: int


class AIReviewRunner:
    def __init__(
        self,
        config: AgentDeskConfig,
        store: Store,
        runner: CommandRunner | None = None,
    ):
        self.config = config
        self.store = store
        self.runner = runner or CommandRunner()

    def review(self, run_id: int, pr_status: PullRequestChecksStatus) -> AIReviewRunResult:
        run = self.store.get_run(run_id)
        worktree_raw = str(run.get("worktree_path") or "")
        if not worktree_raw:
            return self._block(run_id, "ai-review requires worktree_path")
        worktree_path = Path(worktree_raw)
        run_dir_raw = str(run.get("run_dir") or "")
        run_dir = Path(run_dir_raw) if run_dir_raw else run_directory(self.config.data_dir, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt = render_ai_review_prompt(run, pr_status)
        prompt_path = run_dir / "ai-review-prompt.md"
        result_path = run_dir / "ai-review-result.json"
        prompt_path.write_text(prompt, encoding="utf-8")
        self.store.update_run(run_id, state="running", stage="ai-review", last_error="")
        self.store.add_event(
            run_id,
            "info",
            "ai-review",
            "Starting AI review",
            {"summary": pr_status.summary, "state": pr_status.state, "head_sha": pr_status.head_sha},
        )
        argv = [
            "codex",
            "--ask-for-approval",
            "never",
            "--sandbox",
            "workspace-write",
            "-C",
            str(worktree_path),
            "exec",
            "--json",
        ]
        schema_path = Path(__file__).resolve().parent.parent / "schemas" / "ai-review-result.schema.json"
        if schema_path.exists():
            argv.extend(["--output-schema", str(schema_path)])
        argv.extend(["--output-last-message", str(result_path), "-"])
        completed = self.runner.run(
            argv,
            cwd=worktree_path,
            stdin=prompt,
            timeout=self.config.worker_timeout_seconds,
            idle_timeout=self.config.worker_idle_timeout_seconds,
            stdout_path=run_dir / "ai-review.stdout.jsonl",
            stderr_path=run_dir / "ai-review.stderr.log",
        )
        if completed.returncode != 0:
            message = "AI review failed"
            if completed.timeout_reason == "idle":
                message = "AI review idle timeout"
            elif completed.timeout_reason == "timeout":
                message = "AI review timeout"
            return self._block(run_id, message, {"detail": completed.stderr[-4000:]})
        try:
            payload = parse_ai_review_result(result_path, completed.stdout)
        except ValueError as exc:
            return self._block(run_id, str(exc))
        return self._record_payload(run_id, payload, pr_status)

    def _record_payload(
        self,
        run_id: int,
        payload: AIReviewPayload,
        pr_status: PullRequestChecksStatus,
    ) -> AIReviewRunResult:
        status = payload.status
        if status == "changes_requested" and not payload.feedback.strip():
            return self._block(run_id, "AI review changes_requested feedback is required")
        if status not in {"approved", "changes_requested", "blocked"}:
            return self._block(run_id, f"AI review returned unexpected status: {status}")
        fields = {
            "ai_review_status": status,
            "ai_review_summary": payload.summary,
            "ai_review_feedback": payload.feedback,
            "ai_review_checked_at": utc_now(),
            "ai_review_head_sha": pr_status.head_sha,
        }
        if status == "blocked":
            self.store.update_run(
                run_id,
                state="blocked",
                stage="ai-review blocked",
                last_error=payload.summary,
                **fields,
            )
            self.store.add_event(run_id, "warning", "ai-review", payload.summary, payload.__dict__)
            return AIReviewRunResult(False, "blocked", payload.summary, run_id)
        stage = "ai-review approved" if status == "approved" else "ai-review changes requested"
        self.store.update_run(run_id, state="pr_open", stage=stage, last_error="", **fields)
        self.store.add_event(run_id, "info", "ai-review", payload.summary, payload.__dict__)
        return AIReviewRunResult(True, status, payload.summary, run_id)

    def _block(
        self,
        run_id: int,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> AIReviewRunResult:
        self.store.update_run(
            run_id,
            state="blocked",
            stage="ai-review blocked",
            ai_review_status="blocked",
            ai_review_summary=message,
            ai_review_checked_at=utc_now(),
            last_error=message,
        )
        self.store.add_event(run_id, "error", "ai-review", message, payload or {})
        return AIReviewRunResult(False, "blocked", message, run_id)


def render_ai_review_prompt(run: dict[str, Any], pr_status: PullRequestChecksStatus) -> str:
    worker = latest_worker_payload(run)
    return f"""You are an independent AI reviewer for an Agent Desk pull request.

You are not the implementation worker. Do not edit files, commit, push, or merge.
Review the pull request and return a structured review decision.

Repository: {run['repo_name']}
Issue: #{run['issue_number']} {run.get('issue_title') or ''}
Issue URL: {run.get('issue_url') or ''}
Pull request: {run.get('pr_url') or '(missing PR URL)'}
Branch: {run.get('branch_name') or ''}
Run directory: {run.get('run_dir') or ''}
Worktree: {run.get('worktree_path') or ''}

Issue body:
---
{run.get('issue_body') or ''}
---

PR gate status: {pr_status.state}
PR gate summary: {pr_status.summary}
PR head SHA: {pr_status.head_sha or '(unknown)'}

Worker summary: {worker.get('summary') or '(missing)'}
Worker tests:
{format_string_list(worker.get('tests') or [])}
Worker questions:
{format_string_list(worker.get('questions') or [])}
Worker risks:
{format_string_list(worker.get('risks') or [])}
Worker decision log:
{format_string_list(worker.get('decision_log') or [])}

Review instructions:
1. Inspect the PR metadata and diff using gh and local git commands where useful.
2. Check whether the PR satisfies the issue objective and stated acceptance criteria.
3. Check whether the worker's verification evidence is credible and scoped to the change.
4. Check for obvious regressions, unrelated changes, missing tests, or unsafe closeout risk.
5. Treat no_ci as a real absence of GitHub CI. When PR gate status is no_ci, inspect the recorded local verification especially carefully.
6. Do not make changes. Do not push. Do not merge.

Decision rules:
- Use approved when there are no blocking findings.
- Use changes_requested when there is a clear, actionable fix. Put the exact request in feedback so Agent Desk can send it to the implementation worker.
- Use blocked when you cannot complete a reliable review, cannot inspect the diff, or find a high-risk problem without a clear repair instruction.

Return only JSON with this shape:
{{
  "status": "approved | changes_requested | blocked",
  "summary": "Short reviewer summary.",
  "findings": ["Actionable finding or non-blocking note."],
  "feedback": "Text suitable for sending to the implementation Codex thread.",
  "risks": ["Residual risk."],
  "pr_url": "{run.get('pr_url') or ''}"
}}
"""


def latest_worker_payload(run: dict[str, Any]) -> dict[str, Any]:
    for event in reversed(run.get("events") or []):
        if str(event.get("event_type") or "") not in {"codex-done", "worker-result"}:
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            return payload
    return {}


def format_string_list(values: list[Any]) -> str:
    if not values:
        return "- (none)"
    return "\n".join(f"- {str(value)}" for value in values)


def parse_ai_review_result(result_path: Path, stdout: str) -> AIReviewPayload:
    candidates = []
    if result_path.exists():
        candidates.append(result_path.read_text(encoding="utf-8"))
    candidates.extend(line for line in stdout.splitlines() if line.strip())
    for candidate in candidates:
        parsed = parse_json_object(candidate)
        if parsed and "status" in parsed:
            return normalize_ai_review_payload(parsed)
    raise ValueError("Could not parse AI review result JSON")


def normalize_ai_review_payload(payload: dict[str, Any]) -> AIReviewPayload:
    findings_raw = payload.get("findings") or []
    risks_raw = payload.get("risks") or []
    findings = [str(item) for item in findings_raw] if isinstance(findings_raw, list) else [str(findings_raw)]
    risks = [str(item) for item in risks_raw] if isinstance(risks_raw, list) else [str(risks_raw)]
    return AIReviewPayload(
        status=str(payload.get("status") or "blocked"),
        summary=str(payload.get("summary") or payload.get("status") or "AI review returned no summary"),
        findings=findings,
        feedback=str(payload.get("feedback") or ""),
        risks=risks,
        pr_url=str(payload.get("pr_url") or ""),
    )
```

- [ ] **Step 9: Add AI review log files to dashboard log ordering**

In `agent_desk/dashboard.py`, add these entries to `LOG_FILE_ORDER` after auto-finish logs:

```python
    "ai-review-prompt.md",
    "ai-review.stdout.jsonl",
    "ai-review.stderr.log",
    "ai-review-result.json",
```

- [ ] **Step 10: Run AI review tests**

Run:

```bash
python3 -m unittest tests.test_ai_review tests.test_store -v
```

Expected: all tests pass.

- [ ] **Step 11: Commit Task 3**

Run:

```bash
git add agent_desk/ai_review.py agent_desk/store.py agent_desk/dashboard.py schemas/ai-review-result.schema.json tests/test_ai_review.py tests/test_store.py
git commit -m "Add independent AI review runner"
```

Expected: commit succeeds.

---

### Task 4: Integrate Scheduler PR Gate, AI Review, And Feedback Follow-Up

**Files:**
- Modify: `agent_desk/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `AIReviewRunner.review(run_id, pr_status)`
- Produces: Scheduler constructor argument `ai_review_factory: Callable[[AgentDeskConfig, Store], AIReviewRunner] | None`
- Produces: private `_handle_closeout_ready_pr(run: dict, pr_status: PullRequestChecksStatus) -> RunNextResult`
- Produces: private `_handle_ai_review(run: dict, pr_status: PullRequestChecksStatus) -> RunNextResult`
- Produces: private `_run_ai_review(run_id: int, pr_status: PullRequestChecksStatus) -> None`
- Produces: detached job kind `"ai-review"`

- [ ] **Step 1: Extend scheduler test fakes**

In `tests/test_scheduler.py`, add these classes near the other fake continuation classes:

```python
class FakeAIReviewRunner:
    def __init__(self, store, status="approved", message="review ok", feedback=""):
        self.store = store
        self.status = status
        self.message = message
        self.feedback = feedback
        self.calls = []

    def review(self, run_id, pr_status):
        self.calls.append((run_id, pr_status))
        if self.status == "approved":
            self.store.update_run(
                run_id,
                state="pr_open",
                stage="ai-review approved",
                ai_review_status="approved",
                ai_review_summary=self.message,
                ai_review_head_sha=pr_status.head_sha,
                last_error="",
            )
            return type("Result", (), {"ok": True, "status": "approved", "message": self.message, "run_id": run_id})()
        if self.status == "changes_requested":
            self.store.update_run(
                run_id,
                state="pr_open",
                stage="ai-review changes requested",
                ai_review_status="changes_requested",
                ai_review_summary=self.message,
                ai_review_feedback=self.feedback,
                ai_review_head_sha=pr_status.head_sha,
                last_error="",
            )
            return type("Result", (), {"ok": True, "status": "changes_requested", "message": self.message, "run_id": run_id})()
        self.store.update_run(
            run_id,
            state="blocked",
            stage="ai-review blocked",
            ai_review_status="blocked",
            ai_review_summary=self.message,
            ai_review_head_sha=pr_status.head_sha,
            last_error=self.message,
        )
        return type("Result", (), {"ok": False, "status": "blocked", "message": self.message, "run_id": run_id})()
```

- [ ] **Step 2: Add failing scheduler test for no-CI direct auto-finish**

Add to `SchedulerTests`:

```python
    def test_monitor_prs_auto_finishes_no_ci_when_human_review_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=1,
                issue_title="First",
                issue_url="https://example.test/1",
                branch_name="agent/issue-1-first-run-1",
            )
            store.update_run(
                run_id,
                state="pr_open",
                stage="pull request opened",
                pr_url="https://github.com/octo/one/pull/9",
                codex_thread_id="thread-1",
                worktree_path=str(root / "worktree"),
            )
            pr_status = PullRequestChecksStatus(
                state="no_ci",
                summary="No checks reported",
                head_sha="abc123",
                checks=[],
            )
            continuation = FakeContinuationRunner()
            scheduler = NoopScheduler(
                AgentDeskConfig(data_dir=root / "data", repos=[RepoConfig(name="octo/one", local_path=root / "one")]),
                store,
                github=FakePullRequestGitHub(pr_status),
                continuation_factory=lambda config, store: continuation,
            )
            scheduler.update_settings(workspace_path=root / "one", requires_human_review=False)

            scheduler.monitor_prs()
            run = store.get_run(run_id)

        self.assertEqual(run["pr_ci_status"], "no_ci")
        self.assertEqual(run["state"], "running")
        self.assertEqual(run["stage"], "auto-finishing after pr gate ready")
        self.assertEqual(continuation.calls, [("finish_after_ci_success", run_id)])
```

- [ ] **Step 3: Add failing scheduler tests for AI review dispatch and outcomes**

Add to `SchedulerTests`:

```python
    def test_monitor_prs_starts_ai_review_when_enabled_after_successful_ci(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=1,
                issue_title="First",
                issue_url="https://example.test/1",
                branch_name="agent/issue-1-first-run-1",
            )
            store.update_run(
                run_id,
                state="pr_open",
                stage="pull request opened",
                pr_url="https://github.com/octo/one/pull/9",
                codex_thread_id="thread-1",
                worktree_path=str(root / "worktree"),
            )
            pr_status = PullRequestChecksStatus(
                state="success",
                summary="2 passed",
                head_sha="abc123",
                checks=[{"name": "unit", "state": "SUCCESS"}],
            )
            ai_review = FakeAIReviewRunner(store, status="approved", message="review approved")
            continuation = FakeContinuationRunner()
            scheduler = NoopScheduler(
                AgentDeskConfig(data_dir=root / "data", repos=[RepoConfig(name="octo/one", local_path=root / "one")]),
                store,
                github=FakePullRequestGitHub(pr_status),
                continuation_factory=lambda config, store: continuation,
                ai_review_factory=lambda config, store: ai_review,
            )
            scheduler.update_settings(
                workspace_path=root / "one",
                requires_human_review=False,
                enable_ai_review=True,
            )

            scheduler.monitor_prs()
            run = store.get_run(run_id)

        self.assertEqual(run["stage"], "auto-finishing after pr gate ready")
        self.assertEqual(ai_review.calls, [(run_id, pr_status)])
        self.assertEqual(continuation.calls, [("finish_after_ci_success", run_id)])

    def test_ai_review_changes_requested_dispatches_request_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=1,
                issue_title="First",
                issue_url="https://example.test/1",
                branch_name="agent/issue-1-first-run-1",
            )
            store.update_run(
                run_id,
                state="pr_open",
                stage="pull request opened",
                pr_url="https://github.com/octo/one/pull/9",
                codex_thread_id="thread-1",
                worktree_path=str(root / "worktree"),
            )
            pr_status = PullRequestChecksStatus(state="no_ci", summary="No checks reported", head_sha="abc123", checks=[])
            ai_review = FakeAIReviewRunner(
                store,
                status="changes_requested",
                message="needs regression",
                feedback="Please add a regression test for escaped commas.",
            )
            continuation = FakeContinuationRunner()
            scheduler = NoopScheduler(
                AgentDeskConfig(data_dir=root / "data", repos=[RepoConfig(name="octo/one", local_path=root / "one")]),
                store,
                github=FakePullRequestGitHub(pr_status),
                continuation_factory=lambda config, store: continuation,
                ai_review_factory=lambda config, store: ai_review,
            )

            scheduler._run_ai_review(run_id=run_id, pr_status=pr_status)
            run = store.get_run(run_id)

        self.assertEqual(ai_review.calls, [(run_id, pr_status)])
        self.assertEqual(continuation.calls, [("request_changes", run_id, "Please add a regression test for escaped commas.")])
        self.assertEqual(run["stage"], "changes addressed")

    def test_ai_review_blocked_leaves_run_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=1,
                issue_title="First",
                issue_url="https://example.test/1",
                branch_name="agent/issue-1-first-run-1",
            )
            store.update_run(
                run_id,
                state="pr_open",
                stage="pull request opened",
                pr_url="https://github.com/octo/one/pull/9",
            )
            pr_status = PullRequestChecksStatus(state="success", summary="1 passed", head_sha="abc123", checks=[])
            ai_review = FakeAIReviewRunner(store, status="blocked", message="could not inspect diff")
            continuation = FakeContinuationRunner()
            scheduler = NoopScheduler(
                AgentDeskConfig(data_dir=root / "data", repos=[RepoConfig(name="octo/one", local_path=root / "one")]),
                store,
                github=FakePullRequestGitHub(pr_status),
                continuation_factory=lambda config, store: continuation,
                ai_review_factory=lambda config, store: ai_review,
            )

            scheduler._run_ai_review(run_id=run_id, pr_status=pr_status)
            run = store.get_run(run_id)

        self.assertEqual(run["state"], "blocked")
        self.assertEqual(run["stage"], "ai-review blocked")
        self.assertEqual(continuation.calls, [])
```

- [ ] **Step 4: Run new scheduler tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_scheduler.SchedulerTests.test_monitor_prs_auto_finishes_no_ci_when_human_review_disabled tests.test_scheduler.SchedulerTests.test_monitor_prs_starts_ai_review_when_enabled_after_successful_ci tests.test_scheduler.SchedulerTests.test_ai_review_changes_requested_dispatches_request_changes tests.test_scheduler.SchedulerTests.test_ai_review_blocked_leaves_run_blocked -v
```

Expected: FAIL because scheduler has no AI review factory and does not treat `no_ci` as closeout-ready.

- [ ] **Step 5: Wire AI review into scheduler constructor and job map**

In `agent_desk/scheduler.py`, add the import:

```python
from .ai_review import AIReviewRunner
```

Add to `JOB_KIND_BY_TARGET`:

```python
    "_run_ai_review": "ai-review",
```

Add `ai-review` to `run_job()`:

```python
        elif kind == "ai-review":
            repo = self._repo_for_run(run)
            pr_status = self.github.pr_checks_status(repo.name, str(run["pr_url"]))
            self._run_ai_review(run_id=run_id, pr_status=pr_status)
```

In `Scheduler.__init__()` signature, add:

```python
        ai_review_factory: Callable[[AgentDeskConfig, Store], AIReviewRunner] | None = None,
```

Store it after `continuation_factory`:

```python
        self.ai_review_factory = ai_review_factory or (lambda config, store: AIReviewRunner(config, store))
```

- [ ] **Step 6: Replace PR-ready monitor branch**

In `monitor_prs()`, replace:

```python
                elif pr_status.state == "success" and not self._settings_for_repo(repo).requires_human_review:
                    results.append(self._handle_successful_ci_without_review(run, pr_status))
```

with:

```python
                elif pr_status.state in {"success", "no_ci"} and not self._settings_for_repo(repo).requires_human_review:
                    results.append(self._handle_closeout_ready_pr(run, pr_status))
```

Rename `_handle_successful_ci_without_review()` to `_handle_closeout_ready_pr()` and implement AI review branching:

```python
    def _handle_closeout_ready_pr(
        self,
        run: dict,
        pr_status: PullRequestChecksStatus,
    ) -> RunNextResult:
        repo = self._repo_for_run(run)
        settings = self._settings_for_repo(repo)
        if settings.enable_ai_review:
            if (
                str(run.get("ai_review_status") or "") == "approved"
                and str(run.get("ai_review_head_sha") or "") == pr_status.head_sha
            ):
                return self._start_auto_finish(run, pr_status, source="ai-review")
            return self._handle_ai_review(run, pr_status)
        return self._start_auto_finish(run, pr_status, source="pr-gate")
```

Add `_start_auto_finish()`:

```python
    def _start_auto_finish(
        self,
        run: dict,
        pr_status: PullRequestChecksStatus,
        *,
        source: str,
    ) -> RunNextResult:
        run_id = int(run["id"])
        repo = self._repo_for_run(run)
        if self._settings_for_repo(repo).single_closeout_per_workspace:
            conflict = self._closeout_in_progress(repo, exclude_run_id=run_id)
            if conflict:
                return self._block_closeout_for_workspace(run_id, repo, conflict)
        self.store.update_run(run_id, state="running", stage="auto-finishing after pr gate ready", last_error="")
        self.store.add_event(
            run_id,
            "info",
            "auto-finish",
            "PR gate is ready and human review is disabled; starting closeout",
            {
                "summary": pr_status.summary,
                "checks": pr_status.checks,
                "head_sha": pr_status.head_sha,
                "state": pr_status.state,
                "source": source,
            },
        )
        self._start_daemon_thread(self._run_auto_finish, {"run_id": run_id})
        return RunNextResult(True, "Started automatic closeout after PR gate ready", run_id)
```

Add `_handle_ai_review()`:

```python
    def _handle_ai_review(self, run: dict, pr_status: PullRequestChecksStatus) -> RunNextResult:
        run_id = int(run["id"])
        self.store.update_run(run_id, state="running", stage="ai-review queued", last_error="")
        self.store.add_event(
            run_id,
            "info",
            "ai-review",
            "PR gate is ready; starting AI review",
            {"summary": pr_status.summary, "state": pr_status.state, "head_sha": pr_status.head_sha},
        )
        self._start_daemon_thread(self._run_ai_review, {"run_id": run_id, "pr_status": pr_status})
        return RunNextResult(True, "Started AI review", run_id)
```

- [ ] **Step 7: Add `_run_ai_review()` follow-up behavior**

In `agent_desk/scheduler.py`, add:

```python
    def _run_ai_review(self, *, run_id: int, pr_status: PullRequestChecksStatus) -> None:
        try:
            result = self.ai_review_factory(self._config_for_run_id(run_id), self.store).review(
                run_id,
                pr_status,
            )
            if result.status == "approved":
                self._start_auto_finish(self.store.get_run(run_id), pr_status, source="ai-review")
                return
            if result.status == "changes_requested":
                run = self.store.get_run(run_id)
                feedback = str(run.get("ai_review_feedback") or "").strip()
                if not feedback:
                    message = "AI review requested changes without feedback"
                    self.store.update_run(run_id, state="blocked", stage="ai-review blocked", last_error=message)
                    self.store.add_event(run_id, "error", "ai-review", message, {})
                    return
                request_result = self.request_changes(run_id, feedback)
                if not request_result.started:
                    self.store.update_run(run_id, state="blocked", stage="ai-review blocked", last_error=request_result.message)
                    self.store.add_event(
                        run_id,
                        "error",
                        "ai-review",
                        "Could not dispatch AI review feedback",
                        {"detail": request_result.message},
                    )
        except Exception as exc:
            self.store.update_run(run_id, state="failed", stage="failed", last_error=str(exc))
            self.store.add_event(run_id, "error", "ai-review", "AI review failed", {"detail": str(exc)})
```

- [ ] **Step 8: Run scheduler tests**

Run:

```bash
python3 -m unittest tests.test_scheduler -v
```

Expected: all scheduler tests pass after updating old expectations from `auto-finishing after ci success` to `auto-finishing after pr gate ready` where necessary.

- [ ] **Step 9: Add detached job test for AI review**

In `DetachedJobTests.test_run_job_closeout_kinds_call_continuation`, leave existing cases unchanged. Add a separate test:

```python
    def test_run_job_ai_review_refetches_pr_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "desk.sqlite")
            pr_status = PullRequestChecksStatus(state="success", summary="ok", checks=[], head_sha="abc")
            github = FakePullRequestGitHub(pr_status)
            ai_review = FakeAIReviewRunner(store, status="blocked", message="stop after review")
            scheduler = Scheduler(
                self._config(root),
                store,
                github=github,
                ai_review_factory=lambda config, store: ai_review,
            )
            run_id = store.create_run(
                repo_name="octo/one",
                issue_number=8,
                issue_title="T",
                issue_url="u8",
                branch_name="agent/issue-8",
            )
            store.update_run(run_id, pr_url="https://example.test/pr/8")

            scheduler.run_job(run_id, "ai-review")

        self.assertEqual(github.pr_status_calls, [("octo/one", "https://example.test/pr/8")])
        self.assertEqual(ai_review.calls, [(run_id, pr_status)])
```

- [ ] **Step 10: Run detached job test**

Run:

```bash
python3 -m unittest tests.test_scheduler.DetachedJobTests.test_run_job_ai_review_refetches_pr_status -v
```

Expected: PASS.

- [ ] **Step 11: Commit Task 4**

Run:

```bash
git add agent_desk/scheduler.py tests/test_scheduler.py
git commit -m "Gate auto-closeout through AI review"
```

Expected: commit succeeds.

---

### Task 5: Render No-CI And AI Review Status In Dashboard

**Files:**
- Modify: `agent_desk/static/dashboard.js`
- Modify: `agent_desk/static/dashboard.html`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: run fields `pr_ci_status`, `pr_ci_summary`, `ai_review_status`, `ai_review_summary`, `stage`
- Produces: `prStatus(run)` label for `no_ci`
- Produces: `aiReviewStatus(run)` HTML snippet

- [ ] **Step 1: Add failing dashboard render tests**

In `tests/test_dashboard.py`, update `test_dashboard_html_renders_pr_ci_status`:

```python
        self.assertIn("No CI", HTML)
```

Add a new test:

```python
    def test_dashboard_html_renders_ai_review_status(self):
        self.assertIn("aiReviewStatus(run)", HTML)
        self.assertIn("AI review running", HTML)
        self.assertIn("AI review approved", HTML)
        self.assertIn("AI review changes requested", HTML)
        self.assertIn("AI review blocked", HTML)
```

- [ ] **Step 2: Run dashboard render tests to verify they fail**

Run:

```bash
python3 -m unittest tests.test_dashboard.DashboardTests.test_dashboard_html_renders_pr_ci_status tests.test_dashboard.DashboardTests.test_dashboard_html_renders_ai_review_status -v
```

Expected: FAIL because `No CI` and `aiReviewStatus` are not rendered.

- [ ] **Step 3: Implement `No CI` PR label**

In `agent_desk/static/dashboard.js`, update `prStatus()` labels:

```javascript
  const labels = {
    pending: 'CI running',
    success: 'CI passed',
    failure: 'CI failed',
    no_ci: 'No CI',
    unknown: 'CI unknown'
  };
```

- [ ] **Step 4: Implement AI review status renderer**

In `agent_desk/static/dashboard.js`, add this function after `prStatus(run)`:

```javascript
function aiReviewStatus(run) {
  const status = run.ai_review_status || '';
  const running = String(run.stage || '').startsWith('ai-review') && run.state === 'running';
  if (!status && !running) return '';
  const labels = {
    approved: 'AI review approved',
    changes_requested: 'AI review changes requested',
    blocked: 'AI review blocked'
  };
  const label = running ? 'AI review running' : (labels[status] || 'AI review');
  const summary = run.ai_review_summary ? ` · ${esc(run.ai_review_summary)}` : '';
  const cls = running ? 'running' : esc(status || 'unknown');
  return `<div class="ai-review-status ai-review-status-${cls}"><strong>${esc(label)}</strong><span class="muted">${summary}</span></div>`;
}
```

In `runHtml(run)`, add it after `${prStatus(run)}`:

```javascript
    ${aiReviewStatus(run)}
```

- [ ] **Step 5: Run dashboard tests**

Run:

```bash
python3 -m unittest tests.test_dashboard -v
```

Expected: all dashboard tests pass.

- [ ] **Step 6: Manual visual smoke test**

Run:

```bash
make test
```

Expected: full test suite passes.

Then run:

```bash
make serve
```

Expected: dashboard starts and prints a localhost URL. Open the dashboard and confirm Workspace Settings contains `AI review before closeout`. Stop the server with Ctrl-C after the check.

- [ ] **Step 7: Commit Task 5**

Run:

```bash
git add agent_desk/static/dashboard.js agent_desk/static/dashboard.html tests/test_dashboard.py
git commit -m "Show no-CI and AI review status"
```

Expected: commit succeeds.

---

### Task 6: Documentation And Final Verification

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Test: full project tests

**Interfaces:**
- Consumes: behavior implemented in Tasks 1-5
- Produces: user-facing docs for `enable_ai_review`, `no_ci`, and review feedback loop

- [ ] **Step 1: Update README settings examples**

In `README.md`, add `enable_ai_review = false` to both multiple-repository TOML examples after `requires_human_review = true`.

In the runtime settings block, change:

```toml
requires_human_review = true
single_closeout_per_workspace = true
```

to:

```toml
requires_human_review = true
enable_ai_review = false
single_closeout_per_workspace = true
```

- [ ] **Step 2: Update README PR review section**

In `README.md` under `## PR Review And Closeout`, add this paragraph before `Agent Desk records...`:

```markdown
When human review is disabled, Agent Desk treats `CI passed` and `No CI` as
closeout-ready PR gates. `No CI` means GitHub explicitly reported no checks for
the PR; `CI unknown` still means Agent Desk could not determine the status and
will not close out automatically. If `AI review before closeout` is enabled for
the workspace, Agent Desk runs an independent Codex review worker before
automatic closeout. Passing AI reviews proceed to closeout; requested changes
are sent back to the original Codex thread through the existing request-changes
flow.
```

- [ ] **Step 3: Update CLAUDE architecture notes**

In `CLAUDE.md`, update the scheduler bullet to mention no-CI and AI review:

```markdown
  `monitor_prs` polls open PRs, records `success`/`pending`/`failure`/`no_ci`/`unknown`,
  drives auto-CI-fix (up to `MAX_CI_FIX_ATTEMPTS=3`), optionally gates automatic
  closeout through an independent AI review worker, and auto-closes out when
  human review is disabled.
```

Add a new architecture bullet after `continuation.py`:

```markdown
- **`ai_review.py` - independent PR review worker.** Runs a fresh `codex exec`
  reviewer prompt against the PR worktree, never resumes the implementation
  thread, and records `ai_review_*` fields on the run. Approved reviews let the
  scheduler continue to automatic closeout; requested changes are sent back to
  the original thread through `request_changes`.
```

- [ ] **Step 4: Run documentation grep checks**

Run:

```bash
rg -n "enable_ai_review|No CI|AI review before closeout|ai_review.py" README.md CLAUDE.md config/repos.example.toml agent_desk
```

Expected: output includes the new setting in config/docs, dashboard code, scheduler/config, and AI review module.

- [ ] **Step 5: Run targeted behavior tests**

Run:

```bash
python3 -m unittest tests.test_github_client tests.test_continuation tests.test_ai_review tests.test_scheduler tests.test_dashboard tests.test_config tests.test_store -v
```

Expected: all targeted tests pass.

- [ ] **Step 6: Run full test suite**

Run:

```bash
make test
```

Expected: `python3 -m unittest discover -s tests -v` passes.

- [ ] **Step 7: Check git diff hygiene**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 8: Commit Task 6**

Run:

```bash
git add README.md CLAUDE.md
git commit -m "Document AI review no-CI closeout"
```

Expected: commit succeeds.

---

## Plan Self-Review

Spec coverage:

- `no_ci` versus `unknown`: Task 1.
- No-CI direct auto-closeout when human review is disabled: Tasks 1 and 4.
- Optional workspace setting: Task 2.
- Independent Codex CLI AI review worker with English prompt: Task 3.
- AI review approved to auto-finish: Task 4.
- AI review changes requested to original thread: Task 4.
- AI review blocked behavior: Tasks 3 and 4.
- Panel visibility for No CI and AI review: Task 5.
- Detached job support: Task 4.
- Documentation and dependency-free verification: Task 6.

Placeholder scan:

- No placeholder markers or undefined task references are intentional in this plan.
- Each task includes exact files, tests, commands, expected outcomes, and commit commands.

Type consistency:

- Scheduler setting is consistently named `enable_ai_review`.
- Run fields are consistently named `ai_review_status`, `ai_review_summary`, `ai_review_feedback`, `ai_review_checked_at`, and `ai_review_head_sha`.
- AI review result classes are consistently named `AIReviewPayload` and `AIReviewRunResult`.
- PR status value is consistently named `no_ci`.

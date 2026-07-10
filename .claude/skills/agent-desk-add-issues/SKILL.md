---
name: agent-desk-add-issues
description: Add existing GitHub issues from the current repository to a running Agent Desk dashboard. Use when the user asks to queue, add, hand off, or send already-created issue numbers to Agent Desk, especially when the invoking agent already knows the dependency graph.
---

# Agent Desk Add Issues

Use this from any repository managed by Agent Desk. The skill queues existing
GitHub issues into the local Agent Desk dashboard with the dependency graph the
calling agent already knows.

Installed constants:

```text
AGENT_DESK_ROOT={{AGENT_DESK_ROOT}}
DEFAULT_AGENT_DESK_URL={{DEFAULT_AGENT_DESK_URL}}
```

If either value still contains `{{...}}`, this template was not installed by
Agent Desk onboarding. Ask the user to rerun onboarding or provide
`AGENT_DESK_URL` and the Agent Desk checkout path.

## Rules

- Do not create GitHub issues.
- Do not start Agent Desk automatically.
- Do not call `run-next`, `/api/actions/run-next`, or any `/api/run/*/start` endpoint.
- Do not use `dependency_mode: "analyze"` by default. The caller should provide
  the dependency graph it already knows.
- Do not implement the main flow as `direct` followed by `dependency-edge`; that
  creates a ready-window race when `auto_start_ready` is enabled.

## Workflow

1. Infer the current repository:
   ```bash
   git_root=$(git rev-parse --show-toplevel)
   git -C "$git_root" remote get-url origin
   ```
   Parse the origin remote into `OWNER/REPO`. If parsing is ambiguous, ask the
   user for the repo name.

2. Choose the dashboard URL:
   - Use `$AGENT_DESK_URL` when set.
   - Otherwise use `{{DEFAULT_AGENT_DESK_URL}}`.
   ```bash
   agent_desk_url="${AGENT_DESK_URL:-{{DEFAULT_AGENT_DESK_URL}}}"
   agent_desk_url="${agent_desk_url%/}"
   ```

3. Check the dashboard:
   ```bash
   curl -fsS "$agent_desk_url/api/state"
   ```
   If it is unreachable, stop and ask the user to run:
   ```bash
   cd {{AGENT_DESK_ROOT}}
   make serve
   ```
   If Agent Desk auto-increments to another port, ask the user for that URL or
   have them set `AGENT_DESK_URL`.

4. Confirm the current repo is registered. In `/api/state`, match either:
   - `project.path == git_root`
   - `project.name == OWNER/REPO`

   If neither matches, stop and ask the user to register the repo, then restart
   or refresh Agent Desk:
   ```bash
   cd {{AGENT_DESK_ROOT}}
   python3 -m agent_desk add-repo --path "$git_root" --name OWNER/REPO
   ```

5. Build the issue list and dependency edges from the caller's known plan.
   Represent dependencies as edges from an issue to its prerequisite:
   ```json
   [
     {
       "issue": 18,
       "dependency_repo": "OWNER/REPO",
       "dependency": 12,
       "evidence": "issue-authoring plan: #18 after #12"
     }
   ]
   ```
   Use an empty list when the caller knows there are no dependencies.

6. Sync GitHub issues into Agent Desk's local `available` state:
   ```bash
   curl -fsS -X POST "$agent_desk_url/api/actions/sync-issues" \
     -H 'Content-Type: application/json' \
     -d '{"repo":"OWNER/REPO"}'
   ```

7. Queue the issues atomically with provided dependencies:
   ```bash
   curl -fsS -X POST "$agent_desk_url/api/actions/include-issues" \
     -H 'Content-Type: application/json' \
     -d '{
       "repo": "OWNER/REPO",
       "issues": [12, 15, 18],
       "dependency_mode": "provided",
       "dependencies": [
         {
           "issue": 18,
           "dependency_repo": "OWNER/REPO",
           "dependency": 12,
           "evidence": "issue-authoring plan: #18 after #12"
         }
       ]
     }'
   ```

   If the API returns `unknown dependency mode: provided`, the running Agent Desk
   is too old or has not been restarted after upgrade. Ask the user to update and
   restart Agent Desk.

8. Verify final local states:
   ```bash
   python3 - <<'PY'
   import json
   import os
   import urllib.parse
   import urllib.request

   base = os.environ.get("AGENT_DESK_URL", "{{DEFAULT_AGENT_DESK_URL}}").rstrip("/")
   repo = "OWNER/REPO"
   url = f"{base}/api/issues?repo={urllib.parse.quote(repo, safe='')}"
   with urllib.request.urlopen(url, timeout=10) as response:
       payload = json.load(response)
   for issue in payload["issues"]:
       if int(issue["number"]) in {12, 15, 18}:
           print(issue["number"], issue["state"], issue.get("blocked_by") or [])
   PY
   ```

Report each requested issue as `ready`, `waiting_dependencies`, already on desk,
or missing. Mention that no worker was started.

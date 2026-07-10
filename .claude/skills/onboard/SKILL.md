---
name: onboard
description: Use when setting up Agent Desk for the first time on a machine — verifies prerequisites (Python 3.11+, gh, git, codex), generates and walks the user through config/repos.toml, confirms safety defaults, and launches the local dashboard. Triggers on "onboard", "set up agent desk", "get me started".
---

# Onboard Agent Desk

Agent Desk is a local manager for issue-to-Codex worker loops. It has **zero
Python dependencies** — it runs on the standard library plus the `gh`, `git`,
and `codex` command-line tools. Onboarding is therefore about checking tools,
writing a config, and starting the dashboard — not installing packages.

Run from the repository root. Work through the checklist in order and stop at the
first failure rather than pushing ahead.

## Checklist

- [ ] **Verify prerequisites.** All four must be present:
  ```bash
  python3 --version   # must be >= 3.11
  gh --version        # GitHub CLI
  git --version
  codex --version     # Codex CLI
  ```
  If any are missing, tell the user what to install and stop. Also confirm
  `gh auth status` shows an authenticated account — Agent Desk reads issues
  through `gh`.

- [ ] **Generate the config.** Create `config/repos.toml` from the template:
  ```bash
  make init
  ```
  (`make init` wraps `python3 -m agent_desk init-config`.) If the file already
  exists, leave it; do not overwrite the user's settings.

- [ ] **Install the global add-issues skill.** This lets agents invoked from
  other managed repositories queue existing issue numbers into this Agent Desk
  instance. Run from the Agent Desk repository root:
  ```bash
  skill_src="$PWD/.claude/skills/agent-desk-add-issues"
  skill_dst="${CODEX_HOME:-$HOME/.codex}/skills/agent-desk-add-issues"
  mkdir -p "$skill_dst"
  cp -R "$skill_src"/. "$skill_dst"/
  AGENT_DESK_INSTALL_ROOT="$PWD" \
  AGENT_DESK_INSTALL_SKILL="$skill_dst/SKILL.md" \
  python3 - <<'PY'
  from pathlib import Path
  import os

  root = Path(os.environ["AGENT_DESK_INSTALL_ROOT"]).resolve()
  skill = Path(os.environ["AGENT_DESK_INSTALL_SKILL"])
  url = os.environ.get("AGENT_DESK_URL", "http://127.0.0.1:8765")
  text = skill.read_text(encoding="utf-8")
  text = text.replace("{{AGENT_DESK_ROOT}}", str(root))
  text = text.replace("{{DEFAULT_AGENT_DESK_URL}}", url)
  skill.write_text(text, encoding="utf-8")
  print(f"Installed {skill}")
  PY
  ```
  If the dashboard later runs on a different port, users can set
  `AGENT_DESK_URL` when invoking the skill.

- [ ] **Edit `config/repos.toml` with the user.** Read the file, then fill in
  one `[[repos]]` block per repository the user wants to manage. The fields that
  must be correct before anything works:
  - `name` — the GitHub `OWNER/REPO`.
  - `local_path` — an **absolute** path to an existing local clone.
  - `base_branch` — usually `main`.
  - `test_command` — how that repo runs its tests.

  Ask the user for these rather than guessing. Confirm each `local_path` exists.

- [ ] **Confirm the safety defaults.** The generated config ships read-only:
  ```toml
  mutate_github = false   # do not change issue labels
  push_pr = false         # keep branches local, do not open PRs
  closeout_sandbox = "workspace-write"
  ```
  Leave these off for the first run. Explain that Agent Desk will read issues and
  run local workers, but will not touch GitHub until the user flips these
  *after* the local loop behaves as expected.

- [ ] **Launch the dashboard.**
  ```bash
  make serve
  ```
  This starts the scheduler and serves the dashboard at
  `http://127.0.0.1:8765`. Override the bind address with
  `make serve HOST=0.0.0.0 PORT=9000` if needed. Tell the user to open the URL;
  the scheduler queues `agent:ready` issues but never starts a worker on its own
  — they start one with the dashboard `Run` button (or `python3 -m agent_desk
  run-next`).

- [ ] **Sanity-check (optional).** Run the test suite to confirm a healthy
  install:
  ```bash
  make test
  ```

## Notes

- Local state lives under `.agent-desk/` (SQLite + per-run transcripts), which is
  git-ignored. Nothing leaves the machine during onboarding.
- For the full Codex interaction policy and PR/closeout flow, point the user to
  `README.md` and `docs/codex-cli-protocol.md`.

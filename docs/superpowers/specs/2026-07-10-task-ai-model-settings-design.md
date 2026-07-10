# Task AI Model Settings Design

## Problem

Agent Desk currently starts every worker through `codex exec` without passing a
model or reasoning-effort override. Each run therefore inherits the operator's
global Codex defaults, which makes the dashboard unable to show what AI
configuration a task will use and unable to tune easier or harder tasks without
editing `~/.codex/config.toml`.

The operator wants Agent Desk to support GPT-5.5 and GPT-5.6-family choices,
support additional models as they become available, and keep the default
behavior aligned with the current global setup: GPT-5.5 with `xhigh` reasoning.

## Goals

- Show the AI model and reasoning effort used by each task.
- Let each workspace configure default model and reasoning effort from
  Workspace Settings.
- Default new workspaces and newly added tasks to `gpt-5.5` plus `xhigh`.
- Let individual tasks override the workspace defaults before execution or
  before a resume-style continuation.
- Pass both settings to `codex exec` and `codex exec resume`.
- Support the current local Codex model catalog while allowing future custom
  model IDs without code changes.
- Keep the implementation dependency-free and backward-compatible with existing
  run JSON and config files.

## Considered Approaches

### 1. Workspace defaults plus per-task overrides -- selected

Add workspace-level defaults to `RepoConfig` and `SchedulerSettings`, then store
the effective `ai_model` and `ai_reasoning_effort` on each run record. New tasks
inherit the workspace defaults at creation time. The dashboard shows the values
on every task card and allows editable task-level overrides for non-running
states where the next Codex invocation has not yet started.

This preserves historical run behavior, makes task settings explicit, and lets
the operator tune one task without changing the entire workspace.

### 2. Workspace-only settings

This is simpler, but changing a workspace default would ambiguously affect
already queued tasks. It would also make it awkward to run one unusually hard or
cheap task with a different model.

### 3. Task-only settings

This is flexible, but too noisy. Operators would need to choose the same model
for every new task, and the existing Workspace Settings panel would not express
the normal policy for that project.

## Model Catalog

Agent Desk will ship an internal catalog based on the current Codex host model
list:

- `gpt-5.6-sol`: GPT-5.6 Sol, supports `low`, `medium`, `high`, `xhigh`, `max`,
  and `ultra`.
- `gpt-5.6-terra`: GPT-5.6 Terra, supports `low`, `medium`, `high`, `xhigh`,
  `max`, and `ultra`.
- `gpt-5.6-luna`: GPT-5.6 Luna, supports `low`, `medium`, `high`, `xhigh`, and
  `max`.
- `gpt-5.5`: GPT-5.5, supports `low`, `medium`, `high`, and `xhigh`.
- `gpt-5.4`: GPT-5.4, supports `low`, `medium`, `high`, and `xhigh`.
- `gpt-5.4-mini`: GPT-5.4 Mini, supports `low`, `medium`, `high`, and `xhigh`.
- `gpt-5.3-codex-spark`: GPT-5.3 Codex Spark, supports `low`, `medium`,
  `high`, and `xhigh`.

The catalog is advisory for the UI. The backend accepts custom model IDs and
custom reasoning efforts so a newly available Codex model can be used before
Agent Desk is updated. Built-in choices validate reasoning effort against the
catalog and fall back to that model's default effort if the current effort is no
longer supported after a model change.

## Configuration

`RepoConfig` gains two fields:

```toml
default_ai_model = "gpt-5.5"
default_ai_reasoning_effort = "xhigh"
```

The generated example config and appended repo blocks include those keys. Older
configs omit the keys and load with the same defaults.

Workspace Settings exposes the same fields. Saving workspace settings updates
the scheduler's in-memory settings for the running service, matching the
existing settings flow. The TOML fields provide startup defaults and can be
edited directly for persistent per-repository defaults. This feature does not
retrofit persistence for the rest of the existing runtime settings panel.

## Run Data

Every run record gains:

```json
{
  "ai_model": "gpt-5.5",
  "ai_reasoning_effort": "xhigh"
}
```

Existing records are normalized when read so missing fields display and execute
as `gpt-5.5` plus `xhigh`.

New runs copy the current workspace defaults into those fields when they are
created or promoted from `available` to `ready` or `waiting_dependencies`.
Changing workspace defaults later does not rewrite existing queued runs.

## Dashboard Behavior

Workspace Settings adds two compact selects:

- Default model.
- Default reasoning.

The reasoning select updates when the model changes and shows only known
supported efforts for built-in models. For custom models, the UI allows a custom
effort text value.

Each task card displays an AI row with the effective model and reasoning effort.
The task-level controls are editable for `ready`, `waiting_dependencies`,
`interrupted`, `pr_open`, `needs_review`, `blocked`, and `failed` states. They
are read-only while a task is `running` because a process that has already
started cannot be changed in place.

For states that can launch another Codex invocation, the task-level override
applies to that next invocation:

- `ready`: initial worker run.
- `interrupted`: resume interrupted worker.
- `pr_open` and `needs_review`: request changes, approve finish, auto-finish,
  AI review, and CI fixes.
- `blocked` or `failed`: stored for visibility and for any future retry path.

## Codex Invocation

Worker startup passes both model settings:

```bash
codex -m <ai_model> -c model_reasoning_effort="<ai_reasoning_effort>" exec ...
```

Continuation startup passes the same run-level settings:

```bash
codex -m <ai_model> -c model_reasoning_effort="<ai_reasoning_effort>" exec resume ...
```

AI review startup passes the same run-level settings to its independent
`codex exec` review worker. Dependency extraction during issue intake is not a
task run and remains outside this feature.

If a run has an empty model, Agent Desk omits `-m` and lets Codex use its
global default. Built-in dashboard defaults never produce an empty value, but
this fallback keeps hand-edited JSON recoverable.

## API

Add a small run update endpoint:

```http
POST /api/run/<id>/ai-settings
```

Request:

```json
{
  "ai_model": "gpt-5.6-terra",
  "ai_reasoning_effort": "high"
}
```

The endpoint rejects updates for `running` runs and accepts updates for all
other states. For known models, it validates the reasoning effort against the
catalog and falls back to the model default when needed. For custom model IDs,
it stores trimmed model and effort values without catalog validation.

The existing `/api/state` payload includes the catalog, workspace defaults, and
per-run effective values so the static frontend does not need hidden constants
duplicated across files.

## Testing

- Config tests cover loading defaults, loading explicit values, generated
  example config text, and appended repo blocks.
- Store or scheduler tests cover new runs inheriting workspace defaults and
  existing run records normalizing missing fields.
- Worker tests assert initial `codex exec` receives `-m` and
  `-c model_reasoning_effort=...`.
- Continuation tests assert resume-style commands receive the same settings.
- Dashboard tests cover `/api/state` exposing model catalog and settings,
  `/api/settings` updating runtime workspace defaults, and
  `/api/run/<id>/ai-settings` accepting editable states while rejecting
  `running`.
- Static dashboard tests cover rendering Workspace Settings controls and task
  AI controls.
- The full stdlib `unittest` suite must remain green.

## Out of Scope

- Discovering live model catalogs at dashboard runtime.
- Editing the operator's global `~/.codex/config.toml`.
- Changing already running processes in place.
- Pricing estimates or token accounting per model.
- Separate AI-review-only model settings.
- Dependency-extraction model settings.
- Migrating old run artifacts or logs beyond read-time defaults.

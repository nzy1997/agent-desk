# Subagent-Aware Activity Watchdog

## Context

Agent Desk runs Codex with `codex exec --json` and currently treats the worker as
active only when the parent Codex process writes to stdout or stderr. This works
for ordinary command execution, but it misclassifies long subagent work.

In issue #248 for `nzy1997/Suslin.jl`, the parent worker spawned a child agent and
then entered a long `wait_agent` call. The parent `stdout.jsonl` stopped at:

```
item.started ... collab_tool_call ... wait
```

The child thread continued making progress in its own Codex thread, but none of
that activity streamed back to the parent process. Agent Desk saw 30 minutes of
parent silence and killed the run as an idle timeout. That preserved the current
watchdog contract, but it was the wrong signal for a worker that was actively
delegating.

Goal: keep subagent-driven execution available and allow it to run for long
tasks, while still killing real hangs.

## Design

The idle watchdog should consider a run active when either:

1. the parent `codex exec` process writes stdout or stderr, or
2. any known descendant Codex thread shows local activity.

Total runtime remains governed by `worker_timeout_seconds`; child activity never
extends the hard total runtime cap.

## Activity Source

Use local Codex session artifacts so Agent Desk stays dependency-free:

- Parent stdout already contains `collab_tool_call` events for `spawn_agent`.
  These events can expose child ids through `receiver_thread_ids`.
- Parent and child Codex sessions are also written under
  `$CODEX_HOME/sessions/**/rollout-*<thread_id>.jsonl`.
- A rollout file's `(mtime_ns, size)` pair is the activity marker. If either
  changes since the last poll, that thread is active.
- Known child session files are scanned for additional spawned child ids, so the
  monitor follows the descendant tree rather than only direct children.

Optional local sqlite tables such as `threads.updated_at_ms` or
`thread_spawn_edges` may be used later as an optimization, but the first
implementation should not depend on sqlite schema stability.

## Components

### `CodexThreadActivityMonitor`

A small stdlib-only helper used by `CommandRunner` when running Codex.

Inputs:

- `stdout_path`: parent Codex JSONL file.
- `codex_home`: defaults to `$CODEX_HOME`, then `~/.codex`.
- `root_thread_id`: discovered from the parent `thread.started` event.
- `poll_interval_seconds`: short enough to be responsive, e.g. 5 seconds.

Responsibilities:

- Parse appended parent stdout JSONL and discover child ids from
  `receiver_thread_ids`, `agent_id`, and equivalent nested JSON strings.
- Find rollout files for known thread ids by globbing
  `$CODEX_HOME/sessions/**/rollout-*<thread_id>.jsonl`.
- Track each known thread's `(mtime_ns, size)`.
- Recursively scan known thread rollout files for newly spawned descendants.
- Return the latest observed activity timestamp to `CommandRunner`.
- Emit concise diagnostic lines only when useful, such as newly discovered child
  ids and monitor failures.

### `CommandRunner` integration

`CommandRunner.run()` currently keeps one `last_output_at` monotonic timestamp.
It should become `last_activity_at`, updated by:

- stdout or stderr lines from the parent process;
- `CodexThreadActivityMonitor.poll()` when a known descendant rollout file
  changes.

The existing idle timeout check remains:

```
if idle_timeout is not None and now - last_activity_at >= idle_timeout:
    timeout_reason = "idle"
```

The timeout message should mention whether the latest activity was parent output
or child-thread activity, for example:

```
agent-desk: idle timeout killed process after 3188.7s (idle for 1800.0s; last activity: child thread 019f...)
```

### Activation

The monitor should activate only for Codex JSON runs:

- `codex exec --json`
- `codex exec resume --json`

Other subprocesses keep the current stdout/stderr-only idle behavior.

## Data Flow

```
Worker / ContinuationRunner
  -> CommandRunner.run(codex exec --json, stdout_path=...)
       -> parent stdout reader appends JSONL
       -> activity monitor tails parent stdout
       -> discovers child thread ids
       -> finds child rollout files under CODEX_HOME
       -> file mtime/size changes refresh last_activity_at
       -> idle kill only when parent and descendants are all quiet
```

## Failure Handling

- If no child ids are discovered, behavior is unchanged.
- If a child id is discovered but no rollout file exists yet, keep looking until
  the ordinary idle window expires.
- If rollout scanning fails because `$CODEX_HOME` is missing, unreadable, or has
  unexpected structure, record a warning and fall back to parent stdout/stderr
  activity.
- Monitor failures must not keep a run alive by themselves.
- Finished child threads do not automatically mark the parent active forever.
  Only new activity refreshes the heartbeat.

## Testing

Unit tests should cover the monitor independently and through `CommandRunner`:

- Parsing child ids from representative `spawn_agent` JSON events.
- Discovering direct child rollout files from a temporary fake Codex home.
- Discovering grandchildren by scanning known child rollout files.
- Refreshing activity when a child rollout file's size or mtime changes while
  parent stdout is quiet.
- Timing out when parent stdout is quiet and all known child files are unchanged.
- Preserving existing idle timeout behavior when no child ids are present.
- Falling back cleanly when the Codex home cannot be read.

Integration-style tests can use a fake subprocess command that sleeps while a
background thread appends to a fake child rollout file; the command should not be
idle-killed until child activity stops.

## Dashboard And Logs

No dashboard UI is required for the first implementation. The existing run log
viewer will show the timeout message and any concise monitor warnings. A later
dashboard enhancement can surface "last activity source" as run metadata.

## Out Of Scope

- Raising or removing the hard `worker_timeout_seconds` cap.
- Changing the default 30 minute idle window.
- Requiring agents to avoid subagents.
- Dependence on Codex app MCP tools or non-stdlib Python packages.
- Full child status semantics such as success, failure, or cancellation. This
  design only answers whether descendant work is still active.

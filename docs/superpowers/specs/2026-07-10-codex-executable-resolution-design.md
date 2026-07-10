# Codex Executable Resolution Design

## Problem

Agent Desk's business layers invoke Codex with the bare command name `codex`.
Detached supervisors inherit the dashboard service's environment. On this
machine, that PATH does not include the Codex binary bundled inside ChatGPT.app,
so automatic closeout fails before Codex starts even though interactive Codex
sessions can resolve the binary.

The failure affects every workflow that uses a real `CommandRunner`: issue
workers, continuation and closeout, AI review, CI fixes, and dependency
extraction.

## Selected Design

Resolve the executable centrally at the `CommandRunner` subprocess boundary.
Before `subprocess.Popen`, a literal `codex` command is replaced with an
absolute executable path. This covers every current and future business-layer
Codex invocation without duplicating path logic across Worker, Continuation,
AI Review, and Scheduler.

Resolution order is:

1. `AGENT_DESK_CODEX` environment override. It must identify an executable
   file, either directly or through PATH lookup.
2. `shutil.which("codex")` using the process PATH.
3. Known macOS ChatGPT app bundle locations:
   `/Applications/ChatGPT.app/Contents/Resources/codex` and
   `~/Applications/ChatGPT.app/Contents/Resources/codex`.

Every successful result is canonicalized to an absolute path. If resolution
fails, Agent Desk raises a targeted `FileNotFoundError` that explains how to
set `AGENT_DESK_CODEX`, instead of exposing the generic subprocess errno.

Only actual subprocess commands are rewritten. `FakeCommandRunner` remains a
test seam and continues recording the logical `codex` argv used by business
layers. Human-readable resume instructions remain portable and continue to
show `codex resume`.

## Alternatives Considered

### Modify only the dashboard service PATH

This would repair the current launch method but would remain dependent on how
the service was started and would not protect one-shot commands or other
process managers.

### Pass a Codex path through every runner constructor

This is explicit but invasive and easy to omit when new workflows are added.
All those paths already converge at `CommandRunner`.

### Install a global symlink

A symlink would fix this machine but mutate system configuration and leave the
application behavior fragile on other machines.

## Testing

- Resolver tests cover environment override, ordinary PATH lookup, macOS app
  fallback with an empty PATH, and the actionable missing-binary error.
- A real `CommandRunner` test launches a temporary executable while PATH is
  empty and verifies the returned argv contains its absolute path.
- Existing worker, continuation, AI review, scheduler, and full repository
  tests must remain green.
- A stripped-PATH smoke test must run a Codex command through `CommandRunner`
  using the installed ChatGPT.app fallback.

## Recovery of rstim #437

After the code is merged locally, restore run 181 from `failed` to `pr_open`
without clearing its PR URL, thread ID, worktree, CI result, or attempt. The
running dashboard will rediscover the clean open PR, spawn a fresh detached
auto-finish supervisor, and that new process will import the updated resolver.

Monitor until Agent Desk records `done`. Independently verify PR #447 is merged
and issue #437 is closed. If closeout returns `blocked` or `failed`, stop and
report the new evidence rather than forcing GitHub state.

## Out of Scope

- Changing Codex model or sandbox selection.
- Installing or updating Codex.
- Changing scheduler concurrency or Git setup locking.
- Automatically retrying arbitrary failed runs.

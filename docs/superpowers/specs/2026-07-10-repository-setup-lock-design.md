# Repository Setup Lock Design

## Problem

Agent Desk starts each issue in a detached supervisor process. Supervisors for
the same configured checkout can reach `git fetch origin <base>` concurrently.
Those fetches update the same remote-tracking reference, such as
`refs/remotes/origin/master`. If one fetch changes the reference after another
fetch has read its old value, Git protects the newer value and aborts with an
error such as `cannot lock ref ... is at <new> but expected <old>`.

The conflict is local to the shared checkout. It does not indicate remote
repository corruption, but today it fails the whole Agent Desk run.

## Goals

- Serialize repository preparation for runs that share the same physical local
  checkout.
- Keep Codex execution concurrent after each run has its own worktree.
- Recover automatically from a short-lived external reference-lock race.
- Preserve immediate failure for authentication, network, repository, and
  other non-lock Git errors.
- Keep Agent Desk dependency-free and retain actionable run events and logs.

## Considered Approaches

### 1. Cross-process setup lock plus targeted retry — selected

Use `fcntl.flock` on a lock file keyed by the canonical local checkout path.
Hold the lock across `git fetch` and `git worktree add`, then release it before
Codex starts. Retry only fetch failures whose stderr reports a reference-lock
race, up to three total attempts with short bounded backoff.

This directly prevents Agent Desk supervisors from racing while retaining
normal worker concurrency. The targeted retry also covers a brief collision
with a user-initiated Git operation that does not honor Agent Desk's lock.

### 2. Retry without serialization

This is smaller, but concurrent workers can repeatedly collide and produce
avoidable load. Success would depend on timing instead of preventing the race.

### 3. Fetch into a unique per-run reference

Unique references avoid the shared remote-tracking update and can pin a precise
base SHA. They also change established Git/worktree behavior and introduce
reference cleanup requirements. That is unnecessary for this focused fix.

## Design

Create a small repository-setup locking helper that:

1. Canonicalizes `RepoConfig.local_path` with `Path.resolve()`.
2. Hashes that canonical path into a stable lock-file name under
   `<data_dir>/locks/repository-setup/`.
3. Opens the lock file and takes an exclusive `fcntl.flock` for the duration of
   the context manager.
4. Releases the lock automatically on normal return, exception, or process
   exit. On platforms without `fcntl`, it retains the existing best-effort
   behavior rather than adding a runtime dependency.

`Worker.run_issue()` will enter this context before fetch and remain inside it
through successful worktree creation. It will emit a waiting event before lock
acquisition and continue using the existing fetch/worktree events after the
lock is acquired.

Fetch retry behavior will be narrow:

- At most three total fetch attempts.
- Retry only when stderr contains a reference update race, identified by
  `cannot lock ref` together with either `is at ... but expected ...` or
  `unable to update local ref`.
- Record each retry as a warning event with attempt information and the tail of
  stderr.
- Apply short bounded delays between attempts.
- After the final retry, use the existing `git fetch failed` terminal path.
- Do not retry unrelated failures.

## Testing

- A real subprocess-based test will prove two processes using the same checkout
  key cannot occupy the repository-setup critical section simultaneously.
- A worker test will reproduce the observed lock error, then return a successful
  fetch and verify the run proceeds through worktree creation and Codex.
- A worker test will verify an unrelated fetch error is attempted once and
  still fails immediately.
- Existing worker tests and the full stdlib `unittest` suite must remain green.

## Out of Scope

- Changing configured concurrency limits.
- Serializing Codex execution.
- Replacing remote-tracking branches with per-run Git references.
- Retrying arbitrary Git, network, authentication, or worktree errors.
- Adding third-party locking or retry dependencies.

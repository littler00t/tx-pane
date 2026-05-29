---
name: tx-pane-run
description: >-
  Execute a shell-oriented task through a tx-pane session following tx-pane's
  documented best practices — named persistent pane, run/exec/stream selection,
  real exit codes, refuse-on-busy resolution, compaction with handle recovery,
  and safe handling of secrets and confirm-gated commands. Use when the user
  asks to run, build, deploy, test, or operate something via tx-pane, or hands
  you a terminal task to carry out.
argument-hint: "<task description>"
allowed-tools:
  - Bash(./tx-pane *)
  - Bash(tx-pane *)
---

# Execute a task via tx-pane

**Task:** $ARGUMENTS

Carry this out through a `tx-pane` session using the best practices below. The
full guidance is in `docs/tx-doc-agent-playbook.md` and the decision table in
`CLAUDE.md` — read them when the task is non-trivial.

Resolve the command once: prefer `tx-pane` on `PATH`, else `./tx-pane` from the
repo root. **If the task is ambiguous or destructive, ask before executing.**

## 1. Plan the pane(s)

- **Do you even need a pane?** Skip tx-pane for a single trivial one-shot where
  you don't need the exit code. Use it when the command is long-running,
  interactive, stateful, or produces large output.
- **One pane per coherent purpose**, named `<role>-<purpose>` (e.g.
  `build-service`). **Capture the id once and reuse it** — never spawn a fresh
  pane per command:
  ```sh
  pane=$(tx-pane new build-service --cwd <dir>)
  ```

## 2. Pick the right verb

| Situation | Use |
|---|---|
| Wait for output, bounded | `tx-pane run` (default) |
| Bail out early on a pattern | `tx-pane run --wait-for <re>` / `--fail-for <re>` |
| Long-running / server / log-follow | `tx-pane exec` then `tx-pane wait` / `wait-run` / `tail` |
| N seconds of a `-f` stream | `tx-pane stream <cmd> --duration 10s` / `--until <re>` |
| Only the exit code | `tx-pane run --json … \| jq -r .exit` |

Never `tx-pane run` a non-terminating command (`tail -f`, `npm run dev`) — it
will hit `--timeout`. Use `exec` + `wait`.

## 3. Trust exit codes, but verify

- Every run emits `[exit:N]` (or `.exit` in `--json`). **`tx-pane run` itself
  exits 0 even when the wrapped command failed** — read the marker, not
  tx-pane's own status.
- `[exit:?] (hook missing)` after `ssh` / `sudo -i` / `docker exec` → run
  `tx-pane hook-install "$pane"` once, then continue.
- Don't background with `&` for parallelism (the marker reports the
  backgrounding op, not the job) — use `tx-pane exec`.

## 4. Resolve "pane busy" (there is no `--force`)

- Need the prior output first → `tx-pane wait-run "$pane" <run-id>`, then retry.
- New command depends on the current one finishing → `tx-pane run --queue`.
- Current run is junk → `tx-pane run --kill-and-run`.
- Pane is at a Y/n or password prompt → `tx-pane run --stdin` (or `send`/`key`).

## 5. Read output deliberately

- Repeated reads of a running command → `tx-pane tail` (advances the offset).
- "What's on screen now" without moving the pointer → `tx-pane dump --tail N`.
- Errors only → `tx-pane grep "$pane" '(?i)error|fail' -C 2`.
- A specific run's output → `tx-pane output "$pane" <run-id>` / `--last`.

## 6. Compaction

- Trust compaction on the first call; **don't default to `--raw`** (2–3× tokens).
- Need elided content → `tx-pane output "$pane" --handle h-XXX --grep PAT`
  *first* — cheaper than re-running.
- Suspect a normalizer hid a column you need → re-run that one command with
  `--no-normalize`.
- A `tier=passthrough` / `degraded` footer means "not understood" — treat those
  bytes as raw and read them carefully.

## 7. Safety

- Passwords/secrets → `tx-pane send-secret` (stdin only; never argv or log).
  Never put a secret in `tx-pane send` / `run`.
- A command matching `confirm_patterns` with no TTY needs `--yes` — but **pause
  and confirm genuinely destructive actions with the user first**.
- Never `tmux kill-session`. Free a pane with `tx-pane kill`, or just leave it.

## 8. Report

When done, state the outcome, the real exit code(s), and where to find detail
(`tx-pane runs "$pane"`, `tx-pane output "$pane" --last`). Leave the pane up for
follow-up unless the user wants it killed.

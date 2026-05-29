# tx — agent playbook

This document is **for LLM agents driving `tx`**. It is decision
guidance, not a feature reference. When the top-level decision table in
`CLAUDE.md` doesn't tell you what to do, this is the next doc to pull.

It is organized as **questions you'll actually ask yourself** at the
moment you're about to call `tx`.

Cross-references:
- Flag/option reference: [`tx-doc-reference.md`](tx-doc-reference.md)
- Compaction internals + handle protocol: [`tx-doc-compaction.md`](tx-doc-compaction.md)
- Nested shells, secrets, deployments, safety rails: [`tx-doc-advanced.md`](tx-doc-advanced.md)

---

## Pre-flight: do I even need a pane?

You don't need `tx` for every command. Defer to your task tool's plain
shell when:

- Result fits in one line and you don't need the exit code (`hostname`).
- The whole task is one round-trip and you'll never touch the same
  shell state again.
- You need <50ms latency per call in a tight loop.

You do need `tx` (i.e. `tx new` + `tx run`) when **any** of:

- The command might run for more than a couple of seconds.
- You need a real exit code, not an output-shaped guess.
- The command is interactive (`sudo`, `ssh`, `gh auth login`).
- You'll run more than one command and they share state (`cwd`, env,
  background process, virtualenv activation).
- The output is potentially large (`kubectl`, `journalctl`, `find /`,
  `apt-get install`, `tar -tvf`).

If you create a pane, **capture the id in a shell variable on the very
first call** and reuse it. Spawning a fresh pane per call is the most
common waste pattern.

---

## Choosing run vs exec vs stream

| Situation | Use | Why |
|---|---|---|
| You want output back inline, and you'll wait | `tx run` | The default. Bounded by `--timeout`. |
| You want output back inline, but might bail early | `tx run --wait-for <re>` / `--fail-for <re>` | Returns as soon as pattern hits. Exits 0 on `--wait-for`, 1 on `--fail-for`. |
| The command runs essentially forever (server, log follow) | `tx exec` then later `tx tail` or `tx wait` | `exec` returns a run-id immediately. |
| You want N seconds of a `tail -f`-style stream | `tx stream <cmd> --duration 10s` | Auto-`C-c`s at the bound. |
| You want output but plan to do other work first | `tx exec` + `tx wait-run` later | Lets you parallelize. |
| You don't want output, only an exit code | `tx run --json … | jq -r .exit` | Skip parsing stdout. |

**Common mistake:** using `tx run` on `tail -f` or `npm run dev`. The
command will not return; you'll hit `--timeout` and not know whether
the server actually started. Use `tx exec` + `tx wait` for "start a
server and wait until it's listening":

```
tx exec  "$pane" "npm run dev"
tx wait  "$pane" "listening on" --timeout 30
```

---

## Resolving "pane busy"

If `tx run` or `tx exec` errors with state `running` / `tui` /
`waiting-input`, you must pick a resolution. There is no `--force`.

Decision tree:

```
Do I need the current command's output first?
├─ Yes → wait for it: tx wait-run <pane> <prior-run-id>
│        then retry the new command.
└─ No → does the new command depend on the current one finishing?
        ├─ Yes (e.g. "run after build done") → tx run --queue
        ├─ No, and the current command is unwanted → tx run --kill-and-run
        └─ No, current command is *prompting for input* → tx run --stdin
                                                  (or tx send / tx key)
```

`--queue` blocks up to `--max-wait` (default 600s). If you don't want
to block on it forever, set `--max-wait` to something sane and handle
the timeout.

`--kill-and-run` sends `C-c`, waits for the marker, then runs your
command. The killed run's exit code is preserved (usually 130).

`--stdin` is for when the pane is at a "Y/n" or password prompt
(state=`waiting-input`). It sends your text + Enter without checking
busy. **Do not** use `--stdin` to ship sensitive bytes — use
`tx send-secret`.

---

## Exit codes: trust, but verify

The contract: every `tx run` whose pane has a working marker hook
emits `[exit:N]` at the end. With `--json`, it's the `exit` field.

**The marker can be missing.** Symptoms:

```
[exit:?] (hook missing)
```

Causes (in order of likelihood):

1. You're in a nested shell (you ran `ssh`, `sudo -i`, `docker exec`).
   Run `tx hook-install <pane>` once. Subsequent runs will mark correctly.
2. The user manually unset `PROMPT_COMMAND` / overrode `precmd`. Run
   `tx hook-install <pane>` to reinstall.
3. Pane crashed / shell exited. `tx status <pane>` will say `dead`.
   Run `tx restart <pane>`.
4. The command itself replaced the shell (`exec bash`, `exec python`).
   Reinstall the hook in the new shell with `tx hook-install`.

**Backgrounded commands (`cmd &`).** The hook fires when the shell
returns to the prompt, which is immediately — exit code 0. The
backgrounded process may still be running, may crash later, may
succeed. *The exit code you got is the exit code of the
shell-builtin-that-backgrounded-the-job, not the job itself.* For
"start a server and wait until ready" patterns, **don't** background
with `&`; use `tx exec`. For "kick off and check later", `tx exec`
gives you a real run-id you can later poll with `tx wait-run`.

**`tx run` itself exits 0** even when the wrapped command exited
non-zero. Read `[exit:N]` from the body or `.exit` from `--json`.
This is intentional: `tx run`'s own success means "I delivered the
command and observed it complete", which is independent of the
command's success.

---

## Reading output: which command?

| Goal | Command | Notes |
|---|---|---|
| New bytes since last read; I'll read repeatedly | `tx tail <pane>` | Advances `tail_offset`. The most common reader. |
| Look at the last N lines, don't change my read position | `tx dump <pane> --tail N` | Idempotent. Use for "where am I" checks. |
| Look at the first N lines (head) | `tx dump <pane> --head N` | Also idempotent. |
| Drain all unread bytes one-shot | `tx tail <pane> --all` | Use before pane handoff. |
| Pick out errors only | `tx grep <pane> '(?i)error\|fail' -C 2` | Context lines around matches. |
| Output of one specific run | `tx output <pane> <run-id>` | Bounded to that run's span. |
| Output of the previous run | `tx output <pane> --last` | |
| Output of everything since a known run | `tx output <pane> --since-run r-XXX` | |
| Recover content L4 elided | `tx output <pane> --handle h-XXX --range/--grep/--full` | See compaction doc. |

`tail` and `dump` differ in **whether they advance the read pointer**.
This matters when you have an exec'd long-running command and you're
periodically reading its output: use `tail`. When you want to inspect
the same window twice (e.g. "what's on screen right now"), use `dump`.

---

## Compaction: when to override the default

`tx` defaults to compaction-on. **Trust it on the first call.** Then:

1. If you see an `h-XXXX` handle and you need the elided content, use
   `tx output --handle h-XXX --grep PAT` *first*. It's faster and
   cheaper than re-running.
2. If you suspect the *per-tool* normalizer dropped a column you need
   (rare — check the `[tx:compact ...]` footer for the `layers=` list
   to see which fired), re-run that **single** command with
   `--no-normalize`.
3. If the output is genuinely an opaque blob (binary, custom format,
   tool the normalizer registry doesn't know), use `--raw` for that
   one call.

**Anti-pattern: defaulting to `--raw`.** Every `--raw` call costs ~2-3x
more tokens for the same information. The agent's effective context is
much larger with compaction on, and elision is always reversible. Use
`--raw` only when you've identified a specific missing-content
problem.

**Anti-pattern: setting `--token-budget` to a tiny number "to be
safe".** Below ~1000, L4 truncation will start eliding content that
later turns out to be necessary, forcing a recover-via-handle round
trip. The default (4000) is calibrated; lower it only for known
verbose tools.

**When to read the footer carefully:**

```
[tx:compact tier=passthrough layers=L1 in=8420B out=8401B saved=0%]
```

A `tier=passthrough` or `tier=degraded` footer means the normalizer
declined to make a happy-path summary — usually because the tool
returned an error or unexpected output shape. **Treat passthrough
output as raw**: the compaction layers ran but didn't find a known
shape; the bytes are essentially what the tool produced. Read them
carefully rather than assuming "tx compacted this, so it's small for a
reason".

---

## Working with logs that don't end

`journalctl -f`, `tail -f`, `kubectl logs -f`, `docker logs -f`:

| Pattern | Command |
|---|---|
| "I want N seconds of this then stop" | `tx stream <pane> "<cmd> -f" --duration 10s` |
| "I want until I see X" | `tx stream <pane> "<cmd> -f" --until "X"` |
| "I want it running while I do other things" | `tx exec <pane> "<cmd> -f"` |
| "I started it with exec and want to check on it" | `tx tail <pane>` (advances offset) |
| "I want to stop the streaming run" | `tx kill-run <pane> <run-id>` |

`tx stream` always returns. `tx exec` of a non-terminating command
returns a run-id and runs until you kill it; the pane state stays
`running` indefinitely.

---

## Sensitive data

Three options, by sensitivity:

| Sensitivity | Tool | Where bytes land |
|---|---|---|
| Public input | `tx run` / `tx send` | argv (visible to `ps`), stdin, log |
| Should not be in `ps` | `tx send` reads no argv beyond `<text>` | argv, stdin, log |
| Must never hit disk | `tx send-secret` (stdin only, `--enter` to add newline) | stdin only; log gets `[redacted: send-secret N bytes]` placeholder |
| Must redact in stdout | configure `redact_patterns` in `[security]` | agent-facing output rewritten; on-disk log keeps bytes |

For passwords, `tx send-secret` is the only correct answer. For
output redaction (an API response containing a token you don't want
in your context window), set `redact_patterns` per-pane.

---

## When the pane is "stuck"

Symptoms: `tx run` times out, `tx status` says `running` for an
expected-short command, you got no output.

Triage order:

1. `tx info <pane>` — multi-line state, recent runs, hook status.
2. `tx dump <pane> --tail 80` — what's actually on the screen?
3. `tx grep <pane> '(?i)error|exception|panic' -C 3 --max 50` — is
   there a visible error?
4. `tx runs <pane> --limit 5` — what does run history say?

Then decide:

- **TUI app** (`vim`, `htop`, `less`): `tx key <pane> q` or `C-c`,
  then `tx run --kill-and-run` to free the pane.
- **Real hang** (no output for the timeout window, no obvious TUI):
  `tx kill-run <pane> <run-id>`.
- **Waiting for input** (`tx status` says `waiting-input`):
  `tx run --stdin <pane> "<answer>"`.
- **Dead pane** (`tx status` says `dead`): `tx restart <pane>`.

Never `tmux kill-session`. That kills *every* pane the user is using;
you've thrown away their context.

---

## Multi-step workflows: keep one pane

A common bad pattern is "spawn a fresh pane for each step". State
leaks (cwd, env, activated virtualenvs, exported credentials).
Prefer:

```
pane=$(tx new build --cwd ./service)
tx run "$pane" "python -m venv .venv && . .venv/bin/activate"
tx run "$pane" "pip install -r requirements.txt"
tx run "$pane" "pytest -x"
tx run --json "$pane" "docker build -t service:dev ."
```

vs the wrong way:

```
tx run "$(tx new)" "python -m venv .venv && . .venv/bin/activate"   # venv lost
tx run "$(tx new)" "pip install ..."                                # no venv
tx run "$(tx new)" "pytest"                                         # broken
```

The pane-id naming convention is `<role>-<purpose>` (e.g.
`build-service`, `db-primary`, `web-staging`). Stable names make
`tx ls` and the audit log readable.

---

## Audit / forensics: what's on disk

After any session you can reconstruct exactly what happened:

```
~/.tx/logs/<pane>.log         # raw stream, markers visible
~/.tx/offsets.json            # per-pane run history, bookmarks, handles
~/.tx/compact.jsonl           # what compaction did (cmd_head only)
```

For an after-the-fact "what did the agent run on pane X":

```
tx runs <pane> --limit 100 --json | jq -c '.[] | {run_id,cmd,exit,duration_ms}'
```

For "what did the agent see (after compaction) for run r-XYZ":

```
tx output <pane> r-XYZ
```

For "what the *pane* actually emitted, byte-for-byte":

```
tx output <pane> r-XYZ --full
```

The `--full` view is the auditable ground truth. Use it for incidents
and disputes; otherwise the compacted form is what the agent acted on.

---

## Worked examples

### A. "Verify nginx is healthy on three hosts"

```
hosts="web-01 web-02 web-03"
for h in $hosts; do
  p=$(tx new "$h")
  tx run "$p" "ssh deploy@$h" --wait-for '\$ '
  tx hook-install "$p"
done
for h in $hosts; do
  res=$(tx run --terse --json "$h" "systemctl is-active nginx")
  echo "$h: $(echo "$res" | jq -r .stdout)"
done
```

Why this works: one pane per host, each pane stays connected over the
loop, exit codes are real (via `tx hook-install` after ssh).

### B. "Capture the boot of a containerized service, then run smoke tests"

```
p=$(tx new svc --cwd ./service)
tx exec "$p" "docker compose up"
tx wait "$p" "Started SvcApplication" --timeout 60
tx run "$p" --queue "docker compose exec svc curl -fsS localhost:8080/health"
```

Why: `exec` for the long-running `up`; `wait` for the readiness line;
`--queue` so the smoke test waits for `up` to settle (it won't actually
wait because `up` is foreground; in `-d` mode this would matter).

### C. "Triage a journal explosion without dumping 80KB into context"

```
p=$(tx new triage)
tx run --terse "$p" "systemctl status app"   # 1-line if healthy, full block if failed
tx run --token-budget 6000 "$p" "journalctl -u app -n 5000"   # head+tail+handle
# If the elided middle is interesting:
tx output "$p" --handle h-XXXX --grep "Connection refused" --grep-context 5
```

Why: `--terse` collapses healthy `systemctl status`; budget-bounded
`journalctl` returns head+tail; handle lets you laser into the middle
without re-running.

### D. "I asked the agent to run a long deploy and it crashed mid-run"

```
# After resuming the conversation:
p=existing-pane-name        # the agent recorded this earlier
tx info "$p"                # is the pane alive? what state?
tx runs "$p" --limit 10     # what ran, how did it end
tx output "$p" --last       # what was the last command's output
# If pane is still running the deploy:
tx tail "$p"                # any new output since the crash
# If pane went dead while we were gone:
tx restart "$p"             # revive it, log preserved
```

This is the recovery story for an agent crash mid-session: the pane
outlives the agent process; `tx info` / `tx runs` reconstructs state.

---

## Anti-patterns (do not do these)

1. **Spawning a pane per command.** Wasteful and loses state. One pane
   per coherent purpose.
2. **Defaulting to `--raw`.** Costs tokens. The handle protocol makes
   elision reversible.
3. **`tx run` on `tail -f`.** Will time out. Use `tx stream` or
   `tx exec`.
4. **Backgrounding with `cmd &` to get parallelism.** The marker
   reports exit 0 for the *background-the-job* operation, not the job.
   Use `tx exec` for real parallelism.
5. **Sending passwords via `tx send` or `tx run`.** They land in argv
   and the on-disk log. Use `tx send-secret`.
6. **Killing the tmux session directly.** Always go through `tx kill`
   (one pane) or just leave panes alone — they're cheap.
7. **Ignoring `tier=passthrough` footers.** That's the "I didn't
   understand this output" signal. The bytes are roughly raw; treat
   them with the same care as `--raw`.
8. **Setting `--max` very low to "be safe".** Truncates at the
   transport boundary *without* a recovery handle. Prefer
   `--token-budget` (L4 truncation with handle) over `--max`
   (post-compaction line cap).
9. **Forgetting `tx hook-install` after `ssh`/`sudo -i`/`docker exec`.**
   Symptom: `[exit:?]`. The fix is one command and the nested shell
   keeps working until you exit it.

---

## Quick reference: which flag for which problem

| Problem | Flag |
|---|---|
| Output is too big | `--token-budget N` (default 4000) |
| Output mentions a column the normalizer hid | `tx output --handle h-X --range a-b` |
| Need bytes verbatim | `--raw` on that one call |
| Need ANSI colors (rare; for replay) | `--keep-ansi` |
| Command pattern matches `confirm_patterns` and there's no TTY | `--yes` |
| Pane is busy with a junk run | `--kill-and-run` |
| Pane is busy with a run I want to keep | `--queue --max-wait N` |
| Pane is busy at a prompt | `--stdin` |
| Want to fail fast on a known-bad pattern | `--fail-for '<regex>'` |
| Want to return as soon as something happens | `--wait-for '<regex>'` |
| Want structured output for downstream parsing | `--json` |
| Want to bound a streaming log read | `tx stream --duration 5s` or `--lines 200` or `--until <re>` |

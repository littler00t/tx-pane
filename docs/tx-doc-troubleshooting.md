# tx-pane — troubleshooting and FAQ

This doc is the **first place to look when something doesn't work the
way you expected.** It's organized by symptom — the thing you actually
saw — rather than by feature.

Cross-references:
- Reading errors from `tx-pane`: see "Error message reference" below.
- Agent-side decision guidance (anti-patterns, when-to-use-which-flag):
  [`tx-doc-agent-playbook.md`](tx-doc-agent-playbook.md).
- Compaction internals: [`tx-doc-compaction.md`](tx-doc-compaction.md).

---

## Symptom: `[exit:?]` or "hook missing" in agent-facing output

**What it means:** The marker line `\001TX_END <run-id> <code>\001` did
not show up in the on-disk log within the timeout window for that run.
The output is what the pane emitted; `tx-pane` just couldn't confirm the
command completed.

**Most likely causes**, in order:

1. **Nested shell** — you just ran `ssh somehost`, `sudo -i`, `docker
   exec -it … bash`, or similar. The outer shell's `PROMPT_COMMAND` is
   not active in the nested shell. Fix:

   ```
   tx-pane hook-install <pane>
   ```

   This is safe to run repeatedly. The outer shell's hook is
   untouched; when you eventually `exit` the nested shell, marker
   tracking on the outer shell resumes automatically.

2. **The command replaced the shell** — `exec bash`, `exec python -i`,
   `exec /bin/zsh`. The hook lived in the old shell's environment; the
   new shell has no `PROMPT_COMMAND`. Fix: same as nested shell.

3. **The user manually unset the hook** — they ran `unset
   PROMPT_COMMAND` or sourced a script that overwrote it. Fix: same.

4. **Pane is dead** — the shell exited. `tx-pane status <pane>` will say
   `dead`. Fix: `tx-pane restart <pane>` (preserves the log).

5. **Marker stripped by an over-eager filter** — unlikely; reproduce
   with `tx-pane run --raw <pane> <cmd>` and grep the log for `TX_END`. If
   the marker is in the log but `tx-pane` didn't see it, that's a bug
   report.

**Avoid** `tx-pane run --no-marker-check`. There is no such flag, and you
shouldn't want one — exit code accuracy is the whole point.

---

## Symptom: "pane is busy" / refuse-on-busy error

**What you saw:**

```
error: pane <name> is busy (state=running, run=r-91ab, since 12.4s)
       resolve with --queue, --kill-and-run, or --stdin
```

**What `tx-pane` knows:** there is a tracked run on this pane that has not
completed. Sending another command on top would interleave output and
break the marker protocol.

**Decision:** which resolution flag?

- `--queue` — your new command depends on the old one being done
  (e.g. "run smoke test after build"). Set `--max-wait` if you don't
  want to block forever.
- `--kill-and-run` — the old run is junk (a forgotten `tail -f`, an
  unwanted REPL, a hung command). Sends `C-c` then runs your command.
- `--stdin` — the pane is at a prompt expecting input
  (`state=waiting-input`). Send your input as text.

**Anti-pattern**: spawning a new pane to "get around" the busy one.
You'll accumulate dozens of half-used panes and lose the context the
busy pane was building up.

If the busy pane *should* be free (you know the command finished but
`tx-pane` thinks it's still running), see the `[exit:?]` symptom above —
the marker probably didn't reach `tx-pane`.

---

## Symptom: `tx-pane run` times out, but the command was supposed to be quick

**Decision tree:**

1. **Is the pane producing output?** `tx-pane dump <pane> --tail 40`.
   - If yes, the command is just slower than `--timeout` (default
     varies; see `tx-pane config`). Bump `--timeout` or use `--wait-for` /
     `--fail-for` to exit on a pattern.
   - If no, something's wrong; continue.

2. **Is the shell prompt visible?** `tx-pane dump <pane> --tail 10` — look
   for the user's `PS1`. If the prompt is there but no marker line:
   the hook is missing or broken. Run `tx-pane hook-install <pane>`.

3. **Is there a TUI on screen?** Symptoms: ANSI escapes,
   `state=tui` in `tx-pane status`. The command opened `less`, `vim`,
   `htop`, etc. Send `q` or `C-c`: `tx-pane key <pane> q` or
   `tx-pane kill-run <pane> <run-id>`.

4. **Is the pane dead?** `tx-pane status` says `dead`. The shell exited
   (often: the command was `exit`, or a SIGHUP killed it).
   `tx-pane restart <pane>`.

5. **None of the above** — the command is genuinely stuck. Capture
   diagnostics, then `tx-pane kill-run`:

   ```
   tx-pane info <pane>                                  > diag.txt
   tx-pane dump <pane> --tail 200 >> diag.txt
   tx-pane kill-run <pane> <run-id>
   ```

---

## Symptom: agent burned 30K tokens reading `kubectl get pods`

`tx-pane` should have compacted that. Reasons it might not have:

1. **The config explicitly sets `raw`.** Check `tx-pane config | grep compact` — if
   `default_mode = "raw"`, remove that override or set
   `[compact] default_mode = "terse"` in `~/.tx-pane/config.toml`.
2. **The agent passed `--raw`.** Look at the actual command. The
   `--raw` flag wins over global config.
3. **The command was piped or redirected** — `kubectl get pods |
   grep something`. Pipeline rejection (`| ; & > <`) is by design:
   the user has already chosen a representation. Either accept the
   token cost, or pre-filter at the kubectl level (`-o yaml | yq …`)
   inside a script.
4. **The normalizer doesn't exist for that tool yet.** Check
   `tx-pane compact-stats --passthrough` for the top "no normalizer
   matched" cmd_heads. Add one — see "Authoring a new normalizer" in
   [`tx-doc-compaction.md`](tx-doc-compaction.md).
5. **L4 budget wasn't exceeded.** Default `--token-budget` is 4000.
   If the output was 3.5K, no truncation fires. That's the right
   behavior.

To force compaction on a single call: `tx-pane run --terse <pane> <cmd>`.

---

## Symptom: handle reports "handle expired; use --full"

**What it means:** The run record that owned `h-XXXX` has rotated out
of `~/.tx-pane/offsets.json` (default: 100 run records kept per pane).
The elided bytes are still in the pane log, but the handle pointer is
gone.

**Fix:** use the run-id directly with `--full`:

```
tx-pane output <pane> <run-id> --full
```

If you don't have the run-id, grep the on-disk log:

```
grep -n TX_END "$(tx-pane log-path <pane>)" | tail -20
```

Each marker line includes the run-id. Find the one around the time you
need, then `tx-pane output <pane> <run-id> --full`.

**Preventive:** bump `max_run_history` under `[panes]` in
`~/.tx-pane/config.toml` if you do a lot of long-after-the-fact forensics.

---

## Symptom: command worked but agent says it didn't (or vice versa)

This is almost always one of:

1. **The command was backgrounded with `&`** — exit code reported is
   for the shell's "yes I backgrounded that" return (always 0), not
   the actual job. The job may still be running, may have crashed.
   Use `tx-pane exec` for explicit async with real exit codes.

2. **Command was a TUI** — `vim file.txt`, `htop`, etc. The marker
   fires when the *shell* returns to a prompt, not when the TUI
   "succeeded" (whatever that means for an editor). For TUIs use
   `tx-pane key <pane> q` to exit deliberately.

3. **Multi-statement command** — `cd /etc && grep foo bar`. The marker
   only sees the final statement's exit. For accurate per-statement
   tracking, run them as separate `tx-pane run` calls, or wrap in
   `bash -c '...'` if you need atomicity.

4. **stderr was the interesting part** — `tx-pane run` shows stdout+stderr
   combined (it's a tmux pane; there's no separation). If your agent
   is parsing stdout only from `--json`, it might miss stderr-only
   errors. Use `--json` and read `stdout` (which includes both
   streams) for full output.

---

## Symptom: `tx-pane new` hangs or "shell not ready"

**What it means:** `tx-pane new` waits for the hook to be confirmed loaded
in the new pane's shell before returning. If your shell rc files are
slow (e.g. they hit the network), `tx-pane new` may time out.

Fix:

```
tx-pane new <name> --hook-timeout 30     # bump the default
tx-pane new <name> --no-verify           # skip hook verification (use only for known shells)
```

If your rc files do something genuinely slow on every shell open
(e.g. cloud-init, oh-my-zsh updates, network fetches), consider
gating them on `[ -z "$TX_PANE" ] && …` so `tx-pane`-created panes skip
the slow path. The env var `TX_PANE` is exported by `tx-pane new`.

---

## Symptom: `tx-pane write` refuses with "fish not supported"

`tx-pane write` uses bash/zsh heredoc syntax for the atomic staging step.
Fish doesn't have the equivalent. Workarounds:

1. **Create a bash subshell pane for the deploy** —
   `tx-pane new deploy --shell bash`, use that pane for `tx-pane write`, keep
   your interactive fish pane separate.
2. **Use a different deploy mechanism** — `scp`, `rsync`, `ansible
   copy` over the pane. You lose the marker-tracked staging audit
   trail but you can move forward.

A fish-compatible deploy path using `set -e` and explicit `mv` is on the
roadmap, but it's not in v1.5.

---

## Symptom: allowlist refuses a command you expect to run

Two things to check:

1. **Bare entries and regex entries have different semantics.** A bare
   entry like `"systemctl"` matches only the first command token. A
   regex must be wrapped as `"/^systemctl status/"` and is matched
   against the full submitted command after leading whitespace is
   trimmed.

2. **Per-pane lists AND-merge with the global.** If the global is
   `["/^systemctl/"]` and the pane allow is `["/^systemctl status/"]`,
   only commands matching *both* will run. This is intentional —
   per-pane lists can only further restrict, never loosen.

To diagnose:

```
tx-pane config --explain "<your command>"
```

prints which allowlist entries matched / didn't match.

---

## Symptom: confirmed pattern keeps prompting on each run

`confirm_patterns` matches against every `tx-pane run`/`tx-pane exec`/`tx-pane stream`/
`tx-pane sudo` command and asks the local user (the *human*) to acknowledge
via the TTY. If you're running in CI or other non-interactive contexts:

- `--yes` on the call acknowledges that one command.
- `[security] confirm_mode = "allow"` globally bypasses confirm
  (auditing only). Don't do this if you have human-supervised flows.
- `confirm_mode = "deny"` will reject without prompting — useful for
  CI that should never run dangerous commands.

---

## Symptom: pane state shows `tui` and `tx-pane run` won't go

`state=tui` means the shell hook hasn't fired and there's been recent
output suggesting a TUI app. `tx-pane run` refuses to send a new command
because typing into `vim`'s normal-mode buffer doesn't do what you
want.

Resolution:

- `tx-pane key <pane> q` — send a `q` (works for `less`, `htop`, `man`).
- `tx-pane key <pane> Escape :q Enter` — vim.
- `tx-pane kill-run <pane> <run-id>` — send `C-c` (works for most TUI
  apps that allow interrupt).
- `tx-pane run --kill-and-run <pane> <new-cmd>` — sends `C-c` then runs.

If `tx-pane` is mis-detecting TUI (e.g. it sees a colored progress bar and
calls it TUI but actually it's just a CLI with ANSI), file a bug —
the state-machine heuristics live in `tx_state.py` and are tunable.

---

## Symptom: log file is growing huge

`tx-pane maintain` handles rotation, but doesn't run automatically. Add it
to a cron, or run it manually:

```
tx-pane maintain --dry-run     # show what would be rotated
tx-pane maintain
```

Defaults: rotate when a log exceeds `max_size_mb` (50), keep
`max_keep` (3) rotated copies, sweep panes idle longer than
`idle_kill_days` (7).

`tx-pane maintain --force` ignores the per-pane "recently active" check.

---

## Symptom: `tx-pane grep` returns nothing but the pattern is visibly in the log

Two common gotchas:

1. **You used `tx-pane grep <pane> "ERROR"` but the line has `Error:`.** The
   regex is case-sensitive by default. Use an inline regex flag such as
   `(?i)ERROR` when you need case-insensitive matching.
2. **ANSI escape codes are between the characters of your pattern.**
   `tx-pane grep` searches the compacted form (ANSI-stripped) by default,
   but if you passed `--keep-ansi`, the bytes are interleaved with
   escape sequences. Drop `--keep-ansi` for grep.

---

## Symptom: bytes in `tx-pane output --json --raw` don't match `tx-pane log` output

By design. `tx-pane output` (without `--full`) is *agent-facing* output:
ANSI stripped, markers stripped, redaction applied, optionally
compacted. `tx-pane log` is the raw pipe-pane log: every byte the pane
emitted, including markers, including any redacted bytes (the log is
not rewritten — see `[security]` notes).

For audit: prefer `tx-pane log` (or open the file directly at
`~/.tx-pane/logs/<pane>.log`). For agent-faithful replay: `tx-pane output
<run-id> --json` will show exactly what the agent saw.

---

## FAQ

**Q: Why no daemon?**
A: A daemon would solve concurrent writes to `offsets.json`. `fcntl.flock`
also solves it, with no socket / IPC / supervision cost. The daemon
(`txd`) was explicitly evaluated during design and decided against — see
the "Why no daemon" section in the README.

**Q: Why Python and not Go?**
A: PEP-723 inline-deps + `uv` cold start is ~40ms on a recent machine.
The one true binary that Go would give you isn't worth losing the
single-file scriptable distribution. If startup ever becomes the
bottleneck (rare), a Go rewrite is on the table.

**Q: Why tmux and not raw PTY?**
A: Tmux gives you `pipe-pane` (log capture for free), session
persistence, human-attach, and ANSI/state correctness across reflows.
Reimplementing those on a raw PTY is the start of writing a tmux.

**Q: Can I use `tx-pane` over SSH (driving panes on a remote machine)?**
A: Indirectly. You run `tx-pane` *on* the machine; you attach to the tmux
session over SSH if you want to watch. Driving a remote `tx-pane` from a
local agent is a "wrap it in a thin RPC" exercise that isn't built in.

**Q: Does `tx-pane` support `nu`, `xonsh`, `elvish`, `oil`?**
A: Not yet. The marker hook needs a documented "run this on every
prompt return" extension point in the shell. `nu` has `config.nu`'s
`hooks.pre_prompt` and might be tractable. Patches welcome.

**Q: Is the compaction lossless?**
A: For L1-L3 (banners, whitespace, identical-line collapse), yes —
the per-tool normalizers' happy-path collapses (e.g. "active
(running)") are lossy by definition, but the original bytes are
recoverable from the on-disk log via `tx-pane output --full`. L4 truncation
is lossy in the *response*, recoverable via the handle.

**Q: Can the agent disable `tx-pane`'s safety rails?**
A: No. `[security]` is read from `~/.tx-pane/config.toml` at process
startup; commands that try to mutate it (e.g. `sed` on the file)
need to themselves pass the allowlist, which by default does not
include arbitrary writes to `~/.tx-pane`. If you genuinely need the agent
to manage its own config, opt in explicitly with a pane-local
allowlist that permits `tx-pane config set …` — but be aware that's
essentially the same as no allowlist.

---

## Error message reference

| Message | Meaning | Fix |
|---|---|---|
| `pane <name> is busy (state=…)` | refuse-on-busy | `--queue` / `--kill-and-run` / `--stdin` |
| `[exit:?] (hook missing)` | marker did not arrive in window | `tx-pane hook-install <pane>` |
| `pane <name> in handoff` | tx-pane is paused on this pane | `tx-pane resume <pane>` |
| `pane <name> not found` | id/name doesn't match any pane | `tx-pane ls` to find the right id |
| `command not allowed by allowlist` | `[security].command_allowlist` rejection | adjust allowlist or use a different command |
| `command requires confirmation` | `confirm_patterns` match without TTY/`--yes` | `--yes` for one-off, or set `confirm_mode = "allow"` for CI |
| `handle h-XXXX expired` | run rotated out of history | `tx-pane output <pane> <run-id> --full` (use run-id) |
| `pane state is dead` | shell exited | `tx-pane restart <pane>` |
| `tmux server unreachable` | the tmux session disappeared | check `~/.tx-pane/.tmux.sock`; `tx-pane new` will respawn |
| `pipeline rejection (no normalizer)` | command has `\|`/`;`/`&`/`>`/`<` | expected; you chose the representation |
| `plugin disabled (two-strike)` | a normalizer crashed twice | check stderr for the file name; fix the plugin |

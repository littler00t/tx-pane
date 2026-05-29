# tx — a tmux pane controller for AI agents

`tx` turns a tmux server into a structured terminal-execution backend that
an LLM (or any other automated caller) can drive safely. The agent gets
reliable exit codes, per-pane run history, and ~50% smaller output for
most commands; the human still sees an ordinary tmux session and can
attach to watch or take over at any time.

```
agent ──► tx run web-server "systemctl status nginx"
         │
         │  ┌─────────────────────────────────────────────┐
         │  │ tmux session "tx"                            │
         │  │ ├── pane web-server  ◄── pipe-pane log       │
         │  │ ├── pane db-primary  ◄── pipe-pane log       │
         │  │ └── pane builder     ◄── pipe-pane log       │
         │  └─────────────────────────────────────────────┘
         ▼
   [exit:0]
   systemctl: active (running)                                  ◄── normalizer
   [tx:compact tier=full layers=systemctl-status,L2 in=1026B out=27B saved=97%]
```

## Why this exists

Agents that drive shells through a generic `bash` tool have to solve
the same problems over and over:

| Pain | What goes wrong | What `tx` does |
|---|---|---|
| **"Did the command finish?"** | Prompt-pattern detection breaks on multi-line prompts, custom PS1s, interactive tools. | Installs a shell hook (`PROMPT_COMMAND` / `precmd` / `fish_postexec`) that emits a sentinel line with a run-id + exit code on every prompt return. Markers fire even when the user pressed `C-c`. |
| **"What's the exit code?"** | The default is `?` or a guess from the last token. | Real exit codes via the marker — exposed as `[exit:N]` or `--json.exit`. |
| **"Output is huge — agents burn tokens reading it."** | Every `kubectl get pods`, `journalctl -u nginx`, `zpool status` returns hundreds of lines of mostly-redundant text. | 18 per-tool normalizers + 5 generic layers (banner strip, whitespace, repeated-line collapse, token-budget head/tail elision, optional cross-call dedup). Median savings: 45–60%. Elided content is recoverable via an `h-XXXX` handle. |
| **"I want to run two things at once."** | Spawning two shells means losing state, env, cwd. | Each `tx new` creates a named pane that survives across invocations. Run async with `tx exec`; check back with `tx wait-run`. |
| **"The pane is busy."** | Race between agent and prior command. | Refuse-on-busy by default; explicit `--queue` / `--kill-and-run` / `--stdin` resolution. No `--force` footgun. |
| **"Safety."** | Allowlist? Audit log? Redaction? | `[security]` config: per-pane allowlists, stdout redaction patterns, confirm-pattern prompts. Secrets flow via `tx send-secret` (stdin only, never logged). |
| **"The user wants to take over."** | The agent has to stop touching the pane. | `tx handoff` pauses tx and stops pipe-pane; `tx resume` continues. |

Linux is the primary deployment target. macOS is a fully supported dev
environment. No Windows. No daemon — `tx` is a single Python script
plus file-locked state under `~/.tx/`.

## A 30-second demo

```sh
# Spawn a named pane (returns "p1" or the name you give it)
pane=$(tx new web-server --cwd /etc/nginx)

# Run something asynchronously
run=$(tx exec "$pane" "tail -f /var/log/nginx/access.log")

# Drive other commands on the same pane while it's busy
tx run --queue   "$pane" "nginx -t"
tx run --terse   "$pane" "systemctl status nginx"    # ← per-tool normalizer
tx run --token-budget 8000 "$pane" "journalctl -u nginx -n 1000"

# Pull back to the streaming log
tx kill-run "$pane" "$run"
tx tail "$pane" --all

# Hand off to the user, then resume
tx handoff "$pane"        # human takes over in tmux attach -t tx
tx resume  "$pane"        # tx is back in control, no gap in log

# Atomically deploy a config file
tx write "$pane" /etc/nginx/sites-enabled/app.conf \
  --file ./app.conf --sudo --mode 644 --reload-cmd "nginx -s reload"
```

## Install

`tx` is a single PEP-723 Python script. Requirements:

- Python ≥ 3.11
- [`uv`](https://github.com/astral-sh/uv) (handles the inline deps)
- `tmux` ≥ 3.0
- `bash` or `zsh` for the controlling shell (`fish` is supported for
  panes via `tx new --shell fish`; `sh`/`dash` work with reduced
  reliability since they lack a robust `PROMPT_COMMAND`).

**Zero-install (recommended)** — the `tx` script resolves its own deps via `uv`:

```sh
git clone https://github.com/<your-github-username>/tx.git
cd tx
chmod +x tx
ln -s "$PWD/tx" ~/.local/bin/tx       # or copy
tx --help
```

**Or install with pip** (provides the `tx` command via the console entry point):

```sh
pip install git+https://github.com/<your-github-username>/tx.git
tx --help
```

First run creates `~/.tx/config.toml`, `~/.tx/offsets.json`, and
`~/.tx/logs/` with sensible defaults.

## Quick start

```sh
pane=$(tx new server)              # create or adopt the pane
tx exec "$pane" "npm run dev"      # async; returns a run-id
tx wait "$pane" "listening on"     # block until pattern shows up
tx tail "$pane"                    # new bytes since last read

# Run something else without disturbing the dev server:
tx run --queue "$pane" "echo queued behind the server"

# Or interrupt:
tx run --kill-and-run "$pane" "npm test"

# Attach to watch:
tmux attach -t tx                  # all tx panes live in one tmux session
```

## Core concepts

### Panes are the unit of work

Each pane is its own tmux window. `tx new` returns an id; capture it
once and reuse it everywhere. State persists across `tx` invocations
in `~/.tx/`, so the next call from a fresh process sees the same pane.

### The marker protocol

When a pane is created, `tx` installs this hook in the shell:

```sh
__tx_emit() {
  __tx_st=$?
  if [ -n "$__tx_run_id" ]; then
    printf '\001TX_END %s %s\001\n' "$__tx_run_id" "$__tx_st"
    __tx_run_id=
  fi
}
# bash: PROMPT_COMMAND='__tx_emit'
# zsh:  precmd() { __tx_emit; }
# fish: fish_postexec
```

Every `tx run` / `tx exec` wraps the user command as
`__tx_run_id=<rid>; <cmd>`. The hook fires on every prompt return —
including after `C-c` — and emits a sentinel line containing the
run-id + exit code. `tx` watches the on-disk log for that exact byte
sequence. Detection is prompt-agnostic and survives interrupts.

The marker stays in the on-disk log for forensics but is stripped from
agent-facing output.

### Compaction

By default, output goes through:

```
shell → log → ANSI strip → marker strip → redact
   → [normalizer]  → L1 hygiene → L2 whitespace
   → L3 RLE → L4 budget → L5 dedup (off by default) → emit
```

Each layer can be disabled per-call. **If a normalizer strips
something you need**, three escape hatches:

- `tx output <pane> --handle h-XXXX --range N-M` — recover the elided slice (best).
- `tx run --no-normalize <pane> <cmd>` — keep L1-L5, skip the per-tool filter.
- `tx run --raw <pane> <cmd>` — escape hatch, no compaction at all.

See [`docs/tx-doc-compaction.md`](docs/tx-doc-compaction.md) for the
full surface and [`docs/compaction_samples.md`](docs/compaction_samples.md)
for before/after pairs across all 18 tools.

### State machine

Every pane has one of these states (observable via `tx status` / `tx ls`):

```
idle | running | tui | waiting-input | unread | paused | dead
```

`tx run` / `tx exec` refuse if the state is `running`, `tui`, or
`waiting-input`. Resolve with `--queue`, `--kill-and-run`, or `--stdin`.

## Common workflows

### Long-running command, structured exit code

```sh
res=$(tx run --json "$pane" "make ci")
echo "$res" | jq -r '.exit, .duration_ms, .stdout'
```

### Stream a follow-style log for N seconds

```sh
tx stream "$pane" "journalctl -u nginx -f" --duration 10s --until "ERROR"
```

### Wait for a regex, fail-early on another

```sh
tx run "$pane" "make build" --wait-for "BUILD SUCCESS" --fail-for "FATAL"
```

### Atomic file deploy

```sh
tx write "$pane" /etc/nginx/conf.d/app.conf \
  --file ./app.conf --sudo --mode 644 \
  --reload-cmd "nginx -s reload" --diff
```

Stages a temp file in the target directory, sha256-verifies against
the local file, optionally `chmod`/`chown`, atomic `mv`, then runs
`--reload-cmd`. Each step is a marker-tracked run visible in `tx runs`.

### Nested shells (ssh / sudo -i / docker exec)

```sh
tx run "$pane" "ssh me@remote-host"     # → [exit:?] + hook-missing
tx hook-install "$pane"                  # wire the marker hook into the remote shell
tx run "$pane" "zpool status"            # [exit:0] now
```

The outer shell's hook is untouched — leaving the nested shell
restores normal marker tracking automatically.

### Sensitive input

```sh
printf %s "$SUDO_PW" | tx send-secret "$pane" --enter
```

The password never reaches `argv` (so `ps` can't see it) and never
lands in `~/.tx/logs/<pane>.log`. A `[redacted: send-secret N bytes]`
placeholder is appended to the log.

### Hand the pane to the human

```sh
tx handoff "$pane"      # pause tx; pipe-pane stopped; tx run/exec refuse
# user attaches with `tmux attach -t tx`, does interactive work
tx resume "$pane"       # tx is back in control; tail_offset skips the gap
```

## Documentation

The repo is organized so an LLM agent can read only what it needs:

| File | When to read |
|---|---|
| **[CLAUDE.md](CLAUDE.md)** | Top-level decision table: "need X → use Y". The entry point. |
| **[docs/tx-doc-reference.md](docs/tx-doc-reference.md)** | Full command + flag reference. Pull when you hit a flag you don't recognise. |
| **[docs/tx-doc-compaction.md](docs/tx-doc-compaction.md)** | Compaction modes, handle protocol, normalizer authoring, telemetry, all env-var kill switches. |
| **[docs/tx-doc-advanced.md](docs/tx-doc-advanced.md)** | Refuse-on-busy, nested shells, secrets, handoff, sudo, file deploy, safety rails. |
| **[docs/compaction_samples.md](docs/compaction_samples.md)** | Concrete before/after for every builtin normalizer, generated inside the Docker container. |
| **[docs/tx-doc-troubleshooting.md](docs/tx-doc-troubleshooting.md)** | Debugging guide and common issues. |
| **[docs/tx-doc-use-cases.md](docs/tx-doc-use-cases.md)** | Real-world scenarios (CI/CD, interactive dev, ops). |

`tx --help` and `tx <command> --help` mirror the reference doc and are
always authoritative.

## Safety

Three opt-in policies live under `[security]` in `~/.tx/config.toml`
(full details in [`docs/tx-doc-advanced.md`](docs/tx-doc-advanced.md)):

```toml
[security]
command_allowlist = "all"             # or "none", bare command tokens, or /regex/ entries
redact_patterns   = ["(?i)password=\\S+", "AKIA[0-9A-Z]{16}"]
confirm_patterns  = ["^rm -rf /", "DROP TABLE"]
confirm_mode      = "interactive"     # or "deny" / "allow"

[panes.production]
command_allowlist = ["/^systemctl status/", "/^journalctl -u/"]   # AND-merged with global
```

- **Allowlist** runs at the command-head level; per-pane lists can
  only further restrict the global, never loosen it. Bare entries match
  the first command token; `/.../` entries match the full submitted command.
- **Redaction** rewrites agent-facing stdout only. The on-disk log is
  not rewritten — for bytes that must never hit disk, use
  `tx send-secret`.
- **Confirm** patterns require the local user to acknowledge a match
  before `tx run` / `tx exec` / `tx stream` / `tx sudo` will send.
  Without a TTY, `--yes` is the acknowledgement.

## Project status

Stable at **v1.5.0** since 2026-05-14. The wire format (marker
protocol v2, `offsets.json` schema, JSON output shape) is committed —
breaking changes require a major bump and a migration path.

Phases shipped:

| Tag | Headline |
|---|---|
| v0.2 | Marker protocol v2; state machine; refuse-on-busy; run-ids |
| v0.3 | `tx info`, handoff, secrets, bookmarks, on-timeout policies, restart |
| v0.4 | Grep/dump/stream, `--json` everywhere, sudo, paste, safety rails |
| v1.0 | `tx write` atomic deploy, log rotation, fish hook, daemon evaluation (→ stay stateless) |
| v1.1 | Compaction core: L1 banners + L2 whitespace |
| v1.2 | L3 repeated-line collapse, tier model, telemetry, `tx compact-stats` |
| v1.4 | L4 token-budget head/tail elision, `h-XXX` handle protocol, 17 builtin normalizers |
| v1.5 | L5 cross-call dedup (opt-in), comprehensive docs split |

## Development

Run the host test suite (≈600 cases — 581 passing, 16 Linux-only normalizer
tests skipped on a non-Linux/host run; ~2.5 minutes):

```sh
./run-tests              # full suite
./run-tests -q
./run-tests tests/test_compact_layers.py
```

Run the Linux test suite — same tests inside the Debian-based container
with the sysadmin toolbox baked into the Dockerfile (all ≈600 cases run,
including the real-tool normalizer tier that is skipped on the host):

```sh
./run-tests-docker              # builds the image if needed; runs full suite
./run-tests-docker -q tests/test_normalizer_real.py
./run-tests-docker --rebuild    # force docker build --no-cache
./run-tests-docker --mount      # bind-mount the working tree for dev-loop edits
```

Override the container runtime: `DOCKER=podman ./run-tests-docker`.

### Regenerate `docs/compaction_samples.md` from Linux

```sh
./scripts/generate_compaction_samples.py > docs/compaction_samples.md
```

The script auto-detects whether it's running inside the container; on
the macOS dev host it re-launches itself inside `tx-tests:latest` so
the samples are sourced from real Linux tools (or captured Linux-server
fixtures for tools the unprivileged container can't run).

### Authoring a new normalizer

Drop a single file in `~/.tx/filters/<name>.toml` (line-based filter)
or `~/.tx/plugins/<name>.py` (structural parser). See
[`docs/tx-doc-compaction.md`](docs/tx-doc-compaction.md) → "Authoring
a new normalizer" for the schema + an example. Inline tests are
discovered automatically by the test runner.

## Why no daemon

Stage 4 explicitly evaluated shipping a `txd` daemon and decided
against it. The race that a daemon would solve is handled cleanly by
`fcntl.flock` around `~/.tx/offsets.json` read-modify-write cycles.
The streaming-subscription cases that a daemon would do better
(`journalctl -f`, `tail -f`) are covered by `tx stream` (bounded
capture) and `tx tail --continue`. The cost of a daemon — socket
lifecycle, IPC versioning, supervision — is high.

The v1 architecture is: one Python script + tmux pipe-pane +
file-locked offsets. We'll revisit if streaming/subscription becomes
the dominant pattern.

## Out of scope

No GUI. No MCP server. No multi-user. Windows is not supported.
Backgrounded-command exit codes are not tracked (the marker hook fires
on the shell's "backgrounded ok" prompt, not the eventual process
exit). Multi-line commands track only the first top-level statement
— wrap them in a brace group, function, or script.

## License

Released under the MIT License — see [LICENSE](LICENSE).

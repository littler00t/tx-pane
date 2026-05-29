# tx — full command + flag reference

This is the operator-level reference. Read in full when you need a flag
you don't recognise, or when planning a multi-step workflow.

Each row is *one* command. Flags shared across commands are listed in the
"Cross-cutting" section at the bottom.

## Commands

### Pane lifecycle

```
tx new [name] [--cwd <dir>] [--shell bash|zsh|sh|fish]
  → returns pane id (capture it)
tx ls [--format table|tsv|json]
tx kill <pane> [--signal hup|term|kill]
tx restart <pane>             # revive a dead pane keeping the log
tx status <pane>              # one-line state
tx info <pane>                # multi-line state + recent runs
tx runs <pane> [--limit N]    # history
```

### Running commands

```
tx run <pane> <cmd>           # send + wait + return new output
tx exec <pane> <cmd>          # start, return run-id (capture)
tx wait-run <pane> <run-id>   # block until that run finishes
tx kill-run <pane> <run-id>   # send C-c targeting one run
```

`run` accepts:
- `--max N`                       cap output at N lines
- `--timeout SEC`                 marker wait timeout
- `--no-strip`                    keep blank-line runs as-is
- `--queue` / `--kill-and-run`    resolve refuse-on-busy
- `--max-wait SEC`                bound the `--queue` wait
- `--stdin` / `--no-enter`        feed text to the running command
- `--on-timeout report|cancel|kill` what to do when timeout fires
- `--keep-ansi`                   keep ANSI escapes
- `--json`                        emit `{pane,run_id,cmd,started,ended,exit,duration_ms,stdout,truncated,notes?}`
- `--yes`                         skip confirm-pattern prompt
- `--wait-for <re>` / `--fail-for <re>` early return on pattern
- compaction flags: `--raw / --terse / --token-budget N / --no-strip-banners / --no-collapse-repeats / --no-normalize`

`exec` accepts:
- `--timeout SEC`                 default timeout recorded for `wait-run`
- `--queue` / `--kill-and-run`    resolve refuse-on-busy
- `--max-wait SEC`                bound the `--queue` wait
- `--json`                        emit the started run as JSON
- `--yes`                         skip confirm-pattern prompt

`wait-run` accepts:
- `--timeout SEC`                 marker wait timeout
- `--max N`                       cap output at N lines
- `--no-strip`                    keep blank-line runs as-is
- `--on-timeout report|cancel|kill` what to do when timeout fires
- `--keep-ansi`                   keep ANSI escapes
- `--json`                        emit a single JSON record
- compaction flags: `--raw / --terse / --token-budget N / --no-strip-banners / --no-collapse-repeats / --no-normalize`

### Reading output

```
tx tail   <pane> [--max N] [--continue] [--all] [--from <bookmark>]
                  [--no-strip] [--keep-ansi] [--timestamps]
tx dump   <pane> [--max N] [--tail N] [--head N] [--from <bookmark>]
                  [--continue] [--no-strip] [--keep-ansi] [--timestamps]
tx log    <pane> [--max N] [--tail N] [--head N] [--since-run <id>]
                  [--no-strip] [--keep-ansi]
tx grep   <pane> <regex> [-B N] [-A N] [-C N]
tx output <pane> [<run-id>] [--last | --since-run <id> | --handle h-XXX]
                  [--max N] [--no-strip] [--keep-ansi] [--json]
                  [--range N-M] [--grep PAT] [--grep-context N] [--full]
                  compaction flags as above
```

`tail` advances the per-pane `tail_offset`; subsequent `tail` returns
only new bytes. `dump` does NOT advance. `log` is the raw on-disk log,
also non-advancing.

`tail`, `dump`, `wait`, `log`, `grep`, `output`, `wait-run`, `stream`,
and `run` accept the compaction flags listed above. `exec` does not emit
command output and does not accept compaction flags.

### Wait + stream

```
tx wait   <pane> <regex> [--timeout SEC] [--max N] [--no-strip]
tx stream <pane> <cmd>  [--duration 5s | --lines N | --until <regex>]
                          [--timeout SEC] [--max N] [--no-strip]
                          [--keep-ansi] [--yes]
                          compaction flags as above
```

`stream` runs a command + captures output bounded by `--duration` /
`--lines` / `--until`, then `C-c`s. Use for `journalctl -f` style.

### Bookmarks

```
tx mark   <pane> <name>           # record current end-of-log
tx tail   <pane> --from <name>    # read since bookmark
tx dump   <pane> --from <name>
tx reset  <pane> --to <name>      # rewind tail_offset
```

### Special input

```
tx send         <pane> <text>          # raw, no Enter; enforces allowlist/confirm
tx key          <pane> <keys>...       # C-c, Enter, etc.
tx paste        <pane> --file <path>   # bracketed paste
                       (or stdin)
tx sudo         <pane> <cmd>           # local password prompt over TTY
tx send-secret  <pane> [--enter]       # stdin; bytes never hit log
tx handoff      <pane>                 # pause tx for user
tx resume       <pane>                 # reattach pipe-pane
```

### File deployment

```
tx write <pane> <remote-path> --file <local-path>
                              [--sudo]
                              [--mode 644]
                              [--owner user:group]
                              [--reload-cmd 'nginx -s reload']
                              [--overwrite]
                              [--diff]
```

Atomic: stages a temp file in target dir, sha256-verifies, optionally
chmod/chown, then `mv -f`. With `--sudo`, all remote ops use `sudo -n`
— cache credentials first via `tx sudo` once. Refuses if target
exists unless `--overwrite`. Refuses on fish panes (no heredoc).

### Marker hook

```
tx hook-install <pane> [--timeout SEC] [--no-verify] [--shell <name>]
```

Required after entering a nested shell (ssh, sudo -i, docker exec).
Auto-reinstall fires on the next run when `hook_ok` flips False, but
manual reinstall is explicit.

### Maintenance

```
tx maintain [--dry-run] [--force]   # log rotation + sweep
tx compact-stats [--weak] [--passthrough] [--since ISO]
                 [--limit N] [--json] [--forget]
tx config                           # print active config
tx log-path <pane>                  # absolute path to ~/.tx/logs/<pane>.log
```

## Cross-cutting flags

These appear on most commands:

| Flag | Meaning |
|---|---|
| `--max N` | cap returned lines at N (remainder accessible via `tx tail --continue`) |
| `--no-strip` | preserve blank-line runs |
| `--keep-ansi` | do not strip ANSI escapes |
| `--timestamps` | prepend `[hh:mm:ss]` (read-time, not per-line) — `tail`/`dump` only |
| `--json` | emit a single JSON record (single-run commands) |

## State files

```
~/.tx/config.toml          # all config; auto-created on first run
~/.tx/offsets.json         # per-pane state (runs, bookmarks, handles, …)
~/.tx/logs/<pane>.log      # raw pipe-pane log; survives `tx kill`
~/.tx/logs/<pane>.log.[1-N] # rotated copies (max_keep)
~/.tx/compact.jsonl        # compaction telemetry (privacy: cmd_head only)
~/.tx/.lock                # fcntl flock around offsets.json read-modify-write
```

## Exit codes

`tx` exits 0 on success, 1 on user-facing errors (pane busy without
resolution flag, missing run-id, bad regex, etc.). `tx run` itself
exits 0 even when the wrapped command produced a non-zero exit — read
the `[exit:N]` line or the `--json` payload's `exit` field.

`--wait-for` pattern match returns 0; `--fail-for` returns 1.

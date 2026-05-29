# tx-pane — advanced topics

Read this when you need to:
- Work in nested shells (ssh, sudo -i, docker exec).
- Send a password or other secret.
- Hand the pane to the user temporarily.
- Run `sudo` interactively from `tx-pane`.
- Deploy a file atomically with `tx-pane write`.
- Configure safety rails (allowlist / redact / confirm patterns).
- Understand timeout / refuse-on-busy resolution.

## Refuse-on-busy

`tx-pane run` and `tx-pane exec` refuse if a pane is already running a command.
Resolve with exactly one of:

| Flag | Use when |
|---|---|
| `--queue` | wait for the previous run to finish, then send. Bounded by `--max-wait SEC` (default = `--timeout`). |
| `--stdin` | the running command is reading input (sudo password, `read`, `(yes/no)`). Feed text. Errors if pane is idle. |
| `--kill-and-run` | the previous run is hung or wrong; C-c it, wait for idle (≤5s), then send. |

There is **no** `--force`. Resolution is always explicit.

## `--on-timeout` policy

`tx-pane run` and `tx-pane wait-run` accept `--on-timeout report|cancel|kill`:

- `report` (default) — emit `[timeout: …]`, leave the run active.
- `cancel` — send `C-c`, wait briefly for the marker, report `[exit:N]`.
- `kill` — `C-c` twice, then destroy the tmux pane.

## Nested shells (ssh / sudo -i / docker exec)

The marker hook from `tx-pane new` lives in the **outer** shell only. After
you enter a nested shell, `tx-pane run` returns `[exit:?]` plus a
`hook-missing` note. Install the hook in the nested shell:

```
tx-pane hook-install <pane>
```

The probe self-test confirms it's wired. Subsequent runs get real exit
codes again. When you leave the nested shell, the outer shell's hook
is still in place — no further action needed.

**Auto-reinstall**: when a run ends with `exit_code=None` (prompt-pattern
fallback fired), `hook_ok` flips False. On the next `run`/`exec`/`sudo`,
the shell-init snippet is re-sent before the wrap. Disable in config
with `[defaults] auto_reinstall_hook = false`.

## Secrets

For sudo passwords, SSH passphrases, decryption keys, anything that
must not land on disk:

```sh
printf %s "$PW" | tx-pane send-secret <pane> --enter
```

- Reads from stdin, never argv.
- pipe-pane is paused for the duration of the send; the bytes don't
  reach `~/.tx-pane/logs/<pane>.log`.
- A `[redacted: send-secret N bytes]` placeholder is appended to the
  log in their place.
- `--enter` appends a literal Enter (most password prompts need this).

**Differs from `tx-pane send`**: `send` also doesn't auto-log echoes, but
it places the literal bytes in tmux's command stream where some
terminals may render them briefly. `send-secret` is the safe choice.

## Handoff to the user

When you need the user to do something the agent can't (interactive
config wizard, `gpg --gen-key`, `vim` editing, manual SSH key entry):

```
tx-pane handoff <pane>      # pause tx-pane; pipe-pane stopped; tx-pane run/exec/etc refuse
# user does their thing in the terminal
tx-pane resume <pane>       # reattach pipe-pane (append mode); refreshes tail_offset
```

While paused, every input-side `tx-pane` command errors with a clear pointer
to `tx-pane resume`. User keystrokes during handoff are NOT logged.

## Sudo

```
tx-pane sudo <pane> <cmd>
```

Prompts locally on the controlling TTY for the password, sends it via
the secret pathway, then sends the command. Refuses without a TTY
(non-interactive callers should pre-cache creds and use plain
`tx-pane run sudo ...`). Use once per session — `sudo`'s own credential
cache handles subsequent calls (typical default: 15 minutes).

## Bracketed paste

```
cat config.yml | tx-pane paste <pane>
tx-pane paste <pane> --file config.yml
```

Wraps the bytes in tmux's bracketed-paste sequence so the receiving
shell treats them as literal input (no command interpretation,
multi-line preserved). Use for heredocs that need precision, configs
piped into an editor, etc.

## File deployment — `tx-pane write`

```
tx-pane write <pane> <remote-path> --file <local-path>
                              [--sudo]
                              [--mode 644]
                              [--owner user:group]
                              [--reload-cmd 'nginx -s reload']
                              [--overwrite]
                              [--diff]
```

Steps (each is a marker-tracked run visible in `tx-pane runs`):

1. Stage at `<dir>/.tx-write-<rand>` via heredoc + bracketed paste.
2. `sha256sum`-verify against the local hash.
3. Optional `chmod` / `chown`.
4. `mv -f` over the target (atomic).
5. Optional `--reload-cmd` after.

Refuses if the target exists unless `--overwrite`. Refuses on fish
panes (no heredoc). With `--sudo`, all remote ops use `sudo -n` — call
`tx-pane sudo` once first to cache credentials. `--diff` previews the
change before committing.

## Safety rails

Three opt-in policies under `[security]` in `~/.tx-pane/config.toml`:

### `redact_patterns`

```toml
[security]
redact_patterns = ["(?i)password=\\S+", "AKIA[0-9A-Z]{16}"]
```

Matches in agent-facing stdout (`tx-pane tail` / `dump` / `output` / `run` /
`wait-run` / `log` / `grep`) are replaced with `[redacted]`. The
on-disk log is **not** rewritten — for bytes that must never hit
disk at all, use `tx-pane send-secret`.

### `confirm_patterns` + `confirm_mode`

```toml
[security]
confirm_patterns = ["^rm -rf /", "DROP TABLE"]
confirm_mode = "interactive"   # | "deny" | "allow"
```

Commands matching any pattern require confirmation before `tx-pane run`/
`exec`/`stream`/`sudo` will send them. In `interactive` mode (default)
without a TTY, the call refuses with a pointer at `--yes`. Pass `--yes`
once your caller has logged the policy acknowledgement.

### Allowlist

```toml
[security]
command_allowlist = ["ls", "df", "/^cat /var/log//"]   # or "all"

[panes.web-server]
command_allowlist = ["/^systemctl status nginx/"]      # AND-merged with global
```

A per-pane list **further restricts** the global; it cannot loosen it.
Bare entries match the first command token exactly; `/.../` entries are
regular expressions matched against the full submitted command.

## Maintenance

```
tx-pane maintain [--dry-run] [--force]
```

Forces log rotation + sweep of aged rotated logs (`.log.1`, `.2`, …).
`tx-pane ls` also runs an opportunistic sweep at most once per
`[logs] sweep_interval_hours` (default 24).

## Auto-recovery

If a pane shows status `dead` (the tmux pane was closed), revive it with:

```
tx-pane restart <pane>
```

A fresh tmux pane is attached to the same pane id; the existing log
file is preserved. Idle marker hook is re-installed automatically.

## Output retrieval

When `--max` truncated the output:

```
tx-pane tail <pane> --continue    # drain the next chunk
tx-pane tail <pane> --all         # iterate --continue internally until drained
```

When L4 elided content (handle in the response): see `docs/tx-doc-compaction.md`.

## When in doubt

- **`tx-pane status <pane>`** for one-line state.
- **`tx-pane info <pane>`** for multi-line state + recent runs.
- **`tx-pane runs <pane>`** for run history.
- **`tx-pane log-path <pane>`** for the raw on-disk log (cat'able for forensics).

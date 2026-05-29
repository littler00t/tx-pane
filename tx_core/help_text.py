"""Top-level help text shown by `tx-pane --help`.

Kept as a separate module so the giant string doesn't clutter the rest of
the codebase. The CLI module wires this into `TxGroup.format_help`.
"""

from __future__ import annotations

HELP_TEXT = """tx-pane — tmux pane controller for Claude Code (v1.5.0 / protocol v2)

PANE LIFECYCLE
  tx-pane new [name] [--cwd <path>] [--shell bash|zsh|sh|fish]
      Create a pane. Name is optional; a pane id is generated if omitted (e.g. p1).
      Returns the pane id on one line. Always capture this for subsequent commands.
      All panes live in the tmux session defined by config (default: "tx-pane").
      Each tx-pane pane is its own tmux window. Attach with: tmux attach -t tx-pane
      The pane is initialised with a marker-emission hook (PROMPT_COMMAND on
      bash, precmd on zsh); other shells will fall back to less reliable
      completion detection. --cwd starts the shell in the given directory.
      --shell exec's into the chosen shell after pane creation before the
      hook is installed (useful for testing across shells or matching a
      user's preferred login shell).

  tx-pane ls [--format table|tsv|json]
      List managed panes: id, status, foreground command, pid.
      Status: idle | running | tui | waiting-input | paused | unread | dead

  tx-pane kill <pane> [--signal hup|term|kill]
      Destroy pane and stop logging. Log file is preserved at
      ~/.tx-pane/logs/<pane>.log for post-mortem inspection.
      Signals (default: term):
        hup    send C-d (EOF) — works on a shell prompt
        term   send C-c twice, then kill the pane (default)
        kill   skip the C-c; destroy the pane immediately

  tx-pane restart <pane>
      Re-attach a fresh tmux pane to a dead pane id while keeping the existing
      log file. Useful when an ssh session drops or the shell crashes.

  tx-pane status <pane>
      One-line snapshot: pane state, active run id, last completed run,
      foreground, waiting pattern (if any), attached client status.

  tx-pane info <pane>
      Multi-line report: state, shell, foreground, cwd, current_run, last_run,
      buffer/log bytes, tail offset, attached client, created timestamp.
      cwd uses /proc on Linux or lsof on macOS ("?" if neither available).

  tx-pane hook-install <pane>
      Re-install the v2 marker hook in the pane's *current* foreground shell.
      Use after entering a nested interactive shell (ssh / sudo -i / su - /
      docker exec -it / kubectl exec -it / nsenter / chroot / …) so subsequent
      tx-pane run / tx-pane exec calls observe markers and capture exit codes again.
      Self-tests the install by emitting a probe marker; warns if the shell
      does not appear to support PROMPT_COMMAND or precmd.

  tx-pane handoff <pane>
      Pause tx-pane control. Subsequent tx-pane run/exec/send/key refuse with an error
      pointing at 'tx-pane resume'. pipe-pane is stopped while paused so untrusted
      keystrokes are not captured. Use for sudo password entry or any flow
      where you want the user typing without tx-pane interference.

  tx-pane resume <pane>
      End a handoff: re-attach pipe-pane (append mode), refresh tail_offset.

SENDING INPUT (foreground)
  tx-pane run <pane> <cmd>
      Send <cmd> + Enter, wait for the run's end marker, return new output.
      Refuses if the pane is busy; see flags below to resolve.
      Options:
        --max N          cap output at N lines
        --timeout N      override wait timeout in seconds
        --no-strip       disable whitespace collapsing
        --keep-ansi      do not strip ANSI escape sequences
        --json           emit a single JSON record instead of plain text
        --queue          wait for the pane to become idle before sending
        --max-wait N     bound the --queue wait (default = --timeout)
        --kill-and-run   send C-c, wait briefly for idle, then run
        --stdin [--no-enter]
                         feed text to a running command's stdin (refuses on
                         an idle pane; does not allocate a run-id)
        --wait-for REGEX return early (exit=0) when REGEX matches output;
                         the command is interrupted with C-c.
        --fail-for REGEX same as --wait-for but exit=1 (use for error patterns).
        --yes            skip confirm-pattern prompt (for non-interactive use)
        --on-timeout report|cancel|kill
                         report (default) leaves the run active; cancel sends
                         C-c and re-checks; kill sends C-c twice + kill-pane.

  tx-pane exec <pane> <cmd>
      Async variant of tx-pane run: sends the command and prints its run-id
      immediately. Use 'tx-pane wait-run' to block, 'tx-pane output' to fetch later.
      Shares the same --queue / --kill-and-run flags. --json emits a
      structured record; --yes skips confirm-pattern prompts.

  tx-pane stream <pane> <cmd> --duration N[s|m|h] | --lines N | --until <regex>
      Run <cmd> and capture its output until a bound is reached; then send
      C-c and return what was captured. Useful for "give me 5s of
      journalctl -f" or "run until 'Listening on' appears, then stop".

  tx-pane sudo <pane> <cmd>
      Convenience wrapper: prompts the local user for the sudo password on
      the TTY, sends 'sudo -S -p "" <cmd>', pipes the password via the
      send-secret path (no log capture), and waits for completion.
      Requires an interactive TTY; agents without a TTY should use
      'tx-pane exec ... "sudo -S -p \\"\\" ..."' + 'tx-pane send-secret' manually.

  tx-pane paste <pane> [--file <path>]
      Read content (from --file or stdin) and paste it into the pane using
      tmux's bracketed-paste mode. The shell receives the bytes atomically
      (no per-line evaluation); ideal for heredocs, JSON blobs, scripts.
      Refuses on busy or paused panes.

  tx-pane write <pane> <remote-path> --file <local-path>
                 [--sudo] [--mode <octal>] [--owner <user:group>]
                 [--reload-cmd <cmd>] [--overwrite] [--diff]
                 [--timeout N] [--yes]
      Atomically deploy a local file to a remote path via the pane shell.
      Stages alongside the target (`<target-dir>/.tx-write-<rand>`) via a
      heredoc + bracketed paste, sha256-verifies on the remote, optionally
      chmods/chowns the stage, then `mv -f`s into place. --reload-cmd runs
      after a successful move. Refuses if the target exists unless
      --overwrite. Refuses on fish-shell panes (no heredoc). --sudo runs
      every remote operation under `sudo -n`. Each internal step is a
      marker-tracked run visible in `tx-pane runs`.

  tx-pane send <pane> <text>
      Send raw text without Enter. No output returned. Enforces
      allowlist and confirm-pattern policy.

  tx-pane send-secret <pane> [--enter]
      Read text from STDIN (never argv) and send it to the pane. The bytes
      do not appear in the on-disk log — only a '[redacted: send-secret N
      bytes]' placeholder is appended. Use for sudo passwords / decryption
      passphrases. Pipe the value in: `printf %s "$PW" | tx-pane send-secret <pane>`.

  tx-pane key <pane> <key> [key ...]
      Send one or more special keys in sequence.
      Supported: Enter  C-c  C-d  C-z  Esc  Up  Down  Left  Right  Tab
      Example: tx-pane key server C-c Enter

READING OUTPUT
  tx-pane tail <pane> [--max N] [--continue] [--all] [--from <name>] [--no-strip]
                 [--keep-ansi] [--timestamps]
      Return new output since last tail/run call (incremental).
      --continue resumes reading after a truncation. Repeat until [end of output].
      --all drains pending + new output in one call (auto-iterates --continue).
      --from <name> reads from a saved bookmark instead of tail_offset.
      --keep-ansi preserves ANSI escapes; --timestamps prefixes each line
      with [hh:mm:ss] (read-time, not per-line).
      Tail offset only advances after the full buffer is consumed.

  tx-pane dump <pane> [--max N] [--tail N] [--head N] [--from <name>] [--continue]
                 [--no-strip] [--keep-ansi] [--timestamps]
      Return the pane buffer. Default reads from the start; --tail N returns
      the last N cleaned lines, --head N returns the first N. --from <name>
      reads from a bookmark. Does not affect tail_offset.

  tx-pane grep <pane> <regex> [-A N] [-B N] [-C N] [--max N] [--keep-ansi]
      Search the pane log for <regex>. -A/-B/-C work like GNU grep
      (after / before / centred context). Match regions are separated by
      '--' lines when context is requested. Plain-text only — no
      highlighting.

  tx-pane log-path <pane>
      Print the absolute path to ~/.tx-pane/logs/<pane>.log.

  tx-pane log <pane> [--tail N] [--head N] [--since-run <id>] [--max N]
                [--no-strip] [--keep-ansi]
      Read the on-disk log directly. Does NOT advance tail_offset.

  tx-pane wait <pane> <regex> [--timeout N] [--max N] [--no-strip]
      Block until new output matches <regex>. Return all new output up to
      and including the matching line. On timeout: emit partial output with
      [timeout: ...] notice (no error).

  tx-pane mark <pane> <name>
      Save the current end-of-log byte offset under <name> in a per-pane
      bookmark table. Read with tx-pane tail --from / tx-pane dump --from / tx-pane reset --to.

RUN-ID COMMANDS (v2)
  tx-pane wait-run <pane> <run-id> [--timeout N] [--max N] [--no-strip] [--keep-ansi]
              [--json] [--on-timeout ...]
      Block until the named run's end marker is observed; return its output.
      If the run is already complete, returns cached output immediately.
      --json emits a structured record (schema below).

  tx-pane output <pane> [<run-id>] [--last | --since-run <id>] [--max N]
            [--no-strip] [--keep-ansi] [--json]
      Return the slice between a run's start and end markers (cleaned).
      --last returns the most recent completed run.
      --since-run <id> concatenates every run after the named one.
      --json works with single-run selectors (<run-id> / --last).

  tx-pane runs <pane> [--limit N]
      Table of recent runs: id, exit, duration, started, cmd.

  tx-pane kill-run <pane> <run-id>
      Send C-c to the pane and wait briefly for the active run to finalize.

STATE
  tx-pane reset <pane> [--to <name>]
      Reset tail offset to current end of log. With --to <name>, rewinds
      tail_offset to a saved bookmark instead.

  tx-pane config
      Print active configuration plus tx-pane + tmux versions and config paths.

  tx-pane maintain [--dry-run] [--force]
      Rotate every pane's log if it exceeds [logs] max_size_mb, then sweep
      any rotated logs older than [logs] max_age_days. --dry-run previews
      without changing files. --force rotates regardless of size. `tx-pane ls`
      runs an opportunistic age sweep no more than once per
      [logs] sweep_interval_hours (default 24h).

WORKFLOW PATTERNS
  Short command:
      tx-pane run server "npm test"

  Start a long-running process, wait for ready:
      pane=$(tx-pane new server)
      tx-pane exec $pane "npm run dev"
      tx-pane wait $pane "listening on"
      tx-pane tail $pane

  Resolve a busy pane:
      tx-pane run --queue $pane "echo 'will wait for the previous run'"
      tx-pane run --kill-and-run $pane "echo 'will interrupt then run'"
      tx-pane run --stdin $pane "yes"          # feed input to a waiting prompt

  Asynchronous run + later fetch:
      id=$(tx-pane exec $pane "make build")
      # ... do other stuff ...
      tx-pane wait-run $pane $id
      tx-pane output $pane $id --max 200

  Live tail of a streaming process:
      tx-pane dump $pane --tail 50             # last 50 lines on screen, no offset change

  Process hung or needs interrupt:
      tx-pane kill-run $pane <run-id>          # graceful: C-c + wait for marker
      tx-pane key $pane C-c                    # raw C-c without state tracking

  Drive a remote box via SSH:
      pane=$(tx-pane new homeserver)
      tx-pane run $pane "ssh me@potze"         # returns with [exit:?] (fallback)
      tx-pane hook-install $pane               # wire markers into the remote shell
      tx-pane run $pane "zpool status"         # real [exit:0] now
      tx-pane run $pane "exit"                 # leaves SSH; back to local shell
      tx-pane hook-install $pane               # optional: local hook already there,
                                          # but a no-op re-install is safe.

OUTPUT FORMAT
  Plain text only. No JSON (except 'tx-pane ls --format json'). No timestamps.
  ANSI escape sequences always stripped.
  Meta lines use the format: [key: value message]
  Exit codes are surfaced as a [exit:N] line at the start of run output.
  [exit:?] means the command completed (prompt returned) but no marker was
  observed — typically a nested shell without the hook installed. Use
  'tx-pane hook-install <pane>' to wire markers into the current shell.
  Errors:   [error: description]    — exit code 1
  Warnings: [warning: description]  — continues
  [hook-missing: ...] is appended to run output when the prompt fallback fires.

CONFIGURATION
  ~/.tx-pane/config.toml — run 'tx-pane config' to inspect.
  Key settings:
    max_lines         default output cap
    timeout           default wait timeout (seconds)
    idle_method       legacy: "prompt" (default) or "silence"; only used by
                      tx-pane send / tx-pane wait when no marker is in flight
    prompt_patterns   regex list indicating a shell prompt (legacy detection)
    waiting_patterns  regex list for waiting-input detection (password / yes-no)
    idle_silence_ms   ms of silence before idle declared (silence mode only)
    strip             true/false, default true
    strip_ansi        true/false, default true; toggled per-call by --keep-ansi
    tmux_session      tmux session name (default: "tx-pane")
    max_run_history   per-pane run-history cap (default 100)
    history_limit     tmux scrollback set on new panes (default 100000)
    command_allowlist "all" | "none" | [patterns]; regex via /…/ form
                      (the old 'allowed_commands' key still works with a
                      one-time deprecation warning)
    redact_patterns   [security] list of regex; matches in returned stdout
                      are replaced with '[redacted]'. The on-disk log is
                      NOT rewritten.
    confirm_patterns  [security] list of regex; matching commands prompt the
                      local user before being sent.
    confirm_mode      "interactive" (default) | "deny" | "allow". Controls
                      what happens when confirm_patterns fires without a TTY
                      or without --yes.
    auto_reinstall_hook
                      true (default) — when a run finalises with no marker
                      (exit:?), the next tx-pane run resends SHELL_INIT_SETUP
                      before sending its wrap.
    [panes.<id>] command_allowlist
                      per-pane allowlist; AND-merges with the global setting.
    [protocol] version  "v2" (default). The active marker protocol version.
    [logs] max_size_mb     rotate <pane>.log to <pane>.log.1 above this (100).
    [logs] max_age_days    delete rotated logs older than this (30).
    [logs] max_keep        cap on number of rotated copies per pane (10).
    [logs] sweep_interval_hours  lazy sweep cadence triggered by tx-pane ls (24).

JSON SCHEMA (--json)
  tx-pane run / tx-pane wait-run / tx-pane output --last / tx-pane output <run-id> emit:
    {
      "pane": "...",          # tx-pane pane id
      "run_id": "r-xxxxxx",
      "cmd": "...",           # the user's command
      "started": "ISO-8601Z",
      "ended": "ISO-8601Z|null",
      "exit": <int|null>,     # null = hook missing or run cancelled early
      "duration_ms": <int|null>,
      "stdout": "...",        # joined with \\n, ANSI stripped unless --keep-ansi
      "truncated": <bool>,
      "notes": ["..."]        # optional: timeout, hook-missing, wait-for, etc.
    }
  tx-pane exec --json returns the same shape with exit/ended/stdout = null
  (run is still in flight).

PERSISTENT STATE
  ~/.tx-pane/offsets.json     per-pane cursor, pending caches, active_run, runs
  ~/.tx-pane/logs/<id>.log    full pipe-pane capture for each pane (preserved
                         across tx-pane kill so you can read it post-mortem)
  ~/.tx-pane/.lock            advisory exclusive lock around offsets.json reads
                         (so concurrent tx-pane invocations don't clobber state)"""

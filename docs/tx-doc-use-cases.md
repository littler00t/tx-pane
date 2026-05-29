# tx-pane — use cases for humans

This doc is **for operators and developers who are deciding whether `tx-pane`
fits their workflow.** Each section is a complete story: who has the
problem, what they do today, what `tx-pane` changes, and copy-pastable
commands.

If you're looking for the agent-side decision guidance (when to use
which flag, anti-patterns, token-economy), see
[`tx-doc-agent-playbook.md`](tx-doc-agent-playbook.md). If you're
comparing `tx-pane` to other tools before adopting it, see
[`tx-doc-comparison.md`](tx-doc-comparison.md).

## 1. "I want my agent to deploy nginx changes without breaking prod"

You have an LLM-driven workflow that edits nginx configs and reloads
the service. Today the agent SSHes in, writes a file with `cat <<EOF`,
runs `nginx -t`, parses the output, and runs `systemctl reload`.
Roughly every tenth deploy something subtle goes wrong: a heredoc
swallows a backtick, `nginx -t` succeeds on the new config but the
*reload* fails because the file ended up at the wrong path, or the
agent's prompt-regex misses the exit code and it doesn't notice the
syntax error.

What `tx-pane` changes:

```sh
pane=$(tx-pane new web-server --cwd /etc/nginx)

# 1. Atomic file deploy with content verification
tx-pane write "$pane" /etc/nginx/sites-enabled/app.conf \
  --file ./app.conf --sudo --mode 644 \
  --reload-cmd "nginx -t && nginx -s reload" \
  --diff

# 2. If the agent wants to verify, exit codes are real:
status=$(tx-pane run --json "$pane" "systemctl is-active nginx" | jq -r .exit)
[ "$status" = 0 ] || rollback
```

`tx-pane write` stages the file in the target directory, sha256-verifies
against the local source, then `mv`s into place. If the reload fails,
the staging file is removed and the original is untouched. Every step
is a tracked run — `tx-pane runs "$pane"` shows the full audit trail.

For the safety-critical bits, set in `~/.tx-pane/config.toml`:

```toml
[panes.web-server]
command_allowlist = ["/^systemctl (status|reload|is-active)/", "/^nginx -t$/",
                     "/^journalctl -u nginx/", "/^tx-pane write/"]
confirm_patterns  = ["^systemctl restart"]   # full restart needs human ack
```

The allowlist is enforced by the local `tx-pane`, *before* a single byte is
sent to the pane. Bare allowlist entries match the first command token;
`/.../` entries are regexes matched against the full command string the
agent submits.

## 2. "I want to triage an incident with my agent without it spamming me"

It's 02:00. PagerDuty says a service is down. You want the agent to
gather facts — service status, recent logs, disk free, dmesg — and
hand you a summary, not paste 80KB of `journalctl` output into Slack.

```sh
pane=$(tx-pane new triage --cwd /)

# All of these go through the per-tool normalizers.
tx-pane run --terse "$pane" "systemctl status app.service"
tx-pane run --token-budget 6000 "$pane" "journalctl -u app.service -n 2000"
tx-pane run "$pane" "df -h"
tx-pane run --terse "$pane" "dmesg | tail -200"
```

For `systemctl status` of a healthy unit, the normalizer collapses to
one line. For an unhealthy unit it keeps the failed-state block and
the last journal lines. For `journalctl`, the `--token-budget` flag
keeps head and tail, with an `h-XXXX` handle for the elided middle:

```
… (elided 41218 bytes, 980 lines) [handle h-9f3a]
```

If the agent's first read suggests the interesting bit is in the
elided middle:

```sh
tx-pane output "$pane" --handle h-9f3a --grep "Connection reset"
tx-pane output "$pane" --handle h-9f3a --range 400-500
```

You can also `tmux attach -t tx-pane` and watch what the agent is doing in
real time. The agent doesn't know — the pipe-pane log is unchanged.

## 3. "I run a long test suite and need to do other things on the same machine"

```sh
pane=$(tx-pane new tester --cwd ~/work/myrepo)

# Start the long-running test, get back a run-id immediately.
run=$(tx-pane exec "$pane" "make test-integration")

# Do something else on the same pane without disturbing the test:
tx-pane run --queue "$pane" "git status"               # waits its turn
tx-pane run --kill-and-run "$pane" "git status"        # aborts the test, runs git
tx-pane run --stdin "$pane" "y"                        # send keyboard input
                                                  # (e.g. answering a prompt)

# Periodically check on the test:
tx-pane grep "$pane" "FAIL|PASS|ERROR" -C 1
tx-pane wait "$pane" "tests passed|tests failed" --timeout 1800

# When done:
tx-pane output "$pane" "$run" --json | jq .exit
```

Compared to backgrounding with `&`, you get: real exit codes after the
fact, a tracked run history, no stdout/stderr interleaving with later
commands, and the ability to `tmux attach` to watch live.

## 4. "I want to give my agent SSH access to a fleet"

This is the case where the marker-hook design pays off most. SSH into
a remote host, and you've entered a *new* shell — the outer pane's
`PROMPT_COMMAND` hook is irrelevant; the remote shell has its own. If
all you have is prompt-regex detection, the agent has to re-learn the
remote prompt; if you have the marker, you re-install it:

```sh
pane=$(tx-pane new ops)

tx-pane run "$pane" "ssh deploy@web-01"
# At this point the agent is in the remote shell. Without the hook,
# subsequent `tx-pane run` will emit "[exit:?] (hook missing)".
tx-pane hook-install "$pane"

# Now exit codes are correct again, even in the remote shell:
tx-pane run "$pane" "zpool status"
tx-pane run --terse "$pane" "systemctl status haproxy"

# Leaving the remote shell restores the outer hook automatically —
# no action needed.
tx-pane run "$pane" "exit"
tx-pane run "$pane" "hostname"     # back on local host, hook intact
```

Pair this with allowlists on the local `tx-pane` so even if the agent's
prompt gets injected, the remote `rm -rf /` never leaves the local
machine:

```toml
[security]
command_allowlist = [
  "^ssh deploy@",
  "^tx-pane hook-install$",
  "^zpool status",
  "^systemctl (status|is-active)",
  "^journalctl",
  "^exit$",
]
```

## 5. "I want to hand the pane to a human partway through"

Common during interactive debugging. The agent narrows it down to
"this requires `kubectl exec` into a pod and poking around", which is
exactly the case where you want a human at the keyboard.

```sh
tx-pane handoff "$pane"
# tx-pane stops pipe-pane; `tx-pane run` will refuse with "pane in handoff".
# You: tmux attach -t tx-pane, do whatever, then detach (default: C-b d).
tx-pane resume "$pane"
# tx-pane reattaches pipe-pane and skips the gap in the log so the agent
# doesn't see your investigation as agent-driven output.
```

This is the single feature most surprising to people coming from
shell-tool-style agents — the agent and the human can *trade off*
control of the same terminal session without losing state.

## 6. "I want my CI to drive tx-pane the same way my agent does"

`tx-pane` is just a CLI; it doesn't care who's calling. A shell script in
GitHub Actions can use the same primitives:

```yaml
- name: Smoke deploy via tx-pane
  run: |
    pane=$(tx-pane new staging --cwd ./infra)
    tx-pane run "$pane" "terraform plan -out=plan.bin"
    tx-pane run --json "$pane" "terraform apply -auto-approve plan.bin" > apply.json
    if [ "$(jq -r .exit apply.json)" -ne 0 ]; then
      tx-pane output "$pane" --last --full > /tmp/full.log
      exit 1
    fi
```

If you're already using `tx-pane` in interactive agent loops, getting the
exact same execution semantics in CI is a single environment-variable
change away (`TMUX_TMPDIR`, `TX_PANE_HOME` to isolate the test run's state).

## 7. "I want to record what my agent did, for audit / debugging"

Everything `tx-pane` does is on disk, in human-readable formats:

- `~/.tx-pane/logs/<pane>.log` — raw pipe-pane log with markers visible.
- `~/.tx-pane/offsets.json` — per-pane state (runs, bookmarks, handles).
- `~/.tx-pane/compact.jsonl` — compaction telemetry (cmd_head only — no
  args, no output bytes).

After-the-fact forensics:

```sh
tx-pane runs <pane> --limit 100             # list of run-ids with exit codes
tx-pane output <pane> <run-id> --full       # full untruncated output of one run
tx-pane compact-stats --since 2026-05-15    # what was elided, by tool
grep -rn "TX_END" ~/.tx-pane/logs/          # all run terminations across panes
```

`tx-pane output --full` is the canonical "give me the bytes as they were"
escape hatch for any auditor or postmortem.

## 8. "I want to parallelize work across multiple panes"

Many panes, one `tx-pane` process per call:

```sh
# fan out
panes=()
for host in web-01 web-02 web-03; do
  p=$(tx-pane new "$host")
  tx-pane run "$p" "ssh deploy@$host" && tx-pane hook-install "$p"
  tx-pane exec "$p" "systemctl restart app.service" >/dev/null
  panes+=("$p")
done

# fan in
for p in "${panes[@]}"; do
  last_run=$(tx-pane runs "$p" --limit 1 --json | jq -r .[0].run_id)
  tx-pane wait-run "$p" "$last_run"
  tx-pane output "$p" "$last_run" --json | jq -c '{pane:.pane, exit:.exit}'
done
```

The `~/.tx-pane/.lock` flock around `offsets.json` makes this safe — N
concurrent `tx-pane` invocations against different panes are fine; N
concurrent against the same pane will serialize on the read-modify-write
of the offsets file, but each individual command is still atomic.

## 9. "I want to share state between dev and production diagnostics"

`tx-pane ls` enumerates all panes. `tx-pane info <pane>` is a one-shot
human-readable dump. Add to your prompt or status bar:

```sh
# in ~/.bashrc
__tx_ps1() {
  local n
  n=$(tx-pane ls --format tsv 2>/dev/null | wc -l)
  [ "$n" -gt 0 ] && printf '[tx-pane:%s]' "$n"
}
PS1='$(__tx_ps1)\u@\h:\w\$ '
```

So you always know there's a pane the agent is using before you `cd`
somewhere thinking the shell is "fresh".

---

## What `tx-pane` doesn't fit

Be honest about scope:

- **Throwaway one-shot commands.** If your agent does
  `bash -c "ls /tmp"` once and never again, `tx-pane new`/`tx-pane run` is
  overkill. Use bash directly.
- **Pure file editing.** `tx-pane` doesn't replace an editor tool. Pair it
  with an Edit/Write tool for file content; use `tx-pane write` only when
  you need atomicity + reload + audit.
- **Sub-100ms command loops.** `tx-pane run` adds ~40ms of overhead vs
  `bash -c`. If your workload is millions of trivial commands, this
  isn't your tool.
- **Multi-user.** State files in `~/.tx-pane/` are per-UID. There's no
  ACL layer; if multiple humans need to drive the same pane, give
  them a shared user.
- **Windows-native.** WSL works fine. Cmd.exe / PowerShell do not.

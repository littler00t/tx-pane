# tx-pane — comparison with related tools

This doc exists for two audiences:

1. **Humans deciding whether to adopt `tx-pane`** — when is it the right
   tool, and what existing thing should you keep using instead?
2. **Agents that have been asked "can we do this with tx-pane?" and need to
   know when the answer is "no, use X instead"**.

Comparisons are organized by the *role* the alternative plays, not by
project name, so the reader can locate themselves quickly.

---

## Role 1: "the bash tool my agent calls"

This is the most common starting point. Your agent has a tool like
Anthropic's [Bash tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/bash-tool),
OpenAI's code interpreter, or a custom subprocess-per-call wrapper.

| Concern | Generic bash tool | `tx-pane` |
|---|---|---|
| Pane state across calls | none (each call: new shell) | persistent per-pane |
| `cd`, `export`, virtualenv survives next call | no | yes |
| Exit code accuracy | depends on impl; usually prompt-regex or subprocess return | marker hook (real exit code) |
| Background commands | recent additions (`run_in_background`+`BashOutput` in Claude Code; equivalent elsewhere) | `tx-pane exec` + `tx-pane wait-run` |
| Output size handling | character truncation | 18 normalizers + 5 layers + recoverable handles |
| Interrupt running command cleanly | varies (often kill the subprocess) | `tx-pane kill-run` (sends C-c; preserves marker) |
| Human can watch in real-time | no | `tmux attach -t tx-pane` |
| Safety rails (allowlist / redact / confirm) | varies per implementation | yes, configurable |

**When to keep the generic tool:** if your agent's commands are all
short (`git status`, `npm test`), output is small (≤ a few KB), and you
never need to interrupt or watch a running command. Don't add `tx-pane` for
the sake of it.

**When to move to `tx-pane`:** the moment you find yourself doing any of:
re-implementing exit-code detection, manually truncating output to
fit in context, juggling multiple shells with environment leaks, or
wanting to watch the agent's actions while it works.

References: Claude Code background commands now expose
`shell_id`/`BashOutput`/`KillBash`, which solves the "is it still
running" problem but not the prompt-pattern, compaction, or
human-attach problems. ([1](https://github.com/ruvnet/ruflo/wiki/background-commands),
[2](https://platform.claude.com/docs/en/agents-and-tools/tool-use/bash-tool))

---

## Role 2: "a stateful terminal session, accessed by an MCP server"

Examples: [mcpterm](https://github.com/dwrtz/mcpterm),
[ShellKeeper](https://news.ycombinator.com/item?id=45847880),
[Shell Exec](https://mcpmarket.com/server/shell-exec),
[terminals (kirby44)](https://github.com/kirby44/terminals).

These all give your agent a persistent shell process (usually via
`node-pty` or Python `ptyprocess`), accessed over an MCP transport.

| Concern | MCP shell servers | `tx-pane` |
|---|---|---|
| Persistent shell across calls | yes | yes |
| Multiple panes in parallel | varies | yes (one tmux pane each) |
| TUI app support | yes (PTY-based) | yes (tmux is a real terminal) |
| Exit code mechanism | prompt-regex / heuristic | marker hook |
| Survives `C-c` cleanly | usually no (regex confusion) | yes |
| Nested-shell exit codes (ssh, sudo -i) | inherited brokenness from prompt-regex | `tx-pane hook-install` reinstalls the hook |
| Output compaction | none | 18 normalizers + 5 layers |
| Elision recoverable | n/a (no elision) | yes via `h-XXX` handles |
| Human-attach to live session | no (PTY is internal) | yes (`tmux attach -t tx-pane`) |
| Requires running daemon/server | yes (MCP) | no (single Python script, file locks) |
| Transport | MCP / HTTP | local CLI; pipe in any wrapper you want |

**When MCP shell servers are better:** if you're already invested in
MCP, your agent infrastructure is HTTP-first, or you want
plug-and-play integration with the MCP ecosystem (resource browsers,
prompt templates, etc.). `tx-pane` is intentionally not an MCP server —
shell-out composes with anything, but it isn't one-line drop-in for
an MCP host.

**When `tx-pane` is better:** when the marker-protocol reliability or the
compaction matter more than the integration story. The single most
common feedback from users coming from MCP shell servers is "the
`[exit:?]` problem in nested shells finally went away" — that's
specifically the case where pattern-matching the prompt is hardest
and the marker hook is easiest.

It's plausible to wrap `tx-pane` behind an MCP shim; nothing in `tx-pane`'s
design prevents it.

---

## Role 3: "the reference agent in a benchmark"

The canonical example is [Terminus](https://www.tbench.ai/news/terminus),
the [Terminal-Bench](https://www.tbench.ai/) reference agent.

Terminus is explicitly **neutral by design**: it gets exactly one tool
— an interactive tmux session — and drives keystrokes directly. The
agent reads the pane content via `tmux capture-pane`. No exit-code
helper, no file-editing helper, no compaction. The goal is to
benchmark the *model*, not the agent framework.

`tx-pane` is the opposite premise: give the agent a more opinionated
interface so the *system* is more reliable.

**When to use Terminus / a Terminus-style setup:** you're evaluating
how good a model is at terminal work. You want fair comparison
across models. You explicitly do not want a "scaffolding" that hides
the model's failures.

**When to use `tx-pane`:** you're shipping an agent that has to be
reliable in production. You're paying for tokens. You'd rather have
the system handle "did this exit cleanly?" than have the model figure
it out from the screen each time.

Terminal-Bench's findings reinforce why `tx-pane` exists: even frontier
models score <65% on Terminal-Bench 2.0, and a large fraction of
failures are reading-the-terminal mistakes, not reasoning mistakes.
The bet `tx-pane` makes is that you can buy back a lot of those failures
with structured interfaces.

---

## Role 4: "a tmux-aware assistant that watches the human"

Examples: [TmuxAI](https://tmuxai.dev/),
[ATM / agent-deck](https://github.com/asheshgoplani/agent-deck),
many MCP servers that expose `capture-pane` read-only.

The model here is: the *human* is driving tmux, and the AI is
*looking over their shoulder*. The agent observes; it doesn't (by
default) type.

**When to use a tmux-observer tool:** you're an interactive user and
you want a copilot that can summarize the pane, suggest the next
command, or explain an error. The agent never takes actions you
didn't approve.

**When to use `tx-pane`:** you want the agent to *drive* — kick off
commands, parse output, retry, deploy. The human can still attach to
watch (handoff/resume) but the default direction of control is
agent → terminal.

These are complementary, not competing. You could plausibly run
TmuxAI for your interactive shell and `tx-pane` for your CI/agent
workflows, and never have them interact.

---

## Role 5: "a sandboxed code-execution backend"

Example: [SWE-ReX](https://github.com/swe-agent/SWE-ReX), the runtime
that powers SWE-agent. It exposes a shell inside a Docker / cloud
sandbox; the agent can `pip install`, run tests, edit files. Locally
or remotely.

`tx-pane` and SWE-ReX target different layers:

- SWE-ReX is concerned with **where** the shell runs (sandbox / VM
  isolation, dependency installation, parallel runners).
- `tx-pane` is concerned with **how** the shell is talked to (marker
  protocol, compaction, refuse-on-busy, handoff).

They compose. You could run `tx-pane` inside a SWE-ReX sandbox, or treat
`tx-pane` as the local "outer" shell that drives SWE-ReX as one tool.
There's no overlap in feature set.

**When to use SWE-ReX without `tx-pane`:** you want one-shot,
sandbox-per-task isolation (e.g. SWE-bench-style tasks where each
task is a fresh repo). The sandbox is the right boundary.

**When to use `tx-pane` without SWE-ReX:** you trust the host (your own
laptop, a CI runner) and you want long-lived state across many calls.

References: SWE-agent's [ACI paper](https://arxiv.org/abs/2405.15793)
is the foundational case for "language models are a new kind of end
user" — same thesis `tx-pane` operates on, different surface area.

---

## Role 6: "expect / pexpect"

These predate the LLM era by two decades and are the OG of
"automate a shell session by pattern-matching its output". The
[pexpect docs](https://pexpect.readthedocs.io/en/stable/overview.html)
list the gotchas honestly: the `$` regex doesn't mean end-of-line,
look-ahead is impossible, password timing is tricky, TTY buffer
sizes vary across platforms.

Most MCP shell servers and many agent-shell tools are pexpect
descendants — node-pty + a prompt regex is structurally the same
thing.

`tx-pane`'s marker hook is the explicit answer to "how do we stop
pattern-matching the prompt". You give up generality (any shell with
a configurable prompt-return hook) and you get reliability (exit
codes are always real).

**When to keep pexpect:** legacy automation, or talking to a tool
that does *not* run in a normal shell (a custom REPL, an embedded
device's bootloader, a network device's CLI without a real shell).
The marker hook needs a real `PROMPT_COMMAND`/`precmd`/`fish_postexec`
to install into. If that's not available, pexpect is still the right
hammer.

---

## Decision matrix

If you're an agent and the user has asked you to "set up shell access":

```
Does the user already have an MCP host?
├─ yes → propose mcpterm / ShellKeeper unless they specifically want compaction or human-attach.
└─ no
   │
   Is this for benchmarking models?
   ├─ yes → use Terminus-style direct tmux. Do not add a scaffold.
   └─ no
      │
      Does the agent need persistent state across calls?
      ├─ no → keep the generic bash tool. Adding tx-pane is overhead you won't use.
      └─ yes
         │
         Will output frequently exceed ~4KB, or are there `ssh`/`sudo -i`/`docker exec` steps?
         ├─ yes → tx-pane is the strongest fit.
         └─ no  → either tx-pane or an MCP shell server works; pick by ecosystem.
```

If you're a human deciding adoption:

- Set up `tx-pane` in a side directory, point your agent at it for one
  workflow (a deploy, a smoke test), measure: did "`[exit:?]`"
  failures drop? Did average tool-result size drop?
- If both are flat, you don't have the failure modes `tx-pane` solves.
  Save the operational cost.
- If either dropped noticeably, the value is real and likely
  expanding.

---
name: tx-pane-demo
description: >-
  Run a live co-working demo of tx-pane where the agent drives a tmux pane while
  the user attaches in their own terminal to watch and take over. Use when the
  user wants a demo or walkthrough, to "see tx-pane in action", to try the
  human-in-the-loop handoff/resume flow, or to pair with the agent on a shared
  pane.
argument-hint: "(optional: a pane name)"
disable-model-invocation: true
allowed-tools:
  - Bash(./tx-pane *)
  - Bash(tx-pane *)
---

# tx-pane co-working demo

A guided, human-in-the-loop demo. You (the agent) drive a pane; the user
attaches in their own terminal to watch and take over live. **Narrate each step
so the user can follow along, and pause when a step asks them to act.**

Resolve the command once: prefer `tx-pane` on `PATH`, else `./tx-pane` from the
repo root. Pane name = `$ARGUMENTS` if given, else `demo`.

> You drive the pane through `tx-pane`. **Never run `tmux attach` yourself** —
> attaching is the *human's* side of the demo.

## Act 1 — open a shared pane and invite the user to watch

1. `pane=$(tx-pane new demo)`
2. Tell the user to attach **in their own terminal**:
   ```sh
   tmux attach -t tx-pane        # detach anytime with Ctrl-b d
   ```
3. Run a couple of visible commands so they see live updates on their screen:
   - `tx-pane run "$pane" "echo hello from the agent && date"`
   - `tx-pane exec "$pane" "for i in 1 2 3 4 5; do echo tick \$i; sleep 1; done"`
     then `tx-pane tail "$pane"` once or twice to show streaming output.

## Act 2 — hand the pane to the human

1. `tx-pane handoff "$pane"` — explain that tx-pane is now **paused**: the user
   can type directly in the attached pane and the agent won't touch it.
2. Ask the user to run something themselves (e.g. `whoami`, `ls`, edit a file).
   **Wait** until they say they're done.

## Act 3 — resume and show continuity

1. `tx-pane resume "$pane"` — tx-pane is back in control; the read offset skips
   the gap so there's no double-counting.
2. `tx-pane tail "$pane" --all` — show you can see everything that happened
   while they had control.
3. `tx-pane runs "$pane"` — the audit trail of every tracked run.

## Act 4 — show compaction + structured output

- `tx-pane run "$pane" "df -h"` — point out the per-tool normalizer and the
  `[tx-pane:compact ...]` footer (typical 45–60% savings).
- `tx-pane run --json "$pane" "uname -a" | jq .exit` — a real, structured exit
  code, not a guess.

## Wrap up

- **Ask first**, then `tx-pane kill "$pane"` (the user may want to keep
  exploring).
- Summarize what was shown: persistent named panes, live attach, handoff/resume,
  compaction, and structured exit codes.
- Point to `docs/tx-doc-use-cases.md` for more real-world scenarios.

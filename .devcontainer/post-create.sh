#!/usr/bin/env bash
# Runs once after the dev container is built. Anything that needs npm or any
# devcontainer feature MUST live here, not in the Dockerfile.
set -euo pipefail

cd /workspace

# Named volumes mount as root by default; hand /home/vscode/.claude to the
# vscode user so claude-code can write credentials/settings.
echo "==> ensuring ~/.claude is owned by vscode"
sudo chown -R vscode:vscode /home/vscode/.claude || true

# claude-code goes here (not the Dockerfile): npm is only on PATH after the
# Node devcontainer feature has been applied on top of the built image.
echo "==> installing claude-code globally (inside container)"
npm install -g @anthropic-ai/claude-code

echo "==> installing codex globally (inside container)"
npm install -g @openai/codex

# Warm uv's PEP-723 dep cache so the first ./run-tests / ./tx invocation
# isn't a cold download. --collect-only short-circuits pytest before it runs
# any tests; safe to call here (it exits cleanly).
echo "==> warming uv cache for ./run-tests"
./run-tests --collect-only > /dev/null

# Python 3.11 + uv + tmux. Node 20 / npm are layered on via the devcontainer
# Node feature (see devcontainer.json) — NOT here, because the NodeSource
# one-liner does not reliably leave npm on PATH inside this base image.
#
# tx is a single PEP-723 Python script that drives tmux. The runtime deps are
# Python 3.11+, uv, tmux, and a POSIX-ish shell. The heavier T4 normalizer
# toolbox (docker.io, libvirt-clients, zfsutils, systemd, …) lives in the
# top-level Dockerfile / `./run-tests-docker` and is intentionally NOT
# replicated here — keeps the dev container lean.

FROM mcr.microsoft.com/devcontainers/python:1-3.11-bookworm

# uv — used to run inline-script Python files (`# /// script` blocks),
# which is how `./tx` and `./run-tests` execute.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv  /usr/local/bin/uv \
    && mv /root/.local/bin/uvx /usr/local/bin/uvx

# Runtime deps for tx (tmux) + QoL CLI tools.
#
# NOTE: the MS python base image preconfigures a Yarn apt source whose GPG
# key has since rotated, which makes `apt-get update` fail fatally. We don't
# need Yarn (npm comes from the Node feature), so drop the Yarn source
# before updating.
RUN rm -f /etc/apt/sources.list.d/yarn.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
         tmux procps less jq vim-tiny \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/vim.tiny /usr/local/bin/vim

USER vscode
ENV PATH="/usr/local/bin:${PATH}"

# Minimal Linux env for the tx test suite. Single-stage, ~150 MB.
#
# Why this exists:
# - tx ships only ever ran against macOS during development; this image
#   gives us a reproducible Linux run with modern GNU userland tooling
#   (bash 5 / GNU chmod / GNU sha256sum) so we catch BSD-vs-GNU divergence
#   the host can't see.
# - Use `./run-tests-docker` from the repo root; it builds and runs.
FROM python:3.11-slim-bookworm

# Runtime deps:
#   tmux/bash/zsh/procps/vim-tiny/less/ca-certs — base tx-test scaffold
#
# Plus the sysadmin toolbox needed by tx_compact's per-tool normalizer
# tests (T4 tier). Every test in tests/test_normalizer_real_*.py runs
# only when TX_IN_DOCKER=1 (set below), so the host install stays
# minimal. Tool list maps to the §5.3 normalizer roster:
#   iproute2          → ss
#   iptables          → iptables / iptables-legacy
#   util-linux        → lsblk, last, lastlog, dmesg, du, df, find
#   coreutils         → df, du basic forms
#   findutils         → find
#   smartmontools     → smartctl
#   docker.io         → docker CLI (daemon not started)
#   libvirt-clients   → virsh CLI (daemon not started)
#   qemu-utils        → qemu-img helper for virsh tests
#   zfsutils-linux    → zpool/zfs CLI (kernel module won't load, tests skip)
#   systemd / sysv    → systemctl/journalctl CLIs (no PID 1, tests degrade)
# zfsutils-linux lives in Debian's `contrib` repo (not enabled by default).
# Enable it so the zpool normalizer test can install the CLI. The kernel
# module isn't installable in the container regardless, so the test still
# exercises the CLI-only "modprobe: FATAL" path.
RUN echo "deb http://deb.debian.org/debian bookworm main contrib" \
        > /etc/apt/sources.list.d/contrib.list && \
    apt-get update && apt-get install -y --no-install-recommends \
        tmux bash zsh procps vim-tiny less ca-certificates \
        iproute2 iptables psmisc \
        util-linux coreutils findutils \
        smartmontools \
        docker.io \
        libvirt-clients qemu-utils \
        zfsutils-linux \
        systemd systemd-sysv && \
    rm -rf /var/lib/apt/lists/* && \
    ln -sf /usr/bin/vim.tiny /usr/local/bin/vim

# Marker env var so T4 tests can guard with a single check. Host runs of
# `./run-tests` will NOT have this set and so will skip the tool-driven
# tests; only `./run-tests-docker` exercises the §5.3 normalizers.
ENV TX_IN_DOCKER=1

# uv: PEP-723 script runner used by ./run-tests and ./tx
RUN pip install --no-cache-dir uv

WORKDIR /work
# Copy the project. .dockerignore excludes .git, .pytest_cache, the
# ~/.tx state directory if present, and the prompt_impl_stage*.md
# handover docs (not needed at runtime).
COPY . /work/
RUN chmod +x ./tx ./run-tests

# Warm uv's PEP-723 dep cache for the test runner so the first
# ./run-tests invocation inside the container isn't a cold download.
RUN ./run-tests --collect-only > /dev/null

# Default: run the full suite. Override via `docker run … <pytest-args>`.
ENTRYPOINT ["./run-tests"]
CMD ["-q"]

"""tx_core.commands.admin — admin / maintenance commands (config / compact-stats / hook-install / write / maintain)

Extracted verbatim from the monolithic `tx` script during the modular
refactor. Each `@cli.command()` registers itself on the shared `cli`
root group on import.
"""

from __future__ import annotations

from tx_core.commands._common import *  # noqa: F401,F403


@cli.command(
    name="hook-install",
    short_help="Install the v2 marker hook in the pane's current foreground shell.",
    help=(
        "Send the SHELL_INIT_SETUP script to whatever shell is at the prompt now.\n\n"
        "Use after entering a nested interactive shell (ssh, sudo -i, su -, "
        "docker exec -it, kubectl exec -it, etc.) so subsequent tx run / tx exec\n"
        "calls observe markers and capture exit codes.\n\n"
        "A self-test runs after install: a probe wrap is sent and we look for "
        "its marker. If it appears, the hook is wired correctly."
    ),
)
@click.argument("pane")
@click.option("--no-verify", is_flag=True, default=False, help="skip the probe self-test")
@click.option("--timeout", "timeout", type=float, default=5.0, help="probe wait timeout in seconds")
@click.option(
    "--shell",
    "shell",
    type=click.Choice(["bash", "zsh", "sh", "fish"], case_sensitive=False),
    default=None,
    help="hook syntax for the foreground shell (default: bash/zsh form, fits sh)",
)
def cmd_hook_install(pane: str, no_verify: bool, timeout: float, shell: str | None) -> None:
    cfg = load_config()
    max_history = int(cfg["defaults"].get("max_run_history", 100))
    with offsets_lock():
        offsets = load_offsets()
        state, server, tmux_pane, log_path = _resolve_pane_for_input(offsets, pane)
        if state.get("paused_at"):
            err(f"pane '{pane}' is paused (handoff); run 'tx resume {pane}' first")
        finalize_runs(offsets, pane, max_history, cfg["defaults"])
        info = pane_state(server, offsets[pane], pane, cfg["defaults"])
        if info["status"] != "idle":
            err(
                f"pane '{pane}' is {info['status']}; hook-install requires an idle pane "
                f"(at a prompt). Try 'tx kill-run' or 'tx wait-run' first."
            )

    init_snippet = shell_init_setup_for(shell.lower() if shell else None)
    try:
        tmux_pane.send_keys(init_snippet, enter=True, suppress_history=False, literal=True)
    except Exception as e:
        err(f"send-keys failed: {e}")
    time.sleep(0.25)

    if no_verify:
        click.echo(f"[installed: hook setup sent to pane '{pane}' (not verified)]")
        return

    probe_id = make_run_id()
    probe_start = log_path.stat().st_size
    try:
        tmux_pane.send_keys(
            wrap_command("true", probe_id), enter=True, suppress_history=False, literal=True
        )
    except Exception as e:
        err(f"probe send-keys failed: {e}")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with open(log_path, "rb") as f:
            f.seek(probe_start)
            raw = f.read()
        if find_run_marker(raw, probe_id):
            with offsets_lock():
                offsets = load_offsets()
                if pane in offsets:
                    offsets[pane]["hook_ok"] = True
                    save_offsets(offsets)
            click.echo(f"[installed: hook verified in pane '{pane}']")
            return
        time.sleep(0.1)
    warn(
        f"hook setup sent but probe marker '{probe_id}' did not appear within "
        f"{int(timeout)}s. The shell may not support PROMPT_COMMAND/precmd; tx run will "
        f"fall back to prompt-pattern detection (exit codes show as '?')."
    )


@cli.command(
    name="config",
    short_help="Print active configuration.",
    help="Print the active configuration in TOML format, plus tx and tmux versions.",
)
def cmd_config() -> None:
    cfg = load_config()
    out = tomli_w.dumps(cfg)
    click.echo(f"# tx version: {VERSION}")
    click.echo(f"# tmux version: {_detect_tmux_version()}")
    click.echo(f"# active config file: {CONFIG_PATH}")
    click.echo(f"# offsets path: {OFFSETS_PATH}")
    click.echo(f"# logs dir: {LOGS_DIR}")
    click.echo(out.rstrip())


# ----- compact-stats -----
@cli.command(
    name="compact-stats",
    short_help="Summarise compaction telemetry from ~/.tx/compact.jsonl.",
    help=(
        "Read the per-call telemetry log and emit aggregates. Drives the\n"
        "decision about which normalizers to ship next (and which under-\n"
        "perform). Read-only by default; --forget wipes the log.\n\n"
        "Privacy: only the *first word* of each command is recorded\n"
        "(arguments/paths/secrets are not). No network upload."
    ),
)
@click.option("--weak", is_flag=True, default=False,
              help="show cmd_heads with <30% savings (filter-improvement candidates)")
@click.option("--passthrough", is_flag=True, default=False,
              help="show top cmd_heads hitting tier 3 (missing-normalizer candidates)")
@click.option("--since", "since", type=str, default=None,
              help="only consider records with ts >= this ISO-8601 string")
@click.option("--limit", "limit", type=int, default=20,
              help="cap rows in the by-cmd-head table")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="emit a single JSON object")
@click.option("--forget", is_flag=True, default=False,
              help="wipe ~/.tx/compact.jsonl and its backup (no read)")
def cmd_compact_stats(
    weak: bool,
    passthrough: bool,
    since: str | None,
    limit: int,
    as_json: bool,
    forget: bool,
) -> None:
    from tx_compact import telemetry as _tel  # local import — keeps top simple
    if forget:
        n = _tel.wipe()
        click.echo(f"removed {n} telemetry file(s)")
        return
    records = _tel.read_all()
    agg = _tel.aggregate(records, since_ts=since)
    if as_json:
        click.echo(json.dumps(agg, indent=2, sort_keys=True))
        return
    if agg["count"] == 0:
        click.echo("[empty: no telemetry records yet — run with --terse to populate]")
        return
    overall_saved = agg["saved_pct"]
    click.echo(f"# total: {agg['count']} calls, "
               f"{agg['in_bytes']:,}B → {agg['out_bytes']:,}B "
               f"(saved {overall_saved:.1f}%)")
    # Per-cmd-head table
    items = list(agg["by_cmd_head"].items())
    if weak:
        items = [(h, s) for h, s in items if s.get("saved_pct", 0.0) < 30.0]
    items.sort(key=lambda kv: -kv[1]["count"])
    items = items[:limit]
    if items:
        click.echo("# cmd_head           calls  in→out                saved")
        for head, s in items:
            click.echo(f"  {head:<18} {s['count']:>5}  "
                       f"{s['in']:>8,}B → {s['out']:>8,}B  "
                       f"{s['saved_pct']:>5.1f}%")
    if passthrough:
        click.echo()
        if agg["passthrough_cmd_heads"]:
            click.echo("# tier-3 passthrough (no normalizer matched)")
            for head, n in agg["passthrough_cmd_heads"][:limit]:
                click.echo(f"  {head:<18} {n:>5} passthrough hits")
        else:
            click.echo("# no tier-3 passthrough records")


# ----- write (atomic file deploy with hash verify) -----

_OCTAL_MODE_RE = re.compile(r"^[0-7]{3,4}$")


_OWNER_SPEC_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(:[A-Za-z_][A-Za-z0-9_-]*)?$")


_SHA256_HEX_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")


@cli.command(
    name="write",
    short_help="Atomically deploy a local file to a remote path via the pane.",
    help=(
        "Stage a file in the target directory (heredoc + bracketed paste),\n"
        "sha256-verify it, optionally chmod/chown, then atomically `mv` it\n"
        "into place. Optional --reload-cmd runs after.\n\n"
        "Refuses if <remote-path> already exists unless --overwrite is set.\n"
        "Refuses on busy / paused / dead panes and on fish-shell panes (no\n"
        "heredoc syntax). With --sudo, all remote operations run under\n"
        "`sudo -n`; ensure the user has valid cached credentials first\n"
        "(e.g. via `tx sudo` once).\n\n"
        "Returns one of:\n"
        "  [written: <path> (<bytes>, sha256:<first8>...)]      success\n"
        "  [error: ...]                                         abort\n"
        "Every internal step is recorded as a run visible in `tx runs`."
    ),
)
@click.argument("pane")
@click.argument("remote_path")
@click.option("--file", "local_path", type=str, required=True, help="local source file")
@click.option("--sudo", "use_sudo", is_flag=True, default=False, help="run remote ops under `sudo -n`")
@click.option("--mode", "mode_str", type=str, default=None, help="octal mode (e.g. 644) applied after staging")
@click.option("--owner", "owner_str", type=str, default=None, help="user[:group] applied after staging")
@click.option("--reload-cmd", "reload_cmd", type=str, default=None, help="command to run after successful move")
@click.option("--overwrite", is_flag=True, default=False, help="permit replacing an existing remote-path")
@click.option("--diff", "show_diff", is_flag=True, default=False, help="emit a unified diff of old vs new before writing")
@click.option("--timeout", "timeout", type=float, default=None, help="per-step marker timeout (default = [defaults] timeout)")
@click.option("--yes", "yes", is_flag=True, default=False, help="skip confirm-pattern prompts for the synthetic write command")
def cmd_write(
    pane: str,
    remote_path: str,
    local_path: str,
    use_sudo: bool,
    mode_str: str | None,
    owner_str: str | None,
    reload_cmd: str | None,
    overwrite: bool,
    show_diff: bool,
    timeout: float | None,
    yes: bool,
) -> None:
    cfg = load_config()
    timeout = timeout if timeout is not None else float(cfg["defaults"]["timeout"])

    # ---- validate inputs ----
    if mode_str is not None and not _OCTAL_MODE_RE.match(mode_str):
        err(f"--mode '{mode_str}' is not a 3- or 4-digit octal (e.g. 644, 0750)")
    if owner_str is not None and not _OWNER_SPEC_RE.match(owner_str):
        err(f"--owner '{owner_str}' must be user[:group], alnum / _ / - only")

    src = Path(local_path).expanduser()
    if not src.is_file():
        err(f"--file '{local_path}': not a regular file")
    try:
        content = src.read_bytes()
    except OSError as e:
        err(f"cannot read --file '{local_path}': {e}")
    if not content:
        err(f"--file '{local_path}' is empty; tx write refuses to deploy a zero-byte file")

    # heredoc always emits a trailing newline after the body; match local hash
    # to what'll actually land on disk.
    if not content.endswith(b"\n"):
        content = content + b"\n"
    local_hash = hashlib.sha256(content).hexdigest()
    n_bytes = len(content)

    # ---- pane preflight ----
    synth_cmd = f"tx write {remote_path}"
    check_confirm(synth_cmd, cfg, yes)
    offender = check_allowlist(synth_cmd, cfg, pane_id=pane)
    if offender is not None:
        err(f"'{offender}' not in command_allowlist — edit ~/.tx/config.toml")

    max_history = int(cfg["defaults"].get("max_run_history", 100))
    with offsets_lock():
        offsets = load_offsets()
        state, server, tmux_pane, log_path = _resolve_pane_for_input(offsets, pane)
        if state.get("paused_at"):
            err(f"pane '{pane}' is paused (handoff); run 'tx resume {pane}' first")
        finalize_runs(offsets, pane, max_history, cfg["defaults"])
        info = pane_state(server, offsets[pane], pane, cfg["defaults"])
        if info["status"] in ("running", "tui"):
            err(busy_error_message(pane, info))
        if info["status"] == "dead":
            err(f"pane '{pane}' shell is dead — recreate with 'tx kill {pane}' then 'tx new {pane}'")
        if state.get("shell") == "fish":
            err("tx write requires bash/zsh/sh — fish has no heredoc syntax")

    # ---- step helpers (each is a marker-tracked run via the pane shell) ----
    quoted_target = shlex.quote(remote_path)
    target_dir = os.path.dirname(remote_path) or "/"
    quoted_target_dir = shlex.quote(target_dir)

    def step(label: str, cmd: str) -> tuple[int | None, str]:
        ex, out = _internal_marker_run(pane, cmd, cfg, timeout)
        if ex is None:
            err(f"'{label}' timed out after {int(timeout)}s; check pane state")
        return ex, out

    # ---- 1. target directory must exist ----
    # `[ -d ... ]` propagates 0/1 as the run's exit code; using the marker's
    # exit code avoids parsing stdout (which would contain the shell's echo
    # of the typed command, so sentinel-string matches like 'TX_DIR_OK' end
    # up in the captured text either way).
    ex, _out = step("dir-check", f"[ -d {quoted_target_dir} ]")
    if ex != 0:
        err(f"remote directory {target_dir!r} does not exist on this pane")

    # ---- 2. optional diff of existing remote file vs new content ----
    if show_diff:
        sudo = _sudo_prefix(use_sudo)
        ex, old_out = step("diff-fetch", f"{sudo}cat -- {quoted_target} 2>/dev/null")
        # Captured text leads with the shell's echo of the typed command; drop
        # that first line so the diff body is just the file content.
        if "\n" in old_out:
            old_text = old_out.split("\n", 1)[1] if ex == 0 else ""
        else:
            old_text = ""
        # No trailing prompt-line residue: marker-protocol slicing ends at
        # the marker, so old_text is exactly the cat output (possibly empty
        # if the file was empty or missing).
        if ex != 0:
            click.echo(f"[diff: target {remote_path!r} does not exist yet]")
        else:
            diff = "".join(
                difflib.unified_diff(
                    old_text.splitlines(keepends=True),
                    content.decode("utf-8", errors="replace").splitlines(keepends=True),
                    fromfile=f"a{remote_path}",
                    tofile=f"b{remote_path}",
                    n=3,
                )
            )
            if diff:
                click.echo(diff, nl=False)
            else:
                click.echo(f"[diff: no changes vs current {remote_path}]")

    # ---- 3. existence gate ----
    if not overwrite:
        ex, _out = step("exists-check", f"[ -e {quoted_target} ]")
        if ex == 0:  # exit 0 from `[ -e PATH ]` means PATH exists
            err(
                f"{remote_path!r} already exists — pass --overwrite to replace it"
            )

    # ---- 4. stage in the target directory so the final `mv` is a rename ----
    stage_basename = f".tx-write-{secrets.token_hex(4)}"
    stage_path = os.path.join(target_dir, stage_basename)
    quoted_stage = shlex.quote(stage_path)
    heredoc_term = f"TX_WRITE_HEREDOC_{secrets.token_hex(4)}"
    sudo = _sudo_prefix(use_sudo)

    # Cat with a redirect goes through the shell; sudo can't elevate that.
    # `sudo -n tee` reads the heredoc from stdin and writes as root.
    if use_sudo:
        prelude = f"{sudo}tee -- {quoted_stage} >/dev/null <<'{heredoc_term}'"
    else:
        prelude = f"cat > {quoted_stage} <<'{heredoc_term}'"

    paste_body = content + heredoc_term.encode("ascii") + b"\n"

    ex, _out = _internal_paste_then_marker(
        pane, prelude, paste_body, cfg, max(timeout, 30.0)
    )
    if ex is None:
        err(f"stage write timed out for {stage_path!r}; pane state unclear")
    if ex != 0:
        # Best-effort cleanup of the (possibly partial) stage file.
        step("stage-cleanup", f"{sudo}rm -f {quoted_stage} 2>/dev/null")
        err(f"staging cat/tee returned exit={ex}; aborting")

    # ---- 5. verify hash on the staged file ----
    # Portable: prefer sha256sum (GNU coreutils, Linux + recent macOS) and
    # fall back to shasum -a 256 (Perl, always on macOS).
    sha_cmd = (
        f"if command -v sha256sum >/dev/null 2>&1; then "
        f"{sudo}sha256sum {quoted_stage}; else "
        f"{sudo}shasum -a 256 {quoted_stage}; fi"
    )
    ex, sha_out = step("sha256-verify", sha_cmd)
    m = _SHA256_HEX_RE.search(sha_out)
    if not m:
        step("stage-cleanup", f"{sudo}rm -f {quoted_stage} 2>/dev/null")
        err(
            "could not parse sha256 output for staged file; got: "
            + sha_out.strip()[:200]
        )
    remote_hash = m.group(1).lower()
    if remote_hash != local_hash:
        step("stage-cleanup", f"{sudo}rm -f {quoted_stage} 2>/dev/null")
        err(
            f"sha256 mismatch — local={local_hash[:16]}… "
            f"remote={remote_hash[:16]}… ; aborting"
        )

    # ---- 6. optional chmod / chown on the stage (preserves perms across mv) ----
    # Note: BSD chmod/chown don't honour `--` as an options terminator. The
    # staging basename is `.tx-write-<hex>`, never starts with `-`, so we can
    # safely omit `--` here.
    if mode_str is not None:
        ex, _out = step("chmod", f"{sudo}chmod {mode_str} {quoted_stage}")
        if ex != 0:
            step("stage-cleanup", f"{sudo}rm -f {quoted_stage} 2>/dev/null")
            err(f"chmod {mode_str} on stage returned exit={ex}")
    if owner_str is not None:
        ex, _out = step("chown", f"{sudo}chown {owner_str} {quoted_stage}")
        if ex != 0:
            step("stage-cleanup", f"{sudo}rm -f {quoted_stage} 2>/dev/null")
            err(f"chown {owner_str} on stage returned exit={ex}")

    # ---- 7. atomic move into place ----
    ex, _out = step("mv", f"{sudo}mv -f {quoted_stage} {quoted_target}")
    if ex != 0:
        step("stage-cleanup", f"{sudo}rm -f {quoted_stage} 2>/dev/null")
        err(f"mv to {remote_path!r} returned exit={ex}")

    # ---- 8. optional reload ----
    if reload_cmd:
        ex, _out = step("reload-cmd", f"{sudo}{reload_cmd}")
        if ex != 0:
            click.echo(
                f"[written: {remote_path} ({n_bytes} bytes, sha256:{local_hash[:8]}…)]"
            )
            err(f"[warning: reload-cmd '{reload_cmd}' returned exit={ex}]")

    click.echo(
        f"[written: {remote_path} ({n_bytes} bytes, sha256:{local_hash[:8]}…)]"
    )


# ----- maintain (log rotation + age sweep) -----
@cli.command(
    name="maintain",
    short_help="Rotate oversized logs and sweep aged rotated logs.",
    help=(
        "Walks every known pane, rotating <pane>.log if it exceeds\n"
        "[logs] max_size_mb, then deletes any <pane>.log.N older than\n"
        "[logs] max_age_days.\n\n"
        "Honors [logs] max_keep to cap the number of rotated copies. Logs\n"
        "for the active pane are rotated by renaming the source file; the\n"
        "live pipe-pane keeps appending to the recreated empty <pane>.log\n"
        "after the rename. Tail offsets are reset to 0 for rotated panes.\n\n"
        "Use --dry-run to preview without touching files. Use --force to\n"
        "rotate every pane's log regardless of size."
    ),
)
@click.option("--dry-run", is_flag=True, default=False, help="show what would be done without changing anything")
@click.option("--force", is_flag=True, default=False, help="rotate every pane's log unconditionally")
def cmd_maintain(dry_run: bool, force: bool) -> None:
    cfg = load_config()
    lc = _logs_cfg(cfg)
    rotated_panes: list[tuple[str, int]] = []  # (pane, prior_size_bytes)
    deleted: list[Path] = []

    with offsets_lock():
        offsets = load_offsets()
        # Per-pane rotation (size-driven or forced).
        for pane_id, state in list(offsets.items()):
            if pane_id.startswith("_") or not isinstance(state, dict):
                continue
            log_path = pane_log_path(pane_id)
            if not log_path.exists():
                continue
            try:
                size = log_path.stat().st_size
            except OSError:
                continue
            should_rotate = force or (
                int(lc["max_size_mb"]) > 0
                and size >= int(lc["max_size_mb"]) * 1024 * 1024
            )
            if not should_rotate or size == 0:
                continue
            if dry_run:
                rotated_panes.append((pane_id, size))
                continue
            rotated = rotate_log(log_path, int(lc["max_keep"]))
            if rotated is not None:
                rotated_panes.append((pane_id, size))
                # The live pipe is still writing to <pane>.log (now empty),
                # but tail_offset pointed past the rotated content. Reset.
                state["tail_offset"] = 0
                state.pop("pending_lines", None)
                state.pop("dump_pending_lines", None)
                state["continue_offset"] = None

        # Aged-rotated-log sweep across the whole logs dir.
        if dry_run:
            days = int(lc["max_age_days"])
            cutoff = time.time() - (days * 86400) if days > 0 else None
            if cutoff is not None and LOGS_DIR.exists():
                for p in LOGS_DIR.iterdir():
                    parts = p.name.split(".")
                    if len(parts) < 3 or not parts[-1].isdigit() or parts[-2] != "log":
                        continue
                    try:
                        if p.stat().st_mtime < cutoff:
                            deleted.append(p)
                    except OSError:
                        pass
        else:
            deleted = sweep_aged_logs(cfg)
            offsets["_last_sweep"] = now_iso()
            save_offsets(offsets)

    prefix = "[maintain dry-run]" if dry_run else "[maintain]"
    if rotated_panes:
        for pane_id, prior in rotated_panes:
            click.echo(f"{prefix} rotated {pane_id}.log ({prior} bytes -> {pane_id}.log.1)")
    if deleted:
        for p in deleted:
            click.echo(f"{prefix} deleted aged {p.name}")
    if not rotated_panes and not deleted:
        click.echo(f"{prefix} nothing to do (max_size_mb={lc['max_size_mb']}, max_age_days={lc['max_age_days']})")


"""Fixture file for tx-security.yaml.

Each rule has positive and negative annotations (one or more of each) so
the semgrep test harness can verify precision and recall against the
expected matches. Sections are kept short — one rule per banner — and
the bad/good examples mirror the canonical anti-pattern named in the
rule's message.
"""

import os
import subprocess


# =====================================================================
# tx-no-shell-true
# =====================================================================


def bad_subprocess_run(cmd: str) -> None:
    # ruleid: tx-no-shell-true
    subprocess.run(cmd, shell=True, check=True)


def bad_subprocess_popen(cmd: str) -> None:
    # ruleid: tx-no-shell-true
    subprocess.Popen(cmd, shell=True)


def bad_subprocess_call(cmd: str) -> None:
    # ruleid: tx-no-shell-true
    subprocess.call(cmd, shell=True)


def bad_subprocess_check_output(cmd: str) -> None:
    # ruleid: tx-no-shell-true
    subprocess.check_output(cmd, shell=True)


def bad_os_system(cmd: str) -> None:
    # ruleid: tx-no-shell-true
    os.system(cmd)


def bad_os_popen(cmd: str) -> None:
    # ruleid: tx-no-shell-true
    os.popen(cmd)


def good_argv_list(target: str) -> None:
    # ok: tx-no-shell-true
    subprocess.run(["ls", "-la", target], check=True)


def good_explicit_no_shell(target: str) -> None:
    # ok: tx-no-shell-true
    subprocess.Popen(["cat", target])


# =====================================================================
# tx-shell-injection-in-send-keys
# =====================================================================


def bad_fstring_cmd(tmux_pane, cmd: str, run_id: str) -> None:
    # ruleid: tx-shell-injection-in-send-keys
    tmux_pane.send_keys(f"__tx_run_id={run_id}; {cmd}", enter=True, literal=True)


def bad_fstring_text(tmux_pane, text: str) -> None:
    # ruleid: tx-shell-injection-in-send-keys
    tmux_pane.send_keys(f"echo {text}", enter=True, literal=True)


def bad_concat(tmux_pane, cmd: str) -> None:
    # ruleid: tx-shell-injection-in-send-keys
    tmux_pane.send_keys("set -e; " + cmd, enter=True, literal=True)


def bad_percent(tmux_pane, cmd: str) -> None:
    # ruleid: tx-shell-injection-in-send-keys
    tmux_pane.send_keys("set -e; %s" % cmd, enter=True, literal=True)


def good_wrap_command(tmux_pane, cmd: str) -> None:
    from tx_core.marker import make_run_id, wrap_command
    run_id = make_run_id()
    wrapped = wrap_command(cmd, run_id)
    # ok: tx-shell-injection-in-send-keys
    tmux_pane.send_keys(wrapped, enter=True, suppress_history=False, literal=True)


def good_send_raw_text(tmux_pane, text: str) -> None:
    # ok: tx-shell-injection-in-send-keys
    tmux_pane.send_keys(text, enter=False, suppress_history=False, literal=True)


def good_static_key(tmux_pane) -> None:
    # ok: tx-shell-injection-in-send-keys
    tmux_pane.send_keys("C-c", enter=False, suppress_history=False, literal=False)


# =====================================================================
# tx-paste-buffer-via-tempfile
# =====================================================================


def bad_paste_without_load(tmux_pane, buf_name: str) -> None:
    # ruleid: tx-paste-buffer-via-tempfile
    tmux_pane.cmd("paste-buffer", "-d", "-b", buf_name, "-t", tmux_pane.pane_id)


def bad_paste_after_unrelated(tmux_pane, server, buf_name: str) -> None:
    server.cmd("set-buffer", "-b", buf_name, "some inline payload")
    # ruleid: tx-paste-buffer-via-tempfile
    tmux_pane.cmd("paste-buffer", "-d", "-b", buf_name, "-t", tmux_pane.pane_id)


def good_paste_via_tempfile(tmux_pane, server, buf_name: str, tmp_name: str) -> None:
    server.cmd("load-buffer", "-b", buf_name, tmp_name)
    # ok: tx-paste-buffer-via-tempfile
    tmux_pane.cmd("paste-buffer", "-d", "-b", buf_name, "-t", tmux_pane.pane_id)


def good_paste_via_tempfile_bracketed(tmux_pane, server, buf_name: str, tmp_name: str) -> None:
    server.cmd("load-buffer", "-b", buf_name, tmp_name)
    # ok: tx-paste-buffer-via-tempfile
    tmux_pane.cmd("paste-buffer", "-d", "-p", "-b", buf_name, "-t", tmux_pane.pane_id)


# =====================================================================
# tx-sudo-needs-bracketed-paste
# =====================================================================


def bad_sudo_plain_send_keys(tmux_pane, password: str, cmd: str) -> None:
    wrapped = f"sudo -S -p '' {cmd}"
    _ = wrapped
    # ruleid: tx-sudo-needs-bracketed-paste
    tmux_pane.send_keys(password, enter=True, literal=True)


def bad_sudo_string_concat(tmux_pane, password: str) -> None:
    wrapped = "sudo -S -p '' apt-get update"
    _ = wrapped
    # ruleid: tx-sudo-needs-bracketed-paste
    tmux_pane.send_keys(password, enter=True, literal=True)


def good_sudo_via_send_secret(tmux_pane, log_path, password: str, cmd: str) -> None:
    from tx_core.tmux import start_pipe_pane, stop_pipe_pane

    wrapped = f"sudo -S -p '' {cmd}"
    _ = wrapped
    stop_pipe_pane(tmux_pane)
    # ok: tx-sudo-needs-bracketed-paste
    tmux_pane.send_keys(password, enter=True, suppress_history=False, literal=True)
    with open(log_path, "ab") as f:
        f.write(b"[redacted: sudo password (with Enter)]\n")
    start_pipe_pane(tmux_pane, log_path)


# =====================================================================
# tx-no-stale-write-on-failed-mv
# =====================================================================


def step(label: str, cmd: str):
    return 0, ""


def bad_mv_without_sha_verify(quoted_stage: str, quoted_target: str) -> None:
    # ruleid: tx-no-stale-write-on-failed-mv
    ex, _out = step("mv", f"mv -f {quoted_stage} {quoted_target}")
    _ = ex


def bad_mv_with_chmod_only(quoted_stage: str, quoted_target: str) -> None:
    step("chmod", f"chmod 644 {quoted_stage}")
    # ruleid: tx-no-stale-write-on-failed-mv
    ex, _out = step("mv", f"mv -f {quoted_stage} {quoted_target}")
    _ = ex


def good_mv_after_sha_verify(quoted_stage: str, quoted_target: str) -> None:
    step("sha256-verify", f"sha256sum {quoted_stage}")
    # ok: tx-no-stale-write-on-failed-mv
    ex, _out = step("mv", f"mv -f {quoted_stage} {quoted_target}")
    _ = ex


def good_mv_after_sha_verify_with_chmod(quoted_stage: str, quoted_target: str) -> None:
    step("sha256-verify", f"sha256sum {quoted_stage}")
    step("chmod", f"chmod 644 {quoted_stage}")
    # ok: tx-no-stale-write-on-failed-mv
    ex, _out = step("mv", f"mv -f {quoted_stage} {quoted_target}")
    _ = ex

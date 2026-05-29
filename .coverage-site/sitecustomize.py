"""Auto-loaded Python startup hook for subprocess-aware coverage.

When the `tx_runner` fixture sets `PYTHONPATH=<repo>/.coverage-site` and
`COVERAGE_PROCESS_START=<repo>/.coveragerc`, every Python subprocess
(including the uv-managed ephemeral venv that runs ./tx) loads this file
on startup and begins recording into a `.coverage.<pid>` fragment.

A later `coverage combine` rolls those fragments into `.coverage`. The
hook is a no-op when COVERAGE_PROCESS_START isn't set, so the file is
safe to leave on PYTHONPATH in normal runs.
"""

import os

if os.environ.get("COVERAGE_PROCESS_START"):
    try:
        import coverage

        coverage.process_startup()
    except ImportError:
        # `coverage` not installed in this venv — silently skip rather
        # than break script startup.
        pass

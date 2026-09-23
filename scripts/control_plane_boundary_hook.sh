#!/usr/bin/env bash
# ASTRA trusted-workflow boundary — ACTIONS_RUNNER_HOOK_JOB_STARTED pre-job hook.
#
# Runs inside the runner worker before any job step, action pre-entrypoint or
# container initialization. The policy decision is made by
# control_plane_boundary.py (same directory). On DENY the hook must not merely
# exit nonzero: the runner injects this hook as one pre-job step, and
# `if: always()`/`failure()` steps would still execute after a failed step.
# Denial therefore terminates the Runner.Worker process itself after writing
# the durable decision record, so no user-controlled code can run.
#
# This file and its policy must be installed read-only to the runner/job
# security context (root-owned outside the runner work directory in
# production; read-only bind mount for the isolated probe). The environment
# that configures it (ACTIONS_RUNNER_HOOK_JOB_STARTED, ASTRA_BOUNDARY_*) must
# come from the listener launch environment or a protected .env — never from
# job-writable state.

# The runner invokes this file as `bash -e <path>`; disable errexit so the
# deny path (log, flush, worker termination) always executes.
set +e
export PATH="/usr/bin:/bin"

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)/$(basename "${BASH_SOURCE[0]}")"
DIR="$(dirname "$SELF")"
PY="${ASTRA_BOUNDARY_PYTHON:-python3}"

echo "ASTRA-BOUNDARY: pre-job admission check hook=$SELF run=${GITHUB_RUN_ID:-?} job=${GITHUB_JOB:-?}"

"$PY" -I "$DIR/control_plane_boundary.py" enforce --self "$SELF"
rc=$?
if [ "$rc" -eq 0 ]; then
    echo "ASTRA-BOUNDARY: ALLOW run=${GITHUB_RUN_ID:-?} job=${GITHUB_JOB:-?}"
    exit 0
fi

echo "ASTRA-BOUNDARY: DENY rc=$rc run=${GITHUB_RUN_ID:-?} job=${GITHUB_JOB:-?} — no job step may execute"
if [ "${ASTRA_BOUNDARY_KILL:-1}" = "1" ]; then
    # Let the runner flush the hook output before the worker dies.
    sleep "${ASTRA_BOUNDARY_FLUSH_SECONDS:-3}" 2>/dev/null
    "$PY" -I "$DIR/control_plane_boundary.py" deny-job
fi
exit 1

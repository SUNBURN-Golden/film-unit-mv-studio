#!/usr/bin/env bash
# ASTRA probe-runner launcher for CP-BOUNDARY-003 evidence.
#
# Launches an isolated credential-free probe runner inside a fresh
# user+mount namespace (unshare -rm) chrooted into a scratch rootfs:
#
#   - job code sees only the explicitly mounted filesystem: runner dir (rw),
#     boundary dir (read-only), evidence dir (rw), system /usr + minimal /etc
#     (read-only), empty tmpfs /tmp and /home, ro /proc, minimal /dev;
#   - builder credentials, /etc/astra, other lanes' worktrees and this lane's
#     GitHub auth are simply not mounted and therefore unreachable from a job;
#   - the hook + policy + evaluator are read-only inside the namespace;
#   - ACTIONS_RUNNER_HOOK_JOB_STARTED and ASTRA_BOUNDARY_* are supplied in the
#     launch environment, never via a job-writable .env file.
#
# verify-install + check-env run before the listener starts; a failed check
# refuses the launch (fail closed). This is probe plumbing only — the
# production install path is documented in RUNBOOKS/BOUNDARY.md.
#
# Usage:
#   control_plane_boundary_probe.sh launch RUNNER_DIR BOUNDARY_DIR EVIDENCE_DIR
#   control_plane_boundary_probe.sh rootfs  ROOTFS_DIR            (prepare dirs)
#
# Optional env: PROBE_POLICY (in-namespace policy path, default
# /boundary/policy.json), PROBE_POLICY_SHA256 (override pinned digest —
# used only to demonstrate digest-mismatch denial), PROBE_NO_HOOK=1
# (demonstrate the missing-hook configuration), EXTRA_RUNNER_ENV.

set -euo pipefail

MODE="${1:-}"
RUNNER_DIR="${2:-}"
BOUNDARY_DIR="${3:-}"
EVIDENCE_DIR="${4:-}"
ROOTFS="${PROBE_ROOTFS:-${RUNNER_DIR}.rootfs}"

die() { echo "probe-launch: $*" >&2; exit 2; }

case "$MODE" in
    rootfs)
        R="$RUNNER_DIR"
        [ -n "$R" ] || die "rootfs mode needs a rootfs dir"
        mkdir -p "$R"/{usr,etc/ssl,dev,proc,tmp,runner,evidence,boundary,home,work}
        for link in bin lib lib64 sbin; do
            [ -L "$R/$link" ] || ln -s "usr/$link" "$R/$link"
        done
        for f in resolv.conf hosts nsswitch.conf passwd group os-release ld.so.conf ld.so.cache; do
            [ -f "/etc/$f" ] && cp "/etc/$f" "$R/etc/$f" || true
        done
        echo "rootfs prepared at $R"
        exit 0
        ;;
    launch) ;;
    *) die "usage: $0 launch RUNNER_DIR BOUNDARY_DIR EVIDENCE_DIR | rootfs DIR" ;;
esac

[ -d "$RUNNER_DIR" ] || die "runner dir missing: $RUNNER_DIR"
[ -d "$BOUNDARY_DIR" ] || die "boundary dir missing: $BOUNDARY_DIR"
[ -d "$EVIDENCE_DIR" ] || die "evidence dir missing: $EVIDENCE_DIR"
[ -x "$RUNNER_DIR/run.sh" ] || die "runner not unpacked at $RUNNER_DIR"
[ -f "$RUNNER_DIR/.runner" ] || die "runner not configured (missing .runner)"

HOOK_SRC="$BOUNDARY_DIR/control_plane_boundary_hook.sh"
PY_SRC="$BOUNDARY_DIR/control_plane_boundary.py"
POLICY_SRC="${PROBE_POLICY_FILE:-$BOUNDARY_DIR/policy.json}"
[ -f "$HOOK_SRC" ] || die "hook missing: $HOOK_SRC"
[ -f "$PY_SRC" ] || die "evaluator missing: $PY_SRC"
[ -f "$POLICY_SRC" ] || die "policy missing: $POLICY_SRC"

# In-namespace layout (fixed paths the hook environment refers to).
NS_HOOK=/boundary/control_plane_boundary_hook.sh
NS_POLICY="${PROBE_POLICY:-/boundary/policy.json}"
NS_EVIDENCE=/evidence
POLICY_SHA="$(python3 -I -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$POLICY_SRC")"
PINNED_SHA="${PROBE_POLICY_SHA256:-$POLICY_SHA}"
HOOK_SHA="$(python3 -I -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$HOOK_SRC")"

echo "probe-launch: policy sha256=$POLICY_SHA pinned=$PINNED_SHA"
echo "probe-launch: hook   sha256=$HOOK_SHA"

# Structural verification of what the job will see (paths inside the ns).
python3 -I "$PY_SRC" verify-install \
    --hook "$HOOK_SRC" --policy "$POLICY_SRC" \
    --hook-sha256 "$HOOK_SHA" \
    --policy-sha256 "$POLICY_SHA" \
    --forbid-prefix "$RUNNER_DIR/_work" \
    ${PROBE_ENV_FILE:+--env-file "$PROBE_ENV_FILE"} \
    || die "verify-install failed; refusing to start probe runner"

# Refuse a job-writable .env that carries boundary configuration.
if [ -f "$RUNNER_DIR/.env" ]; then
    if grep -qE '^(ACTIONS_RUNNER_HOOK_JOB_STARTED|ASTRA_BOUNDARY_)' "$RUNNER_DIR/.env"; then
        die "runner .env carries boundary configuration; refusing launch"
    fi
fi

if [ "${PROBE_NO_HOOK:-0}" = "1" ]; then
    echo "probe-launch: WARNING launching WITHOUT boundary hook (missing-hook demonstration)"
fi

"$0" rootfs "$ROOTFS"

export RUNNER_ALLOW_RUNASROOT=1
exec unshare -rm bash -c '
set -e
R="$1"; RUNNER_DIR="$2"; BOUNDARY_DIR="$3"; EVIDENCE_DIR="$4"
NS_HOOK="$5"; NS_POLICY="$6"; NS_EVIDENCE="$7"; PINNED_SHA="$8"; NO_HOOK="$9"
mount --bind -o ro /usr "$R/usr"
for link in bin lib lib64 sbin; do
    [ -L "$R/$link" ] || ln -s "usr/$link" "$R/$link"
done
mkdir -p "$R/etc/ssl" && mount --bind -o ro /etc/ssl "$R/etc/ssl"
for f in resolv.conf hosts nsswitch.conf passwd group os-release ld.so.conf ld.so.cache; do
    [ -f "/etc/$f" ] && cp "/etc/$f" "$R/etc/$f" || true
done
for d in null zero full random urandom tty; do
    touch "$R/dev/$d" 2>/dev/null || true
    mount --bind "/dev/$d" "$R/dev/$d" 2>/dev/null || true
done
mount -t tmpfs tmpfs "$R/tmp"
mount -t tmpfs tmpfs "$R/home"
mount --bind -o ro /proc "$R/proc"
mount --bind "$RUNNER_DIR" "$R/runner"
mount --bind "$EVIDENCE_DIR" "$R/evidence"
mkdir -p "$R/boundary"
mount --bind -o ro "$BOUNDARY_DIR" "$R/boundary"

NS_ENV="HOME=/home PATH=/usr/bin:/bin RUNNER_ALLOW_RUNASROOT=1"
NS_ENV="$NS_ENV ASTRA_BOUNDARY_POLICY=$NS_POLICY"
NS_ENV="$NS_ENV ASTRA_BOUNDARY_POLICY_SHA256=$PINNED_SHA"
NS_ENV="$NS_ENV ASTRA_BOUNDARY_EVIDENCE_DIR=$NS_EVIDENCE"
if [ "$NO_HOOK" != "1" ]; then
    NS_ENV="$NS_ENV ACTIONS_RUNNER_HOOK_JOB_STARTED=$NS_HOOK"
fi
# Boundary environment is asserted from inside the namespace before launch
# (only meaningful when the hook is armed).
if [ "$NO_HOOK" != "1" ]; then
    env -i $NS_ENV ${EXTRA_RUNNER_ENV:-} \
        /usr/sbin/chroot "$R" /usr/bin/python3 -I /boundary/control_plane_boundary.py check-env \
            --hook "$NS_HOOK" --policy "$NS_POLICY" --policy-sha256 "$PINNED_SHA"
fi
cd "$R"
exec env -i $NS_ENV ${EXTRA_RUNNER_ENV:-} /usr/sbin/chroot "$R" /bin/bash -c "cd /runner && exec ./run.sh"
' _ "$ROOTFS" "$RUNNER_DIR" "$BOUNDARY_DIR" "$EVIDENCE_DIR" \
    "$NS_HOOK" "$NS_POLICY" "$NS_EVIDENCE" "$PINNED_SHA" "${PROBE_NO_HOOK:-0}"

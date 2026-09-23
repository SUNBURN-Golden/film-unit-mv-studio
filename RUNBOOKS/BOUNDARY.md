# RUNBOOK — trusted workflow boundary (CP-BOUNDARY-003)

Scope: install, verify and roll back the pre-job admission boundary on the
`self-hosted`/`astra-control-plane` runner. This runbook does not activate the
runtime; `runtime_enabled` stays `false` and the production runner stays
disconnected until the separate activation gate passes.

Mechanism, proved on the isolated probe runner: the runner reads
`ACTIONS_RUNNER_HOOK_JOB_STARTED` from its launch environment and executes that
script as the first pre-job step (`JobExtension.cs` inserts it before container
setup and action pre-steps, with condition `always()`). The hook evaluates the
job claims (`GITHUB_*`/`RUNNER_*` env + `GITHUB_EVENT_PATH` payload, all
server-supplied) against a digest-pinned policy. On deny it writes a durable
decision record and **terminates the `Runner.Worker` process**, because a mere
nonzero exit still allows `always()`/`failure()` steps to run (proved by the
report-mode bypass case in the probe evidence).

## Files and layout (production)

```text
/opt/astra/boundary/control_plane_boundary_hook.sh   root:astra-control 0755
/opt/astra/boundary/control_plane_boundary.py        root:astra-control 0644
/opt/astra/boundary/policy.json                      root:astra-control 0644
/var/lib/astra/boundary-evidence/                    astra-runner 0700
```

- None of these paths may be under the runner work dir or any builder-writable
  location. Job code must not be able to modify hook, evaluator or policy.
- The runner unit (or launch wrapper) exports, for the listener process:
  `ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/astra/boundary/control_plane_boundary_hook.sh`
  `ASTRA_BOUNDARY_POLICY=/opt/astra/boundary/policy.json`
  `ASTRA_BOUNDARY_POLICY_SHA256=<sha256 of policy.json>`
  `ASTRA_BOUNDARY_EVIDENCE_DIR=/var/lib/astra/boundary-evidence`
- Do not put these keys in the runner `.env` file: `.env` is job-writable and a
  hostile job could repoint the hook for the next listener start. If `.env`
  must exist, verify-install requires it to contain no boundary keys.

## Install (versioned)

1. Pick the audited commit SHA that contains `scripts/control_plane_boundary*`.
2. Install the three files at the fixed paths above (digests recorded in the
   install log).
3. Build `policy.json` from `boundary-policy.example.json`: pin repository and
   owner **ids** (not just names), the approved `GITHUB_REF`, the exact
   `GITHUB_WORKFLOW_REF`/`GITHUB_WORKFLOW_SHA` (commit SHA containing the reviewed
   workflow file; never its blob SHA), event name, job id and allowed actor
   ids, plus `event:` assertions on `repository.id`, `repository.owner.id`,
   `sender.id`. Record `sha256(policy.json)`.
4. Run, as the runner account context, fail-closed checks:

   ```sh
   python3 -I /opt/astra/boundary/control_plane_boundary.py verify-install \
     --hook /opt/astra/boundary/control_plane_boundary_hook.sh \
     --policy /opt/astra/boundary/policy.json \
     --hook-sha256 <hook sha> --policy-sha256 <policy sha> \
     --owner-uid 0 --forbid-prefix /runner/work/dir \
     --writable-check [--env-file /path/to/runner/.env]
   ```

   and, inside the launch environment before starting the listener:

   ```sh
   python3 -I /opt/astra/boundary/control_plane_boundary.py check-env \
     --hook /opt/astra/boundary/control_plane_boundary_hook.sh \
     --policy /opt/astra/boundary/policy.json --policy-sha256 <policy sha>
   ```

5. Start the listener only when both checks pass. Re-run them on every
   listener start (the launch wrapper does this), because `.env` is read once
   per listener start and job code could otherwise alter it for the next start.

## Rollback / disable

- Stop the runner service; remove the boundary env keys; restart. Removal is a
  deliberate operator act — the launcher must refuse to start the runner when
  `ACTIONS_RUNNER_HOOK_JOB_STARTED` is absent from the launch environment
  (missing-hook is a deny condition for operation, verified by the probe).
- Rollback does NOT mean "run without the boundary": the runner must stay
  disconnected until a verified boundary is installed.

## Probe reproduction (credential-free)

`scripts/control_plane_boundary_probe.sh` launches a probe runner inside a
fresh user+mount namespace chrooted into a scratch rootfs: job code sees only
the runner dir (rw), the boundary dir (ro), the evidence dir (rw) and a
minimal system image. No builder credential, `/etc/astra` content or other
lane file is mounted. Host-side copies of the commands used, run ids and
decision records live under `/workspace/astra-host-evidence/boundary-003/`.

## Known platform limits

- The pre-job hook decision depends on runner-supplied env claims
  (`GITHUB_WORKFLOW_SHA`, `GITHUB_REPOSITORY_ID`, …) and the event payload.
  All are generated from the job message, not workflow input, but only the
  claims enumerated in the policy are checked — anything not pinned is not
  verified.
- Job `container:`/`services:` isolation (docker absent on the probe host) is
  recorded as NOT_TESTED where applicable; the hook still denies before the
  container-init step is reached.
- A denied job terminates the worker; the listener reports the job as failed.
  Repeated hostile pushes each get denied again; deduping them is out of
  scope.

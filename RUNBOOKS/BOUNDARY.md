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
     --evaluator-sha256 <audited-evaluator-sha256> \
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

설치 시 hook·policy·evaluator의 예상 해시는 설치 파일에서 새로 계산해 신뢰하지 않고, 감사된 artifact/승인 기록에서 가져온다. 검사기는 세 파일과 상위 경로의 소유권·교체 가능성을 검사한다. runner `.env`의 모든 `ASTRA_BOUNDARY_*` 및 hook override는 금지한다. 검사기는 `/usr/bin/python3`를 고정 사용하며 DENY는 항상 worker 종료를 시도한다. 기존 report-only 환경변수는 지원하지 않는다.

runner `.env`가 있으면 반드시 `--env-file`로 검사하며 runner/job이 파일이나 상위 디렉터리를 변경할 수 없어야 한다. 허용 키는 `LANG`, `LC_ALL`, `TZ`뿐이다. listener 시작은 operator 소유 서비스의 정리된 환경에서만 수행하고 shell/loader startup 변수(`BASH_ENV`, `ENV`, `LD_*` 등)를 전달하지 않는다. 검사에 실패하면 listener를 시작하지 않는다. 기존 `.env`의 추가 키가 필요하면 별도 검토 전까지 BLOCKED로 처리한다.

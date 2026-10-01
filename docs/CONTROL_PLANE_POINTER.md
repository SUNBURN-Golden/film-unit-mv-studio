# Shared engineering policy pointer

Shared policy owner: `BeautifulMind-JT/ai-ops-control-plane`. This product is not the shared control-plane host.
Target product: `BeautifulMind-JT/film-unit-mv-studio`.

Pinned policy: https://github.com/BeautifulMind-JT/ai-ops-control-plane/tree/3e7c64a515e908b312604e82405b963d100caa07/engineering
Status: `POLICY_ADOPTED_PIN`
Central acceptance: https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/16
(merge `93274ef`; its tree is identical to the pinned commit).
Product adoption: https://github.com/BeautifulMind-JT/film-unit-mv-studio/pull/13.
Adoption is not evidence of installed runtime, builder qualification or activation.

Previous pin: https://github.com/BeautifulMind-JT/ai-ops-control-plane/tree/7ad7f5008bae4004d5158075e8517e24a2d2de6a/engineering
(`SOURCE_EXTRACTED_VERIFIED_PIN`, source-only, FILM_UNIT `deployment_enabled=false`).
Historical source, audit and activation evidence retains its original SHA and scope.

The machine-readable pin and status are `.github/control-plane-client.json`.
Central paths below are relative to the central repository root; the policy root
is its `control_source_subdirectory` (`engineering`).
Project defaults come from pinned `engineering/projects/film-unit-mv-studio.md`.
Builder/model qualification uses the pinned central policy. Astra is Claude Fable, run by the
central `aiops-fable` tool (User decision M5, 2026-09-30).
Product contracts, task specifications, protected files and product CI remain here.
GitHub task/decision/audit records remain authoritative; Slack is a collaboration surface.

## Central dispatch eligibility at this pin

The pinned `engineering/.github/control-plane/projects.json` sets FILM_UNIT
`deployment_enabled=true` and `enabled_builders=["DEVIN"]`. Eligibility changed
from `false` at the previous pin via central PR #18 (2026-09-27); host rollout
still requires its own evidence (central `engineering/CUTOVER.md` step 7).
User decision (2026-09-28, PR #14): keep FILM_UNIT eligible (`deployment_enabled=true`).

Eligibility alone dispatches nothing. Central dispatch also requires the pinned
`engineering/.github/control-plane/activation.json` to reach `runtime_enabled=true`,
which needs User activation approval, implementation audit and runner preflight
evidence; all are `NOT_APPROVED`/`PENDING` at this pin. OPERATING_MODE stays
`MANUAL_ONLY` until that gate passes. This pin does not expand a host allowlist.

`runtime_enabled` in `.github/control-plane-client.json` is a product-side record
only. The central dispatcher does not read it, so it is not a fence.

## Local boundary

No local dispatcher is installed here. Do not enable a runner, copy credentials,
start a builder or assume KIX activation/audit evidence transfers from this
repository. Preserve task, owner, request and ledger identities. Fence/drain
legacy dispatch before retiring it; never run two dispatchers. Runtime identity
separation and host/Slack cutover are separate gates.
User-authorized merge; program mode delegates only the merge executor (User decision M1,
`AGENTS.md` program mode section). No automatic fallback, retries, polling or standing routines.

Migration history: https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/1


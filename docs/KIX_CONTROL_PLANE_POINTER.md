<!-- E2 sibling pointer — docs only; do not copy full KIX policy here -->
# KIX control-plane pointer (sibling)

This repository is a **sibling consumer** of the KIX control plane.

## Source of truth

| Concern | Authoritative location |
|---|---|
| Control-plane SoT (policy, runtime adapters, host helpers) | [`BeautifulMind-JT/kix-protocol`](https://github.com/BeautifulMind-JT/kix-protocol) |
| Dispatch policy / runbooks | `kix-protocol` → `RUNBOOKS/DISPATCH.md`, `docs/CONTROL_PLANE_RUNTIME.md` |
| Activation / `runtime_enabled` / `activated_runtime_sha` / `enabled_builders` | `kix-protocol` → `.github/control-plane/` (**production bring-up lives there**) |
| Production runner / canary evidence | `kix-protocol` issues/PRs (e.g. #40 umbrella, #49 canary) |

## Sibling application status (E2)

- **This PR:** docs/governance **pointer only**.
- **Not in scope here:** copying full AGENTS policy, adding/changing runtime files, workflows, runner config, activation JSON, secrets, or builder allowlists.
- Local `.github/control-plane/*` and workflows that may already exist in this tree remain **inactive / non-authoritative** for production until a future gated adoption explicitly says otherwise. Do **not** treat them as live production SoT.
- Production dispatch and activation changes must go through **kix-protocol** ordinary PR + User merge authorization (builders never self-merge).

## Rollout reference

- Umbrella: https://github.com/BeautifulMind-JT/kix-protocol/issues/40
- Astra Midcoord E2 (2026-09-24): docs/pointer-only sequential sibling rollout


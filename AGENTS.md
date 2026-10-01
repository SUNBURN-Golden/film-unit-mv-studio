# Product agent governance

Shared engineering policy is maintained in `BeautifulMind-JT/ai-ops-control-plane`.
The pin and its adoption status are recorded in `.github/control-plane-client.json`
and explained in `docs/CONTROL_PLANE_POINTER.md`. Policy adoption is not runtime
activation. User-authorized merge (program mode delegates only the executor; see below),
non-author exact-HEAD review, single writer, UNKNOWN
fencing, no polling and no automatic retry remain required.
If shared policy and project contracts conflict, stop with DECISION_REQUIRED.

## AIOPS program mode (User decisions M1 and M5)

These rules apply to a task started by the central AIOPS program mode: its GitHub
task issue carries an `ASTRA_TASK_KEY_V1` line and a TASK ENVELOPE v4. For that task
they take precedence over conflicting rules in this file and in other repository
documents (User decision, 2026-09-30). Everything else still applies.

- **Task input.** The task issue envelope and the documents it names are the task
  document. The control plane generates it from `.aiops/program.json` at the pinned
  plan commit, outside the executing session.
- **Branch.** `astra/<task id in lowercase>`, exactly as the task issue names it.
- **Pull request.** Open it from that branch as ready for review, not a draft, so the
  exact-head CI runs. Program mode authorizes this.
- **Merge.** Builders and reviewers never merge, push to `main`, rewrite history, or
  create, move or delete tags or branches. The User authorizes merges. User decision
  M1 (2026-09-29) delegates only the executor: the central `operation=merge` merges a
  pull request with an ordinary merge commit, pinned to its exact head, and only when
  the computed READY_FOR_MERGE holds, this repository's required checks included.
  Anything it cannot compute goes to the User.
- **Astra.** Architecture and design authority and the required audits are held by
  Claude Fable, run by the central `aiops-fable` tool (User decision M5, 2026-09-30).

Outside program mode the rules below apply unchanged, including User-only merge.

## Repository-specific engineering constraints

Read `README.md` and task-linked production/validation documents before work.
Existing lyrics/subtitle timing, LOCK, asset provenance, renderer/budget
approval, build-history/reproducibility and Preview/Final rules remain
authoritative. Paid generation or production approval is never inferred from
this control-plane policy.





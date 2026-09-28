# Product agent governance

Shared engineering policy is maintained in `BeautifulMind-JT/ai-ops-control-plane`.
The pin and its adoption status are recorded in `.github/control-plane-client.json`
and explained in `docs/CONTROL_PLANE_POINTER.md`. Policy adoption is not runtime
activation. User-only merge, non-author exact-HEAD review, single writer, UNKNOWN
fencing, no polling and no automatic retry remain required.
If shared policy and project contracts conflict, stop with DECISION_REQUIRED.

## Repository-specific engineering constraints

Read `README.md` and task-linked production/validation documents before work.
Existing lyrics/subtitle timing, LOCK, asset provenance, renderer/budget
approval, build-history/reproducibility and Preview/Final rules remain
authoritative. Paid generation or production approval is never inferred from
this control-plane policy.




# Shared engineering policy pointer

Shared policy owner: `BeautifulMind-JT/ai-ops-control-plane`.
Target product: `BeautifulMind-JT/film-unit-mv-studio`.

Candidate policy: https://github.com/BeautifulMind-JT/ai-ops-control-plane/tree/aaa3e0544926dc2789faabc1fbe33fb611a12377/engineering
Status: pending central PR acceptance and this product's policy adoption; this
candidate is not evidence of installed runtime, builder qualification or activation.

The machine-readable pin is `.github/control-plane-client.json`.
Project defaults come from pinned `engineering/projects/film-unit-mv-studio.md`.
Builder/model qualification and Astra Slack routing use the pinned central policy.
Product contracts, task specifications, protected files and product CI remain here.
GitHub task/decision/audit records remain authoritative; Slack is a collaboration surface.

Migration history: https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/1
Historical source, audit and activation evidence retains its original SHA and scope.
No local dispatcher is installed by this reference change. Existing deployment,
fence and rollout decisions are unchanged; never run two dispatchers for one task.
User-only merge; no automatic fallback, retries, polling or standing routines.

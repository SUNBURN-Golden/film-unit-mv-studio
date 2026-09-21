# TASK ENVELOPE v3

Store this envelope on the canonical GitHub task, not as a new runtime file.
Link the existing specification; do not rewrite or duplicate it.

## Required intake

TASK_ID:
PROJECT:
REPO:
CANONICAL_TASK_POINTER:
TASK_REVISION:
TASK_SPEC_POINTER:
TASK_SPEC_REVISION:
APPROVAL_POINTER:
AUTHORITATIVE_DOC_POINTERS:

The pinned specification must identify objective, scope, exclusions,
constraints/contracts and acceptance/verification criteria. Exact section
pointers suffice; the same issue may supply every section. No separate
Astra approval is required for an already authorized, well-scoped task.
Missing consequential requirements need clarification; repository investigation
and ordinary implementation choices belong to Devin.

## Fixed defaults from RUNBOOKS/DISPATCH.md

EXECUTION_CLASS: DEVIN_STANDARD
DELIVERABLE_MODE: PR
AUDIT_FLOOR: A1
REVIEW_POLICY: REQUIRED_NON_A0
REVIEWER_LANE_ID: CONFIG_REQUIRED

REPO/PROJECT must match the project map. Verification/post-merge policy comes
from that map and the repository rules. Overrides require a durable authorized
pointer; Grok never classifies the task or invents missing requirements.
Before dispatch, resolve the required reviewer identity. An unavailable lane
cannot silently waive review.

## Conditional pointers

CONTRACT_POINTERS:
INVARIANT_POINTERS:
LOCKED_AREAS_POINTERS:
PHASE_OR_LAYER_POINTER:

Use exact task-spec sections or existing repository documents; N/A is allowed
only when no such requirement applies. Existing project-specific requirements
remain mandatory, including KIX immutable docs/tasks specifications.

For CHEAP_MECHANICAL/A0 only:
A0_AUTHORIZATION_POINTER:
A0_CHANGE_KIND: TYPO | FORMAT_ONLY | DOC_MECHANICAL
A0_ALLOWED_PATHS:
A0_FORBIDDEN_PATHS:

Other allowed DELIVERABLE_MODE values:
NO_CHANGE_ALLOWED | NON_CODE_EVIDENCE.
Other AUDIT_FLOOR values: A0 | A2 | A3.
A0 requires the runbook's final qualification.

## Dispatch references

CONTROL_RECORD_POINTER:
CANONICAL_SLACK_THREAD:

These are provisioned by intake/User before execution; they are not invented
by Grok. If Slack is used, task and thread link to each other. Mutable
claim/session/HEAD/gate state belongs in the control record, not the task spec.

## Owner instruction

Follow the pinned task and repository rules. Investigate, implement, debug,
test/fix/retest and deliver the PR without routine plan approval.
Preserved approved A3 contracts permit implementation followed by A3 audit.
Required consequential contract changes require DECISION_REQUIRED with the
exact question and evidence before implementing that change.
CI/review/audit feedback returns to the same active owner.

## Completion evidence index

- task revision; observed base SHA; PR and exact final HEAD/evidence SHA;
- complete changed paths and acceptance criterion -> test/CI evidence pointers;
- actual commands/results and repository-required evidence;
- TOUCHED_AREAS and CONTRACT_CHANGE_REQUIRED: NO | YES (advisory only);
- independent review pointer when available;
- unverified behavior, remaining risks, BLOCKED/STALLED when applicable.

Astra independently verifies touched areas and contract changes. This index
does not replace repository evidence requirements or independent examination.
No transcript, repeated status or mutable execution state belongs here.

Missing required fields: BLOCKED / INCOMPLETE_TASK_ENVELOPE, field names only.

# TASK ENVELOPE v2

This file is a pointer envelope, not a second task specification.
Do not copy authoritative documents into it.
Do not paraphrase source requirements when a durable pointer exists.

## Required identity

TASK_ID:
PROJECT:
REPO:
CANONICAL_TASK_POINTER:
CONTROL_RECORD_POINTER:
TASK_REVISION:

<!-- TASK_KEY is mechanically derived as REPO + TASK_ID. -->

## Required source pointers

OBJECTIVE_POINTER:
TASK_SPEC_POINTER:
TASK_SPEC_REVISION:
APPROVAL_POINTER:
AUTHORITATIVE_DOC_POINTERS:

## Dispatch classification

EXECUTION_CLASS: DEVIN_STANDARD | CHEAP_MECHANICAL
DELIVERABLE_MODE: PR | NO_CHANGE_ALLOWED | NON_CODE_EVIDENCE
AUDIT_FLOOR: A0 | A1 | A2 | A3

A0_AUTHORIZATION_POINTER:
<!-- REQUIRED when EXECUTION_CLASS=CHEAP_MECHANICAL; otherwise N/A -->

A0_CHANGE_KIND: TYPO | FORMAT_ONLY | DOC_MECHANICAL | N/A
A0_ALLOWED_PATHS:
A0_FORBIDDEN_PATHS:
<!--
For A0, these are REQUIRED and must be exact paths/globs approved by the
authorization pointer. Grok does not derive them.
-->

## Project policy pointers

REVIEW_POLICY:
REVIEWER_LANE_ID:
VERIFICATION_POLICY:
POST_MERGE_POLICY:

## Boundary pointers

SCOPE_POINTER:
NON_SCOPE_POINTER:
CONTRACT_POINTERS:
INVARIANT_POINTERS:
LOCKED_AREAS_POINTERS:
PHASE_OR_LAYER_POINTER:
<!-- Use N/A only when the project has no such gate. -->

## Required output from writer

- exact task revision consumed;
- deliverable result: PR | NO_CHANGE | NON_CODE_EVIDENCE;
- PR pointer when applicable;
- exact HEAD SHA when applicable;
- verification evidence pointers;
- TOUCHED_AREAS self-report;
- CONTRACT_CHANGE_REQUIRED: NO | YES self-report;
- BLOCKED or STALLED report when applicable.

TOUCHED_AREAS self-report may use:

NONE
CONCURRENCY
STATE_MACHINE
PERSISTENCE
PAYMENTS
SECURITY
PROTOCOL
INVARIANT
SCHEMA
PUBLIC_CONTRACT
SUI
BLOCKCHAIN
FINANCIAL_SEMANTICS
AUTHORITY_BOUNDARY

These reports are evidence only. Astra independently verifies classification.

## Escalation boundary

Devin does NOT escalate merely because two ordinary implementation approaches
exist.

Escalate only when completing the approved task requires a consequential change
outside approved scope/contracts/architecture, including a required change to:

- invariant;
- schema contract;
- public contract;
- authority/security boundary;
- protocol semantics;
- blockchain/Sui architecture;
- financial semantics;
- approved product/architecture decision.

Return:

DECISION_REQUIRED

with exact source/evidence pointers.

## Missing fields

Grok does not invent required fields.

If a required field is missing, return:

[BLOCKED] Reason: INCOMPLETE_TASK_ENVELOPE

and list field names only.

## Execution state is not stored here

Do not store mutable owner/session/CI/review/audit state in this envelope.

Mutable execution facts belong only in the canonical mechanical control record
defined by RUNBOOKS/DISPATCH.md.

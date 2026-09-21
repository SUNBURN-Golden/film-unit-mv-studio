# TASK PACKAGE

Copy this template mechanically. Fill variables from explicit User/Astra input,
Slack source messages, GitHub issues, or authoritative repository documents.
Do not rewrite source documents. Do not invent missing consequential requirements.

EVENT_ID:
TASK_ID:
PROJECT:
WORKER: DEVIN | CHEAP_WORKER
AUDIT_DEPTH_INITIAL: A0 | A1

## OBJECTIVE

OBJECTIVE:
OBJECTIVE_SOURCE:

## SOURCE OF TRUTH

REPO:
BASE_BRANCH: main
ISSUE:
BASE_SHA:
AUTHORITATIVE_DOCS:

## SCOPE

SCOPE:
NON_SCOPE:

## EXISTING CONTRACTS

EXISTING_CONTRACTS:

## INVARIANTS

INVARIANTS:

## LOCKED AREAS

LOCKED_AREAS:

## ACCEPTANCE CRITERIA

ACCEPTANCE_CRITERIA:

## REQUIRED TESTS

REQUIRED_TESTS:

## REQUIRED VERIFICATION

REQUIRED_VERIFICATION:

## EXPECTED OUTPUT

- code or explicit no-code result
- tests where required
- PR
- exact HEAD SHA
- CI evidence
- verification evidence
- TOUCHED_AREAS
- CONTRACT_CHANGE_REQUIRED: NO | YES
- any DECISION_REQUIRED blocker

## TOUCHED_AREAS

Use zero or more exact values:

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
FINANCIAL_SEMANTICS

## ESCALATE_IF

- architecture / contract / schema / security choice appears
- approved invariant cannot be kept
- two materially different designs require selection
- acceptance criteria conflict
- authoritative documents materially conflict
- consequential scope expansion is required

Return DECISION_REQUIRED with pointers and evidence instead of guessing.

## POINTERS_ONLY

Objective source:
Specification:
Architecture:
Relevant issue:
Relevant PR:
Relevant docs:

## DISPATCH RECORD

After successful launch Grok records a machine-readable marker in the associated
GitHub task/issue:

`<!-- GROK_DISPATCH event_id=<EVENT_ID> task_id=<TASK_ID> worker=<WORKER> -->`

The same EVENT_ID must never create two worker sessions.

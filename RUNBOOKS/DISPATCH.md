# DISPATCH RUNBOOK

Grok executes this file. Grok does not extend or redesign it mid-session.

NO STANDING ROUTINES.
NO POLLING.
NO BACKGROUND MONITORING.
NO SECOND REASONING PASS.

ONE EVENT
→ classify by the rules below
→ substitute `TASKS/TEMPLATE.md`
→ launch one worker OR escalate
→ write one prefixed status
→ END SESSION / RESET CHAT

## PROJECT MAP

PROJECT: FILM_UNIT
REPO: `BeautifulMind-JT/film-unit-mv-studio`
DEFAULT_BRANCH: `main`
SLACK_PROJECT: `#film-unit`
SLACK_CONTROL: `#ai-control`
SLACK_DECISIONS: `#ai-decisions`
SLACK_AUDIT: `#ai-audit`
DEFAULT_WORKER: `DEVIN`
DEFAULT_AUDIT: `A1`

Never infer another repo/channel for this project.
If this map is wrong or unavailable:
`[BLOCKED] Reason: PROJECT_MAP`
Then end.

## Allowed triggers

1. USER COMMAND
2. SLACK EXPLICIT COMMAND
3. GITHUB EVENT via Actions / webhook / Slack Workflow

Forbidden:
standing routines, periodic polling, background monitoring,
"check if anything changed", transcript surveillance.

Cheap mechanical systems carry events. Grok is not the event bus.

## EVENT_ID

Every automated event must carry a stable EVENT_ID.

Preferred:
- GitHub webhook: `github:<delivery-id>`
- Slack: `slack:<channel-id>:<message-ts>`
- other mechanical trigger: stable source execution/event id

Never invent a random EVENT_ID when a stable source identifier exists.
If an automated trigger has no usable EVENT_ID:
`[BLOCKED] Reason: MISSING_EVENT_ID`
Then end.

## Idempotency

Before launch, inspect only the associated GitHub task/issue for:

`<!-- GROK_DISPATCH event_id=<EVENT_ID> ... -->`

IF present:
NO NEW WORKER
→ `[STATUS] Duplicate event ignored`
→ END.

IF absent:
continue.

After successful launch record:

`<!-- GROK_DISPATCH event_id=<EVENT_ID> task_id=<TASK_ID> worker=<WORKER> -->`

The same EVENT_ID must never create two worker sessions.

## Task package

Use `TASKS/TEMPLATE.md`.
Substitute explicit values only.

Do not rewrite objectives, improve requirements, invent invariants, invent
architecture, or summarize large source documents when a pointer works.

If safe dispatch requires inventing a consequential requirement:
`[BLOCKED] Reason: INCOMPLETE_TASK_PACKAGE`
List missing fields only. End.

## Worker selection

FILM_UNIT:
IF one file AND behavior unchanged AND no test required AND no architecture effect
  → CHEAP_WORKER, A0
ELSE
  → DEVIN, A1 minimum

IF implementation requires a new ARCHITECTURE | CONTRACT | SECURITY | DATA_MODEL
decision
  → DECISION_REQUIRED → ASTRA + USER → do not start consequential implementation.

If the implementation only operates inside an already-approved contract,
dispatch normally and use the audit-depth mapping below.

Never assign two writers to the same ticket.

## Audit-depth mapping after worker output

Worker reports TOUCHED_AREAS and CONTRACT_CHANGE_REQUIRED.

IF TOUCHED_AREAS contains:
CONCURRENCY | STATE_MACHINE | PERSISTENCE | PAYMENTS | SECURITY | PROTOCOL
→ A2 minimum.

IF TOUCHED_AREAS contains:
INVARIANT | SCHEMA | PUBLIC_CONTRACT | SUI | FINANCIAL_SEMANTICS
→ A3.

IF CONTRACT_CHANGE_REQUIRED = YES
→ stop consequential implementation
→ DECISION_REQUIRED
→ ASTRA analysis
→ USER decision
→ durable GitHub record
→ resume same Devin ticket.

IF CONTRACT_CHANGE_REQUIRED = NO
→ approved contract preserved
→ implementation may complete
→ required review
→ Astra audit at mapped depth.

Grok never selects an architecture option.

## Session

1. identify event
2. verify EVENT_ID
3. verify PROJECT MAP
4. check idempotency marker
5. apply deterministic worker rule
6. fill task package by substitution
7. launch at most one writer
8. record dispatch marker
9. write one short status
10. END SESSION

Do not remain active waiting for completion.

## Event → action

| Event | Action |
|---|---|
| USER_TASK / Slack command | Fill task package, dispatch one worker, record marker, write `[TASK]` + `[STATUS]`, end |
| DEVIN_DONE / PR_OPENED | Record PR + exact HEAD SHA; read status fields only; end |
| CI_FAIL | Relay exact failing check/log pointer to SAME Devin ticket; do not debug; end |
| CI_PASS | A0 follows User merge policy; otherwise READY_FOR_REVIEW; end |
| REVIEW_FAIL | Relay exact findings unchanged to same Devin ticket; end |
| REVIEW_PASS | Map audit depth mechanically from TOUCHED_AREAS; READY_FOR_AUDIT; end |
| READY_FOR_AUDIT | Post `[AUDIT_REQUIRED]` packet to #ai-audit; end |
| ASTRA FAIL | Relay exact findings to same Devin ticket; new HEAD requires fresh gates; end |
| ASTRA PASS / PASS_WITH_NOTES | `[READY_FOR_MERGE]`; do not merge; end |
| DEVIN/ASTRA DECISION_REQUIRED | Post exact packet to #ai-decisions; stop consequential work; end |
| USER DECISION | Ensure durable GitHub decision pointer; relay exact decision to same Devin ticket; end |
| GROK_QUOTA_UNAVAILABLE | `[BLOCKED] Reason: GROK_QUOTA`; fail closed |

## CI failure policy

Grok does not debug CI and does not create a new Devin session for ordinary CI
failure.

Relay only:
- failing check name
- check URL
- exact HEAD SHA
- machine-provided failure pointer

to the SAME Devin ticket, then end.

Devin owns implement → test → fail → debug → fix → retest.

Do not arbitrarily terminate this loop after one or two failures.
If Devin explicitly reports BLOCKED, relay the blocker and apply existing
escalation rules.

## Independent review

Non-A0 code should receive independent read-only review before Astra audit when
a verified reviewer lane is available or project policy requires it.

Preferred:
1. verified CHEAP_WORKER read-only reviewer
2. separately configured read-only reviewer lane

Reviewer must not become a second writer.
FAIL findings return unchanged to the ticket owner.
A changed HEAD requires the applicable gates again.
Independent review never replaces Astra audit.

## Astra audit packet

`[AUDIT_REQUIRED]`

Project:
Task:
Repository:
PR:
Base SHA:
Head SHA:
Objective source:
Authoritative documents:
Existing contracts:
Invariants:
Acceptance criteria:
CI:
Independent review:
Touched areas:
Contract change required:
Known findings:
Depth:

Instruction: `Do not implement fixes.`

Valid result:
PASS | PASS_WITH_NOTES | FAIL | DECISION_REQUIRED

Audit is bound to exact HEAD SHA. HEAD change invalidates the previous final
audit for the new SHA.

## DECISION_REQUIRED packet

Project:
Task:
Current SHA:
Blocking question:
Current authoritative rule:
Why worker is blocked:
Options identified by worker:
Evidence pointers:
Worker status:
Required: ASTRA ANALYSIS + USER DECISION

Grok adds no preferred option.

## States

RECEIVED
ROUTED
RUNNING
BLOCKED
PR_OPEN
CI_FAILED
READY_FOR_REVIEW
REVIEW_FAILED
READY_FOR_AUDIT
AUDIT_FAILED
DECISION_REQUIRED
READY_FOR_MERGE
DONE

One state per task.

## Status prefixes

`[TASK]`
`[STATUS]`
`[BLOCKED]`
`[DECISION_REQUIRED]`
`[DECISION]`
`[AUDIT_REQUIRED]`
`[AUDIT_RESULT]`
`[CI_FAIL]`
`[READY_FOR_REVIEW]`
`[READY_FOR_AUDIT]`
`[READY_FOR_MERGE]`
`[DONE]`

Status body is fields only:

```
[STATUS]
Project:
Task:
State:
Worker:
PR:
HEAD:
CI:
Review:
Audit:
Blocker:
Human action:
```

## Slack

`#ai-control` — explicit control commands/status
`#ai-decisions` — Astra + User consequential decisions
`#ai-audit` — audit request/results
`#film-unit` — project task/status channel

One project → one project channel.
One task → one thread.
Slack is not memory. Durable decisions return to GitHub.

## Cheap vs Cloud Devin

Use cheap/local only when the lane is verified available.
Do not assume a model is free or unlimited.

Cloud Devin:
- long autonomous work
- difficult debugging
- multi-file / multi-system implementation
- isolated iterative development
- end-to-end ticket ownership

Do not spend Cloud Devin on grep, typo, formatting, or status lookup.

## Quota failure

If Grok quota is unavailable:
FAIL CLOSED.
Surface `[BLOCKED] Reason: GROK_QUOTA`.
No substitute dispatcher is appointed automatically.

## Merge

Grok never merges.
Astra PASS is not a merge command.
READY_FOR_MERGE means required gates passed on the current HEAD.
USER makes the merge decision.

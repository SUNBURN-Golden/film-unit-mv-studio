# DISPATCH RUNBOOK v2

This is a deterministic execution contract.
Grok does not extend it mid-session.

NO STANDING ROUTINES.
NO POLLING.
NO BACKGROUND MONITORING.
NO SECOND SEMANTIC REASONING PASS.

## 1. Project map

PROJECT: FILM_UNIT
REPO: `BeautifulMind-JT/film-unit-mv-studio`
DEFAULT_BRANCH: `main`
SLACK_PROJECT: `#film-unit`
SLACK_CONTROL: `#ai-control`
SLACK_DECISIONS: `#ai-decisions`
SLACK_AUDIT: `#ai-audit`

DEFAULT_EXECUTION_CLASS: `DEVIN_STANDARD`
DEFAULT_AUDIT_FLOOR: `A1`
REVIEW_POLICY: `REQUIRED_NON_A0`
REVIEWER_LANE_ID: `CONFIG_REQUIRED`

TASK_SPEC_POLICY: `REPO_DOCS_AND_CANONICAL_ISSUE`
A0_POLICY: `EXPLICIT_AUTHORIZATION_ONLY`
POST_MERGE_POLICY: `NONE_UNLESS_REPO_RULE_REQUIRES`

Production/content approvals are separate from engineering gates.
Lyrics/subtitle timing, LOCK, asset provenance, renderer/budget approval,
build-history/reproducibility and Preview/Final rules remain authoritative.
An engineering PASS does not authorize paid rendering, asset submission, timing
LOCK, or Final production approval.

If PROJECT MAP or required actor/reviewer configuration is missing:
`[BLOCKED] Reason: CONTROL_PLANE_NOT_CONFIGURED`

Do not guess.

## 2. Activation preconditions

Before automation is enabled, the mechanical layer must have:

- configured USER actor identity;
- configured ASTRA actor identity;
- configured Grok/router identity;
- configured Devin/provider identity;
- configured independent reviewer identity/lane;
- per-TASK_KEY single-writer serialization;
- canonical-control-record read/write support;
- self-event filtering;
- provider launch reconciliation or explicit UNKNOWN handling.

Until these exist, these documents are policy only; they do not prove runtime
enforcement.

## 3. Raw event intake

Allowed raw sources:

1. explicit authenticated USER command;
2. configured Slack workflow/command;
3. GitHub webhook / GitHub Actions event;
4. configured worker/reviewer/provider callback;
5. configured ASTRA audit result channel/action.

The mechanical layer rejects:

- unconfigured actors;
- ordinary chat text masquerading as PASS/DECISION;
- router's own status messages as new task triggers;
- events that cannot be mapped to one canonical task;
- duplicate delivery IDs already recorded with the same effect.

A free-form User request without a canonical task record is an intake request,
not a dispatch event. A cheap deterministic intake form/workflow must first
create or identify the canonical GitHub task and task revision.

## 4. Normalized event contract

Only normalized events may invoke Grok.

Every normalized event contains:

EVENT_ID:
EVENT_TYPE:
SOURCE_ACTOR_ID:
SOURCE_POINTER:
REPO:
TASK_ID:
TASK_REVISION:
CANONICAL_TASK_POINTER:
CONTROL_RECORD_POINTER:
ATTEMPT_ID:
PR_POINTER:
HEAD_SHA:
RUN_OR_RESULT_ID:

Fields that do not apply are explicit `N/A`; they are not silently omitted.

Result/gate events that refer to code must carry HEAD_SHA.
Decision events must carry TASK_REVISION and durable decision pointer.

## 5. TASK_KEY and canonical control record

`TASK_KEY = REPO + TASK_ID`

Each TASK_KEY has exactly one canonical control record, durably projected in
the canonical GitHub task/issue at a fixed machine-owned record pointer.

The GitHub projection is durable evidence, not the serialization primitive.

Control record minimum facts:

- TASK_KEY
- TASK_REVISION
- canonical task/spec pointer
- CLAIM_ID
- CLAIM_STATE
- LAUNCH_REQUEST_ID
- LAUNCH_STATE
- ATTEMPT_ID
- OWNER_WORKER
- OWNER_SESSION_ID
- PR_POINTER
- CURRENT_HEAD_SHA
- verification policy and current-head verification facts
- review policy, reviewer lane/session and current-head result
- audit floor, verified audit depth, audited SHA/evidence SHA and result
- unresolved blocker/decision pointer
- merge SHA
- post-merge result/follow-up pointer
- last accepted event IDs

State is derived from these facts. Arrival order does not blindly overwrite
state.

## 6. Per-task serialization

All mutations of one control record occur under a single-writer serialization
primitive keyed by TASK_KEY.

Two concurrent events for the same task must not both observe "unowned" and
launch two writers.

EVENT_ID deduplicates delivery.
TASK_KEY serialization protects job ownership.

Both are required.

## 7. Claim and launch protocol

### 7.1 Existing owner

If OWNER_SESSION_ID exists:
- do not create a new writer;
- route eligible feedback to that owner;
- record the new EVENT_ID as observed;
- end.

This remains true when the incoming EVENT_ID is new.

### 7.2 New owner claim

Under TASK_KEY serialization, if there is no owner and no unresolved launch:

1. create CLAIM_ID;
2. set CLAIM_STATE=`CLAIMED`;
3. create stable LAUNCH_REQUEST_ID;
4. set LAUNCH_STATE=`NOT_STARTED`;
5. persist the control record;
6. emit normalized `DISPATCH_ALLOWED`.

Only the designated launch executor may continue that claim.

### 7.3 Launch

Grok receives `DISPATCH_ALLOWED`, launches exactly the worker/class named in
the canonical envelope, and returns a structured launch receipt containing the
same CLAIM_ID and LAUNCH_REQUEST_ID.

If the provider supports an idempotency key, LAUNCH_REQUEST_ID must be used.

### 7.4 Confirmed success

Mechanical layer records:

LAUNCH_STATE=`CONFIRMED`
OWNER_SESSION_ID=<provider session>
OWNER_WORKER=<worker>
ATTEMPT_ID=<attempt>

Then derived state may become RUNNING.

### 7.5 Confirmed pre-execution failure

If the provider proves that no worker session was created:

LAUNCH_STATE=`FAILED_PRESTART`

A retry requires an explicit retry event/rule and reuses the same task control
record. It does not create a competing owner.

### 7.6 Unknown outcome

If launch may have succeeded but the response is lost/ambiguous:

LAUNCH_STATE=`UNKNOWN`

Do not auto-relaunch.

Reconcile using LAUNCH_REQUEST_ID/provider evidence if the provider supports it.
If existence cannot be determined mechanically, stop and require User
resolution.

UNKNOWN is intentionally safer than duplicate execution.

### 7.7 Crash after claim before launch

If the record proves LAUNCH_STATE=`NOT_STARTED`, the designated launch
executor may resume that exact CLAIM_ID/LAUNCH_REQUEST_ID.

Do not create a new claim.

## 8. Manual dispatch during Grok outage

Grok quota/outage is recorded by the caller/mechanical layer.

Before manual User dispatch, the same control record must be checked.

Manual dispatch is forbidden when:

- OWNER_SESSION_ID exists;
- LAUNCH_STATE=`UNKNOWN`;
- another active claim belongs to a different executor.

A mechanical `MANUAL_CLAIM_ALLOWED` action should reserve the existing task
before User launches Devin manually. The resulting provider session ID must be
written back to the same control record.

No replacement AI dispatcher is appointed.

## 9. Task-envelope use

Grok reads `TASKS/TEMPLATE.md` fields from the canonical task record.

Grok does not:

- write a second task specification;
- paraphrase objectives;
- infer omitted contracts/invariants;
- choose execution class by semantic reading;
- choose audit depth by semantic reading.

If the canonical task omits EXECUTION_CLASS:
use `DEVIN_STANDARD`.

A0/CHEAP requires explicit A0 authorization.

## 10. Writer autonomy and feedback

Normal writer feedback always returns to the same OWNER_SESSION_ID.

CI/review/audit findings do not create a new writer.

Devin owns ordinary implementation/debug/test decisions inside approved
boundaries.

`STALLED` must be explicitly reported by Devin/provider.
`BUDGET_LIMIT_REACHED` may be emitted only by a configured mechanical cost
guard.

Grok does not infer either condition.

## 11. HEAD and task-revision guards

Code-result events are accepted only when their HEAD_SHA equals
CURRENT_HEAD_SHA for the task/PR, except an accepted `HEAD_CHANGED` event that
advances CURRENT_HEAD_SHA.

On accepted HEAD_CHANGED:

- set CURRENT_HEAD_SHA to the new head;
- clear current-head CI/verification facts;
- clear current-head review facts;
- clear current-head audit facts;
- retain historical evidence only as history.

Late results for an older HEAD are recorded as stale evidence and do not
advance gates.

If a consequential User decision changes task scope/contract:
- persist the decision;
- increment TASK_REVISION;
- update the authoritative task/spec pointer as repository policy requires;
- invalidate approvals tied to the older task revision where applicable;
- only then resume the same owner or start an explicitly authorized new attempt.

## 12. Verification gate

A single successful check is never equivalent to CI_GATE_PASS.

The mechanical layer aggregates the complete project policy for CURRENT_HEAD_SHA.

FILM_UNIT verification policy is `CI_REQUIRED`.

For each CURRENT_HEAD_SHA the GitHub Actions workflow
`Compiler regression` must complete successfully, including its configured
matrix jobs.

A success from an older SHA is stale.

Accepted verification facts must include exact HEAD/evidence SHA and run IDs or
local evidence pointers.

CI/verification failure:
- update current-head verification facts;
- emit one normalized failure event for the new gate state;
- Grok relays exact failure pointers to the same owner;
- Grok does not debug;
- repeated identical raw check events do not repeatedly invoke Grok unless gate
  state materially changes.

## 13. Independent review gate

All non-A0 substantive work in this control plane requires independent
read-only review before Astra audit.

After verification gate success, the mechanical layer emits
`REVIEW_DISPATCH_ALLOWED` if no current-head review exists.

Grok launches the configured REVIEWER_LANE_ID in read-only mode and ends.

If the reviewer lane is unavailable:

derived state = `BLOCKED_REVIEW_LANE`

Do not silently skip review.
User may configure/assign another independent read-only reviewer; the reviewer
must not become a writer.

Review result is accepted only when:

- source actor is the configured reviewer;
- TASK_REVISION matches;
- HEAD_SHA/evidence SHA matches current revision;
- reviewer session/attempt ID matches the dispatched review.

Review FAIL:
relay exact findings to the same writer.
A new HEAD invalidates the prior review.

Review PASS:
mechanical layer records the current-head pass and emits
`AUDIT_REQUIRED`.

## 14. Astra audit gate

Grok sends an audit packet to `#ai-audit` only on normalized
`AUDIT_REQUIRED`.

Packet fields:

Project:
Task:
Task revision:
Repository:
PR/evidence pointer:
Base SHA:
Current HEAD/evidence SHA:
Objective/task-spec pointers:
Authoritative document pointers:
Verification facts:
Independent review facts:
Worker-reported touched areas:
Worker-reported contract-change flag:
Audit floor:

Astra independently reads the actual diff/evidence and authoritative docs.

Astra must return:

AUDIT_RESULT: PASS | PASS_WITH_NOTES | FAIL | DECISION_REQUIRED
AUDITED_TASK_REVISION:
AUDITED_HEAD_OR_EVIDENCE_SHA:
VERIFIED_AUDIT_DEPTH:
VERIFIED_TOUCHED_AREAS:
VERIFIED_CONTRACT_CHANGE_REQUIRED:
FINDING_POINTERS:

The mechanical layer accepts an audit result only from the configured ASTRA
actor and only for the current task revision and current head/evidence SHA.

Astra FAIL:
relay findings unchanged to the same writer.

Astra DECISION_REQUIRED:
record blocker and emit normalized decision request.

## 15. Consequential decision gate

A decision request contains:

- TASK_ID / TASK_REVISION;
- blocking question;
- current authoritative rule;
- exact evidence pointers;
- options identified by the worker/Astra, if any.

Grok adds no preferred option.

Only a configured User decision event may authorize the choice.

The decision must be persisted in GitHub/ADR/task authority before the
mechanical layer emits `DECISION_RECORDED`.

Ordinary Slack text is not a decision event.

## 16. A0 final qualification

Before A0 can complete, the mechanical layer checks project-specific objective
rules such as allowed paths and forbidden/locked paths.

A0 also requires the explicit A0 authorization pointer from the task envelope.

If A0 qualification fails:
promote to A1 → independent review → Astra audit.

A0 never bypasses repository-specific evidence/bookkeeping rules.

## 17. Derived states

State is computed from control-record facts, not arrival order.

Allowed derived states:

RECEIVED
CLAIMED
LAUNCH_UNKNOWN
RUNNING
BLOCKED
STALLED
BUDGET_BLOCKED
PR_OPEN
VERIFICATION_FAILED
READY_FOR_REVIEW
REVIEW_RUNNING
REVIEW_FAILED
BLOCKED_REVIEW_LANE
READY_FOR_AUDIT
AUDIT_RUNNING
AUDIT_FAILED
DECISION_REQUIRED
READY_FOR_MERGE
MERGED_POST_VERIFY
POST_MERGE_FAILED
DONE
DONE_NO_CHANGE

Important derivations:

- unresolved decision/blocker outranks progress states;
- LAUNCH_UNKNOWN blocks new launch;
- verification/review/audit facts must match current task revision and head;
- READY_FOR_MERGE is computed, never accepted as arbitrary text.

## 18. READY_FOR_MERGE predicate

For PR deliverables, READY_FOR_MERGE is true only when all are true:

- PR exists and is open;
- CURRENT_HEAD_SHA equals PR current head;
- task revision is current;
- no unresolved blocker/decision;
- verification gate is satisfied for CURRENT_HEAD_SHA;
- required independent review PASS matches CURRENT_HEAD_SHA;
- Astra PASS or PASS_WITH_NOTES matches current task revision and HEAD;
- VERIFIED_AUDIT_DEPTH satisfies the project/audit floor and actual verified
  touched areas;
- VERIFIED_CONTRACT_CHANGE_REQUIRED is NO, or the required User decision has
  been durably recorded and reflected in the current task revision;
- project-specific merge prerequisites are satisfied.

User still makes the merge decision.

## 19. No-change / non-code completion

A writer may return NO_CHANGE only when DELIVERABLE_MODE allows it.

For A1+ NO_CHANGE:
- provide exact evidence/base SHA;
- perform required independent review of the finding/evidence;
- Astra audits the no-change conclusion against that evidence SHA;
- PASS may derive `DONE_NO_CHANGE`;
- no merge is invented.

NON_CODE_EVIDENCE follows the project-specific approval/evidence gates; an
engineering PR gate does not substitute for production/legal/content approval.

## 20. Merge and DONE

On authenticated `PR_MERGED`:

No generic post-merge verification is added by this control plane.
If repository/task production rules require additional verification/LOCK, they
remain required. Otherwise authenticated PR_MERGED may derive DONE.

For projects without post-merge verification, a correctly recorded merge may
derive DONE.

Grok never merges.

## 21. Status/event actions for Grok

Grok handles only these normalized actions:

| Normalized event | One Grok action |
|---|---|
| DISPATCH_ALLOWED | launch designated writer; return launch receipt; end |
| WRITER_FEEDBACK_REQUIRED | relay exact pointer to existing owner; end |
| REVIEW_DISPATCH_ALLOWED | launch configured read-only reviewer; return receipt; end |
| AUDIT_REQUIRED | post exact audit packet to #ai-audit; end |
| DECISION_REQUIRED | post exact decision packet to #ai-decisions; end |
| BLOCKED_STATUS | post one short fields-only status; end |
| READY_FOR_MERGE | post fields-only status; end |
| DONE / DONE_NO_CHANGE | post fields-only status; end |

All other state aggregation is mechanical-layer work.

## 22. Slack

`#ai-control` — explicit control-plane commands/status
`#ai-decisions` — authenticated User/Astra decision work
`#ai-audit` — authenticated Astra audit request/result
`#film-unit` — project task/status cockpit

One task → one thread.

Do not subscribe Grok to every Slack message.
Only explicit commands or normalized workflow events invoke Grok.

Slack decisions/audits are not durable until the corresponding GitHub control
record/decision/audit pointer is written.

## 23. Credentials

Target logical permissions:

- Grok router: repo/PR/check read + narrowly scoped task comment/status relay;
  no source write, PR creation, admin, secrets, delete, merge.
- Devin owner: assigned repo + task branch/PR only; no merge/admin.
- Cheap writer: only explicitly assigned branch/task.
- Reviewer: repo/PR read + finding comment only; no source write.
- Astra: repo/PR read + audit/decision evidence write only; no source write/merge.
- Mechanical layer: event validation + control-record/claim/status mutation
  only; no source write/merge.
- User: final authority.

Technical enforcement is separate from this document and must be verified
before claiming least privilege is enforced.

## 24. Cost discipline

Mechanical layer deduplicates raw repeats before Grok invocation.
Only state transitions that require routing wake Grok.
CI matrix/check chatter does not wake Grok check-by-check.
Grok never waits for completion and never polls.

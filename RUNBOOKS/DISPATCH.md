# DISPATCH RUNBOOK v3

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

OPERATING_MODE: `MANUAL_ONLY`
AUTOMATED_ACTION_ADAPTER: `MECHANICAL`
GROK_EVENT_OVERRIDES: `NONE`

DEFAULT_EXECUTION_CLASS: `BUILDER_STANDARD`
DEFAULT_BUILDER_ID: `CONFIG_REQUIRED`
DEFAULT_AUDIT_FLOOR: `A1`
DEFAULT_ASTRA_GATE: `NONE`
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
- configured action-adapter identities (Grok only for explicit overrides);
- configured identity/adapter for every enabled builder provider;
- configured independent reviewer identity/lane;
- per-TASK_KEY single-writer serialization;
- canonical-control-record read/write support;
- self-event filtering;
- provider launch reconciliation or explicit UNKNOWN handling.

The implementation must receive an independent exact-SHA audit and explicit
User activation after these checks. Documentation approval does not enable it.
Until then the manual protocol in section 8 applies.

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

Only normalized events may invoke external action adapters. Grok itself is not
the event bus and is not required for deterministic workflow transitions.

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

Fields that do not apply are explicit N/A; they are not silently omitted.

Result/gate events that refer to code must carry HEAD_SHA.
Decision events must carry TASK_REVISION and durable decision pointer.

Grok may receive only explicit authenticated User commands or normalized
one-shot relay actions. Builder/reviewer selection comes from canonical
configuration, never from Grok semantic judgment.

## 5. TASK_KEY and canonical control record

`TASK_KEY = REPO + TASK_ID`

Each TASK_KEY has exactly one canonical control record, durably projected in
the canonical GitHub task/issue at a fixed machine-owned record pointer.

The GitHub projection is durable evidence, not the serialization primitive.

Control record minimum facts:

- TASK_KEY
- TASK_REVISION
- canonical task/spec pointer
- CANONICAL_SLACK_THREAD (N/A until linked)
- CONTROL_RECORD_VERSION and pending action/delivery IDs
- CLAIM_ID
- CLAIM_STATE
- LAUNCH_REQUEST_ID
- LAUNCH_STATE
- ATTEMPT_ID
- OWNER_WORKER / BUILDER_ID
- OWNER_SESSION_ID
- PR_POINTER
- CURRENT_HEAD_SHA
- verification policy and current-head verification facts
- review policy, configured audit floor, REVIEW_REQUEST_ID, REVIEW_LAUNCH_STATE,
  reviewer lane/session, review attempt ID, VERIFIED_REVIEW_DEPTH,
  VERIFIED_REQUIRED_DEPTH, EFFECTIVE_AUDIT_FLOOR, verified touched areas,
  contract-change flag and current-head result
- ASTRA_GATE, AUDIT_REQUEST_ID, AUDIT_ATTEMPT_ID, AUDIT_REQUEST_STATE,
  ACCEPTED_AUDITOR_IDENTITY, AUDITOR_DESIGNATION_POINTER, verified audit depth,
  audited SHA/evidence SHA and result when an Astra gate applies
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

Under TASK_KEY serialization:
- Existing active owner: relay eligible feedback to that owner; never launch another.
- Existing unresolved claim/request: reuse it; never create a competing claim.
- Otherwise persist CLAIM_ID, designated executor, stable LAUNCH_REQUEST_ID,
  LAUNCH_STATE=NOT_STARTED and a durable pending dispatch action together.

Before any external launch, atomically consume that action and persist
LAUNCH_STATE=SUBMITTING. Only the executor holding that permission may send.
NOT_STARTED therefore proves no send was authorized; resume only the same request.
SUBMITTING after a crash is potentially sent: reconcile or mark UNKNOWN; never
automatically launch again. Timeout/claim expiry alone does not authorize takeover.

Use LAUNCH_REQUEST_ID as the provider idempotency key when supported.
Confirmed receipt records CONFIRMED + OWNER_SESSION_ID + worker/attempt.
Provider proof of no session permits FAILED_PRESTART; a configured retry event
may reauthorize that same task only after the prior executor is fenced.
Ambiguous outcome records UNKNOWN and blocks relaunch until provider evidence
or explicit User resolution proves the safe next step. Slack response loss
never undoes a confirmed GitHub owner record.

State mutation and pending-action persistence must be atomic in the serialized
store. GitHub holds the durable projection; projection failure blocks further
launches until reconciled. Pending NOT_STARTED actions may be resumed by an
explicit recovery event, not polling. This is not a claim of exactly-once
external execution.

## 8. Manual dispatch and outage

OPERATING_MODE=MANUAL_ONLY until the activation gate passes. All automatic
dispatchers are disabled. User is the sole launch executor and control-record
writer in this mode; a comment alone is not a concurrent lock.

Before direct User → designated-builder dispatch:

1. identify canonical task/revision, configured BUILDER_ID and existing owner/request;
2. reuse an active owner; block if any unresolved SUBMITTING/UNKNOWN request exists;
3. durably reserve CLAIM_ID/LAUNCH_REQUEST_ID and record SUBMITTING before launch;
4. send only the task pointer/revision to the configured builder adapter and
   record the resulting provider/session ID.

If the GitHub reservation cannot be recorded, do not launch. Response loss
requires reconciliation, not a second session. Do not enable automation while
a manual claim/send is unresolved.

In automated mode manual dispatch requires a serialized MANUAL_CLAIM_ALLOWED
action and the same launch protocol. Outage does not bypass ownership.

Grok outage is not a workflow outage: the same fixed mechanical command may be
invoked by another authenticated caller. Provider outage for the assigned
builder is recorded as a blocker; the control plane must not silently select a
different builder. Reassignment requires an authorized control-record update
and fencing/reconciliation of the prior attempt.

## 9. Task-envelope use

The mechanical layer reads TASKS/TEMPLATE.md fields from the canonical task
record. Grok may relay a pointer but does not semantically interpret the task.

No actor in the dispatch path may:

- write a second task specification;
- paraphrase objectives as a new authority;
- infer omitted contracts/invariants;
- choose execution class, builder, reviewer, audit depth or Astra gate by
  semantic reading.

If the canonical task omits EXECUTION_CLASS, use BUILDER_STANDARD.
A BUILDER_STANDARD task without a configured BUILDER_ID is blocked.
A0/CHEAP requires explicit A0 authorization.

## 10. Writer autonomy and feedback

No routine Astra preflight, plan approval or progress review is required for
an already authorized task. The assigned builder investigates and selects
ordinary implementation details within approved contracts. CI failure has no
arbitrary two-attempt cutoff.

Normal writer feedback always returns to the same OWNER_SESSION_ID.
CI/review/Astra-gate findings do not create a new writer.

The assigned builder owns ordinary implementation/debug/test decisions inside
approved boundaries.

STALLED must be explicitly reported by the builder/provider.
BUDGET_LIMIT_REACHED may be emitted only by a configured mechanical cost guard.
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

HEAD_CHANGED is accepted only after reading the live PR head under task
serialization; delayed events cannot restore an old head. PR_OPEN may bind
the authenticated owner's PR before WRITER_DONE arrives. All result events
must match TASK_REVISION and the current request/run attempt, not only SHA.
A merged/terminal task cannot resume its writer from late pre-merge events.
Post-merge verification uses MERGE_SHA and the post-merge phase, not PR HEAD.

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
- The configured adapter relays exact failure pointers to the same owner;
- Grok does not debug;
- repeated identical raw check events do not repeatedly invoke Grok unless gate
  state materially changes.

## 13. Independent review gate

Reviewer sends use the same NOT_STARTED → SUBMITTING →
CONFIRMED/UNKNOWN protocol as writer launches, keyed by
REVIEW_REQUEST_ID/REVIEW_ATTEMPT_ID. Persist the pending action with the
request; a crash is not permission to resend.

Review dispatch is idempotent and serialized.

Under TASK_KEY serialization, before emitting a review request for the current
task revision + HEAD/evidence SHA:

1. if a matching accepted review result already exists, do not launch another reviewer;
2. if a matching REVIEW_REQUEST_ID/session already exists, reuse it;
3. otherwise create stable REVIEW_REQUEST_ID and REVIEW_ATTEMPT_ID;
4. set REVIEW_LAUNCH_STATE=NOT_STARTED;
5. persist the control record;
6. emit exactly one REVIEW_DISPATCH_ALLOWED.

The configured adapter launches REVIEWER_LANE_ID in read-only mode.
The reviewer must not have authored or modified the reviewed change. A peer
builder is eligible only when it is acting in a distinct non-author review
session and has no write role on that task.

If reviewer launch may have succeeded but the outcome is ambiguous, set
REVIEW_LAUNCH_STATE=UNKNOWN and do not auto-launch another reviewer.

All non-A0 substantive work requires independent read-only review unless a
stricter repository rule applies.

The reviewer must inspect the actual diff/evidence and return, for the exact
task revision and HEAD/evidence SHA:

REVIEW_RESULT: PASS | PASS_WITH_NOTES | FAIL | DECISION_REQUIRED
REVIEWER_IDENTITY_OR_SESSION:
REVIEWED_TASK_REVISION:
REVIEWED_HEAD_OR_EVIDENCE_SHA:
VERIFIED_REVIEW_DEPTH:
VERIFIED_REQUIRED_DEPTH: A1 | A2 | A3
VERIFIED_TOUCHED_AREAS:
VERIFIED_CONTRACT_CHANGE_REQUIRED:
FINDING_POINTERS:

VERIFIED_REQUIRED_DEPTH is the reviewer's semantic classification of the
minimum depth required by the actual diff and authoritative repository rules.
It is independent of the task's predeclared AUDIT_FLOOR.

After accepting the current-head review, the mechanical layer computes:

EFFECTIVE_AUDIT_FLOOR =
  max(configured AUDIT_FLOOR, VERIFIED_REQUIRED_DEPTH)

using A0 < A1 < A2 < A3.

If EFFECTIVE_AUDIT_FLOOR=A3, the control record must set
ASTRA_GATE=ARCHITECTURE for this task revision/current HEAD before any merge
predicate is reevaluated. A predeclared NONE/MILESTONE gate cannot suppress
this promotion. If repository rules require a stronger explicit gate, keep the
stronger gate.

PASS_WITH_NOTES cannot hide an unresolved correctness, contract, invariant,
security or acceptance failure.

Review result is accepted only from the configured reviewer and matching
request/session. A new relevant HEAD invalidates the prior review and its
derived EFFECTIVE_AUDIT_FLOOR.

FAIL:
relay exact findings to the same writer.

DECISION_REQUIRED or VERIFIED_CONTRACT_CHANGE_REQUIRED=YES:
record the blocker and emit the required Astra/decision path; do not derive
READY_FOR_MERGE.

PASS/PASS_WITH_NOTES with no consequential contract change:
- if EFFECTIVE_AUDIT_FLOOR=A3 or another Astra gate is required, emit AUDIT_REQUIRED;
- otherwise reevaluate READY_FOR_MERGE without invoking Astra.

## 14. Astra gate

Astra is not the default routine A1/A2 reviewer.

This section applies only when at least one is true:

- EFFECTIVE_AUDIT_FLOOR=A3;
- ASTRA_GATE is MILESTONE, ARCHITECTURE or RELEASE;
- an architecture exception or consequential contract-change question requires
  Astra analysis;
- a repository-specific authoritative rule explicitly requires Astra.

EFFECTIVE_AUDIT_FLOOR=A3 implies ASTRA_GATE=ARCHITECTURE for the current
task revision/current HEAD.

Astra-request delivery uses the same fail-closed send discipline as writer and
reviewer launch and is serialized by task revision + current HEAD/evidence SHA
(or by an explicitly identified milestone/release evidence packet).

Each request is bound at creation to:

AUDIT_REQUEST_ID
AUDIT_ATTEMPT_ID
ACCEPTED_AUDITOR_IDENTITY
AUDITOR_DESIGNATION_POINTER (N/A for configured Astra)
AUDITED_TASK_REVISION_OR_MILESTONE
AUDITED_HEAD_OR_EVIDENCE_SHA

Before emitting AUDIT_REQUIRED, under the applicable serialization key the
mechanical layer:

1. reuses an existing accepted Astra-gate result only when the request identity,
   accepted auditor/designation, task or milestone identity and exact
   HEAD/evidence SHA all still match;
2. if a matching AUDIT_REQUEST_ID/AUDIT_ATTEMPT_ID is already
   NOT_STARTED/SUBMITTING/CONFIRMED/UNKNOWN, reuses/reconciles it instead of
   creating a second request;
   For NOT_STARTED, resume only the existing pending action; never create
   another request/attempt or enqueue another delivery;
3. otherwise creates stable AUDIT_REQUEST_ID and AUDIT_ATTEMPT_ID, binds the
   accepted auditor identity and designation pointer, sets
   AUDIT_REQUEST_STATE=NOT_STARTED, and atomically persists the pending action;
4. immediately before external send, atomically consumes that pending action
   and persists AUDIT_REQUEST_STATE=SUBMITTING;
5. only the executor holding that consumed action may send the audit request;
6. confirmed receipt records AUDIT_REQUEST_STATE=CONFIRMED.

If the process crashes or response is lost after SUBMITTING, the request is
potentially sent. Reconcile that same request; do not create or resend another
request merely because no receipt was observed. If outcome cannot be proven,
set AUDIT_REQUEST_STATE=UNKNOWN and require reconciliation or explicit User
resolution.

The packet contains exact task/milestone identity, revision, repository,
PR/evidence pointers, base/current SHA where applicable, authoritative
documents, verification facts, independent-review facts,
VERIFIED_REQUIRED_DEPTH, EFFECTIVE_AUDIT_FLOOR, reported touched
areas/contract-change flag and ASTRA_GATE.

Astra independently reads the actual relevant diff/evidence and authoritative
contracts. The accepted Astra-gate result contains:

AUDIT_REQUEST_ID:
AUDIT_ATTEMPT_ID:
AUDIT_RESULT: PASS | PASS_WITH_NOTES | FAIL | DECISION_REQUIRED
AUDITOR_IDENTITY_OR_SESSION:
AUDITOR_DESIGNATION_POINTER: (required when the auditor is not Astra)
AUDITED_TASK_REVISION_OR_MILESTONE:
AUDITED_HEAD_OR_EVIDENCE_SHA:
VERIFIED_AUDIT_DEPTH:
VERIFIED_TOUCHED_AREAS:
VERIFIED_CONTRACT_CHANGE_REQUIRED:
FINDING_POINTERS:

An audit result is accepted only when its authenticated auditor
identity/session matches ACCEPTED_AUDITOR_IDENTITY for that exact outstanding
request, and the designation pointer (when any) is still the same active User
designation bound to the request.

If Astra authored or modified the audited change, User must designate an
independent non-author architecture auditor with a durable scope pointer.
That auditor does not receive Astra's architecture/design authority.

Grok, the author and the mechanical layer may not choose a replacement
architecture auditor by semantic judgment or lower the required gate.

If the accepted auditor designation is revoked, replaced or its scope changes:

- outstanding requests bound to the old designation become invalid;
- prior gate results issued under that designation no longer satisfy current
  READY_FOR_MERGE;
- a new request may be created only after the new designation is durably
  recorded and any prior SUBMITTING/UNKNOWN delivery is safely reconciled.

Self-review by a writer/session that participated in the change is rejected.
A relevant HEAD/task revision change invalidates the prior result.

FAIL returns exact findings to the same writer when a writer fix is appropriate.
DECISION_REQUIRED records a blocker and emits the User decision path.
Architecture decisions remain Astra analysis → User decision → durable pointer.

Re-audit begins with the previous audited SHA/evidence delta and unresolved
findings, then expands as required by affected dependencies/contracts.

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

Before A0 can complete, the mechanical layer checks the task envelope's
explicit A0 authorization and path contract:

- A0_AUTHORIZATION_POINTER exists;
- A0_CHANGE_KIND is one of the allowed A0 enum values;
- actual changed paths are a subset of A0_ALLOWED_PATHS;
- no actual changed path matches A0_FORBIDDEN_PATHS;
- repository locked/sensitive rules are still satisfied.

Grok does not generate or broaden these path lists.

If A0 qualification fails:
promote to A1 → independent review; invoke Astra only if the resulting task requires an Astra gate.

A0 never bypasses repository-specific evidence/bookkeeping rules.

Path checks alone do not prove A0 semantics. Final A0 qualification also
requires an authenticated User attestation of typo/format-only changes for
the exact revision/HEAD, or an approved deterministic transform verifier.
Otherwise promote to A1. For qualified A0 only, separate review and Astra gate are N/A unless repository rules require them; section 18 predicates apply to substantive work. All verification/blocker/merge gates still apply.

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
- for non-A0 substantive work, required independent review PASS or
  PASS_WITH_NOTES matches CURRENT_HEAD_SHA;
- VERIFIED_REVIEW_DEPTH satisfies EFFECTIVE_AUDIT_FLOOR for all non-A3 review
  obligations; an A3 requirement additionally requires the Astra architecture
  gate rather than reviewer depth alone;
- VERIFIED_CONTRACT_CHANGE_REQUIRED from the accepted current-head review is NO,
  or the required Astra/User decision has been durably recorded and reflected
  in the current task revision;
- when EFFECTIVE_AUDIT_FLOOR=A3, ASTRA_GATE is ARCHITECTURE and the accepted
  current Astra-gate PASS/PASS_WITH_NOTES matches the applicable
  revision/head/evidence and VERIFIED_AUDIT_DEPTH satisfies A3;
- for any other explicit Astra gate, its accepted PASS/PASS_WITH_NOTES matches
  the applicable revision/head/evidence and required depth;
- when no Astra gate is required and EFFECTIVE_AUDIT_FLOOR is below A3, absence
  of an Astra result does not block merge;
- any auditor designation bound to the accepted Astra result is still active
  and unchanged;
- project-specific merge prerequisites are satisfied.

User still makes the merge decision.

## 19. No-change / non-code completion

A writer may return NO_CHANGE only when DELIVERABLE_MODE allows it.

For A1+ NO_CHANGE:

- provide exact evidence/base SHA;
- perform required independent review of the finding/evidence;
- if the task requires an Astra gate, obtain that gate for the same evidence identity;
- only then may the mechanical predicates derive DONE_NO_CHANGE.

No merge is invented.

NON_CODE_EVIDENCE follows project-specific approval/evidence gates; an
engineering review or Astra gate does not substitute for
production/legal/content approval.

## 20. Merge and DONE

On authenticated `PR_MERGED`:

No generic post-merge verification is added by this control plane.
If repository/task production rules require additional verification/LOCK, they
remain required. Otherwise authenticated PR_MERGED may derive DONE.

For projects without post-merge verification, a correctly recorded merge may
derive DONE.

Grok never merges.

## 21. Fixed action ownership

ACTION_ADAPTER is configured per event below, not selected by Grok.
Automation defaults to MECHANICAL for every row. A User-approved explicit
mapping may select GROK for a limited launch/relay action; never both.
No configuration silently falls back to another AI on error.

| Normalized event | Action | Default adapter |
|---|---|---|
| DISPATCH_ALLOWED | launch designated owner using section 7; record receipt | MECHANICAL |
| WRITER_FEEDBACK_REQUIRED | exact pointer to existing owner | MECHANICAL |
| REVIEW_DISPATCH_ALLOWED | launch configured read-only reviewer | MECHANICAL |
| AUDIT_REQUIRED | exact audit packet to #ai-audit | MECHANICAL |
| DECISION_REQUIRED | exact decision packet to #ai-decisions | MECHANICAL |
| BLOCKED_STATUS | durable blocker + short status/Human action | MECHANICAL |
| READY_FOR_MERGE | project-thread notification only | MECHANICAL |
| DONE / DONE_NO_CHANGE | project-thread final pointers | MECHANICAL |

When explicitly mapped to GROK: one normalized event, one authorized action,
one receipt, end session. State mutation/aggregation stays mechanical.
In MANUAL_ONLY mode User performs delivery with the same durable facts.

## 22. Slack

`#ai-control` — explicit control-plane commands/status
`#ai-decisions` — authenticated User/Astra decision work
`#ai-audit` — authenticated Astra audit request/result
`#film-unit` — project task/status cockpit

One task → one canonical thread. GitHub task and Slack thread link both ways.
Project statuses are projections of the durable control record, keyed by
CONTROL_RECORD_VERSION; old/repeated projections are suppressed.
Fields: task/revision, state, owner/session, PR/HEAD, verification/review/audit,
blocker pointer, required Human action. Long context stays behind GitHub pointers.
Astra attention is #ai-decisions/#ai-audit, never the project-channel firehose.

Do not subscribe Grok to every Slack message.
Only explicit commands or normalized workflow events invoke Grok.

Slack decisions/audits are not durable until the corresponding GitHub control
record/decision/audit pointer is written.

## 23. Credentials

Target logical permissions:

- Grok command relay: only enough permission to invoke approved control-plane
  commands and relay narrow status; no source write, PR creation, admin,
  secrets, delete or merge.
- DEVIN / GROK_BUILD / GLM builder adapters: only the assigned repo + task
  branch/PR; no merge/admin. Provider credentials may differ, but each active
  writer remains scoped to one canonical task lineage.
- Cheap writer: only explicitly assigned branch/task.
- Reviewer: repo/PR read + finding comment only; no source write.
- Astra: repo/PR read + architecture/audit/decision evidence write only; no
  source write/merge.
- Mechanical layer: event validation + control-record/claim/status mutation and
  configured builder/reviewer launch only; no source write/merge.
- User: final authority.

Technical enforcement is separate from this document and must be verified
before claiming least privilege is enforced.

A shared build host is one security domain: separate worktrees/processes are
not credential isolation. Do not place production/root/payment secrets on that
host merely because builders use different worktrees.

## 24. Cost discipline

Deduplicate raw events and aggregate CI matrices before any AI invocation.
Unchanged state does not produce another status, review or Astra request.

Astra receives architecture exceptions, explicit milestone/release packets and
A3 gate-ready evidence, not routine progress chatter or every A1/A2 PR.
The assigned builder owns repository investigation and the entire
test/fix/retest loop. Independent reviewers own routine non-author review.

No polling, standing sessions or transcript surveillance. Grok should execute
or relay short deterministic commands rather than read project context.

Measure validated completed-task throughput, per-builder cost, Astra usage,
Grok usage, User interventions, review findings, rework and integration
conflicts. Report unavailable usage metrics as unknown.

